"""Render a per-instance docker-compose.yml + cloudflared.yml from a template.

Spec: specs/001-per-chat-instances/plan.md §Structure; FR-017 / FR-018.
Constitution V: compose-lint test reads every rendered compose and asserts
no service other than `cloudflared` exposes host ports.

Inputs:
  * An `Instance` row (provides slug, workspace_path, hostnames via tunnel row)
  * Project template directory (contains compose.yml, cloudflared.yml,
    guide.md, project.yaml)
  * Pre-computed injected values: hostnames, db_password, heartbeat_secret,
    github_token

Outputs (written to /workspaces/inst-<slug>/):
  * _compose.yml       — docker compose -f target
  * _cloudflared.yml   — sidecar config mounted into cloudflared

The renderer is deterministic: same inputs → same bytes. No template logic
lives in LLM land; everything is structured substitution.

NOTE: This renderer does NOT apply compose up / compose down — that is the
instance_tasks worker's job. This module only produces the files.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Mapping


FORBIDDEN_ENV_KEY = re.compile(r"SECRET|TOKEN|PASSWORD|KEY|AUTH", re.IGNORECASE)

# The only service allowed to carry a `ports:` key in a rendered per-instance
# compose file. Even `cloudflared` should only expose its internal metrics
# port and ONLY on the compose network, not the host — but the compose-lint
# test treats any `ports:` on other services as a hard failure.
SIDE_CAR_SERVICE = "cloudflared"


@dataclasses.dataclass(frozen=True)
class InstanceRenderContext:
    """Everything the renderer needs about one instance.

    Kept as a frozen dataclass (not directly the SQLAlchemy model) so the
    renderer can be tested without a DB session.
    """

    slug: str
    workspace_path: str
    compose_project: str
    web_hostname: str
    hmr_hostname: str
    ide_hostname: str | None
    cf_tunnel_id: str
    cf_credentials_secret: str
    db_password: str
    heartbeat_secret: str
    # The GitHub installation token is injected at compose-up time, not
    # baked into the rendered file (Principle IV). We only reserve the
    # env-var name here.
    github_token_env: str = "GITHUB_TOKEN"


class ComposeRenderError(Exception):
    """Raised when the template or inputs violate v1 constraints."""


def render(
    ctx: InstanceRenderContext,
    template_dir: pathlib.Path,
    output_dir: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Render a template into the instance's output dir.

    Returns (compose_path, cloudflared_path). Raises ComposeRenderError on
    any v1-constraint violation (secret in env, missing required file).
    """
    template_dir = pathlib.Path(template_dir)
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    compose_tmpl = template_dir / "compose.yml"
    cloudflared_tmpl = template_dir / "cloudflared.yml"
    project_yaml = template_dir / "project.yaml"

    for required in (compose_tmpl, cloudflared_tmpl, project_yaml):
        if not required.is_file():
            raise ComposeRenderError(f"template missing required file: {required}")

    # Load project.yaml env for forbidden-key validation. We don't actually
    # need to parse it fully at render time — a shallow regex scan is enough
    # and it keeps us free of a YAML dependency here.
    _reject_secret_env_in_project_yaml(project_yaml.read_text())

    # Injected env — every key here is either a non-secret (host, slug) or a
    # per-instance secret that the compose file will reference via ${VAR}.
    # The actual secret values are passed to `docker compose up` via env,
    # NOT written to the rendered file.
    env: Mapping[str, str] = {
        "INSTANCE_SLUG": ctx.slug,
        "INSTANCE_HOST": ctx.web_hostname,
        "INSTANCE_HMR_HOST": ctx.hmr_hostname,
        "INSTANCE_IDE_HOST": ctx.ide_hostname or "",
        "COMPOSE_PROJECT": ctx.compose_project,
        "CF_TUNNEL_ID": ctx.cf_tunnel_id,
        "CF_CREDENTIALS_SECRET": ctx.cf_credentials_secret,
    }

    compose_out = output_dir / "_compose.yml"
    cloudflared_out = output_dir / "_cloudflared.yml"

    compose_out.write_text(_substitute(compose_tmpl.read_text(), env))
    cloudflared_out.write_text(_substitute(cloudflared_tmpl.read_text(), env))
    return compose_out, cloudflared_out


