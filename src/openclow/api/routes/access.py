"""User-project access management — admin-only CRUD API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from openclow.api.web_auth import web_user_dep
from openclow.models.base import async_session
from openclow.models.project import Project
from openclow.models.user import User
from openclow.models.user_project_access import UserProjectAccess
from openclow.services.access_service import VALID_ROLES

router = APIRouter(prefix="/api/access", tags=["access"])


def _require_admin(user: User) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    return user


# ── Pydantic schemas ────────────────────────────────────────────────────────


class GrantRequest(BaseModel):
    user_id: int
    project_id: int
    role: str = "developer"


class RoleUpdateRequest(BaseModel):
    role: str


# ── Admin helper lists (for AccessPanel UI) ──────────────────────────────────


@router.get("/users-list")
async def list_users(admin: User = Depends(web_user_dep)):
    """All users. Admin only — used by AccessPanel."""
    _require_admin(admin)
    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.username))
        users = result.scalars().all()
    return {"users": [{"id": u.id, "username": u.username} for u in users]}


@router.get("/projects-list")
async def list_all_projects(admin: User = Depends(web_user_dep)):
    """All projects (unfiltered). Admin only — used by AccessPanel."""
    _require_admin(admin)
    async with async_session() as session:
        result = await session.execute(
            select(Project)
            .where(Project.status.in_(["active", "bootstrapping", "failed"]))
            .order_by(Project.name)
        )
        projects = result.scalars().all()
    return {"projects": [{"id": p.id, "name": p.name} for p in projects]}


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/summary")
async def access_summary(admin: User = Depends(web_user_dep)):
    """All grants with resolved user + project names. Admin only."""
    _require_admin(admin)
    async with async_session() as session:
        result = await session.execute(
            select(UserProjectAccess, User.username, Project.name)
            .join(User, UserProjectAccess.user_id == User.id)
            .join(Project, UserProjectAccess.project_id == Project.id)
            .order_by(User.username, Project.name)
        )
        rows = result.all()
    return [
        {
            "id": r.UserProjectAccess.id,
            "user_id": r.UserProjectAccess.user_id,
            "username": r.username,
            "project_id": r.UserProjectAccess.project_id,
            "project_name": r.name,
            "role": r.UserProjectAccess.role,
            "granted_by": r.UserProjectAccess.granted_by,
        }
        for r in rows
    ]


@router.get("/users/{user_id}/projects")
async def list_user_access(user_id: int, admin: User = Depends(web_user_dep)):
    """All project grants for a specific user. Admin only."""
    _require_admin(admin)
    async with async_session() as session:
        result = await session.execute(
            select(UserProjectAccess, Project.name)
            .join(Project, UserProjectAccess.project_id == Project.id)
            .where(UserProjectAccess.user_id == user_id)
        )
        rows = result.all()
    return [
        {
            "id": r.UserProjectAccess.id,
            "user_id": r.UserProjectAccess.user_id,
            "project_id": r.UserProjectAccess.project_id,
            "project_name": r.name,
            "role": r.UserProjectAccess.role,
            "granted_by": r.UserProjectAccess.granted_by,
        }
        for r in rows
    ]


@router.get("/projects/{project_id}/users")
async def list_project_users(project_id: int, admin: User = Depends(web_user_dep)):
    """All users who have access to a specific project. Admin only."""
    _require_admin(admin)
    async with async_session() as session:
        result = await session.execute(
            select(UserProjectAccess, User.username)
            .join(User, UserProjectAccess.user_id == User.id)
            .where(UserProjectAccess.project_id == project_id)
        )
        rows = result.all()
    return [
        {
            "id": r.UserProjectAccess.id,
            "user_id": r.UserProjectAccess.user_id,
            "username": r.username,
            "project_id": r.UserProjectAccess.project_id,
            "role": r.UserProjectAccess.role,
            "granted_by": r.UserProjectAccess.granted_by,
        }
        for r in rows
    ]


@router.post("/grants", status_code=201)
async def grant_access(body: GrantRequest, admin: User = Depends(web_user_dep)):
    """Grant a user access to a project. Admin only."""
    _require_admin(admin)
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}")

    async with async_session() as session:
        target_user = await session.get(User, body.user_id)
        if not target_user:
            raise HTTPException(404, "User not found")
        project = await session.get(Project, body.project_id)
        if not project:
            raise HTTPException(404, "Project not found")

        access = UserProjectAccess(
            user_id=body.user_id,
            project_id=body.project_id,
            role=body.role,
            granted_by=admin.id,
        )
        session.add(access)
        try:
            await session.commit()
            await session.refresh(access)
        except IntegrityError:
            await session.rollback()
            raise HTTPException(409, "Access grant already exists. Use PUT to update the role.")

    return {
        "id": access.id,
        "user_id": access.user_id,
        "project_id": access.project_id,
        "role": access.role,
    }


@router.put("/grants/{access_id}")
async def update_access(
    access_id: int, body: RoleUpdateRequest, admin: User = Depends(web_user_dep)
):
    """Update the role on an existing access grant. Admin only."""
    _require_admin(admin)
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}")

    async with async_session() as session:
        access = await session.get(UserProjectAccess, access_id)
        if not access:
            raise HTTPException(404, "Access grant not found")
        access.role = body.role
        await session.commit()

    return {"id": access_id, "role": body.role}


@router.delete("/grants/{access_id}")
async def revoke_access(access_id: int, admin: User = Depends(web_user_dep)):
    """Revoke a user's access to a project. Admin only."""
    _require_admin(admin)
    async with async_session() as session:
        access = await session.get(UserProjectAccess, access_id)
        if not access:
            raise HTTPException(404, "Access grant not found")
        await session.delete(access)
        await session.commit()

    return {"status": "ok", "revoked": access_id}
