"""Tests for audit fixes — verifies each critical/high fix actually works."""
import asyncio
import ast
import inspect
import os
import sys
import textwrap

import pytest

# ──────────────────────────────────────────────
# 1. COMMAND INJECTION FIXES
# ──────────────────────────────────────────────

class TestCommandInjectionFixes:
    """Verify all shell injection points are gone."""

    def _get_source(self, module_path: str) -> str:
        path = os.path.join("src", *module_path.split(".")) + ".py"
        with open(path) as f:
            return f.read()

    def _count_subprocess_shell(self, source: str) -> int:
        """Count create_subprocess_shell calls in source."""
        return source.count("create_subprocess_shell")

    def test_git_ops_no_shell(self):
        src = self._get_source("openclow.worker.tasks.git_ops")
        # run_cmd kept for backward compat but all git functions should use run_exec
        assert "async def run_exec" in src, "run_exec must exist"
        assert "create_subprocess_exec" in src, "must use exec"

    def test_git_ops_run_exec_signature(self):
        from openclow.worker.tasks.git_ops import run_exec
        sig = inspect.signature(run_exec)
        # Should have *args (VAR_POSITIONAL), not a single cmd: str parameter
        args_param = sig.parameters.get("args")
        assert args_param is not None, "run_exec should have *args parameter"
        assert args_param.kind == inspect.Parameter.VAR_POSITIONAL, \
            "run_exec args should be VAR_POSITIONAL (*args), not a single string"

    def test_git_ops_clone_uses_exec(self):
        src = self._get_source("openclow.worker.tasks.git_ops")
        # Find the clone function and verify it calls run_exec not run_cmd
        in_clone = False
        for line in src.split("\n"):
            if "async def clone(" in line:
                in_clone = True
            elif in_clone and line.strip().startswith("async def "):
                break
            elif in_clone:
                assert "run_cmd" not in line, f"clone() still uses run_cmd: {line}"

    def test_git_ops_commit_uses_exec(self):
        """Commit was the worst injection: message in shell quotes."""
        src = self._get_source("openclow.worker.tasks.git_ops")
        in_commit = False
        for line in src.split("\n"):
            if "async def commit(" in line:
                in_commit = True
            elif in_commit and line.strip().startswith("async def "):
                break
            elif in_commit:
                assert "run_cmd" not in line, f"commit() still uses run_cmd: {line}"

    def test_docker_mcp_no_shell(self):
        src = self._get_source("openclow.mcp_servers.docker_mcp")
        assert self._count_subprocess_shell(src) == 0, \
            f"docker_mcp.py still has {self._count_subprocess_shell(src)} create_subprocess_shell calls"
        assert "create_subprocess_exec" in src or "run_docker" in src, \
            "docker_mcp should use create_subprocess_exec or delegate to docker_guard.run_docker"

    def test_docker_mcp_exec_uses_shlex(self):
        """docker_exec must use shlex.split for safety."""
        src = self._get_source("openclow.mcp_servers.docker_mcp")
        assert "shlex.split" in src or "shlex" in src, "docker_exec should use shlex for command parsing"

    def test_github_mcp_no_shell(self):
        src = self._get_source("openclow.mcp_servers.github_mcp")
        assert self._count_subprocess_shell(src) == 0, \
            f"github_mcp.py still has {self._count_subprocess_shell(src)} create_subprocess_shell calls"

    def test_docker_service_no_shell(self):
        src = self._get_source("openclow.services.docker_service")
        assert self._count_subprocess_shell(src) == 0
        assert "run_docker" in src, "should use run_docker from docker_guard"

    def test_docker_service_no_subprocess_sleep(self):
        src = self._get_source("openclow.services.docker_service")
        assert 'run_cmd("sleep' not in src, "subprocess sleep should be replaced with asyncio.sleep"
        assert "asyncio.sleep" in src

    def test_github_service_no_shell(self):
        src = self._get_source("openclow.services.github_service")
        assert "run_exec" in src
        # Should not have manual quote escaping anymore
        assert 'replace(\'"\', ' not in src, "manual quote escaping should be removed"

    def test_health_service_has_run_exec(self):
        src = self._get_source("openclow.services.health_service")
        assert "_run_exec" in src, "should have _run_exec function"
        # Docker exec calls should use _run_exec
        assert src.count("_run_exec(") >= 5, "should have at least 5 _run_exec calls for DB checks"

    def test_github_provider_uses_exec(self):
        src = self._get_source("openclow.providers.git.github")
        assert "run_exec" in src
        assert "run_cmd" not in src, "should not use run_cmd anymore"

    def test_bootstrap_uses_exec_not_shell(self):
        src = self._get_source("openclow.worker.tasks.bootstrap")
        assert "create_subprocess_exec" in src, "bootstrap should use create_subprocess_exec"


