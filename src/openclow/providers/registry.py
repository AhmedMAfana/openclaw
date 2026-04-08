"""Provider registry — pluggable provider system.

Providers register themselves via decorators. Third-party packages
can add providers by calling register_llm/register_chat/register_git.
"""
from openclow.providers.base import ChatProvider, GitProvider, LLMProvider

_llm_providers: dict[str, type[LLMProvider]] = {}
_chat_providers: dict[str, type[ChatProvider]] = {}
_git_providers: dict[str, type[GitProvider]] = {}


def register_llm(name: str):
    def decorator(cls):
        _llm_providers[name] = cls
        return cls
    return decorator


def register_chat(name: str):
    def decorator(cls):
        _chat_providers[name] = cls
        return cls
    return decorator


def register_git(name: str):
    def decorator(cls):
        _git_providers[name] = cls
        return cls
    return decorator


def get_llm_provider(name: str, config: dict) -> LLMProvider:
    cls = _llm_providers.get(name)
    if not cls:
        available = list(_llm_providers.keys())
        raise ValueError(f"Unknown LLM provider: {name}. Available: {available}")
    return cls(config)


def get_chat_provider(name: str, config: dict) -> ChatProvider:
    cls = _chat_providers.get(name)
    if not cls:
        available = list(_chat_providers.keys())
        raise ValueError(f"Unknown chat provider: {name}. Available: {available}")
    return cls(config)


def get_git_provider(name: str, config: dict) -> GitProvider:
    cls = _git_providers.get(name)
    if not cls:
        available = list(_git_providers.keys())
        raise ValueError(f"Unknown git provider: {name}. Available: {available}")
    return cls(config)


def available_providers() -> dict[str, list[str]]:
    return {
        "llm": list(_llm_providers.keys()),
        "chat": list(_chat_providers.keys()),
        "git": list(_git_providers.keys()),
    }


def provider_schema() -> dict[str, dict[str, list[dict]]]:
    """Return field definitions for each provider (used by the settings UI)."""
    return {
        "llm": {
            "claude": [
                {"name": "coder_max_turns", "label": "Coder Max Turns", "type": "number", "default": 50, "min": 1, "max": 200},
                {"name": "reviewer_max_turns", "label": "Reviewer Max Turns", "type": "number", "default": 20, "min": 1, "max": 100},
            ],
            "openai": [
                {"name": "api_key", "label": "API Key", "type": "password", "required": True},
                {"name": "model", "label": "Model", "type": "text", "default": "gpt-4o"},
            ],
        },
        "chat": {
            "telegram": [
                {"name": "token", "label": "Bot Token", "type": "password", "required": True, "help": "Get from @BotFather on Telegram"},
            ],
            "slack": [
                {"name": "bot_token", "label": "Bot Token", "type": "password", "required": True, "help": "xoxb-... token"},
                {"name": "app_token", "label": "App Token", "type": "password", "required": True, "help": "xapp-... token"},
                {"name": "signing_secret", "label": "Signing Secret", "type": "password", "required": True},
            ],
        },
        "git": {
            "github": [
                {"name": "token", "label": "Personal Access Token", "type": "password", "required": True, "help": "Fine-grained PAT with Contents + PR permissions"},
            ],
            "gitlab": [
                {"name": "token", "label": "Access Token", "type": "password", "required": True},
                {"name": "base_url", "label": "GitLab URL", "type": "text", "default": "https://gitlab.com"},
            ],
        },
    }
