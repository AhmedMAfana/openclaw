"""Main orchestrator pipeline — the heart of OpenClow.

Interactive flow:
1. Analyze project → create plan → send to user for approval
2. User approves plan → agent codes step by step with progress updates
3. Reviewer checks quality → fixes if needed
4. Send summary + diff → user approves → create PR
"""
import asyncio
import re
import time
import uuid

from slugify import slugify
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from openclow.models import Task, TaskLog, async_session
from openclow.providers import factory
from openclow.services.workspace_service import WorkspaceService
from openclow.utils.logging import get_logger
from openclow.worker.tasks import git_ops

log = get_logger()


# Valid status transitions — used to guard against out-of-order execution
_VALID_ENTRY_STATUS = {
    "execute_task": {"pending", "preparing"},
    "execute_plan": {"plan_review"},
    "approve_task": {"diff_preview"},
    "merge_task": {"awaiting_approval"},
    "reject_task": {"awaiting_approval"},
    "discard_task": {"diff_preview", "plan_review"},
}


async def _get_task(task_id: str) -> Task:
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .options(selectinload(Task.project), selectinload(Task.user))
            .where(Task.id == uuid.UUID(task_id))
        )
        task = result.scalar_one_or_none()
        if not task:
            raise ValueError(f"Task {task_id} not found")
        # Expunge so the object can be used after session closes
        session.expunge(task)
        return task


async def _update_task(task_id: str, **kwargs):
    async with async_session() as session:
        await session.execute(
            update(Task).where(Task.id == uuid.UUID(task_id)).values(**kwargs)
        )
        await session.commit()


async def _log_to_db(task_id: str, agent: str, level: str, message: str, metadata: dict | None = None):
    async with async_session() as session:
        entry = TaskLog(
            task_id=uuid.UUID(task_id),
            agent=agent, level=level, message=message, metadata_=metadata,
        )
        session.add(entry)
        await session.commit()


def _parse_plan_steps(plan_text: str) -> list[str]:
    """Extract numbered steps from plan text."""
    steps = []
    for line in plan_text.split("\n"):
        line = line.strip()
        match = re.match(r"^\d+[\.\)]\s+(.+)$", line)
        if match:
            steps.append(match.group(1))
    return steps


def _extract_summary(agent_output: str) -> str:
    """Extract DONE_SUMMARY from agent output."""
    if "DONE_SUMMARY:" in agent_output:
        parts = agent_output.split("DONE_SUMMARY:", 1)
        return parts[1].strip()[:2000]
    return ""


