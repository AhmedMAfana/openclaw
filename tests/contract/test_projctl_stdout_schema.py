"""T029: validate projctl's stdout JSON-line events against the schema.

Schema: specs/001-per-chat-instances/contracts/projctl-stdout.schema.json.

This is a **contract test** — it asserts that representative events (the
ones projctl actually emits from events.go) conform to the schema's
allOf branches. When projctl ships a real binary (via CI), a follow-up
test will run `projctl up` end-to-end and pipe every stdout line through
this validator; for now we validate hand-built fixture events that mirror
what events.go produces.
"""
from __future__ import annotations

import json
import pathlib

import pytest


try:
    import jsonschema  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    jsonschema = None


SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "specs/001-per-chat-instances/contracts/projctl-stdout.schema.json"
)


@pytest.fixture(scope="module")
def schema():
    if jsonschema is None:
        pytest.skip("jsonschema package not installed")
    return json.loads(SCHEMA_PATH.read_text())


def _common() -> dict:
    return {
        "at": "2026-04-23T14:22:01.123456Z",
        "projctl_version": "0.1.0-dev",
        "instance_slug": "inst-0123456789abcd",
    }


def test_step_start_valid(schema) -> None:
    ev = {**_common(), "event": "step_start", "step": "install-php"}
    jsonschema.validate(ev, schema)


def test_step_success_requires_exit_code_zero(schema) -> None:
    ev = {
        **_common(),
        "event": "step_success",
        "step": "install-php",
        "attempt": 1,
        "exit_code": 0,
    }
    jsonschema.validate(ev, schema)


def test_step_success_rejects_nonzero_exit_code(schema) -> None:
    ev = {
        **_common(),
        "event": "step_success",
        "step": "install-php",
        "attempt": 1,
        "exit_code": 1,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(ev, schema)


def test_step_failure_requires_nonzero_exit_code(schema) -> None:
    ok = {
        **_common(),
        "event": "step_failure",
        "step": "install-php",
        "attempt": 2,
        "exit_code": 1,
    }
    jsonschema.validate(ok, schema)

    bad = {**ok, "exit_code": 0}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_success_check_shape(schema) -> None:
    ev = {
        **_common(),
        "event": "success_check",
        "step": "install-php",
        "success_check_cmd": "test -d /app/vendor",
        "success_check_passed": True,
    }
    jsonschema.validate(ev, schema)


def test_llm_action_shape(schema) -> None:
    ev = {
        **_common(),
        "event": "llm_action",
        "step": "install-php",
        "attempt": 1,
        "llm_action": {
            "action": "shell_cmd",
            "payload": "composer clear-cache",
            "reason": "cache likely stale",
        },
    }
    jsonschema.validate(ev, schema)


def test_llm_action_invalid_action_rejected(schema) -> None:
    ev = {
        **_common(),
        "event": "llm_action",
        "step": "install-php",
        "attempt": 1,
        "llm_action": {"action": "invalid"},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(ev, schema)


def test_heartbeat(schema) -> None:
    jsonschema.validate({**_common(), "event": "heartbeat"}, schema)


def test_doctor_result(schema) -> None:
    ev = {
        **_common(),
        "event": "doctor_result",
        "doctor": {
            "healthy": True,
            "checks": [
                {"name": "guide_parses", "ok": True},
                {"name": "state_present", "ok": True},
            ],
        },
    }
    jsonschema.validate(ev, schema)


def test_fatal_requires_reason(schema) -> None:
    jsonschema.validate(
        {**_common(), "event": "fatal", "fatal_reason": "guide parse error"},
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**_common(), "event": "fatal"}, schema)


def test_slug_pattern_enforced(schema) -> None:
    bad = {**_common(), "event": "step_start", "step": "foo"}
    bad["instance_slug"] = "inst-SHORT"   # not 14 hex chars → rejected
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_unknown_event_rejected(schema) -> None:
    bad = {**_common(), "event": "made_up_event", "step": "foo"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
