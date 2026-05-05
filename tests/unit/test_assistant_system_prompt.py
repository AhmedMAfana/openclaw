"""Senior-DevOps refactor — Change 1: assert the system prompt carries
the CONTAINER mode branch, the anti-kiosk rule, and the senior-DevOps
reflex clause.

These strings are load-bearing for the chat persona: without them the
LLM falls back to legacy host/docker vocabulary and the kiosk-bullet
greeting that the refactor is specifically eliminating.
"""
from __future__ import annotations

from taghdev.api.routes.assistant import _build_system_prompt


_KW = dict(
    current_project="acme-app",
    tunnel_display="not running",
    chat_id="web:1:1",
    asst_msg_id="42",
    context_str="(no context)",
    conv_str="",
    skip_planning_val=False,
)


def test_prompt_contains_container_mode_branch():
    out = _build_system_prompt(
        mode_label="CONTAINER",
        git_mode_label="session_branch",
        **_KW,
    )
    assert "CONTAINER-MODE PROJECT" in out
    assert "instance_status()" in out
    assert "provision_now()" in out
    assert "terminate_now()" in out


def test_prompt_forbids_kiosk_bullets():
    out = _build_system_prompt(
        mode_label="CONTAINER",
        git_mode_label="session_branch",
        **_KW,
    )
    assert "NEVER respond" in out
    assert "kiosk" in out.lower()
    # Wording may wrap across lines; collapse whitespace before checking.
    flat = " ".join(out.split())
    assert "bullet menus" in flat


def test_prompt_carries_senior_reflex_clause():
    out = _build_system_prompt(
        mode_label="CONTAINER",
        git_mode_label="session_branch",
        **_KW,
    )
    assert "SENIOR-DEVOPS REFLEX" in out
    # The reflex must surface env state on every greeting (live URL,
    # spinning up, idle) and forbid the context-free "how can I help?"
    # response that the kiosk-bullet refactor is killing.
    assert "Never reply with a context-free" in out
    assert "URL is the user's primary handle" in out


def test_prompt_works_for_legacy_host_mode_too():
    """The container branch is additive — host/docker mode still has its own."""
    out = _build_system_prompt(
        mode_label="HOST",
        git_mode_label="session_branch",
        **_KW,
    )
    assert "HOST-MODE PROJECT" in out
    assert "FOCUSED PROJECT" in out
    # Still carries the anti-kiosk rule for ALL modes.
    assert "kiosk" in out.lower()
