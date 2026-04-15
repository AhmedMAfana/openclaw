"""User-project access control service.

Roles and their allowed MCP tool sets:
  viewer    — read-only: list projects/tasks/status
  deployer  — ops: bootstrap, docker up/down, relink/unlink
  developer — coding: trigger tasks, add projects, QA
  all       — everything (project-scoped, not system-wide)

Admins (is_admin=True) always bypass all checks.

Backward compat rule:
  If a user has ZERO rows in user_project_access → unrestricted (sees all projects).
  Once any row is added for them → restricted to only those projects.
"""
from __future__ import annotations

from sqlalchemy import select

from openclow.models.base import async_session
from openclow.models.project import Project
from openclow.models.user_project_access import UserProjectAccess

# Tools allowed per role. None means unrestricted.
ROLE_TOOLS: dict[str, set[str] | None] = {
    "viewer": {
        "list_projects",
        "list_tasks",
        "system_status",
    },
    "deployer": {
        "list_projects",
        "list_tasks",
        "system_status",
        "bootstrap",
        "docker_up",
        "docker_down",
        "relink_project",
        "unlink_project",
    },
    "developer": {
        "list_projects",
        "list_tasks",
        "system_status",
        "trigger_task",
        "trigger_addproject",
        "check_pending_project",
        "confirm_project",
        "run_qa",
    },
    "all": None,  # None = unrestricted
}

_ROLE_PRIORITY = ["all", "developer", "deployer", "viewer"]

VALID_ROLES = frozenset(ROLE_TOOLS.keys())


def is_tool_allowed(role: str | None, tool_name: str) -> bool:
    """Return True if the role permits calling this tool.

    role=None means unrestricted (admin or no access rows configured).
    """
    if role is None:
        return True
    allowed = ROLE_TOOLS.get(role)
    if allowed is None:  # "all" role
        return True
    return tool_name in allowed


async def get_user_project_access_rows(user_id: int) -> list[UserProjectAccess]:
    """Fetch all access rows for a user. Empty list = no restrictions configured yet."""
    async with async_session() as session:
        result = await session.execute(
            select(UserProjectAccess).where(UserProjectAccess.user_id == user_id)
        )
        return list(result.scalars().all())


async def get_user_role_for_project(user_id: int, project_id: int) -> str | None:
    """Return role string if user has access to this project, else None."""
    async with async_session() as session:
        result = await session.execute(
            select(UserProjectAccess).where(
                UserProjectAccess.user_id == user_id,
                UserProjectAccess.project_id == project_id,
            )
        )
        row = result.scalar_one_or_none()
    return row.role if row else None


async def get_accessible_projects_for_mcp(
    user_id: int, is_admin: bool
) -> tuple[list[Project], str | None]:
    """Return (projects, effective_role) the user may access.

    effective_role=None means unrestricted (admin, or no rows configured).
    effective_role set = restricted to those projects and that role's tool set.
    """
    async with async_session() as session:
        if is_admin:
            result = await session.execute(
                select(Project).where(
                    Project.status.in_(["active", "bootstrapping", "failed"])
                )
            )
            return list(result.scalars().all()), None

        rows = await get_user_project_access_rows(user_id)

        if not rows:
            # Backward compat: no rows = unrestricted
            result = await session.execute(
                select(Project).where(
                    Project.status.in_(["active", "bootstrapping", "failed"])
                )
            )
            return list(result.scalars().all()), None

        project_ids = [r.project_id for r in rows]
        roles = [r.role for r in rows]
        effective_role = _broadest_role(roles)

        result = await session.execute(
            select(Project).where(
                Project.id.in_(project_ids),
                Project.status.in_(["active", "bootstrapping", "failed"]),
            )
        )
        return list(result.scalars().all()), effective_role


def _broadest_role(roles: list[str]) -> str:
    """Return the broadest role from a list (e.g. developer > deployer > viewer)."""
    for r in _ROLE_PRIORITY:
        if r in roles:
            return r
    return "viewer"
