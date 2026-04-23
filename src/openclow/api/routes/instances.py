"""Per-chat instance endpoints for the web UI.

Spec: specs/001-per-chat-instances/tasks.md T043; FR-030b (per-user cap
error UI needs the list of active chat IDs so the user can navigate back
to one of them to end their session).

All routes are authenticated via ``web_user_dep``. A user can only list
their own instances; admins see any user by supplying ``user_id`` in the
path.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from openclow.api.web_auth import web_user_dep
from openclow.models.user import User
from openclow.services.instance_service import InstanceService

router = APIRouter(prefix="/api", tags=["instances"])


@router.get("/users/{user_id}/instances")
async def list_user_instances(
    user_id: int,
    user: User = Depends(web_user_dep),
) -> dict:
    """Return the active instances owned by ``user_id``.

    Shape (one entry per active instance):

    ```json
    {
      "instances": [
        {
          "id": "<uuid>",
          "slug": "inst-<hex>",
          "chat_session_id": 42,
          "project_id": 7,
          "status": "running",
          "web_hostname": "inst-<hex>.dev.<domain>" | null,
          "started_at": "2026-04-23T12:34:56Z" | null,
          "last_activity_at": "2026-04-23T13:00:00Z",
          "expires_at": "2026-04-24T13:00:00Z"
        }
      ]
    }
    ```

    Non-admin users can only see their own instances — a 403 is returned
    if ``user_id`` is not the caller. This is the backing API for the
    ``PerUserCapExceeded`` chat card (FR-030b), so the path includes the
    user id explicitly rather than deriving it from the JWT, keeping the
    shape admin-ready.
    """
    if user.id != user_id and not user.is_admin:
        raise HTTPException(status_code=403, detail="forbidden")

    rows = await InstanceService().list_active(user_id=user_id)
    payload = []
    for inst in rows:
        # Tunnels are loaded eagerly via `selectin` (see Instance.tunnels);
        # pick the first non-destroyed one for the preview URL surface.
        web_host: str | None = None
        for t in getattr(inst, "tunnels", []) or []:
            if t.status != "destroyed":
                web_host = t.web_hostname
                break
        payload.append({
            "id": str(inst.id),
            "slug": inst.slug,
            "chat_session_id": inst.chat_session_id,
            "project_id": inst.project_id,
            "status": inst.status,
            "web_hostname": web_host,
            "started_at": inst.started_at.isoformat() if inst.started_at else None,
            "last_activity_at": inst.last_activity_at.isoformat(),
            "expires_at": inst.expires_at.isoformat(),
        })
    return {"instances": payload}
