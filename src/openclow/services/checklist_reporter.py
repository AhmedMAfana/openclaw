"""Live checklist reporter that updates a single Telegram message in-place.

Shows ALL steps upfront with status icons and a progress bar,
then ticks each one off as work completes. Background heartbeat
keeps the elapsed timer alive even during long-running commands.
"""
from openclow.services.base_reporter import BaseReporter
from openclow.utils.logging import get_logger

log = get_logger()

ICONS = {"pending": "⬜", "running": "🔄", "done": "✅", "failed": "❌", "skipped": "✅"}


class ChecklistReporter(BaseReporter):
    """Step-based checklist with progress bar and live Telegram updates."""

    def __init__(self, chat, chat_id: str, message_id: str, title: str, subtitle: str = ""):
        super().__init__(chat, chat_id, message_id, heartbeat_interval=5.0, rate_limit=1.5)
        self.title = title
        self.subtitle = subtitle
        self.steps: list[dict] = []
        self._footer = ""
        self._keyboard = None

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

    async def log(self, line: str):
        """Compat with StatusReporter.log() — updates the currently running step's detail."""
        running = next((i for i, s in enumerate(self.steps) if s["status"] == "running"), None)
        if running is not None:
            await self.update_step(running, line[:50])

    # -- Rendering -------------------------------------------------------------

    def _build_text(self) -> str:
        total = len(self.steps)
        done = sum(1 for s in self.steps if s["status"] in ("done", "skipped"))
        failed = sum(1 for s in self.steps if s["status"] == "failed")
        running = sum(1 for s in self.steps if s["status"] == "running")

        # Progress: completed steps fill fully, running step fills based on elapsed time
        # This gives smooth visual progress instead of jumping 0→33→66→100
        bar_len = 10
        if total > 0:
            base_progress = (done + failed) / total
            # Running step fills gradually over 30s
            if running > 0:
                step_progress = min(0.9, self.elapsed / 30) / total
                progress = base_progress + step_progress
            else:
                progress = base_progress
            filled = max(1 if running or done else 0, min(bar_len, int(progress * bar_len)))
            bar = "🟩" * filled + "⬜" * (bar_len - filled)
        else:
            bar = "⬜" * bar_len

        # Header — compact
        lines = [f"🤖 *{self.title}* `{self.elapsed}s`"]
        lines.append(bar)
        lines.append("")

        # Steps — compact
        for step in self.steps:
            icon = ICONS.get(step["status"], "⬜")
            line = f"{icon} {step['name']}"
            if step["detail"]:
                line += f" — {step['detail'][:50]}"
            elif step["status"] == "running":
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

    # -- Web chat structured card (overrides text-based rendering) ----------------

    async def _render(self, keyboard=None):
        """Web chat: emit a structured card on every step update; other platforms: text."""
        if keyboard is not None:
            self._keyboard = keyboard
        if hasattr(self._chat, "send_progress_card"):
            await self._emit_card()
            return
        await super()._render(keyboard)

    async def _force_render(self, keyboard=None):
        """Web chat gets a structured progress_card event; other platforms get text."""
        if keyboard is not None:
            self._keyboard = keyboard
        if hasattr(self._chat, "send_progress_card"):
            await self._emit_card()
            return
        await super()._force_render(keyboard)

    def _build_card(self) -> dict:
        total = len(self.steps)
        done = sum(1 for s in self.steps if s["status"] in ("done", "skipped"))
        failed = sum(1 for s in self.steps if s["status"] == "failed")
        pending = sum(1 for s in self.steps if s["status"] == "pending")
        all_terminal = (done + failed) == total and total > 0
        # Failed + all remaining are pending = terminal failure (cancelled/aborted).
        # Without this, a cancel during step 1 leaves overall_status="running"
        # because steps 2-4 are still "pending" (never started).
        if failed and (failed + pending) == total:
            overall = "failed"
        elif all_terminal and failed and not done:
            overall = "failed"
        elif all_terminal:
            overall = "done"
        else:
            overall = "running"
        card = {
            "title": self.title,
            "elapsed": self.elapsed,
            "overall_status": overall,
            "steps": [
                {"name": s["name"], "status": s["status"], "detail": s.get("detail", "")}
                for s in self.steps
            ],
            "footer": self._footer,
        }
        if self._keyboard is not None:
            card["buttons"] = self._keyboard_to_buttons(self._keyboard)
        return card

    def _keyboard_to_buttons(self, keyboard) -> list | None:
        """Convert ActionKeyboard to JSON-serializable button rows."""
        if not keyboard:
            return None
        buttons = []
        for row in keyboard.rows:
            row_buttons = []
            for btn in row.buttons:
                row_buttons.append({
                    "label": btn.label,
                    "action_id": btn.action_id,
                })
            buttons.append(row_buttons)
        return buttons

    async def _emit_card(self):
        try:
            await self._chat.send_progress_card(
                self._chat_id, self._message_id, self._build_card()
            )
        except Exception as e:
            log.warning("checklist_reporter.emit_card_failed",
                        message_id=self._message_id, error=str(e)[:100])

    async def finalize(self, footer: str = "", success: bool = True):
        """Mark all remaining steps done, emit final card, stop heartbeat.

        Only converts "pending" and "running" steps to done — never touches "failed"
        steps. A failed step means something genuinely didn't work; lying about it
        with a green checkmark hides real problems from the user.

        success=False: also marks the currently-running step as failed.
        """
        if footer:
            self._footer = footer
        for s in self.steps:
            if s["status"] in ("pending", "running"):
                if success:
                    s["status"] = "done"
                else:
                    s["status"] = "failed"
        if hasattr(self._chat, "send_progress_card"):
            await self._emit_card()
        await self.stop()
