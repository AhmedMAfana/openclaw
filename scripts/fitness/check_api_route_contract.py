"""Fitness function: frontend ``fetch('/api/X')`` URLs ↔ FastAPI routes.

Catches the bug class where the frontend calls an endpoint that
doesn't exist on the server (typo, rename, dropped router), or the
backend ships a route nobody calls (likely dead code).

Walks:

  * Every ``src/openclow/api/routes/*.py`` for
    ``@router.<verb>("/path")`` decorators. Combines with the
    ``router = APIRouter(prefix="/api", ...)`` declaration to produce
    canonical full paths.
  * Every ``chat_frontend/src/**/*.{ts,tsx}`` for ``fetch('...')`` /
    ``fetch(\`...\`)`` calls whose URL starts with ``/api/``.
  * Diffs the two sets, normalising path parameters (``/api/users/42``
    ↔ ``/api/users/{user_id}``).

Maps to Constitution Principle VII (Verified Work — every call must
resolve to a real registration).
"""
from __future__ import annotations

import ast
import pathlib
import re

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
ROUTES_DIR = REPO / "src" / "openclow" / "api" / "routes"
PAGES_FILE = REPO / "src" / "openclow" / "api" / "pages.py"
MAIN_FILE = REPO / "src" / "openclow" / "api" / "main.py"
FRONTEND_DIR = REPO / "chat_frontend" / "src"


# Frontend URLs that legitimately have no FastAPI route — e.g. routes
# served by upstream nginx (login pages), or paths handled by the
# pages.py HTML router that lives outside routes/.
_FRONTEND_ONLY_OK: set[str] = {
    "/api/auth/login",   # if served by middleware, not a router
}

# Backend routes that legitimately have no frontend caller — e.g.
# called only by external clients (projctl) or by other backend code.
_BACKEND_ONLY_OK: set[str] = {
    # T050 / T064 / T079 — projctl calls these from inside the
    # instance container; no browser-side caller.
    "/internal/instances/{slug}/heartbeat",
    "/internal/instances/{slug}/rotate-git-token",
    "/internal/instances/{slug}/explain",
    # Health probe — used by Docker / Kubernetes / Prometheus, not
    # the chat frontend.
    "/health",
}


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="api_route_contract",
        principles=["VII"],
        description="Frontend fetch URLs match the FastAPI route surface.",
        passed=True,
    )

    if not ROUTES_DIR.is_dir():
        result.error = f"missing {ROUTES_DIR}"
        result.passed = False
        return result

    backend_routes = _backend_routes()
    frontend_calls = _frontend_calls()

    # Compile each backend route into a regex that matches concrete
    # frontend URLs. ``/api/users/{user_id}/instances`` becomes the
    # regex ``^/api/users/[^/]+/instances$``. This handles the case
    # where the frontend interpolates a variable (numeric ID, string
    # key, slug) into a path-parameter slot.
    backend_regexes = [(p, _route_to_regex(p)) for p in backend_routes]

    fe_only = set(_FRONTEND_ONLY_OK)
    matched_backend: set[str] = set()
    for fe_url in sorted(frontend_calls):
        if fe_url in fe_only:
            continue
        match_route = _first_matching_route(fe_url, backend_regexes)
        if match_route is None:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                message=(
                    f"frontend calls `{fe_url}` but no FastAPI route "
                    "serves it — user request will 404. Either add "
                    "the route under src/openclow/api/routes/ or "
                    "whitelist in _FRONTEND_ONLY_OK with a one-line "
                    "reason."
                ),
                location="chat_frontend/src/",
            ))
        else:
            matched_backend.add(match_route)

    # Dead-code-class: backend ships a route nobody calls.
    be_only = set(_BACKEND_ONLY_OK)
    be_not_fe = set(backend_routes) - matched_backend - be_only
    for p in sorted(be_not_fe):
        findings.append(Finding(
            severity=Severity.LOW,
            message=(
                f"FastAPI route `{p}` has no frontend caller in "
                "chat_frontend/src/ — possibly dead code, called by "
                "external clients (projctl, dashboards), or wired via "
                "another consumer. Add to _BACKEND_ONLY_OK with a "
                "reason if intentional."
            ),
            location="src/openclow/api/routes/",
        ))

    result.findings = findings
    # Only CRITICAL fails the suite — frontend-only and backend-only
    # gaps are usually intentional or self-healing.
    result.passed = result.critical_count == 0
    return result


# --- helpers ---------------------------------------------------------


