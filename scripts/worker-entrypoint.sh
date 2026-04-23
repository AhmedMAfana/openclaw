#!/bin/bash
# Worker entrypoint — ensures Claude credentials persist across restarts.
# HOME is resolved dynamically from the OS so no paths are hardcoded.

# Resolve the real home dir of the openclow user (the volume is mounted there)
export HOME="$(getent passwd openclow | cut -d: -f6)"

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

# Playwright MCP sanity check — fails loud on first boot if the image
# was rebuilt without @playwright/mcp or the Chromium cache got nuked.
# Keeps the "Failed" state that caused this in Claude Code from silently
# recurring after a worker rebuild.
if [ ! -x /usr/local/bin/playwright-mcp ] && [ ! -x /usr/local/nvm/versions/node/v20.20.2/bin/playwright-mcp ]; then
    echo "[entrypoint] ERROR: playwright-mcp binary is missing; rebuild Dockerfile.worker." >&2
fi
if [ -n "$PLAYWRIGHT_BROWSERS_PATH" ] && [ ! -d "$PLAYWRIGHT_BROWSERS_PATH" ]; then
    echo "[entrypoint] WARNING: PLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH missing; run npx playwright install chromium inside the container." >&2
fi

exec "$@"
