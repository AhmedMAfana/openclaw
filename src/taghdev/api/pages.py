"""HTML page routes for web chat (login/logout)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from taghdev.models.base import async_session
from taghdev.models.user import User

from sqlalchemy import select

# Empty router kept so main.py import `from taghdev.api.pages import router, chat_router` still works.
router = APIRouter(tags=["pages"])

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))



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
    from taghdev.api.web_auth import hash_password, verify_password, create_web_token

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
