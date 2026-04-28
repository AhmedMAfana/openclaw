"""Settings API — config CRUD, connection tests, project/user management."""
from __future__ import annotations

import html as _html
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func as sa_func

from openclow.api.auth import verify_settings_auth
from openclow.api.schemas.settings import (
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    ProviderConfigUpdate,
    TestResult,
    UserCreate,
    UserResponse,
)
from openclow.models.base import async_session
from openclow.models.config import PlatformConfig
from openclow.models.project import Project
from openclow.models.user import User
from openclow.providers.registry import available_providers
from openclow.services import config_service
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(verify_settings_auth)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask_token(value: str) -> str:
    """Mask sensitive tokens for display: show first 4 and last 4 chars."""
    if len(value) <= 10:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def _mask_config(config: dict) -> dict:
    """Return a copy of config with sensitive fields masked."""
    sensitive_keys = {"token", "api_key", "bot_token", "app_token", "signing_secret"}
    masked = {}
    for k, v in config.items():
        if k in sensitive_keys and isinstance(v, str) and v:
            masked[k] = _mask_token(v)
        else:
            masked[k] = v
    return masked


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

@router.get("/config")
async def get_all_config():
    """Return all platform configs with tokens masked."""
    configs = await config_service.get_all_config()
    return {key: _mask_config(val) for key, val in configs.items()}


@router.get("/config/{category}")
async def get_category_config(category: str):
    """Return config for a single category."""
    config = await config_service.get_config(category, "provider")
    if not config:
        return {"configured": False}
    return {"configured": True, **_mask_config(config)}


@router.put("/config/{category}")
async def update_config(category: str, body: ProviderConfigUpdate):
    """Update provider config for a category, then reset factory cache."""
    if category not in ("llm", "chat", "git", "system"):
        raise HTTPException(400, f"Invalid category: {category}")

    config_dict = body.model_dump()
    provider_type = config_dict.pop("type", None)
    if not provider_type:
        raise HTTPException(400, "Missing 'type' field")

    # Merge with existing config: skip masked/placeholder token values
    # so we don't overwrite real secrets with "8606****yI0" strings.
    sensitive_keys = {"token", "api_key", "bot_token", "app_token", "signing_secret"}
    existing = await config_service.get_provider_config_by_type(category, provider_type) or {}
    for key in sensitive_keys:
        new_val = config_dict.get(key, "")
        if isinstance(new_val, str) and ("****" in new_val or not new_val):
            if key in existing:
                config_dict[key] = existing[key]

    # Read old active type BEFORE writing (for switch warning)
    old_type = None
    if category == "chat":
        try:
            old_active = await config_service.get_config(category, "provider") or {}
            old_type = old_active.get("type")
        except Exception:
            pass

    # Store per-type (preserves both Telegram and Slack configs)
    await config_service.set_provider_config(category, provider_type, config_dict)
    # Also update legacy key for backwards compat
    await config_service.set_config(category, "provider", {"type": provider_type, **config_dict})

    # Reset cached provider instances so next call picks up new config
    from openclow.providers.factory import reset
    await reset()

    # Auto-restart the bot when chat provider changes
    warning = None
    if category == "chat":
        if old_type and old_type != provider_type:
            from openclow.models.task import Task
            terminal_statuses = {"merged", "rejected", "discarded", "failed", "orphaned"}
            async with async_session() as session:
                count = (await session.execute(
                    select(sa_func.count(Task.id))
                    .where(Task.chat_provider_type == old_type)
                    .where(~Task.status.in_(terminal_statuses))
                )).scalar() or 0
                if count > 0:
                    warning = f"{count} active task(s) on {old_type} will be marked as orphaned"

        try:
            from openclow.worker.arq_app import get_arq_pool
            pool = await get_arq_pool()
            await pool.enqueue_job("restart_bot_task", "provider_changed")
        except Exception as e:
            log.warning("settings.bot_restart_enqueue_failed", error=str(e))

    result = {"status": "ok", "message": f"{category} provider updated to '{body.type}'"}
    if warning:
        result["warning"] = warning
    return result


# Cache for bot status to avoid overwhelming the queue
_bot_status_cache = {"data": None, "timestamp": 0}
_BOT_STATUS_CACHE_TTL = 25  # seconds (matches dashboard polling interval)

