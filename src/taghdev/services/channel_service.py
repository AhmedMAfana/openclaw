"""Channel-to-project binding — links chat channels to specific projects."""
from __future__ import annotations

from taghdev.services.config_service import get_config, set_config
from taghdev.utils.logging import get_logger

log = get_logger()

CATEGORY = "channel"


async def get_channel_project(channel_id: str, provider_type: str = "slack") -> dict | None:
    """Get the project linked to a channel. Returns {project_id, project_name, channel_name} or None."""
    # Try new provider-scoped key first
    config = await get_config(CATEGORY, f"{provider_type}.{channel_id}")
    if config and config.get("project_id"):
        return config
    # Backward compat: old format without provider type
    config = await get_config(CATEGORY, f"project.{channel_id}")
    if config and config.get("project_id"):
        # Migrate to new format
        await set_channel_project(channel_id, config["project_id"], config.get("project_name", "unknown"), provider_type=provider_type)
        return config
    return None


async def set_channel_project(channel_id: str, project_id: int, project_name: str, provider_type: str = "slack", channel_name: str = "") -> None:
    """Link a chat channel to a project."""
    await set_config(CATEGORY, f"{provider_type}.{channel_id}", {
        "project_id": project_id,
        "project_name": project_name,
        "channel_name": channel_name,
    })
    log.info("channel.project_linked", channel_id=channel_id, project=project_name, provider=provider_type)


async def unset_channel_project(channel_id: str, provider_type: str = "slack") -> None:
    """Unlink a chat channel from its project."""
    from taghdev.models.base import async_session
    from taghdev.models.config import PlatformConfig
    from sqlalchemy import select
    async with async_session() as session:
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == CATEGORY,
                PlatformConfig.key == f"{provider_type}.{channel_id}",
            )
        )
        config = result.scalar_one_or_none()
        if config:
            await session.delete(config)
            await session.commit()
        else:
            # Backward compat: delete old-format key too
            result2 = await session.execute(
                select(PlatformConfig).where(
                    PlatformConfig.category == CATEGORY,
                    PlatformConfig.key == f"project.{channel_id}",
                )
            )
            config2 = result2.scalar_one_or_none()
            if config2:
                await session.delete(config2)
                await session.commit()
    log.info("channel.project_unlinked", channel_id=channel_id, provider=provider_type)


async def get_all_channel_bindings(provider_type: str | None = None) -> list[dict]:
    """Get all channel-project bindings. Optionally filter by provider_type."""
    from taghdev.services.config_service import get_all_config
    configs = await get_all_config()
    bindings = []
    for key, val in configs.items():
        if not key.startswith("channel."):
            continue
        # New format: channel.{provider_type}.{channel_id}
        prefix = "channel."
        rest = key[len(prefix):]
        if rest.startswith("project."):
            # Old format: channel.project.{channel_id}
            binding_provider = "slack"
            channel_id = rest.replace("project.", "")
        elif "." in rest:
            binding_provider, channel_id = rest.split(".", 1)
        else:
            continue
        if provider_type and binding_provider != provider_type:
            continue
        if val.get("project_id"):
            bindings.append({
                "channel_id": channel_id,
                "project_id": val["project_id"],
                "project_name": val.get("project_name", "unknown"),
                "channel_name": val.get("channel_name", ""),
                "provider_type": binding_provider,
            })
    return bindings
