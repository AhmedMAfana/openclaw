"""T032: adversarial harness — agents bound to inst-A cannot touch inst-B.

Four escape categories, each verified at the MCP layer (where the
guard belongs per Principle III) rather than at a deeper service:

  a. ``workspace_mcp`` refuses paths outside its ``--root``.
  b. ``instance_mcp`` refuses any service not in ``--allowed-services``;
     ``cloudflared`` MUST never be in the allowlist and the module
     refuses to start if it appears.
  c. ``git_mcp`` under ``--branch`` pinning refuses commit/push when
     HEAD has drifted off the pinned branch. (Destructive ops like
     ``git_checkout`` / ``git_reset --hard`` / ``git_branch -D`` are
     simply not exported — they don't exist as tools at all.)
  d. ``providers/llm/claude.py`` factories take an ``Instance`` row
     and bake identity into argv at spawn time — an LLM cannot
     substitute a different slug/path/branch at call time because no
     tool schema accepts such an argument (T033 invariant).

Most assertions run without a live Redis / Postgres / Docker — they
exercise argv-level bindings and tool-registration surfaces. The one
git_mcp test that needs a live repo creates a temporary one.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

pytest.importorskip(
    "taghdev.mcp_servers.instance_mcp",
    reason="T038 instance_mcp not landed yet",
)
pytest.importorskip(
    "taghdev.mcp_servers.workspace_mcp",
    reason="T039 workspace_mcp not landed yet",
)
pytest.importorskip(
    "taghdev.mcp_servers.git_mcp",
    reason="T040 git_mcp extension not landed yet",
)


pytestmark = pytest.mark.asyncio


# --- (a) workspace_mcp path isolation ---------------------------------


async def test_workspace_read_rejects_cross_instance_absolute_path(tmp_path):
    """Escape (a): absolute path into a sibling root is rejected."""
    inst_a = tmp_path / "inst-a"
    inst_b = tmp_path / "inst-b"
    inst_a.mkdir()
    inst_b.mkdir()
    (inst_b / "secret.txt").write_text("do not read me")

    # Re-import workspace_mcp with the --root bound to inst-a.
    # The module reads argv at import time; spawn a subprocess to
    # exercise that exact path.
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; sys.argv = ['workspace_mcp', '--root', %r];"
            "from taghdev.mcp_servers.workspace_mcp import _resolve;"
            "path, err = _resolve(%r);"
            "print('path=', path); print('err=', err)"
            % (str(inst_a), str(inst_b / "secret.txt")),
        ],
        capture_output=True, text=True, timeout=15,
    )
    out = proc.stdout + proc.stderr
    assert "REFUSED" in out or "outside the workspace root" in out, (
        f"cross-root read should be refused. stdout+stderr:\n{out}"
    )


async def test_workspace_read_rejects_symlink_escape(tmp_path):
    """Escape (a'): symlink chase must be resolved before allow-listing."""
    inst_a = tmp_path / "inst-a"
    inst_b = tmp_path / "inst-b"
    inst_a.mkdir()
    inst_b.mkdir()
    (inst_b / "secret.txt").write_text("do not read me")

    # Plant a symlink inside inst-a that points to inst-b's secret.
    (inst_a / "link").symlink_to(inst_b / "secret.txt")

    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; sys.argv = ['workspace_mcp', '--root', %r];"
            "from taghdev.mcp_servers.workspace_mcp import _resolve;"
            "path, err = _resolve('link');"
            "print('err=', err)"
            % (str(inst_a),),
        ],
        capture_output=True, text=True, timeout=15,
    )
    out = proc.stdout + proc.stderr
    assert "REFUSED" in out or "outside the workspace root" in out, (
        f"symlink escape should be refused. stdout+stderr:\n{out}"
    )


# --- (b) instance_mcp service allowlist -------------------------------


async def test_instance_exec_refuses_cloudflared() -> None:
    """Escape (b): `cloudflared` is never in the allowlist."""
    from taghdev.mcp_servers import instance_mcp as m
    result = await m.instance_exec(service="cloudflared", cmd="kill 1")
    assert "REFUSED" in result
    assert "cloudflared" in result.lower() or "allowlist" in result.lower()


async def test_instance_exec_refuses_unlisted_service() -> None:
    """Escape (b'): any service not in the bound allowlist is refused."""
    from taghdev.mcp_servers import instance_mcp as m
    result = await m.instance_exec(service="arbitrary_service", cmd="ls")
    assert "REFUSED" in result


