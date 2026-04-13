"""Bot entrypoint — starts ALL configured chat providers concurrently."""
import asyncio

from openclow.providers import factory
from openclow.utils.logging import get_logger

log = get_logger()


async def main():
    log.info("bot.starting")
    providers = await factory.get_all_configured_chat_providers()

    if not providers:
        log.error("bot.no_providers_configured")
        raise SystemExit("No chat providers configured. Use the Settings Dashboard to add one.")

    log.info("bot.providers_found", types=[p[0] for p in providers])

    # Start all providers concurrently — each start_bot() blocks,
    # so we run them as parallel tasks.
    async def _run_provider(ptype: str, provider):
        try:
            await provider.start_bot()
        except Exception as e:
            log.error("bot.provider_crashed", provider=ptype, error=str(e), exc_info=True)
            raise

    tasks = []
    for ptype, provider in providers:
        log.info("bot.starting_provider", provider=ptype)
        tasks.append(asyncio.create_task(_run_provider(ptype, provider), name=f"bot-{ptype}"))

    # Wait for all — if any crashes, log it but keep others running.
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in done:
        if t.exception():
            log.error("bot.provider_exited", provider=t.get_name(), error=str(t.exception()))

    # If all crashed, exit
    if not pending:
        raise SystemExit("All chat providers crashed.")

    # Otherwise keep waiting on survivors
    await asyncio.wait(pending)


if __name__ == "__main__":
    asyncio.run(main())
