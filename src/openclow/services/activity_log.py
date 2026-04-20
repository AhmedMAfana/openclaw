"""Drop-in activity log for TAGH Dev — append-only JSONL.

Usage:
    from openclow.services.activity_log import log_event, log_task, log_agent, query, stats

    log_event("startup", {"version": "1.0"})
    log_task("abc-123", "coding", project="trade-bot")
    log_agent("coder", "started", task_id="abc-123", tool="bash")

    recent = query("error", last_n=20)
    dashboard = stats()
"""

import json
import os
import sys
import time
import threading
from typing import Any

from openclow.settings import settings

LOG_FILE = settings.activity_log
_lock = threading.Lock()


# ──────────────────────────────────────────────
# Core writer
# ──────────────────────────────────────────────

def log_event(event_type: str, data: dict[str, Any] | None = None):
    """Append one JSON line. Thread-safe. Never crashes the caller."""
    entry = {"ts": time.time(), "type": event_type, **(data or {})}
    with _lock:
        try:
            os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            print(f"activity_log write failed: {e}", file=sys.stderr)


# ──────────────────────────────────────────────
# Typed helpers — TAGH Dev domain
# ──────────────────────────────────────────────

def log_task(task_id: str, status: str, **kwargs):
    """Task lifecycle: created, planning, coding, reviewing, pushed, merged, failed."""
    log_event("task", {"task_id": task_id, "status": status, **kwargs})


def log_agent(agent: str, action: str, **kwargs):
    """Agent activity: planner/coder/reviewer/doctor/chat started/finished/error."""
    log_event("agent", {"agent": agent, "action": action, **kwargs})


def log_llm_call(model: str, tokens_in: int = 0, tokens_out: int = 0,
                 duration_ms: int = 0, cost: float = 0, **kwargs):
    """LLM API call tracking."""
    log_event("llm_call", {
        "model": model, "tokens_in": tokens_in, "tokens_out": tokens_out,
        "duration_ms": duration_ms, "cost": cost, **kwargs,
    })


def log_tool_use(tool: str, agent: str = "", duration_ms: int = 0,
                 success: bool = True, **kwargs):
    """Tool use by agents (bash, edit, read, etc.)."""
    log_event("tool_use", {
        "tool": tool, "agent": agent, "duration_ms": duration_ms,
        "success": success, **kwargs,
    })


def log_request(method: str, path: str, status: int, duration_ms: int = 0, **kwargs):
    """HTTP request to our API."""
    log_event("request", {
        "method": method, "path": path, "status": status,
        "duration_ms": duration_ms, **kwargs,
    })


def log_bot_command(command: str, user_id: str = "", **kwargs):
    """Telegram bot command received."""
    log_event("bot_command", {"command": command, "user_id": user_id, **kwargs})


def log_worker_job(job: str, status: str, duration_s: float = 0, **kwargs):
    """Background worker job lifecycle."""
    log_event("worker_job", {
        "job": job, "status": status, "duration_s": round(duration_s, 2), **kwargs,
    })


def log_git_op(operation: str, project: str = "", duration_ms: int = 0,
               success: bool = True, **kwargs):
    """Git operations: clone, branch, push, pr_create, merge."""
    log_event("git_op", {
        "operation": operation, "project": project,
        "duration_ms": duration_ms, "success": success, **kwargs,
    })


def log_error(source: str, error: str, **kwargs):
    """Error from any component."""
    log_event("error", {
        "source": source, "error": str(error)[:500], **kwargs,
    })


def log_docker(action: str, container: str = "", project: str = "",
               success: bool = True, **kwargs):
    """Docker operations: compose_up, compose_down, exec, health_check."""
    log_event("docker", {
        "action": action, "container": container,
        "project": project, "success": success, **kwargs,
    })


# ──────────────────────────────────────────────
# Reader / query
# ──────────────────────────────────────────────

def query(event_type: str = "", last_n: int = 50, since_ts: float = 0,
          filters: dict[str, Any] | None = None) -> list[dict]:
    """Query activity log. Returns last N matching entries."""
    from collections import deque
    filters = filters or {}
    matches = deque(maxlen=last_n)
    try:
        with open(LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and entry.get("type") != event_type:
                    continue
                if since_ts and entry.get("ts", 0) < since_ts:
                    continue
                if filters and not all(entry.get(k) == v for k, v in filters.items()):
                    continue
                matches.append(entry)
    except FileNotFoundError:
        return []
    return list(matches)


def stats() -> dict:
    """Summary statistics from the activity log."""
    entries = query(last_n=999999)
    by_type: dict[str, int] = {}
    errors_last_hour = 0
    tasks_by_status: dict[str, int] = {}
    now = time.time()
    hour_ago = now - 3600

    for e in entries:
        t = e.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

        if t == "error" and e.get("ts", 0) > hour_ago:
            errors_last_hour += 1

        if t == "task":
            s = e.get("status", "unknown")
            tasks_by_status[s] = tasks_by_status.get(s, 0) + 1

    return {
        "total_events": len(entries),
        "by_type": by_type,
        "errors_last_hour": errors_last_hour,
        "tasks_by_status": tasks_by_status,
    }


def tail(n: int = 20) -> list[dict]:
    """Last N events regardless of type."""
    return query(last_n=n)
