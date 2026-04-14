"""Centralized user-facing message templates.

This module provides consistent, professional messaging across all chat platforms.
Messages are designed for:
- Clarity: Users understand what happened and why
- Actionability: Users know what to do next
- Empathy: Errors are explained without blame
- Context: Progress includes relevant details
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openclow.models import Project, Task


# ═══════════════════════════════════════════════════════════════════════════════
# Welcome & Onboarding
# ═══════════════════════════════════════════════════════════════════════════════

WELCOME_MESSAGE = """Welcome to THAG GROUP 👋

Your AI-powered development assistant is ready. I can help you:
• Build features and fix bugs
• Review and deploy code
• Monitor your applications

Choose an option below to get started, or type /help for detailed commands."""

HELP_MESSAGE = """📖 Available Commands

<b>Task Management</b>
/task — Submit a new development task
/status — View active tasks and their progress
/cancel — Cancel a running task

<b>Project Management</b>
/projects — List all connected projects
/addproject — Connect a GitHub repository
/removeproject &lt;name&gt; — Remove a project

<b>Operations</b>
/dockerup &lt;name&gt; — Start project containers
/dockerdown &lt;name&gt; — Stop project containers
/bootstrap &lt;name&gt; — Re-initialize project

<b>Monitoring</b>
/dashboard — Live container logs
/settings — Configuration dashboard
/logs — AI-analyzed system logs

<b>Admin</b>
/adduser — Authorize a new user
/qa [smoke|full] — Run automated tests

Need help? Just ask me anything!"""

SLACK_HELP_MESSAGE = """*📖 Available Commands*

*Task Management*
• `/oc-task` — Submit a new development task
• `/oc-status` — View active tasks and their progress
• `/oc-cancel` — Cancel a running task

*Project Management*
• `/oc-projects` — List all connected projects
• `/oc-addproject` — Connect a GitHub repository
• `/oc-removeproject <name>` — Remove a project

*Operations*
• `/oc-dockerup <name>` — Start project containers
• `/oc-dockerdown <name>` — Stop project containers
• `/oc-bootstrap <name>` — Re-initialize project

*Monitoring*
• `/oc-dashboard` — Live container logs
• `/oc-settings` — Configuration dashboard
• `/oc-logs` — AI-analyzed system logs

*Admin*
• `/oc-adduser` — Authorize a new user
• `/oc-qa [smoke|full]` — Run automated tests

Need help? Just mention me or send a DM!"""


# ═══════════════════════════════════════════════════════════════════════════════
# Task Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

def task_submitted_message(task_description: str) -> str:
    """Message shown when task is successfully submitted."""
    return f"""🚀 Task Received — Setting Up

<i>{task_description[:100]}{'...' if len(task_description) > 100 else ''}</i>

We're preparing your development environment:
• Reserving workspace...
• Cloning repository...
• Analyzing codebase structure...

⏱️ <b>Estimated start:</b> ~30 seconds
You'll receive updates as work progresses."""


def task_queued_message(position: int | None = None) -> str:
    """Message shown when task is queued."""
    if position:
        return f"⏳ Your task is #{position} in queue. Starting soon..."
    return "⏳ Your task is queued and will start shortly..."


# ═══════════════════════════════════════════════════════════════════════════════
# Implementation Plan
# ═══════════════════════════════════════════════════════════════════════════════

def plan_preview_message(plan_text: str, estimated_minutes: int = 5) -> str:
    """Format the implementation plan for user review."""
    return f"""📋 Implementation Plan Ready for Review

I've analyzed your request and created a step-by-step plan. Here's what I'll implement:

{plan_text[:3000]}

⏱️ <b>Estimated time:</b> {estimated_minutes}-{estimated_minutes + 2} minutes

Please review the plan above and approve to begin implementation, or let me know if you'd like any adjustments."""


PLAN_APPROVED_MESSAGE = """✅ Plan Approved — Starting Implementation

The agent is now coding your solution. You'll receive:
• Live progress updates
• Completion summary with changes
• Option to review before creating PR

Sit back and relax! ☕"""


# ═══════════════════════════════════════════════════════════════════════════════
# Progress Updates
# ═══════════════════════════════════════════════════════════════════════════════