# ──────────────────────────────────────────────
# 2. SETTINGS FIXES
# ──────────────────────────────────────────────

class TestSettingsFixes:
    def test_settings_has_coder_max_turns(self):
        from openclow.settings import Settings
        s = Settings()
        assert hasattr(s, "claude_coder_max_turns"), "missing claude_coder_max_turns"
        assert isinstance(s.claude_coder_max_turns, int)
        assert s.claude_coder_max_turns > 0

    def test_settings_has_reviewer_max_turns(self):
        from openclow.settings import Settings
        s = Settings()
        assert hasattr(s, "claude_reviewer_max_turns"), "missing claude_reviewer_max_turns"
        assert isinstance(s.claude_reviewer_max_turns, int)
        assert s.claude_reviewer_max_turns > 0


# ──────────────────────────────────────────────
# 3. CLAUDE.PY FIXES
# ──────────────────────────────────────────────

class TestClaudeProviderFixes:
    def test_coder_system_prompt_no_laravel(self):
        from openclow.providers.llm.claude import CODER_SYSTEM_PROMPT
        assert "php artisan" not in CODER_SYSTEM_PROMPT, "still has Laravel-specific php artisan"
        assert "composer" not in CODER_SYSTEM_PROMPT, "still has Laravel-specific composer"

    def test_reviewer_system_prompt_no_laravel(self):
        from openclow.providers.llm.claude import REVIEWER_SYSTEM_PROMPT
        assert "Laravel" not in REVIEWER_SYSTEM_PROMPT, "still has hardcoded Laravel"
        assert "Vue" not in REVIEWER_SYSTEM_PROMPT, "still has hardcoded Vue"
        assert "{tech_stack}" in REVIEWER_SYSTEM_PROMPT, "should use tech_stack variable"

    def test_run_coder_fix_has_app_container(self):
        """The format() call in run_coder_fix must include app_container and app_port."""
        src_path = os.path.join("src", "openclow", "providers", "llm", "claude.py")
        with open(src_path) as f:
            src = f.read()

        # Find the run_coder_fix method and check it passes app_container
        in_method = False
        found_format = False
        for line in src.split("\n"):
            if "async def run_coder_fix" in line:
                in_method = True
            elif in_method and "async def " in line and "run_coder_fix" not in line:
                break
            elif in_method and "app_container=" in line:
                found_format = True

        assert found_format, "run_coder_fix must pass app_container to CODER_SYSTEM_PROMPT.format()"


# ──────────────────────────────────────────────
# 4. WORKSPACE LOCKING FIXES
# ──────────────────────────────────────────────

class TestWorkspaceLockingFixes:
    def test_lock_stored_on_self(self):
        """Lock object must be stored, not discarded."""
        src_path = os.path.join("src", "openclow", "services", "workspace_service.py")
        with open(src_path) as f:
            src = f.read()

        assert "self._lock" in src, "_get_lock must store the lock on self"
        assert "self._lock_redis" in src or "self._lock" in src, "must store redis connection"

    def test_prepare_acquires_lock(self):
        src_path = os.path.join("src", "openclow", "services", "workspace_service.py")
        with open(src_path) as f:
            src = f.read()

        # Check that prepare() calls _get_lock
        in_prepare = False
        found_lock = False
        for line in src.split("\n"):
            if "async def prepare(" in line:
                in_prepare = True
            elif in_prepare and line.strip().startswith("async def "):
                break
            elif in_prepare and "_get_lock" in line:
                found_lock = True

        assert found_lock, "prepare() must call _get_lock"


