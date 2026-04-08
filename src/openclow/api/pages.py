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

def _mask_token(value: str) -> str:
    if len(value) <= 10:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


async def _get_status_context() -> dict:
    """Build context data for the dashboard."""
    configs = await config_service.get_all_config()

    categories = {
        "llm": {"configured": False, "type": None, "config": {}},
        "chat": {"configured": False, "type": None, "config": {}},
        "git": {"configured": False, "type": None, "config": {}},
    }

    for key, val in configs.items():
        cat = key.split(".")[0]
        if cat in categories:
            categories[cat]["configured"] = True
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

@router.get("/settings/llm", response_class=HTMLResponse)
async def llm_settings(request: Request):
    config = await config_service.get_config("llm", "provider") or {}
    return templates.TemplateResponse(request, "settings/llm.html", {
        "active_page": "llm",
        "config": config,
        "providers": available_providers().get("llm", []),
        "schemas": provider_schema().get("llm", {}),
    })


@router.get("/settings/chat", response_class=HTMLResponse)
async def chat_settings(request: Request):
    config = await config_service.get_config("chat", "provider") or {}
    return templates.TemplateResponse(request, "settings/chat.html", {
        "active_page": "chat",
        "config": config,
        "providers": available_providers().get("chat", []),
        "schemas": provider_schema().get("chat", {}),
    })


@router.get("/settings/git", response_class=HTMLResponse)
async def git_settings(request: Request):
    config = await config_service.get_config("git", "provider") or {}
    return templates.TemplateResponse(request, "settings/git.html", {
        "active_page": "git",
        "config": config,
        "providers": available_providers().get("git", []),
        "schemas": provider_schema().get("git", {}),
    })


@router.get("/settings/system", response_class=HTMLResponse)
async def system_settings(request: Request):
    return templates.TemplateResponse(request, "settings/system.html", {
        "active_page": "system",
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

    # Get current chat provider type for labels
    chat_config = await config_service.get_config("chat", "provider") or {}
    chat_type = chat_config.get("type", "telegram")

    return templates.TemplateResponse(request, "settings/users.html", {
        "active_page": "users",
        "users": users,
        "chat_type": chat_type,
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
    config = await config_service.get_config(category, "provider") or {}

    # Determine if this is the "coming soon" type
    registered = available_providers().get(category, [])
    is_available = provider_type in registered

    return templates.TemplateResponse(request, "partials/provider_fields/generic.html", {
        "category": category,
        "provider_type": provider_type,
        "fields": fields,
        "config": config,
        "is_available": is_available,
    })
