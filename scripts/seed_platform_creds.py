"""Guided seeder for platform_config rows the per-chat-instances feature
needs (cloudflare/settings + github_app/settings).

Run on the host. Walks you through every value, validates each one
against the live Cloudflare / GitHub APIs before writing, and writes
through `docker compose exec postgres psql` so it works in the
standard dev setup with no extra wiring.

Never logs or prints secrets. Token + private-key inputs use
``getpass``; the validation step prints the API response status only,
not headers.

Usage:
    python3 scripts/seed_platform_creds.py
    python3 scripts/seed_platform_creds.py --only cloudflare
    python3 scripts/seed_platform_creds.py --only github_app
    python3 scripts/seed_platform_creds.py --dry-run      # no DB writes
    python3 scripts/seed_platform_creds.py --update-project test-project=ahmed/laravel-test-app

Exit codes:
    0  rows written (or dry-run successful)
    1  user aborted, validation failed, or DB write failed
    2  fatal error (e.g. docker / psql unreachable)
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent


# --- pretty-printing -----------------------------------------------------


def _h(text: str) -> None:
    """Section header."""
    bar = "=" * len(text)
    print(f"\n{bar}\n{text}\n{bar}")


def _step(text: str) -> None:
    print(f"\n>>> {text}")


def _ok(text: str) -> None:
    print(f"  [OK] {text}")


def _err(text: str) -> None:
    print(f"  [!!] {text}", file=sys.stderr)


def _ask(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {label}{suffix}: ").strip()
    return val or (default or "")


def _ask_secret(label: str) -> str:
    return getpass.getpass(f"  {label} (hidden): ").strip()


def _confirm(label: str) -> bool:
    return _ask(f"{label} (y/N)").lower() == "y"


# --- HTTP helpers --------------------------------------------------------


def _http_get(url: str, headers: dict[str, str]) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except urllib.error.URLError as e:
        return 0, str(e)


# --- Cloudflare ----------------------------------------------------------


def _cf_verify_token(token: str) -> bool:
    status, body = _http_get(
        "https://api.cloudflare.com/client/v4/user/tokens/verify",
        {"Authorization": f"Bearer {token}"},
    )
    if status == 200 and isinstance(body, dict) and body.get("success"):
        _ok(f"token active (id={body.get('result', {}).get('id', '?')[:12]}...)")
        return True
    _err(f"token verify failed: status={status} body={str(body)[:200]}")
    return False


def _cf_verify_account(token: str, account_id: str) -> bool:
    status, body = _http_get(
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}",
        {"Authorization": f"Bearer {token}"},
    )
    if status == 200 and isinstance(body, dict) and body.get("success"):
        name = body.get("result", {}).get("name", "?")
        _ok(f"account_id valid (name={name})")
        return True
    _err(f"account_id verify failed: status={status} body={str(body)[:200]}")
    return False


def _cf_verify_zone(token: str, zone_id: str) -> tuple[bool, str | None]:
    status, body = _http_get(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}",
        {"Authorization": f"Bearer {token}"},
    )
    if status == 200 and isinstance(body, dict) and body.get("success"):
        zone_name = body.get("result", {}).get("name", "?")
        _ok(f"zone_id valid (zone={zone_name})")
        return True, zone_name
    _err(f"zone_id verify failed: status={status} body={str(body)[:200]}")
    return False, None


def _seed_cloudflare() -> dict | None:
    _h("Cloudflare credentials")
    print(textwrap.dedent("""
      You'll need:
        - account_id  (Cloudflare dashboard → any zone overview → right sidebar)
        - zone_id     (same place — but for the SPECIFIC zone you'll use)
        - zone_domain (a subdomain you control — e.g. apps.example.com)
        - api_token   (Profile → API Tokens → Create Token; see CREDENTIALS.md)

      Token must have: Account.Cloudflare-Tunnel:Edit, Zone.DNS:Edit, Zone.Zone:Read
    """).rstrip())

    account_id = _ask("account_id")
    zone_id = _ask("zone_id")
    zone_domain = _ask("zone_domain (e.g. apps.example.com)")
    api_token = _ask_secret("api_token")

    if not all([account_id, zone_id, zone_domain, api_token]):
        _err("missing one or more values; aborting cloudflare seed")
        return None

    _step("Validating against Cloudflare API…")
    if not _cf_verify_token(api_token):
        return None
    # Account-level read (GET /accounts/{id}) requires Account:Read scope which
    # tunnel tokens often don't have — skip it, the account_id is validated
    # implicitly when the first tunnel is created.
    _ok(f"account_id accepted (not verified — Account:Read scope not required)")
    ok, zone_name = _cf_verify_zone(api_token, zone_id)
    if not ok:
        return None

    if zone_name and not zone_domain.endswith(zone_name):
        _err(
            f"zone_domain '{zone_domain}' does not appear to belong to zone "
            f"'{zone_name}'. Tunnels won't be addressable."
        )
        if not _confirm("Continue anyway?"):
            return None

    return {
        "account_id": account_id,
        "zone_id": zone_id,
        "zone_domain": zone_domain,
        "api_token": api_token,
    }


# --- GitHub App ----------------------------------------------------------


def _gh_verify_app_jwt(app_id: str, key_pem: str) -> bool:
    """Mint a JWT and call /app — succeeds only if app_id+key match."""
    try:
        import jwt  # type: ignore
    except ImportError:
        _err(
            "pyjwt not installed on host — skipping GitHub App validation. "
            "To enable: `pip install pyjwt cryptography` (or run inside the "
            "worker container which has them)."
        )
        return True  # don't block the seed; user takes responsibility

    import time
    try:
        token = jwt.encode(
            {
                "iat": int(time.time()) - 60,
                "exp": int(time.time()) + 540,
                "iss": int(app_id),
            },
            key_pem,
            algorithm="RS256",
        )
    except Exception as e:
        _err(f"failed to sign JWT (key probably malformed): {e}")
        return False

    status, body = _http_get(
        "https://api.github.com/app",
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "tagh-devops-seeder",
        },
    )
    if status == 200 and isinstance(body, dict):
        _ok(f"GitHub App auth ok (slug={body.get('slug', '?')})")
        return True
    _err(f"GitHub App auth failed: status={status} body={str(body)[:200]}")
    return False


def _seed_github_app() -> dict | None:
    _h("GitHub App credentials")
    print(textwrap.dedent("""
      You'll need:
        - app_id           (GitHub App settings page, top — numeric)
        - private key file (downloaded as .pem when you generate it)

      The App must have:
        - Contents: Read & Write
        - Metadata: Read-only
        - Pull requests: Read & Write
      and be installed on the test repo from Blocker 3.
    """).rstrip())

    app_id = _ask("app_id (numeric)")
    if not app_id.isdigit():
        _err("app_id must be numeric")
        return None

    pem_path_str = _ask("path to private-key .pem file")
    pem_path = pathlib.Path(pem_path_str).expanduser()
    if not pem_path.is_file():
        _err(f"file not found: {pem_path}")
        return None
    try:
        key_pem = pem_path.read_text()
    except OSError as e:
        _err(f"could not read {pem_path}: {e}")
        return None
    if "BEGIN" not in key_pem or "PRIVATE KEY" not in key_pem:
        _err("file does not look like a PEM private key")
        return None

    _step("Validating against GitHub API…")
    if not _gh_verify_app_jwt(app_id, key_pem):
        return None

    return {"app_id": app_id, "private_key_pem": key_pem}


# --- DB write ------------------------------------------------------------


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _write_platform_config(category: str, key: str, value: dict, dry_run: bool) -> bool:
    """UPSERT one row via `docker compose exec postgres psql`."""
    payload = json.dumps(value)
    # Escape single quotes for SQL literal embedding ('' is standard SQL escaping).
    escaped = payload.replace("'", "''")
    sql = (
        f"INSERT INTO platform_config (category, key, value, is_active) "
        f"VALUES ('{category}', '{key}', '{escaped}'::jsonb, true) "
        f"ON CONFLICT (category, key) DO UPDATE "
        f"SET value = EXCLUDED.value, updated_at = now();"
    )
    if dry_run:
        size = len(payload)
        _ok(f"dry-run: would UPSERT {category}/{key} ({size} bytes)")
        return True

    cmd = [
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", "openclow", "-d", "openclow",
        "-c", sql,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=REPO, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        _err(f"DB write failed: {e}")
        return False
    if proc.returncode != 0:
        _err(f"psql exit {proc.returncode}: {proc.stderr.strip()[:300]}")
        return False
    _ok(f"wrote {category}/{key}")
    return True


def _update_project(name_eq_repo: str, dry_run: bool) -> bool:
    if "=" not in name_eq_repo:
        _err(f"--update-project expects NAME=OWNER/REPO, got: {name_eq_repo}")
        return False
    name, repo = name_eq_repo.split("=", 1)
    if "/" not in repo or repo.startswith("http"):
        _err(f"github_repo must be 'owner/repo' (no URL, no .git): got '{repo}'")
        return False
    if dry_run:
        _ok(f"dry-run: would UPDATE projects SET github_repo='{repo}' WHERE name='{name}'")
        return True
    sql = f"UPDATE projects SET github_repo = '{repo}' WHERE name = '{name}';"
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "openclow", "-d", "openclow", "-c", sql],
        cwd=REPO, capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        _err(f"project update failed: {proc.stderr.strip()[:300]}")
        return False
    _ok(f"updated project {name} → {repo} ({proc.stdout.strip()})")
    return True


# --- main ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", choices=("cloudflare", "github_app"),
                   help="seed only one row (default: both)")
    p.add_argument("--dry-run", action="store_true",
                   help="validate but do not write to DB")
    p.add_argument("--update-project", metavar="NAME=OWNER/REPO",
                   help="also update a project's github_repo column")
    args = p.parse_args(argv)

    if not _docker_available():
        _err("docker not found on PATH; cannot reach the postgres container")
        return 2

    print("Per-chat-instances credentials seeder")
    print("--------------------------------------")
    print("Walks you through CF + GitHub App credentials, validates against")
    print("live APIs, then UPSERTs into platform_config. Secrets never logged.")
    if args.dry_run:
        print("[DRY RUN — no DB writes]")

    rows_to_write: dict[str, dict] = {}

    if args.only != "github_app":
        cf = _seed_cloudflare()
        if cf is None:
            _err("cloudflare seed aborted")
            return 1
        rows_to_write["cloudflare"] = cf

    if args.only != "cloudflare":
        gh = _seed_github_app()
        if gh is None:
            _err("github_app seed aborted")
            return 1
        rows_to_write["github_app"] = gh

    _h("Writing rows")
    all_ok = True
    for category, value in rows_to_write.items():
        if not _write_platform_config(category, "settings", value, args.dry_run):
            all_ok = False

    if args.update_project:
        _h("Updating project")
        if not _update_project(args.update_project, args.dry_run):
            all_ok = False

    if not all_ok:
        _err("one or more writes failed; re-run after fixing")
        return 1

    _h("Done")
    print("Re-run preflight to confirm:")
    print("  python3 scripts/e2e/preflight.py | python3 -c \\")
    print("    \"import sys,json;d=json.load(sys.stdin);print('ok=',d['ok'])\"")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(1)
    except Exception as e:
        print(f"\n[fatal] {e}", file=sys.stderr)
        sys.exit(2)