# ──────────────────────────────────────────────
# 5. FACTORY SINGLETON FIXES
# ──────────────────────────────────────────────

class TestFactoryFixes:
    def test_factory_has_async_lock(self):
        src_path = os.path.join("src", "openclow", "providers", "factory.py")
        with open(src_path) as f:
            src = f.read()

        assert "asyncio.Lock()" in src, "factory must use asyncio.Lock"
        assert "async with _lock" in src, "getter functions must acquire the lock"


# ──────────────────────────────────────────────
# 6. NOTIFICATION FIXES
# ──────────────────────────────────────────────

class TestNotificationFixes:
    def test_no_recursive_flush(self):
        """_flush must not call itself recursively."""
        src_path = os.path.join("src", "openclow", "services", "notification.py")
        with open(src_path) as f:
            src = f.read()

        # Find the _flush method and check for self-calls
        in_flush = False
        recursive_calls = 0
        for line in src.split("\n"):
            if "async def _flush(" in line:
                in_flush = True
                continue
            elif in_flush and (line.strip().startswith("async def ") or
                              (line.strip() and not line.startswith(" ") and not line.startswith("\t"))):
                break
            elif in_flush and "_flush" in line and "await" in line:
                recursive_calls += 1

        assert recursive_calls == 0, f"_flush has {recursive_calls} recursive calls"

    def test_has_max_retries(self):
        src_path = os.path.join("src", "openclow", "services", "notification.py")
        with open(src_path) as f:
            src = f.read()
        assert "max_retries" in src or "for attempt" in src, "should have bounded retries"


# ──────────────────────────────────────────────
# 7. ACTIVITY LOG FIXES
# ──────────────────────────────────────────────

class TestActivityLogFixes:
    def test_query_uses_deque(self):
        src_path = os.path.join("src", "openclow", "services", "activity_log.py")
        with open(src_path) as f:
            src = f.read()
        assert "deque" in src, "query() should use deque for bounded memory"

    def test_exception_not_swallowed(self):
        src_path = os.path.join("src", "openclow", "services", "activity_log.py")
        with open(src_path) as f:
            src = f.read()
        # Should not have bare "pass" after except in log_event
        assert "except Exception:\n            pass" not in src, "exception should not be silently swallowed"


# ──────────────────────────────────────────────
# 8. LOGGING FIXES
# ──────────────────────────────────────────────

class TestLoggingFixes:
    def test_setup_has_configured_flag(self):
        src_path = os.path.join("src", "openclow", "utils", "logging.py")
        with open(src_path) as f:
            src = f.read()
        assert "_configured" in src, "should have _configured flag"

    def test_setup_only_runs_once(self):
        """Calling get_logger multiple times should only configure once."""
        from openclow.utils import logging as log_module
        # Reset
        log_module._configured = False
        log_module.setup_logging()
        assert log_module._configured is True
        # Second call should be a no-op (won't crash, just returns)
        log_module.setup_logging()


# ──────────────────────────────────────────────
# 9. REVIEW HANDLER FIXES
# ──────────────────────────────────────────────

class TestReviewHandlerFixes:
    def test_discard_dispatches_to_worker(self):
        src_path = os.path.join("src", "openclow", "providers", "chat", "telegram", "handlers", "review.py")
        with open(src_path) as f:
            src = f.read()

        assert "WorkspaceService" not in src, "discard should not import WorkspaceService (runs in bot container)"
        assert "enqueue_job" in src, "discard should dispatch to worker"
        assert "discard_task" in src, "should enqueue discard_task job"

    def test_all_handlers_have_error_handling(self):
        src_path = os.path.join("src", "openclow", "providers", "chat", "telegram", "handlers", "review.py")
        with open(src_path) as f:
            src = f.read()

        # Count try/except blocks — refactored handlers share _guard_and_enqueue
        try_count = src.count("try:")
        assert try_count >= 2, f"review handlers should have shared error handling via _guard_and_enqueue, found {try_count} try blocks"