def _main_menu_keyboard():
    """Rich next-action keyboard for terminal states."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="New Task", callback_data="menu:task"),
            InlineKeyboardButton(text="Projects", callback_data="menu:projects"),
        ],
        [InlineKeyboardButton(text="Main Menu", callback_data="menu:main")],
    ])


async def execute_task(ctx: dict, task_id: str):
    """Phase 1: Analyze project and create plan, send to user for approval."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)
    start_time = time.time()

    llm = await factory.get_llm()
    chat = await factory.get_chat()
    ws = WorkspaceService()

    # ── Acquire project lock (prevent concurrent tasks on same repo) ──
    from openclow.services.project_lock import acquire_project_lock, get_lock_holder
    lock = await acquire_project_lock(task.project_id, task_id=task_id_str, wait=10)
    if lock is None:
        holder = await get_lock_holder(task.project_id)
        await _update_task(task_id_str, status="failed",
                           error_message=f"Project busy — another task is running ({holder})")
        await chat.edit_message(task.chat_id, task.chat_message_id,
                                f"Project is busy. Another task ({holder or 'unknown'}) is already running.\n"
                                f"Wait for it to finish or use /cancel.")
        await chat.close()
        return

    log.info("orchestrator.started", task_id=task_id_str, project=task.project.name)

    from openclow.services.status_reporter import StatusReporter
    reporter = StatusReporter(chat, task.chat_id, task.chat_message_id,
                              title=f"Planning: {task.description[:40]}")
    await reporter.start()

    try:
        # ── Step 1: Prepare workspace ──
        await _update_task(task_id_str, status="preparing")
        await reporter.stage("Preparing workspace", step=1, total=3)

        workspace = await ws.prepare(task.project, task_id_str)
        await reporter.log(f"Workspace ready")

        # Create branch
        branch_slug = slugify(task.description, max_length=50)
        branch_name = f"openclow/{task_id_str[:8]}-{branch_slug}"
        await git_ops.create_branch(workspace.path, branch_name)
        await _update_task(task_id_str, branch_name=branch_name)
        await reporter.log(f"Branch: {branch_name[:30]}")

        await _log_to_db(task_id_str, "system", "info", f"Branch: {branch_name}")

        # ── Step 2: Analyze and create plan ──
        await _update_task(task_id_str, status="planning")
        await reporter.stage("Analyzing project + creating plan", step=2, total=3)

        plan_text = await llm.run_planner(
            workspace_path=workspace.path,
            task_description=task.description,
            project_name=task.project.name,
            tech_stack=task.project.tech_stack or "",
            description=task.project.description or "",
            agent_system_prompt=task.project.agent_system_prompt or "",
        )
        await reporter.log("Plan created")

        await _log_to_db(task_id_str, "planner", "info", "Plan created", {
            "plan": plan_text[:3000],
        })

        # ── Step 3: Send plan to user for approval ──
        await reporter.stop()
        await _update_task(task_id_str, status="plan_review")
        await chat.send_plan_preview(
            task.chat_id, task.chat_message_id, task_id_str, plan_text,
        )
        # Pipeline pauses here — continues when user clicks [Approve Plan]

    except (asyncio.CancelledError, TimeoutError) as e:
        error_msg = "Task timed out" if isinstance(e, TimeoutError) else "Task was cancelled"
        log.warning("orchestrator.planning_interrupted", task_id=task_id_str, reason=error_msg)
        await _update_task(task_id_str, status="failed",
                           error_message=error_msg,
                           duration_seconds=int(time.time() - start_time))
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            await reporter.error(
                f"{error_msg}. You can retry.",
                keyboard=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Retry", callback_data="menu:task")],
                    [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
                ]),
            )
        except Exception:
            pass
        try:
            await ws.cleanup(task_id_str)
        except Exception:
            pass
    except Exception as e:
        duration = int(time.time() - start_time)
        log.error("orchestrator.planning_failed", task_id=task_id_str, error=str(e))
        await _update_task(task_id_str, status="failed",
                           error_message=str(e), duration_seconds=duration)
        await reporter.error(str(e)[:200], keyboard=_main_menu_keyboard())
        try:
            await ws.cleanup(task_id_str)
        except Exception as cleanup_err:
            log.error("orchestrator.cleanup_failed", task_id=task_id_str, error=str(cleanup_err))
    finally:
        await reporter.stop()
        # Release lock — user is now reviewing the plan (no repo access needed)
        if lock:
            await lock.release()
        await chat.close()


