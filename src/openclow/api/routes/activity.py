"""Activity log API — query, stats, and SSE streaming."""

import asyncio
import json
import os
import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from openclow.api.auth import verify_settings_auth
from openclow.services.activity_log import query, stats, tail
from openclow.settings import settings

router = APIRouter(dependencies=[Depends(verify_settings_auth)])


@router.get("/activity/stats")
async def activity_stats():
    """Dashboard stats — event counts, errors, task breakdown."""
    return stats()


@router.get("/activity/tail")
async def activity_tail(n: int = Query(default=50, ge=1, le=500)):
    """Last N events."""
    return tail(n)


@router.get("/activity/query")
async def activity_query(
    type: str = Query(default="", description="Event type filter"),
    last_n: int = Query(default=50, le=1000),
    since_minutes: int = Query(default=0, ge=0, description="Events from last N minutes"),
    task_id: str = Query(default="", description="Filter by task_id"),
    agent: str = Query(default="", description="Filter by agent name"),
):
    """Query activity log with filters."""
    since_ts = time.time() - (since_minutes * 60) if since_minutes else 0
    filters = {}
    if task_id:
        filters["task_id"] = task_id
    if agent:
        filters["agent"] = agent
    return query(event_type=type, last_n=last_n, since_ts=since_ts, filters=filters)


@router.get("/activity/stream")
async def activity_stream(
    type: str = Query(default="", description="Comma-separated event-type allowlist"),
    slug: str = Query(default="", description="Filter to events whose payload carries this instance slug"),
):
    """Server-Sent Events stream — real-time tail of the activity log.

    Both filters are applied server-side so non-matching events are dropped
    before the network. ``type`` accepts a comma-separated list (e.g.
    ``instance_status,instance_action``) so a single connection can multiplex
    related event families. ``slug`` is exact-match on the entry's ``slug``
    field — used by the per-instance detail view to subscribe only to its
    own events. (Spec 003 — see contracts/sse-events.md.)
    """
    type_set = {t for t in (s.strip() for s in type.split(",")) if t}

    async def event_generator():
        try:
            # Wait for log file to exist
            while not os.path.exists(settings.activity_log):
                await asyncio.sleep(1)

            with open(settings.activity_log) as f:
                # Seek to end
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if not line:
                        await asyncio.sleep(0.5)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if type_set and entry.get("type") not in type_set:
                        continue
                    if slug and entry.get("slug") != slug:
                        continue
                    yield f"data: {json.dumps(entry)}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/activity/types")
async def activity_types():
    """List all event types seen in the log."""
    entries = query(last_n=999999)
    types = set()
    for e in entries:
        types.add(e.get("type", "unknown"))
    return sorted(types)
