"""Fitness function: ARQ ``enqueue_job("X", ...)`` ↔ worker registrations.

Reuses the audit logic from ``scripts/audit_arq_jobs.py``. Catches:

  * Called job names that aren't registered → worker KeyErrors at
    dequeue. Caught a real bug in the
    ``chat_session_service.delete_chat_cascade`` / ``gc_session_branch``
    pair on 2026-04-24.

Maps to Constitution Principle VII (Verified Work — function calls
must resolve to a real registration) and Principle VI (Durable State,
Idempotent Lifecycle — every lifecycle operation has a registered
job).
"""
from __future__ import annotations

import ast
import pathlib

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "openclow"
ARQ_APP = SRC / "worker" / "arq_app.py"

_REGISTERED_NO_CALLER_OK: set[str] = {
    "reaper_cron",
    "tunnel_health_check_cron",
    "check_tunnel_health_task",
    "claude_auth_get_url",
    "rotate_github_token",
}

_ENQUEUE_WRAPPER_NAMES: set[str] = {
    "enqueue_job", "_enqueue", "_default_enqueuer", "job_enqueuer",
}


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="arq_job_contract",
        principles=["VII", "VI"],
        description="enqueue_job(\"X\") names match arq_app._load_functions registrations.",
        passed=True,
    )

    if not ARQ_APP.is_file():
        result.passed = False
        result.error = f"missing {ARQ_APP}"
        return result

    called = _called_names()
    registered = _registered_names()

    called_not_registered = called - registered
    for name in sorted(called_not_registered):
        findings.append(Finding(
            severity=Severity.CRITICAL,
            message=(
                f"enqueue_job({name!r}, ...) is called but {name!r} is NOT "
                f"in arq_app._load_functions — worker will KeyError at "
                f"dequeue time."
            ),
            location=f"{ARQ_APP.relative_to(REPO)}",
        ))

    registered_not_called = registered - called - _REGISTERED_NO_CALLER_OK
    for name in sorted(registered_not_called):
        findings.append(Finding(
            severity=Severity.LOW,
            message=(
                f"job {name!r} is registered but never enqueued from src/. "
                "Either dead code, or invoked from cron / external tests / "
                "manual triggers — add to _REGISTERED_NO_CALLER_OK with a "
                "reason if intentional."
            ),
            location=f"{ARQ_APP.relative_to(REPO)}",
        ))

    result.findings = findings
    result.passed = result.critical_count == 0
    return result


def _called_names() -> set[str]:
    out: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id in _ENQUEUE_WRAPPER_NAMES:
                pass
            elif isinstance(f, ast.Attribute) and f.attr in _ENQUEUE_WRAPPER_NAMES:
                pass
            else:
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                out.add(node.args[0].value)
    return out


def _registered_names() -> set[str]:
    tree = ast.parse(ARQ_APP.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_load_functions"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.List):
                for elt in sub.value.elts:
                    if isinstance(elt, ast.Name):
                        out.add(elt.id)
    return out