def progress_message(step: str, current: int, total: int, elapsed_seconds: int) -> str:
    """Format a progress update message."""
    percentage = int((current / total) * 100) if total > 0 else 0
    
    # Estimate remaining time (rough heuristic: ~30s per step)
    remaining_steps = total - current
    estimated_remaining = remaining_steps * 30
    
    if estimated_remaining < 60:
        eta_text = "~1 minute"
    else:
        eta_text = f"~{estimated_remaining // 60} minutes"
    
    filled = "█" * current
    empty = "░" * (total - current)
    
    return f"""🔄 Implementing Your Task

<code>{filled}{empty}</code> <b>{percentage}%</b> complete ({eta_text} remaining)

<b>Step {current} of {total}:</b> {step}

⏱️ Elapsed: {elapsed_seconds}s"""


# ═══════════════════════════════════════════════════════════════════════════════
# Completion & Success
# ═══════════════════════════════════════════════════════════════════════════════

def task_complete_message(
    summary: str,
    files_modified: int,
    lines_added: int,
    lines_removed: int,
    duration_seconds: int,
    tunnel_url: str | None = None,
) -> str:
    """Format task completion message."""
    duration_text = _format_duration(duration_seconds)
    
    message = f"""✅ Implementation Complete

📊 <b>Summary:</b>
• Duration: {duration_text}
• Files modified: {files_modified}
• Changes: +{lines_added}, -{lines_removed}

📝 <b>What was done:</b>
{summary[:1500]}

🔍 <b>Review your changes:</b>
Changes are staged and ready for your review."""
    
    if tunnel_url:
        message += f"""

🌐 <b>Live Preview:</b> {tunnel_url}"""
    
    return message


def pr_created_message(pr_url: str, pr_number: int) -> str:
    """Message shown when PR is successfully created."""
    return f"""🎉 Pull Request Created Successfully

Your changes are now ready for review on GitHub:
🔗 {pr_url}

<b>Next steps:</b>
• Review the PR on GitHub
• Merge when ready (or click Merge below)
• The changes will deploy automatically

PR #{pr_number}"""


PR_MERGED_MESSAGE = """✅ Pull Request Merged

Your changes have been merged to the main branch and will be deployed automatically.

🚀 Deployment typically completes within 1-2 minutes."""


# ═══════════════════════════════════════════════════════════════════════════════
# Error Messages (Support-Quality)
# ═══════════════════════════════════════════════════════════════════════════════

class ErrorMessages:
    """Professional error messages with context and next steps."""
    
    WORKER_UNAVAILABLE = """❌ Service Temporarily Unavailable

We're experiencing a temporary issue connecting to the worker service. This usually resolves automatically within a minute.

<b>What you can do:</b>
• Wait a moment and try again
• Check system status with /status
• Contact support if the issue persists

<i>Error reference: WORKER_CONN_001</i>"""
    
    AGENT_NO_CHANGES = """⚠️ No Changes Detected

The agent completed the task but didn't modify any files. This can happen when:
• The feature already exists in the codebase
• The task description needs more specific details
• The agent encountered a technical limitation

<b>What you can do:</b>
• Rephrase your request with more specific requirements
• Check if the feature is already implemented

<i>Would you like to try again with a different approach?</i>"""
    
    PROJECT_BUSY = """⏳ Project Currently Busy

Another task is currently running on this project. Only one task can run per project at a time to prevent conflicts.

<b>Current status:</b>
{holder_info}

<b>What you can do:</b>
• Wait for the current task to complete
• Cancel the current task with /cancel
• Submit this task to a different project

<i>You'll be notified when your project is available.</i>"""
    
    PROJECT_NOT_FOUND = """❌ Project Not Found

I couldn't find a project with that name. Please check the spelling or list your projects with /projects.

<b>Available actions:</b>
• /projects — View all your projects
• /addproject — Connect a new repository

<i>Project names are case-sensitive.</i>"""
    
    TASK_NOT_FOUND = """❌ Task Not Found

I couldn't find that task. It may have been completed, cancelled, or the ID may be incorrect.

<b>What you can do:</b>
• /status — View your active tasks
• Submit a new task if needed"""
    
    UNAUTHORIZED = """🔒 Access Denied

Your account doesn't have permission to use THAG GROUP yet.

<b>To get access:</b>
Contact your workspace administrator to authorize your account.

<i>Your ID: {user_id}</i>"""
    
    GITHUB_FETCH_FAILED = """⚠️ GitHub Connection Issue

I couldn't fetch your repositories. This usually means:
• Your GitHub token isn't configured
• The token has expired or lacks permissions
• GitHub's API is experiencing issues

<b>What you can do:</b>
• Run the setup wizard: /settings
• Check your GitHub token permissions (needs 'repo' access)
• Try again in a few minutes

<i>Error reference: GITHUB_API_001</i>"""
    
    DOCKER_ERROR = """❌ Docker Operation Failed

There was an issue with the Docker operation. Common causes:
• Container is already running (or stopped)
• Insufficient resources on the host
• Configuration issue with docker-compose

<b>What you can do:</b>
• Check current status with /status
• Try the operation again
• Review logs with /logs for details

<i>Error reference: DOCKER_OP_001</i>"""
    
    TIMEOUT = """⏱️ Task Timed Out

Your task took longer than expected and was automatically cancelled to prevent resource issues.

<b>This can happen when:</b>
• The task is very complex
• The codebase is large
• Network issues occurred during execution

<b>What you can do:</b>
• Try breaking the task into smaller pieces
• Submit again (it may complete faster on retry)
• Submit again (it may complete faster on retry)"""
    
    GENERIC_ERROR = """❌ Something Went Wrong

An unexpected error occurred. Don't worry — this has been logged and we'll investigate.

<b>What you can do:</b>
• Try the operation again
• Check /status for system health
• Contact support with error reference: {ref}

<i>We apologize for the inconvenience.</i>"""


