"""HTML page routes for the Settings Dashboard (Jinja2 + HTMX)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from openclow.api.auth import verify_settings_auth
from openclow.models.base import async_session
from openclow.models.project import Project
from openclow.models.user import User
from openclow.providers.registry import available_providers, provider_schema
from openclow.services import config_service
from openclow.settings import settings

from sqlalchemy import select, func as sa_func

router = APIRouter(tags=["pages"], dependencies=[Depends(verify_settings_auth)])

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MASK_SENTINEL = "****"


def _mask_token(value: str) -> str:
    if not value or len(value) <= 10:
        return MASK_SENTINEL
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


def _mask_config(config: dict) -> dict:
    """Return a copy of config with sensitive fields masked for display."""
    sensitive_keys = {"token", "api_key", "bot_token", "app_token", "signing_secret"}
    masked = {}
    for k, v in config.items():
        if k in sensitive_keys and isinstance(v, str) and v:
            masked[k] = _mask_token(v)
        else:
            masked[k] = v
    return masked


async def _get_status_context() -> dict:
    """Build context data for the dashboard."""
    configs = await config_service.get_all_config()

    categories = {
        "llm": {"configured": False, "type": None, "config": {}},
        "chat": {"configured": False, "type": None, "config": {}},
        "git": {"configured": False, "type": None, "config": {}},
    }

    for key, val in configs.items():
        parts = key.split(".")
        cat = parts[0]
        if cat in categories:
            categories[cat]["configured"] = True
            # New format: "chat.provider.telegram" -> type is parts[2]
            if len(parts) == 3 and parts[1] == "provider":
                categories[cat]["type"] = parts[2]
            else:
                categories[cat]["type"] = val.get("type", "unknown")
            categories[cat]["config"] = val

    async with async_session() as session:
        project_count = (await session.execute(
            select(sa_func.count(Project.id)).where(Project.status == "active")
        )).scalar() or 0
        user_count = (await session.execute(
            select(sa_func.count(User.id)).where(User.is_allowed == True)
        )).scalar() or 0

    return {
        "categories": categories,
        "project_count": project_count,
        "user_count": user_count,
        "providers": available_providers(),
        "schemas": provider_schema(),
    }


# ---------------------------------------------------------------------------
# Dashboard Home
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
async def dashboard(request: Request):
    ctx = await _get_status_context()
    return templates.TemplateResponse(request, "settings/dashboard.html", {
        "active_page": "dashboard",
        **ctx,
    })


# ---------------------------------------------------------------------------
# Provider settings pages
# ---------------------------------------------------------------------------

async def _get_provider_config_safe(category: str) -> dict:
    """Get provider config — tries new per-type format, falls back to legacy."""
    try:
        ptype, config = await config_service.get_provider_config(category)
        config["type"] = ptype
        return config
    except (ValueError, Exception):
        # Fallback to legacy format
        return await config_service.get_config(category, "provider") or {}


@router.get("/settings/llm", response_class=HTMLResponse)
async def llm_settings(request: Request):
    config = await _get_provider_config_safe("llm")
    return templates.TemplateResponse(request, "settings/llm.html", {
        "active_page": "llm",
        "config": _mask_config(config),
        "providers": available_providers().get("llm", []),
        "schemas": provider_schema().get("llm", {}),
    })


@router.get("/settings/chat", response_class=HTMLResponse)
async def chat_settings(request: Request):
    config = await _get_provider_config_safe("chat")
    return templates.TemplateResponse(request, "settings/chat.html", {
        "active_page": "chat",
        "config": _mask_config(config),
        "providers": available_providers().get("chat", []),
        "schemas": provider_schema().get("chat", {}),
    })


@router.get("/settings/git", response_class=HTMLResponse)
async def git_settings(request: Request):
    config = await _get_provider_config_safe("git")
    return templates.TemplateResponse(request, "settings/git.html", {
        "active_page": "git",
        "config": _mask_config(config),
        "providers": available_providers().get("git", []),
        "schemas": provider_schema().get("git", {}),
    })


@router.get("/settings/system", response_class=HTMLResponse)
async def system_settings(request: Request):
    # Check if dev password is set
    dev_pw = await config_service.get_config("system", "dev_password")
    dev_password_set = bool(dev_pw and dev_pw.get("value"))

    return templates.TemplateResponse(request, "settings/system.html", {
        "active_page": "system",
        "dev_password_set": dev_password_set,
        "settings": {
            "database_url": settings.database_url,
            "redis_url": settings.redis_url,
            "workspace_base_path": settings.workspace_base_path,
            "log_level": settings.log_level,
            "activity_log": settings.activity_log,
        },
    })


# ---------------------------------------------------------------------------
# Project and User management pages
# ---------------------------------------------------------------------------

@router.get("/settings/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.status == "active").order_by(Project.id)
        )
        projects = result.scalars().all()
    return templates.TemplateResponse(request, "settings/projects.html", {
        "active_page": "projects",
        "projects": projects,
    })


@router.get("/settings/users", response_class=HTMLResponse)
async def users_page(request: Request):
    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.id))
        users = result.scalars().all()

    # Get current (active) chat provider type
    chat_config = await _get_provider_config_safe("chat")
    chat_type = chat_config.get("type", "telegram")

    # Detect ALL configured chat providers (even inactive)
    configured_chat_types = []
    for ptype in ("telegram", "slack"):
        cfg = await config_service.get_provider_config_by_type("chat", ptype)
        if cfg:
            configured_chat_types.append(ptype)
    if not configured_chat_types:
        configured_chat_types = [chat_type]

    return templates.TemplateResponse(request, "settings/users.html", {
        "active_page": "users",
        "users": users,
        "chat_type": chat_type,
        "configured_chat_types": configured_chat_types,
    })


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

@router.get("/settings/channels", response_class=HTMLResponse)
async def channels_page(request: Request):
    from openclow.services.channel_service import get_all_channel_bindings
    bindings = await get_all_channel_bindings()

    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.status == "active").order_by(Project.name)
        )
        projects = result.scalars().all()

    return templates.TemplateResponse(request, "settings/channels.html", {
        "active_page": "channels",
        "bindings": bindings,
        "projects": projects,
    })


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

@router.get("/settings/wizard", response_class=HTMLResponse)
async def wizard(request: Request):
    ctx = await _get_status_context()
    return templates.TemplateResponse(request, "settings/wizard.html", {
        "active_page": "wizard",
        **ctx,
    })


# ---------------------------------------------------------------------------
# HTMX partials — provider field fragments
# ---------------------------------------------------------------------------

@router.get("/settings/wizard/step/{step_name}", response_class=HTMLResponse)
async def wizard_step(request: Request, step_name: str):
    """Serve individual wizard step partials for HTMX swap."""
    valid_steps = ["step_llm", "step_chat", "step_git", "step_projects", "step_users", "step_review"]
    if step_name not in valid_steps:
        return HTMLResponse("<p class='text-red-500'>Invalid step</p>", status_code=400)
    return templates.TemplateResponse(request, f"partials/wizard_steps/{step_name}.html")


@router.get("/settings/partials/provider-fields/{category}/{provider_type}", response_class=HTMLResponse)
async def provider_fields_partial(request: Request, category: str, provider_type: str):
    schemas = provider_schema()
    fields = schemas.get(category, {}).get(provider_type, [])

    # Load per-type config so switching providers shows saved credentials
    config = await config_service.get_provider_config_by_type(category, provider_type) or {}

    # Determine if this is the "coming soon" type
    registered = available_providers().get(category, [])
    is_available = provider_type in registered

    return templates.TemplateResponse(request, "partials/provider_fields/generic.html", {
        "category": category,
        "provider_type": provider_type,
        "fields": fields,
        "config": _mask_config(config),
        "is_available": is_available,
    })


# ---------------------------------------------------------------------------
# Web Chat Routes (no auth)
# ---------------------------------------------------------------------------

chat_router = APIRouter(tags=["web_chat"])


@chat_router.get("/chat/login", response_class=HTMLResponse)
async def chat_login(request: Request):
    """Web chat login page."""
    return templates.TemplateResponse(request, "chat/login.html")


@chat_router.post("/chat/login")
async def chat_login_post(request: Request):
    """Handle web chat login."""
    from openclow.api.web_auth import hash_password, verify_password, create_web_token

    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()

    if not username or not password:
        return templates.TemplateResponse(
            request, "chat/login.html",
            {"error": "Username and password required"},
            status_code=400
        )

    # Look up user by username
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()

    if not user or not user.web_password_hash:
        return templates.TemplateResponse(
            request, "chat/login.html",
            {"error": "Invalid username or password"},
            status_code=401
        )

    # Verify password
    if not verify_password(password, user.web_password_hash):
        return templates.TemplateResponse(
            request, "chat/login.html",
            {"error": "Invalid username or password"},
            status_code=401
        )

    # Create token and redirect
    token = create_web_token(user.id)
    response = RedirectResponse(url="/chat", status_code=302)
    response.set_cookie("web_token", token, httponly=True, max_age=30*24*3600)
    return response


@chat_router.get("/chat/logout")
async def chat_logout():
    """Clear web chat session and redirect to login."""
    response = RedirectResponse(url="/chat/login", status_code=302)
    response.delete_cookie("web_token")
    return response


# NOTE: chat_router is exported separately and included by main.py as a
# top-level router so the auth dep on `router` (verify_settings_auth) does
# NOT cascade onto /chat/login. Including chat_router here as a sub-router
# makes login require admin auth — which is impossible (chicken/egg).
