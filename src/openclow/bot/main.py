"""Bot entrypoint — delegates to the configured chat provider."""
import asyncio

from openclow.providers import factory
from openclow.utils.logging import get_logger

log = get_logger()


async def main():
    log.info("bot.starting")
    chat = await factory.get_chat()
    await chat.start_bot()


if __name__ == "__main__":
    asyncio.run(main())
