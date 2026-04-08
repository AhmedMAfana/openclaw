#!/bin/bash
# Worker entrypoint — ensures Claude credentials persist across restarts

CLAUDE_DIR="/home/openclow/.claude"
CLAUDE_JSON="/home/openclow/.claude.json"
CREDS_FILE="${CLAUDE_DIR}/.credentials.json"
CONFIG_BACKUP="${CLAUDE_DIR}/.claude.json.persistent"

# Restore .claude.json from volume if it exists
if [ -f "$CONFIG_BACKUP" ] && [ ! -f "$CLAUDE_JSON" ]; then
    cp "$CONFIG_BACKUP" "$CLAUDE_JSON"
    echo "[entrypoint] Restored .claude.json from volume"
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

# Execute the actual command
exec "$@"
