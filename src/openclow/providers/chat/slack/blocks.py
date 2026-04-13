"""Slack Block Kit builders — rich UI components for the OpenClow Slack app.

All block construction is centralized here. Handlers never build raw block
dicts inline — they call these builders instead.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openclow.providers.actions import ActionKeyboard


# ── Core Block Primitives ────────────────────────────────────────────

def header_block(text: str) -> dict:
    """Header block — large bold text."""
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150]}}


def section_block(text: str, accessory: dict | None = None) -> dict:
    """Section block with optional accessory (button, image, overflow)."""
    block: dict = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text[:3000]},
    }
    if accessory:
        block["accessory"] = accessory
    return block


def fields_section(field_pairs: list[tuple[str, str]]) -> dict:
    """Section block with two-column field layout."""
    fields = []
    for label, value in field_pairs[:10]:  # Slack max 10 fields
        fields.append({"type": "mrkdwn", "text": f"*{label}*\n{value}"})
    return {"type": "section", "fields": fields}


def context_block(elements: list[str]) -> dict:
    """Context block — small muted text elements."""
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": e[:3000]} for e in elements[:10]],
    }


def divider() -> dict:
    return {"type": "divider"}


def actions_block(elements: list[dict], block_id: str | None = None) -> dict:
    """Actions block with interactive elements."""
    block: dict = {"type": "actions", "elements": elements[:10]}
    if block_id:
        block["block_id"] = block_id
    return block


def button_element(
    text: str,
    action_id: str,
    value: str | None = None,
    style: str | None = None,
    url: str | None = None,
) -> dict:
    """Single button element for use inside actions blocks."""
    btn: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": text[:75]},
        "action_id": action_id,
    }
    if value:
        btn["value"] = value
    if style in ("primary", "danger"):
        btn["style"] = style
    if url:
        btn["url"] = url
    return btn


def overflow_element(options: list[tuple[str, str]], action_id: str = "overflow") -> dict:
    """Compact '...' menu for secondary actions. options = [(label, value), ...]."""
    return {
        "type": "overflow",
        "action_id": action_id,
        "options": [
            {"text": {"type": "plain_text", "text": label[:75]}, "value": value}
            for label, value in options[:5]  # Slack max 5 overflow options
        ],
    }


def image_block(url: str, alt: str = "image") -> dict:
    return {"type": "image", "image_url": url, "alt_text": alt}


# ── Keyboard Translation (ActionKeyboard → Blocks) ──────────────────

# Admin action IDs stripped from all Slack responses (employee mode).
# Dev mode welcome menu adds admin buttons through a separate code path.
_ADMIN_ACTION_IDS = frozenset({
    "menu:projects",
    "menu:addproject",
    "menu:logs",
    "menu:dashboard",
    "menu:settings",
})


def translate_keyboard(keyboard: ActionKeyboard | None) -> list[dict]:
    """Convert ActionKeyboard → Slack Block Kit action blocks.

    Automatically strips admin-only buttons so worker tasks
    don't need to know about Slack's user/dev mode.
    """
    if keyboard is None:
        return []

    blocks: list[dict] = []
    for row in keyboard.rows:
        elements = []
        for btn in row.buttons:
            if btn.action_id in _ADMIN_ACTION_IDS:
                continue
            if btn.url:
                elements.append(button_element(
                    btn.label, btn.action_id,
                    value=btn.action_id, url=btn.url,
                ))
            else:
                element = button_element(
                    btn.label, btn.action_id,
                    value=btn.action_id,
                    style=btn.style if btn.style != "default" else None,
                )
                elements.append(element)
        if elements:
            blocks.append(actions_block(elements))
    return blocks


def build_message_blocks(
    text: str,
    keyboard: ActionKeyboard | None = None,
) -> list[dict]:
    """Build a full Slack message payload as blocks (backward compat)."""
    blocks = [section_block(text)]
    blocks.extend(translate_keyboard(keyboard))
    return blocks


# ── StatusReporter Rich Blocks ───────────────────────────────────────

_STATUS_PATTERN = re.compile(r"^[🔄⏳] (.+) \((\d+)s\)$")


def status_update_blocks(text: str) -> list[dict] | None:
    """Parse StatusReporter text into rich Block Kit blocks.

    Returns None if the text doesn't match the expected format,
    so callers fall back to generic rendering.
    """
    lines = text.split("\n")
    if not lines:
        return None

    match = _STATUS_PATTERN.match(lines[0])
    if not match:
        return None

    title = match.group(1)
    elapsed = match.group(2)

    progress_line = None
    stage = None
    log_lines: list[str] = []

    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and "]" in stripped and ("█" in stripped or "░" in stripped):
            progress_line = stripped
        elif stripped.startswith("▸"):
            log_lines.append(stripped[2:].strip())
        elif stage is None:
            stage = stripped

    blks: list[dict] = []

    # Title row
    blks.append(section_block(f":gear: *{title}*  —  `{elapsed}s`"))

    # Progress bar
    if progress_line:
        blks.append(section_block(f"`{progress_line}`"))

    # Stage
    if stage:
        blks.append(context_block([f":arrow_right: {stage}"]))

    # Live log lines
    if log_lines:
        blks.append(context_block([f":small_blue_diamond: {l}" for l in log_lines]))

    return blks


# ── Status Icons ─────────────────────────────────────────────────────

STATUS_ICONS = {
    "pending": ":hourglass_flowing_sand:",
    "preparing": ":wrench:",
    "planning": ":brain:",
    "plan_review": ":scroll:",
    "coding": ":computer:",
    "reviewing": ":mag:",
    "diff_preview": ":page_facing_up:",
    "awaiting_approval": ":raised_hand:",
    "pushing": ":outbox_tray:",
    "merged": ":white_check_mark:",
    "failed": ":x:",
}

PROJECT_STATUS_ICONS = {
    "active": ":large_green_circle:",
    "bootstrapping": ":arrows_counterclockwise:",
    "failed": ":red_circle:",
    "inactive": ":white_circle:",
}

PROJECT_STATUS_LABELS = {
    "active": "Ready",
    "bootstrapping": "Bootstrapping...",
    "failed": "Setup Failed",
    "inactive": "Unlinked",
}


# ── Composite Builders — Views ───────────────────────────────────────

def welcome_blocks(project_name: str | None = None, dev_mode: bool = False) -> list[dict]:
    """Welcome / main menu — employee mode by default, full admin if dev_mode."""
    if project_name:
        text = (
            f":wave: *Welcome to OpenClow*\n"
            f"This channel is linked to *{project_name}*. "
            "I can help you build features, fix bugs, and deploy code."
        )
    else:
        text = (
            ":wave: *Welcome to OpenClow*\n"
            "Your AI-powered development assistant is ready. "
            "I can help you build features, fix bugs, and deploy code."
        )

    if dev_mode:
        text += "\n:unlock: _Developer mode active_"

    blks = [
        section_block(text),
        context_block([
            "Use the buttons below or type `/oc-help` for commands."
        ]),
        actions_block([
            button_element("🚀 New Task", "menu:task", value="menu:task", style="primary"),
            button_element("📊 Status", "menu:status", value="menu:status"),
            button_element("❓ Help", "menu:help", value="menu:help"),
        ]),
    ]

    if dev_mode:
        blks.append(actions_block([
            button_element("📂 Projects", "menu:projects", value="menu:projects"),
            button_element("➕ Add Project", "menu:addproject", value="menu:addproject"),
            button_element("📋 Logs", "menu:logs", value="menu:logs"),
            button_element("📈 Dashboard", "menu:dashboard", value="menu:dashboard"),
            button_element("⚙️ Settings", "menu:settings", value="menu:settings"),
        ]))

    return blks


def help_blocks() -> list[dict]:
    """Help view — employee-level commands only."""
    return [
        header_block("📖 Command Reference"),
        section_block(
            "*Task Management*\n"
            "• `/oc-task` — Submit a new development task\n"
            "• `/oc-status` — View active tasks and progress\n"
            "• `/oc-cancel` — Cancel a running task"
        ),
        section_block(
            "*Other*\n"
            "• `/oc-qa [smoke|full]` — Run automated tests\n"
            "• `/oc-help` — Show this help"
        ),
        context_block([
            ":bulb: *Tip:* @mention me in a channel or DM me directly for AI-powered chat"
        ]),
        actions_block([
            button_element("🚀 New Task", "menu:task", value="menu:task", style="primary"),
            button_element("📊 Status", "menu:status", value="menu:status"),
            button_element("📋 Main Menu", "menu:main", value="menu:main"),
        ]),
    ]


# ── Project Views ────────────────────────────────────────────────────

def project_card(project: Any) -> list[dict]:
    """Single project card — clean, mobile-friendly."""
    status = getattr(project, "status", "active")
    icon = PROJECT_STATUS_ICONS.get(status, ":white_circle:")
    label = PROJECT_STATUS_LABELS.get(status, status)
    stack = getattr(project, "tech_stack", None) or ""
    # Shorten tech stack for mobile — just first 3 items
    if stack:
        items = [s.strip() for s in stack.split(",")]
        stack_short = ", ".join(items[:3])
        if len(items) > 3:
            stack_short += f" +{len(items) - 3}"
    else:
        stack_short = ""

    text = f"{icon} *{project.name}* — {label}"
    if stack_short:
        text += f"\n{stack_short}"

    return [
        section_block(
            text,
            accessory=button_element(
                "View", f"project_detail:{project.id}",
                value=f"project_detail:{project.id}",
            ),
        ),
    ]


def project_list_blocks(projects: list) -> list[dict]:
    """Project list — professional view with clear actions."""
    if not projects:
        return [
            header_block("📂 Projects"),
            section_block(
                "No projects linked to this channel yet.\n\n"
                "Ask an admin to link a project via Telegram or the Settings Dashboard."
            ),
            actions_block([
                button_element("📋 Main Menu", "menu:main", value="menu:main"),
            ]),
        ]

    blks: list[dict] = [
        header_block(f"📂 Projects ({len(projects)})"),
        context_block(["Tap a project to view details."]),
    ]
    for p in projects[:8]:
        blks.extend(project_card(p))
    blks.append(actions_block([
        button_element("📋 Main Menu", "menu:main", value="menu:main"),
    ]))
    return blks


def project_detail_blocks(project: Any, tunnel_url: str | None = None) -> list[dict]:
    """Project detail view — clean, mobile-friendly."""
    status = getattr(project, "status", "active")
    icon = PROJECT_STATUS_ICONS.get(status, ":white_circle:")
    label = PROJECT_STATUS_LABELS.get(status, status)
    pid = project.id
    docker = getattr(project, "is_dockerized", False)
    stack = getattr(project, "tech_stack", None) or ""

    # Shorten tech stack
    if stack:
        items = [s.strip() for s in stack.split(",")]
        stack_short = ", ".join(items[:3])
        if len(items) > 3:
            stack_short += f" +{len(items) - 3}"
    else:
        stack_short = ""

    # Header
    text = f"{icon} *{project.name}* — {label}"
    if stack_short:
        text += f"\n{stack_short}"
    if project.github_repo:
        text += f"\n:link: {project.github_repo}"

    # No overflow menu — admin actions are managed via Telegram / Settings Dashboard.
    blks: list[dict] = [section_block(text)]

    # Description (short)
    desc = getattr(project, "description", None)
    if desc:
        blks.append(context_block([desc[:150]]))

    # Primary buttons — row 1: Open App + Chat with Agent
    if status == "active":
        row1 = []
        if tunnel_url:
            row1.append(button_element("Open App", f"open_app:{pid}", value=f"open_app:{pid}", url=tunnel_url))
        row1.append(button_element("Chat with Agent", f"agent_diagnose:{pid}", value=f"agent_diagnose:{pid}", style="primary"))
        blks.append(actions_block(row1))

        # Row 2: Task + Health
        blks.append(actions_block([
            button_element("New Task", f"task_for:{pid}", value=f"task_for:{pid}"),
            button_element("Health", f"health:{pid}", value=f"health:{pid}"),
            button_element("Back", "menu:projects", value="menu:projects"),
        ]))
    elif status == "failed":
        blks.append(actions_block([
            button_element("Chat with Agent", f"agent_diagnose:{pid}", value=f"agent_diagnose:{pid}", style="primary"),
            button_element("Retry", f"project_bootstrap:{pid}", value=f"project_bootstrap:{pid}"),
            button_element("Back", "menu:projects", value="menu:projects"),
        ]))
    elif status == "inactive":
        blks.append(actions_block([
            button_element("Re-link", f"project_relink:{pid}", value=f"project_relink:{pid}", style="primary"),
            button_element("Back", "menu:projects", value="menu:projects"),
        ]))
    elif status == "bootstrapping":
        blks.append(context_block([":hourglass_flowing_sand: Bootstrap is running..."]))
        blks.append(actions_block([
            button_element("Chat with Agent", f"agent_diagnose:{pid}", value=f"agent_diagnose:{pid}"),
            button_element("Back", "menu:projects", value="menu:projects"),
        ]))

    return blks


# ── Task Views ───────────────────────────────────────────────────────

def task_card(task: Any) -> list[dict]:
    """Single task status card."""
    icon = STATUS_ICONS.get(task.status, ":question:")
    desc = task.description[:80] if task.description else "No description"
    text = f"{icon} *{task.status.replace('_', ' ').title()}*\n{desc}"
    if getattr(task, "pr_url", None):
        text += f"\n:link: <{task.pr_url}|View PR>"
    return [section_block(text)]


def status_blocks(tasks: list) -> list[dict]:
    """Active tasks — professional dashboard view."""
    if not tasks:
        return [
            header_block("📊 Task Status"),
            section_block(
                "You don't have any tasks running right now.\n\n"
                "*Get started:* Submit a task or check your projects."
            ),
            actions_block([
                button_element("🚀 New Task", "menu:task", value="menu:task", style="primary"),
                button_element("📋 Main Menu", "menu:main", value="menu:main"),
            ]),
        ]

    task_lines = []
    for t in tasks[:5]:
        icon = STATUS_ICONS.get(t.status, ":question:")
        status_label = t.status.replace('_', ' ').title()
        desc = t.description[:60] + "..." if len(t.description) > 60 else t.description
        line = f"{icon} *{status_label}* — {desc}"
        if getattr(t, "pr_url", None):
            line += f"  <{t.pr_url}|View PR>"
        task_lines.append(line)

    return [
        header_block(f"📊 Active Tasks ({len(tasks)})"),
        section_block("\n".join(task_lines)),
        actions_block([
            button_element("🚀 New Task", "menu:task", value="menu:task"),
            button_element("📋 Main Menu", "menu:main", value="menu:main"),
        ]),
    ]


# ── Task Workflow Views ──────────────────────────────────────────────

def progress_blocks(
    step: str,
    current: int,
    total: int,
    elapsed: int | None = None,
    log_lines: list[str] | None = None,
) -> list[dict]:
    """Live progress view during task execution."""
    filled = "█" * current
    empty = "░" * (total - current)
    bar = f"`{filled}{empty}` [{current}/{total}]"

    elapsed_txt = f"  —  `{elapsed}s`" if elapsed is not None else ""
    blks: list[dict] = [
        section_block(f":gear: *Implementing...*{elapsed_txt}\n\n{bar}"),
        context_block([f":arrow_right: {step[:300]}"]),
    ]

    if log_lines:
        blks.append(context_block([f":small_blue_diamond: {l}" for l in log_lines[-4:]]))

    return blks


def plan_preview_blocks(plan: str, task_id: str, tunnel_url: str | None = None) -> list[dict]:
    """Implementation plan — professional with context and clear actions."""
    elements = [
        button_element(
            ":white_check_mark: Approve & Start",
            f"approve_plan:{task_id}",
            value=f"approve_plan:{task_id}",
            style="primary",
        ),
        button_element(
            ":x: Request Changes",
            f"discard:{task_id}",
            value=f"discard:{task_id}",
            style="danger",
        ),
    ]
    if tunnel_url:
        elements.append(button_element(
            ":globe_with_meridians: Open Live App",
            f"open_app:{task_id}",
            value=f"open_app:{task_id}", url=tunnel_url,
        ))
    
    return [
        section_block(
            f":scroll: *Implementation Plan Ready for Review*\n\n"
            f"{plan[:2500]}\n\n"
            f":clock1: *Estimated time:* 3-5 minutes"
        ),
        context_block([
            "Please review the plan above and approve to begin, or request changes if you'd like adjustments."
        ]),
        actions_block(elements),
    ]


def diff_preview_blocks(diff: str, task_id: str, tunnel_url: str | None = None) -> list[dict]:
    """Code diff — professional with context."""
    elements = [
        button_element(
            ":white_check_mark: Create Pull Request",
            f"approve:{task_id}",
            value=f"approve:{task_id}",
            style="primary",
        ),
        button_element(
            ":wastebasket: Discard Changes",
            f"discard:{task_id}",
            value=f"discard:{task_id}",
            style="danger",
        ),
    ]
    if tunnel_url:
        elements.append(button_element(
            ":globe_with_meridians: Review Live",
            f"open_app:{task_id}",
            value=f"open_app:{task_id}", url=tunnel_url,
        ))
    
    # Count stats from diff
    files_changed = diff.count(" | ") if diff else 0
    additions = diff.count("\n+") if diff else 0
    deletions = diff.count("\n-") if diff else 0
    
    stats_text = f"*{files_changed} files changed, {additions} insertions(+), {deletions} deletions(-)*"
    
    return [
        section_block(f":page_facing_up: *Changes Ready for Review*\n{stats_text}"),
        section_block(f"```\n{diff[:2200]}\n```"),
        context_block([
            "Review the changes above. When you're ready, create a PR to merge or discard if changes aren't right."
        ]),
        actions_block(elements),
    ]


def summary_blocks(summary: str, diff: str, task_id: str, tunnel_url: str | None = None) -> list[dict]:
    """Task completion — professional summary with clear actions."""
    # Extract stats from diff
    files_changed = diff.count(" | ") if diff else 0
    additions = diff.count("\n+") if diff else 0
    deletions = diff.count("\n-") if diff else 0
    
    main_text = (
        f":white_check_mark: *Implementation Complete*\n\n"
        f"{summary[:1500]}"
    )
    
    stats_text = f"📊 *{files_changed}* files modified • *+{additions}* / *-{deletions}* lines"
    
    elements = [
        button_element(
            ":white_check_mark: Create Pull Request",
            f"approve:{task_id}",
            value=f"approve:{task_id}",
            style="primary",
        ),
        button_element(
            ":wastebasket: Discard Changes",
            f"discard:{task_id}",
            value=f"discard:{task_id}",
            style="danger",
        ),
    ]
    if tunnel_url:
        elements.append(button_element(
            ":globe_with_meridians: Review Live",
            f"open_app:{task_id}",
            value=f"open_app:{task_id}", url=tunnel_url,
        ))
    
    blocks = [
        section_block(main_text),
        context_block([stats_text]),
    ]
    
    if diff:
        blocks.append(section_block(f"*Changes overview:*\n```\n{diff[:800]}\n```"))
    
    blocks.append(context_block([
        "Your changes are staged and ready. Create a PR to merge them, or review in the live app first."
    ]))
    blocks.append(actions_block(elements))
    
    return blocks


def pr_created_blocks(pr_url: str, task_id: str) -> list[dict]:
    """PR created — celebratory with clear next steps."""
    # Extract PR number from URL
    pr_number = ""
    try:
        pr_number = f" #{pr_url.split('/')[-1]}"
    except (ValueError, IndexError):
        pass
    
    return [
        section_block(
            f":tada: *Pull Request Created Successfully*{pr_number}\n\n"
            f"Your changes are ready for review on GitHub:\n"
            f":link: <{pr_url}|View Pull Request on GitHub>"
        ),
        context_block([
            "The PR has been created and is ready for review. You can merge it here when approved."
        ]),
        actions_block([
            button_element(
                ":white_check_mark: Merge PR",
                f"merge:{task_id}",
                value=f"merge:{task_id}",
                style="primary",
            ),
            button_element(
                ":x: Reject & Close",
                f"reject:{task_id}",
                value=f"reject:{task_id}",
                style="danger",
            ),
        ]),
    ]


def error_blocks(text: str, project_id: int | str | None = None) -> list[dict]:
    """Error message — professional with context and next steps."""
    # Use improved error messages for known errors
    error_lower = text.lower()
    if "worker" in error_lower and ("unavailable" in error_lower or "failed" in error_lower):
        message = (
            ":x: *Service Temporarily Unavailable*\n\n"
            "We're experiencing a temporary issue connecting to the worker service. "
            "This usually resolves automatically within a minute.\n\n"
            "*What you can do:*\n"
            "• Wait a moment and try again\n"
            "• Check system status with `/oc-status`\n"
            "• Contact support if the issue persists"
        )
    elif "no changes" in error_lower or "agent made no" in error_lower:
        message = (
            ":warning: *No Changes Detected*\n\n"
            "The agent completed the task but didn't modify any files. This can happen when:\n"
            "• The feature already exists in the codebase\n"
            "• The task description needs more specific details\n"
            "• The agent encountered a technical limitation\n\n"
            "*What you can do:*\n"
            "• Rephrase your request with more specific requirements\n"
            "• Check if the feature is already implemented\n"
            "• Use :speech_balloon: Chat with Agent to discuss"
        )
    elif "timeout" in error_lower or "timed out" in error_lower:
        message = (
            ":stopwatch: *Task Timed Out*\n\n"
            "Your task took longer than expected and was automatically cancelled.\n\n"
            "*This can happen when:*\n"
            "• The task is very complex\n"
            "• The codebase is large\n"
            "• Network issues occurred\n\n"
            "*What you can do:*\n"
            "• Try breaking the task into smaller pieces\n"
            "• Submit again (may complete faster on retry)\n"
            "• Use :speech_balloon: Chat with Agent to discuss"
        )
    elif "not found" in error_lower:
        message = (
            ":mag: *Not Found*\n\n"
            f"{text}\n\n"
            "*What you can do:*\n"
            "• Check the spelling and try again\n"
            "• `/oc-projects` — View all your projects\n"
            "• `/oc-addproject` — Connect a new repository"
        )
    else:
        # Generic error with context
        message = (
            ":x: *Something Went Wrong*\n\n"
            f"{text[:300]}\n\n"
            "This has been logged for investigation. Please try again or contact support if the issue persists."
        )
    
    elements = [
        button_element("📋 Main Menu", "menu:main", value="menu:main"),
    ]
    if project_id is not None:
        elements.insert(0, button_element(
            ":speech_balloon: Chat with Agent",
            f"agent_diagnose:{project_id}",
            value=f"agent_diagnose:{project_id}",
            style="primary",
        ))
    
    return [
        section_block(message),
        actions_block(elements),
    ]


def terminal_blocks(text: str) -> list[dict]:
    """Terminal state — professional completion message with navigation."""
    # Enhance common terminal messages
    text_lower = text.lower()
    if "cancel" in text_lower:
        enhanced_text = (
            ":white_check_mark: *Task Cancelled*\n\n"
            f"{text}\n\n"
            "Your task has been cancelled and any changes have been discarded."
        )
    elif "complete" in text_lower or "done" in text_lower:
        enhanced_text = f":white_check_mark: *Complete*\n\n{text}"
    elif "remove" in text_lower or "delete" in text_lower:
        enhanced_text = f":wastebasket: *Removed*\n\n{text}"
    else:
        enhanced_text = text[:3000]
    
    return [
        section_block(enhanced_text),
        actions_block([
            button_element("🚀 New Task", "menu:task", value="menu:task", style="primary"),
            button_element("📋 Main Menu", "menu:main", value="menu:main"),
        ]),
    ]


def agent_thinking_blocks(user_text: str | None = None, frame: int = 0) -> list[dict]:
    """Professional thinking indicator with animated progress.

    Args:
        frame: Animation frame (0-2) for spinning animation
    """
    preview = f"\n> _{user_text[:100]}..._" if user_text else ""

    # Animated spinner frames
    spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    spinner = spinners[frame % len(spinners)]

    # Animated dots for loading indicators
    dots_frames = ["  ·", " · ·", "· · ·", " · · ", "  · ", ""]
    dots = dots_frames[frame % len(dots_frames)]

    return [
        section_block(
            f"{spinner} *Starting...*{preview}\n\n"
            f":hourglass_flowing_sand: Initializing Claude AI {dots}\n"
            f":file_folder: Preparing workspace\n"
            f":gear: Loading tools...\n\n"
            f"_Step 1/3: Initialization_{dots}"
        ),
        actions_block([button_element("Cancel", "cancel_session", style="danger")]),
    ]


def agent_working_blocks(tool_lines: list[str], elapsed: int = 0) -> list[dict]:
    """Live agent activity with animated progress indicators."""
    # Animated spinner for main activity
    spinners = ["⠋ ", "⠙ ", "⠹ ", "⠸ ", "⠼ ", "⠴ ", "⠦ ", "⠧ ", "⠇ ", "⠏ "]
    spinner = spinners[(elapsed // 1) % len(spinners)]  # Change every second

    # Show last 4 activities with checkmarks
    activity = "\n".join(f":white_check_mark: {line}" for line in tool_lines[-4:]) or ":thinking_face: _Analyzing..._"

    # Animated progress bar (smoothly fills as time passes)
    bar_length = 10
    filled = min(bar_length, max(1, elapsed // 3))  # Fills over 30 seconds
    empty = bar_length - filled
    progress_bar = "".join([":green_square:"] * filled) + "".join([":white_square:"] * empty)

    # Determine current stage
    if elapsed < 3:
        stage = ":one: Planning"
    elif elapsed < 8:
        stage = ":two: Executing"
    elif elapsed < 15:
        stage = ":three: Reviewing"
    else:
        stage = ":four: Finalizing"

    elapsed_msg = f"{elapsed}s elapsed"
    return [
        section_block(
            f"{spinner}*AI Agent Working*\n\n"
            f"*Current Activity:*\n{activity}\n\n"
            f"*Progress:*\n{progress_bar}\n"
            f"{stage} • _{elapsed_msg}_"
        ),
        context_block([":rocket: _Request is being processed. This usually takes 10-20 seconds._"]),
    ]


def agent_response_blocks(
    response: str,
    project_id: int | None = None,
    tunnel_url: str | None = None,
) -> list[dict]:
    """Professional agent response with context-aware actions."""
    blks: list[dict] = [section_block(response[:3000])]

    # Only add hint for longer responses
    if len(response) > 400:
        blks.append(context_block([":speech_balloon: _Reply to continue the conversation_"]))

    elements = []
    if project_id and tunnel_url:
        elements.append(button_element(
            "Open App", f"open_app:{project_id}",
            value=f"open_app:{project_id}", url=tunnel_url,
            style="primary",
        ))
    if project_id:
        elements.append(button_element(
            "New Task", f"task_for:{project_id}",
            value=f"task_for:{project_id}",
        ))
    elements.append(button_element("Menu", "menu:main", value="menu:main"))
    blks.append(actions_block(elements))
    return blks


def project_busy_blocks(running_task_id: str | None = None) -> list[dict]:
    """Interactive message when project is busy with another task."""
    task_ref = f"`{running_task_id[:8]}...`" if running_task_id else "another task"

    return [
        section_block(
            f":hourglass_flowing_sand: *Project is Busy*\n\n"
            f"Task {task_ref} is currently running on this project.\n\n"
            f"*Options:*\n"
            f"• Wait for it to finish (usually 5-15 minutes)\n"
            f"• Retry your request in a moment\n"
            f"• Cancel the running task (if needed)"
        ),
        actions_block([
            button_element("⏸️ View Task", f"view_task:{running_task_id}", style="primary"),
            button_element("🔄 Retry", "retry_task"),
            button_element("❌ Cancel", f"cancel_task:{running_task_id}", style="danger"),
            button_element("◀️ Menu", "menu:main"),
        ]),
    ]


def loading_blocks(text: str = "Processing your request...") -> list[dict]:
    """Loading indicator with professional messaging."""
    # Enhance common loading messages
    if "task" in text.lower() and ("submit" in text.lower() or "received" in text.lower()):
        enhanced_text = (
            ":rocket: *Task Received — Setting Up*\n\n"
            "Your task has been queued and we're preparing the workspace:\n"
            "• Reserving environment...\n"
            "• Cloning repository...\n"
            "• Analyzing codebase structure...\n\n"
            ":clock1: *Estimated start:* ~30 seconds"
        )
    elif "onboard" in text.lower() or "bootstrap" in text.lower():
        enhanced_text = (
            f":arrows_counterclockwise: *{text}*\n\n"
            "Setting up Docker environment and dependencies. This may take a few minutes..."
        )
    elif "health" in text.lower() or "check" in text.lower():
        enhanced_text = f":mag: *{text}*"
    else:
        enhanced_text = f":hourglass_flowing_sand: {text}"
    
    return [
        section_block(enhanced_text),
    ]


# ── Dashboard / Settings ─────────────────────────────────────────────

def dashboard_blocks(url: str) -> list[dict]:
    """Dashboard tunnel — compact with inline open button."""
    return [
        section_block(
            ":chart_with_upwards_trend: *Live Dashboard*\nReal-time container logs via Dozzle",
            accessory=button_element("Open", "open_dashboard", value="open_dashboard", url=url),
        ),
        context_block([f":link: `{url}`"]),
        actions_block([
            button_element("New Link", "menu:dashboard_refresh", value="menu:dashboard_refresh"),
            button_element("Stop", "menu:dashboard_stop", value="menu:dashboard_stop", style="danger"),
            button_element("Menu", "menu:main", value="menu:main"),
        ]),
    ]


def dashboard_retry_blocks() -> list[dict]:
    """Dashboard unavailable — retry or force start."""
    return [
        section_block(
            ":chart_with_upwards_trend: *Dashboard*\n_Tunnel starting up — try again in a moment_"
        ),
        actions_block([
            button_element("Retry", "menu:dashboard", value="menu:dashboard"),
            button_element("Force Start", "menu:dashboard_refresh", value="menu:dashboard_refresh"),
            button_element("Menu", "menu:main", value="menu:main"),
        ]),
    ]


def dashboard_stopped_blocks() -> list[dict]:
    return [
        section_block(":octagonal_sign: Dashboard tunnel stopped"),
        actions_block([
            button_element("Restart", "menu:dashboard_refresh", value="menu:dashboard_refresh"),
            button_element("Menu", "menu:main", value="menu:main"),
        ]),
    ]


def settings_blocks(settings_url: str, wizard_url: str) -> list[dict]:
    """Settings — compact with inline open button."""
    return [
        section_block(
            ":gear: *Settings Dashboard*\nProviders, projects, and connections",
            accessory=button_element("Open", "open_settings", value="open_settings", url=settings_url),
        ),
        context_block([f":link: `{settings_url}`"]),
        actions_block([
            button_element("Wizard", "open_wizard", value="open_wizard", url=wizard_url),
            button_element("New Link", "menu:settings_refresh", value="menu:settings_refresh"),
            button_element("Stop", "menu:settings_stop", value="menu:settings_stop", style="danger"),
            button_element("Menu", "menu:main", value="menu:main"),
        ]),
    ]


def settings_retry_blocks() -> list[dict]:
    return [
        section_block(
            ":gear: *Settings*\n_Tunnel starting up — try again in a moment_"
        ),
        actions_block([
            button_element("Retry", "menu:settings", value="menu:settings"),
            button_element("Force Start", "menu:settings_refresh", value="menu:settings_refresh"),
            button_element("Menu", "menu:main", value="menu:main"),
        ]),
    ]


def settings_stopped_blocks() -> list[dict]:
    return [
        section_block(":octagonal_sign: Settings tunnel stopped"),
        actions_block([
            button_element("Restart", "menu:settings_refresh", value="menu:settings_refresh"),
            button_element("Menu", "menu:main", value="menu:main"),
        ]),
    ]


# ── Add Project Flow ─────────────────────────────────────────────────

def repo_list_blocks(repos: list[dict], existing_map: dict[str, int]) -> list[dict]:
    """GitHub repo selection — compact list with inline Add buttons."""
    blks: list[dict] = [
        section_block("*:heavy_plus_sign: Add Project*"),
    ]

    if not repos:
        blks.append(section_block(
            "Could not fetch repos.\n"
            "_Check GitHub token in setup wizard._"
        ))
    else:
        for repo_info in repos[:15]:
            repo = repo_info["name"]
            desc = repo_info.get("desc", "")

            if repo.lower() in existing_map:
                pid = existing_map[repo.lower()]
                blks.append(section_block(
                    f":white_check_mark: `{repo}`" + (f"  _{desc[:50]}_" if desc else ""),
                    accessory=button_element("Check", f"health:{pid}", value=f"health:{pid}"),
                ))
            else:
                label = f"`{repo}`" + (f"  _{desc[:50]}_" if desc else "")
                blks.append(section_block(
                    label,
                    accessory=button_element(
                        "Add", f"add_repo:{repo}",
                        value=f"add_repo:{repo}", style="primary",
                    ),
                ))

    blks.append(actions_block([
        button_element("Enter URL", "add_repo_manual", value="add_repo_manual"),
        button_element("Retry", "add_repo_retry", value="add_repo_retry"),
        button_element("Menu", "menu:main", value="menu:main"),
    ]))
    return blks


# ── Home Tab ─────────────────────────────────────────────────────────

def home_tab_blocks(
    projects: list | None = None,
    tasks: list | None = None,
) -> list[dict]:
    """App Home Tab — employee mode, clean and simple."""
    blks: list[dict] = [
        section_block(":zap: *OpenClow*\nAI Dev Orchestrator"),
        actions_block([
            button_element("New Task", "home:task", value="home:task", style="primary"),
            button_element("Status", "home:status", value="home:status"),
            button_element("Help", "home:help", value="home:help"),
        ]),
        divider(),
    ]

    # Recent tasks
    if tasks:
        task_lines = []
        for t in tasks[:3]:
            icon = STATUS_ICONS.get(t.status, ":question:")
            desc = t.description[:50] if t.description else "—"
            line = f"{icon} *{t.status.replace('_', ' ').title()}*  {desc}"
            if getattr(t, "pr_url", None):
                line += f"  <{t.pr_url}|PR>"
            task_lines.append(line)
        blks.append(section_block("*Recent Tasks*\n" + "\n".join(task_lines)))
    else:
        blks.append(context_block(["No active tasks — use *New Task* above"]))

    # Connected projects (just info, no admin actions)
    if projects:
        proj_lines = []
        for p in projects[:5]:
            status = getattr(p, "status", "active")
            icon = PROJECT_STATUS_ICONS.get(status, ":white_circle:")
            stack = getattr(p, "tech_stack", None) or ""
            proj_lines.append(f"{icon} *{p.name}*  {stack}")
        blks.append(section_block("*Projects*\n" + "\n".join(proj_lines)))

    return blks


# ── Modal Builders ───────────────────────────────────────────────────

def build_task_modal(projects: list, channel_id: str, preselected_project_id: int | None = None) -> dict:
    """Task creation modal with project selection + description.

    Raises ValueError if projects is empty (Slack requires at least 1 option).
    Callers must check for empty projects before calling this.

    Args:
        projects: List of projects to choose from
        channel_id: The channel ID to store in private_metadata
        preselected_project_id: Optional project ID to pre-select in the dropdown
    """
    if not projects:
        raise ValueError("Cannot build task modal with empty projects list")

    options = [
        {
            "text": {"type": "plain_text", "text": f"{p.name} ({p.tech_stack or ''})"[:75]},
            "value": str(p.id),
        }
        for p in projects
    ]

    # Build project select element
    project_element = {
        "type": "static_select",
        "placeholder": {"type": "plain_text", "text": "Select a project"},
        "options": options,
        "action_id": "project_select",
    }
    # Add initial_option if preselected
    if preselected_project_id is not None:
        for opt in options:
            if opt["value"] == str(preselected_project_id):
                project_element["initial_option"] = opt
                break

    return {
        "type": "modal",
        "callback_id": "task_submit",
        "title": {"type": "plain_text", "text": "🚀 New Task", "emoji": True},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": channel_id,
        "blocks": [
            {
                "type": "input",
                "block_id": "project_block",
                "element": project_element,
                "label": {"type": "plain_text", "text": "Project"},
            },
            {
                "type": "input",
                "block_id": "description_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "task_description",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Describe your development task in detail...",
                    },
                },
                "label": {"type": "plain_text", "text": "Task Description"},
                "hint": {
                    "type": "plain_text",
                    "text": "Minimum 10 characters. Be specific about what you want built.",
                },
            },
            {
                "type": "input",
                "block_id": "mode_block",
                "element": {
                    "type": "radio_buttons",
                    "action_id": "task_mode",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "⚡ Quick — skip planning, start coding immediately", "emoji": True},
                            "value": "quick",
                        },
                        {
                            "text": {"type": "plain_text", "text": "📋 Full — create a plan first, then code", "emoji": True},
                            "value": "full",
                        },
                    ],
                    "initial_option": {
                        "text": {"type": "plain_text", "text": "⚡ Quick — skip planning, start coding immediately", "emoji": True},
                        "value": "quick",
                    },
                },
                "label": {"type": "plain_text", "text": "Mode"},
            },
        ],
    }


def build_task_modal_with_project(projects: list, channel_id: str, project_id: int) -> dict:
    """Task creation modal with pre-selected project.

    Convenience wrapper around build_task_modal.
    """
    return build_task_modal(projects, channel_id, preselected_project_id=project_id)


def build_task_modal_channel_scoped(channel_id: str, project_id: int, project_name: str) -> dict:
    """Task modal for a linked channel — no project picker, just describe + pick mode."""
    # Encode channel + project in metadata so the handler can extract both
    metadata = f"{channel_id}:{project_id}"
    return {
        "type": "modal",
        "callback_id": "task_submit_scoped",
        "title": {"type": "plain_text", "text": f"🚀 {project_name}"[:24], "emoji": True},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": metadata,
        "blocks": [
            {
                "type": "input",
                "block_id": "description_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "task_description",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": f"What do you need done on {project_name}?",
                    },
                },
                "label": {"type": "plain_text", "text": "Task Description"},
                "hint": {
                    "type": "plain_text",
                    "text": "Minimum 10 characters. Be specific about what you want.",
                },
            },
            {
                "type": "input",
                "block_id": "mode_block",
                "element": {
                    "type": "radio_buttons",
                    "action_id": "task_mode",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "⚡ Quick — skip planning, start coding immediately", "emoji": True},
                            "value": "quick",
                        },
                        {
                            "text": {"type": "plain_text", "text": "📋 Full — create a plan first, then code", "emoji": True},
                            "value": "full",
                        },
                    ],
                    "initial_option": {
                        "text": {"type": "plain_text", "text": "⚡ Quick — skip planning, start coding immediately", "emoji": True},
                        "value": "quick",
                    },
                },
                "label": {"type": "plain_text", "text": "Mode"},
            },
        ],
    }


def build_addproject_modal(repos: list[dict], existing_map: dict[str, int], channel_id: str) -> dict:
    """Add project modal with repo selection or manual URL."""
    blocks: list[dict] = []

    # If we have repos, show them as radio buttons
    if repos:
        available = [r for r in repos if r["name"].lower() not in existing_map]
        if available:
            options = []
            for r in available[:20]:
                desc = r.get("desc", "")
                text = r["name"]
                if desc:
                    text += f" — {desc[:40]}"
                options.append({
                    "text": {"type": "plain_text", "text": text[:75]},
                    "value": r["name"],
                })

            blocks.append({
                "type": "input",
                "block_id": "repo_select_block",
                "element": {
                    "type": "radio_buttons",
                    "options": options,
                    "action_id": "repo_select",
                },
                "label": {"type": "plain_text", "text": "Select a Repository"},
                "optional": True,
            })
            blocks.append({"type": "divider"})

    # Always show manual URL input
    blocks.append({
        "type": "input",
        "block_id": "manual_url_block",
        "element": {
            "type": "plain_text_input",
            "action_id": "manual_url",
            "placeholder": {
                "type": "plain_text",
                "text": "https://github.com/owner/repo",
            },
        },
        "label": {"type": "plain_text", "text": "Or Enter Repository URL"},
        "optional": True,
        "hint": {"type": "plain_text", "text": "Use this if your repo isn't listed above"},
    })

    return {
        "type": "modal",
        "callback_id": "addproject_submit",
        "title": {"type": "plain_text", "text": "➕ Add Project", "emoji": True},
        "submit": {"type": "plain_text", "text": "Add"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": channel_id,
        "blocks": blocks,
    }


def build_adduser_modal(channel_id: str) -> dict:
    """Add user modal with Slack user ID + username."""
    return {
        "type": "modal",
        "callback_id": "adduser_submit",
        "title": {"type": "plain_text", "text": "👤 Add User", "emoji": True},
        "submit": {"type": "plain_text", "text": "Add"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": channel_id,
        "blocks": [
            {
                "type": "input",
                "block_id": "user_select_block",
                "element": {
                    "type": "users_select",
                    "action_id": "user_select",
                    "placeholder": {"type": "plain_text", "text": "Select a user"},
                },
                "label": {"type": "plain_text", "text": "Slack User"},
            },
            {
                "type": "input",
                "block_id": "username_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "username_input",
                    "placeholder": {"type": "plain_text", "text": "Optional display name"},
                },
                "label": {"type": "plain_text", "text": "Username (optional)"},
                "optional": True,
            },
        ],
    }


# ── Dev Mode ────────────────────────────────────────────────────────

def build_dev_modal() -> dict:
    """Dev mode unlock modal — password prompt."""
    return {
        "type": "modal",
        "callback_id": "dev_unlock",
        "title": {"type": "plain_text", "text": "🔒 Developer Mode", "emoji": True},
        "submit": {"type": "plain_text", "text": "Unlock"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Enter the developer password to unlock admin commands."},
            },
            {
                "type": "input",
                "block_id": "password_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "dev_password",
                    "placeholder": {"type": "plain_text", "text": "Password"},
                },
                "label": {"type": "plain_text", "text": "Password"},
            },
        ],
    }


def build_unlinked_channel_prompt(channel_id: str) -> list[dict]:
    """Prompt shown when user messages an unlinked channel."""
    return [
        section_block(
            ":warning: *This channel isn't linked to a project yet*\n\n"
            "Link it to a project so I can help you build features and fix bugs."
        ),
        actions_block([
            button_element("🔗 Link to Project", "menu:addproject", value="menu:addproject", style="primary"),
            button_element("❓ Help", "menu:help", value="menu:help"),
        ]),
    ]


def build_dm_project_selector(projects: list, pending_text: str) -> list[dict]:
    """Project selector for DMs when no default project is set."""
    blks: list[dict] = [
        section_block(
            "📦 *Which project should I use?*\n\n"
            f"Your message: _{pending_text[:100]}_"
        ),
    ]
    for p in projects[:5]:
        blks.append(
            section_block(
                f"*{p.name}* — {p.tech_stack or 'N/A'}",
                accessory=button_element(
                    "Select",
                    f"dm_project_select:{p.id}:{pending_text[:50]}",
                    value=f"dm_project_select:{p.id}:{pending_text[:50]}",
                    style="primary",
                ),
            )
        )
    return blks


def dev_mode_status_blocks(active: bool, remaining_mins: int = 0) -> list[dict]:
    """Dev mode status message blocks."""
    if active:
        return [
            section_block(
                f":unlock: *Developer Mode Active*\n"
                f"Admin commands unlocked for ~{remaining_mins} min.\n"
                "Use `/oc-dev` again to extend, or `/oc-devoff` to deactivate."
            ),
        ]
    return [
        section_block(
            ":lock: *Developer Mode Deactivated*\n"
            "Admin commands are locked."
        ),
    ]
