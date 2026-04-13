"""Project health service — real-time Docker/HTTP/DB checks."""
import asyncio
import json
import re
from dataclasses import dataclass, field

from openclow.utils.logging import get_logger

log = get_logger()


@dataclass
class HealthCheck:
    name: str
    status: str  # pass, fail, warn, skip
    detail: str


@dataclass
class ContainerInfo:
    name: str
    state: str  # running, exited, restarting
    health: str  # healthy, unhealthy, starting, none
    ports: str
    image: str = ""  # e.g. "postgres:16", "mysql:8.0", "redis:alpine"


@dataclass
class HealthReport:
    project_name: str
    checks: list[HealthCheck] = field(default_factory=list)
    containers: list[ContainerInfo] = field(default_factory=list)
    tunnel_url: str | None = None
    is_running: bool = False


async def _run(cmd: str, timeout: int = 10) -> tuple[int, str]:
    """Run a shell command with timeout. Returns (returncode, stdout)."""
    from openclow.services.audit_service import log_action

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
        output = stdout.decode().strip()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        rc, output = -1, "timeout"
    except Exception as e:
        rc, output = -1, str(e)

    await log_action(
        actor="health_service", action="bash", command=cmd,
        exit_code=rc, output_summary=output[:500],
    )
    return rc, output


async def _run_exec(*args: str, timeout: int = 10) -> tuple[int, str]:
    """Run a command safely with argument list. Returns (returncode, stdout)."""
    cmd_str = " ".join(args)

    # Route Docker commands through the guard
    if args and args[0] == "docker":
        from openclow.services.docker_guard import run_docker
        return await run_docker(*args, actor="health_service", timeout=timeout)

    from openclow.services.audit_service import log_action

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
        output = stdout.decode().strip()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        rc, output = -1, "timeout"
    except Exception as e:
        rc, output = -1, str(e)

    await log_action(
        actor="health_service", action="bash", command=cmd_str,
        exit_code=rc, output_summary=output[:500],
    )
    return rc, output


async def find_project_containers(project_name: str) -> list[ContainerInfo]:
    """Find running Docker containers for a project using compose label (not name substring)."""
    rc, output = await _run_exec(
        "docker", "ps",
        "--filter", f"label=com.docker.compose.project=openclow-{project_name}",
        "--format", "json",
    )
    if rc != 0 or not output:
        return []

    containers = []
    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            ports = data.get("Ports", "")
            containers.append(ContainerInfo(
                name=data.get("Names", ""),
                state=data.get("State", "unknown"),
                health=data.get("Status", ""),
                ports=ports,
                image=data.get("Image", ""),
            ))
        except (json.JSONDecodeError, KeyError):
            continue
    return containers


async def check_http(port: int, container_ip: str | None = None) -> HealthCheck:
    """Check HTTP health — uses container IP if available, falls back to localhost."""
    if container_ip:
        target = f"http://{container_ip}:{port}"
    else:
        target = f"http://localhost:{port}"

    rc, output = await _run(
        f'curl -sf -o /dev/null -w "%{{http_code}}" {target}/ --max-time 5',
        timeout=8,
    )
    label = f":{port}" if not container_ip else f"{container_ip}:{port}"
    if rc == 0 and output.startswith("2"):
        return HealthCheck("HTTP", "pass", f"HTTP {output} on :{port}")
    if rc == 0 and output.startswith("3"):
        return HealthCheck("HTTP", "pass", f"HTTP {output} (redirect) on :{port}")
    if output and output.isdigit():
        return HealthCheck("HTTP", "fail", f"HTTP {output} on :{port}")
    return HealthCheck("HTTP", "fail", f"No response on :{port}")


