"""Onboarding Agent — auto-discovers project configuration from a repo."""
import re
from dataclasses import dataclass

from openclow.utils.logging import get_logger

log = get_logger()

ONBOARDING_PROMPT = """Analyze this repository and extract the project configuration needed to run it.

## What to Read

1. Find the Docker Compose file — check these locations in order:
   docker-compose.yml, docker-compose.yaml, infra/docker-compose.yml, docker/docker-compose.yml,
   .docker/docker-compose.yml, docker-compose.prod.yml
   Use the first one found. Prefer a file with an "app" or "web" service.

2. Identify the main application service in the compose file:
   - Look for a service named: app, web, api, server, backend, laravel, django, rails, node
   - Read its "ports" to find the host-mapped port (e.g. "8000:8000" → port 8000)
   - Record the service name exactly as it appears in the compose file

3. Detect tech stack from files present:
   - composer.json → PHP/Laravel (or Symfony, Slim, etc.)
   - package.json → Node.js/Express or Next.js/Vue/React (check "dependencies" for framework)
   - requirements.txt or pyproject.toml → Python (check for django, fastapi, flask, etc.)
   - Gemfile → Ruby on Rails
   - go.mod → Go
   - pom.xml → Java/Spring
   List the primary language + framework, e.g. "PHP/Laravel", "Python/FastAPI", "Node.js/Express"

4. Read README.md — extract a one-sentence description of what the project does

5. Check for CLAUDE.md — if present, extract any developer conventions or setup requirements

6. Identify setup commands needed before `docker compose up`:
   - .env.example present → "cp .env.example .env"
   - Any seed or key generation steps mentioned in README? Include them.
   Separate multiple commands with semicolons.

## Output Format

Output ONLY this block — no text before or after:

PROJECT_CONFIG_START
PROJECT_NAME: <slug name derived from repo folder or git remote — lowercase, underscores>
TECH_STACK: <primary language/framework, e.g. PHP/Laravel>
DOCKER_COMPOSE: <relative path to docker-compose file, or "none">
APP_CONTAINER: <exact service name from compose file, or "none">
APP_PORT: <port number the app listens on inside compose, or "none">
DESCRIPTION: <one sentence describing what this project does>
SETUP_COMMANDS: <semicolon-separated setup commands, or "none">
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
    # Host-mode (mode="host") fields — all optional; stay None for Docker projects.
    install_guide_path: str | None = None
    start_command: str | None = None
    stop_command: str | None = None
    health_url: str | None = None
    process_manager: str | None = None


HOST_ONBOARDING_PROMPT = """Analyze this repository — it lives on the VPS host as an already-installed
(and possibly already-running) app. There is no Docker wrapping it. The deploy team
installs and starts it by following the project's own install guide.

## What to Read

1. Find the install guide — check in this order and STOP at the first match:
   README.md, README.rst, INSTALL.md, SETUP.md, docs/INSTALL.md, CLAUDE.md
   Record its relative path.

2. Detect tech stack from files present:
   - package.json → Node.js (Express / Next.js / Vue / React — check dependencies)
   - composer.json → PHP (Laravel / Symfony)
   - requirements.txt or pyproject.toml → Python (FastAPI / Django / Flask)
   - go.mod → Go
   - Gemfile → Ruby on Rails
   List the primary language + framework (e.g. "Python/FastAPI", "PHP/Laravel").

3. From the install guide, extract the EXACT shell commands a fresh deploy would run
   to install dependencies (e.g. `pip install -r requirements.txt`,
   `npm install && npm run build`, `composer install && php artisan migrate`).
   Join them with semicolons in execution order.

4. From the install guide, extract the start command the app uses to run in
   production or long-running dev mode. Examples:
   - "uvicorn app.main:app --host 0.0.0.0 --port 8000"
   - "npm start" or "PORT=3000 npm start"
   - "php artisan serve --host=0.0.0.0 --port=8000"
   - "pm2 start ecosystem.config.js"
   If the README says "run it with pm2", record it as a pm2 command.

5. Find the PORT the app listens on. Look in the start command first,
   then .env.example, then the README.

6. If documented, record the stop command (e.g. "pm2 stop ecosystem").

