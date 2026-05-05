"""WebSocket endpoint — bridges Redis pub/sub to browser for worker progress updates."""
from __future__ import annotations

import asyncio
import json

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from taghdev.api.web_auth import verify_web_token
from taghdev.settings import settings
from taghdev.utils.logging import get_logger

log = get_logger()

router = APIRouter(tags=["websocket"])

# Ping every 30s — keeps the connection alive through nginx/cloudflare proxies
# that drop idle connections after 60-120s.
_WS_PING_INTERVAL = 30


@router.websocket("/api/ws/{user_id}/{session_id}")
async def ws_worker_updates(websocket: WebSocket, user_id: int, session_id: int):
    """Subscribe to Redis pub/sub channel for a web chat session and forward events to the browser.

    Auth: reads the web_token cookie from the WS handshake, validates JWT,
    and confirms the token's user_id matches the URL parameter.

    Sends periodic pings to prevent intermediate proxies from dropping the connection.
    """
    token = websocket.cookies.get("web_token")
    if not token:
        await websocket.close(code=4401)
        return

    user = await verify_web_token(token)
    if not user or user.id != user_id:
        await websocket.close(code=4403)
        return

    await websocket.accept()

    r = aioredis.from_url(settings.redis_url)
    pubsub = r.pubsub()
    channel = f"wc:{user_id}:{session_id}"
    await pubsub.subscribe(channel)
    log.info("ws.connected", user_id=user_id, session_id=session_id, channel=channel)

    async def _ping_loop():
        """Send periodic pings so proxies don't drop the connection."""
        try:
            while True:
                await asyncio.sleep(_WS_PING_INTERVAL)
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    ping_task = asyncio.create_task(_ping_loop())

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                await websocket.send_text(data)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        log.warning("ws.error", user_id=user_id, session_id=session_id, error=str(e))
    finally:
        ping_task.cancel()
        await pubsub.unsubscribe(channel)
        await r.aclose()
        log.info("ws.disconnected", user_id=user_id, session_id=session_id)
