"""Spec 003 — admin role guard regression test.

Pins FR-003: every admin instance endpoint MUST raise 403 when called by a
non-admin authenticated user. The check is centralized via
``access._require_admin``; this test asserts each handler in
``api/routes/admin_instances.py`` calls it and uses ``Depends(web_user_dep)``.

We use AST parsing rather than regex so multi-line handler signatures are
handled correctly.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_ADMIN_ROUTES_FILE = Path(__file__).resolve().parents[2] / "src" / "openclow" / "api" / "routes" / "admin_instances.py"


def _route_handlers() -> dict[str, ast.AsyncFunctionDef]:
    """Return mapping of handler-name → its AST node, for any async function
    decorated with @router.* or @pages_router.*."""
    text = _ADMIN_ROUTES_FILE.read_text()
    tree = ast.parse(text)
    out: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec
            if isinstance(dec, ast.Call):
                target = dec.func
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                if target.value.id in ("router", "pages_router"):
                    out[node.name] = node
                    break
    return out


def _handler_body_source(node: ast.AsyncFunctionDef) -> str:
    return ast.unparse(node.body)


def _handler_signature_source(node: ast.AsyncFunctionDef) -> str:
    return ast.unparse(node.args)


_EXPECTED = {
    "list_admin_instances",
    "admin_instances_summary",
    "get_admin_instance_detail",
    "get_admin_instance_logs",
    "get_admin_instance_audit",
    "admin_terminate",
    "admin_bulk_terminate",
    "admin_reprovision",
    "admin_rotate_token",
    "admin_extend_expiry",
    "page_instances",
    "page_instance_detail",
}


def test_handlers_were_discovered():
    handlers = _route_handlers()
    missing = _EXPECTED - set(handlers)
    assert not missing, f"could not discover handlers: {sorted(missing)}"


@pytest.mark.parametrize("handler_name", sorted(_EXPECTED))
def test_every_handler_calls_require_admin(handler_name: str):
    handlers = _route_handlers()
    body = _handler_body_source(handlers[handler_name])
    assert "_require_admin(user)" in body, (
        f"{handler_name}() missing _require_admin(user) call — Principle FR-003 violation"
    )


@pytest.mark.parametrize("handler_name", sorted(_EXPECTED))
def test_every_handler_uses_web_user_dep(handler_name: str):
    """Auth shape: Depends(web_user_dep) — never raw User type."""
    handlers = _route_handlers()
    sig = _handler_signature_source(handlers[handler_name])
    assert "Depends(web_user_dep)" in sig, (
        f"{handler_name}() missing Depends(web_user_dep) — auth not wired"
    )


def test_require_admin_is_imported_from_access():
    """The handler file must use the canonical guard, not a local copy."""
    text = _ADMIN_ROUTES_FILE.read_text()
    assert "from openclow.api.routes.access import _require_admin" in text


def test_no_handler_bypasses_admin_check_with_inline_user_id_match():
    """admin_instances routes are admin-only — no per-user-id fallthrough."""
    text = _ADMIN_ROUTES_FILE.read_text()
    assert "user.is_admin" not in text, (
        "admin_instances.py must call _require_admin(), not check is_admin inline"
    )
    assert "if user.id" not in text, (
        "admin_instances.py routes are admin-only — no per-user-id fallthrough"
    )
