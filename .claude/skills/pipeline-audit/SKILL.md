---
name: "pipeline-audit"
description: "TAGH Dev architecture-fitness audit. Runs scripts/pipeline_fitness.py, which discovers every scripts/fitness/check_*.py and aggregates findings into a Markdown report mapped to constitution principles. Catches the bug class where backend emits/calls a thing the other side forgot to handle, where redactor coverage breaks on a new emit site, where MCP tools accidentally accept ambient-identifier arguments, where compose templates leak host ports, where httpx clients are constructed without timeouts, where ARQ enqueue_job names typo'd against the worker registry. Type checkers can't see these because the contracts are implicit (string keys, naming conventions, schema-vs-code drift). Run before claiming any feature done and before any e2e test session."
argument-hint: "Optional: comma-separated check names to run (default all). Append --json for machine output."
user-invocable: true
disable-model-invocation: false
---

## User Input

```text
$ARGUMENTS
```

If `$ARGUMENTS` is empty, run the full suite. Otherwise treat it as a comma-separated list of fitness-check names (e.g. `stream_event_contract,arq_job_contract`).

## Goal

Prove every architectural-fitness check defined under `scripts/fitness/check_*.py` passes. Each check enforces ONE invariant from the project constitution. The skill is the canonical "is the system internally consistent?" gate — run it BEFORE you reach for Playwright or another expensive test runner.

This skill is **read-only**. It runs the suite, reports the findings, recommends concrete fix sites with `file:line` citations. It NEVER edits code.

## Operating Constraints

- **STRICTLY READ-ONLY.** The fitness checks themselves are read-only by contract; this skill MUST NOT add edits on top.
- **Authoritative report.** When a check fails, quote the script's output verbatim — do not summarise away severity or location.
- **Map to principles.** Every failure cites which constitution principle the check enforces. The user should be able to read the report and know which non-negotiable was violated.
- **Honest about scope.** The fitness suite catches **static** contracts (what types, names, signatures, structures). It does NOT catch behavioural correctness — that's what tests are for.

## How the suite works

`scripts/pipeline_fitness.py` is the runner. It:

1. Discovers every file under `scripts/fitness/check_*.py`.
2. Imports each module, calls its `check()` function.
3. Each `check()` returns a `FitnessResult(name, principles, description, passed, findings)`.
4. Aggregates results into a Markdown report (or JSON via `--json`).
5. Exit codes: 0 (clean), 1 (findings at or above the `--fail-on` threshold; default `critical`), 2 (a check crashed).

### Adding a new fitness function

When the system grows a new contract surface, add `scripts/fitness/check_<name>.py` exporting a `check() -> FitnessResult` function. The runner picks it up automatically. Map the function to one or more constitution principles via the `principles` field of the result. Keep each check ≤200 lines, ≤2 s offline, deterministic.

## Execution Steps

### 1. Run the suite

Default: `python scripts/pipeline_fitness.py`.

If `$ARGUMENTS` names specific checks, pass them as `--check <name1>,<name2>`. Validate the names exist under `scripts/fitness/` first; if any don't, list the valid set and exit cleanly.

### 2. Aggregate the output

The runner already produces a Markdown report. Pass it through verbatim. Add a one-line summary above it:

> "X of Y checks pass. Z critical findings. W high. See per-check details below."

### 3. Recommend next actions

For each failing check, drill into the findings and recommend concrete file edits with `file:line` citations.

Common patterns:

- **`stream_event_contract` → frontend handler missing**: point at `chat_frontend/src/App.tsx:120-128` (or wherever `parseStream` lives) and reference Phase 10 task IDs (T100–T106). Show the exact `case` arm template.
- **`arq_job_contract` → name not registered**: point at `src/openclow/worker/arq_app.py::_load_functions` and quote the missing function name.
- **`no_ambient_args`** failure: point at the offending `@mcp.tool` definition and show how to reshape the args without an ambient identifier.
- **`compose_no_host_ports`** failure: point at the `ports:` line in the compose template and recommend moving ingress to the cloudflared sidecar.
- **`redactor_coverage`** failure: point at the unguarded `controller.add_data` site and show the `redact()` wrap.
- **`timeouts`** failure: point at the `httpx.AsyncClient(...)` site and show the `timeout=DEFAULT_TIMEOUT` parameter.

### 4. Honest reporting rules

- NEVER claim the suite passed without running it. The runner's exit code is your source of truth.
- NEVER summarise away the principle citations. The user needs to know WHICH constitutional invariant is at stake.
- NEVER suggest skipping a check or whitelisting a finding without an explicit reason from the user. Constitution conflicts are CRITICAL by definition (Principle VIII: Root-Cause Fixes Over Bypasses).

## What this skill is NOT

- Not a runtime monitor — it's static.
- Not a replacement for tests — it catches "A calls B but B doesn't know about it", not "A calls B and B does the wrong thing".
- Not a security scanner — use `/security-review` for that.
- Not a refactorer — it reports, you (or another skill) fix.

## When to invoke

- **Before claiming "feature done"** on any change that touches: stream events, ARQ jobs, MCP tools, compose templates, async I/O.
- **Before starting an end-to-end test session** — Playwright, manual quickstart walks, anything expensive. Static contract check first.
- **After `/speckit-analyze`** as a complementary check — analyze runs over spec/plan/tasks artifacts; this runs over actual code.
- **In a code review**, on the changed files, to spot drift the diff introduces.
- **In CI** — the runner is wired into pre-commit (see `.pre-commit-config.yaml::audit-pipeline-fitness`) and any CI workflow that runs pre-commit-hooks-on-changed-files will pick it up.

## Today's check catalogue

(Verify by `ls scripts/fitness/check_*.py` before claiming any of these run.)

| Check | Principle(s) | What it asserts |
|-------|---|---|
| `stream_event_contract` | VII, VIII | Backend `controller.add_data` event types match the JSON schema, the runtime `_REQUIRED_BY_TYPE` table is in sync, the generated TS types are fresh, every schema type has a frontend handler. |
| `arq_job_contract` | VII, VI | Every `enqueue_job("X", ...)` name is registered in `arq_app._load_functions`. |
| `api_route_contract` | VII | Every frontend `fetch('/api/X')` URL is served by some FastAPI route (path-parameter aware). |
| `mcp_tool_contract` | III, VII | Every `mcp__<server>__<tool>` string in `CONTAINER_MODE_TOOLS` corresponds to a real `@mcp.tool()` registration on the pinned MCP server. |
| `db_model_drift` | VI, VII | Every SQLAlchemy `mapped_column` declaration is reflected in alembic migration history. Optional live `alembic check --autogenerate` when `DATABASE_URL` is reachable. |
| `no_ambient_args` | III | No `@mcp.tool` parameter name contains `instance`/`project`/`workspace`/`container`. |
| `compose_no_host_ports` | V | No service in any per-instance compose template publishes host ports outside cloudflared. |
| `redactor_coverage` | IV | Every `tool_result` emit wraps `content` in a known redactor function. |
| `timeouts` | IX | Every `httpx.AsyncClient(...)` in `services/` passes a `timeout=` kwarg. |

To add new checks, drop `scripts/fitness/check_<name>.py` and follow the existing template. The runner discovers them; this catalogue should be updated to keep humans informed.
