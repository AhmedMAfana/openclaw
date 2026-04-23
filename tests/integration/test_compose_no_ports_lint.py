"""T016: enforce Principle V across all compose templates.

For every template under src/openclow/setup/compose_templates/, render a
sample compose file and assert no service other than `cloudflared` carries
a `ports:` key. Fails the CI build on violation.

NOTE: when a template has no compose.yml yet (early-stage scaffolds with
just .gitkeep), the test skips that template. Once T056/T057 land the
Laravel+Vue template files, the test naturally starts exercising them.
"""
import pathlib

import pytest

from openclow.services.instance_compose_renderer import (
    ComposeRenderError,
    InstanceRenderContext,
    assert_no_host_ports,
    render,
)

TEMPLATES_ROOT = pathlib.Path(__file__).resolve().parents[2] / (
    "src/openclow/setup/compose_templates"
)


def _sample_ctx(tmp_workspace: pathlib.Path) -> InstanceRenderContext:
    """A realistic but synthetic render context. Matches FR-018a slug format."""
    return InstanceRenderContext(
        slug="inst-0123456789abcd",
        workspace_path=str(tmp_workspace),
        compose_project="tagh-inst-0123456789abcd",
        web_hostname="inst-0123456789abcd.dev.example.com",
        hmr_hostname="hmr-inst-0123456789abcd.dev.example.com",
        ide_hostname=None,
        cf_tunnel_id="11111111-2222-3333-4444-555555555555",
        cf_credentials_secret="tagh-inst-0123456789abcd-cf",
        db_password="x" * 16,  # renderer doesn't embed this
        heartbeat_secret="y" * 44,  # renderer doesn't embed this
    )


def _iter_templates() -> list[pathlib.Path]:
    """Every template dir that has a compose.yml."""
    if not TEMPLATES_ROOT.is_dir():
        return []
    return sorted(
        p.parent
        for p in TEMPLATES_ROOT.glob("*/compose.yml")
        if p.is_file()
    )


@pytest.mark.parametrize(
    "template_dir",
    _iter_templates() or [pytest.param(None, marks=pytest.mark.skip(
        reason="no compose templates present yet; enable once T056/T057 land"
    ))],
    ids=lambda p: p.name if p else "no-templates",
)
def test_rendered_compose_has_no_host_ports(
    template_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    ctx = _sample_ctx(tmp_path / "workspace")
    compose_path, _ = render(ctx, template_dir, tmp_path / "out")
    rendered = compose_path.read_text()
    # Raises on violation — pytest catches and reports with full context.
    assert_no_host_ports(rendered)


def test_assert_no_host_ports_rejects_published_app_port() -> None:
    """Sanity check: the lint helper actually catches violations.

    Without this we could ship a broken linter that silently passes
    everything. Inline mini-compose simulating a bad template.
    """
    bad = (
        "version: '3'\n"
        "services:\n"
        "  app:\n"
        "    image: php:8.3-fpm\n"
        "    ports:\n"
        "      - '9000:9000'\n"
        "  cloudflared:\n"
        "    image: cloudflare/cloudflared\n"
        "    ports:\n"
        "      - '2000:2000'\n"
    )
    with pytest.raises(ComposeRenderError, match=r"app.*ports"):
        assert_no_host_ports(bad)


def test_assert_no_host_ports_allows_cloudflared_only() -> None:
    """Only cloudflared may carry `ports:` (internal metrics port)."""
    ok = (
        "version: '3'\n"
        "services:\n"
        "  app:\n"
        "    image: php:8.3-fpm\n"
        "  cloudflared:\n"
        "    image: cloudflare/cloudflared\n"
        "    ports:\n"
        "      - '2000:2000'\n"
    )
    # Should not raise.
    assert_no_host_ports(ok)
