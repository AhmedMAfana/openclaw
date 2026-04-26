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

# ── Live backup loop: re-snapshot every 30 s + on graceful shutdown ──────
# Without this, post-startup login or feature-flag refreshes never make it
# into `.claude.json.persistent`. Next container restart's restore step
# brings back the *startup-time* state and the user's login is lost.
# Running as the same user as the entrypoint (root in dev override / the
# Dockerfile USER otherwise); both can write the volume.
backup_claude_json() {
    if [ -f "$CLAUDE_JSON" ]; then
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
trap '[ -n "$_BACKUP_LOOP_PID" ] && kill "$_BACKUP_LOOP_PID" 2>/dev/null; backup_claude_json; echo "[entrypoint] backed up .claude.json on shutdown"' EXIT TERM INT
echo "[entrypoint] .claude.json backup loop started (pid=$_BACKUP_LOOP_PID, every 30s + on SIGTERM)"

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
