#!/usr/bin/env python
"""Pipeline-integrity audit: ARQ ``enqueue_job(\"X\")`` ↔ worker registrations.

Catches the bug class where someone calls ``enqueue_job("typo_name", ...)``
or renames a worker function but forgets to update a caller. ARQ raises
``KeyError`` at job-pick-up time, NOT at enqueue time, so the failure
surfaces minutes/hours later in worker logs that nobody is watching.
Type checkers can't help — the job name is a string literal.

What the audit does:

  * AST-walks every ``.py`` under ``src/openclow/`` for
    ``enqueue_job("X", ...)`` calls and records ``X`` as a "called"
    job name.
  * AST-walks ``src/openclow/worker/arq_app.py::_load_functions`` for
    the actual function names returned in its list (these are the
    worker's job registrations — anything not in this list is
    unreachable).
  * Diffs:
      - **Called but not registered** → CRITICAL. Worker will raise
        ``KeyError`` when the job is dequeued.
      - **Registered but not called** → INFO. Possibly dead code, or
        called from outside src/ (cron, tests, manual triggers).

Exit codes:
  0  every called job is registered
  1  called-but-not-registered job names exist (CI fail)
  2  audit error (couldn't parse the file)

Same template as ``audit_stream_events.py``. See that file for the
broader rationale of pipeline-integrity audits.
"""
from __future__ import annotations

import ast
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "openclow"
ARQ_APP = SRC_ROOT / "worker" / "arq_app.py"

# Job names that are intentionally called from outside src/ — e.g. by
# ARQ crons, manual operator triggers, or tests. Add deliberately +
# document why the name has no enqueue_job() caller in the tree.
_REGISTERED_NO_CALLER_OK: set[str] = {
    "reaper_cron",                # registered as a cron, not enqueued by name
    "tunnel_health_check_cron",   # cron, same story
    "check_tunnel_health_task",   # invoked from arq_app's _tunnel_health_loop background task
    "claude_auth_get_url",        # called from WebSocket handler (api/routes/ws.py) not src/
    "rotate_github_token",        # invoked by future projctl cron via internal /rotate-git-token endpoint, not from src/
}

# Wrapper method/attribute names that ultimately delegate to the ARQ
# pool's enqueue_job. Calls of the form ``self.<name>("X", ...)`` or
# ``obj.<name>("X", ...)`` are treated as job-name references just like
# bare ``enqueue_job("X", ...)``. Add new wrappers as they emerge.
_ENQUEUE_WRAPPER_NAMES: set[str] = {
    "enqueue_job",
    "_enqueue",                # InstanceService injectable seam
    "_default_enqueuer",       # InstanceService default
    "job_enqueuer",             # generic constructor-arg name
}


def _called_job_names() -> set[str]:
    """Walk every .py under src/openclow/ for ``enqueue_job("X", ...)``.

    Skips the enqueue_job definition itself.
    """
    names: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            print(f"audit_arq_jobs: WARN: failed to parse {path}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Bare `enqueue_job(...)`, `module.enqueue_job(...)`, or any
            # known wrapper name (see _ENQUEUE_WRAPPER_NAMES).
            target = None
            if isinstance(func, ast.Name) and func.id in _ENQUEUE_WRAPPER_NAMES:
                target = func.id
            elif isinstance(func, ast.Attribute) and func.attr in _ENQUEUE_WRAPPER_NAMES:
                target = func.attr
            if target is None:
                continue
            if not node.args:
                continue
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                names.add(arg0.value)
            else:
                # Dynamic name — log to stderr so reviewers can audit.
                print(
                    f"audit_arq_jobs: NOTE dynamic job name in {path.relative_to(REPO_ROOT)}",
                    file=sys.stderr,
                )
    return names


def _registered_job_names() -> set[str]:
    """Parse arq_app.py::_load_functions and harvest the returned names.

    The convention is ``return [name1, name2, ...]`` where each name is
    a function reference. We collect the identifier names directly.
    """
    tree = ast.parse(ARQ_APP.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_load_functions"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.List):
                for elt in sub.value.elts:
                    if isinstance(elt, ast.Name):
                        names.add(elt.id)
    return names


def main() -> int:
    if not ARQ_APP.is_file():
        print(f"audit_arq_jobs: missing {ARQ_APP}", file=sys.stderr)
        return 2

    called = _called_job_names()
    registered = _registered_job_names()

    called_not_registered = called - registered
    registered_not_called = registered - called - _REGISTERED_NO_CALLER_OK

    print("=== arq-jobs pipeline audit ===")
    print(f"called by enqueue_job ({len(called)}): {sorted(called) or '(none)'}")
    print(f"registered in arq_app._load_functions ({len(registered)}): {sorted(registered) or '(none)'}")
    print()

    fail = False
    if called_not_registered:
        fail = True
        print(
            "CRITICAL: enqueue_job(\"X\") is called for jobs that are NOT "
            "registered in arq_app._load_functions — worker will KeyError at "
            "dequeue time:"
        )
        for n in sorted(called_not_registered):
            print(f"  - {n}")
        print()

    if registered_not_called:
        print(
            "INFO: registered job functions with no enqueue_job(\"X\") "
            "caller in src/ — possibly dead code, or called from cron / "
            "tests / manual triggers. Add to _REGISTERED_NO_CALLER_OK with "
            "a reason if intentional:"
        )
        for n in sorted(registered_not_called):
            print(f"  - {n}")
        print()

    if fail:
        print(
            "Audit FAILED. Either register the missing function in "
            "src/openclow/worker/arq_app.py::_load_functions, or correct "
            "the typo'd job name at the call site."
        )
        return 1

    print("Audit PASSED — every enqueued job name is registered with the worker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