def _substitute(text: str, env: Mapping[str, str]) -> str:
    """Replace ${VAR} with values from env. Missing keys raise."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in env:
            # KeyError in a regex sub is ugly; wrap for clarity.
            raise ComposeRenderError(
                f"template references ${{{key}}} but no value was provided"
            )
        return env[key]

    return re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", repl, text)


def _reject_secret_env_in_project_yaml(project_yaml: str) -> None:
    """Fail render if project.yaml's `env:` block names a secret-shaped key.

    GUIDE_SPEC.md §1 forbids secrets in project.yaml env — secrets come in
    via orchestrator env-injection, not template embedding.
    """
    # Minimal-line-range scan: find `env:` heading, then enumerate its
    # indented children until an un-indented line.
    in_env = False
    env_indent = None
    for line in project_yaml.splitlines():
        stripped = line.rstrip()
        if not in_env:
            if stripped == "env:":
                in_env = True
                env_indent = None
            continue
        # First non-blank indented line defines env block indent.
        if stripped == "" or stripped.startswith("#"):
            continue
        leading = len(line) - len(line.lstrip(" "))
        if env_indent is None:
            env_indent = leading
        if leading < env_indent:
            in_env = False
            continue
        # Expect `KEY: value` or `KEY:` (empty → ignored).
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
        if not m:
            continue
        key = m.group(1)
        if FORBIDDEN_ENV_KEY.search(key):
            raise ComposeRenderError(
                f"project.yaml env key {key!r} looks secret-shaped; "
                "secrets must come from orchestrator env injection, "
                "not template embedding (GUIDE_SPEC.md §1)."
            )


def assert_no_host_ports(rendered_compose: str) -> None:
    """Raise if any non-cloudflared service in the rendered compose has a `ports:` key.

    Used by tests/integration/test_compose_no_ports_lint.py (Principle V enforcement).
    Kept as a library function so any caller can verify — not just the test.
    """
    # Parse the rendered YAML into lines and find service blocks. We avoid
    # a YAML dep: the compose structure is well-known and our templates are
    # under our control, so a line-based scan is sufficient + auditable.
    lines = rendered_compose.splitlines()
    current_service: str | None = None
    service_indent: int | None = None
    in_services = False
    services_indent = None

    for lineno, raw in enumerate(lines, start=1):
        stripped_left = raw.lstrip(" ")
        indent = len(raw) - len(stripped_left)
        stripped = stripped_left.rstrip()

        if stripped == "" or stripped.startswith("#"):
            continue

        # Top-level `services:` heading.
        if stripped == "services:" and indent == 0:
            in_services = True
            services_indent = None
            continue

        if not in_services:
            continue

        # Any other top-level key closes the services block.
        if indent == 0:
            in_services = False
            current_service = None
            continue

        # First child of services: defines per-service indent.
        if services_indent is None:
            services_indent = indent

        if indent == services_indent:
            # `<service>:` heading.
            m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*$", stripped)
            if not m:
                continue
            current_service = m.group(1)
            service_indent = None
            continue

        if current_service is None:
            continue

        if service_indent is None:
            service_indent = indent

        # `ports:` line inside a service block.
        if indent == service_indent and stripped.rstrip(":").strip() == "ports":
            if current_service != SIDE_CAR_SERVICE:
                raise ComposeRenderError(
                    f"service {current_service!r} has `ports:` on line {lineno} — "
                    "Principle V forbids host port publishing on any service "
                    f"other than {SIDE_CAR_SERVICE!r}."
                )


def to_json(ctx: InstanceRenderContext) -> str:
    """Serialise the render context for debugging / audit log."""
    return json.dumps(dataclasses.asdict(ctx), sort_keys=True)