async def check_database(containers: list[ContainerInfo]) -> list[HealthCheck]:
    """Check database connectivity by detecting DB containers via Docker image name."""
    checks = []
    for c in containers:
        # Use the Docker image name (e.g. "postgres:16", "mysql:8.0") — more reliable
        # than matching on container name which is user-defined
        image_lower = c.image.lower()
        # Strip registry prefix if present (e.g. "docker.io/library/postgres:16" → "postgres:16")
        image_base = image_lower.rsplit("/", 1)[-1]

        if image_base.startswith("postgres"):
            rc, _ = await _run_exec("docker", "exec", c.name, "pg_isready", "-q", timeout=5)
            if rc == 0:
                checks.append(HealthCheck("PostgreSQL", "pass", "connected"))
            else:
                checks.append(HealthCheck("PostgreSQL", "fail", "not responding"))

        elif image_base.startswith("mysql") or image_base.startswith("mariadb"):
            rc, _ = await _run_exec("docker", "exec", c.name, "mysqladmin", "ping", "--silent", timeout=5)
            if rc == 0:
                checks.append(HealthCheck("MySQL", "pass", "connected"))
            else:
                checks.append(HealthCheck("MySQL", "fail", "not responding"))

        elif image_base.startswith("redis") or image_base.startswith("valkey"):
            rc, output = await _run_exec("docker", "exec", c.name, "redis-cli", "ping", timeout=5)
            if rc == 0 and "PONG" in output:
                checks.append(HealthCheck("Redis", "pass", "connected"))
            else:
                checks.append(HealthCheck("Redis", "fail", "not responding"))

        elif image_base.startswith("mongo"):
            rc, _ = await _run_exec("docker", "exec", c.name, "mongosh", "--eval", "db.runCommand({ping:1})", "--quiet", timeout=5)
            if rc == 0:
                checks.append(HealthCheck("MongoDB", "pass", "connected"))
            else:
                checks.append(HealthCheck("MongoDB", "fail", "not responding"))

    return checks


def check_config(project) -> HealthCheck:
    """Validate project configuration completeness."""
    missing = []
    if not project.app_container_name:
        missing.append("app_container_name")
    if not project.app_port:
        missing.append("app_port")
    if project.is_dockerized and not project.docker_compose_file:
        missing.append("docker_compose_file")

    if missing:
        return HealthCheck("Config", "warn", f"missing: {', '.join(missing)}")
    return HealthCheck("Config", "pass", "all fields set")


def _extract_published_port(containers: list[ContainerInfo], app_container: str | None, default_port: int | None) -> int | None:
    """Find the published host port for the app container."""
    for c in containers:
        if app_container and app_container in c.name.lower():
            port_match = re.search(r"0\.0\.0\.0:(\d+)->", c.ports)
            if port_match:
                return int(port_match.group(1))
    return default_port


async def _get_container_ip(container_name: str) -> str | None:
    """Get the Docker network IP of a container."""
    rc, output = await _run_exec(
        "docker", "inspect", container_name,
        "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
    )
    ip = output.strip() if rc == 0 else None
    return ip if ip else None


async def run_full_health_check(project, with_tunnel: bool = True) -> HealthReport:
    """Run all health checks for a project. Returns a HealthReport."""
    report = HealthReport(project_name=project.name)

    # 1. Find containers
    containers = await find_project_containers(project.name)
    report.containers = containers
    report.is_running = any(c.state == "running" for c in containers)

    if not report.is_running:
        report.checks.append(HealthCheck("Docker", "fail", "no running containers"))
        report.checks.append(check_config(project))
        return report

    # Count running
    running = sum(1 for c in containers if c.state == "running")
    total = len(containers)
    report.checks.append(HealthCheck("Docker", "pass", f"{running}/{total} containers running"))

    # 2. HTTP check — use container IP directly (worker can't reach host ports)
    app_container_full = None
    container_ip = None
    internal_port = project.app_port or 80

    for c in containers:
        if project.app_container_name and project.app_container_name in c.name.lower():
            app_container_full = c.name
            break

    if app_container_full:
        container_ip = await _get_container_ip(app_container_full)

    if container_ip:
        http_check = await check_http(internal_port, container_ip=container_ip)
        report.checks.append(http_check)
    else:
        # Fallback: try published port from host
        port = _extract_published_port(containers, project.app_container_name, project.app_port)
        if port:
            http_check = await check_http(port)
            report.checks.append(http_check)
        else:
            report.checks.append(HealthCheck("HTTP", "skip", "no app container or port found"))

    # 3. Database checks
    db_checks = await check_database(containers)
    report.checks.extend(db_checks)

    # 4. Config check
    report.checks.append(check_config(project))

    # 5. Tunnel (read from DB — worker manages the tunnel lifecycle)
    if with_tunnel:
        from openclow.services.tunnel_service import get_tunnel_url
        report.tunnel_url = await get_tunnel_url(project.name)

    return report