def test_instance_mcp_refuses_startup_if_cloudflared_allowlisted() -> None:
    """Operator cannot enable cloudflared via --allowed-services."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "taghdev.mcp_servers.instance_mcp",
            "--compose-project", "tagh-inst-deadbeef000000",
            "--allowed-services", "app,cloudflared,web",
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 2, (
        f"expected exit 2 when cloudflared in allowlist, got "
        f"{proc.returncode}\nstderr: {proc.stderr[:300]}"
    )
    assert "cloudflared" in proc.stderr


# --- (c) git_mcp branch pinning ---------------------------------------


def _init_git_repo(path: str, *, branch: str = "main") -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", branch, path], check=True,
    )
    subprocess.run(
        ["git", "-C", path, "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", path, "config", "user.name", "t"], check=True,
    )
    open(os.path.join(path, "README.md"), "w").write("#\n")
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", path, "commit", "-q", "-m", "init"], check=True,
    )


async def test_git_commit_refused_when_head_off_pinned_branch(tmp_path):
    """Escape (c): commit refused if HEAD drifted off --branch."""
    repo = str(tmp_path / "repo")
    _init_git_repo(repo, branch="session-A")
    # Create a stray branch and switch onto it — git_mcp pinned to
    # session-A should refuse commit/push.
    subprocess.run(
        ["git", "-C", repo, "checkout", "-q", "-b", "stray"], check=True,
    )

    # Drive the git_mcp module as a subprocess with --branch=session-A.
    # The tool functions are exported; we can call them after setting
    # the module-level _workspace / _pinned_branch via argv parsing.
    code = (
        "import asyncio, sys;"
        "sys.argv = ['git_mcp', '--workspace', %r, '--branch', 'session-A'];"
        "from taghdev.mcp_servers import git_mcp as g;"
        "r = asyncio.run(g.git_commit('test commit'));"
        "print('R:', r)"
    ) % (repo,)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=15,
    )
    out = proc.stdout + proc.stderr
    assert "REFUSED" in out, (
        f"git_commit should be refused off pinned branch. Got:\n{out}"
    )


async def test_git_mcp_pinned_mode_does_not_expose_checkout_or_reset() -> None:
    """Escape (c'): destructive tools are simply not exported.

    The pinned git_mcp module is deliberately narrow. An agent cannot
    call `git_checkout` / `git_reset` / `git_branch -D` because those
    tool names don't exist.
    """
    from taghdev.mcp_servers import git_mcp as g
    manifest = g.get_tool_manifest()
    exposed = {t["name"] for t in manifest}
    # The forbidden destructive ops:
    assert "git_checkout" not in exposed
    assert "git_reset" not in exposed
    assert "git_branch_delete" not in exposed
    assert "git_force_push" not in exposed
    # Sanity: the allowed ones ARE present.
    assert "git_status" in exposed
    assert "git_commit" in exposed
    assert "git_push" in exposed


# --- (d) factory-level bindings (defence in depth) --------------------


def test_factories_bake_identity_into_argv() -> None:
    """Escape (d): claude.py factories take an Instance row, not a string.

    The argv returned by each factory MUST include the specific
    slug / root / branch drawn from the Instance — NOT a placeholder
    or a template variable. This prevents an LLM from substituting a
    different identifier at call time (because the identifier is
    already baked before the subprocess spawns).
    """
    from taghdev.providers.llm.claude import (
        _mcp_instance, _mcp_workspace, _mcp_git_pinned,
    )

    class _FakeInstance:
        slug = "inst-aaaabbbbccccdd"
        compose_project = "tagh-inst-aaaabbbbccccdd"
        workspace_path = "/workspaces/inst-aaaabbbbccccdd/"
        session_branch = "chat-42-session"

    inst = _FakeInstance()
    inst_cfg = _mcp_instance(inst)
    ws_cfg = _mcp_workspace(inst)
    git_cfg = _mcp_git_pinned(inst)

    joined_inst = " ".join(inst_cfg["args"])
    joined_ws = " ".join(ws_cfg["args"])
    joined_git = " ".join(git_cfg["args"])

    # Identity appears in argv (baked at spawn time).
    assert "tagh-inst-aaaabbbbccccdd" in joined_inst
    assert "/workspaces/inst-aaaabbbbccccdd/" in joined_ws
    assert "chat-42-session" in joined_git

    # cloudflared never ends up in the instance_mcp allowlist.
    assert "cloudflared" not in joined_inst
