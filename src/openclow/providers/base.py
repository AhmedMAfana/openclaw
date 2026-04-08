"""Abstract base classes for all providers.

OpenClow is provider-agnostic. The core engine never imports
aiogram, claude_agent_sdk, or gh CLI directly. It uses these abstractions.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator


# ── LLM Provider ──────────────────────────────────────────────


@dataclass
class AgentResult:
    full_output: str
    num_turns: int


@dataclass
class ReviewResult:
    has_issues: bool
    issues: str
    raw_output: str


class LLMProvider(ABC):
    """Abstract LLM provider for coding and reviewing agents."""

    @abstractmethod
    async def run_planner(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        description: str,
        agent_system_prompt: str,
    ) -> str:
        """Analyze codebase and create implementation plan. Returns plan text."""
        ...

    @abstractmethod
    async def run_coder(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        description: str,
        agent_system_prompt: str,
        max_turns: int,
        plan: str = "",
        on_tool_use: Any | None = None,
    ) -> AsyncIterator[Any]:
        ...

    @abstractmethod
    async def run_coder_fix(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        description: str,
        agent_system_prompt: str,
        issues: str,
        max_turns: int,
    ) -> AsyncIterator[Any]:
        ...

    @abstractmethod
    async def run_reviewer(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        max_turns: int,
    ) -> ReviewResult:
        ...

    @abstractmethod
    def is_tool_use(self, message: Any) -> str | None:
        """If message contains a tool use, return the tool name. Else None."""
        ...

    @abstractmethod
    def is_result(self, message: Any) -> int | None:
        """If message is a final result, return num_turns. Else None."""
        ...


# ── Chat Provider ─────────────────────────────────────────────


@dataclass
class ChatContext:
    chat_id: str
    message_id: str | None
    user_identifier: str


class ChatProvider(ABC):
    """Abstract chat provider for user interaction."""

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> str:
        """Send a message. Returns message_id."""
        ...

    @abstractmethod
    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        ...

    @abstractmethod
    async def send_plan_preview(
        self, chat_id: str, message_id: str, task_id: str, plan: str
    ) -> None:
        """Send implementation plan with approve/reject actions."""
        ...

    @abstractmethod
    async def send_progress(
        self, chat_id: str, message_id: str, step: str, total_steps: int, current_step: int
    ) -> None:
        """Send progress update with step counter."""
        ...

    @abstractmethod
    async def send_summary(
        self, chat_id: str, message_id: str, task_id: str, summary: str, diff_summary: str
    ) -> None:
        """Send completion summary with Create PR / Discard buttons."""
        ...

    @abstractmethod
    async def send_diff_preview(
        self, chat_id: str, message_id: str, task_id: str, diff_summary: str
    ) -> None:
        ...

    @abstractmethod
    async def send_pr_created(
        self, chat_id: str, message_id: str, task_id: str, pr_url: str
    ) -> None:
        ...

    @abstractmethod
    async def send_error(self, chat_id: str, message_id: str | None, text: str) -> None:
        ...

    @abstractmethod
    async def send_terminal_message(self, chat_id: str, message_id: str | None, text: str) -> None:
        """Send a terminal-state message with navigation back to main menu."""
        ...

    @abstractmethod
    async def start_bot(self) -> None:
        """Start the chat bot (blocking)."""
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


# ── Git Provider ──────────────────────────────────────────────


class GitProvider(ABC):
    """Abstract git hosting provider."""

    @abstractmethod
    async def clone_repo(self, repo: str, dest: str) -> None:
        ...

    @abstractmethod
    async def create_pr(
        self, repo: str, branch: str, base: str, title: str, body: str
    ) -> tuple[str, int]:
        """Returns (pr_url, pr_number)."""
        ...

    @abstractmethod
    async def merge_pr(self, repo: str, pr_number: int) -> None:
        ...

    @abstractmethod
    async def close_pr(self, repo: str, pr_number: int) -> None:
        ...

    @abstractmethod
    async def delete_branch(self, repo: str, branch: str) -> None:
        ...

    @abstractmethod
    def generate_pr_body(self, task: Any) -> str:
        ...
