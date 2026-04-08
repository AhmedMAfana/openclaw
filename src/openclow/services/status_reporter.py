"""Reusable real-time status reporters for Telegram messages.

StatusReporter — animated spinner with stages, progress bar, and live logs.
LineReporter   — simple line-accumulator (drop-in for bootstrap's old StatusReporter).

Both extend BaseReporter for heartbeat, rate-limited editing, and elapsed timer.
"""
import time

from openclow.services.base_reporter import BaseReporter

SPINNERS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class StatusReporter(BaseReporter):
    """Live status reporter with animated spinner and progress bar."""

    def __init__(self, chat, chat_id: str, message_id: str, title: str = "Processing"):
        super().__init__(chat, chat_id, message_id, heartbeat_interval=2.0, rate_limit=1.5)
        self._title = title
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

    async def complete(self, message: str, keyboard=None):
        """Mark operation as complete with final message."""
        await self.stop()

        text = f"✅ {self._title} — Done ({self.elapsed}s)\n\n{message}"

        if keyboard is None:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Main Menu", callback_data="menu:main")],
            ])

        bot = self._chat._get_bot()
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=int(self._chat_id),
                message_id=int(self._message_id),
                reply_markup=keyboard,
            )
        except Exception:
            await self._force_render()

    async def error(self, message: str, keyboard=None):
        """Mark operation as failed."""
        await self.stop()

        text = f"❌ {self._title} — Failed ({self.elapsed}s)\n\n{message}"

        if keyboard is None:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Main Menu", callback_data="menu:main")],
            ])

        bot = self._chat._get_bot()
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=int(self._chat_id),
                message_id=int(self._message_id),
                reply_markup=keyboard,
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
            bar = "█" * filled + "░" * (self._total_steps - filled)
            parts.append(f"[{filled}/{self._total_steps}] {bar}")

        # Current stage
        if self._current_stage:
            parts.append(f"\n{self._current_stage}")

        # Live logs
        if self._logs:
            parts.append("")
            for line in self._logs:
                parts.append(f"  ▸ {line[:60]}")

        return "\n".join(parts)

    async def _render(self):
        """Render current state to Telegram (skip if text unchanged)."""
        text = self._build_text()
        if text != self._last_text:
            self._last_text = text
            await super()._render()


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
        """Add a status line and update Telegram."""
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
