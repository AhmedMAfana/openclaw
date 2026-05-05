"""Bootstrap settings — only what's needed before DB is available.

Provider config (LLM, chat, git) is stored in the database,
managed via `python -m taghdev.setup`.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database (needed to boot)
    database_url: str = "postgresql+asyncpg://taghdev:taghdev@postgres:5432/taghdev"

    # Redis (needed to boot)
    redis_url: str = "redis://:taghdev@redis:6379/0"
    redis_password: str = "taghdev"

    # Workspace
    workspace_base_path: str = "/workspaces"

    # Groq API (speech-to-text fallback if DB config missing)
    groq_api_key: str = ""

    # Logging
    log_level: str = "INFO"

    # Activity log (JSONL file)
    activity_log: str = "/app/logs/activity.jsonl"

    # Claude agent limits
    claude_coder_max_turns: int = 50
    claude_reviewer_max_turns: int = 20

    # Retry behavior — never give up
    coder_max_retries: int = 3
    coder_retry_enabled: bool = True

    # Pipeline cache — skip health check if recently verified (seconds)
    health_cache_ttl: int = 120

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
