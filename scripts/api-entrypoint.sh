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

exec "$@"
