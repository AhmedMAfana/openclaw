"""Tests for Direct Commit and Session Branch git modes."""
import ast
import inspect
import os

import pytest


# ──────────────────────────────────────────────
# 1. MODEL SCHEMA
# ──────────────────────────────────────────────

class TestModelSchema:
    def test_task_has_git_mode(self):
        src_path = os.path.join("src", "taghdev", "models", "task.py")
        with open(src_path) as f:
            src = f.read()
        assert "git_mode" in src, "Task model must have git_mode column"
        assert 'default="branch_per_task"' in src, "Task.git_mode must default to branch_per_task"

    def test_webchat_session_has_git_mode(self):
        src_path = os.path.join("src", "taghdev", "models", "web_chat.py")
        with open(src_path) as f:
            src = f.read()
        assert "git_mode" in src, "WebChatSession model must have git_mode column"
        assert "session_branch_name" in src, "WebChatSession model must have session_branch_name column"


# ──────────────────────────────────────────────
# 2. GIT OPERATIONS
# ──────────────────────────────────────────────

class TestGitOps:
    def test_reset_hard_exists(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "git_ops.py")
        with open(src_path) as f:
            src = f.read()
        assert "async def reset_hard(" in src, "git_ops must have reset_hard function"
        assert '"git", "reset", "--hard", "HEAD"' in src, "reset_hard must reset to HEAD"


# ──────────────────────────────────────────────
# 3. GIT PROVIDER
# ──────────────────────────────────────────────

class TestGitProvider:
    def test_github_has_get_pr_for_branch(self):
        src_path = os.path.join("src", "taghdev", "providers", "git", "github.py")
        with open(src_path) as f:
            src = f.read()
        assert "async def get_pr_for_branch(" in src, "GitHubProvider must have get_pr_for_branch"
        assert '"gh", "pr", "view"' in src, "get_pr_for_branch must use gh pr view"

    def test_base_provider_has_get_pr_for_branch(self):
        src_path = os.path.join("src", "taghdev", "providers", "base.py")
        with open(src_path) as f:
            src = f.read()
        assert "get_pr_for_branch" in src, "GitProvider base must declare get_pr_for_branch"


# ──────────────────────────────────────────────
# 4. TASK CREATION — git_mode propagation
# ──────────────────────────────────────────────

class TestTaskCreation:
    def test_trigger_task_accepts_git_mode(self):
        src_path = os.path.join("src", "taghdev", "mcp_servers", "actions_mcp.py")
        with open(src_path) as f:
            src = f.read()
        assert "git_mode: str = \"branch_per_task\"" in src, "trigger_task must accept git_mode param"
        assert "resolved_git_mode" in src, "trigger_task must resolve git_mode from WebChatSession"
        assert "ws.git_mode" in src, "trigger_task must read git_mode from web session"

    def test_trigger_task_stores_git_mode_on_task(self):
        src_path = os.path.join("src", "taghdev", "mcp_servers", "actions_mcp.py")
        with open(src_path) as f:
            src = f.read()
        assert "git_mode=resolved_git_mode" in src, "trigger_task must store resolved git_mode on Task"

    def test_slack_task_defaults_to_branch_per_task(self):
        src_path = os.path.join("src", "taghdev", "providers", "chat", "slack", "handlers", "modals.py")
        with open(src_path) as f:
            src = f.read()
        assert 'git_mode="branch_per_task"' in src, "Slack task creation must default to branch_per_task"

    def test_telegram_task_defaults_to_branch_per_task(self):
        src_path = os.path.join("src", "taghdev", "providers", "chat", "telegram", "handlers", "task.py")
        with open(src_path) as f:
            src = f.read()
        assert 'git_mode="branch_per_task"' in src, "Telegram task creation must default to branch_per_task"


# ──────────────────────────────────────────────
# 5. ORCHESTRATOR — execute_task branching
# ──────────────────────────────────────────────

