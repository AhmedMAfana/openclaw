"""Live checklist reporter that updates a single Telegram message in-place.

Shows ALL steps upfront with status icons and a progress bar,
then ticks each one off as work completes. Background heartbeat
keeps the elapsed timer alive even during long-running commands.
"""
from openclow.services.base_reporter import BaseReporter

ICONS = {"pending": "⬜", "running": "🔄", "done": "✅", "failed": "❌", "skipped": "⏭️"}


class ChecklistReporter(BaseReporter):
    """Step-based checklist with progress bar and live Telegram updates."""

    def __init__(self, chat, chat_id: str, message_id: str, title: str, subtitle: str = ""):
        super().__init__(chat, chat_id, message_id, heartbeat_interval=5.0, rate_limit=1.5)
        self.title = title
        self.subtitle = subtitle
        self.steps: list[dict] = []
        self._footer = ""

    # -- Step management -------------------------------------------------------

    def set_steps(self, step_names: list[str]):
        """Set the full checklist from step names."""
        self.steps = [{"name": n, "status": "pending", "detail": ""} for n in step_names]

    def add_steps(self, step_names: list[str]):
        """Append more steps to the checklist."""
        self.steps.extend({"name": n, "status": "pending", "detail": ""} for n in step_names)

    async def start_step(self, index: int):
        if 0 <= index < len(self.steps):
            self.steps[index]["status"] = "running"
            self.steps[index]["detail"] = ""
            await self._render()

    async def update_step(self, index: int, detail: str):
        if 0 <= index < len(self.steps):
            self.steps[index]["detail"] = detail
            await self._render()

    async def complete_step(self, index: int, detail: str = ""):
        if 0 <= index < len(self.steps):
            self.steps[index]["status"] = "done"
            if detail:
                self.steps[index]["detail"] = detail
            await self._render()

    async def fail_step(self, index: int, detail: str = ""):
        if 0 <= index < len(self.steps):
            self.steps[index]["status"] = "failed"
            if detail:
                self.steps[index]["detail"] = detail
            await self._render()

    async def skip_step(self, index: int, detail: str = ""):
        if 0 <= index < len(self.steps):
            self.steps[index]["status"] = "skipped"
            if detail:
                self.steps[index]["detail"] = detail
            await self._render()

    # -- Rendering -------------------------------------------------------------

    def _build_text(self) -> str:
        total = len(self.steps)
        done = sum(1 for s in self.steps if s["status"] in ("done", "skipped"))
        failed = sum(1 for s in self.steps if s["status"] == "failed")

        # Header with title
        lines = [f"🤖 {self.title} ({self.elapsed}s)"]

        # Progress bar
        if total > 0:
            filled = done + failed
            bar_len = 20
            bar_filled = int((filled / total) * bar_len)
            bar = "█" * bar_filled + "░" * (bar_len - bar_filled)
            pct = int((filled / total) * 100)
            lines.append(f"[{bar}] {filled}/{total} ({pct}%)")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━")

        if self.subtitle:
            lines.append(self.subtitle)

        lines.append("")

        # Steps
        for step in self.steps:
            icon = ICONS.get(step["status"], "⬜")
            line = f"{icon} {step['name']}"
            if step["detail"]:
                line += f" — {step['detail'][:50]}"
            if step["status"] == "running" and not step["detail"]:
                line += "..."
            lines.append(line)

        if self._footer:
            lines.append("")
            lines.append(self._footer)

        return "\n".join(lines)

    def text(self) -> str:
        """Plain-text summary without sending to Telegram."""
        lines = []
        for step in self.steps:
            icon = ICONS.get(step["status"], "⬜")
            line = f"{icon} {step['name']}"
            if step["detail"]:
                line += f" — {step['detail'][:50]}"
            lines.append(line)
        return "\n".join(lines)
