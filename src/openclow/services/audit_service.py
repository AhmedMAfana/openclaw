"""Audit service — logs every agent action to the database.

Usage:
    from openclow.services.audit_service import audit

    # Log a command
    await audit.log("doctor", "bash", "docker logs my-container", workspace="/path/to/workspace/trade-bot")

    # Log a blocked command
    await audit.log_blocked("coder", "docker", "docker system prune -af")

    # Check if a command is allowed before running
    if not audit.is_docker_allowed("docker rm -f postgres"):
        await audit.log_blocked(...)
        raise PermissionError(...)

Secret redaction (Constitution Principle IV):
    from openclow.services.audit_service import redact
    safe = redact(raw_stdout)

    Used by BOTH the chat-UI log path AND the LLM-fallback envelope path.
    FR-033 requires a SINGLE shared redactor so a secret the chat hides
    cannot leak through the LLM path (or vice versa).
"""
import asyncio
import re
from typing import Any

from openclow.utils.logging import get_logger

log = get_logger()


# ---------------------------------------------------------------------------
# Redactor (Constitution Principle IV; FR-032, FR-033)
# ---------------------------------------------------------------------------
#
# Runs on stdout/stderr before it reaches EITHER the chat UI or the LLM
# fallback envelope. Idempotent: redact(redact(x)) == redact(x).
#
# Masks:
#   1. HTTP bearer tokens ("Authorization: Bearer <x>")
#   2. AWS access keys / secret access keys
#   3. GCP / PEM-encoded private keys (whole block)
#   4. Cloudflare API tokens (CF_API_TOKEN=<x>, cf-token=<x>)
#   5. SSH private-key blocks
#   6. `.env`-style KEY=VALUE where key matches /SECRET|TOKEN|PASSWORD|KEY|AUTH/i
#   7. GitHub App installation tokens (ghs_... / ghp_...)
#
# Deliberate non-goals:
#   * Not a general-purpose DLP — focus is on categories we actually generate.
#   * Not reversible — replacement is a stable marker, not an encrypted form.

_REDACT_MARK = "[REDACTED]"

# Order matters: more-specific patterns first. Each produces the same marker.
_REDACT_PATTERNS: tuple[re.Pattern, ...] = (
    # 1. Bearer tokens in headers, curl, log lines.
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([A-Za-z0-9._\-+/=]+)"),
    # 2. AWS
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)(aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*)[A-Za-z0-9/+=]{40}"),
    # 3. PEM private-key blocks (GCP, SSH, generic).
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED |)PRIVATE KEY-----"
        r".*?"
        r"-----END (?:RSA |EC |OPENSSH |ENCRYPTED |)PRIVATE KEY-----",
        re.DOTALL,
    ),
    # 4. Cloudflare tokens (common env-var and CLI-flag shapes).
    re.compile(r"(?i)(cf[_-]?api[_-]?token\s*[=:]\s*)[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)(cf[_-]?token\s*=\s*)[A-Za-z0-9_\-]{20,}"),
    # 5. GitHub App installation tokens (ghs_) and PATs (ghp_/github_pat_).
    re.compile(r"\bgh[osup]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    # 6. Generic KEY=VALUE where key matches the env-var name pattern.
    #    Matches both shell (KEY=value) and YAML (KEY: value) forms.
    re.compile(
        r"\b([A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|KEY|AUTH)[A-Z0-9_]*)\s*[=:]\s*"
        r"([^\s\"']+)"
    ),
)


def redact(text: str) -> str:
    """Mask known secret shapes. Safe for non-secret text (byte-for-byte pass-through)."""
    if not text:
        return text

    def _mask_kv(m: re.Match) -> str:
        return f"{m.group(1)}={_REDACT_MARK}"

    # Pattern 1 (Bearer): keep the prefix "Authorization: Bearer " and mask the rest.
    out = _REDACT_PATTERNS[0].sub(rf"\1{_REDACT_MARK}", text)
    # Pattern 2 (AWS AKIA) — bare match, replace whole thing.
    out = _REDACT_PATTERNS[1].sub(_REDACT_MARK, out)
    # Pattern 3 (aws_secret_access_key=...) — preserve the key prefix.
    out = _REDACT_PATTERNS[2].sub(rf"\1{_REDACT_MARK}", out)
    # Pattern 4 (PEM blocks) — wholesale.
    out = _REDACT_PATTERNS[3].sub(_REDACT_MARK, out)
    # Patterns 5/6 (CF tokens) — preserve key prefix.
    out = _REDACT_PATTERNS[4].sub(rf"\1{_REDACT_MARK}", out)
    out = _REDACT_PATTERNS[5].sub(rf"\1{_REDACT_MARK}", out)
    # Pattern 7/8 (GitHub tokens) — wholesale.
    out = _REDACT_PATTERNS[6].sub(_REDACT_MARK, out)
    out = _REDACT_PATTERNS[7].sub(_REDACT_MARK, out)
    # Pattern 9 (generic KEY=VALUE with secret-like name) — keep key, mask value.
    out = _REDACT_PATTERNS[8].sub(_mask_kv, out)
    return out


