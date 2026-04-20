# TAGH Dev Agent System — Responsibility Map

## Overview

TAGH Dev uses a multi-agent pipeline where each agent has specific responsibilities,
access levels, and outputs. The orchestrator (Python code) coordinates between agents
and handles deterministic operations (git push, PR creation, Docker management).

## Pipeline Flow

```
User sends task via Telegram
        ↓
┌─── PLANNER ───┐
│  Analyze &     │ → Sends plan to Telegram
│  create plan   │ → [Approve Plan] [Reject]
└───────┬────────┘
        ↓ user approves
┌─── CODER ─────┐
│  Implement     │ → Step-by-step progress on Telegram
│  the plan      │ → "Implementing [2/5] ██░░░"
└───────┬────────┘
        ↓ code done
┌─── REVIEWER ──┐
│  Check quality │ → APPROVED or ISSUES
│  & security    │ → If issues → back to CODER (max 2 retries)
└───────┬────────┘
        ↓ approved
┌─── ORCHESTRATOR ─┐
│  Push, PR,        │ → Summary + diff on Telegram
│  notifications    │ → [Create PR] [Discard]
└───────┬───────────┘
        ↓ user approves
┌─── ORCHESTRATOR ─┐
│  Create PR on     │ → PR link on Telegram
│  GitHub           │ → [Merge] [Reject] [View PR]
└──────────────────┘
```

---

## Agent Details

### 1. PLANNER (Claude, read-only)

**Responsibility:** Analyze the project codebase and create an implementation plan before any code is written.

**Access:**
- `Read` — read project files
- `Glob` — find files by pattern
- `Grep` — search code content
- `CLAUDE.md` — reads project conventions if file exists

**Does NOT have:**
- Write/Edit (cannot modify files)
- Bash (cannot run commands)
- Docker access
- Git write access

**Output:** Implementation plan sent to user on Telegram with `[Approve Plan]` / `[Reject]` buttons.

**Purpose:** User sees and approves the plan BEFORE any code changes happen. This prevents the agent from going in the wrong direction.

---

### 2. CODER (Claude, full access)

**Responsibility:** Implement the approved plan step-by-step.

**Access:**
- `Read/Write/Edit` — full file access in workspace
- `Bash` — any shell command
- `Docker exec` — run commands inside project containers:
  - `docker exec <app-container> php artisan migrate`
  - `docker exec <app-container> php artisan test`
  - `docker exec <app-container> npm run build`
  - `docker exec <app-container> composer require <package>`
- `Git MCP` — git status, diff, add (via mcp-server-git)
- `Playwright MCP` — visual testing:
  - Navigate to `http://localhost:<port>`
  - Fill forms, click buttons
  - Take screenshots
  - Verify UI changes

**Does NOT have:**
- Git push (orchestrator controls when code reaches GitHub)
- Git commit (orchestrator commits with a standard message)
- PR creation (orchestrator handles via GitHub API)

**Error Handling:**
- If tests fail → Claude reads the error → fixes the code → re-runs tests
- This self-correction loop runs automatically within the agent session
- Up to `max_turns` (default 50) iterations

**Output:** Code changes + `STEP_DONE` markers for progress tracking + `DONE_SUMMARY` at the end.

---

### 3. REVIEWER (Claude, read-only)

**Responsibility:** Review the code changes for quality, security, and convention adherence.

**Access:**
- `Read/Glob/Grep` — read files
- `Git MCP` (read-only tools only):
  - `git_diff_staged` — see what changed
  - `git_diff_unstaged` — see uncommitted changes
  - `git_log` — check commit history
  - `git_show` — inspect specific commits
  - `git_status` — current state

**Does NOT have:**
- Write/Edit (cannot modify files)
- Bash (cannot run commands)
- Docker access
- Playwright access

**Checks for:**
1. Security — SQL injection, XSS, CSRF, mass assignment
2. Laravel best practices — validation, middleware, form requests
3. Vue best practices — reactivity, component structure, props
4. Missing error handling or edge cases
5. Broken imports, unused code
6. Code style consistency with existing codebase
7. Missing or incorrect migrations
8. Test coverage

