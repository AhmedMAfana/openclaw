"""Onboarding Agent — auto-discovers project configuration from a repo."""
import re
from dataclasses import dataclass

from openclow.utils.logging import get_logger

log = get_logger()

ONBOARDING_PROMPT = """Analyze this repository and extract project configuration.

Look for:
1. Docker setup: find docker-compose.yml (check root, infra/, docker/, .docker/, etc.)
2. App service: identify the main application service name and its port
3. Tech stack: detect from files (package.json, composer.json, requirements.txt, Gemfile, go.mod, etc.)
4. Description: read README.md for a short project description
5. Conventions: read CLAUDE.md if it exists
6. Setup commands: any needed setup (cp .env.example .env, etc.)

Output in this EXACT format (no extra text before or after):
PROJECT_CONFIG_START
PROJECT_NAME: <name derived from repo>
TECH_STACK: <comma-separated list>
DOCKER_COMPOSE: <relative path to docker-compose.yml, or "none">
APP_CONTAINER: <main app service name from docker-compose, or "none">
APP_PORT: <port the app runs on, or "none">
DESCRIPTION: <one-line description>
SETUP_COMMANDS: <semicolon-separated commands, or "none">
IS_DOCKERIZED: <true or false>
PROJECT_CONFIG_END
"""


@dataclass
class ProjectConfig:
    name: str
    tech_stack: str
    docker_compose: str | None
    app_container: str | None
    app_port: int | None
    description: str
    setup_commands: str | None
    is_dockerized: bool


def parse_config(output: str) -> ProjectConfig | None:
    """Parse the agent's structured output into ProjectConfig."""
    match = re.search(r"PROJECT_CONFIG_START\n(.+?)PROJECT_CONFIG_END", output, re.DOTALL)
    if not match:
        log.error("onboarding.parse_failed", output=output[:500])
        return None

    block = match.group(1)
    fields = {}
    for line in block.strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()

    port = fields.get("APP_PORT", "none")
    port_int = int(port) if port.isdigit() else None

    return ProjectConfig(
        name=fields.get("PROJECT_NAME", "unknown"),
        tech_stack=fields.get("TECH_STACK", ""),
        docker_compose=fields.get("DOCKER_COMPOSE") if fields.get("DOCKER_COMPOSE") != "none" else None,
        app_container=fields.get("APP_CONTAINER") if fields.get("APP_CONTAINER") != "none" else None,
        app_port=port_int,
        description=fields.get("DESCRIPTION", ""),
        setup_commands=fields.get("SETUP_COMMANDS") if fields.get("SETUP_COMMANDS") != "none" else None,
        is_dockerized=fields.get("IS_DOCKERIZED", "false").lower() == "true",
    )


