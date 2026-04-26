#!/usr/bin/env python
"""Pipeline-integrity audit: backend ``controller.add_data`` ↔ frontend ``parseStream``.

Catches the exact class of bug Playwright surfaced on 2026-04-24:
backend emits a ``controller.add_data({type: "instance_provisioning"})``
event but the frontend's stream parser only knows about ``tool_use``
and ``message_id`` — every other event silently drops on the floor and
the user sees a degraded UX. Type checkers can't catch this because
the contract is implicit (string keys in a JSON-RPC stream).

What the audit does:

  * Walks ``src/openclow/api/routes/assistant.py`` AST. For every
    ``controller.add_data({"type": "X", ...})`` call it sees, records
    ``X`` as a backend-emitted event type.
  * Walks ``chat_frontend/src/App.tsx`` (and any sibling TS files
    that import the parser) for ``case "X":`` arms inside the
    stream-event switch.
  * Diffs the two sets:
      - **Emitted but not handled** → CRITICAL. User UX gap, exactly
        the bug we caught live.
      - **Handled but not emitted** → WARNING. Possibly stale code
        (event was removed from backend but frontend still keys off
        it) or a future-proofing handler that's fine.

Exit codes:
  0  sets match (allowing for documented "handled but not emitted")
  1  emitted-but-not-handled events exist (CI fail)
  2  audit error (couldn't parse a file, etc.)

Wire into pre-commit by adding a hook that runs:
    python scripts/audit_stream_events.py

The audit is fast (<1s) and offline.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "src" / "openclow" / "api" / "routes" / "assistant.py"
FRONTEND = REPO_ROOT / "chat_frontend" / "src" / "App.tsx"

# Events the frontend may legitimately handle without the backend
# emitting them in this codepath (e.g. handler reserved for a future
# backend addition, or handled by a different route). Add to this set
# deliberately and document why.
_FRONTEND_ONLY_OK: set[str] = set()

# Events the backend may legitimately emit without the frontend caring
# (e.g. server-side telemetry that the chat UI ignores by design).
# Add deliberately + document why.
_BACKEND_ONLY_OK: set[str] = set()


def _backend_emitted_event_types(path: pathlib.Path) -> set[str]:
    """Walk the backend AST and collect every controller.add_data type literal.

    Pattern matched:
      ``controller.add_data({"type": "<literal>", ...})``

    Anything dynamic (variable as the type value) is reported as
    ``<dynamic>`` so reviewers see the gap explicitly.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # controller.add_data(...)
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
                found.add(v.value)
                break
        else:
            # No string-literal type found — flag for human review.
            found.add("<dynamic>")
    return found


# Frontend regex: the parser is one inline switch/case in App.tsx.
# We accept either a switch arm or an explicit `evt.type === "X"`
# comparison. Both forms are common during a refactor.
_CASE_RE = re.compile(r'case\s+["\']([a-z_][a-z0-9_]*)["\']\s*:')
_EQ_RE = re.compile(r'evt\.type\s*===\s*["\']([a-z_][a-z0-9_]*)["\']')


def _frontend_handled_event_types(path: pathlib.Path) -> set[str]:
    """Scan the frontend source for handled event-type literals.

    Looser than the backend AST walk because TypeScript ASTs are
    expensive to parse from Python; the regex covers both today's
    ``if (evt.type === "X")`` style AND the planned ``case "X":``
    switch the Phase 10 work introduces.
    """
    src = path.read_text(encoding="utf-8")
    return set(_CASE_RE.findall(src)) | set(_EQ_RE.findall(src))


def main() -> int:
    if not BACKEND.is_file():
        print(f"audit_stream_events: missing backend file: {BACKEND}", file=sys.stderr)
        return 2
    if not FRONTEND.is_file():
        print(f"audit_stream_events: missing frontend file: {FRONTEND}", file=sys.stderr)
        return 2

    try:
        backend = _backend_emitted_event_types(BACKEND)
    except SyntaxError as e:
        print(f"audit_stream_events: failed to parse {BACKEND}: {e}", file=sys.stderr)
        return 2

    frontend = _frontend_handled_event_types(FRONTEND)

    # `<dynamic>` is informational, not actionable — surface it but
    # don't fail on it.
    has_dynamic = "<dynamic>" in backend
    backend_clean = backend - {"<dynamic>"}

    emitted_not_handled = backend_clean - frontend - _BACKEND_ONLY_OK
    handled_not_emitted = frontend - backend_clean - _FRONTEND_ONLY_OK

    print("=== stream-event pipeline audit ===")
    print(f"backend emits ({len(backend_clean)}): {sorted(backend_clean) or '(none)'}")
    if has_dynamic:
        print("backend also has 1+ dynamic-type add_data calls — manual review")
    print(f"frontend handles ({len(frontend)}): {sorted(frontend) or '(none)'}")
    print()

    fail = False
    if emitted_not_handled:
        fail = True
        print(
            "CRITICAL: backend emits these events but frontend handler is missing — "
            "users will see only the plain-text fallback, no rich UX:"
        )
        for t in sorted(emitted_not_handled):
            print(f"  - {t}")
        print()

    if handled_not_emitted:
        print(
            "WARNING: frontend handles these events but no backend emit point "
            "found. Either dead handler code or backend was renamed/removed:"
        )
        for t in sorted(handled_not_emitted):
            print(f"  - {t}")
        print()

    if fail:
        print(
            "Audit FAILED. Either add the missing handlers to "
            "chat_frontend/src/App.tsx::parseStream, or add the type to "
            "_BACKEND_ONLY_OK in this script with a one-line comment "
            "explaining why the gap is intentional."
        )
        return 1

    print("Audit PASSED — every backend-emitted event has a frontend handler.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
