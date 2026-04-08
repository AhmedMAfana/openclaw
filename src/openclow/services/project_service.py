"""Project configuration service."""
from sqlalchemy import select

from openclow.models import Project, async_session


async def get_all_projects(include_inactive: bool = False) -> list[Project]:
    async with async_session() as session:
        query = select(Project).order_by(Project.name)
        if not include_inactive:
            query = query.where(Project.status == "active")
        result = await session.execute(query)
        return list(result.scalars().all())


async def get_project_by_name(name: str) -> Project | None:
    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == name))
        return result.scalar_one_or_none()


async def get_project_by_id(project_id: int) -> Project | None:
    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        return result.scalar_one_or_none()