async def execute_plan(ctx: dict, task_id: str):
    """Phase 2: User approved plan → code it, review it, send summary."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)

    # Guard: only proceed if task is in the right state
    valid = _VALID_ENTRY_STATUS.get("execute_plan", set())
    if task.status not in valid:
        log.warning("orchestrator.invalid_status", task_id=task_id_str,
                    expected=valid, actual=task.status)
        return

    start_time = time.time()

    llm = await factory.get_llm()
    chat = await factory.get_chat()
    ws = WorkspaceService()

    # Re-acquire project lock for coding phase
    from openclow.services.project_lock import acquire_project_lock, get_lock_holder
    lock = await acquire_project_lock(task.project_id, task_id=task_id_str, wait=10)
    if lock is None:
        holder = await get_lock_holder(task.project_id)
        await _update_task(task_id_str, status="failed",
                           error_message=f"Project busy — lock held by {holder}")
        await chat.edit_message(task.chat_id, task.chat_message_id,
                                f"Cannot start coding — project is locked by another task ({holder}).")
        await chat.close()
        return

    workspace_path = ws.get_path(task_id_str)

    # Get the plan from task_logs
    plan_text = ""
    async with async_session() as session:
        result = await session.execute(
            select(TaskLog).where(
                TaskLog.task_id == uuid.UUID(task_id_str),
                TaskLog.agent == "planner",
            ).order_by(TaskLog.created_at.desc()).limit(1)
        )
        plan_log = result.scalar_one_or_none()
        if plan_log and plan_log.metadata_:
            plan_text = plan_log.metadata_.get("plan", "")

    plan_steps = _parse_plan_steps(plan_text)
    total_steps = len(plan_steps) or 5

    from openclow.services.status_reporter import StatusReporter
    reporter = StatusReporter(chat, task.chat_id, task.chat_message_id,
                              title=f"Coding: {task.description[:40]}")
    await reporter.start()

    try:
        # ── Step 1: Run Coder Agent with plan ──
        await _update_task(task_id_str, status="coding")
        await reporter.stage("Implementing plan", step=0, total=total_steps)

        turn_count = 0
        current_step = 0
        last_diff_size = 0
        stall_count = 0
        full_output = ""

        async for message in llm.run_coder(
            workspace_path=workspace_path,
            task_description=task.description,
            project_name=task.project.name,
            tech_stack=task.project.tech_stack or "",
            description=task.project.description or "",
            agent_system_prompt=task.project.agent_system_prompt or "",
            max_turns=0,
            plan=plan_text,
        ):
            turn_count += 1

            # Track agent text output for step detection
            from claude_agent_sdk.types import AssistantMessage, TextBlock
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text
                        # Detect STEP_DONE markers
                        if "STEP_DONE:" in block.text:
                            current_step += 1
                            step_desc = block.text.split("STEP_DONE:", 1)[1].strip().split("\n")[0]
                            await reporter.stage(
                                step_desc[:50],
                                step=min(current_step, total_steps),
                                total=total_steps,
                            )

            # Tool use progress
            tool_name = llm.is_tool_use(message)
            if tool_name:
                await reporter.log(tool_name)

            # Stall detection
            if turn_count % 10 == 0:
                diff_size = await git_ops.diff_size(workspace_path)
                if diff_size == last_diff_size:
                    stall_count += 1
                    if stall_count >= 2:
                        raise RuntimeError("Agent stalled — no progress for 20 turns")
                else:
                    stall_count = 0
                    last_diff_size = diff_size

            result_turns = llm.is_result(message)
            if result_turns is not None:
                turn_count = result_turns

        await _log_to_db(task_id_str, "coder", "info",
                         f"Coding complete. Turns: {turn_count}")
        await _update_task(task_id_str, agent_turns=turn_count)

        # ── Step 2: Run Reviewer ──
        await _update_task(task_id_str, status="reviewing")
        await reporter.stage("Reviewing changes for quality & security")

        review_result = await llm.run_reviewer(
            workspace_path=workspace_path,
            task_description=task.description,
            project_name=task.project.name,
            tech_stack=task.project.tech_stack or "",
            max_turns=0,
        )
        await _log_to_db(task_id_str, "reviewer", "info",
                         f"Review: {'ISSUES' if review_result.has_issues else 'APPROVED'}")

        # Fix loop
        if review_result.has_issues:
            for retry in range(2):
                await reporter.stage(f"Fixing review issues (attempt {retry + 1})")
                async for _ in llm.run_coder_fix(
                    workspace_path=workspace_path,
                    task_description=task.description,
                    project_name=task.project.name,
                    tech_stack=task.project.tech_stack or "",
                    description=task.project.description or "",
                    agent_system_prompt=task.project.agent_system_prompt or "",
                    issues=review_result.issues,
                    max_turns=10,  # Fixes should be quick
                ):
                    pass
                review_result = await llm.run_reviewer(
                    workspace_path=workspace_path,
                    task_description=task.description,
                    project_name=task.project.name,
                    tech_stack=task.project.tech_stack or "",
                    max_turns=0,
                )
                if not review_result.has_issues:
                    break

        # ── Step 3: Stage changes + send summary ──
        await git_ops.add_all(workspace_path)
        diff_summary = await git_ops.diff_stat(workspace_path)

        if not diff_summary.strip():
            await _update_task(task_id_str, status="failed",
                               error_message="Agent made no changes")
            await reporter.error("Agent finished but made no changes.", keyboard=_main_menu_keyboard())
            return

        duration = int(time.time() - start_time)
        await _update_task(task_id_str, status="diff_preview", duration_seconds=duration)

        # Extract summary from agent output
        summary = _extract_summary(full_output)
        if not summary:
            summary = f"Task completed in {turn_count} turns, {duration}s"

        await reporter.stop()
        await chat.send_summary(
            task.chat_id, task.chat_message_id, task_id_str,
            summary, diff_summary,
        )
        await _log_to_db(task_id_str, "system", "info",
                         f"Summary sent. Duration: {duration}s")

    except (asyncio.CancelledError, TimeoutError) as e:
        error_msg = "Task timed out" if isinstance(e, TimeoutError) else "Task was cancelled"
        log.warning("orchestrator.coding_interrupted", task_id=task_id_str, reason=error_msg)
        await _update_task(task_id_str, status="failed",
                           error_message=error_msg,
                           duration_seconds=int(time.time() - start_time))
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            await reporter.error(
                f"{error_msg}. You can retry.",
                keyboard=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Retry", callback_data="menu:task")],
                    [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
                ]),
            )
        except Exception:
            pass
    except Exception as e:
        duration = int(time.time() - start_time)
        log.error("orchestrator.coding_failed", task_id=task_id_str, error=str(e))
        await _update_task(task_id_str, status="failed",
                           error_message=str(e), duration_seconds=duration)
        await reporter.error(str(e)[:200], keyboard=_main_menu_keyboard())
        try:
            git_status = await git_ops.status(workspace_path)
            await _log_to_db(task_id_str, "system", "error", str(e),
                             {"git_status": git_status})
        except Exception:
            await _log_to_db(task_id_str, "system", "error", str(e))
        try:
            await ws.cleanup(task_id_str)
        except Exception as cleanup_err:
            log.error("orchestrator.cleanup_failed", task_id=task_id_str, error=str(cleanup_err))
    finally:
        await reporter.stop()
        if lock:
            await lock.release()
        await chat.close()


async def approve_task(ctx: dict, task_id: str):
    """User clicked [Create PR] — push and create PR."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)

    chat = await factory.get_chat()
    git = await factory.get_git()
    ws = WorkspaceService()

    from openclow.services.status_reporter import StatusReporter
    reporter = StatusReporter(chat, task.chat_id, task.chat_message_id, title="Creating PR")
    await reporter.start()

    try:
        workspace = ws.get_path(task_id_str)

        await _update_task(task_id_str, status="pushing")
        await reporter.stage("Pushing changes")

        await git_ops.commit_and_push(workspace, task.branch_name,
                                       f"feat: {task.description[:72]}")
        await reporter.log(f"Pushed to {task.branch_name[:30]}")

        await reporter.stage("Creating pull request")
        pr_url, pr_number = await git.create_pr(
            repo=task.project.github_repo,
            branch=task.branch_name,
            base=task.project.default_branch,
            title=f"[OpenClow] {task.description[:60]}",
            body=git.generate_pr_body(task),
        )
        await reporter.log(f"PR #{pr_number} created")

        await _update_task(task_id_str, status="awaiting_approval",
                           pr_url=pr_url, pr_number=pr_number)
        await reporter.stop()
        await chat.send_pr_created(task.chat_id, task.chat_message_id, task_id_str, pr_url)
        await _log_to_db(task_id_str, "system", "info", f"PR created: {pr_url}")

    except Exception as e:
        log.error("approve.failed", task_id=task_id_str, error=str(e))
        await _update_task(task_id_str, status="failed", error_message=str(e))
        await reporter.error(f"PR creation failed: {str(e)[:200]}", keyboard=_main_menu_keyboard())
    finally:
        await reporter.stop()
        await chat.close()


