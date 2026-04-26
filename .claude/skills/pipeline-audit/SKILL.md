---
name: "pipeline-audit"
description: "Static contract-drift audit across the TAGH Dev pipeline. Catches the bug class where backend emits/calls a thing the other side forgot to handle — stream-event drift (backend `controller.add_data` vs frontend `parseStream`), ARQ job dead-ends (`enqueue_job(\"X\")` vs registered worker functions), API-route drift (frontend `fetch('/api/X')` vs FastAPI routes), MCP tool-name drift. Type checkers can't see these contracts because they live in string keys. Run before claiming \"feature done\" and before any e2e test session."
argument-hint: "Optional: specific audit to run (stream-events | arq-jobs | api-routes | mcp-tools | all). Default: all."
user-invocable: true
disable-model-invocation: false
---

## User Input

```text
$ARGUMENTS
```

If `$ARGUMENTS` is empty or `all`, run every audit listed below in order.
If `$ARGUMENTS` matches a single audit name, run only that one.
If `$ARGUMENTS` is unrecognised, list the available audits and exit cleanly without running anything.

## Goal

Prove the project's cross-component contracts hold **before** anyone runs a test session. The contracts checked here are all of the form "side A emits / calls a string that side B is expected to recognise" — JSON-RPC event types, ARQ job names, REST URL paths, MCP tool identifiers. Type checkers and linters are blind to these. Tests catch them only after the fact, and only the cases someone wrote tests for.

This skill is **read-only**. It runs the audit scripts under `scripts/audit_*.py`, reports the findings, and recommends concrete next actions. It does NOT edit code on its own.

## Operating Constraints

- **STRICTLY READ-ONLY**. No file edits. No git operations. Just run the scripts and report.
- **Fast**. Each individual audit must finish in under 2 seconds offline. If something needs network or a running stack, it does not belong here.
- **Deterministic**. Re-running with no code changes must produce identical output.
- **Honest**. If an audit script doesn't exist yet, report that, don't pretend it ran.

## Available audits

The skill discovers audits by listing `scripts/audit_*.py`. Today's catalogue (verify by `ls scripts/audit_*.py` before claiming any of them exist):

| Audit | Script | Catches |
|---|---|---|
| `stream-events` | `scripts/audit_stream_events.py` | Backend `controller.add_data({type: "X"})` calls that have no matching `case "X":` arm in `chat_frontend/src/App.tsx::parseStream`. Was used to find the 9-event UI gap on 2026-04-24. |
| `arq-jobs` | `scripts/audit_arq_jobs.py` *(may not exist yet — report if absent)* | `enqueue_job("X", ...)` references that don't match a function registered in `worker/arq_app.py::_load_functions`. |
| `api-routes` | `scripts/audit_api_routes.py` *(may not exist yet)* | Frontend `fetch('/api/X')` URLs that no FastAPI router serves. |
| `mcp-tools` | `scripts/audit_mcp_tools.py` *(may not exist yet)* | Tool names referenced in `providers/llm/claude.py::CONTAINER_MODE_TOOLS` (or any `allowed_tools` list) that aren't registered by an `@mcp.tool()` decorator on the corresponding MCP server. |

If a referenced script does not exist, the skill MUST say so explicitly — do not invent its output.

## Execution Steps

### 1. Resolve which audits to run

Parse `$ARGUMENTS`. Map to the catalogue above. If empty/`all`, set the run list to every script that actually exists under `scripts/audit_*.py`.

### 2. Run each script via Bash

For each audit in the run list:

- Invoke `python scripts/audit_<name>.py` (or whatever its filename is).
- Capture stdout, stderr, and exit code.
- Bound it: if the script takes longer than 30 seconds, kill it and treat it as a failure with cause "audit timed out — likely needs network or a running stack, file a bug".

### 3. Aggregate results into a structured report

Output one Markdown section per audit:

```
## <audit-name>
**Status**: PASS / FAIL / SCRIPT-MISSING / TIMEOUT
**Exit code**: <n>
**Findings**:
<the script's stdout>
```

Then a top-level summary table:

| Audit | Result | Critical | Warnings |
|---|---|---:|---:|
| stream-events | FAIL | 9 | 0 |
| ... |

### 4. Recommend next actions

If any audit FAILED:

- For `stream-events` failures: point at `chat_frontend/src/App.tsx::parseStream` and quote the exact `case "X":` arms the user needs to add. Reference Phase 10 task IDs (T100–T106) if they exist in `tasks.md`.
- For `arq-jobs` failures: point at `worker/arq_app.py::_load_functions` and quote the missing function name.
- For `api-routes` failures: point at the FastAPI router file the URL pattern would belong to (`api/routes/<group>.py`).
- For `mcp-tools` failures: point at the MCP server file (`mcp_servers/<server>.py`) and the factory in `providers/llm/claude.py`.

If all audits PASSED:

- State that the static contracts hold.
- Remind the caller this only covers contract drift — semantic correctness still requires tests.
- Suggest the next gate: `python -m py_compile` on changed files, then any relevant `pytest` selection, then `quickstart.md` walk-through against staging if the change touches user-facing code.

### 5. Honest reporting rules

- NEVER claim an audit script exists without checking. `ls scripts/audit_*.py` is your source of truth.
- NEVER claim an audit passed without seeing its exit code = 0. Treat any uncertainty as a fail-loud.
- NEVER edit files. If a fix is obvious, recommend it; do NOT apply it.
- Cite `file_path:line_number` when pointing at the next-action site so the caller can navigate directly. (Constitution Principle VII: evidence-based claims.)

## What this skill is NOT

- Not a runtime monitor. It only checks **static** contracts.
- Not a replacement for tests. It catches "A calls B but B doesn't know about it" — not "A calls B and B does the wrong thing".
- Not a security scan. Use `/security-review` for that.
- Not a refactor. Use plain editing for that.

## When to invoke

- Before claiming "feature done" on any change that touches a string-keyed contract surface (chat UI events, ARQ jobs, REST routes, MCP tools).
- Before starting an end-to-end test session — Playwright in particular is expensive to set up and you want to know the static gaps first.
- After a `/speckit-analyze` run, as a complementary check on the actual code (analyze checks artifacts; this checks code).
- During a code review, on the changed files, to spot drift introduced by the diff.

## Extending the audit suite

When you add a new contract surface to the system, add a matching audit script under `scripts/audit_<name>.py` following the same template:

1. AST-walk one side, regex-scan the other.
2. Diff the two sets.
3. Print exit-1 with a CRITICAL summary on drift.
4. Add a row to the catalogue table at the top of this SKILL.md.
5. Wire into `.pre-commit-config.yaml` so commits drifting it fail locally.

The pattern is intentionally one-script-per-contract so each is small, fast, and easy to read. Don't try to build a single mega-audit.
