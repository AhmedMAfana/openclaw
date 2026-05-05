"""Seed project configurations.

Usage: python -m scripts.seed_projects
Edit the PROJECTS list below to add your projects.
"""
import asyncio

from sqlalchemy import select

from taghdev.models import Project, async_session

# =============================================
# ADD YOUR PROJECTS HERE
# =============================================
PROJECTS = [
    {
        "name": "test-app",
        "github_repo": "your-username/your-laravel-repo",
        "default_branch": "main",
        "description": "A Laravel + Vue web application",
        "tech_stack": "Laravel 11, Vue 3, Tailwind CSS",
        "agent_system_prompt": "",
        "setup_commands": "cp .env.example .env\nphp artisan key:generate",
    },
    # Add more projects:
    # {
    #     "name": "admin-panel",
    #     "github_repo": "your-username/admin-panel",
    #     "default_branch": "main",
    #     ...
    # },
]


async def main():
    async with async_session() as session:
        for proj_data in PROJECTS:
            existing = await session.execute(
                select(Project).where(Project.name == proj_data["name"])
            )
            project = existing.scalar_one_or_none()

            if project:
                for key, value in proj_data.items():
                    setattr(project, key, value)
                print(f"Updated project: {proj_data['name']}")
            else:
                project = Project(**proj_data)
                session.add(project)
                print(f"Created project: {proj_data['name']}")

        await session.commit()
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
