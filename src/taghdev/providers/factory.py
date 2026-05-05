"""Provider factory — instantiates providers from DB config.

Usage:
    llm = await get_llm()
    chat = await get_chat()
    git = await get_git()
"""
import asyncio

from taghdev.providers.base import ChatProvider, GitProvider, LLMProvider
from taghdev.providers.registry import get_chat_provider, get_git_provider, get_llm_provider

# Import concrete providers so they register themselves.
# Each import is guarded — missing optional deps shouldn't crash the whole app.
import taghdev.providers.llm.claude  # noqa: F401
import taghdev.providers.chat.telegram  # noqa: F401
import taghdev.providers.chat.web  # noqa: F401
import taghdev.providers.git.github  # noqa: F401

try:
    import taghdev.providers.chat.slack  # noqa: F401
except ImportError:
    pass  # slack-bolt/slack-sdk not installed — Slack provider unavailable

_instances: dict[str, object] = {}
_lock = asyncio.Lock()


def _make_factory_managed(provider: ChatProvider) -> ChatProvider:
    """Mark provider as factory-managed so close() is a no-op."""
    provider._factory_managed = True
    _real_close = provider.close

    async def _noop_close():
        pass

    provider.close = _noop_close
    provider._real_close = _real_close
    return provider


async def get_llm() -> LLMProvider:
    async with _lock:
        if "llm" not in _instances:
            from taghdev.services.config_service import get_provider_config
            ptype, config = await get_provider_config("llm")
            _instances["llm"] = get_llm_provider(ptype, config)
    return _instances["llm"]


async def get_chat() -> ChatProvider:
    """Get the primary (active) chat provider."""
    async with _lock:
        if "chat" not in _instances:
            from taghdev.services.config_service import get_provider_config
            ptype, config = await get_provider_config("chat")
            provider = get_chat_provider(ptype, config)
            _make_factory_managed(provider)
            _instances["chat"] = provider
            _instances[f"chat.{ptype}"] = provider
    return _instances["chat"]


async def get_chat_by_type(provider_type: str) -> ChatProvider:
    """Get a chat provider by type, loading from DB config if needed.

    Used by worker tasks to route responses to the correct platform.
    """
    key = f"chat.{provider_type}"
    async with _lock:
        if key not in _instances:
            # Web provider is always available — no DB config needed, just Redis URL from settings
            if provider_type == "web":
                provider = get_chat_provider("web", {})
                _make_factory_managed(provider)
                _instances[key] = provider
            else:
                from taghdev.services.config_service import get_provider_config_by_type
                config = await get_provider_config_by_type("chat", provider_type)
                if not config:
                    raise ValueError(
                        f"No config found for chat provider '{provider_type}'. "
                        f"Configure it via Settings Dashboard → Chat."
                    )
                provider = get_chat_provider(provider_type, config)
                _make_factory_managed(provider)
                _instances[key] = provider
    return _instances[key]


async def get_all_configured_chat_providers() -> list[tuple[str, ChatProvider]]:
    """Load all configured (even inactive) chat providers. Used by bot startup."""
    from taghdev.services.config_service import get_all_chat_configs
    providers = []
    for ptype, config in await get_all_chat_configs():
        key = f"chat.{ptype}"
        async with _lock:
            if key not in _instances:
                try:
                    provider = get_chat_provider(ptype, config)
                    _make_factory_managed(provider)
                    _instances[key] = provider
                except Exception:
                    continue
            providers.append((ptype, _instances[key]))
    return providers


async def get_git() -> GitProvider:
    async with _lock:
        if "git" not in _instances:
            from taghdev.services.config_service import get_provider_config
            ptype, config = await get_provider_config("git")
            _instances["git"] = get_git_provider(ptype, config)
    return _instances["git"]


async def reset():
    """Clear cached instances (call after config change)."""
    async with _lock:
        for inst in _instances.values():
            # Use real close for factory-managed instances
            close_fn = getattr(inst, '_real_close', None) or getattr(inst, 'close', None)
            if close_fn:
                try:
                    await close_fn()
                except Exception:
                    pass
        _instances.clear()
