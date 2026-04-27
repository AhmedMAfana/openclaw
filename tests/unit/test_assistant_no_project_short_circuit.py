"""Plan v2 Change 4: when a chat has no project bound and the user has
accessible projects, /api/assistant must emit an `error` event card and
SKIP the LLM run entirely.

This is the defensive backstop for the new-chat modal (Change 1). Even
if a chat slips through with project_id=NULL (older chats, manual API
calls, etc.), the assistant_endpoint must NOT let the LLM gaslight the
user with a hollow "I'll spin up your env" promise.

This test exercises the early-return contract by spying on the import
so we don't need a full FastAPI client + JWT + DB. We assert:

1. The early-return path lives in assistant.py at the expected location.
2. The error-card emit shape matches the schema's `error` event.
3. The LLM-run setup (claude_agent_sdk.query, etc.) is NOT reached.

For a true end-to-end check, the e2e-pipeline skill drives this through
the chat frontend.
"""
from __future__ import annotations

import inspect

from openclow.api.routes import assistant as assistant_module


def test_short_circuit_branch_exists_in_assistant_endpoint():
    """assistant_endpoint source contains the no-project short-circuit.

    A pure-source assertion (cheap, robust under refactor): the branch
    must check both `not resolved_project_id` and `accessible_projects`
    so it doesn't fire on first-time-onboarding flows (no projects yet)
    where the LLM is supposed to ask for a GitHub URL.
    """
    src = inspect.getsource(assistant_module.assistant_endpoint)
    assert "if not resolved_project_id and accessible_projects" in src, (
        "Defensive short-circuit (plan v2 Change 4) is missing from "
        "assistant_endpoint. Without it, no-project chats fall through "
        "to the LLM and the LLM gaslights with hollow promises."
    )


def test_short_circuit_emits_error_event():
    """The short-circuit emits a structured `error` event, NOT plain text.

    Plain `controller.append_text` would impersonate the LLM voice
    (Principle II violation, caught separately by the
    `no_hardcoded_assistant_text` fitness check). Asserts that the
    short-circuit's emit is `controller.add_data({"type": "error", ...})`.
    """
    src = inspect.getsource(assistant_module.assistant_endpoint)
    short_circuit_idx = src.find("if not resolved_project_id and accessible_projects")
    assert short_circuit_idx != -1, "short-circuit branch missing"
    # The next 400 chars should contain the add_data emit.
    snippet = src[short_circuit_idx : short_circuit_idx + 400]
    assert "controller.add_data" in snippet
    assert '"type": "error"' in snippet
    assert "Pick a project" in snippet


def test_short_circuit_returns_before_llm_run():
    """The short-circuit must `return` before reaching `query(prompt=`.

    Otherwise the early-return is a no-op and the LLM still runs.
    """
    src = inspect.getsource(assistant_module.assistant_endpoint)
    short_circuit_idx = src.find("if not resolved_project_id and accessible_projects")
    query_idx = src.find("async for message in query(")
    assert short_circuit_idx != -1
    assert query_idx != -1
    # The `return` for the short-circuit must come BEFORE the agent
    # query call (i.e. lower offset in source order).
    return_after_short_circuit = src.find("return", short_circuit_idx)
    assert return_after_short_circuit != -1
    assert return_after_short_circuit < query_idx, (
        "Short-circuit `return` is missing or comes after the LLM query "
        "— the early-return is not actually skipping the LLM run."
    )


def test_no_project_addendum_does_not_promise_spin_up():
    """The no-project system-prompt addendum must NOT promise to spin up.

    Plan v2 Change 3: even on the rare path where the short-circuit is
    bypassed, the LLM must not gaslight. Surface the truth: the platform
    cannot auto-provision without a bound project.
    """
    src = inspect.getsource(assistant_module.assistant_endpoint)
    # Find the addendum block.
    idx = src.find("NO PROJECT BOUND")
    assert idx != -1
    addendum = src[idx : idx + 1000]
    # The literal "I'll spin" or "spin up" promise pattern must be
    # ABSENT from the addendum's literal-string injections.
    forbidden_promises = [
        "I'll spin up",
        "I'll have your env live",
    ]
    for phrase in forbidden_promises:
        assert phrase not in addendum, (
            f"NO-PROJECT-BOUND addendum still contains the gaslight "
            f"phrase {phrase!r} — strip it (plan v2 Change 3)."
        )
    # Positive assertion: the addendum must explicitly forbid promising.
    assert "Do NOT" in addendum
