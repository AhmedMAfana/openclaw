"""Configuration service — reads/writes platform config from DB.

All provider configuration is stored in the platform_config table.
This service is the single source of truth for provider settings.
"""
from sqlalchemy import select

from taghdev.models.base import async_session
from taghdev.models.config import PlatformConfig


async def get_config(category: str, key: str) -> dict | None:
    async with async_session() as session:
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == category,
                PlatformConfig.key == key,
                PlatformConfig.is_active == True,
            )
        )
        config = result.scalar_one_or_none()
        return config.value if config else None


def _select_config_stmt(category: str, key: str):
    return select(PlatformConfig).where(
        PlatformConfig.category == category,
        PlatformConfig.key == key,
    )


async def set_config(category: str, key: str, value: dict) -> None:
    from sqlalchemy.exc import IntegrityError

    async with async_session() as session:
        result = await session.execute(_select_config_stmt(category, key))
        existing = result.scalar_one_or_none()

        if existing:
            existing.value = value
            existing.is_active = True
        else:
            config = PlatformConfig(category=category, key=key, value=value)
            session.add(config)

        try:
            await session.commit()
        except IntegrityError:
            # Race condition: another worker inserted first — retry as update
            await session.rollback()
            result = await session.execute(_select_config_stmt(category, key))
            existing = result.scalar_one_or_none()
            if existing:
                existing.value = value
                existing.is_active = True
                await session.commit()


async def get_provider_config(category: str) -> tuple[str, dict]:
    """Get provider type and config for a category.

    Looks for per-type keys (e.g. provider.telegram) first,
    falls back to legacy single-key format (provider).

    Returns: (provider_type, config_dict)
    Example: ("claude", {"coder_max_turns": 50, ...})
    """
    # New format: look for any active provider.{type} key
    async with async_session() as session:
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == category,
                PlatformConfig.key.like("provider.%"),
                PlatformConfig.is_active == True,
            )
        )
        config_row = result.scalar_one_or_none()
        if config_row:
            ptype = config_row.key.split(".", 1)[1]  # "provider.telegram" -> "telegram"
            return ptype, config_row.value

    # Fallback: legacy single "provider" key
    config = await get_config(category, "provider")
    if not config:
        raise ValueError(
            f"No {category} provider configured. Run `python -m taghdev.setup` first."
        )
    provider_type = config.get("type")
    if not provider_type:
        raise ValueError(f"No 'type' field in {category} provider config.")
    remaining = {k: v for k, v in config.items() if k != "type"}
    return provider_type, remaining


async def set_provider_config(category: str, provider_type: str, config: dict) -> None:
    """Save provider config and mark it as the active one.

    Stores under key 'provider.{type}' (e.g. provider.telegram).
    Deactivates other provider configs in the same category.
    """
    key = f"provider.{provider_type}"

    async with async_session() as session:
        # Deactivate all other provider configs in this category
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == category,
                PlatformConfig.key.like("provider%"),
                PlatformConfig.key != key,
            )
        )
        for row in result.scalars().all():
            row.is_active = False

        # Upsert the new config
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == category,
                PlatformConfig.key == key,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = config
            existing.is_active = True
        else:
            session.add(PlatformConfig(category=category, key=key, value=config))

        await session.commit()


async def get_provider_config_by_type(category: str, provider_type: str) -> dict | None:
    """Get config for a specific provider type, even if it's not active.

    Used by test endpoints to retrieve saved credentials for non-active providers.
    """
    key = f"provider.{provider_type}"
    async with async_session() as session:
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == category,
                PlatformConfig.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            return row.value

    # Fallback: legacy single "provider" key
    config = await get_config(category, "provider")
    if config and config.get("type") == provider_type:
        return {k: v for k, v in config.items() if k != "type"}
    return None


async def get_all_chat_configs() -> list[tuple[str, dict]]:
    """Get all chat provider configs (active or not) that have provider.{type} keys.

    Returns list of (provider_type, config_dict) for every configured chat provider.
    """
    async with async_session() as session:
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == "chat",
                PlatformConfig.key.like("provider.%"),
            )
        )
        rows = result.scalars().all()

    configs = []
    for row in rows:
        ptype = row.key.split(".", 1)[1]
        configs.append((ptype, row.value))
    return configs


async def get_all_config() -> dict[str, dict]:
    async with async_session() as session:
        result = await session.execute(
            select(PlatformConfig).where(PlatformConfig.is_active == True)
        )
        configs = result.scalars().all()

    return {f"{c.category}.{c.key}": c.value for c in configs}


async def get_config_with_meta(category: str, key: str) -> dict | None:
    """Return config value plus updated_at timestamp."""
    async with async_session() as session:
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == category,
                PlatformConfig.key == key,
                PlatformConfig.is_active == True,
            )
        )
        config = result.scalar_one_or_none()
        if not config:
            return None
        return {
            "value": config.value,
            "updated_at": config.updated_at.isoformat() if config.updated_at else None,
        }


# ---------------------------------------------------------------------------
# Host-mode settings (category="host")
# ---------------------------------------------------------------------------
# Keys:
#   projects_base   — absolute host path where user apps live (simulate or prod)
#   mode_default    — "docker" | "host" — fallback when onboarding picks a mode
#   auto_clone      — whether the agent should clone missing repos on first setup

import os


_HOST_DEFAULTS = {
    "projects_base": os.environ.get("HOST_PROJECTS_BASE", "/srv/projects"),
    "mode_default": os.environ.get("PROJECT_MODE_DEFAULT", "docker"),
    "auto_clone": True,
}


async def get_host_setting(key: str):
    """Read a host.<key> setting from DB; fall back to env/default on miss."""
    row = await get_config("host", key)
    if row is None:
        return _HOST_DEFAULTS.get(key)
    # PlatformConfig stores dicts; unwrap {"value": ...} if present
    if isinstance(row, dict) and "value" in row and len(row) == 1:
        return row["value"]
    return row


async def set_host_setting(key: str, value) -> None:
    await set_config("host", key, {"value": value})


async def get_all_host_settings() -> dict:
    return {k: await get_host_setting(k) for k in _HOST_DEFAULTS}


async def delete_config(category: str, key: str) -> bool:
    """Soft-delete a config entry by setting is_active = False."""
    async with async_session() as session:
        result = await session.execute(
            select(PlatformConfig).where(
                PlatformConfig.category == category,
                PlatformConfig.key == key,
            )
        )
        config = result.scalar_one_or_none()
        if not config:
            return False
        config.is_active = False
        await session.commit()
        return True