@router.get("/bot-status")
async def bot_status():
    """Get bot status by asking the worker (which has Docker socket access).

    Uses caching to prevent overwhelming the job queue with repeated requests
    from the dashboard polling every 15 seconds.
    """
    import time
    now = time.time()

    # Return cached result if still fresh
    if _bot_status_cache["data"] and (now - _bot_status_cache["timestamp"]) < _BOT_STATUS_CACHE_TTL:
        return _bot_status_cache["data"]

    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await get_arq_pool()
        job = await pool.enqueue_job("get_bot_status_task")
        result = await job.result(timeout=10)
        # Cache the result
        _bot_status_cache["data"] = result
        _bot_status_cache["timestamp"] = now
        return result
    except Exception as e:
        # Fallback: just return provider config
        try:
            ptype, _ = await config_service.get_provider_config("chat")
            fallback = {"running": None, "health": "unknown", "provider": ptype}
            _bot_status_cache["data"] = fallback
            _bot_status_cache["timestamp"] = now
            return fallback
        except Exception:
            error_result = {"running": False, "health": "error", "error": str(e)[:200]}
            _bot_status_cache["data"] = error_result
            _bot_status_cache["timestamp"] = now
            return error_result


@router.get("/providers")
async def get_available_providers():
    """Return registered providers per category."""
    return available_providers()


# ---------------------------------------------------------------------------
# Connection test endpoints
# ---------------------------------------------------------------------------

@router.post("/test/{category}")
async def test_connection(request: Request, category: str):
    """Test a provider connection. Returns HTML partial for HTMX, JSON otherwise."""
    # HTMX sends form-encoded data via hx-include; API callers send JSON.
    # Accept both gracefully — no FastAPI body param (it rejects form data).
    config: dict = {}
    content_type = request.headers.get("content-type", "")
    try:
        if "json" in content_type:
            config = await request.json()
        else:
            form = await request.form()
            config = dict(form)
    except Exception:
        config = {}

    if category == "database":
        result = await _test_database()
    elif category == "redis":
        result = await _test_redis()
    elif category == "chat":
        result = await _test_chat(config)
    elif category == "git":
        result = await _test_git(config)
    elif category == "llm":
        result = await _test_llm(config)
    else:
        raise HTTPException(400, f"Unknown test category: {category}")

    # Return HTML partial for HTMX requests
    if request.headers.get("HX-Request"):
        color = "green" if result.status == "ok" else "red"
        dot = "status-ok" if result.status == "ok" else "status-error"
        html = f'<div class="test-result flex items-center gap-2 text-sm mt-1"><span class="status-dot {dot}"></span><span class="text-{color}-700">{result.message}</span></div>'
        return HTMLResponse(html)

    return result


async def _test_database() -> TestResult:
    try:
        from openclow.models.base import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return TestResult(status="ok", message="PostgreSQL connected")
    except Exception as e:
        log.error("test_database_failed", error=str(e))
        return TestResult(status="error", message="Database connection failed")


async def _test_redis() -> TestResult:
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        await r.aclose()
        return TestResult(status="ok", message="Redis connected")
    except Exception as e:
        log.error("test_redis_failed", error=str(e))
        return TestResult(status="error", message="Redis connection failed")


