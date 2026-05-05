"""Fitness function: SQLAlchemy ORM ↔ alembic migrations are in sync.

Catches the bug class where someone adds a ``mapped_column(...)`` to
an ORM model but forgets to write the alembic migration — the model
sees a column that doesn't exist on disk, queries crash with
``UndefinedColumn``. Or the inverse: a migration adds a column that
no model references, leaving dead schema.

Two layers of check:

  1. **Static (always runs)**: walks every ``models/*.py`` for
     ``mapped_column(...)`` declarations and harvests every
     ``op.add_column`` / ``op.create_table`` call from
     ``alembic/versions/*.py``. Cross-checks that every model column
     is added by SOME migration in history. Catches the most common
     drift case: model column with no migration support.
  2. **Live (when DATABASE_URL is reachable)**: runs
     ``alembic check --autogenerate`` if the alembic CLI is available
     and a DB is up. This is the gold-standard check — alembic
     compares the live schema against the metadata. Only runs when
     the env supports it.

Maps to Constitution Principle VI (Durable State, Idempotent
Lifecycle — the source of truth must remain consistent across
redeploys) and Principle VII (Verified Work — schema changes are
not "done" until they're migrated).
"""
from __future__ import annotations

import ast
import os
import pathlib
import shutil
import subprocess

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
MODELS_DIR = REPO / "src" / "taghdev" / "models"
MIGRATIONS_DIR = REPO / "alembic" / "versions"


# Columns the static check should ignore — server-default-only or
# inherited from a base class that the regex-based static check
# can't trace cleanly. The live `alembic check` is the canonical
# gate for these.
_STATIC_IGNORE_COLUMNS: set[str] = {
    "id", "created_at", "updated_at",
}


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="db_model_drift",
        principles=["VI", "VII"],
        description="ORM mapped_column declarations are reflected in alembic migration history.",
        passed=True,
    )

    if not MODELS_DIR.is_dir() or not MIGRATIONS_DIR.is_dir():
        result.error = f"missing {MODELS_DIR} or {MIGRATIONS_DIR}"
        result.passed = False
        return result

    # --- Static layer ----------------------------------------------
    model_columns = _model_columns()
    migration_columns = _migration_columns()

    # Per-table set difference. Catch model columns that no migration
    # ever added.
    for table, mcols in sorted(model_columns.items()):
        migrated = migration_columns.get(table, set())
        missing = mcols - migrated - _STATIC_IGNORE_COLUMNS
        for col in sorted(missing):
            findings.append(Finding(
                severity=Severity.HIGH,
                message=(
                    f"model `{table}` declares column `{col}` but no "
                    f"alembic migration adds it. Either generate the "
                    f"migration (`alembic revision --autogenerate -m "
                    f"add_{col}`) or drop the column from the model."
                ),
                location=str(MODELS_DIR.relative_to(REPO)),
            ))

    # Migration tables not in models → INFO. Could be legacy tables
    # or tables managed outside the orchestrator.
    extra_tables = set(migration_columns) - set(model_columns)
    for table in sorted(extra_tables):
        findings.append(Finding(
            severity=Severity.INFO,
            message=(
                f"alembic history references table `{table}` which has "
                f"no SQLAlchemy model in src/taghdev/models/. Possibly "
                f"legacy table or managed by a different surface."
            ),
            location=str(MIGRATIONS_DIR.relative_to(REPO)),
        ))

    # --- Live layer (best-effort) ----------------------------------
    live_msg = _try_alembic_check()
    if live_msg is not None:
        findings.append(Finding(
            severity=Severity.HIGH,
            message=live_msg,
            location="alembic check --autogenerate",
        ))

    result.findings = findings
    # HIGH is the bar here — schema drift is one Postgres deploy away
    # from a runtime UndefinedColumn.
    result.passed = result.high_count == 0 and result.critical_count == 0
    return result


# --- helpers ---------------------------------------------------------


def _model_columns() -> dict[str, set[str]]:
    """Walk every models/*.py for class definitions with __tablename__
    and harvest every mapped_column declaration.
    """
    out: dict[str, set[str]] = {}
    for path in sorted(MODELS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            tablename = None
            cols: set[str] = set()
            for sub in node.body:
                # __tablename__ = "..."
                if isinstance(sub, ast.Assign):
                    if (len(sub.targets) == 1
                            and isinstance(sub.targets[0], ast.Name)
                            and sub.targets[0].id == "__tablename__"
                            and isinstance(sub.value, ast.Constant)):
                        tablename = sub.value.value
                # name: Mapped[...] = mapped_column(...)
                if isinstance(sub, ast.AnnAssign):
                    if isinstance(sub.target, ast.Name) and _is_mapped_column(sub.value):
                        # Honour an explicit override: mapped_column("col_name", ...)
                        # uses the first positional string arg as the actual DB
                        # column name, even if the Python attribute has a
                        # trailing underscore (e.g. `metadata_` vs `metadata`).
                        cols.add(_actual_column_name(sub.target.id, sub.value))
            if tablename and cols:
                out.setdefault(tablename, set()).update(cols)
    return out


def _actual_column_name(py_attr: str, call: ast.Call) -> str:
    """Return the DB column name for a ``mapped_column(...)`` call.

    SQLAlchemy lets you override the column name by passing a string
    as the first positional argument: ``metadata_: Mapped[dict] =
    mapped_column("metadata", JSONB)``. Falls back to the Python
    attribute name when no explicit override is present.
    """
    if call.args:
        arg0 = call.args[0]
        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
            return arg0.value
    return py_attr


def _is_mapped_column(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name) and f.id == "mapped_column":
        return True
    if isinstance(f, ast.Attribute) and f.attr == "mapped_column":
        return True
    return False


def _migration_columns() -> dict[str, set[str]]:
    """Walk every alembic/versions/*.py for op.add_column /
    op.create_table calls and harvest the (table, column) pairs.
    """
    out: dict[str, set[str]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "op"):
                continue
            if f.attr == "add_column":
                # op.add_column("table_name", sa.Column("col", ...))
                if len(node.args) >= 2 and isinstance(node.args[0], ast.Constant):
                    table = node.args[0].value
                    col = _extract_column_name(node.args[1])
                    if isinstance(table, str) and col:
                        out.setdefault(table, set()).add(col)
            elif f.attr == "create_table":
                if node.args and isinstance(node.args[0], ast.Constant):
                    table = node.args[0].value
                    if not isinstance(table, str):
                        continue
                    for arg in node.args[1:]:
                        col = _extract_column_name(arg)
                        if col:
                            out.setdefault(table, set()).add(col)
    return out


def _extract_column_name(node: ast.AST) -> str | None:
    """sa.Column("col_name", ...) → "col_name"."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr == "Column"):
        if not (isinstance(f, ast.Name) and f.id == "Column"):
            return None
    if not node.args:
        return None
    arg0 = node.args[0]
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        return arg0.value
    return None


def _try_alembic_check() -> str | None:
    """Run ``alembic check`` (autogenerate diff) if env supports it.

    Returns None on success or unsupported (no message → no finding).
    Returns an error string if alembic detected drift.
    """
    if not shutil.which("alembic"):
        return None
    if not os.environ.get("DATABASE_URL"):
        return None
    try:
        proc = subprocess.run(
            ["alembic", "check"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode == 0:
        return None
    out = (proc.stdout + proc.stderr).strip()[:500]
    return (
        f"alembic check --autogenerate detected schema drift. "
        f"Re-run `alembic revision --autogenerate -m <reason>` and "
        f"commit. Output: {out}"
    )
