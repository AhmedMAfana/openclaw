"""Spec 003 — log endpoint redaction & shape guards."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from taghdev.api.serializers.admin_instance import to_log_line


def test_log_line_message_redacts_bearer_token():
    raw = {
        "ts": datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc).timestamp(),
        "type": "instance_log",
        "level": "info",
        "message": "calling https://api.example.com Authorization: Bearer abc.def.ghi-jwt-token",
        "slug": "inst-aaaaaaaaaaaaaa",
    }
    line = to_log_line(raw)
    assert "abc.def.ghi-jwt-token" not in line.message
    assert "[REDACTED]" in line.message


def test_log_line_context_redacts_secret_in_nested_dict():
    raw = {
        "ts": datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc).timestamp(),
        "type": "instance_log",
        "level": "warning",
        "message": "build step failed",
        "slug": "inst-aaaaaaaaaaaaaa",
        "env": {"GITHUB_TOKEN": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "BUILD_ID": "42"},
    }
    line = to_log_line(raw)
    blob = json.dumps(line.model_dump(mode="json"))
    assert "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in blob


def test_log_line_preserves_non_secret_context_keys():
    raw = {
        "ts": datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc).timestamp(),
        "type": "instance_log",
        "level": "info",
        "message": "phase done",
        "slug": "inst-aaaaaaaaaaaaaa",
        "step_index": 3,
        "elapsed_ms": 1234,
    }
    line = to_log_line(raw)
    assert line.context.get("step_index") == 3
    assert line.context.get("elapsed_ms") == 1234


def test_log_line_drops_control_keys_from_context():
    raw = {
        "ts": datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc).timestamp(),
        "type": "instance_log",
        "level": "info",
        "message": "phase",
        "slug": "inst-aaaaaaaaaaaaaa",
    }
    line = to_log_line(raw)
    assert "ts" not in line.context
    assert "type" not in line.context
    assert "message" not in line.context
    assert "slug" not in line.context
    assert "level" not in line.context
