# Per-Chat Isolated Instances — Target Architecture

**Date:** 2026-04-22
**Branch:** `multi-instance`
**Status:** Design. Companion to [audit.md](audit.md). Prior-art decisions in [../research/prior-art.md](../research/prior-art.md).
**Audience:** Anyone touching orchestrator, tunnel, or bootstrap code.

This document specifies the target state. It does not cover migration steps — those are per-PR.

---

## 1. Shape of the change

Today the unit of lifecycle is a **task**: each user action creates a workspace, runs, tears down. Instances (the running app) are ad-hoc; a tunnel is spawned during bootstrap and lingers until the next bootstrap rotates it. One user's app can't be isolated from another's; a shared worker container does everything.

Target: the unit of lifecycle is an **instance**. A chat has exactly one instance. An instance has its own container stack (app, db, redis, node, cloudflared sidecar), its own named Cloudflare Tunnel, its own hostnames, its own short-lived credentials. Tasks run *inside* an instance and mutate its working tree; they no longer create or destroy workspaces. Inactivity of 24 h (or explicit terminate) destroys the instance.

Per-task workspaces don't go away — they're now *per-instance* workspaces, long-lived for the chat's duration. The existing `WorkspaceService` cache+worktree pattern is reused, re-scoped.

Host-mode code is **kept** for backwards-compatibility with existing `mode="host"` projects. New projects default to per-chat containers. Deprecation of host-mode is a separate decision in a later release.

---

## 2. Component view

```
┌──────────────────────────────────────────────────────────────────────┐
│  USER / CHAT CLIENT  (web chat, Telegram, Slack)                     │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │ chat message, "terminate", etc.
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR  (FastAPI + ARQ, Python, lives in `api` + `worker`)    │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────┐     │
│  │ InstanceService  │  │ TunnelService    │  │ CredentialsSvc  │     │
│  │  provision/start │  │  CF API + DB     │  │  GH App tokens  │     │
│  │  destroy/resume  │  │  sidecar config  │  │  per-instance   │     │
│  └──────────────────┘  └──────────────────┘  └─────────────────┘     │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────┐     │
│  │ InactivityReaper │  │ TaskExecutor     │  │ AuditService    │     │
│  │  cron, dry-run   │  │  runs inside     │  │  redacts logs   │     │
│  │  24h watermark   │  │  existing inst.  │  │                 │     │
│  └──────────────────┘  └──────────────────┘  └─────────────────┘     │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │ docker compose up/down/exec
                                   │ on per-instance compose network
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│  INSTANCE  (one docker compose project per chat)                     │
│                                                                      │
│   network: tagh-inst-<slug>                                          │
│                                                                      │
│   ┌────────┐  ┌────────┐  ┌────────┐  ┌───────┐  ┌───────────────┐   │
│   │  app   │  │  web   │  │  node  │  │  db   │  │  cloudflared  │◄──┼── internet
│   │php-fpm │  │ nginx  │  │ vite   │  │mysql  │  │   SIDECAR     │   │   (outbound-only)
│   │  :9000 │  │  :80   │  │ :5173  │  │:3306  │  │               │   │
│   └────────┘  └────────┘  └────────┘  └───────┘  └───────────────┘   │
│                                                                      │
│      projctl (single binary, baked into app image)                   │
└──────────────────────────────────────────────────────────────────────┘
```

Key points on the diagram:

- **No published ports** on any instance container. `cloudflared` is outbound-only; everything else is compose-network-only.
- **One `cloudflared` sidecar per instance**, with its own named tunnel credentials. Teardown = `docker compose down` on that stack.
- **`projctl`** lives inside the `app` image (or a dedicated `toolbox` container). It runs `guide.md` steps, exposes `doctor`, `explain`, `heartbeat` — see §7.
- **Orchestrator is single-tenant** (it is the TAGH Dev control plane itself; its own `api` + `worker` containers). It is not replicated per chat.

---

## 3. Data model

Add one table, extend one, deprecate one field path.

### 3.1 New: `instances`

```sql
instances (
  id                 uuid        primary key,
  slug               text        unique not null,   -- URL-safe, stable: inst-<8>
  chat_session_id    bigint      unique references web_chat_sessions(id),
  project_id         bigint      not null references projects(id),
  status             text        not null,          -- see §4
  compose_project    text        not null,          -- docker compose -p value
  workspace_path     text        not null,          -- /workspaces/inst-<slug>
  session_branch     text        not null,          -- git branch inside the worktree
  image_digest       text,                          -- app image digest, for reproducibility
  resource_profile   text        not null default 'standard',  -- standard|large
  created_at         timestamptz not null default now(),
  started_at         timestamptz,
  last_activity_at   timestamptz not null default now(),
  expires_at         timestamptz not null,          -- last_activity_at + idle_ttl
  terminated_at      timestamptz,
  terminated_reason  text,                          -- idle_24h|user_request|failed|project_deleted
  failure_code       text,                          -- set when status=failed
  failure_message    text
);

create index idx_instances_status_expires on instances(status, expires_at);
create index idx_instances_chat on instances(chat_session_id);
```

