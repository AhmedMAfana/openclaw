# TAGH Dev Full Code Audit & Fix Plan

> Generated: 2026-04-08 | 82 Python files audited line-by-line
> Status: **IN PROGRESS**

---

## Executive Summary

Full codebase audit revealed **7 CRITICAL**, **18 HIGH**, **25+ MEDIUM** issues across the entire project. The most dangerous systemic problem is **command injection** in 8+ files (~20 injection points) where user-controlled strings are interpolated into shell commands via `create_subprocess_shell()`.

---

## Phase 1: CRITICAL Security & Crash Fixes

### 1.1 Command Injection Eradication
**Files affected:** `git_ops.py`, `bootstrap.py`, `docker_service.py`, `docker_mcp.py`, `github_service.py`, `health_service.py`, `github.py (provider)`, `github_mcp.py`

**Problem:** Every file that calls `run_cmd()` or `create_subprocess_shell()` interpolates user input (repo names, branch names, commit messages, container names, compose files) directly into shell command strings. An attacker who controls any of these values (via DB or user input) can execute arbitrary commands on the host.

**Fix:**
- Replace ALL `create_subprocess_shell(f"...")` with `create_subprocess_exec("cmd", "arg1", "arg2")`
- Where shell features are needed (pipes, redirects), use `shlex.quote()` on every interpolated value
- Refactor `run_cmd()` in `git_ops.py` to accept argument lists instead of a single command string

**Injection points:**
| File | Line(s) | Injected var |
|------|---------|--------------|
| git_ops.py | 47 | `repo`, `dest` |
| git_ops.py | 59 | `branch_name` |
| git_ops.py | 69 | `message` |
| git_ops.py | 94-95 | `title`, `body` |
| bootstrap.py | 120 | `workspace`, `default_branch` |
| bootstrap.py | 136 | `env_example`, `env_path` |
| bootstrap.py | 151-153 | `compose`, `compose_project` |
| bootstrap.py | 196 | `cname` |
| docker_service.py | 21,53,75 | `project_name`, `container_name`, `compose_file` |
| docker_mcp.py | 18,41,54,67,82,96,110 | ALL user inputs |
| github_service.py | 10-16 | `title`, `body` |
| health_service.py | 57,100 | `project_name`, `c.name` |
| github.py (provider) | 17,24-30 | `repo`, `token`, `title`, `body` |
| github_mcp.py | 44+ | `repo`, `state`, `limit` |

---

### 1.2 Settings Missing Fields (Runtime Crash)
**File:** `settings.py`

**Problem:** `coder.py:64` references `settings.claude_coder_max_turns` and `reviewer.py:75` references `settings.claude_reviewer_max_turns` -- neither field exists on the `Settings` class. Both agents crash with `AttributeError` on first use.

**Fix:** Add fields to Settings with sensible defaults.

---

### 1.3 `run_coder_fix` KeyError Crash
**File:** `claude.py:230`

**Problem:** `CODER_SYSTEM_PROMPT.format(...)` is called without `app_container` or `app_port` arguments. The template references `{app_container}` and `{app_port}`. This raises `KeyError` every time the review-fix loop runs, meaning code review findings are **never auto-fixed**.

**Fix:** Pass `app_container` and `app_port` to the format call, same as `run_coder()` does.

---

### 1.4 Docker Service Shell Injection + Subprocess Sleep
**File:** `docker_service.py`

**Problem:** Shell injection via `project_name`/`container_name` + `await run_cmd("sleep 10")` spawns a subprocess just to sleep.

**Fix:** `create_subprocess_exec` with arg lists + `await asyncio.sleep(10)` + poll for readiness.

---

## Phase 2: HIGH Severity Fixes

### 2.1 Hardcoded Laravel/Vue Agent Prompts
**Files:** `coder.py`, `reviewer.py`, `doctor.py`, `claude.py`

**Problem:** System prompts contain Laravel-specific instructions (`php artisan`, `composer`, `npm run build`) for ALL projects regardless of `tech_stack`. A Python or Go project gets told to run `php artisan migrate`.

**Fix:** Make prompts tech-stack-aware using the project's `tech_stack` field. Use conditional template sections or let the agent discover conventions.

---

### 2.2 Broken Workspace Locking
**File:** `workspace_service.py:36-53`

**Problem:** Redis Lock is acquired but the Lock object is immediately discarded. `_release_lock` creates a NEW Lock instance and calls `release()` -- this Lock was never acquired, so it fails. ALSO: neither `_get_lock` nor `_release_lock` is ever called anywhere.

**Fix:** Store the Lock object on `self`, release the same instance. Actually USE the locks in `prepare()`/`cleanup()`.

---

### 2.3 Factory Singleton Race Condition
**File:** `factory.py:16-41`

**Problem:** `_instances` dict used as singleton cache with no async lock. Two concurrent coroutines can both create instances. Also, after `chat.close()`, the cached instance has a dead session but future calls return it.