def _backend_routes() -> set[str]:
    """Collect every full route path served by the API.

    Combines THREE prefix sources to produce a canonical full path:

      1. ``main.py``: ``app.include_router(<mod>.router, prefix="...")``
         adds an outer prefix (commonly ``/api``).
      2. The routes/*.py module's own ``APIRouter(prefix="...")``.
      3. The decorator's path literal.

    Concatenation order: (1) + (2) + (3). Without step (1) the audit
    misses the ``/api`` prefix that nearly every route is mounted
    behind, producing 100% false positives — which is exactly the
    failure mode of the first revision of this script.
    """
    out: set[str] = set()
    outer_prefix_by_module = _outer_prefixes()
    for path in sorted(ROUTES_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        inner_prefix = _router_prefix(tree)
        outer_prefix = outer_prefix_by_module.get(path.stem, "")
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) and not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                p = _route_from_decorator(dec)
                if p is not None:
                    out.add(outer_prefix + inner_prefix + p)
    return out


def _outer_prefixes() -> dict[str, str]:
    """Walk main.py for ``app.include_router(<mod>.router, prefix="...")``.

    Returns ``{module_basename: outer_prefix}`` so each routes/*.py
    file can have its own outer prefix applied. Modules that aren't
    explicitly mounted with a prefix get ``""`` (so they appear in
    the cross-check at their own router-level prefix).
    """
    out: dict[str, str] = {}
    if not MAIN_FILE.is_file():
        return out
    try:
        tree = ast.parse(MAIN_FILE.read_text(encoding="utf-8"))
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "include_router"):
            continue
        if not node.args:
            continue
        arg0 = node.args[0]
        # arg0 is typically `<module>.router` or `<name>_router`
        mod_name = None
        if isinstance(arg0, ast.Attribute) and isinstance(arg0.value, ast.Name):
            mod_name = arg0.value.id
        elif isinstance(arg0, ast.Name):
            # `chat_router` → strip `_router`
            mod_name = arg0.id.removesuffix("_router")
        if not mod_name:
            continue
        prefix = ""
        for kw in node.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = str(kw.value.value)
        # Only record if non-empty (otherwise the routes file's own prefix
        # is the canonical answer).
        if prefix:
            out[mod_name] = prefix
    return out


def _router_prefix(tree: ast.Module) -> str:
    """Find ``router = APIRouter(prefix="/api", ...)`` and return prefix."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        # Match `router = APIRouter(...)` or `internal_router = APIRouter(...)`
        if not (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "APIRouter"
        ):
            continue
        for kw in node.value.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
    return ""


def _route_from_decorator(dec: ast.AST) -> str | None:
    """Extract the path literal from ``@router.<verb>("/path")``."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return None
    if func.value.id not in {"router", "internal_router", "chat_router", "pages_router"}:
        return None
    if func.attr not in {"get", "post", "put", "delete", "patch", "websocket"}:
        return None
    if not dec.args:
        return None
    arg0 = dec.args[0]
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        return arg0.value
    return None


_FETCH_RE = re.compile(
    r"""fetch\(\s*[`'"](/api/[^'"`)]+)[`'"]""",
)


def _frontend_calls() -> set[str]:
    """Walk chat_frontend/src for fetch('/api/...') calls."""
    out: set[str] = set()
    for path in FRONTEND_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".tsx"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in _FETCH_RE.finditer(text):
            url = m.group(1)
            # Strip query string if present
            url = url.split("?", 1)[0]
            out.add(url)
    return out


def _route_to_regex(template: str) -> re.Pattern[str]:
    """Compile a FastAPI path template into a strict-match regex.

    ``/api/users/{user_id}/instances`` →
        ``re.compile(r"^/api/users/[^/]+/instances$")``

    Path parameters match exactly one URL segment (no slashes), which
    matches FastAPI's default behaviour. The trailing slash is
    optional so the regex tolerates either form.
    """
    template = template.rstrip("/")
    # Escape literal slashes / dots / etc., but unescape our path-param
    # placeholders.
    placeholder_re = re.compile(r"\{[^}]+\}")
    parts = []
    last = 0
    for m in placeholder_re.finditer(template):
        parts.append(re.escape(template[last:m.start()]))
        parts.append(r"[^/]+")
        last = m.end()
    parts.append(re.escape(template[last:]))
    return re.compile(r"^" + "".join(parts) + r"/?$")


def _first_matching_route(url: str, routes: list[tuple[str, re.Pattern[str]]]) -> str | None:
    """Return the first backend route template whose regex matches `url`.

    Frontend URLs are themselves sometimes templated (the regex parser
    in ``_frontend_calls`` extracts e.g. ``/api/threads/${id}/messages``
    where ``${id}`` is a JS template-string interpolation). Treat
    ``${...}`` and ``{...}`` in the frontend URL as wildcards by
    substituting a representative single segment before matching.
    """
    candidate = re.sub(r"\$\{[^}]+\}", "X", url)
    candidate = re.sub(r"\{[^}]+\}", "X", candidate).rstrip("/")
    for template, rx in routes:
        if rx.match(candidate):
            return template
    return None
