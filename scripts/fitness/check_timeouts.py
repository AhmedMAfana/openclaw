"""Fitness function: every external HTTP call carries an explicit timeout.

Constitution Principle IX (Async-Python Correctness): "Every external
call (HTTP, cloudflared, Docker CLI, LLM, git) carries an explicit
timeout; 'no timeout' is a bug, not a default."

Walks every ``.py`` under ``src/openclow/services/`` for
``httpx.AsyncClient(...)`` constructions and asserts each one passes
a ``timeout=`` kwarg (or sets ``DEFAULT_TIMEOUT`` via the constant
named ``DEFAULT_TIMEOUT``, which the project uses everywhere).

Conservative scope — only checks the services layer because that's
where every external call originates per the architecture. Adding
the workers layer would cover the ARQ jobs that shell out to
``cloudflared``; flagged as future extension.
"""
from __future__ import annotations

import ast
import pathlib

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICES = REPO / "src" / "openclow" / "services"


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="timeouts",
        principles=["IX"],
        description="Every httpx.AsyncClient construction in services/ has a timeout.",
        passed=True,
    )

    for path in sorted(SERVICES.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            # match httpx.AsyncClient(...)
            if not (
                isinstance(f, ast.Attribute)
                and f.attr == "AsyncClient"
                and isinstance(f.value, ast.Name)
                and f.value.id == "httpx"
            ):
                continue
            kw_names = {k.arg for k in node.keywords if k.arg}
            if "timeout" not in kw_names:
                findings.append(Finding(
                    severity=Severity.HIGH,
                    message=(
                        "httpx.AsyncClient(...) constructed without an "
                        "explicit timeout= kwarg. Principle IX: 'no timeout' "
                        "is a bug. Pass DEFAULT_TIMEOUT (constant in the "
                        "same module) or build an httpx.Timeout object."
                    ),
                    location=f"{path.relative_to(REPO)}:{node.lineno}",
                ))

    result.findings = findings
    result.passed = result.high_count == 0
    return result
