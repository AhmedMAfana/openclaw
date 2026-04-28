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

# /workspaces sanity check — must exist AND be writable. The orchestrator
# clones project repos into /workspaces/_cache/<project>/ and the per-
# instance app containers bind-mount the same host path back into
# /var/www/html. On prod this is the `app_workspaces` named volume;
# on dev it's a host bind. If an operator brings the worker up with the
# wrong overlay (e.g. forgets `-f docker-compose.prod.yml`), the worker
# silently mounts an empty fresh dir and every provision fails 30s in
# with "Permission denied: /workspaces/_cache". Fail LOUD on boot so the
# next operator hits a clear actionable error instead.
# WORKSPACES_BIND_MODE=skip disables the check (offline / unit-test).
if [ "${WORKSPACES_BIND_MODE:-check}" != "skip" ]; then
    if [ ! -d /workspaces ]; then
        echo "[entrypoint] FATAL: /workspaces does not exist — compose file is missing the workspaces volume mount." >&2
        exit 78
    fi
    _probe="/workspaces/.entrypoint_writable_$$"
    if ! touch "$_probe" 2>/dev/null; then
        echo "[entrypoint] FATAL: /workspaces is not writable by uid=$(id -u). Most likely cause: container started with the dev compose only — re-run with \`-f docker-compose.yml -f docker-compose.prod.yml\` (prod) so /workspaces gets the named volume mount, or chown the host bind dir if you're on dev." >&2
        ls -ld /workspaces >&2 || true
        exit 78
    fi
    rm -f "$_probe"
    echo "[entrypoint] /workspaces ok (writable by uid=$(id -u))"
fi

exec "$@"
