#!/usr/bin/env python
"""TAGH Dev architecture-fitness runner.

Discovers every ``scripts/fitness/check_*.py``, invokes its ``check()``
function, aggregates findings into a Markdown report mapped to
constitution principles. This is the senior-audit entry point — the
older ``audit_*.py`` scripts are folded into individual fitness
functions under ``scripts/fitness/``.

Run modes:

  * ``python scripts/pipeline_fitness.py``         — markdown report
  * ``python scripts/pipeline_fitness.py --json``  — machine-readable
  * ``python scripts/pipeline_fitness.py --check NAME[,NAME...]`` —
    run only the listed checks
  * ``python scripts/pipeline_fitness.py --fail-on critical|high|none``
    — controls exit code policy. Default ``critical``.

Exit codes:
  0  no findings at or above the fail-on threshold
  1  findings exceed the threshold (CI fail)
  2  runner error (a check crashed)

Maps to Constitution: every check declares which principles it
enforces; the report's summary table is grouped by principle so a
reviewer can see at a glance which constitutional invariants hold
and which don't.
"""
from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import sys
import traceback


REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _discover_checks() -> list:
    fitness_dir = REPO / "scripts" / "fitness"
    out = []
    for path in sorted(fitness_dir.glob("check_*.py")):
        mod_name = f"scripts.fitness.{path.stem}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            print(
                f"pipeline_fitness: WARN failed to import {mod_name}: {e}",
                file=sys.stderr,
            )
            continue
        if not hasattr(mod, "check"):
            print(
                f"pipeline_fitness: WARN {mod_name} has no check() function",
                file=sys.stderr,
            )
            continue
        out.append(mod)
    return out


def _run_one(mod):
    from scripts.fitness import FitnessResult, Severity, Finding
    name = getattr(mod, "__name__", "<?>").rsplit(".", 1)[-1]
    try:
        result = mod.check()
    except Exception:
        result = FitnessResult(
            name=name.removeprefix("check_"),
            principles=[],
            description="(crashed)",
            passed=False,
            error=traceback.format_exc(limit=4),
        )
    return result


def _markdown_report(results: list) -> str:
    lines = ["# Pipeline-fitness report", ""]

    # Summary table.
    lines.append("| # | Check | Status | Principles | Critical | High | Medium | Low |")
    lines.append("|---|-------|--------|------------|---------:|-----:|-------:|----:|")
    for i, r in enumerate(results, 1):
        from scripts.fitness import Severity
        crit = r.critical_count
        high = r.high_count
        med = sum(1 for f in r.findings if f.severity == Severity.MEDIUM)
        low = sum(1 for f in r.findings if f.severity == Severity.LOW)
        status = "✅ PASS" if r.passed else ("❌ ERROR" if r.error else "❌ FAIL")
        principles = ", ".join(r.principles) or "—"
        lines.append(
            f"| {i} | `{r.name}` | {status} | {principles} | {crit} | {high} | {med} | {low} |"
        )
    lines.append("")

    # Per-principle rollup.
    by_principle: dict[str, list] = {}
    for r in results:
        for p in r.principles or ["—"]:
            by_principle.setdefault(p, []).append(r)
    lines.append("## Constitution principle coverage")
    lines.append("")
    lines.append("| Principle | Checks running | All passing |")
    lines.append("|-----------|---------------:|:-----------:|")
    for p in sorted(by_principle):
        checks = by_principle[p]
        all_pass = all(r.passed for r in checks)
        lines.append(
            f"| {p} | {len(checks)} | {'✅' if all_pass else '❌'} |"
        )
    lines.append("")

    # Per-check detail.
    for r in results:
        from scripts.fitness import Severity
        lines.append(f"## {r.name}")
        lines.append("")
        lines.append(f"**Principles**: {', '.join(r.principles) or '—'}")
        lines.append(f"**Description**: {r.description}")
        if r.error:
            lines.append("")
            lines.append("**Error**:")
            lines.append("```")
            lines.append(r.error.strip())
            lines.append("```")
            lines.append("")
            continue
        if not r.findings:
            lines.append("")
            lines.append("✅ No findings.")
            lines.append("")
            continue
        lines.append("")
        lines.append("| Severity | Location | Message |")
        lines.append("|----------|----------|---------|")
        for f in r.findings:
            loc = f.location or "—"
            msg = f.message.replace("|", "\\|")
            lines.append(f"| {f.severity.value} | `{loc}` | {msg} |")
        lines.append("")

    return "\n".join(lines)


def _json_report(results: list) -> str:
    out = []
    for r in results:
        out.append({
            "name": r.name,
            "principles": r.principles,
            "description": r.description,
            "passed": r.passed,
            "error": r.error,
            "findings": [
                {"severity": f.severity.value, "message": f.message, "location": f.location}
                for f in r.findings
            ],
        })
    return json.dumps(out, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--check", default=None,
        help="Comma-separated check names to run (default: all).",
    )
    parser.add_argument(
        "--fail-on", default="critical",
        choices=("critical", "high", "none"),
        help="Exit non-zero if any finding at or above this severity exists.",
    )
    args = parser.parse_args()

    checks = _discover_checks()
    if args.check:
        wanted = {c.strip() for c in args.check.split(",")}
        checks = [
            c for c in checks
            if c.__name__.rsplit(".", 1)[-1].removeprefix("check_") in wanted
        ]
        if not checks:
            print(
                f"pipeline_fitness: no checks matched {args.check!r}",
                file=sys.stderr,
            )
            return 2

    results = [_run_one(m) for m in checks]

    if args.json:
        print(_json_report(results))
    else:
        print(_markdown_report(results))

    from scripts.fitness import Severity
    threshold = {
        "critical": Severity.CRITICAL,
        "high": Severity.HIGH,
        "none": None,
    }[args.fail_on]
    if threshold is None:
        return 0

    rank = {
        Severity.CRITICAL: 4, Severity.HIGH: 3,
        Severity.MEDIUM: 2, Severity.LOW: 1, Severity.INFO: 0,
    }
    threshold_rank = rank[threshold]
    for r in results:
        if r.error:
            return 2
        for f in r.findings:
            if rank[f.severity] >= threshold_rank:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
