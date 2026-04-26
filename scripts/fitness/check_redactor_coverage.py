"""Fitness function: every chat-UI / LLM emit point that could carry
secrets passes through ``audit_service.redact``.

Constitution Principle IV (Credential Scoping & Log Redaction):
"Before any log reaches the chat UI or the LLM fallback envelope, it
passes through a redactor." A user-visible stream that streams raw
stderr can leak ``GITHUB_TOKEN=...`` or bearer headers — exactly the
C14 finding the speckit-analyze audit flagged before remediation.

This check walks ``api/routes/assistant.py`` for every
``controller.add_data({type: "tool_result", ...})`` call and asserts
the ``content`` field is wrapped in ``redact(...)`` (or a known
alias). Same idea for the ``explain`` endpoint's envelope handling.

Heuristic, not perfect — a determined dev can still construct a
content string out of unredacted variables. The goal is catching
**straightforward regressions** where someone reverts the redact()
wrapper or forgets it on a new emit point.
"""
from __future__ import annotations

import ast
import pathlib

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
ASSISTANT = REPO / "src" / "openclow" / "api" / "routes" / "assistant.py"

# Names treated as the redactor function. `redact` from audit_service
# is the canonical one; aliases (e.g. `_redact_chat`) are accepted.
_REDACTOR_NAMES = {"redact", "_redact_chat", "_redact"}


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="redactor_coverage",
        principles=["IV"],
        description="tool_result content emitted to chat goes through audit_service.redact().",
        passed=True,
    )

    if not ASSISTANT.is_file():
        result.error = f"missing {ASSISTANT}"
        result.passed = False
        return result

    tree = ast.parse(ASSISTANT.read_text(encoding="utf-8"))
    src_lines = ASSISTANT.read_text(encoding="utf-8").splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "add_data"
            and isinstance(func.value, ast.Name)
            and func.value.id == "controller"
        ):
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        # Only check emit sites whose type literal is "tool_result".
        type_val = None
        content_val = None
        for k, v in zip(node.args[0].keys, node.args[0].values):
            if not isinstance(k, ast.Constant):
                continue
            if k.value == "type" and isinstance(v, ast.Constant):
                type_val = v.value
            elif k.value == "content":
                content_val = v
        if type_val != "tool_result":
            continue
        if content_val is None:
            findings.append(Finding(
                severity=Severity.HIGH,
                message="tool_result emit has no content field",
                location=f"{ASSISTANT.relative_to(REPO)}:{node.lineno}",
            ))
            continue
        if not _looks_redacted(content_val):
            findings.append(Finding(
                severity=Severity.CRITICAL,
                message=(
                    "tool_result.content is not wrapped in a redactor call. "
                    "Principle IV: every emit to the chat UI MUST run "
                    "through audit_service.redact(). Wrap with redact() "
                    "before emitting."
                ),
                location=f"{ASSISTANT.relative_to(REPO)}:{node.lineno}",
            ))

    result.findings = findings
    result.passed = result.critical_count == 0
    return result


def _looks_redacted(node: ast.AST) -> bool:
    """True if the AST node is a call to a known redactor name."""
    # Direct: redact(x[:1500]) or _redact_chat(x[:1500])
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id in _REDACTOR_NAMES:
            return True
        if isinstance(f, ast.Attribute) and f.attr in _REDACTOR_NAMES:
            return True
    return False
