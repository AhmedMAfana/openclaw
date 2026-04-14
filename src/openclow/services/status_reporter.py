"""Reusable real-time status reporters for chat messages.

StatusReporter — animated spinner with stages, progress bar, and live logs.
LineReporter   — simple line-accumulator (drop-in for bootstrap's old StatusReporter).

Both extend BaseReporter for heartbeat, rate-limited editing, and elapsed timer.
Platform-agnostic — uses ActionKeyboard instead of aiogram InlineKeyboardMarkup.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from openclow.services.base_reporter import BaseReporter

if TYPE_CHECKING:
    from openclow.providers.actions import ActionKeyboard

SPINNERS = ["🔄", "⏳", "🔄", "⏳"]


class StatusReporter(BaseReporter):
    """Live status reporter with animated spinner and progress bar."""

    def __init__(self, chat, chat_id: str, message_id: str, title: str = "Processing",
                 task_id: str = ""):
        super().__init__(chat, chat_id, message_id, heartbeat_interval=2.0, rate_limit=1.5)
        self._title = title
        self._task_id = task_id
        self._current_stage = ""
        self._current_step = 0
        self._total_steps = 0
        self._logs: list[str] = []
        self._max_logs = 4
        self._last_text = ""

    async def stage(self, name: str, step: int = 0, total: int = 0):
        """Set the current stage. Updates display immediately."""
        self._current_stage = name
        if step:
            self._current_step = step
        if total:
            self._total_steps = total
        await self._render()

    async def log(self, line: str):
        """Add a live log line (shows last N lines)."""
        self._logs.append(line)
        if len(self._logs) > self._max_logs:
            self._logs = self._logs[-self._max_logs:]
        await self._render()

    async def complete(self, message: str, keyboard: ActionKeyboard | None = None):
        """Mark operation as complete with final message."""
        await self.stop()

        if keyboard is None:
            from openclow.providers.actions import back_keyboard
            keyboard = back_keyboard()

        text = f"✅ {self._title} — Done ({self.elapsed}s)\n\n{message}"
        try:
            await self._chat.edit_message_with_actions(
                self._chat_id, self._message_id, text, keyboard,
            )
        except Exception:
            await self._force_render()

    async def error(self, message: str, keyboard: ActionKeyboard | None = None):
        """Mark operation as failed."""
        await self.stop()

        if keyboard is None:
            from openclow.providers.actions import back_keyboard
            keyboard = back_keyboard()

        text = f"❌ {self._title} — Failed ({self.elapsed}s)\n\n{message}"
        try:
            await self._chat.edit_message_with_actions(
                self._chat_id, self._message_id, text, keyboard,
            )
        except Exception:
            await self._force_render()

    def _build_text(self) -> str:
        """Build the current status message."""
        # Derive spinner frame from elapsed time instead of a counter
        spin_idx = self.elapsed // 2
        spinner = SPINNERS[spin_idx % len(SPINNERS)]

        parts = [f"{spinner} {self._title} ({self.elapsed}s)"]

        # Progress bar
        if self._total_steps > 0:
            filled = min(self._current_step, self._total_steps)
            bar = "🟩" * filled + "⬜" * (self._total_steps - filled)
            pct = int(filled / self._total_steps * 100) if self._total_steps else 0
            parts.append(f"{bar} {pct}%")

        # Current stage
        if self._current_stage:
            parts.append(f"\n{self._current_stage}")

        # Live logs — show last N with truncation
        if self._logs:
            parts.append("")
            for line in self._logs:
                parts.append(f"  ▸ {line[:80]}")

        return "\n".join(parts)

    def _cancel_keyboard(self) -> ActionKeyboard | None:
        """Build a cancel keyboard if task_id is set."""
        if not self._task_id:
            return None
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        return ActionKeyboard(rows=[
            ActionRow([ActionButton("❌ Cancel", f"task_cancel:{self._task_id}")]),
        ])

    async def _render(self):
        """Render current state (skip if text unchanged)."""
        text = self._build_text()
        if text != self._last_text:
            self._last_text = text
            await super()._render(self._cancel_keyboard())


class LineReporter(BaseReporter):
    """Simple line-accumulator reporter.

    Drop-in replacement for the old bootstrap.StatusReporter that used
    add(icon, text) / section() / force_send().
    """

    def __init__(self, chat, chat_id: str, message_id: str, title: str):
        # No heartbeat by default — these are used for short sequences
        super().__init__(chat, chat_id, message_id, heartbeat_interval=0, rate_limit=2.0)
        self.lines = [f"⚙️ {title}\n"]

    def _build_text(self) -> str:
        return "\n".join(self.lines)

    async def add(self, icon: str, text: str, replace_last: bool = False):
        """Add a status line and update."""
        line = f"{icon} {text}"
        if replace_last and len(self.lines) > 1:
            self.lines[-1] = line
        else:
            self.lines.append(line)
        await self._render()

    async def section(self, title: str):
        """Add a section separator."""
        self.lines.append(f"\n{'─' * 20}")
        self.lines.append(f"📌 {title}")
        await self._render()

    async def force_send(self):
        """Force send regardless of rate limit."""
        await self._force_render()

    def text(self) -> str:
        return self._build_text()