# ═══════════════════════════════════════════════════════════════════════════════
# Status & Project Views
# ═══════════════════════════════════════════════════════════════════════════════

def project_status_message(project: Project, tunnel_url: str | None = None) -> str:
    """Format project status for display."""
    status_emoji = {
        "active": "🟢",
        "bootstrapping": "🔄",
        "failed": "🔴",
        "inactive": "⚪",
    }.get(project.status, "❓")
    
    status_label = {
        "active": "Ready",
        "bootstrapping": "Setting up...",
        "failed": "Setup Failed",
        "inactive": "Unlinked",
    }.get(project.status, project.status)
    
    message = f"""📦 <b>{project.name}</b>
{status_emoji} Status: {status_label}

<b>Repository:</b> <code>{project.github_repo}</code>
<b>Tech Stack:</b> {project.tech_stack or "N/A"}
<b>Docker:</b> {"Yes" if project.is_dockerized else "No"}"""
    
    if project.description:
        message += f"""
<b>Description:</b> {project.description[:200]}"""
    
    if tunnel_url:
        message += f"""

🌐 <b>Live URL:</b> {tunnel_url}"""
    
    return message


def active_tasks_message(tasks: list[Task]) -> str:
    """Format list of active tasks."""
    if not tasks:
        return """📊 No Active Tasks

You don't have any tasks running right now.

<b>Get started:</b>
• /task — Submit a new task
• /projects — Browse your projects"""
    
    status_emoji = {
        "pending": "⏳",
        "preparing": "🔧",
        "planning": "🧠",
        "plan_review": "📋",
        "coding": "💻",
        "reviewing": "🔍",
        "diff_preview": "📄",
        "awaiting_approval": "✋",
        "pushing": "📤",
    }
    
    lines = [f"📊 Active Tasks ({len(tasks)})\n"]
    
    for task in tasks:
        emoji = status_emoji.get(task.status, "❓")
        desc = task.description[:50] + "..." if len(task.description) > 50 else task.description
        lines.append(f"{emoji} <b>{task.status.replace('_', ' ').title()}</b>")
        lines.append(f"   {desc}")
        if task.pr_url:
            lines.append(f"   🔗 <a href='{task.pr_url}'>View PR</a>")
        lines.append("")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Confirmation & Prompts
# ═══════════════════════════════════════════════════════════════════════════════

CONFIRM_REMOVE_PROJECT = """🗑️ Confirm Project Removal

You're about to permanently remove <b>{project_name}</b>.

<b>This will:</b>
• Stop and remove all Docker containers
• Delete the project from the database
• Remove the workspace and tunnel

⚠️ <b>This action cannot be undone.</b>

Are you sure you want to continue?"""


CONFIRM_DISCARD_CHANGES = """🗑️ Confirm Discard Changes

You're about to discard all changes made by the agent.

<b>This will:</b>
• Revert all modified files
• Delete any new files created
• Clear the workspace

The task will be marked as cancelled.

Are you sure you want to continue?"""


# ═══════════════════════════════════════════════════════════════════════════════
# Utility Functions
# ═══════════════════════════════════════════════════════════════════════════════

def _format_duration(seconds: int) -> str:
    """Format duration in human-readable form."""
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    if minutes < 60:
        if remaining_seconds > 0:
            return f"{minutes} min {remaining_seconds}s"
        return f"{minutes} minutes"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes > 0:
        return f"{hours}h {remaining_minutes}m"
    return f"{hours} hours"


def truncate(text: str, max_length: int, suffix: str = "...") -> str:
    """Truncate text to max length with suffix."""
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix
