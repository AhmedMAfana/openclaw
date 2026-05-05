"""Bot entrypoint — starts ALL configured chat providers concurrently."""
import asyncio
import time
from pathlib import Path

from openclow.providers import factory
from openclow.utils.logging import get_logger

log = get_logger()

_HEALTH_FILE = Path("/tmp/bot_health")
_POLL_INTERVAL = 15  # seconds between provider re-checks when none configured


async def _wait_for_providers():
    """Block until at least one provider is configured, writing the health file
    every poll cycle so the Docker healthcheck doesn't evict us."""
    log.warning("bot.no_providers_configured", hint="Use the Settings Dashboard to add one.")
    while True:
        _HEALTH_FILE.write_text(str(time.time()))
        await asyncio.sleep(_POLL_INTERVAL)
        providers = await factory.get_all_configured_chat_providers()
        if providers:
            return providers
        log.info("bot.waiting_for_providers")


async def main():
    log.info("bot.starting")
    providers = await factory.get_all_configured_chat_providers()

    if not providers:
        providers = await _wait_for_providers()

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
