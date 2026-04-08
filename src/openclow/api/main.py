"""FastAPI application — health checks, task status, activity log, and settings dashboard."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from openclow.api.routes import activity, health, settings, tasks

app = FastAPI(title="OpenClow API", version="0.1.0")


@app.on_event("shutdown")
async def shutdown_event():
    from openclow.models.base import dispose_engine
    await dispose_engine()

# Mount static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# JSON API routes
app.include_router(health.router)
app.include_router(tasks.router, prefix="/api")
app.include_router(activity.router, prefix="/api")
app.include_router(settings.router, prefix="/api")

# HTML page routes (settings dashboard)
from openclow.api.pages import router as pages_router  # noqa: E402
app.include_router(pages_router)
