"""T077: LLM fallback envelopes validate against the canonical JSON Schema.

Contract gate for
[contracts/llm-fallback-envelope.schema.json](../../specs/001-per-chat-instances/contracts/llm-fallback-envelope.schema.json).

Three assertions:

1. A well-formed envelope (from a real failure) validates green.
2. An envelope with an unknown top-level field is REJECTED (schema
   has ``additionalProperties: false``).
3. An envelope whose ``stdout_tail`` exceeds 32 768 chars is
   REJECTED (``maxLength: 32768``).

Requires the ``jsonschema`` package. Skips cleanly when it's not
installed so a bare ``pytest`` run on a minimal env still passes.
"""
from __future__ import annotations

import json
import pathlib

import pytest

jsonschema = pytest.importorskip(
    "jsonschema",
    reason="requires the `jsonschema` package",
)


_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "specs"
    / "001-per-chat-instances"
    / "contracts"
    / "llm-fallback-envelope.schema.json"
)


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def _base_envelope() -> dict:
    return {
        "instance_slug": "inst-abcdef0123456789"[:20 - 0] if False else "inst-" + "0" * 14,
        "project_name": "laravel-vue",
        "step": {
            "name": "install-php",
            "cmd": "composer install",
            "cwd": "/app",
            "success_check": "test -d /app/vendor",
            "skippable": False,
        },
        "exit_code": 1,
        "stdout_tail": "line 1\nline 2\n",
        "stderr_tail": "Error: composer not found\n",
        "guide_section": "## install-php\n...\n",
        "previous_attempts": 0,
    }


def test_wellformed_envelope_passes(schema: dict) -> None:
    jsonschema.validate(_base_envelope(), schema)


def test_unknown_field_rejected(schema: dict) -> None:
    env = _base_envelope()
    env["extra"] = "oops"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(env, schema)


def test_stdout_tail_length_cap_rejected(schema: dict) -> None:
    env = _base_envelope()
    env["stdout_tail"] = "x" * (32_768 + 1)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(env, schema)


def test_previous_attempts_cap_rejected(schema: dict) -> None:
    env = _base_envelope()
    env["previous_attempts"] = 4
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(env, schema)


def test_slug_pattern_rejected(schema: dict) -> None:
    env = _base_envelope()
    env["instance_slug"] = "inst-NOTHEX-00000000"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(env, schema)
