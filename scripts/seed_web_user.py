#!/usr/bin/env python
"""Seed a test user for web chat testing."""
from __future__ import annotations

import asyncio
import sys

async def seed_user(username: str = "testuser", password: str = "testpass123"):
    """Create or update a test user with web password."""
    from openclow.models.user import User
    from openclow.models.base import async_session
    from openclow.models.project import Project
    from openclow.api.web_auth import hash_password
    from sqlalchemy import select

    async with async_session() as session:
        # Check if user exists
        result = await session.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()

        if not user:
            print(f"📝 Creating test user '{username}'...")
            user = User(
                username=username,
                chat_provider_uid=f"test:{username}",
                chat_provider_type="web",
                is_allowed=True,
                is_admin=True,
            )
            session.add(user)
            await session.flush()
        else:
            print(f"✏️  Updating existing user '{username}'...")

        # Set web password
        user.web_password_hash = hash_password(password)
        await session.commit()

        user_id = user.id
        print(f"✅ User '{username}' ready (ID: {user_id})")
        print(f"   Username: {username}")
        print(f"   Password: {password}")

        # Get or create a test project
        result = await session.execute(
            select(Project).where(Project.name == "test-project").where(Project.status == "active")
        )
        project = result.scalar_one_or_none()

        if not project:
            print(f"📁 Creating test project...")
            project = Project(
                name="test-project",
                github_repo="local/test-project",
                tech_stack="Python/FastAPI",
                description="Test project for web chat",
                status="active",
            )
            session.add(project)
            await session.commit()
            print(f"✅ Test project created (ID: {project.id})")
        else:
            print(f"✅ Test project exists (ID: {project.id})")

        print(f"\n🌐 Web chat login URL: http://localhost/chat/login")
        print(f"📝 Credentials: {username} / {password}")

if __name__ == "__main__":
    username = sys.argv[1] if len(sys.argv) > 1 else "testuser"
    password = sys.argv[2] if len(sys.argv) > 2 else "testpass123"

    asyncio.run(seed_user(username, password))