async def _test_chat(config: dict) -> TestResult:
    provider_type = config.get("type", "")

    if provider_type == "telegram":
        token = config.get("token", "")
        if not token or "****" in token:
            # Masked or empty — load real token from DB (per-type storage)
            saved = await config_service.get_provider_config_by_type("chat", "telegram")
            if saved:
                token = saved.get("token", "")
        if not token:
            return TestResult(status="error", message="No Telegram token provided")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                data = resp.json()
                if data.get("ok"):
                    bot = data["result"]
                    bot_username = bot["username"]

                    # Send test message if a chat_id target is available
                    test_chat_id = config.get("test_chat_id", "")
                    if not test_chat_id:
                        # Find first allowed Telegram user to send test to
                        async with async_session() as session:
                            result = await session.execute(
                                select(User)
                                .where(User.chat_provider_type == "telegram", User.is_allowed == True)
                                .limit(1)
                            )
                            user = result.scalar_one_or_none()
                            if user:
                                test_chat_id = user.chat_provider_uid

                    if test_chat_id:
                        try:
                            msg_resp = await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={
                                    "chat_id": int(test_chat_id),
                                    "text": "THAG GROUP test message — connection verified!",
                                },
                            )
                            msg_data = msg_resp.json()
                            if msg_data.get("ok"):
                                return TestResult(
                                    status="ok",
                                    message=f"Connected as @{bot_username} — test message sent!",
                                    details={"bot_id": bot["id"], "username": bot_username},
                                )
                            else:
                                return TestResult(
                                    status="ok",
                                    message=f"Connected as @{bot_username} (message send failed: {msg_data.get('description', 'unknown')})",
                                    details={"bot_id": bot["id"], "username": bot_username},
                                )
                        except Exception:
                            pass  # Fall through to basic success

                    return TestResult(
                        status="ok",
                        message=f"Connected as @{bot_username} (no users configured — add a user to test messaging)",
                        details={"bot_id": bot["id"], "username": bot_username},
                    )
                return TestResult(status="error", message=data.get("description", "Invalid token"))
        except Exception as e:
            return TestResult(status="error", message=str(e))

    elif provider_type == "slack":
        saved = await config_service.get_provider_config_by_type("chat", "slack") or {}

        def _resolve(key):
            """Use provided value unless it's masked or empty, then fall back to saved."""
            val = config.get(key, "")
            if not val or "****" in val:
                return saved.get(key, "")
            return val

        bot_token = _resolve("bot_token")
        app_token = _resolve("app_token")
        signing_secret = _resolve("signing_secret")

        errors = []
        if not bot_token:
            errors.append("Bot Token (xoxb-...) is required")
        elif not bot_token.startswith("xoxb-"):
            errors.append("Bot Token must start with xoxb-")
        if not app_token:
            errors.append("App Token (xapp-...) is required for Socket Mode")
        elif not app_token.startswith("xapp-"):
            errors.append("App Token must start with xapp-")
        if not signing_secret:
            errors.append("Signing Secret is required")
        if errors:
            return TestResult(status="error", message="; ".join(errors))

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # 1. Verify bot token
                resp = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"},
                )
                data = resp.json()
                if not data.get("ok"):
                    return TestResult(status="error", message=f"Bot Token invalid: {data.get('error', 'unknown')}")

                team = data.get("team", "workspace")
                bot_id = data.get("bot_id", "bot")

                # 2. Check scopes by trying chat.postMessage (dry-run not possible, so check via conversations.list)
                resp2 = await client.get(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"},
                )
                scopes = resp2.headers.get("x-oauth-scopes", "")
                required = {"chat:write", "commands", "users:read", "channels:read"}
                have = {s.strip() for s in scopes.split(",")} if scopes else set()
                missing = required - have
                scope_warning = ""
                if missing:
                    scope_warning = f" (missing scopes: {', '.join(missing)})"

                # 3. Verify app token (try Socket Mode connection info)
                resp3 = await client.post(
                    "https://slack.com/api/apps.connections.open",
                    headers={"Authorization": f"Bearer {app_token}"},
                )
                app_data = resp3.json()
                if not app_data.get("ok"):
                    return TestResult(
                        status="error",
                        message=f"App Token invalid: {app_data.get('error', 'unknown')}. Enable Socket Mode in your Slack app settings.",
                    )

                # 4. Send a test message if test_channel is specified
                test_channel = config.get("test_channel", "")
                if test_channel:
                    try:
                        msg_resp = await client.post(
                            "https://slack.com/api/chat.postMessage",
                            headers={"Authorization": f"Bearer {bot_token}"},
                            json={
                                "channel": test_channel,
                                "text": "THAG GROUP test message — if you see this, the bot is connected! This message will be deleted shortly.",
                            },
                        )
                        msg_data = msg_resp.json()
                        if msg_data.get("ok"):
                            # Delete the test message after a short delay
                            ts = msg_data.get("ts", "")
                            if ts:
                                import asyncio
                                await asyncio.sleep(3)
                                await client.post(
                                    "https://slack.com/api/chat.delete",
                                    headers={"Authorization": f"Bearer {bot_token}"},
                                    json={"channel": test_channel, "ts": ts},
                                )
                            return TestResult(
                                status="ok",
                                message=f"Connected to {team} — test message sent to channel{scope_warning}",
                                details={"bot_id": bot_id, "team": team},
                            )
                        else:
                            ch_error = msg_data.get("error", "unknown")
                            return TestResult(
                                status="error",
                                message=f"Connected to {team} but failed to send test message: {ch_error}. Invite the bot to the channel first.",
                            )
                    except Exception as msg_err:
                        return TestResult(
                            status="error",
                            message=f"Connected to {team} but test message failed: {str(msg_err)[:100]}",
                        )

                return TestResult(
                    status="ok",
                    message=f"Connected to {team}{scope_warning}",
                    details={"bot_id": bot_id, "team": team, "scopes": scopes},
                )
        except Exception as e:
            return TestResult(status="error", message=str(e))

    return TestResult(status="error", message=f"Unknown chat provider: {provider_type}")