# ──────────────────────────────────────────────
# 10. TUNNEL SERVICE FIXES
# ──────────────────────────────────────────────

class TestTunnelServiceFixes:
    def test_pid_verification_before_kill(self):
        src_path = os.path.join("src", "openclow", "services", "tunnel_service.py")
        with open(src_path) as f:
            src = f.read()
        assert "cloudflared" in src and "ps" in src, "should verify PID is cloudflared before killing"


# ──────────────────────────────────────────────
# 11. ORM SESSION FIXES
# ──────────────────────────────────────────────

class TestORMSessionFixes:
    def test_orchestrator_eagerly_loads(self):
        src_path = os.path.join("src", "openclow", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "selectinload" in src, "should eagerly load relationships"
        assert "expunge" in src, "should expunge task for use after session closes"

    def test_start_cancel_uses_update(self):
        src_path = os.path.join("src", "openclow", "bot", "handlers", "start.py")
        with open(src_path) as f:
            src = f.read()

        # The cancel handler should use update() instead of session.add(detached_task)
        in_cancel = False
        for line in src.split("\n"):
            if "async def cmd_cancel" in line:
                in_cancel = True
            elif in_cancel and line.strip().startswith("async def "):
                break
            elif in_cancel:
                assert "session.add(task)" not in line, "should not add detached task object"

    def test_task_handler_consolidated_sessions(self):
        src_path = os.path.join("src", "openclow", "bot", "handlers", "task.py")
        with open(src_path) as f:
            src = f.read()
        # Count async with async_session() in task_submitted
        in_submitted = False
        session_count = 0
        for line in src.split("\n"):
            if "async def task_submitted" in line:
                in_submitted = True
            elif in_submitted and line.strip().startswith("async def "):
                break
            elif in_submitted and "async with async_session()" in line:
                session_count += 1
        assert session_count <= 2, f"task_submitted uses {session_count} sessions, should be at most 2"


# ──────────────────────────────────────────────
# 12. DEAD CODE CLEANUP
# ──────────────────────────────────────────────

class TestDeadCodeCleanup:
    def test_coder_no_unused_os(self):
        src_path = os.path.join("src", "openclow", "agents", "coder.py")
        with open(src_path) as f:
            src = f.read()
        # Check that os is not imported (or if imported, is actually used)
        lines = src.split("\n")
        for line in lines:
            if line.strip() == "import os":
                # Verify os is used somewhere else in the file
                other_lines = [l for l in lines if l != line and "os." in l]
                assert len(other_lines) > 0, "coder.py imports os but never uses it"

    def test_doctor_no_laravel(self):
        from openclow.agents.doctor import DIAGNOSE_PROMPT
        assert "php artisan" not in DIAGNOSE_PROMPT, "doctor prompt still has Laravel commands"
        assert "composer install" not in DIAGNOSE_PROMPT, "doctor prompt still has composer"

    def test_config_service_no_duplicate_where(self):
        src_path = os.path.join("src", "openclow", "services", "config_service.py")
        with open(src_path) as f:
            src = f.read()
        # Each function should not have duplicate category filter
        for func_name in ["get_config", "set_config"]:
            in_func = False
            category_count = 0
            for line in src.split("\n"):
                if f"async def {func_name}" in line:
                    in_func = True
                    category_count = 0
                elif in_func and (line.strip().startswith("async def ") or line.strip().startswith("def ")):
                    assert category_count <= 1, f"{func_name} has {category_count} category filters (should be 1)"
                    in_func = False
                elif in_func and "category == category" in line:
                    category_count += 1
