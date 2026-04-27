<!--
SYNC IMPACT REPORT
==================
Version change: 1.0.0 → 1.1.0
Bump rationale: MINOR — three AI-contributor-facing principles added (VIII Root-Cause
Fixes Over Bypasses, IX Async-Python Correctness), Principle VII strengthened with
explicit "no hallucinated APIs / cite file:line" sub-rules, and a scope-respect
rule added to Development Workflow. No existing principle removed or redefined.

Modified principles:
  - VII. Verified Work, No Half-Features — EXPANDED with evidence-based-claims rules
    (read before edit, grep before cite, no hallucinated APIs).

Added principles:
  - VIII. Root-Cause Fixes Over Bypasses (NON-NEGOTIABLE)
  - IX. Async-Python Correctness

Added to Development Workflow & Quality Gates:
  - Scope respect rule (bug fixes do not drag in unrelated refactors).
  - No-new-dependencies-without-justification rule.

Removed sections: none.

Templates requiring updates:
  - ✅ .specify/templates/plan-template.md — Constitution Check now covers nine
    principles; reviewers MUST enforce VIII and IX at the gate.
  - ✅ .specify/templates/spec-template.md — no change needed.
  - ✅ .specify/templates/tasks-template.md — no change needed.
  - ⚠ CLAUDE.md — Principle VIII partially overlaps with CLAUDE.md "Agent Never
    Gives Up" and hook-failure rules; no conflict, but flag for cross-reference
    when CLAUDE.md is next edited.