Notes:
- `chat_session_id` UNIQUE: one chat → one instance. Reusing an existing chat after teardown creates a new row (archive-forward).
- `slug` is the single stable identifier for DNS, compose project name, network name, volume prefix, and all audit logs. Formation: `inst-<8 hex chars>` picked from `uuid` at create time. Must be <20 chars to fit DNS label limits.
- `workspace_path` is a *per-instance* worktree (not shared across chats), rooted at `/workspaces/inst-<slug>/`. The per-project clone cache at `/workspaces/_cache/<project>/` is still shared across instances of the same project — reuse [workspace_service.py:30–80](../../src/openclow/services/workspace_service.py#L30-L80) as-is, scoped to the instance ID instead of the task ID.
- `expires_at` is derived (`last_activity_at + idle_ttl`). Reaper queries are index-backed.

### 3.2 New: `instance_tunnels`

Decided: tunnel state moves out of the worker-local `_active_processes` dict at [tunnel_service.py:29](../../src/openclow/services/tunnel_service.py#L29) and out of the overloaded `platform_config` rows. A dedicated table makes lifecycle explicit.

```sql
instance_tunnels (
  id                   uuid        primary key,
  instance_id          uuid        not null references instances(id) on delete cascade,
  cf_tunnel_id         text        not null,          -- Cloudflare named-tunnel UUID
  cf_tunnel_name       text        not null,          -- tagh-inst-<slug>
  web_hostname         text        not null,          -- <slug>.dev.<our-domain>
  hmr_hostname         text        not null,          -- hmr-<slug>.dev.<our-domain>
  ide_hostname         text,                          -- ide-<slug>.dev.<our-domain> (optional)
  credentials_secret   text        not null,          -- path/reference, never the JSON
  status               text        not null,          -- provisioning|active|rotating|destroyed
  last_health_at       timestamptz,
  created_at           timestamptz not null default now(),
  destroyed_at         timestamptz,
  unique (instance_id, status) where status = 'active'
);

create index idx_instance_tunnels_instance on instance_tunnels(instance_id);
```

- **One active tunnel per instance** enforced by the partial unique index.
- `credentials_secret` is a reference (Docker secret name, or K/V path), not the credential blob itself — see §8.
- The old "anonymous quick-tunnel URL in `platform_config`" path is kept for the legacy host-mode bootstrap. New code must not write there.

### 3.3 Extend: `web_chat_sessions`

Add one nullable column:

```sql
alter table web_chat_sessions
  add column instance_id uuid references instances(id);
```

Back-reference so the chat UI can `join` for status display. Not authoritative (the other direction is).

### 3.4 Unchanged but worth restating

- `Project.mode` stays. New projects will be `mode="container"` (new value; keep `"docker"` and `"host"` for backwards compatibility).
- `Project.public_url` + `Project.tunnel_enabled=false` stays as the "operator owns domain" escape hatch for non-chat deployments.
- `Task` rows now carry an `instance_id` (new FK) so per-instance task history is queryable:
  ```sql
  alter table tasks add column instance_id uuid references instances(id);
  ```

---

## 4. State machine

```
                ┌──────────────┐
                │ provisioning │◄──── new chat opens / resume on dead inst.
                └──────┬───────┘
                       │ compose up OK + projctl up OK + tunnel healthy
                       ▼
       ┌────────────►┌─────────┐
       │             │ running │◄──── any activity bumps last_activity_at
       │             └────┬────┘
       │ activity          │ 24h idle watermark crossed
       │                   ▼
       │             ┌─────────┐
       └─────────────│  idle   │    (container still up; grace period)
                     └────┬────┘
                          │ grace expires OR user terminates
                          ▼
                     ┌─────────────┐
                     │ terminating │    compose down + tunnel delete + DNS cleanup
                     └─────┬───────┘
                           │
                           ▼
                     ┌──────────┐
                     │destroyed │  (terminal; row stays for audit, workspace purged)
                     └──────────┘

                     ┌────────┐
                     │ failed │◄──── any unrecoverable error during provisioning
                     └────┬───┘      or running (also terminal; teardown runs)
                          │
                          └──────► terminating (async cleanup)
```

**Invariants:**
- Exactly one row per `chat_session_id` with `status ∈ {provisioning, running, idle, terminating}`. Enforced by partial unique constraint.
- `running → idle` is a *soft* transition: it just means "eligible for reaping" — the containers are still up and the user can still hit the tunnel. It exists so the UI can show a "going-to-sleep in N minutes" warning and the reaper has a well-defined cursor.
- `terminating` is idempotent: re-running teardown on an already-terminating instance is a no-op.
- `failed` carries `failure_code` + `failure_message`. Codes are a closed set: `image_build`, `compose_up`, `projctl_up`, `tunnel_provision`, `dns`, `health_check`, `oom`, `storage_full`, `orchestrator_crash`, `unknown`.

**Resume policy:** a user returning to a chat whose instance is `destroyed` gets a fresh instance provisioned on their next message (not eagerly). Their session branch is reattached — the branch lives in the per-project cache repo, which is shared and persistent across instances.

---

## 5. Networking

### 5.1 Sidecar `cloudflared` — why not shared

Both options considered in the audit. Choice: **sidecar per instance**. Justification:

1. **Teardown simplicity.** `docker compose down -p tagh-inst-<slug>` removes the compose network, all containers including the sidecar, all volumes. Nothing touches a host-wide config file. A shared multiplexer requires reconciling an `ingress:` list in a shared tunnel config and pruning DNS CNAMEs — doable, but one more thing to get wrong on failure paths.
2. **Credential blast radius.** Each instance has its own tunnel credentials. A compromised instance can't disable another instance's tunnel. A shared `cloudflared` would hold credentials for every live instance at once.
3. **Scalability ceiling.** One shared `cloudflared` process has a finite connection-per-tunnel limit (typically ~4 connections × one tunnel per process). With a sidecar, scaling is horizontal and self-balancing per instance.
4. **Observability.** Tunnel metrics (Prometheus on `cloudflared --metrics`) are per-instance by construction, not by aggregation.

Cost: more `cloudflared` processes on the host. Each is ~20 MB RSS, negligible at the numbers we target. Cloudflare's tunnel limit is generous (1,000 named tunnels on free tier).

### 5.2 Named tunnel provisioning

Orchestrator `TunnelService` owns a long-lived Cloudflare API token with scopes `Account.Cloudflare Tunnel:Edit`, `Zone.DNS:Edit` on `dev.<our-domain>`. The token lives in `platform_config` (`category="cloudflare"`, `key="api_token"`), inherited from the audit's existing pattern for provider secrets.

Provisioning flow (idempotent, resumable):
1. `POST /accounts/:a/cfd_tunnel` → create tunnel named `tagh-inst-<slug>`, capture `tunnel_id` + `tunnel_token` (the credential JSON).
2. Write `instance_tunnels` row with `status='provisioning'`.
3. Store the credential JSON as a Docker secret: `docker secret create tagh-inst-<slug>-cf <json>`. The **secret reference** (`tagh-inst-<slug>-cf`), not the JSON, goes on `instance_tunnels.credentials_secret`.
4. `POST /zones/:z/dns_records` for each hostname (web, hmr, optional ide) — all `CNAME` to `<tunnel_id>.cfargotunnel.com`. Capture record IDs (stored in the instance's audit log, not the table — we re-query CF on teardown).
5. Render the per-instance `cloudflared` config file with the ingress rules (see §5.3), mount it into the sidecar via compose.
6. `docker compose up -p tagh-inst-<slug>`. When `cloudflared` reports `Registered tunnel connection`, flip `instance_tunnels.status='active'`.

Teardown is the reverse. The partial unique index on `instance_tunnels.status='active'` prevents double-provisioning on retries.

### 5.3 Per-instance `cloudflared` config

The sidecar mounts one config file. Not templated through the LLM — deterministic:

```yaml
tunnel: <tunnel_id>
credentials-file: /etc/cloudflared/creds.json
metrics: 0.0.0.0:2000
ingress:
  - hostname: <slug>.dev.<our-domain>
    service: http://web:80
  - hostname: hmr-<slug>.dev.<our-domain>
    service: http://node:5173
    originRequest:
      noTLSVerify: true
  - hostname: ide-<slug>.dev.<our-domain>     # if instance has an IDE surface
    service: http://toolbox:3000
  - service: http_status:404
```

- Hostnames resolve on Cloudflare's edge via CNAMEs created in step 4 above.
- Services resolve inside the compose network via service-name DNS. No `depends_on` on `cloudflared`; it retries the origin until connectable (~10 s cold start).

### 5.4 Vite HMR survival

The known-gotcha combo (cites in [../research/prior-art.md](../research/prior-art.md) §E). Minimum Vite config:

```js
export default defineConfig({
  server: {
    host: '0.0.0.0',
    origin: `https://${process.env.INSTANCE_HOST}`,
    allowedHosts: [process.env.INSTANCE_HOST, process.env.INSTANCE_HMR_HOST],
    hmr: {
      host: process.env.INSTANCE_HMR_HOST,
      clientPort: 443,
      protocol: 'wss',
    },
  },
});
```

Key points:
- **Separate hostname for HMR** — `hmr-<slug>.dev.<our-domain>` → `http://node:5173`. Avoids path-based routing, which can break WebSocket upgrades.
- `clientPort: 443` because the browser talks to Cloudflare over TLS regardless of the origin port.
- `noTLSVerify: true` on the HMR ingress: `cloudflared` connects to `node:5173` over plain HTTP (Vite doesn't serve TLS inside the network), but still gets WSS at the edge. This is safe inside the compose network.
- **Env-var contract:** orchestrator injects two pre-computed hostnames, `INSTANCE_HOST=<slug>.dev.<our-domain>` and `INSTANCE_HMR_HOST=hmr-<slug>.dev.<our-domain>`. Projects don't concatenate domains, don't know the slug, don't hard-code anything. Swapping the zone later is a one-place change in the orchestrator.
- Projects ship this snippet in `vite.config.js` as part of the Laravel+Vue template. Projects that diverge must still honour the env-var contract.

### 5.5 No host port publishing — verification

CI rule: the compose template and any generated per-instance compose file MUST NOT contain `ports:` on any service except `cloudflared` (which itself exposes nothing — only the metrics port internally). A unit test reads the rendered compose YAML and fails the build if it finds any `ports:` key.

---

## 6. Inactivity detection

Two sources, one ground truth.

**Source A — chat activity (authoritative).** The web/bot handlers call `InstanceService.touch(instance_id)` on every inbound message for the chat session. This bumps `last_activity_at = now()`, recomputes `expires_at`. Cheap (one indexed update), correct for the common case where the user is working via chat.

**Source B — in-instance heartbeat (augmentation).** `projctl` writes a heartbeat to the orchestrator every 60 s while any of: (a) the Vite dev server is running, (b) a task is executing, (c) the user has an interactive shell attached via the IDE surface. The heartbeat is `POST /internal/instances/<slug>/heartbeat` over the instance's outbound network, authenticated by a per-instance short-lived HMAC token (see §8).

Why both: the user can leave a chat tab open and hack in the browser IDE for 23 h 59 m without sending a chat message. Heartbeat catches that. Equally, they can be actively chatting without the container doing anything — chat activity catches that.

**What is NOT an activity source:** raw HTTP requests hitting the tunnel. `cloudflared` quick-tunnel does not expose request logs without Cloudflare Enterprise; parsing connector metrics is possible but brittle. Explicitly out of scope for v1. If the product later needs "treat browser clicks as activity," we add a lightweight logging sidecar (Caddy with a simple access log → Redis), not try to retrofit cloudflared.

**Reaper loop:** ARQ cron, every 5 min. Query:

```sql
select id from instances
 where status in ('running', 'idle')
   and expires_at <= now()
 for update skip locked
 limit 50;
```

For each row: transition `running → idle` on first hit (grace notification to the chat, "instance will be destroyed in 60 min"), `idle → terminating` on second hit after the grace window. Dry-run mode: set env `REAPER_DRY_RUN=1` — emits the planned actions to the audit log and returns without mutating.

Grace window (idle → terminating) defaults to 60 min and is configurable via `platform_config` (`category="instance"`, `key="idle_grace_minutes"`). The grace exists so a user returning from lunch to an "idle" instance gets a fast warm path (just bump activity, skip re-provisioning).

**Manual terminate:** user sends `/terminate` (chat) or clicks "End session" (UI). `InstanceService.terminate(instance_id, reason='user_request')` → straight to `terminating`, no grace.

---

## 7. `projctl` (the CLI)

Scope of this doc: how the orchestrator interacts with `projctl`. The CLI's own spec (commands, exit codes, JSON schema of logs, `guide.md` format) is in [../project-spec.md](../project-spec.md) — separate deliverable.

Summary only, for architecture continuity:

- **Language:** Go first-preference (single static binary, trivial to `COPY --from=<projctl>` into any project image). Python fallback if Go isn't justified at build time — packaged as a PEX or `pyoxidizer` bundle so it's still one file. **No Node/TS in the runtime.** If we adopt Runme (see prior-art §C), its Go binary is vendored and called from `projctl`, not the other way around.
- **Location inside the instance:** `/usr/local/bin/projctl`. The app image's Dockerfile does `COPY --from=ghcr.io/<org>/projctl:<ver> /projctl /usr/local/bin/projctl`.
- **Commands the orchestrator calls:**
  - `projctl up` during provisioning. Streams JSON-line logs to the orchestrator over stdout; the orchestrator tails stdout from `docker compose up` and parses. Steps are resumable: if a step succeeded in a prior run (recorded in `/var/lib/projctl/state.json` on an instance-persistent volume), re-runs are no-ops.
  - `projctl doctor` during health checks and on demand from the chat UI.
  - `projctl down` before teardown (graceful stop of dev servers and queue workers).
  - `projctl step <name> --retry` exposed to the user through a "retry install" chat action.
  - `projctl explain <error-id>` invoked by the step runner itself when a step fails — not by the orchestrator directly. The envelope (§9) is what the orchestrator redacts and ships.
- **Heartbeat:** `projctl heartbeat` is the command the `projctl daemon` loop calls every 60 s; it hits the orchestrator's internal heartbeat endpoint (§6). This runs as a background process inside `app`, supervised by `tini` (or equivalent) — not a separate container.

The orchestrator never shells into the instance directly for lifecycle operations. Every mutation is either (a) `docker compose up/down -p tagh-inst-<slug>`, or (b) `docker exec tagh-inst-<slug>-app projctl <subcommand>`. Keeps the audit trail clean.

---

## 7.5 Agent access isolation

Two users running coding tasks on two chats means two concurrent `claude_agent_sdk` sessions in the same worker container. Infrastructure isolation (§8.4) stops the *running apps* from seeing each other; this section specifies how the **LLM agents** themselves are prevented from targeting the wrong instance.

**Principle: MCP servers are bound to one instance at spawn time. The agent has no tool that takes "which instance."**

### 7.5.1 Per-task MCP server fleet

When `TaskExecutor` starts a coding agent for a task on `inst-<slug>`, it spawns a fresh trio of MCP subprocesses, scoped by argv at launch. The scope is fixed for the session's lifetime; nothing the agent does at runtime can change it.

| MCP server      | Replaces / extends       | Launch argv                                                                    | Tools exposed                                                                 |
|-----------------|--------------------------|--------------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| `instance_mcp`  | [docker_mcp.py](../../src/openclow/mcp_servers/docker_mcp.py) (new-mode only) | `--compose-project tagh-inst-<slug> --allowed-services app,web,node,db,redis` | `instance_exec(service, cmd)`, `instance_logs(service)`, `instance_restart(service)`, `instance_ps()`, `instance_health()` |
| `workspace_mcp` | generic Read/Write/Edit  | `--root /workspaces/inst-<slug>`                                               | `read_file(path)`, `write_file(path, content)`, `edit_file(...)`, `list_dir(path)`, `search(pattern)` |
| `git_mcp`       | [git_mcp.py](../../src/openclow/mcp_servers/git_mcp.py) (extended)            | `--workspace /workspaces/inst-<slug> --branch <session_branch>`                | `git_status()`, `git_diff()`, `git_add(path)`, `git_commit(msg)`, `git_push()`, `git_log()` |

Hard rules enforced at the MCP-server layer, not the agent layer:

- **No tool accepts a project/container/workspace name.** The server knows its one target; the agent cannot name another.
- **Path escape is rejected.** `workspace_mcp` resolves every path against `--root` and rejects anything that, after symlink-chase, lands outside it. Symlink chase is mandatory, not optional.
- **Branch is pinned.** `git_mcp` refuses `checkout`, `branch -D`, `reset --hard <other-ref>`, or any operation that would leave `<session_branch>`. Merges from main come through a separate orchestrator-driven code path.
- **Service allowlist.** `instance_mcp`'s `--allowed-services` flag limits exec/logs/restart to known services. The agent cannot `instance_exec("cloudflared", ...)` to tamper with the sidecar.

### 7.5.2 No cross-cutting tools

New-mode coding agents do **not** get `Bash`, raw `docker`, or `host_run_command`. Everything flows through the three bound MCP servers above. The existing `host_mcp` is kept only for legacy `mode="host"` projects — new-mode tasks never load it.

The agent's visible MCP manifest describes tools with phrases like "…in the current instance" / "…in the current workspace." No tool answers "what other instances exist?" so the concept is invisible-by-design. Not redacted from a richer surface: the richer surface never existed in this session.

### 7.5.3 Concurrency model

- **Per-instance Redis lock** `openclow:instance:<slug>` — prevents two concurrent tasks in the same chat. Inherits the pattern at [workspace_service.py:36](../../src/openclow/services/workspace_service.py#L36); re-scoped from project to instance.
- **Across different instances, full parallelism.** Separate compose projects, networks, volumes, and MCP processes. No shared mutable state.
- **ARQ worker concurrency** (`max_jobs`) caps the fleet. Each concurrent task adds ~3 MCP subprocesses × ~30 MB RSS + one agent session; budget accordingly.
- **MCP subprocesses carry `instance_slug` in their argv** so `ps` and container debugging make cross-contamination visible at a glance.

### 7.5.4 Audit trail

Every MCP tool call is logged via [audit_service.py](../../src/openclow/services/audit_service.py) with fields `{instance_slug, chat_session_id, task_id, server, tool, args_hash, outcome}`. Two invariants the post-hoc auditor can assert:

1. For any given `task_id`, every MCP call shares the same `instance_slug`.
2. For any given `instance_slug`, every MCP call's `chat_session_id` is the one bound to that instance.

Violations of either imply a cross-tenancy bug and trip an alert.

### 7.5.5 Git push isolation — belt and braces

`CredentialsService.github_push_token(instance_id)` (§8.2) mints an installation token scoped to the **specific repo** this instance's project is bound to. Even if an agent somehow constructed a push to a different repo, GitHub rejects it at the auth layer. Tool-layer pinning + credential-layer scoping; two independent failures required before an agent can push to the wrong place.

### 7.5.6 What `TaskExecutor` actually does, in order

1. Resolve `Instance` from `chat_session_id`; require `status='running'` (else wait or provision).
2. Acquire Redis lock `openclow:instance:<slug>`.
3. Spawn `instance_mcp`, `workspace_mcp`, `git_mcp` subprocesses with their bound argv.
4. Start `claude_agent_sdk` session. MCP config lists *only* those three servers. No `Bash`, no system tool defaults.
5. Run the task. Tool calls stream through audit service with `instance_slug`.
6. On exit (success, failure, or cancel): terminate the three MCP subprocesses, release the Redis lock, persist the task outcome.

One instance, one lock, one short-lived MCP fleet per task. No shared processes, no shared handles, no ambient authority.

---

## 8. Security model

### 8.1 Credential scoping

Three credential types per instance:

| Credential            | Lifetime             | Where stored                                             | Visible to instance         |
|-----------------------|----------------------|----------------------------------------------------------|-----------------------------|
| CF tunnel creds (JSON) | Instance lifetime    | Docker secret `tagh-inst-<slug>-cf`                      | Yes, mounted at `/etc/cloudflared/creds.json`  |
| GitHub push token     | ≤ 1 hour rotating    | In-memory in orchestrator; injected via env to `app`     | Yes, as `GITHUB_TOKEN` env  |
| Heartbeat HMAC secret  | Instance lifetime    | Orchestrator DB (`instances.heartbeat_secret`, not shown in schema above — add) | Yes, as `HEARTBEAT_SECRET` env |
| DB password (instance MySQL) | Instance lifetime | Generated at provision, stored on `instances` row     | Yes, as `DB_PASSWORD` env   |

None of these are baked into images. All are injected at `docker compose up` via env vars rendered from the orchestrator's compose template and, for the CF creds, mounted as a Docker secret.

### 8.2 PR push auth — decided: GitHub App

The audit identified a single shared PAT as the status quo. Replace with a **GitHub App** registered per TAGH-Dev deployment (not per customer):

1. The app has `contents:write`, `pull-requests:write` on the projects it's been installed into.
2. `CredentialsService.get_push_token(instance_id)` mints an installation token for the specific repo, TTL 1 hour (GitHub's max).
3. Token is injected into `app` container via a git credential helper (`git config credential.helper store` + `.git-credentials` written at provision, rotated by `projctl rotate-git-token` on a cron inside the instance — called every 45 min).
4. No token survives container teardown. No token is ever written to logs.

This is net-new code. The existing PAT path stays for legacy `mode="host"` and `mode="docker"` projects.

### 8.3 Secret injection

- Secrets enter via Docker secrets (`cloudflared`), compose env interpolation (everything else), and never via image build args.
- The rendered per-instance compose file is written to `/workspaces/inst-<slug>/_compose.yml` and fed to `docker compose -f`. It's git-ignored and scrubbed on teardown.

### 8.4 Filesystem boundaries

- Workspace per instance: `/workspaces/inst-<slug>/` on the orchestrator's `workspaces` volume. Bind-mounted into the instance's `app` container at `/app`. **Not** shared with any other instance.
- Per-project cache `/workspaces/_cache/<project_name>/` is shared *read-only* with instances during clone/fetch; writes go through a controlled `git worktree add` from the orchestrator, not from inside the instance. The instance never writes to the cache.
- The instance cannot see the host's Docker socket. Unlike the orchestrator worker, the instance does not run Docker-in-Docker. If a project needs Docker for its tests (uncommon for Laravel+Vue), that's out of scope for v1.

### 8.5 Log redaction

Before any log reaches either (a) the chat UI, or (b) the LLM fallback envelope (§9), it passes through a redactor that masks: bearer tokens, AWS/GCP keys, CF tokens, SSH private keys, `.env`-style `KEY=value` pairs where key matches `/SECRET|TOKEN|PASSWORD|KEY|AUTH/i`. Implementation: extend the existing [audit_service.py](../../src/openclow/services/audit_service.py) with a redactor module; use it on both paths, not just one.

### 8.6 Outbound network

Each instance compose network has **egress allowed**, **ingress only via `cloudflared`**. `cloudflared` doesn't open a listening port on the host. The app containers can `curl` out (for `composer install` etc.), but nothing on the internet can reach them except through the Cloudflare edge → tunnel.

Per-instance egress *policy* (e.g., deny egress to metadata services) is out of scope for v1. Revisit if a customer runs untrusted code in their own instance.

---

## 9. LLM fallback — context envelope

`projctl` runs steps. When a step fails, it builds an envelope and calls `projctl explain`:

```json
{
  "instance_slug": "inst-a1b2c3d4",
  "project_name": "acme-portal",
  "step": {
    "name": "install-php",
    "cmd": "composer install --no-interaction --prefer-dist",
    "cwd": "/app",
    "success_check": "composer show -i"
  },
  "exit_code": 1,
  "stdout_tail": "... last 200 lines, redacted ...",
  "stderr_tail": "... last 200 lines, redacted ...",
  "guide_section": "## install-php\n<text from guide.md for this step>",
  "previous_attempts": 0
}
```

The envelope is *all* the LLM sees. Not the full repo, not the full log, not every other step's output. Caps: 200 lines of stdout + 200 of stderr; if longer, head and tail with a truncation marker. Max 3 LLM attempts per step (configurable via `guide.md` step metadata). Required structured response:

```json
{ "action": "shell_cmd" | "patch" | "skip" | "give_up",
  "payload": "...", "reason": "..." }
```

- `shell_cmd`: a shell command to run before retrying the step. Re-uses the same allowlist as `host_guard` for safety.
- `patch`: a unified diff applied against the workspace. Applied by `git apply --check` first; rejected if it doesn't apply cleanly.
- `skip`: move on with a warning. Only allowed if the step's metadata has `skippable: true`.
- `give_up`: tell the orchestrator to mark the instance `failed` with `failure_code='projctl_up'`. Surfaces to the user as an error with a "retry" button.

LLM transport: decision deferred to the prior-art doc (LiteLLM vs direct `claude_agent_sdk`). Either way the redactor runs before the call.

---

## 10. Orchestrator surface (API shape only — not implementation)

Four new internal services. Existing `bot_actions`, `workspace_service`, `tunnel_service`, `project_service` are reused.

- **`InstanceService`** — `provision(chat_session_id) → Instance`, `touch(instance_id)`, `terminate(instance_id, reason)`, `get_or_resume(chat_session_id) → Instance`, `list_active() → [Instance]`. Owns the state machine (§4).
- **`TunnelService` (rewritten)** — `provision(instance_id) → InstanceTunnel`, `destroy(instance_id)`, `health(instance_id) → bool`, `rotate_credentials(instance_id)`. Replaces the quick-tunnel paths in [tunnel_service.py](../../src/openclow/services/tunnel_service.py). The legacy quick-tunnel functions move to `legacy_tunnel_service.py` and are only called by host-mode code.
- **`CredentialsService`** — `github_push_token(instance_id) → str`, `heartbeat_secret(instance_id) → str`, `cf_token(instance_id) → str` (internal; CLI doesn't call). Handles rotation.
- **`InactivityReaper`** — ARQ cron, see §6.

Task execution (`chat_task.py`, `orchestrator.py`) is re-plumbed to require an `Instance` in `running` state before it can run. Previously it called `WorkspaceService.prepare(project, task_id)`; now it calls `InstanceService.get_or_resume(chat_session_id)` and runs `docker exec` against the existing containers.

---

## 11. What stays legacy

- **`mode="host"` and `mode="docker"` projects** continue to use the current bootstrap agent flow via [bootstrap.py](../../src/openclow/worker/tasks/bootstrap.py). No changes in this refactor. A one-line router at the top of `bootstrap_project` decides: new `mode="container"` → `InstanceService`, old modes → existing code.
- **Quick-tunnel service** (`cloudflared --url`) stays available as `legacy_tunnel_service.start_quick_tunnel(...)` for host-mode and for out-of-band admin testing. Not reachable from the new instance code path.
- **Shared PAT** for git stays for legacy modes. GitHub App is the new-code default.

Follow-up (separate ticket, after one release cycle): deprecate and remove.

---

## 12. Capacity & limits

Back-of-envelope for planning, not hard caps:

- ~200 MB RAM per idle Laravel+Vue instance (php-fpm idle + mysql + node-ish + cloudflared ~20 MB).
- ~1 GB RAM per actively-building instance (npm/composer peaks).
- Disk: ~500 MB per instance workspace (Laravel + node_modules + vendor); shared cache amortises.
- Cloudflare: one named tunnel per instance; free tier allows 1,000+ per account.
- DNS records: three per instance (web, hmr, ide). At 500 instances → 1,500 records; well inside CF limits.

Single-host operation works up to ~50 concurrent instances on a 32 GB VPS. Beyond that, orchestrator is stateless enough to add hosts with a shared database, but multi-host deployment is out of scope for v1.

---

## 13. Open items for the implementation PRs

Things deliberately not decided here, to be worked through in the per-PR design notes:

1. **Go vs Python for `projctl`.** Defer until the build-template PR; depends on whether we vendor Runme.
2. **IDE-in-browser surface.** Brief mentions "user can browse code". Options: code-server (VS Code web), Theia, or defer entirely and expose only the running app. Lean: add `ide` hostname now in the schema, wire the container later; cheap to stub.
3. **Image pre-warming.** First provisioning is slow (~2 min: compose build, composer, npm). Option: pre-build per-project "base" images when the project is registered, re-use as FROM. Design in the compose-template PR.
4. **Cross-chat project hand-off.** If two users of the same project want to see each other's instance, today the answer is "no". The data model supports it (relax the partial unique on `chat_session_id`), but the product question is unanswered.
5. **Idle TTL per-project override.** Currently global 24 h. If a customer wants 4 h (cheaper) or 72 h (workshops), we need `Project.idle_ttl_hours nullable`. Add when the first customer asks.

---

## 14. Implementation order (confirming working agreement)

1. `guide.md` spec + `project.yaml` schema → `docs/project-spec.md`.
2. `projctl` step runner + JSON log format + success-check semantics.
3. Laravel+Vue compose template → `templates/laravel-vue/`.
4. `TunnelService` rewrite + `instance_tunnels` table + Cloudflare API client.
5. `InstanceService` + state machine + `instances` table.
6. `InactivityReaper` + cron wiring.
7. LLM fallback + redactor + envelope.
8. E2E test: spin dummy Laravel+Vue instance, confirm HMR via tunnel, teardown, assert zero leftover containers/volumes/DNS records.

Each ships as its own PR with rollback notes. Host-mode and existing Docker-mode code is not touched by any of these PRs until step 8 verifies parity; then the `bootstrap.py` router at the top gets its new-mode branch.

---

## 15. Finalised decisions from Spec Kit clarifications (T089)

The following items were finalised during the Spec Kit clarification
phase and supersede any looser language earlier in this document. See
[specs/001-per-chat-instances/spec.md §Clarifications](../../specs/001-per-chat-instances/spec.md#clarifications)
for the original Q1–Q5 discussion.

| ID | Decision | Supersedes |
|----|----------|------------|
| **Q1** | Public preview URL is always a Cloudflare Tunnel hostname: `https://<slug>.<zone>` for the app, `https://hmr-<slug>.<zone>` for Vite HMR. No path-prefix routing, no per-user subdomains. | earlier mention of "custom domain per user" |
| **Q2** | Per-user cap is **3 concurrent active instances**, operator-tunable via `platform_config(category='instance', key='per_user_cap')`. Read fresh on every `provision()` call — no worker restart required (T053). | "no cap" / "platform-wide cap only" |
| **Q3** | Retention is **chat-lifetime**: deleting a chat synchronously tears down its instance, cascades `instances`/`instance_tunnels`/`tasks`/`web_chat_messages` via FK, deletes audit rows keyed by `instance_slug`, and enqueues a branch GC job. See [services/chat_session_service.py](../../src/openclow/services/chat_session_service.py). | "retain audit for 30 days after teardown" |
| **Q4** | **Keep-running on upstream outage** (FR-027a). A CF / GitHub / DNS outage renders a non-blocking chat banner but MUST NOT flip `instances.status` to `failed`. The prober (`tunnel_health_check_cron`) records degradation in Redis at `openclow:instance_upstream:<slug>:<capability>` with a 180s TTL (3× its 60s cadence). | "flip to failed on any upstream error" |
| **Q5** | **60-minute grace window** after the idle TTL. Running → idle transition notifies the chat; only after `grace_notification_at + 60min` does the reaper move idle → terminating. Any activity (chat message or projctl heartbeat) during the grace cancels the teardown. Window is operator-tunable via `platform_config(category='instance', key='idle_grace_minutes')` — also read fresh per sweep. | "immediate teardown at TTL" |

### Deltas from this document's original design

* **§6 workspace volume** was originally a named Docker volume. In v1 it
  is a **bind mount** from `/workspaces/${INSTANCE_SLUG}` on the host
  into each service's `/app`. This keeps the host-side `git worktree`
  (from `WorkspaceService.reattach_session_branch`) visible to app,
  web, and node containers and matches the scoped
  `workspace_mcp --root`.
* **§7 heartbeat loop** is implemented in Go inside `projctl`
  ([projctl/internal/steps/heartbeat.go](../../projctl/internal/steps/heartbeat.go)),
  not a Python daemon. Signals: Vite dev server probe, task-running
  marker, any TTY-attached shell.
* **§9 LLM fallback** is split across two files: the Python endpoint
  at `POST /internal/instances/<slug>/explain` and the Go client at
  [projctl/internal/steps/explain.go](../../projctl/internal/steps/explain.go).
  The envelope shape is authoritative in
  [contracts/llm-fallback-envelope.schema.json](../../specs/001-per-chat-instances/contracts/llm-fallback-envelope.schema.json);
  both sides validate against it.
* **§11 reaper** is implemented as an ARQ cron, not a separate
  process. 5-min cadence; `FOR UPDATE SKIP LOCKED` for multi-replica
  safety. Honors `REAPER_DRY_RUN=1`.

See [specs/001-per-chat-instances/plan.md](../../specs/001-per-chat-instances/plan.md)
and [tasks.md](../../specs/001-per-chat-instances/tasks.md) for the
mapping of T0xx task IDs to concrete PRs.
