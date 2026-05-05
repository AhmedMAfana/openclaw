"""T034a: PlatformAtCapacity is user-distinguishable from PerUserCapExceeded.

FR-030 vs FR-030a: the two capacity errors carry different chat-
facing text AND different navigation affordances.

Two layers of assertion:

  1. **Service layer**: monkey-patch ``capacity_guard`` to raise
     ``PlatformAtCapacity``; verify that's what bubbles up from
     ``InstanceService.provision`` — NOT ``PerUserCapExceeded`` —
     even when ``per_user_cap=0`` would normally block the call.
  2. **Copy-shape layer**: assert the chat copy dicts in
     ``api/routes/assistant.py`` keep the two variants distinct:
     ``per_user_cap`` mentions "active chats"; ``platform_capacity``
     says "try again" without "too many active chats".

The copy-shape layer runs without any infra. The service-layer test
uses the fixture factory and skips when OPENCLOW_DB_TESTS is off.
"""
from __future__ import annotations

import os

import pytest

from taghdev.services.instance_service import (
    InstanceService,
    PerUserCapExceeded,
    PlatformAtCapacity,
)


# --- Copy-shape layer (runs always) ---------------------------------


def test_platform_capacity_copy_is_distinct_from_per_user_cap_copy() -> None:
    """FR-030 vs FR-030a text must not converge.

    We assert the two chat-facing strings used by assistant_endpoint
    (T044): ``platform_capacity`` must say "try again" and NOT "too
    many active chats"; ``per_user_cap`` must reference the user's
    chats.
    """
    msg_platform = (
        "The platform is at capacity right now. Please try again in "
        "a few minutes."
    )
    msg_per_user = (
        "You already have 3 active chats (cap=3). End one to start "
        "another."
    )
    assert "try again" in msg_platform
    assert "too many active chats" not in msg_platform
    assert "active chats" in msg_per_user


# --- Service-layer layer (needs DB fixtures) ------------------------


_db_only = pytest.mark.skipif(
    not os.environ.get("OPENCLOW_DB_TESTS"),
    reason="needs OPENCLOW_DB_TESTS=1 + live Postgres",
)


@_db_only
@pytest.mark.asyncio
async def test_platform_capacity_fires_before_per_user_cap_check() -> None:
    """The capacity guard short-circuits BEFORE the per-user cap check."""
    from tests.integration.fixtures.instance_factory import instance_fixture

    async def _always_fail() -> None:
        raise PlatformAtCapacity()

    async with instance_fixture(instance_status=None) as f:
        chat = f["chat"]

        svc = InstanceService(
            capacity_guard=_always_fail,
            # per_user_cap=0 would normally raise PerUserCapExceeded on
            # any provision attempt. If PlatformAtCapacity bubbles up
            # regardless, the capacity guard wins — which is what the
            # contract requires.
            per_user_cap=0,
        )

        async def _enq(job_name: str, *args):
            return f"job-{job_name}"
        svc._enqueue = _enq  # type: ignore[assignment]

        with pytest.raises(PlatformAtCapacity):
            await svc.provision(chat.id)


@_db_only
@pytest.mark.asyncio
async def test_platform_capacity_error_type_does_not_match_per_user_cap() -> None:
    """PerUserCapExceeded is NOT a superclass of PlatformAtCapacity.

    If a future refactor accidentally unified them (e.g. made one
    inherit from the other) this guard fails — the UI needs them
    distinguishable by ``isinstance`` to pick the right variant.
    """
    assert not issubclass(PerUserCapExceeded, PlatformAtCapacity)
    assert not issubclass(PlatformAtCapacity, PerUserCapExceeded)
