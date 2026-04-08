"""Settings API — config CRUD, connection tests, project/user management."""
from __future__ import annotations

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
    await config_service.set_config(category, "provider", config_dict)

    # Reset cached provider instances so next call picks up new config
    from openclow.providers.factory import reset
    await reset()

    return {"status": "ok", "message": f"{category} provider updated to '{body.type}'"}


@router.get("/providers")
async def get_available_providers():
    """Return registered providers per category."""
    return available_providers()


# ---------------------------------------------------------------------------
# Connection test endpoints
# ---------------------------------------------------------------------------

@router.post("/test/{category}")
async def test_connection(request: Request, category: str, body: dict | None = None):
    """Test a provider connection. Returns HTML partial for HTMX, JSON otherwise."""
    if category == "database":
        result = await _test_database()
    elif category == "redis":
        result = await _test_redis()
    elif category == "chat":
        result = await _test_chat(body or {})
    elif category == "git":
        result = await _test_git(body or {})
    elif category == "llm":
        result = await _test_llm(body or {})
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
        if not token:
            # Try loading from DB
            saved = await config_service.get_config("chat", "provider")
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
                    return TestResult(
                        status="ok",
                        message=f"Connected as @{bot['username']}",
                        details={"bot_id": bot["id"], "username": bot["username"]},
                    )
                return TestResult(status="error", message=data.get("description", "Invalid token"))
        except Exception as e:
            return TestResult(status="error", message=str(e))

    elif provider_type == "slack":
        return TestResult(status="error", message="Slack integration coming soon")

    return TestResult(status="error", message=f"Unknown chat provider: {provider_type}")


async def _test_git(config: dict) -> TestResult:
    provider_type = config.get("type", "")

    if provider_type == "github":
        token = config.get("token", "")
        if not token:
            saved = await config_service.get_config("git", "provider")
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
            import claude_agent_sdk  # noqa: F401
            return TestResult(status="ok", message="Claude Agent SDK available")
        except ImportError:
            return TestResult(status="error", message="claude-agent-sdk is not installed")

    elif provider_type == "openai":
        return TestResult(status="error", message="OpenAI integration coming soon")

    return TestResult(status="error", message=f"Unknown LLM provider: {provider_type}")


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
# Project CRUD
# ---------------------------------------------------------------------------

@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects():
    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.status == "active").order_by(Project.id)
        )
        return result.scalars().all()


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
    async with async_session() as session:
        user = User(**body.model_dump())
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


# ---------------------------------------------------------------------------
# Auth login (set cookie)
# ---------------------------------------------------------------------------

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