7. Classify the process manager based on the start command and any notes:
   pm2 | systemd | supervisor | manual (manual = plain nohup/setsid or just `npm start`).

8. Determine the health-check URL. Prefer an explicit health endpoint from the
   README (e.g. /health, /healthz, /api/ping). Fallback: http://localhost:<PORT>/

9. Read README's first substantive line as a one-sentence description.

## Output Format

Output ONLY this block — no text before or after:

PROJECT_CONFIG_START
PROJECT_NAME: <slug derived from the folder name — lowercase, dashes ok>
TECH_STACK: <primary language/framework>
INSTALL_GUIDE: <relative path to README/INSTALL>
APP_PORT: <port number, or "none">
DESCRIPTION: <one sentence>
SETUP_COMMANDS: <semicolon-separated install commands, or "none">
START_COMMAND: <the run command, or "none">
STOP_COMMAND: <the stop command, or "none">
PROCESS_MANAGER: <pm2|systemd|supervisor|manual>
HEALTH_URL: <URL to hit for health, or "none">
MODE: host
PROJECT_CONFIG_END
"""


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

    def _clean(key: str) -> str | None:
        v = fields.get(key)
        if v is None or v.lower() in ("none", "", "n/a"):
            return None
        return v

    return ProjectConfig(
        name=fields.get("PROJECT_NAME", "unknown"),
        tech_stack=fields.get("TECH_STACK", ""),
        docker_compose=_clean("DOCKER_COMPOSE"),
        app_container=_clean("APP_CONTAINER"),
        app_port=port_int,
        description=fields.get("DESCRIPTION", ""),
        setup_commands=_clean("SETUP_COMMANDS"),
        is_dockerized=fields.get("IS_DOCKERIZED", "false").lower() == "true",
        install_guide_path=_clean("INSTALL_GUIDE"),
        start_command=_clean("START_COMMAND"),
        stop_command=_clean("STOP_COMMAND"),
        health_url=_clean("HEALTH_URL"),
        process_manager=_clean("PROCESS_MANAGER"),
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


# ---------------------------------------------------------------------------
# Host-mode onboarding (mode="host" — app already lives on the VPS host)
# ---------------------------------------------------------------------------


async def _fallback_analyze_host(project_dir: str) -> ProjectConfig | None:
    """Direct file analysis for host-mode when the Claude agent isn't available."""
    import asyncio
    import json
    import os
    import re as _re

    name = os.path.basename(project_dir.rstrip("/")) or "project"
    # Prefer the git remote slug
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "remote", "get-url", "origin", cwd=project_dir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        url = stdout.decode().strip()
        if url:
            slug = url.rstrip("/").split("/")[-1].replace(".git", "")
            if slug:
                name = slug
    except Exception:
        pass

    tech: list[str] = []
    description = ""
    setup_commands = None
    start_command = None
    stop_command = None
    process_manager = "manual"
    app_port: int | None = None
    install_guide_path = None

    # install guide — first hit wins
    guide_body = ""
    for candidate in ["README.md", "README.rst", "README.txt",
                      "INSTALL.md", "SETUP.md", "docs/INSTALL.md", "CLAUDE.md"]:
        p = os.path.join(project_dir, candidate)
        if os.path.isfile(p):
            install_guide_path = candidate
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    guide_body = f.read(8192)
            except Exception:
                guide_body = ""
            break

    if guide_body:
        for line in guide_body.splitlines()[:25]:
            stripped = line.strip().lstrip("#").strip()
            if stripped and len(stripped) > 10:
                description = stripped[:200]
                break

    # tech stack
    checks = {
        "package.json": "Node.js",
        "composer.json": "PHP/Laravel",
        "requirements.txt": "Python",
        "pyproject.toml": "Python",
        "go.mod": "Go",
        "Gemfile": "Ruby",
        "Cargo.toml": "Rust",
        "pom.xml": "Java",
    }
    for filename, stack in checks.items():
        if os.path.exists(os.path.join(project_dir, filename)):
            tech.append(stack)
            if filename == "package.json":
                try:
                    with open(os.path.join(project_dir, filename)) as f:
                        pkg = json.load(f)
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    for key in ["next", "nuxt", "vue", "react", "express", "fastify"]:
                        if key in deps:
                            tech.append(key.capitalize())
                            break
                    scripts = pkg.get("scripts") or {}
                    if "start" in scripts:
                        start_command = "npm start"
                    setup_commands = "npm install" + (" && npm run build" if "build" in scripts else "")
                except Exception:
                    pass
            elif filename == "composer.json":
                try:
                    with open(os.path.join(project_dir, filename)) as f:
                        comp = json.load(f)
                    if "laravel/framework" in comp.get("require", {}):
                        tech.append("Laravel")
                        start_command = "php artisan serve"
                        setup_commands = "composer install"
                        if os.path.exists(os.path.join(project_dir, ".env.example")):
                            setup_commands += "; cp .env.example .env; php artisan key:generate; php artisan migrate --force"
                except Exception:
                    pass
            elif filename == "requirements.txt" or filename == "pyproject.toml":
                setup_commands = "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
                # best-effort start command for FastAPI/Django
                if os.path.exists(os.path.join(project_dir, "app", "main.py")):
                    start_command = ".venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
                elif os.path.exists(os.path.join(project_dir, "manage.py")):
                    start_command = ".venv/bin/python manage.py runserver 0.0.0.0:8000"

    # port: scan guide body for `--port 1234` or `PORT=1234`
    if guide_body:
        m = _re.search(r"--port[=\s]+(\d{2,5})", guide_body)
        if m:
            app_port = int(m.group(1))
        else:
            m = _re.search(r"\bPORT\s*=\s*(\d{2,5})\b", guide_body)
            if m:
                app_port = int(m.group(1))

    # process manager hint
    if guide_body and "pm2" in guide_body.lower():
        process_manager = "pm2"
    elif guide_body and "systemctl" in guide_body.lower():
        process_manager = "systemd"

    if not tech:
        tech = ["Unknown"]

    health_url = f"http://localhost:{app_port}/" if app_port else None

    log.info("onboarding.host_fallback_success", name=name, tech=", ".join(tech))
    return ProjectConfig(
        name=name,
        tech_stack=", ".join(tech),
        docker_compose=None,
        app_container=None,
        app_port=app_port,
        description=description or f"{name} (host-mode app)",
        setup_commands=setup_commands,
        is_dockerized=False,
        install_guide_path=install_guide_path,
        start_command=start_command,
        stop_command=stop_command,
        health_url=health_url,
        process_manager=process_manager,
    )


