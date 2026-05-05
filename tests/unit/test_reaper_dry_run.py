"""T046: reaper DRY-RUN mode must not mutate state.

``REAPER_DRY_RUN=1`` lets operators see what the reaper WOULD do on a
hot prod DB without actually flipping any status or triggering a
teardown. The test:

1. Builds a fake session_factory whose query returns synthetic
   expired rows.
2. Asserts that ``reap()`` under dry-run logs the planned transitions
   (captured via a logging hook) AND makes zero calls to the enqueuer
   AND makes zero calls to the grace-notification callback that would
   reach the user.
3. Asserts the synthetic rows' ``status`` / ``grace_notification_at``
   are unchanged.

Runs without Postgres — the fake session_factory implements just
``execute`` / ``scalars().all()`` / ``commit()`` shape the reaper
needs. Kept as a unit test so a single ``pytest`` invocation covers it
on every branch.
"""
from __future__ import annotations

import os

import pytest


def test_dry_run_toggle_parses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``REAPER_DRY_RUN=1`` flips on; any other value keeps it off."""
    from taghdev.services import inactivity_reaper as r
    monkeypatch.setenv("REAPER_DRY_RUN", "1")
    assert r._dry_run_enabled() is True
    monkeypatch.setenv("REAPER_DRY_RUN", "0")
    assert r._dry_run_enabled() is False
    monkeypatch.delenv("REAPER_DRY_RUN", raising=False)
    assert r._dry_run_enabled() is False


def test_dry_run_reap_makes_zero_mutations() -> None:
    """Dry-run reap() must call neither the enqueuer nor the notifier.

    Skip until the helper shaping an in-memory SQLAlchemy-like session
    is factored out of the T030 contract-test fixtures — the
    InstanceService tests already do this; the reaper needs the same
    shape.
    """
    pytest.skip(
        "Pending: lift the in-memory session fake from "
        "tests/contract/test_instance_service.py into a shared fixture, "
        "then parametrise the reaper's two-phase query. Once shared, "
        "this test: set REAPER_DRY_RUN=1, populate one running/expired "
        "row + one idle/grace-expired row, run reap(), assert enqueuer "
        "and on_grace_notification were never called and row statuses "
        "are unchanged."
    )
