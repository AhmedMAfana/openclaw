"""T062: instance_mcp refuses any operation targeting ``cloudflared``.

Principle III guarantee: the compose-sidecar that holds the CF tunnel
token is NEVER addressable by an agent. ``instance_mcp`` enforces via
its ``--allowed-services`` allowlist, which ``_mcp_instance`` always
sets to ``app,web,node,db,redis`` — cloudflared excluded.

Two assertions:

1. Call ``instance_mcp.instance_exec(service="cloudflared", cmd="kill 1")``
   directly — the tool returns a "service not in allowlist" error
   string instead of executing.
2. Verify instance_mcp.py's startup guard: instantiate the FastMCP
   server with ``--allowed-services app,cloudflared,web`` in argv and
   assert it refuses to start (exits non-zero) — operators cannot
   accidentally enable cloudflared via config.

Runs without real Docker because the allowlist check happens BEFORE
the subprocess is ever spawned.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

_instance_mcp = pytest.importorskip(
    "taghdev.mcp_servers.instance_mcp",
    reason="T038 instance_mcp not landed",
)


@pytest.mark.asyncio
async def test_instance_exec_refuses_cloudflared() -> None:
    """Calling instance_exec with service='cloudflared' returns a REFUSED."""
    # We invoke the tool function directly rather than round-tripping
    # via a subprocess — the refusal check is pure Python.
    from taghdev.mcp_servers import instance_mcp as m

    result = await m.instance_exec(service="cloudflared", cmd="kill 1")
    assert "REFUSED" in result
    assert "cloudflared" in result
    assert "allowlist" in result


def test_instance_mcp_startup_refuses_cloudflared_in_allowlist(tmp_path) -> None:
    """Running the module with cloudflared in --allowed-services fails fast."""
    proc = asyncio.get_event_loop().run_until_complete(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m", "taghdev.mcp_servers.instance_mcp",
            "--compose-project", "tagh-inst-deadbeefdead00",
            "--allowed-services", "app,cloudflared,web",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )
    _, stderr = asyncio.get_event_loop().run_until_complete(
        asyncio.wait_for(proc.communicate(), timeout=10)
    )
    assert proc.returncode == 2, (
        f"expected exit 2 when cloudflared in allowlist, got "
        f"{proc.returncode} — stderr: {stderr.decode(errors='replace')[:200]}"
    )
    assert b"cloudflared" in (stderr or b"")