Historical (v1.0.0 ratification — kept for audit trail):
  - Initial ratification replaced placeholder template with seven concrete
    principles derived from docs/architecture/audit.md,
    docs/architecture/per-chat-instances.md, and CLAUDE.md.
  - Architecture Constraints replaced [SECTION_2_NAME].
  - Development Workflow & Quality Gates replaced [SECTION_3_NAME].
  - Principles took a position on: sidecar-per-instance (audit open item #1) and
    Python orchestrator (audit open item #4). CLI name and host-mode deprecation
    left open by design.
-->

# TAGH Dev Constitution

TAGH Dev is an AI Dev Orchestrator (Python 3.12 async + FastAPI + ARQ workers +
Telegram/Slack providers) that runs per-chat isolated development instances behind
Cloudflare Tunnels. The principles below govern every architectural and
implementation decision in the repository. They are derived from, and supersede
ad-hoc guidance in, `docs/architecture/audit.md` and
`docs/architecture/per-chat-instances.md`.

## Core Principles

### I. Per-Chat Instance Isolation (NON-NEGOTIABLE)

Every user chat maps to exactly one instance with its own docker-compose project,
network, volumes, workspace, named Cloudflare Tunnel, and short-lived credentials.
No mutable state is shared across instances. Cross-tenancy defense is layered:
tool-layer pinning (MCP bound to one instance at spawn) AND credential-layer
scoping (per-repo installation tokens) MUST both be in place, so two independent
failures are required before an agent can act on the wrong instance.

**Rationale**: The audit established that today's per-task shared-worker model
cannot safely host multiple concurrent users. Per-instance isolation is the whole
point of the multi-instance refactor; anything that re-introduces shared mutable
state across chats voids the security model.

**Enforcement**: `chat_session_id` is UNIQUE on the `instances` table with a
partial unique constraint on active statuses. Every MCP tool call is logged with
`{instance_slug, chat_session_id, task_id}`; a post-hoc auditor MUST be able to
assert that every call for a given `task_id` carries the same `instance_slug`.

### II. Deterministic Execution Over LLM Drift

Instance provisioning and lifecycle are driven by deterministic code — declarative
`guide.md` steps executed by `projctl`, `docker compose up/down`, and the
orchestrator's state machine. The LLM is the **fallback**, not the happy path:
it is called only when a `projctl` step fails, bounded by a capped context
envelope (≤200 lines stdout, ≤200 lines stderr, the failing step, the guide
section, and a structured response schema). Max 3 LLM attempts per step.

**Rationale**: The audit identified the 1,882-line LLM-driven bootstrap agent as
the single biggest cost + reliability liability. Deterministic steps are
resumable, auditable, and cheap; LLM retries are none of those.

**Enforcement**: No new code path for bootstrap or lifecycle may add an MCP tool
that performs a multi-step workflow autonomously. If a step requires reasoning,
it MUST be modelled as `projctl explain <error-id>` with a structured
`{action, payload, reason}` response, not as free-form agent turns.

### III. No Ambient Authority for Agents (NON-NEGOTIABLE)

MCP servers are bound to a single instance at subprocess spawn time via argv.
No tool exposed to a coding agent accepts a project, container, workspace, or
instance identifier as an argument. Path arguments are resolved against the
server's `--root` with symlink chase and rejected if they escape. `git_mcp`
refuses operations that leave the pinned session branch. `instance_mcp`'s
`--allowed-services` flag limits exec/logs/restart to the declared service list
— agents cannot target the `cloudflared` sidecar or any service outside the list.
New-mode coding agents do NOT get `Bash`, raw `docker`, `host_run_command`, or
any equivalent escape hatch.

**Rationale**: An agent that can name a target can target the wrong one.
Removing the parameter from the tool surface removes the class of bug.

**Enforcement**: Any PR that adds a coding-agent tool MUST show that (a) the
tool's argument schema contains no instance/project/workspace identifier and
(b) the MCP server process binding that tool was spawned with a fixed-scope
argv. CI test asserts rendered MCP manifests contain no "which instance" tools.

### IV. Credential Scoping & Log Redaction

Credentials are per-instance, short-lived, and never baked into images. GitHub
push auth uses GitHub App installation tokens (≤1 hour TTL, scoped to the
specific repo bound to the instance). Cloudflare Tunnel credentials live in
Docker secrets, referenced (not embedded) from `instance_tunnels`. Heartbeat
HMAC secrets and instance DB passwords are generated at provision time and
injected via compose env — never committed, never logged. Before any log
reaches the chat UI or the LLM fallback envelope, it passes through a redactor
that masks bearer tokens, cloud provider keys, SSH keys, and `.env`-style
`KEY=value` pairs where the key matches `/SECRET|TOKEN|PASSWORD|KEY|AUTH/i`.

**Rationale**: The audit found a single shared PAT, raw stdout piped to Redis
pub/sub without redaction, and worker-local process handles. All three leak in
the multi-tenant model. Scoping + redaction are prerequisites, not polish.

**Enforcement**: The redactor is a single module (extend `audit_service.py`);
both the chat-UI path and the LLM-fallback path MUST call it. A unit test
asserts the redactor masks each category above. No new credential type may be
added without specifying its lifetime, storage, visibility, and rotation.

### V. Egress-Only Network Surface

Instance containers publish no host ports. The instance is reachable from the
internet **only** through its `cloudflared` sidecar and its named Cloudflare
Tunnel. Inside the compose network, services talk by service-name DNS. Ingress
is mediated by a per-instance `cloudflared` config rendered deterministically
from the orchestrator — not by an agent.

**Rationale**: Anonymous quick-tunnels leaked random hostnames and required
app-config rewriting after every rotation; host-port publication would create a
cross-tenant blast radius. Named tunnels give stable hostnames, per-instance
credentials, and per-instance teardown.

**Enforcement**: A CI test reads every rendered per-instance compose YAML and
fails the build if any service — other than `cloudflared` exposing its internal
metrics port — contains a `ports:` key. The sidecar-per-instance topology is
the chosen architecture (see audit §Outstanding before architecture doc, item
1); the shared-multiplexer variant is rejected for blast-radius and teardown
reasons documented in per-chat-instances.md §5.1.

### VI. Durable State, Idempotent Lifecycle

Source of truth for instance, tunnel, task, and credential state is Postgres.
Redis holds only ephemeral state (locks, pub/sub, cancel flags). No worker
process may hold long-lived handles (tunnel processes, MCP subprocesses, in-flight
tasks) in an in-memory dict as the canonical record — if the worker restarts,
the DB MUST still reflect reality and reconciliation MUST be possible.
Every lifecycle operation (`provision`, `destroy`, `rotate`, `reap`) is
idempotent and resumable: re-running it on a partial-success state is a no-op
or a forward-completion, never a duplicate-resource creation.

**Rationale**: The audit flagged `tunnel_service._active_processes` as a
distributed-systems smell — a pattern that must not repeat for instances or
credentials. Idempotency is what makes the reaper, the heartbeat, and the
stuck-lock cleaner safe to retry.

**Enforcement**: Partial unique indexes enforce "one active X per Y" invariants
at the DB layer (e.g., one active tunnel per instance). Every new lifecycle
operation's design note MUST list its idempotency key and its
partial-success recovery path.

### VII. Verified Work, No Half-Features

A task is not "done" until the full path is traced end-to-end (user action →
handler → service → worker → response), every function call resolves to a real
import, every new handler/route/module is registered in the appropriate wiring
code, and the code has been executed where possible (at minimum
`python -m py_compile`). Features that cannot finish end-to-end in the current
PR MUST NOT be partially landed; they are either scoped down to a complete
vertical slice or deferred.

**Evidence-based claims (AI contributors specifically)**:

- Before any edit: read the target file. Do not edit from memory.
- Before claiming a function, method, route, or flag exists: grep for it.
- Before citing a file path or line number: open it and confirm.
- No hallucinated APIs, endpoints, CLI flags, environment variables, or
  library behaviors — if it isn't in the code or the official tool help,
  it doesn't exist. Prefer "I don't know, let me check" over invention.
- Claims about behavior MUST cite `file_path:line_number` so a reviewer
  can verify independently.

**Rationale**: The CLAUDE.md "CRITICAL RULES" section exists because premature
"done" claims have historically broken wiring (missing registration, misnamed
imports). For AI contributors specifically, hallucinated APIs are the most
expensive failure mode — they compile, they pattern-match plausible code,
and they fail only at runtime far from the change.

**Enforcement**: Every PR description MUST state, per changed surface, one of:
"Done and verified" (with the verification command), "Done but unverified"
(with the manual-test list), or "Partially done" (with what works vs. what's
left). Every user-visible error path MUST include a navigation affordance
(Main Menu button for chat surfaces; no dead-end bare-text errors). Reviewers
reject claims about behavior that are not backed by a `file:line` citation.

### VIII. Root-Cause Fixes Over Bypasses (NON-NEGOTIABLE)

When a signal goes red — a pre-commit hook, a failing test, a type-check
error, a runtime assertion — the response order is: diagnose, then fix.
Bypassing the check is not on the list. The following are explicitly
prohibited unless the user has authorized them for the specific situation,
in writing, in the same conversation:

- `git commit --no-verify`, `--no-gpg-sign`, or skipping any hook.
- `git push --force` / `--force-with-lease` to shared branches.
- Commenting out or deleting a failing test to make CI pass.
- Catching an exception and swallowing it to silence an error.
- Adding broad `# type: ignore`, `# noqa`, or `except Exception: pass`
  to make a lint/type/test failure disappear.
- Downgrading or removing a dependency to avoid fixing a breakage.

If a hook or test appears broken on its own (not caused by the current
change), fix the hook or test in the same PR rather than routing around it.
If a root-cause fix is genuinely out of scope, open an issue, link it in
the PR, and confirm the bypass with the user before landing.

**Rationale**: AI contributors and humans alike are tempted to silence red
signals under time pressure. Every silenced signal is an incident waiting.
A codebase that tolerates bypasses loses the ability to trust its own CI.

**Enforcement**: Reviewers reject any diff that disables a check without an
accompanying issue link or user-approval quote. `[skip ci]`, `--no-verify`,
and equivalent bypass markers are banned in commit messages for `main` and
active release branches.

### IX. Async-Python Correctness

All I/O happens over `await`. No `requests.get`, no bare `time.sleep`, no
blocking `subprocess.run` inside an `async def` — use `httpx.AsyncClient`,
`asyncio.sleep`, `asyncio.create_subprocess_exec`. Every external call
(HTTP, `cloudflared`, Docker CLI, LLM, git) carries an explicit timeout;
"no timeout" is a bug, not a default. `asyncio.CancelledError` is
propagated, never swallowed — long-running tasks check for cancellation at
natural checkpoints (the orchestrator's 20-turn watchdog and bootstrap's
idle watchdog are the model). Background tasks are owned by a supervising
coroutine or ARQ job; `asyncio.create_task(...)` without a held reference
is prohibited.

**Rationale**: The stack (FastAPI + ARQ + asyncio + async SQLAlchemy +
aiogram) breaks silently under blocking I/O — one sync call in a hot
handler stalls the whole event loop. Missing timeouts turn transient
upstream issues into hung workers. Orphan `create_task` calls lose errors
and leak resources. These are the rules that keep the stack honest.

**Enforcement**: Code-review checklist item on every PR that touches async
code. Where practical, add `ruff` / `flake8-async` rules that catch
`asyncio-dangling-task`, `asyncio-sync-in-async`, and missing timeouts;
wire them into pre-commit. New external-call helpers MUST accept or set a
default timeout in their signature.

## Architecture Constraints

These constraints bind any new code, regardless of which principle is most
directly implicated.

- **Language & runtime**: Python 3.12 async (asyncio, async def, await) for
  orchestrator, API, workers, and services. `projctl` MAY be Go for single-binary
  distribution. No Node/TypeScript in the runtime path. Decided in audit
  §Outstanding item 4.
- **Orchestrator topology**: Single-tenant control plane (one `api` + one
  `worker` deployment). It is NOT replicated per chat. Horizontal scale is
  deferred past v1.
- **Instance identifier**: A single `slug` of form `inst-<14 hex>` (19 chars,
  under the 20-char DNS-label cap) is the stable identifier for DNS hostname,
  compose project name, network name, volume prefix, and every audit log row.
  Entropy floor is **≥56 bits** per FR-018a — anything less is guessable at
  scale once the public preview URLs are considered openly reachable. Do not
  introduce parallel identifier schemes.
- **Workspace layout**: Shared per-project clone cache at
  `/workspaces/_cache/<project_name>/` (read-only to instances); per-instance
  workspace is a **host bind mount** from `/workspaces/inst-<slug>/` into each
  compose service's `/app` (not a named Docker volume — the orchestrator-side
  `workspace_mcp` and `WorkspaceService.reattach_session_branch` must see the
  same bytes the containers do). The bind-mounted directory holds a git
  worktree pinned to the chat's session branch. Re-use
  `workspace_service.py` cache+worktree pattern; do not duplicate it.
- **Keep-as-is** (from audit §Keep/Refactor/Delete): `port_allocator.py`
  deterministic allocation pattern, `host_guard.py` / `docker_guard.py`
  allowlist+audit model, `web_chat.py` session-branch model, ARQ + Redis +
  Postgres stack.
- **Legacy path**: Existing `mode="host"` and `mode="docker"` projects
  continue to use the current bootstrap agent flow during the transition. New
  projects default to `mode="container"`. A one-line router in
  `bootstrap.py` routes between old and new. Host-mode deprecation is a
  separate decision.
- **Agent loops**: LLM agents decide; no hardcoded regex/if-elif chains for
  task routing, error diagnosis, or repair. Button-driven user actions are the
  only exception (user-initiated, finite state). Agents never get a `Bash`
  tool in their config; they use MCP tools with graceful error handling.
- **Shared agent utilities**: Use `worker/tasks/_agent_base.py` for tool
  descriptions and auth-error detection; use MCP factories in
  `providers/llm/claude.py` for MCP server configs. Do not inline.

## Development Workflow & Quality Gates

- **Planning**: Any task spanning 3+ files MUST go through Plan Mode before
  implementation. Break into small, verifiable sub-tasks.
- **Typecheck gate**: Every changed Python file is run through
  `python -m py_compile` at minimum before a task is marked done.
- **Restart vs rebuild**: Use `docker compose restart bot worker` for Python-only
  changes (5× faster); use `--build` only when `pyproject.toml`, `Dockerfile.worker`,
  or `Dockerfile.app` changed. Do not rebuild by default.
- **Constitution Check gate (in plan-template.md)**: Every feature plan MUST
  pass the Constitution Check before Phase 0 research and re-check after
  Phase 1 design. A violation requires an entry in the plan's Complexity
  Tracking table with the simpler alternative rejected and why.
- **Architecture docs as source of record**: Decisions that contradict
  `docs/architecture/audit.md` or `docs/architecture/per-chat-instances.md`
  require updating those docs in the same PR. The docs are the canonical
  reference for Keep/Refactor/Delete calls; the constitution is the canonical
  reference for non-negotiable principles.
- **PR reporting discipline**: Each PR states "Done and verified" / "Done but
  unverified" / "Partially done" per changed surface, with commands or manual
  test lists (Principle VII).
- **Scope respect**: A bug fix does not refactor surrounding code. A one-shot
  operation does not introduce a helper "while we're in here". Variable
  renames, file moves, and drive-by tidying happen in their own PRs. Propose
  scope expansions in the PR description or a follow-up issue; do not enact
  them in-line. Three similar lines is better than a premature abstraction.
- **Dependencies**: No new runtime or dev dependency without a line in the PR
  description stating what it provides that the stdlib / existing deps cannot,
  and why a smaller alternative was rejected. Prefer existing libraries
  (`httpx`, `sqlalchemy[asyncio]`, `redis.asyncio`, `pydantic`, `aiogram`,
  `arq`, `claude_agent_sdk`) before adding new ones.

## Governance

- **Authority**: This constitution supersedes ad-hoc guidance in other
  documents where they conflict. CLAUDE.md's CRITICAL RULES complement
  (not override) the principles here; if they diverge, the principle wins and
  CLAUDE.md is updated in the same PR.
- **Amendments**: An amendment PR MUST (a) update the version line at the
  bottom of this file per the semver rules below, (b) update the Sync Impact
  Report comment at the top, and (c) propagate changes across
  `.specify/templates/*.md`, `CLAUDE.md`, and architecture docs as applicable.
- **Versioning** (semver for governance):
  - **MAJOR**: Removing a principle, redefining one in a backward-incompatible
    way, or a governance change that invalidates prior compliance claims.
  - **MINOR**: Adding a new principle, materially expanding a principle's
    scope, or adding a new binding constraint.
  - **PATCH**: Clarifications, rewording, typo fixes, examples, non-semantic
    refinements.
- **Compliance review**: Every PR review MUST verify the seven principles.
  Violations that cannot be removed MUST be listed in the plan's Complexity
  Tracking with justification and a simpler-alternative-rejected rationale.
- **Runtime guidance**: For agent-facing runtime conventions (async style,
  agentic design rules, error-path affordances), see `CLAUDE.md`. For
  architectural context and the Keep/Refactor/Delete map, see
  `docs/architecture/audit.md` and `docs/architecture/per-chat-instances.md`.

**Version**: 1.1.0 | **Ratified**: 2026-04-23 | **Last Amended**: 2026-04-23