# ---------------------------------------------------------------------------
# Audit-log buffered writes
# ---------------------------------------------------------------------------

# Singleton buffer — flush to DB in batches to avoid per-command DB overhead
_buffer: list[dict] = []
_buffer_lock = asyncio.Lock()
_FLUSH_SIZE = 10


def _classify_risk(action: str, command: str) -> str:
    """Classify the risk level of a command."""
    cmd_lower = command.lower()

    # Dangerous operations
    dangerous_patterns = [
        "rm -rf", "rm -f /",
        "docker rm -f", "docker rmi", "docker system prune",
        "docker volume rm", "docker network rm",
        "git push --force", "git push -f",
        "git reset --hard",
        "drop table", "drop database", "truncate",
        "chmod 777", "chown root",
        "kill -9",
    ]
    for pattern in dangerous_patterns:
        if pattern in cmd_lower:
            return "dangerous"

    # Elevated operations
    elevated_patterns = [
        "docker", "git push", "git commit",
        "pip install", "npm install", "apt-get install",
        "curl", "wget",
    ]
    for pattern in elevated_patterns:
        if pattern in cmd_lower:
            return "elevated"

    return "normal"


async def log_action(
    actor: str,
    action: str,
    command: str,
    workspace: str | None = None,
    project_name: str | None = None,
    exit_code: int | None = None,
    output_summary: str | None = None,
    blocked: bool = False,
    metadata: dict[str, Any] | None = None,
):
    """Log an agent action. Buffers writes for performance."""
    risk_level = _classify_risk(action, command)

    entry = {
        "actor": actor,
        "action": action,
        "command": command[:5000],  # cap command length
        "workspace": workspace,
        "project_name": project_name,
        "exit_code": exit_code,
        "output_summary": (output_summary or "")[:2000] or None,
        "risk_level": risk_level,
        "blocked": blocked,
        "metadata_": metadata,
    }

    # Log dangerous actions immediately to structlog too
    if risk_level == "dangerous":
        log.warning("audit.dangerous_action",
                     actor=actor, action=action, command=command[:200],
                     blocked=blocked)

    async with _buffer_lock:
        _buffer.append(entry)
        if len(_buffer) >= _FLUSH_SIZE:
            await _flush_buffer()


async def log_blocked(
    actor: str,
    action: str,
    command: str,
    reason: str = "",
    **kwargs,
):
    """Log a blocked (denied) action."""
    await log_action(
        actor=actor,
        action=action,
        command=command,
        blocked=True,
        metadata={"reason": reason, **(kwargs.get("metadata") or {})},
        **{k: v for k, v in kwargs.items() if k != "metadata"},
    )


async def _flush_buffer():
    """Write buffered entries to DB."""
    global _buffer
    if not _buffer:
        return

    entries = _buffer.copy()
    _buffer = []

    try:
        from openclow.models.audit import AuditLog
        from openclow.models.base import async_session

        async with async_session() as session:
            for entry in entries:
                session.add(AuditLog(**entry))
            await session.commit()
    except Exception as e:
        log.error("audit.flush_failed", error=str(e), count=len(entries))
        # Don't lose entries on failure — put them back
        async with _buffer_lock:
            _buffer = entries + _buffer


async def flush():
    """Force flush all buffered entries. Call this on shutdown."""
    async with _buffer_lock:
        await _flush_buffer()


async def get_recent(
    limit: int = 50,
    actor: str | None = None,
    project_name: str | None = None,
    risk_level: str | None = None,
    blocked_only: bool = False,
) -> list[dict]:
    """Query recent audit log entries."""
    from sqlalchemy import select
    from openclow.models.audit import AuditLog
    from openclow.models.base import async_session

    async with async_session() as session:
        q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if actor:
            q = q.where(AuditLog.actor == actor)
        if project_name:
            q = q.where(AuditLog.project_name == project_name)
        if risk_level:
            q = q.where(AuditLog.risk_level == risk_level)
        if blocked_only:
            q = q.where(AuditLog.blocked.is_(True))

        result = await session.execute(q)
        rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "actor": r.actor,
                "action": r.action,
                "command": r.command,
                "risk_level": r.risk_level,
                "blocked": r.blocked,
                "exit_code": r.exit_code,
                "output_summary": r.output_summary,
                "project_name": r.project_name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "metadata": r.metadata_,
            }
            for r in rows
        ]
