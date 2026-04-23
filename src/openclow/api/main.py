"""FastAPI application — health checks, task status, activity log, and settings dashboard."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from openclow.api.routes import activity, health, settings, tasks, assistant, threads, plans, ws, actions, access, instances

app = FastAPI(title="THAG GROUP API", version="0.1.0")

# Trigger provider registration so registry.available_providers() works.
# Factory imports all concrete providers (telegram, slack, claude, github, etc.)
import openclow.providers.factory  # noqa: F401, E402


@app.on_event("shutdown")
async def shutdown_event():
    from openclow.models.base import dispose_engine
    await dispose_engine()

@app.get("/")
async def root():
    return RedirectResponse(url="/settings")


# JSON API routes
app.include_router(health.router)
app.include_router(tasks.router, prefix="/api")
app.include_router(activity.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
# Web chat routes
app.include_router(assistant.router)
app.include_router(threads.router)
app.include_router(plans.router)
app.include_router(ws.router)
app.include_router(actions.router)
app.include_router(access.router)
app.include_router(instances.router)

# HTML page routes
# - pages_router: /settings* — admin-only via verify_settings_auth dep
# - chat_router:  /chat/login, /chat/logout — public (no auth dep) so users
#   can actually log in. Mounted as a top-level router to avoid the
#   verify_settings_auth dep cascading from pages_router.
from openclow.api.pages import router as pages_router, chat_router  # noqa: E402
app.include_router(chat_router)
app.include_router(pages_router)

# Mount static files (AFTER routes so /chat/login etc. work)
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Mount web chat frontend (built React app from Vite)
# Note: This must come AFTER the pages_router so /chat/login, /chat/api etc. can be routed first
chat_frontend_dir = Path(__file__).parent.parent.parent.parent / "chat_frontend" / "dist"
if chat_frontend_dir.exists():
    app.mount("/chat", StaticFiles(directory=str(chat_frontend_dir), html=True), name="chat")
