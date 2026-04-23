"""T032: adversarial harness — agents bound to inst-A cannot touch inst-B.

Spawns the per-task MCP fleet (``instance_mcp``, ``workspace_mcp``,
``git_mcp``) bound to ``inst-A``, then attempts the known escape paths:

  a. Read ``/workspaces/inst-B/...`` via ``workspace_mcp.read_file``
  b. Invoke ``instance_exec('cloudflared', ...)`` — cloudflared is NEVER
     in the allowed-services list (FR-031 / Principle V + III)
  c. ``git_checkout other-branch`` via ``git_mcp`` — must be refused if
     it would move HEAD off the bound branch
  d. Push to a different repo URL via ``git_mcp.git_push``

Each must FAIL at the MCP layer with a clear rejection, NOT at a deeper
service. That is Principle III in practice: ambient authority cannot be
requested, it is not even exposed.

Requires T038 (instance_mcp), T039 (workspace_mcp), and T040 (git_mcp
extension). Until they land the module skips cleanly.
"""
from __future__ import annotations

import pytest

pytest.importorskip(
    "openclow.mcp_servers.instance_mcp",
    reason="T038 instance_mcp not landed yet",
)
pytest.importorskip(
    "openclow.mcp_servers.workspace_mcp",
    reason="T039 workspace_mcp not landed yet",
)
# git_mcp exists already (as noted in plan.md); T040 EXTENDS it with
# --workspace + --branch binding. Test the extended surface.
pytest.importorskip(
    "openclow.mcp_servers.git_mcp",
    reason="T040 git_mcp extension not landed yet",
)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


@pytest.fixture
async def fleet_inst_a_with_inst_b_present(tmp_path):
    """Two workspaces on disk + fleet bound to inst-A only.

    Layout:
      tmp_path/inst-a/   (bound to fleet)
      tmp_path/inst-b/   (separate — the attacker tries to reach this)
    """
    pytest.skip("fleet fixture requires T038 + T039 + T040 runtime wiring")


async def test_workspace_read_rejects_cross_instance_paths(
    fleet_inst_a_with_inst_b_present,
):
    """Escape (a): absolute path into inst-B's workspace is rejected."""
    pytest.skip("depends on T039 workspace_mcp realpath guard")


async def test_workspace_read_rejects_symlink_escape(
    fleet_inst_a_with_inst_b_present,
):
    """Escape (a'): symlink chase must be resolved before allow-listing."""
    pytest.skip("depends on T039 workspace_mcp os.path.realpath gate")


async def test_instance_exec_refuses_cloudflared(
    fleet_inst_a_with_inst_b_present,
):
    """Escape (b): `cloudflared` is NEVER in the allowed-services list."""
    pytest.skip("depends on T038 instance_mcp allowed-services allowlist")


async def test_instance_exec_refuses_foreign_compose_project(
    fleet_inst_a_with_inst_b_present,
):
    """Escape (b'): cannot target inst-B's compose project via argv forgery."""
    pytest.skip("depends on T038 instance_mcp argv-bound --compose-project")


async def test_git_checkout_refuses_branch_leaving_ref(
    fleet_inst_a_with_inst_b_present,
):
    """Escape (c): checkout to a branch that is not the bound one is refused."""
    pytest.skip("depends on T040 git_mcp --branch guard")


async def test_git_reset_hard_refused_if_it_leaves_branch(
    fleet_inst_a_with_inst_b_present,
):
    """Escape (c'): reset --hard to a non-branch ref is refused."""
    pytest.skip("depends on T040 git_mcp reset --hard guard")


async def test_git_push_refuses_different_repo_url(
    fleet_inst_a_with_inst_b_present,
):
    """Escape (d): git push with a remote URL other than the bound repo is refused."""
    pytest.skip("depends on T040 git_mcp bound-repo guard")