async def analyze_repo_host(project_dir: str, on_progress=None) -> ProjectConfig | None:
    """Host-mode analyzer. Reads the already-on-disk project directory and
    extracts install guide + start/stop commands + health URL. Falls back to
    direct file analysis on agent failure."""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock

    options = ClaudeAgentOptions(
        cwd=project_dir,
        system_prompt=(
            "You are a senior DevOps engineer analyzing a host-mode project. "
            "Only read files; never modify anything. Follow the install guide to "
            "extract exact shell commands — do not paraphrase."
        ),
        model="claude-sonnet-4-6",
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=8,
    )

    log.info("onboarding.host_started", project_dir=project_dir)
    full_output = ""
    try:
        async for message in query(prompt=HOST_ONBOARDING_PROMPT, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text
                    elif isinstance(block, ToolUseBlock) and on_progress:
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
        log.error("onboarding.host_agent_failed", error=str(e)[:300])
        if on_progress:
            await on_progress("Falling back to direct file analysis...")
        return await _fallback_analyze_host(project_dir)

    config = parse_config(full_output)
    if config is None:
        return await _fallback_analyze_host(project_dir)
    # Host mode: always is_dockerized=False, and make sure install_guide_path set
    config.is_dockerized = False
    if not config.install_guide_path:
        for candidate in ["README.md", "INSTALL.md", "SETUP.md", "CLAUDE.md"]:
            import os as _os
            if _os.path.isfile(_os.path.join(project_dir, candidate)):
                config.install_guide_path = candidate
                break
    log.info("onboarding.host_success", name=config.name, tech=config.tech_stack,
             start=config.start_command, port=config.app_port)
    return config
