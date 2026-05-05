# Per-chat-instances credentials setup

The per-chat-instances feature provisions a real Docker stack + a real
Cloudflare named tunnel + clones a real GitHub repo for every chat.
That requires three things wired into your environment before
`/e2e-pipeline` (or any real chat in container mode) can run:

| # | What | Why | Lives in |
|---|---|---|---|
| 1 | Cloudflare account + API token + zone | Each instance gets its own named tunnel under your zone | `platform_config.cloudflare/settings` |
| 2 | GitHub App + private key | Mints short-lived install tokens so the per-instance container can clone + push | `platform_config.github_app/settings` |
| 3 | A real GitHub repo | The actual code your dev container will clone | `projects.github_repo` |

Total time: **~15 minutes** the first time. After that, any new dev
machine just needs to re-run the seeder script (creds reused).

## TL;DR — the guided seeder

The friendliest path:

```bash
python3 scripts/seed_platform_creds.py
```

This walks you through every value, validates each one against the
real Cloudflare + GitHub API before writing, and never logs your
secrets. Everything below is the manual version + the explanation
of *why* each value is needed.

---

## Blocker 1 — Cloudflare (account, zone, API token)

### What you need

| Field | Example | Where to find it |
|---|---|---|
| `account_id` | `f1234567890abcdef1234567890abcde` | Right sidebar on any zone overview page |
| `zone_id` | `9876543210fedcba9876543210fedcba` | Right sidebar of the **specific zone** you'll use |
| `zone_domain` | `apps.example.com` | A subdomain you control under the zone — every chat gets `<chat-id>.apps.example.com` |
| `api_token` | `ABcd1234EFgh5678IJkl9012MNop3456QRst7890` | Created in step 1.3 below |

### 1.1 — Pick the zone

You need a domain (or subdomain of one) that Cloudflare hosts your
DNS for. If you don't already have one in Cloudflare:

1. Buy a cheap domain (`example.dev` is ~$10/yr at most registrars).
2. Add it to Cloudflare (free plan is fine): https://dash.cloudflare.com → "Add a Site".
3. Cloudflare gives you two nameservers; set them at your registrar.
4. Wait ~10 min for propagation.

Once the zone shows status "Active" in Cloudflare, you're ready.

### 1.2 — Copy `account_id` and `zone_id`

1. Open https://dash.cloudflare.com.
2. Click your domain.
3. Scroll down on the **Overview** page; right sidebar shows:
   ```
   API
   Zone ID    9876543210fedcba9876543210fedcba   [Click to copy]
   Account ID f1234567890abcdef1234567890abcde   [Click to copy]
   ```
4. Save both somewhere temporary.

### 1.3 — Pick `zone_domain`

This is the subdomain pattern your tunnels will use. Recommendation:
use a dedicated subdomain like `apps.<yourdomain>` so dev chats don't
clash with anything you might host directly on the apex.

Examples:
- Domain `example.com` → `zone_domain = "apps.example.com"`
- Domain `mycompany.dev` → `zone_domain = "chats.mycompany.dev"`

You don't need to create the DNS record manually — the orchestrator
creates one per-instance under this subdomain.

### 1.4 — Create the API token

1. Open https://dash.cloudflare.com/profile/api-tokens.
2. Click **Create Token**.
3. Choose **Custom token** → **Get started**.
4. Token name: `tagh-devops-instances` (or whatever).
5. **Permissions** — add these three rows:
   | Section | Resource | Action |
   |---|---|---|
   | Account | Cloudflare Tunnel | Edit |
   | Zone | DNS | Edit |
   | Zone | Zone | Read |
6. **Account Resources**: Include → your specific account.
7. **Zone Resources**: Include → Specific zone → your zone.
8. (Optional) Set TTL (recommend ~1 year).
9. Click **Continue to summary** → **Create Token**.
10. **Copy the token immediately.** Cloudflare shows it ONCE.

### 1.5 — Verify the token works

```bash
TOKEN="<paste here>"
curl -s -H "Authorization: Bearer $TOKEN" \
  https://api.cloudflare.com/client/v4/user/tokens/verify | head
# Should show: "success": true, "result": {"status": "active", ...}
```

### 1.6 — Write the row

Either use the seeder (`scripts/seed_platform_creds.py`) or this
direct INSERT:

```bash
docker compose exec -T postgres psql -U taghdev -d taghdev -c "
INSERT INTO platform_config (category, key, value, is_active)
VALUES ('cloudflare', 'settings', jsonb_build_object(
  'account_id',  '<account_id>',
  'zone_id',     '<zone_id>',
  'zone_domain', '<apps.example.com>',
  'api_token',   '<token>'
), true)
ON CONFLICT (category, key) DO UPDATE SET value = EXCLUDED.value;"
```

---

## Blocker 2 — GitHub credentials (App OR PAT)

### Two supported modes

The system accepts EITHER of two credential shapes — pick one:

| Mode | Best for | Setup time | Token lifetime | Repo coverage |
|---|---|---|---|---|
| **PAT** (Personal Access Token) | Single-developer dev setup | 2 min | Long (you set, up to no-expiry) | Every repo your account can reach |
| **GitHub App** | Multi-tenant production | 8 min | 1 hour, auto-rotated | Per-installation; install on each repo |

For a single-dev local setup, **PAT is fine and faster**. Skip ahead
to "Mode B — Personal Access Token" below.

For production / multi-developer / least-privilege, prefer the App
path — installation tokens are short-lived and scoped per repo, so
the blast radius of a leak is bounded to 1 hour.

The code paths are unified: `services/credentials_service.py::
GitHubAppConfig` accepts either shape; `github_push_token()` returns
the PAT verbatim in PAT mode (no JWT minting, no exchange) and mints
a fresh installation token in App mode. No other code changes.

### Mode A — GitHub App (preferred for multi-tenant)

### What you need

| Field | Example | Where to find it |
|---|---|---|
| `app_id` | `123456` | Shown on the App's settings page after creation |
| `private_key_pem` | `-----BEGIN RSA PRIVATE KEY-----\n...` | Downloaded as a `.pem` file when you generate it |

### 2.1 — Create the GitHub App

1. Open https://github.com/settings/apps.
2. Click **New GitHub App**.
3. Fill in:
   - **GitHub App name**: `tagh-devops-<your-handle>` (must be globally unique on GitHub).
   - **Homepage URL**: `http://localhost:8000` (anything works).
   - **Webhook**:
     - Active: **uncheck** (we don't use webhooks for this).
   - **Repository permissions**:
     - Contents: **Read and write**
     - Metadata: **Read-only** (auto-selected)
     - Pull requests: **Read and write**
     - Workflows: **Read and write** (only if your repos have GitHub Actions you want updated)
   - **Where can this GitHub App be installed?**
     - Choose **Any account** (so you can install on personal AND org repos) OR
     - **Only on this account** if it's for your stuff only.
4. Click **Create GitHub App**.

### 2.2 — Copy the App ID

Right after creation, the page shows:
```
App ID: 123456
```
Save it.

### 2.3 — Generate the private key

1. On the same App settings page, scroll down to **Private keys**.
2. Click **Generate a private key**.
3. Browser downloads a `.pem` file like
   `tagh-devops-yourhandle.2026-04-26.private-key.pem`.
4. Save it somewhere safe (the seeder will read from this path).

### 2.4 — Install the App on your test repo

1. Top-right of the App settings page, click **Public page**.
2. Click **Install** (or **Configure** if you've installed it before).
3. Select **Only select repositories**, pick the repo you'll use for
   the e2e test (the one from Blocker 3).
4. Click **Install**.

### 2.5 — Verify the App works

```bash
APP_ID=123456
KEY_FILE=~/Downloads/tagh-devops-yourhandle.2026-04-26.private-key.pem
# Mint a JWT (10-min lifetime) and list installations:
JWT=$(python3 -c "
import jwt, time
key = open('$KEY_FILE').read()
print(jwt.encode({'iat': int(time.time()), 'exp': int(time.time())+600, 'iss': $APP_ID}, key, algorithm='RS256'))
")
curl -s -H "Authorization: Bearer $JWT" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/app/installations | python3 -m json.tool | head -20
# Should list at least one installation
```

(Requires `pip install pyjwt cryptography` on the host. The seeder
script does this for you.)

### 2.6 — Write the row

Easiest way (handles PEM newlines correctly):

```bash
KEY_FILE=~/Downloads/tagh-devops-yourhandle.2026-04-26.private-key.pem
APP_ID=123456

# Build the JSON in shell so we don't fight psql about escaping:
JSON=$(python3 -c "
import json, sys
print(json.dumps({
  'app_id': '$APP_ID',
  'private_key_pem': open('$KEY_FILE').read(),
}))
")

docker compose exec -T postgres psql -U taghdev -d taghdev -c \
  "INSERT INTO platform_config (category, key, value, is_active) \
   VALUES ('github_app', 'settings', '$JSON'::jsonb, true) \
   ON CONFLICT (category, key) DO UPDATE SET value = EXCLUDED.value;"
```

(Or just run the seeder.)

### Mode B — Personal Access Token (PAT, single-developer)

#### What you need

| Field | Example | Notes |
|---|---|---|
| `pat` | `ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789` | Classic or fine-grained; needs `repo` scope |

#### 2.B.1 — Create the PAT

1. Open https://github.com/settings/tokens (classic) or
   https://github.com/settings/personal-access-tokens (fine-grained).
2. Click **Generate new token (classic)** (recommended for this use case).
3. Note: `tagh-devops-pat`
4. Expiration: pick anything (the rotate cron treats PATs as non-expiring,
   so expiry is your call — recommend 90 days for safety, "No expiration"
   if you're a single developer who doesn't want token renewal hassle).
5. Scopes: tick **`repo`** (full control of private repositories — covers
   read + write to all your repos). That's all the orchestrator needs.
6. Click **Generate token** at the bottom.
7. **Copy the token immediately** — GitHub shows it once.

#### 2.B.2 — Verify the PAT works

```bash
TOKEN="<paste here>"
curl -s -H "Authorization: Bearer $TOKEN" https://api.github.com/user | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f\"login={d.get('login')}, plan={d.get('plan',{}).get('name')}, public_repos={d.get('public_repos')}\")
"
# Should print your login + plan + repo count.
```

(Don't actually run this — the `tagh-devops` skill seeder validates the
PAT for you without exposing it on shell argv.)

#### 2.B.3 — Write the row

Either use the seeder (`scripts/seed_platform_creds.py --only github_app`,
which prompts whether to use App or PAT mode) or directly:

```bash
# Drop the PAT into a gitignored file first (safer than shell argv).
echo "ghp_..." > github_pat.md
chmod 600 github_pat.md
echo "github_pat.md" >> .gitignore

docker compose exec -T postgres psql -U taghdev -d taghdev <<EOF
INSERT INTO platform_config (category, key, value, is_active)
VALUES ('github_app', 'settings', jsonb_build_object('pat', '$(cat github_pat.md)'), true)
ON CONFLICT (category, key) DO UPDATE SET value = EXCLUDED.value;
EOF

# Then delete the file:
rm github_pat.md
```

#### Trade-offs of PAT mode

- **Pro**: 1 token covers every repo your account can access — no
  per-repo install dance.
- **Pro**: 2-minute setup vs ~8 min for App.
- **Con**: longer-lived (your expiry choice). If it leaks, blast
  radius is whatever scope you granted, until you manually revoke.
- **Con**: tied to your user account — if your account loses access
  to a repo, the orchestrator does too.
- **Neutral**: the `rotate_github_token` cron still runs but is a
  no-op (returns the same PAT each rotation). If you want true
  rotation, use App mode.

---

## Blocker 3 — A real GitHub repo on a real project

### What you need

| Field | Example | Notes |
|---|---|---|
| `projects.github_repo` | `yourname/laravel-test-app` | Format `owner/repo`, no `.git`, no `https://` |

The repo must be one the GitHub App from Blocker 2 is **installed
on** (otherwise the install-token mint fails).

### 3.1 — Pick or create a repo

For first run, recommend creating a small public repo with the
Laravel scaffold the compose template expects:

```bash
gh repo create yourname/laravel-test-app --public --clone
cd laravel-test-app
# Optional: pre-populate with `composer create-project laravel/laravel .`
# Or just push a dummy README and let the per-instance container
# scaffold during boot.
git push -u origin main
```

(If you don't have `gh` installed: https://cli.github.com/)

### 3.2 — Confirm the GitHub App is installed on it

Re-run the install flow from step 2.4 if you skipped it. The repo
must show up under "Repository access" in the App settings.

### 3.3 — Update the test project row

Either via the dashboard:
1. Open http://localhost:8000/settings/projects.
2. Click `test-project`.
3. Change the GitHub Repo from `local/test-project` to `yourname/laravel-test-app`.
4. Save.

Or directly via SQL:

```bash
docker compose exec -T postgres psql -U taghdev -d taghdev -c "
UPDATE projects
SET github_repo = 'yourname/laravel-test-app'
WHERE name = 'test-project';"
```

---

## Verify everything

```bash
python3 scripts/e2e/preflight.py | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('Overall:', 'OK' if d['ok'] else 'BLOCKED')
print(f\"Blockers: {d['blocker_count']}\")
for c in d['checks']:
    print(f\"  {c['name']:20s}  {'OK' if c.get('ok') else 'BLOCK'}\")
"
```

Expected: `Overall: OK`, all 7 checks `OK`.

Once green, run `/e2e-pipeline` to drive the real provision through
all 10 phases.

---

## Operating notes

- **You only do this once per environment.** The seeded credentials
  persist in the `platform_config` table; subsequent dev machines that
  share the DB inherit them. A fresh DB needs a fresh seed.
- **Rotation**: when your CF token expires (default 1 year if you
  set TTL), re-run `scripts/seed_platform_creds.py --only cloudflare`
  to overwrite. The instance row's `tunnel_token` is per-tunnel and
  refreshed every provision; the platform-wide token is just used to
  CALL the Cloudflare API to create those tunnels.
- **GitHub App key rotation**: GitHub lets you have up to 25 active
  private keys per App at once. Generate a new one, run
  `--only github_app`, then revoke the old key from the App settings
  page once you've confirmed nothing broke.
- **What the seeder does NOT do**: it doesn't create the Cloudflare
  account, the GitHub App, or the test repo for you. Those are owned
  by you (and by your account, with your billing). It only validates
  + writes credentials you've already obtained.

---

## Why no UI for this yet

`api/routes/settings.py::update_config` whitelists only `("llm",
"chat", "git", "system")` — there's no UI surface for
`cloudflare/settings` or `github_app/settings` today. Filed as
release-blocker product debt in the Phase 0 e2e report; will be
addressed by a dedicated admin-dashboard feature.