async def _test_git(config: dict) -> TestResult:
    provider_type = config.get("type", "")

    if provider_type == "github":
        token = config.get("token", "")
        if not token or "****" in token:
            saved = await config_service.get_provider_config_by_type("git", "github")
            if saved:
                token = saved.get("token", "")
        if not token:
            return TestResult(status="error", message="No GitHub token provided")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                )
                if resp.status_code == 200:
                    user = resp.json()
                    return TestResult(
                        status="ok",
                        message=f"Authenticated as {user['login']}",
                        details={"login": user["login"], "name": user.get("name")},
                    )
                return TestResult(status="error", message=f"GitHub API returned {resp.status_code}")
        except Exception as e:
            return TestResult(status="error", message=str(e))

    elif provider_type == "gitlab":
        return TestResult(status="error", message="GitLab integration coming soon")

    return TestResult(status="error", message=f"Unknown git provider: {provider_type}")


async def _test_llm(config: dict) -> TestResult:
    provider_type = config.get("type", "")

    if provider_type == "claude":
        try:
            from openclow.services import bot_actions
            job = await bot_actions.enqueue_job("claude_auth_check")
            result = await job.result(timeout=15)
            if result and result.get("loggedIn"):
                method = result.get("authMethod", "unknown")
                return TestResult(status="ok", message=f"Claude authenticated ({method})")
            else:
                return TestResult(status="error", message="Claude not authenticated — click Re-authenticate below")
        except Exception as e:
            return TestResult(status="error", message=f"Auth check failed: {str(e)[:100]}")

    elif provider_type == "openai":
        return TestResult(status="error", message="OpenAI integration coming soon")

    return TestResult(status="error", message=f"Unknown LLM provider: {provider_type}")


# ---------------------------------------------------------------------------
# Claude Auth
# ---------------------------------------------------------------------------

@router.get("/claude-auth-status")
async def claude_auth_status():
    """Get Claude authentication status — runs on worker via arq."""
    try:
        from openclow.services import bot_actions
        job = await bot_actions.enqueue_job("claude_auth_check")
        result = await job.result(timeout=15)
        return result or {"loggedIn": False, "error": "No response from worker"}
    except Exception as e:
        return {"loggedIn": False, "error": str(e)[:200]}


@router.post("/claude-auth-login")
async def claude_auth_login():
    """Start Claude login — fires long-running worker task, polls Redis for URL."""
    import asyncio
    import redis.asyncio as aioredis

    SESSION = "claude_auth:web"
    r = aioredis.from_url(settings.redis_url, decode_responses=True)

    # Clear stale session keys so the new task starts fresh
    await r.delete(f"{SESSION}:url", f"{SESSION}:code", f"{SESSION}:status")

    # Fire the long-running task without waiting for its final result
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await get_arq_pool()
        await pool.enqueue_job("claude_auth_login_web")
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}

    # Poll Redis until the subprocess publishes the URL (up to 15 s)
    for _ in range(30):
        url = await r.get(f"{SESSION}:url")
        if url:
            return {"status": "pending", "url": url}
        await asyncio.sleep(0.5)

    return {"status": "error", "message": "Failed to get auth URL — check worker logs"}


@router.post("/claude-auth-submit-code")
async def claude_auth_submit_code(request: Request):
    """Store the auth code in Redis so the worker task can forward it to the CLI.

    The CLI's HTTP callback server runs inside the worker container — only the
    worker can reach it.  The API just drops the code into Redis; the waiting
    claude_auth_login_web task picks it up and makes the callback locally.
    """
    import redis.asyncio as aioredis

    body = await request.json()
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code is required")

    SESSION = "claude_auth:web"
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    if not await r.get(f"{SESSION}:port"):
        raise HTTPException(status_code=400, detail="No active auth session — click 'Authenticate with Claude' first")

    await r.setex(f"{SESSION}:code", 120, code)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Setup status
# ---------------------------------------------------------------------------

@router.get("/setup-status")
async def setup_status():
    """Check which categories are configured."""
    configs = await config_service.get_all_config()
    categories = {"llm", "chat", "git"}
    configured = set()
    for key in configs:
        cat = key.split(".")[0]
        if cat in categories:
            configured.add(cat)

    # Count projects and users
    async with async_session() as session:
        project_count = (await session.execute(
            select(sa_func.count(Project.id)).where(Project.status == "active")
        )).scalar() or 0
        user_count = (await session.execute(
            select(sa_func.count(User.id)).where(User.is_allowed == True)
        )).scalar() or 0

    return {
        "is_complete": categories == configured and project_count > 0,
        "configured": list(configured),
        "missing": list(categories - configured),
        "project_count": project_count,
        "user_count": user_count,
    }


