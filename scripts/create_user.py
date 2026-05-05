"""Add a Telegram user to the allowlist.

Usage: python -m scripts.create_user <telegram_id> [username]
"""
import asyncio
import sys

from sqlalchemy import select

from taghdev.models import User, async_session


async def main():
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.create_user <telegram_id> [username]")
        sys.exit(1)

    telegram_id = int(sys.argv[1])
    username = sys.argv[2] if len(sys.argv) > 2 else None

    async with async_session() as session:
        existing = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = existing.scalar_one_or_none()

        if user:
            user.is_allowed = True
            if username:
                user.telegram_username = username
            print(f"User {telegram_id} updated: is_allowed=True")
        else:
            user = User(
                telegram_id=telegram_id,
                telegram_username=username,
                is_allowed=True,
            )
            session.add(user)
            print(f"User {telegram_id} created: is_allowed=True")

        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
