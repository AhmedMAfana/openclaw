"""Preflight readiness check for the /e2e-pipeline skill.

Emits a single JSON object on stdout. Exit code:
  0  → all green, ok to proceed
  1  → one or more BLOCKER conditions; do not start the live test
  2  → script crashed (config DB unreachable etc.)

The skill consumes the JSON to decide what to test and what to skip.
This script is read-only: it inspects state, never mutates.

Probes:

  services        — `docker compose ps` for api, worker, postgres, redis
  cloudflare      — platform_config row for `cloudflare/settings` exists +
                    api_token looks well-formed
  github          — platform_config row for `github_app/settings` exists +
                    at least one project with mode='container' and a
                    repo_url
  mcp_playwright  — `playwright-mcp` binary reachable inside the worker
  compose_template— setup/compose_templates/laravel-vue/{compose.yml,
                    cloudflared.yml,project.yaml,vite.config.js,nginx.conf}
                    all exist
  api_reachable   — http://localhost:8000/health returns 200
  fitness_audit   — pipeline_fitness.py --fail-on high exit code
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO / "src" / "taghdev" / "setup" / "compose_templates" / "laravel-vue"
TEMPLATE_FILES = ("compose.yml", "cloudflared.yml", "project.yaml",
                  "vite.config.js", "guide.md", "_variant.sh")
# Note: nginx.conf is NOT required — the serversideup/php-alpine base
# image ships its own nginx config (s6-supervised). The historical
# `_copy_template_support_files` reference to nginx.conf is a no-op
# (skipped when the file doesn't exist). _variant.sh IS required:
# it's the dispatcher invoked by every variant-aware guide.md step.


def _result(name: str, ok: bool, **kw):
    return {"name": name, "ok": ok, **kw}


def _check_services() -> dict:
    """`docker compose ps` for the four core services."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=REPO, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return _result("services", False, error=str(e))
    if proc.returncode != 0:
        return _result("services", False, error=proc.stderr.strip()[:300])

    states: dict[str, str] = {}
    for line in proc.stdout.strip().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        states[row.get("Service", "?")] = row.get("State", "?")

    required = ("api", "worker", "postgres", "redis")
    missing = [s for s in required if states.get(s) != "running"]
    return _result(
        "services",
        ok=not missing,
        states=states,
        missing=missing,
        hint=("docker compose up -d " + " ".join(missing)) if missing else None,
    )


def _check_api_reachable() -> dict:
    """Hit /health on the public api port."""
    url = os.environ.get("API_BASE", "http://localhost:8000") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            ok = resp.status == 200
            return _result("api_reachable", ok, url=url, status=resp.status)
    except Exception as e:
        return _result("api_reachable", False, url=url, error=str(e)[:200])


