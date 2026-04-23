"""Seed the `tagh-fre` project row for per-chat-instances dev + T031 e2e.

Usage:
    docker compose run --rm api python -m scripts.seed_tagh_fre

Idempotent: re-running updates the existing row in place.

Why this script exists:
    T031 (provision/teardown e2e) needs a real `Project` row whose
    `github_repo` clones to a live Laravel+Vue repo the per-instance
    compose renderer can work against. `tagh-fre` is that fixture.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from openclow.models import Project, async_session


PROJECT = {
    "name": "tagh-fre",
    "github_repo": "AhmedMAfana/tagh-fre",
    "default_branch": "main",
    "description": "Laravel 12 + Vue 3 + Vite — per-chat-instances seed repo",
    "tech_stack": "Laravel 12, PHP 8.2+, Vue 3, Vite, MySQL 8, Sail",
    "mode": "container",
    "auto_clone": True,
    "tunnel_enabled": True,
    "status": "active",
    "is_dockerized": True,
}


async def main() -> None:
    async with async_session() as session:
        existing = await session.execute(
            select(Project).where(Project.name == PROJECT["name"])
        )
        project = existing.scalar_one_or_none()

        if project:
            for key, value in PROJECT.items():
                setattr(project, key, value)
            action = "updated"
        else:
            project = Project(**PROJECT)
            session.add(project)
            action = "created"

        await session.commit()
        await session.refresh(project)
        print(f"[seed_tagh_fre] {action} project id={project.id} "
              f"name={project.name!r} mode={project.mode!r}")


if __name__ == "__main__":
    asyncio.run(main())
