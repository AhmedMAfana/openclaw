"""Provider factory — instantiates providers from DB config.

Usage:
    llm = await get_llm()
    chat = await get_chat()
    git = await get_git()
"""
import asyncio

from openclow.providers.base import ChatProvider, GitProvider, LLMProvider
from openclow.providers.registry import get_chat_provider, get_git_provider, get_llm_provider

# Import concrete providers so they register themselves
import openclow.providers.llm.claude  # noqa: F401
import openclow.providers.chat.telegram  # noqa: F401
import openclow.providers.git.github  # noqa: F401

_instances: dict[str, object] = {}
_lock = asyncio.Lock()


async def get_llm() -> LLMProvider:
    async with _lock:
        if "llm" not in _instances:
            from openclow.services.config_service import get_provider_config
            ptype, config = await get_provider_config("llm")
            _instances["llm"] = get_llm_provider(ptype, config)
    return _instances["llm"]


async def get_chat() -> ChatProvider:
    async with _lock:
        if "chat" not in _instances:
            from openclow.services.config_service import get_provider_config
            ptype, config = await get_provider_config("chat")
            _instances["chat"] = get_chat_provider(ptype, config)
    return _instances["chat"]


async def get_git() -> GitProvider:
    async with _lock:
        if "git" not in _instances:
            from openclow.services.config_service import get_provider_config
            ptype, config = await get_provider_config("git")
            _instances["git"] = get_git_provider(ptype, config)
    return _instances["git"]


async def reset():
    """Clear cached instances (call after config change)."""
    async with _lock:
        for inst in _instances.values():
            if hasattr(inst, 'close'):
                try:
                    await inst.close()
                except Exception:
                    pass
        _instances.clear()
