# Testing Guide — TAGH Dev System Improvements

## Three Main Issues Fixed

### 1. **Open App Button URL** 

**What was fixed:**
- Enhanced error handling for tunnel URL fetching
- Better logging to diagnose missing URLs
- Improved exception handling in agent_session.py

**How to test:**
1. Ensure a project has a tunnel URL in the database:
   ```bash
   docker compose exec postgres psql -U taghdev -d taghdev -c "SELECT key, value FROM platform_config WHERE category='tunnel';"
   ```
2. Look for entries with non-empty `url` field (should contain `https://...trycloudflare.com`)
3. Send a message in a Slack channel linked to a project
4. The response should include an "Open App" button (if tunnel URL exists)
5. Click the button - it should open the tunnel URL

**If button doesn't appear:**
- Check that the channel is linked to a project in dashboard settings
- Check that the project has bootstrapped (has a tunnel URL in DB)
- Check Docker logs for tunnel URL fetch errors: `docker compose logs worker | grep tunnel`

**If button appears but URL doesn't work:**
- The Cloudflare tunnel might be dead. Restart the worker: `docker compose restart worker`
- Check cloudflared process: `docker compose exec worker ps aux | grep cloudflared`

---

### 2. **Project Selector Lock** 

**What was implemented:**
- Redis cache (1 hour) for DM project selection
- Database-persistent default_project_id
- Fallback chain: Cache → Default Project ID → Selector (only if multiple projects)

**How to test:**
1. DM the bot with multiple projects available:
   - First message: Project selector should appear
   - User clicks a project button
2. Send a second message in the same DM:
   - The same project should be used (NO selector)
   - Message shows "Using Project X — processing..."
3. Close/reopen DM thread later:
   - Should still use the saved default_project_id
   - Selector should NOT appear

**To change project:**
- Users need to explicitly set a new default via the dashboard or admin commands
- Or wait 1 hour for the Redis cache to expire (then selector reappears)

---

### 3. **Immediate Loading Indicators** 

**What was fixed:**
- Enhanced thinking blocks with better messages
- Text now shows: "Processing your request... _Loading Claude AI, preparing workspace..._"
- Visible within 1-2 seconds (not 10 seconds)

**How to test:**
1. Send a message to the bot
2. Check that within 1-2 seconds, a "Processing..." message appears
3. The message should show the task text being processed
4. As the agent works, it updates to show "Working..." with tool activity
5. Finally, the complete response appears

---

## Docker Volume & Rebuild Issue

**The Problem:**
Using `docker compose up bot worker --build` rebuilds the Docker image EVERY time, which is slow if you've only changed Python code.

**The Solution:**

```bash
# If you only changed Python code (NOT Dockerfile or pyproject.toml):
docker compose restart bot worker

# If you changed dependencies in pyproject.toml:
docker compose up bot worker --build

# If you changed Dockerfile:
docker compose up bot worker --build
```

**How it works:**
- `./src` is mounted as a volume in containers
- Code changes are live-reloaded via watchfiles
- No need to rebuild unless Dockerfile/dependencies changed
- Use `restart` for code-only changes (much faster)

---

## Verifying All Three Fixes

### Full Flow Test:

1. **Channel Setup** (if not already done):
   ```bash
   # Check if channels are linked
   docker compose exec postgres psql -U taghdev -d taghdev -c "SELECT category, key, value FROM platform_config WHERE category='slack_channel';"
   ```
   - If empty, link a channel in the dashboard: http://localhost:8000/settings/chat

2. **Test Message in Linked Channel:**
   - Send a message in Slack channel linked to a project
   - Within 1-2 seconds, see "⏳ *Processing your request...*" with "Loading Claude AI..."
   - After ~5-10 seconds, see the response
   - Response should have "Open App" button (if tunnel exists)

3. **Test DM Flow:**
   - DM the bot with multiple projects available
   - See project selector
   - Click a project
   - See "Using Project X — processing..." message
   - Send another message in same DM
   - Should use same project (NO selector)

---

## Troubleshooting

**Bot not responding to messages:**
```bash
docker compose logs bot -f | grep -E "error|ERROR|Socket|tunnel"
```

**Tunnel URL not appearing:**
```bash
docker compose logs worker | grep tunnel
docker compose exec postgres psql -U taghdev -d taghdev -c "SELECT key, value FROM platform_config WHERE category='tunnel';"
```

**Project selector keeps reappearing:**
```bash
# Check Redis cache
docker compose exec redis redis-cli -a taghdev
> GET taghdev:dm_project:slack:U<USER_ID>
> exit

# Check DB default_project_id
docker compose exec postgres psql -U taghdev -d taghdev -c "SELECT chat_provider_uid, default_project_id FROM users;"
```

**Services not starting:**
```bash
docker compose logs --all -f
```

---

## Key Files Modified

- `src/taghdev/worker/tasks/agent_session.py` — Better tunnel URL error handling & logging
- `src/taghdev/providers/chat/slack/blocks.py` — Enhanced thinking blocks message
- `docker-compose.override.yml` — Watchfiles grace period (prevents infinite restarts)
- `src/taghdev/alembic/versions/005_*.py` — Default project DB migration

## Notes

- Per-user session isolation is working (separate Redis keys per user)
- Admin bypass for task ownership is working
- Timeout protection (90s) prevents indefinite hangs
- Watchfiles restarts bot after 3s grace period (prevents Socket Mode connection loss)
