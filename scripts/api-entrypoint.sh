#!/bin/bash
# API entrypoint — restores Claude credentials from shared claude_auth volume.
# HOME is resolved dynamically from the OS so no paths are hardcoded.

# Resolve the real home dir of whoever is running this container
export HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"

CLAUDE_JSON="${HOME}/.claude.json"
CREDS_FILE="${HOME}/.claude/.credentials.json"
CONFIG_BACKUP="${HOME}/.claude/.claude.json.persistent"

# Restore .claude.json from the volume backup if it's missing
if [ -f "$CONFIG_BACKUP" ] && [ ! -f "$CLAUDE_JSON" ]; then
    cp "$CONFIG_BACKUP" "$CLAUDE_JSON"
    echo "[api-entrypoint] Restored .claude.json from volume (user=$(id -un), home=$HOME)"
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