# ---------------------------------------------------------------------------
# Host-mode settings (where user apps live on the VPS host)
# ---------------------------------------------------------------------------


@router.get("/host")
async def get_host_settings():
    """Return all host-mode settings: projects_base, mode_default, auto_clone."""
    from openclow.services import config_service as _cs
    return await _cs.get_all_host_settings()


@router.put("/host")
async def update_host_settings(body: dict):
    """Update host-mode settings. Accepts any subset of:
    projects_base (str), mode_default ("docker"|"host"|"container"), auto_clone (bool)."""
    from openclow.services import config_service as _cs

    allowed = {"projects_base", "mode_default", "auto_clone"}
    bad = set(body) - allowed
    if bad:
        raise HTTPException(400, f"Unknown keys: {sorted(bad)}")

    # Accept "container" — spec 001 added per-chat container mode (see
    # bootstrap.py routing). Old check rejected it, so the Settings UI
    # couldn't pick container as the default-for-new-projects.
    if "mode_default" in body and body["mode_default"] not in ("docker", "host", "container"):
        raise HTTPException(400, "mode_default must be 'docker', 'host', or 'container'")

    if "projects_base" in body:
        path = (body["projects_base"] or "").strip()
        if not path or not path.startswith("/"):
            raise HTTPException(400, "projects_base must be an absolute path")

    for k, v in body.items():
        await _cs.set_host_setting(k, v)

    return {"status": "ok", **await _cs.get_all_host_settings()}


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------

@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects():
    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.status == "active").order_by(Project.id)
        )
        return result.scalars().all()


@router.get("/projects/github-repos")
async def list_github_repos():
    """List repos the configured GitHub PAT can see — owner + collaborator
    + org-member affiliated. Used by the Settings → Add Project modal so
    the user picks from a dropdown instead of typing `owner/repo` by hand.

    Defined BEFORE `/projects/{project_id}` routes so FastAPI doesn't
    interpret `github-repos` as a project_id path parameter and return
    405. (Static-segment routes must precede `{param}` routes that share
    the same prefix.)

    Reads `git/provider.github.token` from platform_config. Returns a
    flat list of `{full_name, default_branch, private, description}`
    sorted by full_name. Up to 300 repos (3 pages × 100).
    """
    cfg = await config_service.get_config("git", "provider.github")
    token = (cfg or {}).get("token") if cfg else None
    if not token:
        raise HTTPException(
            400,
            "git/provider.github not configured — add a GitHub token in "
            "platform_config first",
        )
    repos: list[dict] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        for page in (1, 2, 3):
            try:
                resp = await client.get(
                    "https://api.github.com/user/repos",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    params={
                        "per_page": 100, "page": page,
                        "sort": "full_name", "affiliation": "owner,collaborator,organization_member",
                    },
                )
            except httpx.HTTPError as e:
                raise HTTPException(502, f"GitHub request failed: {e}")
            if resp.status_code == 401:
                raise HTTPException(
                    401,
                    "GitHub token rejected — check git/provider.github in "
                    "platform_config",
                )
            if resp.status_code != 200:
                raise HTTPException(
                    502, f"GitHub returned {resp.status_code}: {resp.text[:200]}"
                )
            page_repos = resp.json()
            if not page_repos:
                break
            for r in page_repos:
                repos.append({
                    "full_name": r.get("full_name"),
                    "default_branch": r.get("default_branch") or "main",
                    "private": bool(r.get("private")),
                    "description": r.get("description"),
                })
            if len(page_repos) < 100:
                break
    repos.sort(key=lambda r: (r["full_name"] or "").lower())
    return {"repos": repos, "count": len(repos)}


async def _fetch_branches_by_repo(repo: str, current_default: str | None = None) -> list[dict]:
    """Shared GitHub branches fetcher — used by both the project-id route
    (Edit modal) and the repo-keyed route (Add modal, where no project
    row exists yet)."""
    cfg = await config_service.get_config("git", "provider.github")
    token = (cfg or {}).get("token") if cfg else None
    if not token:
        raise HTTPException(400, "git/provider.github not configured")
    branches: list[dict] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        for page in (1, 2, 3):
            try:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/branches",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    params={"per_page": 100, "page": page},
                )
            except httpx.HTTPError as e:
                raise HTTPException(502, f"GitHub request failed: {e}")
            if resp.status_code == 404:
                raise HTTPException(404, f"Repo {repo} not found or token lacks access")
            if resp.status_code != 200:
                raise HTTPException(502, f"GitHub {resp.status_code}: {resp.text[:200]}")
            page_data = resp.json()
            if not page_data:
                break
            for b in page_data:
                branches.append({
                    "name": b.get("name"),
                    "is_default": current_default is not None and b.get("name") == current_default,
                    "protected": bool(b.get("protected")),
                })
            if len(page_data) < 100:
                break
    branches.sort(key=lambda b: (b["name"] or "").lower())
    return branches


