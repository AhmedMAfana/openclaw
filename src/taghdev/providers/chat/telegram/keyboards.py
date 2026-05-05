"""Inline keyboard builders for Telegram bot."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from taghdev.models import Project


def project_keyboard(projects: list[Project]) -> InlineKeyboardMarkup:
    """Build project selection keyboard."""
    buttons = []
    for project in projects:
        buttons.append([
            InlineKeyboardButton(
                text=f"{project.name} ({project.tech_stack or ''})",
                callback_data=f"project:{project.id}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Build task confirmation keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Submit", callback_data="submit"),
            InlineKeyboardButton(text="Cancel", callback_data="cancel"),
        ]
    ])


def review_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """Build diff review keyboard (after agent finishes)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Create PR", callback_data=f"approve:{task_id}"),
            InlineKeyboardButton(text="Discard", callback_data=f"discard:{task_id}"),
        ]
    ])


def projects_keyboard(projects: list[Project]) -> InlineKeyboardMarkup:
    """Build project list keyboard with health check buttons."""
    buttons = []
    for p in projects:
        label = f"{p.name} — {p.tech_stack or 'N/A'}"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"health:{p.id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def pr_keyboard(task_id: str, pr_url: str) -> InlineKeyboardMarkup:
    """Build PR action keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Merge", callback_data=f"merge:{task_id}"),
            InlineKeyboardButton(text="Reject", callback_data=f"reject:{task_id}"),
        ],
        [
            InlineKeyboardButton(text="View PR", url=pr_url),
        ],
    ])
