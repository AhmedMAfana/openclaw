# Contract: Internal Heartbeat API

**Audience**: `projctl` (caller, inside an instance) + orchestrator FastAPI (server, in the control plane).
**Authentication**: HMAC-SHA256. No bearer tokens, no JWTs. See [research.md §8](../research.md#8-heartbeat-authentication).

---

## `POST /internal/instances/<slug>/heartbeat`

Called by `projctl heartbeat` every 60 seconds while any of (a) the Vite dev server is running, (b) a task is executing, (c) a user has an interactive shell attached. Bumps `instances.last_activity_at` and recomputes `expires_at`.

### Request

- **Path**: `/internal/instances/{slug}/heartbeat` where `{slug}` matches `^inst-[0-9a-f]{14}$`.
- **Headers**:
  - `Content-Type: application/json`
  - `X-Projctl-Version: <semver>` — identifies the client for rolling-upgrade diagnostics.
  - `X-Signature: hmac-sha256=<hex>` — HMAC of the raw request body using `instances.heartbeat_secret`.
- **Body**:
  ```json
  {
    "at": "2026-04-23T14:22:01.123Z",
    "signals": {
      "dev_server_running": true,
      "task_executing": false,
      "shell_attached": false
    },
    "guide_state_sha": "<sha256 of /var/lib/projctl/state.json>"
  }
  ```

### Response

- **200 OK**:
  ```json
  {
    "acknowledged_at": "2026-04-23T14:22:01.456Z",
    "expires_at":      "2026-04-24T14:22:01.456Z",
    "status":          "running"
  }
  ```
- **401 Unauthorized** — HMAC mismatch. Instance should NOT retry; a mismatched HMAC means the instance secret is wrong (most likely the instance has been terminated and re-provisioned with a new secret). `projctl` should exit and let compose supervise the restart.
- **404 Not Found** — slug unknown. Same behaviour as 401.
- **409 Conflict** — instance is in a non-active status (`terminating`, `destroyed`, `failed`). Body includes `{"status": "<current>"}`. Instance should stop heartbeating.
- **429 Too Many Requests** — rate limited (one heartbeat per 30 s hard floor). Retry after `Retry-After` seconds.

### Security rules

1. The endpoint is mounted on the orchestrator's **internal** port (not public). Ingress is compose-network only; `nginx` front door does not route it.
2. HMAC verification uses `hmac.compare_digest` (constant-time).
3. Rejected requests increment `instance.heartbeat_auth_fail` counter; >10 in 5 min trips an alert.
4. The `slug` in the path MUST match the `instance_id` the HMAC secret belongs to. Cross-instance forgery is the attack this prevents.

---

## `POST /internal/instances/<slug>/rotate-git-token`

Called by `projctl rotate-git-token` (every 45 min, cron inside the instance) to receive a fresh GitHub App installation token before the old one expires (see [research.md §3](../research.md#3-github-app-vs-pat-for-per-instance-push-auth)).

### Request

- **Path**: `/internal/instances/{slug}/rotate-git-token`.
- **Headers**: same HMAC contract as `/heartbeat`.
- **Body**: `{"at": "<ISO 8601>"}`.

### Response

- **200 OK**:
  ```json
  {
    "token":      "ghs_<opaque>",
    "expires_at": "2026-04-23T15:22:01Z",
    "repo":       "org/repo"
  }
  ```
  `projctl` writes the token to `~/.git-credentials` and `GITHUB_TOKEN` env for the `app` container's shell, then exits 0.
- **401 / 404 / 409** — same semantics as `/heartbeat`.
- **503 Service Unavailable** — GitHub API itself degraded. Response includes `Retry-After`. The banner policy from FR-027a/b/c governs the chat-side UX; `projctl` silently retries on the next cron tick.

### Notes

- The orchestrator NEVER returns the GitHub App's private key or JWT. The caller only ever sees the ephemeral installation token.
- Token is scoped to exactly one repo (the project's repo); GitHub rejects pushes elsewhere — belt-and-braces for Principle I.

---

## Implementation notes

- Both endpoints live in [src/taghdev/api/routers/instances.py](../../../src/taghdev/api/routers/instances.py) (new). They MUST NOT be re-exported from any public router.
- Rate limiting is per `instance_id`, implemented with Redis `INCR` + `EXPIRE`. Ephemeral state per Principle VI.
- Every request is logged via `audit_service` with `{instance_slug, signal_summary}`. Redactor runs on the log path.

---

## Test coverage

Contract tests (`tests/contract/test_heartbeat_api.py`) MUST assert:

1. Valid HMAC + live instance → 200, `expires_at` moved forward, `last_activity_at` bumped in DB.
2. Forged HMAC → 401, counter incremented.
3. HMAC against wrong instance's secret → 401 (cross-instance forgery guard).
4. Slug in path does not match HMAC key → 401.
5. Instance in `terminating` status → 409 with current status in body.
6. >30 req/s from a single instance → 429 with `Retry-After`.
7. `rotate-git-token` on a GitHub App outage → 503 with `Retry-After` (mocked failure).
