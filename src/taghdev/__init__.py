"""TAGH Dev package — AI-powered development orchestration."""

# SECURITY PATCH: Disable Bash tool in claude_agent_sdk.
#
# Agents running inside the worker container must NOT use Bash for docker
# commands. Bash bypasses docker_guard.py's path translation, causing Docker
# Desktop to receive container paths (/workspaces/...) that don't exist on
# the host — which crashes Docker Desktop.
#
# All docker operations must go through MCP tools (compose_up, compose_build,
# docker_exec, etc.) which inject --project-directory with the host path.
#
# This patch adds "Bash" to disallowed_tools on every ClaudeAgentOptions
# instance, regardless of where it's created in the codebase.
try:
    from claude_agent_sdk import ClaudeAgentOptions

    _original_claude_options_init = ClaudeAgentOptions.__init__

    def _patched_claude_options_init(self, *args, **kwargs):
        _original_claude_options_init(self, *args, **kwargs)
        if "Bash" not in self.disallowed_tools:
            self.disallowed_tools = list(self.disallowed_tools) + ["Bash"]

    ClaudeAgentOptions.__init__ = _patched_claude_options_init
except Exception:
    pass
