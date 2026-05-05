"""Pydantic schemas for the settings dashboard API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Provider config schemas
# ---------------------------------------------------------------------------

class ClaudeConfig(BaseModel):
    type: Literal["claude"] = "claude"
    coder_max_turns: int = Field(default=50, ge=1, le=200)
    reviewer_max_turns: int = Field(default=20, ge=1, le=100)


class OpenAIConfig(BaseModel):
    type: Literal["openai"] = "openai"
    api_key: str = Field(..., min_length=10)
    model: str = "gpt-4o"


class TelegramConfig(BaseModel):
    type: Literal["telegram"] = "telegram"
    token: str = Field(..., min_length=20)
    redis_url: str = ""


class SlackConfig(BaseModel):
    type: Literal["slack"] = "slack"
    bot_token: str = Field(..., min_length=10)
    app_token: str = Field(..., min_length=10)
    signing_secret: str = Field(..., min_length=10)


class GitHubConfig(BaseModel):
    type: Literal["github"] = "github"
    token: str = Field(..., min_length=10)


class GitLabConfig(BaseModel):
    type: Literal["gitlab"] = "gitlab"
    token: str = Field(..., min_length=10)
    base_url: str = "https://gitlab.com"


# ---------------------------------------------------------------------------
# Generic provider update (accepts any provider config)
# ---------------------------------------------------------------------------

class ProviderConfigUpdate(BaseModel):
    """Flexible schema — validated per-category in the endpoint."""
    type: str
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Project schemas
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    github_repo: str = Field(..., min_length=3)
    default_branch: str = "main"
    description: str | None = None
    tech_stack: str | None = None
    agent_system_prompt: str | None = None
    setup_commands: str | None = None
    mode: str = "docker"
    is_dockerized: bool = True
    docker_compose_file: str | None = "docker-compose.yml"
    app_container_name: str | None = None
    app_port: int | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    github_repo: str | None = None
    default_branch: str | None = None
    description: str | None = None
    tech_stack: str | None = None
    agent_system_prompt: str | None = None
    setup_commands: str | None = None
    is_dockerized: bool | None = None
    docker_compose_file: str | None = None
    app_container_name: str | None = None
    app_port: int | None = None
    # Host mode + public URL
    mode: str | None = None
    project_dir: str | None = None
    start_command: str | None = None
    stop_command: str | None = None
    health_url: str | None = None
    process_manager: str | None = None
    public_url: str | None = None
    tunnel_enabled: bool | None = None


class ProjectResponse(BaseModel):
    id: int
    name: str
    github_repo: str
    # Tolerate NULL on legacy/manually-inserted rows — older container-mode
    # rows can be added with just (name, mode, status, github_repo) and
    # the rest left to defaults. Without `| None` the list endpoint 500s
    # on the first NULL row and the Settings page goes blank.
    default_branch: str | None = None
    description: str | None = None
    tech_stack: str | None = None
    is_dockerized: bool | None = None
    docker_compose_file: str | None
    app_container_name: str | None
    app_port: int | None
    status: str
    mode: str = "docker"
    project_dir: str | None = None
    start_command: str | None = None
    stop_command: str | None = None
    health_url: str | None = None
    process_manager: str | None = None
    public_url: str | None = None
    tunnel_enabled: bool = True
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# User schemas
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    chat_provider_type: str = "telegram"
    chat_provider_uid: str = Field(..., min_length=1)
    username: str | None = None
    is_allowed: bool = True
    web_password_hash: str | None = None


class UserResponse(BaseModel):
    id: int
    chat_provider_type: str
    chat_provider_uid: str
    username: str | None
    is_allowed: bool
    is_admin: bool = False
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Connection test result
# ---------------------------------------------------------------------------

class TestResult(BaseModel):
    status: Literal["ok", "error"]
    message: str
    details: dict | None = None