**Output:**
- `STATUS: APPROVED` — changes are good
- `STATUS: ISSUES` + list of issues — sent back to CODER for fixing

**Retry Loop:** If issues found → CODER fixes → REVIEWER re-checks → up to 2 retries.

---

### 4. ORCHESTRATOR (Python code, NOT an AI agent)

**Responsibility:** Coordinate the pipeline and handle deterministic operations.

**Handles:**
- Workspace management (clone, cache, git worktree, dependency caching)
- Docker management (start/stop project containers)
- Git operations (branch, commit, push)
- GitHub API (create PR, merge, close, delete branch)
- Telegram notifications (status updates, buttons, progress bars)
- Task state management (DB updates, logging)
- Error recovery and cleanup

**Why NOT an AI agent?** These operations are deterministic — git push, PR creation, Docker commands always work the same way. Using an AI agent for them wastes rate limits and adds unnecessary latency and failure points.

---

## MCP Servers Connected

| MCP Server | Used By | Purpose |
|------------|---------|---------|
| `mcp-server-git` | Coder, Reviewer | Git status, diff, add, commit, branch |
| `@playwright/mcp` | Coder | Visual testing — browser navigation, clicks, screenshots |
| `docker` (custom) | Coder, Onboarding, Doctor | List containers, read logs, restart, exec, compose up/down |
| `github-openclow` (custom) | Coder, Onboarding | List repos, check access, repo info, list PRs/branches |
| `project-info` (custom) | Coder | Read project config from DB |

### Custom MCP Tools

**Docker MCP (`openclow.mcp_servers.docker_mcp`):**
- `list_containers` — list running containers, filter by project
- `container_logs` — read logs from any container
- `container_health` — check health status
- `restart_container` — restart a container
- `docker_exec` — run command inside a container
- `compose_up` — start a Docker Compose stack
- `compose_down` — stop a Docker Compose stack
- `compose_ps` — list containers in a stack

**GitHub MCP (`openclow.mcp_servers.github_mcp`):**
- `list_repos` — list repos the user has access to
- `repo_info` — get repo details (languages, default branch)
- `list_branches` — list branches
- `list_prs` — list open/closed PRs
- `check_repo_access` — verify write access

---

## Docker Integration

Projects run as Docker containers. The worker container has Docker socket access to manage project stacks.

```
Worker Container
    ├── Docker socket mounted (/var/run/docker.sock)
    ├── Clones project repo to /workspaces/task-xxx/
    ├── Runs: docker compose -p openclow-<project>-<task> up -d
    │   ├── <project>-app    (PHP/Laravel)
    │   ├── <project>-db     (MySQL/Postgres)
    │   └── <project>-redis  (Redis)
    ├── Claude edits files in workspace (mounted into containers)
    ├── Claude runs: docker exec <app-container> php artisan test
    ├── Playwright tests: http://localhost:<port>
    └── Cleanup: docker compose down (after task completes)
```

---

## Error Correction — 3 Layers

```
Layer 1: Self-correction (within CODER agent)
  Claude writes code → runs tests → FAILS
  → Claude reads error → fixes → re-runs
  → Automatic, up to max_turns

Layer 2: REVIEWER catches remaining issues
  Coder finishes → Reviewer finds issues
  → Issues sent back to Coder → fixes → re-reviewed
  → Up to 2 retry loops

Layer 3: User approval
  All code done → user sees summary + diff
  → [Create PR] or [Discard]
  → Nothing touches the real repo without user approval
```

---

## Security Model

| Agent | Can read files | Can write files | Can run commands | Can access Docker | Can push to GitHub |
|-------|---------------|----------------|-----------------|------------------|-------------------|
| Planner | ✅ | ❌ | ❌ | ❌ | ❌ |
| Coder | ✅ | ✅ | ✅ | ✅ (exec only) | ❌ |
| Reviewer | ✅ | ❌ | ❌ | ❌ | ❌ |
| Orchestrator | ✅ | ✅ (git only) | ✅ | ✅ | ✅ (after user approval) |
