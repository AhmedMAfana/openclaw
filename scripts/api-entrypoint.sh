#!/bin/bash
# API entrypoint — restores Claude credentials from shared claude_auth volume.
# HOME is resolved dynamically from the OS so no paths are hardcoded.

# Resolve the real home dir of whoever is running this container
export HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"

CLAUDE_JSON="${HOME}/.claude.json"
CREDS_FILE="${HOME}/.claude/.credentials.json"
CONFIG_BACKUP="${HOME}/.claude/.claude.json.persistent"

# Restore .claude.json from the volume backup if it's missing or stale.
# `.claude.json` lives in the container's ephemeral root fs (NOT inside
# the claude_auth volume), so a container restart wipes it. The volume
# stores the latest snapshot at `.claude.json.persistent` — restore is
# the only way to keep the user's login state across restarts.
if [ -f "$CONFIG_BACKUP" ] && [ ! -f "$CLAUDE_JSON" ]; then
    cp "$CONFIG_BACKUP" "$CLAUDE_JSON"
    echo "[api-entrypoint] Restored .claude.json from volume (user=$(id -un), home=$HOME, size=$(wc -c <"$CLAUDE_JSON")B)"
fi

# Create an empty one so the CLI doesn't crash on first run
if [ ! -f "$CLAUDE_JSON" ]; then
    echo '{}' > "$CLAUDE_JSON"
fi

if [ -f "$CREDS_FILE" ]; then
    echo "[api-entrypoint] Claude credentials found — inline agent ready"
else
    echo "[api-entrypoint] WARNING: No Claude credentials at $CREDS_FILE"
    echo "[api-entrypoint]   Fix: docker exec -it openclow-worker-1 claude login"
fi

# ── Persistent backup of .claude.json into the volume ─────────────────────
# The Claude CLI updates `.claude.json` on login, on subscription refresh,
# on every feature-flag fetch — but `.claude.json` is on the container's
# ephemeral fs. Without a backup loop, every container restart restores
# the *original* stub in `.claude.json.persistent` and the user's login is
# silently lost. This loop snapshots the live file into the volume every
# 30 s and again on graceful shutdown (SIGTERM/SIGINT/EXIT trap).
backup_claude_json() {
    if [ -f "$CLAUDE_JSON" ] && [ -w "$(dirname "$CONFIG_BACKUP")" ]; then
        # Atomic write: write to a tmp file then rename so a mid-write
        # crash never leaves the persistent file truncated.
        local tmp="${CONFIG_BACKUP}.tmp.$$"
        if cp "$CLAUDE_JSON" "$tmp" 2>/dev/null && mv -f "$tmp" "$CONFIG_BACKUP" 2>/dev/null; then
            return 0
        fi
        rm -f "$tmp" 2>/dev/null
    fi
    return 1
}
(
    while sleep 30; do
        backup_claude_json
    done
) &
_BACKUP_LOOP_PID=$!
trap '[ -n "$_BACKUP_LOOP_PID" ] && kill "$_BACKUP_LOOP_PID" 2>/dev/null; backup_claude_json; echo "[api-entrypoint] backed up .claude.json on shutdown"' EXIT TERM INT
echo "[api-entrypoint] .claude.json backup loop started (pid=$_BACKUP_LOOP_PID, every 30s + on SIGTERM)"

# ── JWT secret — auto-generate once, persist across restarts ──────────────
# If WEB_CHAT_JWT_SECRET is already set (e.g. via .env or CI), use it as-is.
# Otherwise generate a strong random secret and store it in a volume file so
# the same secret survives container restarts. A new volume = a new secret,
# which correctly invalidates all existing tokens on a fresh deployment.
if [ -z "$WEB_CHAT_JWT_SECRET" ]; then
    SECRET_FILE="${HOME}/.openclow/jwt_secret"
    mkdir -p "$(dirname "$SECRET_FILE")"
    if [ ! -f "$SECRET_FILE" ]; then
        openssl rand -hex 32 > "$SECRET_FILE"
        echo "[api-entrypoint] Generated new WEB_CHAT_JWT_SECRET (stored in volume)"
    else
        echo "[api-entrypoint] Loaded WEB_CHAT_JWT_SECRET from volume"
    fi
    export WEB_CHAT_JWT_SECRET="$(cat "$SECRET_FILE")"
fi

exec "$@"