@router.get("/projects/branches")
async def list_branches_for_repo(repo: str):
    """Branches for an arbitrary `owner/name` repo. Used by the Add Project
    modal where no DB row exists yet — the user picks a repo first, then
    the Default Branch field populates from this. Defined BEFORE
    /projects/{project_id}/branches and /projects/{project_id} so the
    static `branches` segment doesn't get swallowed as a project_id."""
    if not repo or "/" not in repo:
        raise HTTPException(400, "repo query param must be 'owner/name'")
    branches = await _fetch_branches_by_repo(repo)
    return {"repo": repo, "branches": branches, "count": len(branches)}


@router.get("/projects/{project_id}/branches")
async def list_project_branches(project_id: int):
    """List branches on the project's GitHub repo using the configured PAT.

    Defined BEFORE the generic /projects/{project_id} PUT/DELETE so static
    sub-segments are matched first. Returns up to 100 branches sorted
    name-asc with the project's current default_branch flagged.
    """
    async with async_session() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        if not project.github_repo:
            raise HTTPException(400, "Project has no github_repo")
        repo = project.github_repo
        current_default = project.default_branch
    branches = await _fetch_branches_by_repo(repo, current_default)
    return {"repo": repo, "current_default": current_default,
            "branches": branches, "count": len(branches)}


@router.post("/projects", response_model=ProjectResponse)
async def create_project(body: ProjectCreate):
    from sqlalchemy.exc import IntegrityError

    async with async_session() as session:
        project = Project(**body.model_dump())
        session.add(project)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(400, f"Project '{body.name}' already exists")
        await session.refresh(project)
        return project


@router.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: int, body: ProjectUpdate):
    async with async_session() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(project, field, value)
        await session.commit()
        await session.refresh(project)
        return project


@router.delete("/projects/{project_id}")
async def delete_project(project_id: int):
    async with async_session() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        project.status = "inactive"
        await session.commit()
    return {"status": "ok", "message": f"Project '{project.name}' deactivated"}


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

@router.get("/users", response_model=list[UserResponse])
async def list_users():
    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.id))
        return result.scalars().all()


@router.post("/users", response_model=UserResponse)
async def create_user(body: UserCreate):
    # Default provider type to the currently active chat provider
    data = body.model_dump()
    if not data.get("chat_provider_type") or data["chat_provider_type"] == "telegram":
        try:
            ptype, _ = await config_service.get_provider_config("chat")
            data["chat_provider_type"] = ptype
        except Exception:
            pass  # Keep default
    async with async_session() as session:
        user = User(**data)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@router.delete("/users/{user_id}")
async def delete_user(user_id: int):
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        await session.delete(user)
        await session.commit()
    return {"status": "ok", "message": "User deleted"}


