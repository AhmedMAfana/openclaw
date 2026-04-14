"""Platform-agnostic action buttons and keyboards.

Every worker task, reporter, and service uses these types instead of
importing aiogram/slack-specific UI types directly.  Each ChatProvider
translates ActionKeyboard → native format (InlineKeyboardMarkup, Block Kit, etc.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openclow.models import Project


@dataclass
class ActionButton:
    """A single interactive button."""
    label: str
    action_id: str          # e.g. "approve_plan:task-uuid", "menu:main"
    url: str | None = None  # external link (PR URL, dashboard URL)
    style: str = "default"  # "default", "primary", "danger"


@dataclass
class ActionRow:
    """A horizontal row of buttons."""
    buttons: list[ActionButton] = field(default_factory=list)


@dataclass
class ActionKeyboard:
    """A collection of button rows attached to a message."""
    rows: list[ActionRow] = field(default_factory=list)


# ── Factory helpers ──────────────────────────────────────────────
# These replace all the scattered InlineKeyboardButton construction
# across worker tasks, reporters, and services.


def menu_keyboard() -> ActionKeyboard:
    """Main menu keyboard."""
    return ActionKeyboard(rows=[
        ActionRow([
            ActionButton("🚀 New Task", "menu:task"),
            ActionButton("📂 Projects", "menu:projects"),
        ]),
        ActionRow([
            ActionButton("📊 Status", "menu:status"),
            ActionButton("📋 Logs", "menu:logs"),
        ]),
        ActionRow([
            ActionButton("📈 Dashboard", "menu:dashboard"),
            ActionButton("➕ Add Project", "menu:addproject"),
        ]),
        ActionRow([
            ActionButton("⚙️ Settings", "menu:settings"),
            ActionButton("❓ Help", "menu:help"),
        ]),
    ])


def back_keyboard() -> ActionKeyboard:
    """Single 'back to main menu' button."""
    return ActionKeyboard(rows=[
        ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
    ])


def open_app_btn(project_id: int, tunnel_url: str | None = None) -> ActionButton:
    """Open App direct link if tunnel_url known, else health check action."""
    if tunnel_url:
        return ActionButton("🌐 Open App", f"open_app_link:{project_id}", url=tunnel_url, style="primary")
    return ActionButton("🌐 Open App", f"open_app:{project_id}", style="primary")


def open_app_btns(project_id: int, tunnel_url: str | None = None) -> list[ActionButton]:
    """Open App (direct) + Check & Fix (health check). Both buttons."""
    btns = []
    if tunnel_url:
        btns.append(ActionButton("🌐 Open App", f"open_app_link:{project_id}", url=tunnel_url, style="primary"))
    btns.append(ActionButton(
        "🔧 Check & Fix" if tunnel_url else "🌐 Open App",
        f"open_app:{project_id}",
    ))
    return btns


def nav_keyboard(*extra: ActionButton) -> ActionKeyboard:
    """Compact navigation: optional extra buttons + Menu. All on one row."""
    buttons = list(extra) + [ActionButton("Menu", "menu:main")]
    return ActionKeyboard(rows=[ActionRow(buttons)])


def project_nav_keyboard(project_id: int, *extra: ActionButton) -> ActionKeyboard:
    """Compact project navigation: extra buttons + Back + Menu on one row."""
    buttons = list(extra) + [
        ActionButton("Back", f"project_detail:{project_id}"),
        ActionButton("Menu", "menu:main"),
    ]
    return ActionKeyboard(rows=[ActionRow(buttons)])


def confirm_keyboard() -> ActionKeyboard:
    """Submit / Cancel for task confirmation."""
    return ActionKeyboard(rows=[
        ActionRow([
            ActionButton("Submit", "submit", style="primary"),
            ActionButton("Cancel", "cancel"),
        ]),
    ])


def review_keyboard(task_id: str) -> ActionKeyboard:
    """Create PR / Discard after diff preview."""
    return ActionKeyboard(rows=[
        ActionRow([
            ActionButton("Create PR", f"approve:{task_id}", style="primary"),
            ActionButton("Discard", f"discard:{task_id}", style="danger"),
        ]),
    ])


def plan_review_keyboard(task_id: str) -> ActionKeyboard:
    """Approve Plan / Reject after plan preview."""
    return ActionKeyboard(rows=[
        ActionRow([
            ActionButton("Approve Plan", f"approve_plan:{task_id}", style="primary"),
            ActionButton("Reject", f"discard:{task_id}", style="danger"),
        ]),
    ])


def pr_keyboard(task_id: str, pr_url: str) -> ActionKeyboard:
    """Merge / Reject / View PR after PR creation."""
    return ActionKeyboard(rows=[
        ActionRow([
            ActionButton("Merge", f"merge:{task_id}", style="primary"),
            ActionButton("Reject", f"reject:{task_id}", style="danger"),
        ]),
        ActionRow([ActionButton("View PR", f"view_pr:{task_id}", url=pr_url)]),
    ])


def project_keyboard(projects: list[Project]) -> ActionKeyboard:
    """Project selection list."""
    rows = []
    for p in projects:
        label = f"{p.name} ({p.tech_stack or ''})"
        rows.append(ActionRow([ActionButton(label, f"project:{p.id}")]))
    rows.append(ActionRow([ActionButton("Cancel", "cancel")]))
    return ActionKeyboard(rows=rows)


def project_detail_keyboard(project, is_active: bool = True, status: str = None) -> ActionKeyboard:
    """Project detail actions based on project status.

    Status: active, bootstrapping, failed, inactive.
    Falls back to is_active bool for backward compat.
    """
    if status is None:
        status = "active" if is_active else "inactive"

    if status == "active":
        rows = [
            ActionRow([
                ActionButton("💚 Health Check", f"health:{project.id}"),
                ActionButton("🔄 Bootstrap", f"project_bootstrap:{project.id}"),
            ]),
        ]
        if getattr(project, "is_dockerized", False):
            rows.append(ActionRow([
                ActionButton("▶️ Docker Up", f"project_up:{project.id}"),
                ActionButton("⏹ Docker Down", f"project_down:{project.id}"),
            ]))
        rows.extend([
            ActionRow([
                ActionButton("🔗 Unlink", f"project_unlink:{project.id}"),
                ActionButton("🗑 Remove", f"project_remove:{project.id}", style="danger"),
            ]),
            ActionRow([ActionButton("◀️ Back", "menu:projects")]),
        ])
    elif status == "bootstrapping":
        rows = [
            ActionRow([ActionButton("🔄 Bootstrap is running...", "noop")]),
            ActionRow([ActionButton("◀️ Back", "menu:projects")]),
        ]
    elif status == "failed":
        rows = [
            ActionRow([ActionButton("🔄 Retry Bootstrap", f"project_bootstrap:{project.id}")]),
            ActionRow([
                ActionButton("🔗 Unlink", f"project_unlink:{project.id}"),
                ActionButton("🗑 Remove", f"project_remove:{project.id}", style="danger"),
            ]),
            ActionRow([ActionButton("◀️ Back", "menu:projects")]),
        ]
    else:  # inactive
        rows = [
            ActionRow([ActionButton("🔗 Re-link (Bootstrap)", f"project_relink:{project.id}")]),
            ActionRow([ActionButton("🗑 Remove Permanently", f"project_remove:{project.id}", style="danger")]),
            ActionRow([ActionButton("◀️ Back", "menu:projects")]),
        ]
    return ActionKeyboard(rows=rows)


def terminal_keyboard() -> ActionKeyboard:
    """Navigation after a terminal state (task done, cancelled, etc.)."""
    return ActionKeyboard(rows=[
        ActionRow([
            ActionButton("New Task", "menu:task"),
            ActionButton("Projects", "menu:projects"),
        ]),
        ActionRow([ActionButton("Main Menu", "menu:main")]),
    ])


def dashboard_keyboard(url: str) -> ActionKeyboard:
    """Dashboard with open/refresh/stop buttons."""
    return ActionKeyboard(rows=[
        ActionRow([ActionButton("🌐 Open Dashboard", "open_dashboard", url=url)]),
        ActionRow([
            ActionButton("🔄 New Link", "menu:dashboard_refresh"),
            ActionButton("🛑 Stop Tunnel", "menu:dashboard_stop"),
        ]),
        ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
    ])


def dashboard_retry_keyboard() -> ActionKeyboard:
    """Dashboard unavailable — retry or force start."""
    return ActionKeyboard(rows=[
        ActionRow([ActionButton("🔄 Retry", "menu:dashboard")]),
        ActionRow([ActionButton("🔧 Force Start", "menu:dashboard_refresh")]),
        ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
    ])


def settings_keyboard(settings_url: str, wizard_url: str) -> ActionKeyboard:
    """Settings dashboard with open/refresh/stop buttons."""
    return ActionKeyboard(rows=[
        ActionRow([ActionButton("⚙️ Open Settings", "open_settings", url=settings_url)]),
        ActionRow([ActionButton("🧙 Setup Wizard", "open_wizard", url=wizard_url)]),
        ActionRow([
            ActionButton("🔄 New Link", "menu:settings_refresh"),
            ActionButton("🛑 Stop Tunnel", "menu:settings_stop"),
        ]),
        ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
    ])


def settings_retry_keyboard() -> ActionKeyboard:
    """Settings unavailable — retry or force start."""
    return ActionKeyboard(rows=[
        ActionRow([ActionButton("🔄 Retry", "menu:settings")]),
        ActionRow([ActionButton("🔧 Force Start", "menu:settings_refresh")]),
        ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
    ])