def _check_platform_config() -> dict:
    """Query platform_config for cloudflare + github_app rows.

    Uses `docker compose exec postgres psql` rather than importing the
    app — keeps this script standalone, no Python deps to align.
    """
    # github_app accepts either shape — encoded as a tuple-of-tuples.
    # The check passes if AT LEAST ONE shape is fully satisfied.
    SHAPE_GH_APP = (("app_id", "private_key_pem"), ("pat",))
    out = {"name": "platform_config", "ok": True, "rows": {}}
    for category, key, must_have in (
        ("cloudflare", "settings", ("account_id", "zone_id", "api_token")),
        ("github_app", "settings", SHAPE_GH_APP),
    ):
        try:
            proc = subprocess.run(
                [
                    "docker", "compose", "exec", "-T", "postgres",
                    "psql", "-U", "taghdev", "-d", "taghdev",
                    "-At", "-c",
                    f"SELECT value::text FROM platform_config "
                    f"WHERE category='{category}' AND key='{key}';",
                ],
                cwd=REPO, capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            out["ok"] = False
            out["rows"][f"{category}/{key}"] = {"ok": False, "error": str(e)}
            continue
        raw = proc.stdout.strip()
        if not raw:
            out["ok"] = False
            out["rows"][f"{category}/{key}"] = {
                "ok": False,
                "error": "row missing",
                "hint": (
                    f"seed via the dashboard at /admin/platform-config "
                    f"with category={category} key={key}"
                ),
            }
            continue
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            blob = {}
        # Single-shape vs alt-shapes branching.
        if must_have and isinstance(must_have[0], tuple):
            # Alt-shapes: pass if ANY shape is fully satisfied.
            ok = any(all(blob.get(k) for k in shape) for shape in must_have)
            missing = [] if ok else [
                f"need EITHER {' + '.join(s)}" for s in must_have
            ]
            present = sorted(k for k in blob if blob.get(k))
            mode = (
                "pat" if blob.get("pat")
                else "app" if blob.get("app_id") and blob.get("private_key_pem")
                else "?"
            )
            out["rows"][f"{category}/{key}"] = {
                "ok": ok,
                "mode": mode,
                "missing_fields": missing,
                "present_fields": present,
            }
        else:
            missing = [k for k in must_have if not blob.get(k)]
            ok = not missing
            out["rows"][f"{category}/{key}"] = {
                "ok": ok,
                "missing_fields": missing,
                "present_fields": sorted(k for k in blob if blob.get(k)),
            }
        if not ok:
            out["ok"] = False
    return out


def _check_container_project() -> dict:
    """At least one project: mode='container', status='active',
    github_repo IS NOT NULL.
    """
    try:
        proc = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "psql", "-U", "taghdev", "-d", "taghdev",
                "-At", "-c",
                "SELECT id, name, github_repo FROM projects "
                "WHERE mode='container' AND status='active' "
                "AND github_repo IS NOT NULL ORDER BY name LIMIT 5;",
            ],
            cwd=REPO, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return _result("container_project", False, error=str(e))
    rows = [r for r in proc.stdout.strip().splitlines() if r]
    parsed = []
    for r in rows:
        parts = r.split("|")
        if len(parts) >= 3:
            parsed.append({"id": parts[0], "name": parts[1], "github_repo": parts[2]})
    return _result(
        "container_project",
        ok=bool(parsed),
        candidates=parsed,
        hint=(
            "open the dashboard, add a project with mode=container and a "
            "GitHub repo" if not parsed else None
        ),
    )


def _check_mcp_playwright() -> dict:
    """`playwright-mcp` binary reachable inside the worker container."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "exec", "-T", "worker",
             "which", "playwright-mcp"],
            cwd=REPO, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return _result("mcp_playwright", False, error=str(e))
    path = proc.stdout.strip()
    return _result(
        "mcp_playwright",
        ok=bool(path) and proc.returncode == 0,
        path=path,
        hint=("rebuild the worker image; entrypoint should symlink "
              "/usr/local/bin/playwright-mcp") if not path else None,
    )


def _check_compose_template() -> dict:
    if not TEMPLATE_DIR.is_dir():
        return _result("compose_template", False,
                       error=f"missing dir {TEMPLATE_DIR}")
    missing = [f for f in TEMPLATE_FILES if not (TEMPLATE_DIR / f).is_file()]
    return _result(
        "compose_template",
        ok=not missing,
        dir=str(TEMPLATE_DIR.relative_to(REPO)),
        missing=missing,
    )


def _check_fitness() -> dict:
    """Run the static fitness suite as a gate. Don't fail on the missing
    `pipeline_fitness.py` — just report it."""
    runner = REPO / "scripts" / "pipeline_fitness.py"
    if not runner.is_file():
        return _result("fitness_audit", False,
                       error=f"missing {runner}")
    try:
        proc = subprocess.run(
            [sys.executable, str(runner), "--fail-on", "high", "--json"],
            cwd=REPO, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return _result("fitness_audit", False, error=str(e))
    return _result(
        "fitness_audit",
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        hint=("run /pipeline-audit to see findings"
              if proc.returncode != 0 else None),
    )


def main() -> int:
    checks = [
        _check_services(),
        _check_api_reachable(),
        _check_platform_config(),
        _check_container_project(),
        _check_mcp_playwright(),
        _check_compose_template(),
        _check_fitness(),
    ]
    blockers = [c for c in checks if not c.get("ok")]
    out = {
        "ok": not blockers,
        "blocker_count": len(blockers),
        "checks": checks,
    }
    print(json.dumps(out, indent=2, default=str))
    return 0 if not blockers else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(json.dumps({"ok": False, "fatal": str(e)}, indent=2))
        sys.exit(2)