@router.patch("/users/{user_id}/allow")
async def toggle_user_allowed(user_id: int, body: dict):
    """Set is_allowed flag for a user."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        user.is_allowed = bool(body.get("is_allowed", False))
        await session.commit()
    return {"status": "ok", "is_allowed": user.is_allowed}


# ---------------------------------------------------------------------------
# Slack workspace discovery (members + channels)
# ---------------------------------------------------------------------------

@router.get("/slack/members")
async def list_slack_members():
    """Fetch workspace members from Slack API. Requires users:read scope."""
    config = await config_service.get_provider_config_by_type("chat", "slack") or {}

    bot_token = config.get("bot_token", "")
    if not bot_token:
        return {"ok": False, "error": "No Slack bot token configured"}

    # Get already-added user IDs so we can mark them
    existing_uids: set[str] = set()
    async with async_session() as session:
        result = await session.execute(
            select(User.chat_provider_uid).where(User.chat_provider_type == "slack")
        )
        existing_uids = {r[0] for r in result.all()}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://slack.com/api/users.list",
                headers={"Authorization": f"Bearer {bot_token}"},
            )
            data = resp.json()
            if not data.get("ok"):
                error = data.get("error", "unknown")
                if error == "missing_scope":
                    return {
                        "ok": False,
                        "error": "Bot needs users:read scope. Add it in your Slack app's OAuth & Permissions.",
                    }
                return {"ok": False, "error": error}

            members = []
            for m in data.get("members", []):
                if m.get("deleted") or m.get("is_bot") or m.get("id") == "USLACKBOT":
                    continue
                members.append({
                    "id": m["id"],
                    "name": m.get("name", ""),
                    "real_name": m.get("real_name", ""),
                    "avatar": m.get("profile", {}).get("image_48", ""),
                    "already_added": m["id"] in existing_uids,
                })
            return {"ok": True, "members": members}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/slack/channels")
async def list_slack_channels():
    """Fetch channels the bot is in. Requires channels:read + groups:read scopes."""
    config = await config_service.get_provider_config_by_type("chat", "slack") or {}

    bot_token = config.get("bot_token", "")
    if not bot_token:
        return {"ok": False, "error": "No Slack bot token configured"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://slack.com/api/conversations.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"types": "public_channel,private_channel", "limit": 200},
            )
            data = resp.json()
            if not data.get("ok"):
                error = data.get("error", "unknown")
                if error == "missing_scope":
                    return {
                        "ok": False,
                        "error": "Bot needs channels:read scope. Add it in your Slack app's OAuth & Permissions.",
                    }
                return {"ok": False, "error": error}

            channels = []
            for ch in data.get("channels", []):
                channels.append({
                    "id": ch["id"],
                    "name": ch.get("name", ""),
                    "is_member": ch.get("is_member", False),
                    "num_members": ch.get("num_members", 0),
                })
            return {"ok": True, "channels": channels}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/slack/channels-select")
async def slack_channels_select():
    """Return an HTML <select> partial with Slack channels for HTMX swap."""
    # Load Slack config specifically (works even when Slack isn't the active provider)
    config = await config_service.get_provider_config_by_type("chat", "slack") or {}
    saved_channel = config.get("default_channel", "")

    bot_token = config.get("bot_token", "")
    if not bot_token:
        return HTMLResponse('<p class="text-xs text-gray-400">Save Slack tokens first, then reload channels.</p>')

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://slack.com/api/conversations.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"types": "public_channel,private_channel", "limit": 200},
            )
            data = resp.json()
            if not data.get("ok"):
                error = data.get("error", "unknown")
                return HTMLResponse(f'<p class="text-xs text-red-500">Failed to load channels: {error}</p>')

            options = ['<option value="">Select a channel...</option>']
            for ch in sorted(data.get("channels", []), key=lambda c: c.get("name", "")):
                ch_id = ch["id"]
                ch_name = ch.get("name", ch_id)
                is_member = ch.get("is_member", False)
                is_private = ch.get("is_private", False)
                prefix = "🔒 " if is_private else "#"
                badge = " (bot joined)" if is_member else ""
                selected = " selected" if ch_id == saved_channel else ""
                options.append(f'<option value="{ch_id}"{selected}>{prefix}{ch_name}{badge}</option>')

            html = (
                f'<select id="default_channel" name="default_channel" class="form-input">{"".join(options)}</select>'
                f'<p class="text-xs text-gray-400 mt-1">Channel for system notifications and status updates</p>'
                f'<button type="button" class="mt-2 text-xs text-brand-600 hover:text-brand-700 font-medium" '
                f'hx-get="/api/settings/slack/channels-select" hx-target="#channel-select-wrapper" hx-swap="innerHTML">'
                f'Reload channels</button>'
            )
            return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f'<p class="text-xs text-red-500">Error: {_html.escape(str(e)[:100])}</p>')


@router.post("/channels")
async def link_channel(body: dict):
    """Link a chat channel to a project."""
    channel_id = body.get("channel_id", "").strip()
    project_id = body.get("project_id")
    provider_type = body.get("provider_type", "slack").strip().lower()
    if provider_type not in ("slack", "telegram"):
        raise HTTPException(400, "Invalid provider_type")
    if not channel_id or not project_id:
        raise HTTPException(400, "channel_id and project_id are required")

    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid project_id")

    # Look up project name
    async with async_session() as session:
        project = await session.get(Project, project_id)
        if not project or project.status != "active":
            raise HTTPException(404, "Project not found")

    from openclow.services.channel_service import set_channel_project
    channel_name = body.get("channel_name", channel_id)
    await set_channel_project(channel_id, project_id, project.name, provider_type=provider_type, channel_name=channel_name)

    # JSON response for React frontend
    if not request.headers.get("HX-Request"):
        return {
            "status": "ok",
            "binding": {
                "channel_id": channel_id,
                "channel_name": channel_name or channel_id,
                "project_id": project_id,
                "project_name": project.name,
                "provider_type": provider_type,
            },
        }

    # HTML row for HTMX swap (legacy dashboard)
    display_name = channel_name or channel_id
    prefix = "#" if provider_type == "slack" else "@"
    html = (
        f'<tr id="ch-{provider_type}-{channel_id}" class="border-b border-gray-100">'
        f'<td class="px-5 py-3"><span class="inline-flex items-center gap-1.5">'
        f'<span class="text-gray-400">{prefix}</span><span class="font-medium text-gray-900">{display_name}</span>'
        f'</span><span class="text-xs text-gray-400 ml-2">{channel_id}</span></td>'
        f'<td class="px-5 py-3 font-medium text-gray-700">{project.name}</td>'
        f'<td class="px-5 py-3 text-right">'
        f'<button class="text-red-500 hover:text-red-700 text-xs font-medium" '
        f'hx-delete="/api/settings/channels/{provider_type}/{channel_id}" '
        f'hx-confirm="Unlink {prefix}{display_name} from {project.name}?" '
        f'hx-target="#ch-{provider_type}-{channel_id}" hx-swap="outerHTML">Unlink</button></td></tr>'
    )
    return HTMLResponse(html)


@router.delete("/channels/{provider_type}/{channel_id}")
async def unlink_channel(provider_type: str, channel_id: str):
    from openclow.services.channel_service import unset_channel_project
    await unset_channel_project(channel_id, provider_type=provider_type)
    return HTMLResponse("")


@router.get("/chat/active-task-count")
async def active_task_count():
    """Return count of active (non-terminal) tasks per chat provider type."""
    from openclow.models.task import Task
    terminal_statuses = {"merged", "rejected", "discarded", "failed", "orphaned"}
    async with async_session() as session:
        result = await session.execute(
            select(Task.chat_provider_type, sa_func.count(Task.id))
            .where(~Task.status.in_(terminal_statuses))
            .group_by(Task.chat_provider_type)
        )
        counts = {row[0]: row[1] for row in result.all()}
    return counts


# ---------------------------------------------------------------------------
# New JSON-only endpoints for React settings panel
# ---------------------------------------------------------------------------

@router.get("/system-info")
async def system_info():
    """Return masked system configuration values for display."""
    def mask_url(url: str) -> str:
        if url and "@" in url:
            scheme, rest = (url.split("://", 1) + [""])[:2]
            host_part = rest.rsplit("@", 1)[-1]
            return f"{scheme}://****@{host_part}"
        return url or ""

    return {
        "database_url": mask_url(settings.database_url or ""),
        "redis_url": mask_url(settings.redis_url or ""),
        "workspace_base_path": settings.workspace_base_path or "",
        "log_level": settings.log_level or "INFO",
        "activity_log": settings.activity_log if hasattr(settings, "activity_log") else False,
    }


@router.get("/channel-bindings")
async def list_channel_bindings():
    """Return all channel-to-project bindings as JSON."""
    from openclow.services.channel_service import get_all_channel_bindings
    return await get_all_channel_bindings()


# ---------------------------------------------------------------------------
# Auth login (set cookie)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dev mode password
# ---------------------------------------------------------------------------

@router.put("/dev-password")
async def set_dev_password(body: dict):
    """Set or clear the Slack dev mode password."""
    password = body.get("password", "").strip()
    if password:
        await config_service.set_config("system", "dev_password", {"value": password})
        return {"status": "ok", "message": "Dev password set"}
    else:
        # Clear the password (disable dev mode)
        await config_service.set_config("system", "dev_password", {"value": ""})
        return {"status": "ok", "message": "Dev password cleared — dev mode disabled"}


@router.post("/login")
async def settings_login(body: dict):
    """Validate API key and return a cookie."""
    from openclow.api.auth import _get_api_key
    import secrets
    from fastapi.responses import JSONResponse

    api_key = _get_api_key()
    if not api_key:
        return {"status": "ok", "message": "Auth disabled — no API key configured"}

    provided = body.get("api_key", "")
    if not secrets.compare_digest(provided, api_key):
        raise HTTPException(401, "Invalid API key")

    response = JSONResponse({"status": "ok", "message": "Authenticated"})
    response.set_cookie(
        "settings_token",
        value=api_key,
        httponly=True,
        max_age=86400 * 7,  # 7 days
        samesite="lax",
    )
    return response
