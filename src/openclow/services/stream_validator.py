"""Runtime contract validator for assistant-stream `controller.add_data` payloads.

The senior-level layer that sits behind the static codegen + fitness
audit. Even with the JSON Schema as source of truth and TS types
generated for the frontend, a lazy backend dev can still emit a
malformed dict — a stale field name, a typo'd value, an off-by-one
spelling. The static check only catches **drift on the type
discriminator**; this catches everything else (missing required
fields, pattern violations, enum violations, etc.).

Layering:

  * **build time**: codegen forces the frontend to handle every type
    (TypeScript exhaustiveness on `StreamEvent`).
  * **test time**: contract tests load real fixtures and validate
    against the schema (recommended in T077 for envelopes; same
    pattern here).
  * **DEBUG runtime** (this module): every `controller.add_data` call
    runs through ``validate_event`` — invalid payloads raise
    immediately so the dev sees the bug at emit time, not in
    production.
  * **prod runtime** (this module): invalid payloads emit a
    ``stream_event_invalid`` log line + telemetry counter, but
    fall through to the wire so a partial event is still delivered.
    Failing closed in prod would degrade the chat into errors for
    every user; failing OPEN with telemetry is the right trade.

Mode is controlled by the ``OPENCLOW_STREAM_VALIDATE`` env var:

  * ``strict`` (default in dev): raise ``StreamEventInvalidError`` on
    invalid payloads. Use in unit + integration tests.
  * ``warn`` (default in prod): log + emit telemetry, do not raise.
  * ``off``: skip validation entirely; use when latency budget is
    tight and the caller is trusted (currently nowhere — kept as an
    escape hatch).

Maps to Constitution Principle VII (Verified Work) — we don't claim
"emit done" without a runtime guarantee that the emit conforms.
"""
from __future__ import annotations

import json
import os
import pathlib
from functools import lru_cache
from typing import Any, Mapping

from openclow.utils.logging import get_logger

log = get_logger()


# Schema lives in the spec dir so any reader of the contract finds the
# authoritative copy in one place. Path is computed at module-load to
# avoid a startup race against the editable-install layout.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCHEMA_PATH = (
    _REPO_ROOT
    / "specs"
    / "001-per-chat-instances"
    / "contracts"
    / "stream-events.schema.json"
)


class StreamEventInvalidError(ValueError):
    """Raised in strict mode when a payload fails schema validation."""


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any] | None:
    """Lazy-load the schema once per process. Returns None if the file
    isn't shipped with the install (e.g. inside a Docker image that
    only bind-mounts ``src/``). The hot path uses the embedded
    ``_REQUIRED_BY_TYPE`` table either way; the JSON schema is for
    contract tests, IDE support, and the fitness audit's drift check.
    """
    if not _SCHEMA_PATH.is_file():
        return None
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _allowed_types() -> frozenset[str]:
    """Closed set of allowed event-type discriminators.

    Derived from the embedded ``_REQUIRED_BY_TYPE`` keys so the
    runtime validator doesn't depend on the JSON Schema file being
    present at runtime. The fitness audit
    (``scripts/fitness/check_schema_code_sync.py``) cross-checks this
    set against the schema enum so the two cannot drift.
    """
    return frozenset(_REQUIRED_BY_TYPE.keys())


def _mode() -> str:
    """Read the validation mode env var, normalised + defaulted.

    Default is ``strict`` so test environments fail loud unless the
    deployment explicitly opts down. Production sets ``warn`` via
    docker-compose env.
    """
    raw = (os.environ.get("OPENCLOW_STREAM_VALIDATE") or "strict").lower()
    if raw not in {"strict", "warn", "off"}:
        return "strict"
    return raw


