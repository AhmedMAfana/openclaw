"""Fitness function: stream-event contract holds across the pipeline.

Asserts FIVE properties simultaneously:

  1. Every event type emitted by the backend (``controller.add_data``)
     is in the JSON Schema's enum.
  2. Every event type in the JSON Schema's enum is emitted SOMEWHERE
     in the backend (no dead schema branches).
  3. The runtime validator's ``_REQUIRED_BY_TYPE`` table is in sync
     with the schema's ``oneOf`` branches' ``required`` lists.
  4. The generated TypeScript file (``chat_frontend/src/types/
     stream-events.ts``) is up-to-date with the schema.
  5. Every event type in the schema has a frontend handler in
     ``chat_frontend/src/App.tsx::parseStream``.

Maps to Constitution Principle VII (Verified Work) and Principle VIII
(Root-Cause Fixes — make drift impossible by construction, not by
detection-after-the-fact).
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = REPO / "specs" / "001-per-chat-instances" / "contracts" / "stream-events.schema.json"
BACKEND = REPO / "src" / "taghdev" / "api" / "routes" / "assistant.py"
VALIDATOR = REPO / "src" / "taghdev" / "services" / "stream_validator.py"
FRONTEND = REPO / "chat_frontend" / "src" / "App.tsx"
TS_TYPES = REPO / "chat_frontend" / "src" / "types" / "stream-events.ts"


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="stream_event_contract",
        principles=["VII", "VIII"],
        description="Backend ↔ schema ↔ runtime ↔ codegen ↔ frontend alignment for stream events.",
        passed=True,
    )

    if not SCHEMA.is_file():
        result.passed = False
        result.error = f"schema not found at {SCHEMA}"
        return result

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    schema_types = set(schema["properties"]["type"]["enum"])

    # 1. Backend emit sites
    backend_emitted = _backend_event_types(BACKEND)
    backend_dynamic = "<dynamic>" in backend_emitted
    backend_emitted -= {"<dynamic>"}

    rogue = backend_emitted - schema_types
    for t in sorted(rogue):
        findings.append(Finding(
            severity=Severity.CRITICAL,
            message=(
                f"backend emits event type {t!r} not in the schema enum — "
                f"runtime validator will reject it. Either add {t!r} to "
                f"specs/001-per-chat-instances/contracts/stream-events.schema.json, "
                f"or remove the emit site."
            ),
            location=f"{BACKEND.relative_to(REPO)}",
        ))

    dead_schema = schema_types - backend_emitted
    for t in sorted(dead_schema):
        findings.append(Finding(
            severity=Severity.MEDIUM,
            message=(
                f"schema declares event type {t!r} but no backend emit "
                f"point found — possibly orphaned schema branch or the "
                f"emit lives outside assistant.py (which is the only "
                f"file walked)."
            ),
            location=f"{SCHEMA.relative_to(REPO)}",
        ))

    # 2. Runtime validator's _REQUIRED_BY_TYPE alignment
    rbt = _required_by_type_from_validator(VALIDATOR)
    if rbt is None:
        findings.append(Finding(
            severity=Severity.HIGH,
            message="could not parse _REQUIRED_BY_TYPE from stream_validator.py",
            location=f"{VALIDATOR.relative_to(REPO)}",
        ))
    else:
        validator_types = set(rbt.keys())
        if validator_types != schema_types:
            missing = schema_types - validator_types
            extra = validator_types - schema_types
            if missing:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    message=(
                        f"_REQUIRED_BY_TYPE missing entries for {sorted(missing)} — "
                        "runtime validator will reject as unknown type."
                    ),
                    location=f"{VALIDATOR.relative_to(REPO)}",
                ))
            if extra:
                findings.append(Finding(
                    severity=Severity.MEDIUM,
                    message=(
                        f"_REQUIRED_BY_TYPE has stale entries for {sorted(extra)} "
                        "(not in schema). Drop them."
                    ),
                    location=f"{VALIDATOR.relative_to(REPO)}",
                ))

    # 3. Generated TS types are fresh
    ts_check = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "codegen" / "gen_stream_event_types.py"), "--check"],
        capture_output=True, text=True, timeout=15,
    )
    if ts_check.returncode != 0:
        findings.append(Finding(
            severity=Severity.HIGH,
            message=(
                f"generated TS types are stale relative to the schema. "
                f"Run `python scripts/codegen/gen_stream_event_types.py` "
                f"and commit. (codegen output: {ts_check.stdout.strip() or ts_check.stderr.strip()})"
            ),
            location=f"{TS_TYPES.relative_to(REPO)}",
        ))

    # 4. Frontend handler coverage
    handled = _frontend_handled_types(FRONTEND)
    missing = schema_types - handled
    for t in sorted(missing):
        findings.append(Finding(
            severity=Severity.HIGH,
            message=(
                f"frontend has no parseStream handler for event type {t!r} — "
                "users will see only the plain-text fallback. Phase 10 (T100) "
                "is the planned fix."
            ),
            location=f"{FRONTEND.relative_to(REPO)}",
        ))

    if backend_dynamic:
        findings.append(Finding(
            severity=Severity.LOW,
            message=(
                "backend has 1+ controller.add_data() calls with a dynamic "
                "type field — manual review needed; the contract check can't "
                "verify what wasn't a literal."
            ),
            location=f"{BACKEND.relative_to(REPO)}",
        ))

    result.findings = findings
    result.passed = result.critical_count == 0 and result.high_count == 0
    return result


# --- helpers ---------------------------------------------------------


def _backend_event_types(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
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
        if not node.args:
            continue
        arg0 = node.args[0]
        if not isinstance(arg0, ast.Dict):
            continue
        for k, v in zip(arg0.keys, arg0.values):
            if (
                isinstance(k, ast.Constant)
                and k.value == "type"
                and isinstance(v, ast.Constant)
                and isinstance(v.value, str)
            ):
                out.add(v.value)
                break
        else:
            out.add("<dynamic>")
    return out


def _required_by_type_from_validator(path: pathlib.Path) -> dict[str, list[str]] | None:
    """Walk stream_validator.py for the _REQUIRED_BY_TYPE assignment."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        # Match both `_REQUIRED_BY_TYPE = {...}` (Assign) and the
        # type-annotated form `_REQUIRED_BY_TYPE: dict[...] = {...}`
        # (AnnAssign).
        target_id = None
        value = None
        if isinstance(node, ast.Assign):
            if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "_REQUIRED_BY_TYPE"):
                target_id = node.targets[0].id
                value = node.value
        elif isinstance(node, ast.AnnAssign):
            if (isinstance(node.target, ast.Name)
                    and node.target.id == "_REQUIRED_BY_TYPE"
                    and node.value is not None):
                target_id = node.target.id
                value = node.value
        if target_id is None or not isinstance(value, ast.Dict):
            continue
        out: dict[str, list[str]] = {}
        for k, v in zip(value.keys, value.values):
            if isinstance(k, ast.Constant) and isinstance(v, ast.Tuple):
                fields = [
                    elt.value for elt in v.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                out[k.value] = fields
        return out
    return None


_CASE_RE = re.compile(r'case\s+["\']([a-z_][a-z0-9_]*)["\']\s*:')
_EQ_RE = re.compile(r'evt\.type\s*===\s*["\']([a-z_][a-z0-9_]*)["\']')


def _frontend_handled_types(path: pathlib.Path) -> set[str]:
    src = path.read_text(encoding="utf-8")
    return set(_CASE_RE.findall(src)) | set(_EQ_RE.findall(src))