async def merge_task(ctx: dict, task_id: str):
    """User clicked [Merge]."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)
    chat = await factory.get_chat()
    git = await factory.get_git()

    from openclow.services.status_reporter import StatusReporter
    reporter = StatusReporter(chat, task.chat_id, task.chat_message_id, title="Merging")
    await reporter.start()

    try:
        await reporter.stage("Merging PR")
        await git.merge_pr(task.project.github_repo, task.pr_number)
        await reporter.log(f"PR #{task.pr_number} merged")

        await _update_task(task_id_str, status="merged")
        await _log_to_db(task_id_str, "system", "info", "PR merged")

        await reporter.stage("Cleaning up workspace")
        await WorkspaceService().cleanup(task_id_str)
        await reporter.log("Workspace cleaned")

        await reporter.complete(
            f"PR #{task.pr_number} merged and live!",
            keyboard=_main_menu_keyboard(),
        )
    except Exception as e:
        log.error("merge.failed", task_id=task_id_str, error=str(e))
        await reporter.error(f"Merge failed: {str(e)[:200]}", keyboard=_main_menu_keyboard())
    finally:
        await reporter.stop()
        await chat.close()


async def reject_task(ctx: dict, task_id: str):
    """User clicked [Reject]."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)
    chat = await factory.get_chat()
    git = await factory.get_git()

    from openclow.services.status_reporter import StatusReporter
    reporter = StatusReporter(chat, task.chat_id, task.chat_message_id, title="Rejecting")
    await reporter.start()

    try:
        if task.pr_number:
            await reporter.stage("Closing PR")
            await git.close_pr(task.project.github_repo, task.pr_number)
            await reporter.log(f"PR #{task.pr_number} closed")

        if task.branch_name:
            await reporter.stage("Deleting branch")
            await git.delete_branch(task.project.github_repo, task.branch_name)
            await reporter.log(f"Branch deleted")

        await reporter.stage("Cleaning up workspace")
        await _update_task(task_id_str, status="rejected")
        await _log_to_db(task_id_str, "system", "info", "Task rejected")
        await WorkspaceService().cleanup(task_id_str)
        await reporter.log("Workspace cleaned")

        await reporter.complete("Task rejected. PR closed.", keyboard=_main_menu_keyboard())
    except Exception as e:
        log.error("reject.failed", task_id=task_id_str, error=str(e))
        await reporter.error(f"Reject failed: {str(e)[:200]}", keyboard=_main_menu_keyboard())
    finally:
        await reporter.stop()
        await chat.close()


async def discard_task(ctx: dict, task_id: str):
    """User clicked [Discard] — clean up workspace and branch."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)
    chat = await factory.get_chat()
    git = await factory.get_git()
    ws = WorkspaceService()

    from openclow.services.status_reporter import StatusReporter
    reporter = StatusReporter(chat, task.chat_id, task.chat_message_id, title="Discarding")
    await reporter.start()

    try:
        if task.branch_name:
            await reporter.stage("Deleting branch")
            await git.delete_branch(task.project.github_repo, task.branch_name)
            await reporter.log(f"Branch {task.branch_name[:30]} deleted")

        await reporter.stage("Removing workspace")
        await ws.cleanup(task_id_str, task.project.name)
        await reporter.log("Workspace cleaned")

        await _update_task(task_id_str, status="discarded")
        await _log_to_db(task_id_str, "system", "info", "Task discarded by user")

        await reporter.complete("Changes discarded. Ready for next task!", keyboard=_main_menu_keyboard())
    except Exception as e:
        log.error("discard.failed", task_id=task_id_str, error=str(e))
        await reporter.error(f"Discard failed: {str(e)[:200]}", keyboard=_main_menu_keyboard())
    finally:
        await reporter.stop()
        await chat.close()