def validate_event(payload: Mapping[str, Any]) -> None:
    """Validate one ``controller.add_data`` payload against the schema.

    Behaviour by mode:

      * ``strict``: raises ``StreamEventInvalidError`` on any failure.
      * ``warn``:   logs a warning + telemetry-shaped log line, returns.
      * ``off``:    no-op.

    The check is lightweight: ``type`` is in the closed enum, the
    branch's required fields are present, and known-pattern fields
    match. We do NOT pull a full ``jsonschema`` runtime in here — that
    library is a 200 KB dependency and the hot path runs on every
    streamed event. The static codegen + the contract tests cover the
    deeper structural checks.
    """
    mode = _mode()
    if mode == "off":
        return

    err = _validate(payload)
    if err is None:
        return

    if mode == "strict":
        raise StreamEventInvalidError(err)
    # warn: log + telemetry, do not raise
    log.warning(
        "stream_event_invalid",
        reason=err,
        type=payload.get("type") if isinstance(payload, Mapping) else None,
    )


def _validate(payload: Any) -> str | None:
    """Return None on success, an error string on failure.

    Hand-rolled, narrow check tailored to this schema. Replace with a
    full ``jsonschema`` validator if the schema grows complex enough
    that hand-rolling becomes a liability.
    """
    if not isinstance(payload, Mapping):
        return f"payload must be a mapping; got {type(payload).__name__}"

    t = payload.get("type")
    if not isinstance(t, str):
        return "missing or non-string `type` field"
    if t not in _allowed_types():
        return f"unknown event type {t!r}; allowed = {sorted(_allowed_types())}"

    # Per-event required-fields. Mirrors the schema's oneOf branches.
    required = _REQUIRED_BY_TYPE.get(t, ())
    missing = [f for f in required if f not in payload]
    if missing:
        return f"event {t!r} missing required field(s): {missing}"

    # Slug pattern check for the events that carry one.
    if t in _SLUG_BEARING and not _looks_like_slug(payload.get("slug")):
        return (
            f"event {t!r} slug does not match ^inst-[0-9a-f]{{14}}$: "
            f"got {payload.get('slug')!r}"
        )

    # tool_result content cap (Principle IV — bounded for the redactor's
    # blast radius if a future regression bypasses redact).
    if t == "tool_result":
        content = payload.get("content", "")
        if isinstance(content, str) and len(content) > 1500:
            return (
                f"tool_result.content exceeds 1500 chars "
                f"({len(content)}); truncate before emit"
            )

    # instance_limit_exceeded variant discriminator.
    if t == "instance_limit_exceeded":
        v = payload.get("variant")
        if v not in {"per_user_cap", "platform_capacity"}:
            return f"instance_limit_exceeded.variant must be per_user_cap|platform_capacity; got {v!r}"
        if v == "per_user_cap":
            extra = ("cap", "active_chat_ids", "instances_endpoint", "actions")
            missing2 = [f for f in extra if f not in payload]
            if missing2:
                return f"per_user_cap variant missing: {missing2}"
        elif v == "platform_capacity" and "retry_after_s" not in payload:
            return "platform_capacity variant missing retry_after_s"

    return None


# Required-fields lookup, derived from the schema's oneOf branches.
# Kept in code so the hot path is one dict lookup, not a schema walk.
# The pipeline-fitness audit asserts this stays in sync with the schema.
_REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "message_id": ("type", "id"),
    "tool_use": ("type", "id", "tool"),
    "tool_result": ("type", "tool_use_id", "content"),
    "instance_provisioning": ("type", "slug", "estimated_seconds"),
    "instance_failed": ("type", "slug", "failure_code", "actions"),
    "instance_limit_exceeded": ("type", "variant"),
    "instance_upstream_degraded": ("type", "slug", "capabilities"),
    "instance_busy": ("type", "slug"),
    "instance_terminating": ("type", "slug"),
    "instance_retry_started": ("type", "failed_instance_id"),
    "confirm": ("type", "prompt", "actions"),
}

_SLUG_BEARING: frozenset[str] = frozenset({
    "instance_provisioning",
    "instance_failed",
    "instance_upstream_degraded",
    "instance_busy",
    "instance_terminating",
})


def _looks_like_slug(s: Any) -> bool:
    if not isinstance(s, str):
        return False
    if not s.startswith("inst-"):
        return False
    rest = s[5:]
    return len(rest) == 14 and all(c in "0123456789abcdef" for c in rest)


__all__ = [
    "StreamEventInvalidError",
    "validate_event",
]
