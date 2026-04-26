"""Fitness function: per-instance compose templates publish no host ports.

Constitution Principle V (Egress-Only Network Surface): "Instance
containers publish no host ports. The instance is reachable from the
internet ONLY through its `cloudflared` sidecar." Host port
publishing would create cross-tenant blast radius.

Walks every per-instance compose template under
``src/openclow/setup/compose_templates/*/compose.yml`` and asserts no
service other than ``cloudflared`` carries a ``ports:`` key.

Reuses the line-scan from ``instance_compose_renderer.assert_no_host_ports``
so this check and the renderer's runtime guard cannot drift.
"""
from __future__ import annotations

import pathlib

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "src" / "openclow" / "setup" / "compose_templates"


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="compose_no_host_ports",
        principles=["V"],
        description="Per-instance compose templates expose no host ports outside cloudflared.",
        passed=True,
    )

    if not TEMPLATES.is_dir():
        result.error = f"missing {TEMPLATES}"
        result.passed = False
        return result

    # Inline the no-host-ports check so this fitness function doesn't
    # depend on the openclow package being importable (the runner
    # may run from outside the venv on a fresh dev box).
    for compose_path in sorted(TEMPLATES.glob("*/compose.yml")):
        violations = _scan_compose_for_host_ports(compose_path.read_text(encoding="utf-8"))
        for service, lineno in violations:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                message=(
                    f"service {service!r} declares `ports:` — Principle V "
                    f"forbids host port publishing on any service except "
                    f"`cloudflared`."
                ),
                location=f"{compose_path.relative_to(REPO)}:{lineno}",
            ))

    result.findings = findings
    result.passed = result.critical_count == 0
    return result


_SIDECAR = "cloudflared"


def _scan_compose_for_host_ports(text: str) -> list[tuple[str, int]]:
    """Inline reimplementation of instance_compose_renderer.assert_no_host_ports.

    Returns a list of (service_name, lineno) for any service other than
    cloudflared that declares a ``ports:`` block.
    """
    out: list[tuple[str, int]] = []
    in_services = False
    services_indent: int | None = None
    current_service: str | None = None
    service_indent: int | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        line = stripped.rstrip()
        if not line or line.startswith("#"):
            continue

        if line == "services:" and indent == 0:
            in_services = True
            services_indent = None
            continue
        if not in_services:
            continue
        if indent == 0:
            in_services = False
            current_service = None
            continue
        if services_indent is None:
            services_indent = indent
        if indent == services_indent:
            import re as _re
            m = _re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*$", line)
            if m:
                current_service = m.group(1)
                service_indent = None
            continue
        if current_service is None:
            continue
        if service_indent is None:
            service_indent = indent
        if indent == service_indent and line.rstrip(":").strip() == "ports":
            if current_service != _SIDECAR:
                out.append((current_service, lineno))
    return out