class TestOrchestratorExecuteTask:
    def test_direct_commit_skips_branch_creation(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert 'task.git_mode == "direct_commit"' in src, "orchestrator must check direct_commit mode"
        assert 'branch_name=None' in src, "direct_commit must set branch_name to None"
        assert "no branch created" in src.lower(), "direct_commit must log no branch creation"

    def test_session_branch_uses_or_creates_session_branch(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert 'task.git_mode == "session_branch"' in src, "orchestrator must check session_branch mode"
        assert "session_branch_name" in src, "session_branch must reference session_branch_name"
        assert "WebChatSession" in src, "session_branch must query WebChatSession"
        assert "session_branch_name = branch_name" in src, "session_branch must store branch on session"


# ──────────────────────────────────────────────
# 6. ORCHESTRATOR — approve_task branching
# ──────────────────────────────────────────────

class TestOrchestratorApproveTask:
    def test_direct_commit_commits_to_main(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert 'task.git_mode == "direct_commit"' in src, "approve_task must branch for direct_commit"
        assert "committing to main" in src.lower(), "direct_commit approve must title as committing to main"
        assert 'status="merged"' in src, "direct_commit must set status to merged after commit"

    def test_session_branch_checks_existing_pr(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert 'task.git_mode == "session_branch"' in src, "approve_task must branch for session_branch"
        assert "get_pr_for_branch" in src, "session_branch must check for existing PR"
        assert "existing" in src.lower(), "session_branch must handle existing PR case"


# ──────────────────────────────────────────────
# 7. ORCHESTRATOR — merge/reject/discard cleanup
# ──────────────────────────────────────────────

class TestOrchestratorCleanup:
    def test_merge_task_handles_direct_commit(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert 'task.git_mode == "direct_commit"' in src, "merge_task must handle direct_commit"
        assert "already merged" in src.lower(), "direct_commit merge must explain already merged"

    def test_reject_task_preserves_session_branch(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        # Session branch should NOT be deleted on reject
        assert 'task.git_mode == "branch_per_task"' in src, "reject must only delete branch for branch_per_task"

    def test_discard_task_preserves_session_branch(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert 'task.git_mode == "branch_per_task" and task.branch_name' in src, \
            "discard must only delete branch for branch_per_task"


# ──────────────────────────────────────────────
# 8. WEB API
# ──────────────────────────────────────────────

class TestWebAPI:
    def test_threads_endpoint_returns_git_mode(self):
        src_path = os.path.join("src", "taghdev", "api", "routes", "threads.py")
        with open(src_path) as f:
            src = f.read()
        assert '"gitMode": s.git_mode' in src or '"gitMode": result.git_mode' in src, "threads endpoints must return gitMode"

    def test_git_mode_patch_endpoint_exists(self):
        src_path = os.path.join("src", "taghdev", "api", "routes", "threads.py")
        with open(src_path) as f:
            src = f.read()
        assert '@router.patch("/threads/{thread_id}/git-mode")' in src, \
            "threads API must have git-mode PATCH endpoint"
        assert "valid_modes" in src, "git-mode endpoint must validate mode values"


# ──────────────────────────────────────────────
# 9. WEB FRONTEND
# ──────────────────────────────────────────────

class TestWebFrontend:
    def test_frontend_has_git_mode_state(self):
        src_path = os.path.join("chat_frontend", "src", "App.tsx")
        with open(src_path) as f:
            src = f.read()
        assert "activeGitMode" in src, "frontend must track activeGitMode state"
        assert "setActiveGitMode" in src, "frontend must have setActiveGitMode setter"

    def test_frontend_has_mode_selector_dropdown(self):
        src_path = os.path.join("chat_frontend", "src", "App.tsx")
        with open(src_path) as f:
            src = f.read()
        assert 'value={activeGitMode}' in src, "frontend must bind dropdown to activeGitMode"
        assert "updateGitMode" in src, "frontend must call updateGitMode on change"
        assert 'value="branch_per_task"' in src, "dropdown must have branch_per_task option"
        assert 'value="direct_commit"' in src, "dropdown must have direct_commit option"
        assert 'value="session_branch"' in src, "dropdown must have session_branch option"

    def test_frontend_displays_git_mode_in_prompt(self):
        src_path = os.path.join("chat_frontend", "src", "App.tsx")
        with open(src_path) as f:
            src = f.read()
        assert "gitMode" in src, "ChatThread interface must include gitMode"