**Fix:** Add `asyncio.Lock()` around instance creation. Invalidate cache on `close()`.

---

### 2.4 Bootstrap Process Handling
**File:** `bootstrap.py`

**Problems:**
- L64-68: `proc.kill()` in except block but `proc` may be unassigned (`UnboundLocalError`)
- L293-315: cloudflared subprocess started but PID never stored -- leaks forever

**Fix:** Init `proc = None`, guard kill. Store tunnel PID in registry/DB.

---

### 2.5 Notification Unbounded Recursion
**File:** `notification.py:45-48`

**Problem:** On `TelegramRetryAfter`, recursively calls `_flush()`. Repeated rate-limits = stack overflow.

**Fix:** Convert to loop with max retry count.

---

### 2.6 Activity Log Issues
**File:** `activity_log.py`

**Problems:**
- L22: `threading.Lock` blocks event loop in async code
- L127-152: Reads entire JSONL file into memory. `stats()` calls `query(last_n=999999)`

**Fix:** Use `asyncio.Lock`. Add file rotation or tail-based reading.

---

### 2.7 Logging Setup Redundancy
**File:** `utils/logging.py:27`

**Problem:** `setup_logging()` reconfigures structlog on every `get_logger()` call (every module import).

**Fix:** Call `setup_logging()` once at app startup. Make `get_logger()` just call `structlog.get_logger()`.

---

### 2.8 Review Handler Runs in Wrong Container
**File:** `bot/handlers/review.py:39-53`

**Problem:** `discard_changes()` calls `WorkspaceService().cleanup()` from the bot container, which has no filesystem access to `/workspaces`.

**Fix:** Dispatch via arq to the worker, same as approve/merge/reject.

---

### 2.9 Tunnel Service Blind PID Kill
**File:** `tunnel_service.py:113-116`

**Problem:** `os.kill(pid, 9)` from DB. After restart, PID could belong to a different process.

**Fix:** Verify process is actually `cloudflared` before killing (check cmdline).

---

## Phase 3: MEDIUM Severity Fixes

### 3.1 ChatProvider Abstraction Leaks
4+ files call `chat._get_bot()` directly, bypassing the abstract interface. Add `send_message_with_keyboard()` and `edit_message_with_keyboard()` to `ChatProvider` base class.

### 3.2 Detached ORM Sessions
`orchestrator.py`, `start.py`, `task.py` load ORM objects in one session and use them after close. Fix with `expire_on_commit=False` or re-query in the same session.

### 3.3 Subprocess Sleep Patterns
Replace `await run_cmd("sleep N")` with `await asyncio.sleep(N)` + readiness polling in `docker_service.py`, `workspace_service.py`, `bootstrap.py`.

### 3.4 Duplicate Code
- `github_service.py` vs `git_ops.py`: Two implementations of PR create/merge/close
- `run_cmd` in git_ops vs `_run` in bootstrap: Two shell command runners
- Menu vs command handlers in `start.py`: Same logic duplicated

### 3.5 Swallowed Exceptions
Replace `except Exception: pass` with proper logging in: bootstrap notify, git config fetch, admin handler, cancel handler.

### 3.6 Health Endpoint
`api/routes/health.py` returns OK unconditionally. Add DB and Redis connectivity checks.

### 3.7 No DB Indexes
Add indexes on `tasks.chat_id`, `tasks.status`, `tasks.user_id`, `task_logs.task_id`.

### 3.8 Auth Middleware
Add `is_admin` role check. Cache user lookups with TTL.

### 3.9 API Authentication
Add API key or bearer token middleware to FastAPI.

---

## Phase 4: LOW Severity Cleanup

- Remove unused imports across all files
- Replace deprecated `asyncio.get_event_loop()` with `get_running_loop()`
- Remove dead code (`AgentResult`, `ChatContext`, `get_session`, `reset()`)
- Fix `BigInteger` unused imports in models
- Pin npm package versions in Dockerfile.worker
- Add `UniqueConstraint` to config model

---

## Architecture Notes

### Why Command Injection is Systemic
The root cause is `git_ops.run_cmd()` which uses `create_subprocess_shell`. Every caller builds a command string. The fix must be at the `run_cmd` level -- change it to accept argument lists and use `create_subprocess_exec`. All callers then pass lists instead of f-strings.

### Why the Locking is Broken
The Redis Lock pattern requires holding the Lock object reference between acquire and release. The current code creates one Lock to acquire, discards it, then creates a new Lock to release. The new Lock doesn't own the lock, so release fails. The fix is trivial but the locking functions also need to actually be called.

### Why Agent Prompts Must Be Dynamic
The project model already has a `tech_stack` field and `agent_system_prompt`. The prompts should use these instead of hardcoding Laravel. The `agent_system_prompt` is already parsed for `APP_CONTAINER` and `APP_PORT` -- extend this pattern for tech-stack-specific instructions.
