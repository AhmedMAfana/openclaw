#!/usr/bin/env python
"""Seed RBAC test users and access grants for E2E testing.

Grants are on REAL projects already in the DB — no fake projects created.

Users created:
  admin_user    / admin123    → is_admin=True (sees everything)
  dev_user      / dev123      → developer on every connected project
  viewer_user   / viewer123   → viewer on every connected project
  deployer_user / deploy123   → deployer on every connected project
  noaccess_user / noaccess123 → no rows (backward compat: sees all)

Run inside the container:
  docker compose exec api python scripts/seed_rbac.py
"""
from __future__ import annotations
import asyncio


async def main() -> None:
    from sqlalchemy import select
    from taghdev.models.base import async_session
    from taghdev.models.user import User
    from taghdev.models.project import Project
    from taghdev.models.user_project_access import UserProjectAccess
    from taghdev.api.web_auth import hash_password

    async with async_session() as session:

        # ── Real projects from DB ─────────────────────────────────────────────
        print("\n── Real connected projects ───────────────────────────────────")
        r = await session.execute(
            select(Project).where(Project.status.in_(["active", "bootstrapping"]))
        )
        real_projects = list(r.scalars().all())
        if not real_projects:
            print("  ⚠️  No active projects in DB — add a project first, then re-run this seeder.")
            return
        for p in real_projects:
            print(f"  ✔  id={p.id}  name={p.name!r}  status={p.status}")

        # ── Users ─────────────────────────────────────────────────────────────
        async def upsert_user(username: str, password: str, is_admin: bool = False) -> User:
            r = await session.execute(select(User).where(User.username == username))
            u = r.scalar_one_or_none()
            if not u:
                u = User(
                    username=username,
                    chat_provider_uid=f"web:{username}",
                    chat_provider_type="web",
                    is_allowed=True,
                    is_admin=is_admin,
                )
                session.add(u)
                await session.flush()
                print(f"  ✅ Created '{username}' (id={u.id}, admin={is_admin})")
            else:
                u.is_admin = is_admin
                u.is_allowed = True
                print(f"  ✔  Updated '{username}' (id={u.id}, admin={is_admin})")
            u.web_password_hash = hash_password(password)
            return u

        print("\n── Users ─────────────────────────────────────────────────────")
        admin_u    = await upsert_user("admin_user",    "admin123",   is_admin=True)
        dev_u      = await upsert_user("dev_user",      "dev123")
        viewer_u   = await upsert_user("viewer_user",   "viewer123")
        deployer_u = await upsert_user("deployer_user", "deploy123")
        _          = await upsert_user("noaccess_user", "noaccess123")   # no grants

        # ── Access grants on real projects ────────────────────────────────────
        async def grant(user: User, project: Project, role: str) -> None:
            r = await session.execute(
                select(UserProjectAccess).where(
                    UserProjectAccess.user_id == user.id,
                    UserProjectAccess.project_id == project.id,
                )
            )
            row = r.scalar_one_or_none()
            if row:
                row.role = role
                print(f"  ✔  Updated  {user.username} → {project.name} : {role}")
            else:
                session.add(UserProjectAccess(
                    user_id=user.id,
                    project_id=project.id,
                    role=role,
                    granted_by=admin_u.id,
                ))
                print(f"  ✅ Granted  {user.username} → {project.name} : {role}")

        print("\n── Access grants ─────────────────────────────────────────────")
        for proj in real_projects:
            await grant(dev_u,      proj, "developer")
            await grant(viewer_u,   proj, "viewer")
            await grant(deployer_u, proj, "deployer")
            # noaccess_user intentionally has NO rows

        await session.commit()

    print("\n── Summary ───────────────────────────────────────────────────────")
    proj_names = ", ".join(p.name for p in real_projects)
    print(f"  admin_user    / admin123    → admin, sees everything")
    print(f"  dev_user      / dev123      → developer on: {proj_names}")
    print(f"  viewer_user   / viewer123   → viewer on: {proj_names}")
    print(f"  deployer_user / deploy123   → deployer on: {proj_names}")
    print(f"  noaccess_user / noaccess123 → no grants (backward compat: sees all)")
    print(f"\n  URL: http://localhost:8000/chat/login")


if __name__ == "__main__":
    asyncio.run(main())
