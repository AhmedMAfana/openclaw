"""Tests for the Never Give Up retry system — agent retries instead of failing on stall."""
import ast
import os

import pytest


class TestRetrySettings:
    def test_settings_has_retry_config(self):
        src_path = os.path.join("src", "taghdev", "settings.py")
        with open(src_path) as f:
            src = f.read()
        assert "coder_max_retries" in src, "Settings must have coder_max_retries"
        assert "coder_retry_enabled" in src, "Settings must have coder_retry_enabled"


class TestRetryException:
    def test_agent_retry_needed_exists(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "class AgentRetryNeeded" in src, "Must define AgentRetryNeeded exception"
        assert "reason" in src, "AgentRetryNeeded must have a reason attribute"

    def test_max_turns_detection_exists(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "_is_max_turns_reached" in src, "Must have _is_max_turns_reached helper"
        assert "max_turns_reached" in src, "Must detect max_turns_reached"


class TestRetryLoopStructure:
    def test_coder_run_is_in_retry_loop(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "while coding_attempt <= max_coding_retries" in src, \
            "Coder run must be wrapped in a retry while loop"
        assert "coding_attempt" in src, "Must track coding_attempt counter"
        assert "max_coding_retries" in src, "Must use max_coding_retries limit"

    def test_stall_raises_retry_not_runtime_error(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "raise AgentRetryNeeded(\"stalled\"" in src, \
            "Stall must raise AgentRetryNeeded, not RuntimeError"
        # Old RuntimeError should be gone
        assert 'raise RuntimeError(\n                        f"Agent stalled' not in src, \
            "Old RuntimeError stall must be removed"

    def test_retry_catches_agent_retry_needed(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "except AgentRetryNeeded as e:" in src, \
            "Retry loop must catch AgentRetryNeeded"

    def test_empty_diff_raises_retry_needed(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert 'raise AgentRetryNeeded("empty_diff"' in src, \
            "Empty diff must raise AgentRetryNeeded"


class TestRecoveryPrompts:
    def test_recovery_prompts_escalate(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "_build_recovery_prompt" in src, "Must have _build_recovery_prompt helper"
        assert "MOST IMPORTANT change" in src, "Recovery prompt 1 must focus agent"
        assert "concrete file edits NOW" in src, "Recovery prompt 2 must be more direct"
        assert "SIMPLEST possible approach" in src, "Recovery prompt 3 must be nuclear"

    def test_recovery_prompt_uses_attempt_number(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "coding_attempt - 1" in src or "attempt - 1" in src, \
            "Recovery prompt must use attempt number to escalate"


class TestRetryWorkspacePrep:
    def test_prepare_retry_workspace_exists(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "_prepare_retry_workspace" in src, "Must have _prepare_retry_workspace helper"
        assert "reset_hard" in src, "Must reset workspace before retry"


class TestRetryNotification:
    def test_notify_retry_exists(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "_notify_retry" in src, "Must have _notify_retry helper"
        assert "orchestrator.retrying" in src, "Must log retry attempts"

    def test_notify_shows_attempt_count(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "coder_max_retries" in src, "Notification must show max retries"


class TestUserPromptAfterMaxRetries:
    def test_ask_user_continue_or_cancel_exists(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "_ask_user_continue_or_cancel" in src, \
            "Must have _ask_user_continue_or_cancel helper"
        assert "Keep Trying" in src, "Must offer 'Keep Trying' button"
        assert "Cancel" in src, "Must offer 'Cancel' button"

    def test_after_max_retries_asks_user(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "Max retries exhausted" in src or "max retries" in src.lower(), \
            "After max retries must ask user, not fail silently"


class TestCancelRaceCondition:
    def test_cancel_handler_checks_user_cancelled(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "user_cancelled" in src, "Cancel handler must check if user already cancelled"
        assert "taghdev:cancel_session" in src, "Cancel handler must check Redis cancel key"
        assert "not user_cancelled" in src, "Cancel handler must skip card render if user cancelled"


class TestCardButtons:
    def test_checklist_reporter_builds_buttons(self):
        src_path = os.path.join("src", "taghdev", "services", "checklist_reporter.py")
        with open(src_path) as f:
            src = f.read()
        assert "buttons" in src, "_build_card must include buttons field"
        assert "_keyboard_to_buttons" in src, "Must have _keyboard_to_buttons helper"

    def test_retry_keyboard_has_discard(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "Discard" in src, "_retry_keyboard must include Discard button"
        assert "discard_task:" in src, "Discard button action must be discard_task:"

    def test_discard_allows_failed_status_in_orchestrator(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert '"failed"' in src and "discard_task" in src, \
            "discard_task valid statuses must include failed"

    def test_discard_allows_failed_status_in_bot_actions(self):
        src_path = os.path.join("src", "taghdev", "services", "bot_actions.py")
        with open(src_path) as f:
            src = f.read()
        assert '"failed"' in src and "discard" in src, \
            "bot_actions discard expected status must include failed"

    def test_threads_api_has_action_endpoint(self):
        src_path = os.path.join("src", "taghdev", "api", "routes", "threads.py")
        with open(src_path) as f:
            src = f.read()
        assert '"/threads/{session_id}/action"' in src, \
            "threads API must have session action endpoint"

    def test_frontend_card_renders_buttons(self):
        src_path = os.path.join("chat_frontend", "src", "components", "assistant-ui", "thread.tsx")
        with open(src_path) as f:
            src = f.read()
        assert "card.buttons" in src, "WorkerProgressCard must render card.buttons"
        assert "/threads/" in src and "/action" in src, \
            "Frontend must call session action endpoint"


class TestOldStallBehaviorRemoved:
    def test_no_agent_stopped_message(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        assert "Agent stopped making progress" not in src, \
            "Old 'Agent stopped making progress' message must be removed"

    def test_stall_branch_removed_from_outer_handler(self):
        src_path = os.path.join("src", "taghdev", "worker", "tasks", "orchestrator.py")
        with open(src_path) as f:
            src = f.read()
        # Old pattern: elif "stalled" in error_str must be gone
        assert 'elif "stalled" in error_str' not in src, \
            "Old stall branch must be removed from exception handler"