async def _fallback_analyze(workspace_path: str) -> ProjectConfig | None:
    """Fallback: analyze repo by reading files directly (no Claude needed)."""
    import json
    import os

    name = os.path.basename(workspace_path).replace("_onboard-", "").replace("-", "_")
    # Try to get name from git remote
    try:
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "git", "remote", "get-url", "origin", cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        url = stdout.decode().strip()
        if "github.com" in url:
            name = url.split("/")[-1].replace(".git", "")
    except Exception:
        pass

    tech = []
    docker_compose = None
    app_container = None
    app_port = None
    description = ""
    setup_commands = None
    is_dockerized = False

    # Detect tech stack from files
    checks = {
        "package.json": "Node.js", "composer.json": "PHP/Laravel",
        "requirements.txt": "Python", "pyproject.toml": "Python",
        "go.mod": "Go", "Gemfile": "Ruby", "Cargo.toml": "Rust",
        "pom.xml": "Java", "build.gradle": "Java/Kotlin",
    }
    for filename, stack in checks.items():
        if os.path.exists(os.path.join(workspace_path, filename)):
            tech.append(stack)
            # Try to read more detail
            if filename == "package.json":
                try:
                    with open(os.path.join(workspace_path, filename)) as f:
                        pkg = json.load(f)
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    for key in ["react", "vue", "angular", "next", "nuxt", "express", "fastify"]:
                        if key in deps:
                            tech.append(key.capitalize())
                except Exception:
                    pass
            if filename == "composer.json":
                try:
                    with open(os.path.join(workspace_path, filename)) as f:
                        comp = json.load(f)
                    req = comp.get("require", {})
                    if "laravel/framework" in req:
                        tech.append("Laravel")
                except Exception:
                    pass

    # Detect docker
    for dc_path in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
                     "infra/docker-compose.yml", "docker/docker-compose.yml"]:
        full = os.path.join(workspace_path, dc_path)
        if os.path.exists(full):
            docker_compose = dc_path
            is_dockerized = True
            try:
                with open(full) as f:
                    content = f.read()
                # Simple parsing: find service names (lines like "  servicename:")
                import re as _re
                svc_matches = _re.findall(r"^  (\w[\w-]*):\s*$", content, _re.MULTILINE)
                for svc_name in svc_matches:
                    if svc_name in ["app", "web", "api", "php", "node", "python", name]:
                        app_container = svc_name
                        break
                if not app_container and svc_matches:
                    # Skip infra services
                    for s in svc_matches:
                        if s not in ["postgres", "redis", "mysql", "nginx", "traefik", "volumes", "networks"]:
                            app_container = s
                            break
                # Find port
                port_match = _re.search(r"ports:\s*\n\s*-\s*[\"']?(\d+):", content)
                if port_match:
                    app_port = int(port_match.group(1))
            except Exception:
                pass
            break

    # Read README for description
    for readme in ["README.md", "readme.md", "README.rst"]:
        rpath = os.path.join(workspace_path, readme)
        if os.path.exists(rpath):
            try:
                with open(rpath) as f:
                    lines = f.readlines()
                for line in lines[:10]:
                    stripped = line.strip().lstrip("#").strip()
                    if stripped and len(stripped) > 10:
                        description = stripped[:200]
                        break
            except Exception:
                pass
            break

    # Check for .env.example
    if os.path.exists(os.path.join(workspace_path, ".env.example")):
        setup_commands = "cp .env.example .env"

    if not tech:
        tech = ["Unknown"]

    log.info("onboarding.fallback_success", name=name, tech=", ".join(tech))
    return ProjectConfig(
        name=name,
        tech_stack=", ".join(tech),
        docker_compose=docker_compose,
        app_container=app_container,
        app_port=app_port,
        description=description or f"{name} project",
        setup_commands=setup_commands,
        is_dockerized=is_dockerized,
    )


async def analyze_repo(workspace_path: str, on_progress=None) -> ProjectConfig | None:
    """Run the onboarding agent to analyze a cloned repo.

    Streams live progress via on_progress callback so the user
    sees what the agent is doing in real-time.
    """
    from claude_agent_sdk import query, ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock

    # Onboarding is read-only file scanning — Sonnet is fast, no MCP needed
    options = ClaudeAgentOptions(
        cwd=workspace_path,
        system_prompt="You are a project analyzer. Only read files, never modify anything.",
        model="claude-sonnet-4-6",  # File scanning — Sonnet is faster and cheaper
        allowed_tools=["Read", "Glob", "Grep"],  # Read-only, no MCP needed
        permission_mode="bypassPermissions",
        max_turns=8,  # File scanning shouldn't need 15 turns
    )

    log.info("onboarding.started", workspace=workspace_path)
    full_output = ""
    try:
        async for message in query(prompt=ONBOARDING_PROMPT, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text
                    elif isinstance(block, ToolUseBlock) and on_progress:
                        # Show what file/tool the agent is reading
                        tool = block.name if hasattr(block, "name") else ""
                        if hasattr(block, "input") and isinstance(block.input, dict):
                            target = block.input.get("file_path", block.input.get("pattern", ""))
                            if target:
                                short = str(target).split("/")[-1][:40]
                                if "Glob" in tool:
                                    await on_progress(f"Scanning: {short}")
                                elif "Read" in tool:
                                    await on_progress(f"Reading: {short}")
                                elif "Grep" in tool:
                                    await on_progress(f"Searching: {short}")
    except Exception as e:
        log.error("onboarding.agent_failed", error=str(e)[:300])
        if on_progress:
            await on_progress("Falling back to direct file analysis...")
        return await _fallback_analyze(workspace_path)

    config = parse_config(full_output)
    if config:
        log.info("onboarding.success", name=config.name, tech=config.tech_stack,
                 container=config.app_container, port=config.app_port)
    return config
