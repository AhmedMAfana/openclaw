#!/bin/bash
# Worker entrypoint — ensures Claude credentials persist across restarts.
# HOME is resolved dynamically from the OS so no paths are hardcoded.

# Resolve the real home dir of whoever is running this container
export HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"

CLAUDE_JSON="${HOME}/.claude.json"
CREDS_FILE="${HOME}/.claude/.credentials.json"
CONFIG_BACKUP="${HOME}/.claude/.claude.json.persistent"

# Restore .claude.json from volume if it exists
if [ -f "$CONFIG_BACKUP" ] && [ ! -f "$CLAUDE_JSON" ]; then
    cp "$CONFIG_BACKUP" "$CLAUDE_JSON"
    echo "[entrypoint] Restored .claude.json from volume (user=$(id -un), home=$HOME)"
fi

# Create .claude.json if it doesn't exist at all
if [ ! -f "$CLAUDE_JSON" ]; then
    echo '{}' > "$CLAUDE_JSON"
    echo "[entrypoint] Created empty .claude.json"
fi

# Verify Claude auth status
if [ -f "$CREDS_FILE" ]; then
    echo "[entrypoint] Claude credentials found"
    claude auth status 2>/dev/null | head -3
else
    echo "[entrypoint] WARNING: No Claude credentials. Run: docker exec -it openclow-worker-1 claude login"
fi

# Back up .claude.json to volume on every start (so it persists)
cp "$CLAUDE_JSON" "$CONFIG_BACKUP" 2>/dev/null

exec "$@"
