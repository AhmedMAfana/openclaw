"""Fitness function: assistant_endpoint never injects hardcoded English
into the LLM's text channel.

Constitution Principle II (deterministic infra status flows through
structured events, not impersonated LLM voice) and Principle VII
(verified work — every emit point on the assistant stream must be
either real LLM output or a structured `controller.add_data` card).

The bug this guards against: a fresh chat replied with a kiosk-style
bullet menu, partly because `controller.append_text("...")` calls in
``assistant.py`` were injecting orchestrator prose into the SAME
text channel the LLM streams its tokens through. The user sees them
indistinguishably; the prose impersonates the LLM.

Allowed:
  * ``controller.append_text(text)`` where ``text`` is a name (a
    variable carrying real LLM tokens).
  * ``controller.append_text(...)`` inside files NOT in the assistant
    pipeline (this check is scoped to ``api/routes/assistant.py``).

Forbidden in ``api/routes/assistant.py``:
  * ``controller.append_text("...")`` with a string-literal argument.
  * ``controller.append_text(f"...")`` with an f-string argument.

Surfaced as a CRITICAL finding so the pipeline-fitness suite fails
on regressions.
"""
from __future__ import annotations

import ast
import pathlib

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
TARGETS = (
    REPO / "src" / "taghdev" / "api" / "routes" / "assistant.py",
)


def _is_string_literal(node: ast.expr) -> bool:
    """True if the node evaluates to a hardcoded string (no LLM origin).

    ``ast.Constant`` with a str value is a plain string. ``ast.JoinedStr``
    is an f-string — we treat it as hardcoded too, because every
    legitimate LLM-token call passes a bare variable, never an f-string.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    return False


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="no_hardcoded_assistant_text",
        principles=["II", "VII"],
        description=(
            "assistant.py never injects hardcoded English / f-strings "
            "via controller.append_text — orchestrator status flows "
            "through controller.add_data cards instead."
        ),
        passed=True,
    )

    for path in TARGETS:
        if not path.is_file():
            findings.append(Finding(
                severity=Severity.HIGH,
                message=f"target file missing: {path.relative_to(REPO)}",
                location=str(path.relative_to(REPO)),
            ))
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                message=f"could not parse {path.name}: {e}",
                location=str(path.relative_to(REPO)),
            ))
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # match `<anything>.append_text(...)` — the receiver name
            # in this pipeline is `controller`, but a regression that
            # renames it shouldn't slip past the check.
            if not (
                isinstance(func, ast.Attribute) and func.attr == "append_text"
            ):
                continue
            if not node.args:
                continue
            arg0 = node.args[0]
            if not _is_string_literal(arg0):
                continue
            findings.append(Finding(
                severity=Severity.CRITICAL,
                message=(
                    f"controller.append_text() called with a hardcoded "
                    f"string/f-string at {path.name}:{node.lineno}. "
                    f"Orchestrator status MUST flow through "
                    f"controller.add_data({{type: ...}}) so the UI can "
                    f"render it as a card; the text channel is reserved "
                    f"for genuine LLM tokens (Principle II)."
                ),
                location=f"{path.relative_to(REPO)}:{node.lineno}",
            ))

    result.findings = findings
    result.passed = result.critical_count == 0
    return result
