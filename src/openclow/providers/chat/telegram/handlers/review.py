"""Review handler — approve, discard, merge, reject callbacks.

Includes double-click guard: checks task status before enqueuing
to prevent duplicate job submissions from rapid button clicks.
"""
import asyncio
import uuid

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from openclow.providers.chat.telegram.handlers.start import back_to_menu_keyboard
from openclow.models import Task, async_session
from openclow.utils.logging import get_logger

router = Router()
log = get_logger()


async def _get_task_status(task_id: str) -> str | None:
    """Read current task status from DB. Returns None if not found."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Task.status).where(Task.id == uuid.UUID(task_id))
            )
            row = result.one_or_none()
            return row[0] if row else None
    except Exception:
        return None


# Expected status(es) for each action — prevents duplicate/out-of-order execution
_EXPECTED_STATUS: dict[str, set[str]] = {
    "approve_plan": {"plan_review"},
    "approve": {"diff_preview"},
    "discard": {"diff_preview", "plan_review"},  # Can reject from plan OR diff
    "merge": {"awaiting_approval"},
    "reject": {"awaiting_approval"},
}


async def _guard_and_enqueue(
    callback: CallbackQuery,
    action: str,
    task_id: str,
    job_name: str,
    progress_text: str,
):
    """Shared logic: validate status, enqueue job, handle errors."""
    expected = _EXPECTED_STATUS.get(action)
    current_status = await _get_task_status(task_id)

    if expected and current_status not in expected:
        log.warning("review.wrong_status", action=action,
                    task_id=task_id, expected=expected, actual=current_status)
        await callback.answer(
            f"This task is already being processed ({current_status or 'unknown'}).",
            show_alert=True,
        )
        return

    await callback.message.edit_text(progress_text)

    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(job_name, task_id)
    except Exception as e:
        log.error(f"review.{action}_failed", task_id=task_id, error=str(e))
        await callback.message.edit_text(
            f"Failed: {e}",
            reply_markup=back_to_menu_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("approve_plan:"))
async def approve_plan(callback: CallbackQuery):
    """User clicked [Approve Plan] — start coding."""
    task_id = callback.data.split(":", 1)[1]
    log.info("review.approve_plan", task_id=task_id)
    await _guard_and_enqueue(callback, "approve_plan", task_id,
                             "execute_plan", "Plan approved! Starting implementation...")


@router.callback_query(F.data.startswith("approve:"))
async def approve_changes(callback: CallbackQuery):
    """User clicked [Create PR] — dispatch PR creation."""
    task_id = callback.data.split(":", 1)[1]
    log.info("review.approve", task_id=task_id)
    await _guard_and_enqueue(callback, "approve", task_id,
                             "approve_task", "Creating PR...")


@router.callback_query(F.data.startswith("discard:"))
async def discard_changes(callback: CallbackQuery):
    """User clicked [Discard] — dispatch cleanup to worker."""
    task_id = callback.data.split(":", 1)[1]
    log.info("review.discard", task_id=task_id)
    await _guard_and_enqueue(callback, "discard", task_id,
                             "discard_task", "Discarding changes...")


@router.callback_query(F.data.startswith("merge:"))
async def merge_pr(callback: CallbackQuery):
    """User clicked [Merge] — dispatch merge."""
    task_id = callback.data.split(":", 1)[1]
    log.info("review.merge", task_id=task_id)
    await _guard_and_enqueue(callback, "merge", task_id,
                             "merge_task", "Merging PR...")


@router.callback_query(F.data.startswith("reject:"))
async def reject_pr(callback: CallbackQuery):
    """User clicked [Reject] — dispatch rejection."""
    task_id = callback.data.split(":", 1)[1]
    log.info("review.reject", task_id=task_id)
    await _guard_and_enqueue(callback, "reject", task_id,
                             "reject_task", "Rejecting and cleaning up...")
