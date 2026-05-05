"""Resolve the Docker binary path across all host environments.

The worker runs inside a container but spawns `docker` commands against the
HOST Docker socket (Docker-in-Docker pattern). The worker process PATH at
startup is often minimal and does NOT include the Docker binary directory —
especially on:

  - macOS Docker Desktop  → /usr/local/bin/docker  OR  ~/.docker/bin/docker
  - Ubuntu / Debian       → /usr/bin/docker  OR  /usr/local/bin/docker
  - Homebrew Mac          → /opt/homebrew/bin/docker
  - Snap Linux            → /snap/bin/docker

Every subprocess.run(["docker", ...]) or asyncio.create_subprocess_exec("docker", ...)
call must use get_docker_env() as the env kwarg so the binary is always found.
"""
from __future__ import annotations

import os
import shutil

# Ordered list of known Docker binary locations to probe when PATH lookup fails
_DOCKER_SEARCH_PATHS: list[str] = [
    "/usr/local/bin/docker",
    "/usr/bin/docker",
    "/opt/homebrew/bin/docker",
    os.path.expanduser("~/.docker/bin/docker"),
    "/opt/docker/bin/docker",
    "/snap/bin/docker",
    "/usr/local/share/docker/bin/docker",
]

# Cached result — resolved once per process lifetime
_resolved_docker_bin: str | None = None


def get_docker_bin() -> str:
    """Return the absolute path to the docker binary.

    Resolution order:
      1. DOCKER_BIN env var — explicit override, highest priority
      2. shutil.which("docker")  — honours the current PATH
      3. Probe _DOCKER_SEARCH_PATHS in order

    Raises FileNotFoundError if docker is nowhere to be found.
    Caches the result so the filesystem probe runs only once.
    """
    global _resolved_docker_bin

    if _resolved_docker_bin and os.path.isfile(_resolved_docker_bin):
        return _resolved_docker_bin

    # 1. Explicit env override — set in docker-compose.override.yml for reliability
    if env_bin := os.environ.get("DOCKER_BIN"):
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            _resolved_docker_bin = env_bin
            return _resolved_docker_bin

    # 2. Honour current PATH (works when entrypoint or Dockerfile set it up)
    found = shutil.which("docker")
    if found:
        _resolved_docker_bin = found
        return _resolved_docker_bin

    # 3. Fall back to known locations across Mac + Linux distros
    for path in _DOCKER_SEARCH_PATHS:
        expanded = os.path.expanduser(path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            _resolved_docker_bin = expanded
            return _resolved_docker_bin

    raise FileNotFoundError(
        "Docker binary not found in PATH or any known location.\n"
        f"Searched: PATH={os.environ.get('PATH', '(empty)')!r}\n"
        f"Also tried: {_DOCKER_SEARCH_PATHS}\n"
        "Fix: set DOCKER_BIN=/path/to/docker in the worker environment."
    )


def get_docker_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment dict with Docker binary directories prepended to PATH.

    Pass this as the `env` kwarg to every subprocess/asyncio call that runs
    docker commands so they work regardless of how the worker process started.

    Args:
        base: Starting environment dict (defaults to os.environ copy).

    Returns:
        New dict with augmented PATH — caller's base is not mutated.
    """
    env = dict(base) if base is not None else dict(os.environ)

    # Directories to prepend (highest-priority first)
    prepend_dirs: list[str] = [
        "/usr/local/bin",
        "/usr/bin",
        "/opt/homebrew/bin",
        os.path.expanduser("~/.docker/bin"),
    ]

    # If we can resolve the binary, add its parent dir at the very front
    try:
        docker_bin = get_docker_bin()
        bin_dir = os.path.dirname(docker_bin)
        if bin_dir not in prepend_dirs:
            prepend_dirs.insert(0, bin_dir)
    except FileNotFoundError:
        pass  # Will surface naturally when docker is called

    current_path = env.get("PATH", "")
    existing_dirs = [d for d in current_path.split(":") if d]

    # Deduplicate while preserving order (prepend_dirs win)
    seen: set[str] = set()
    final: list[str] = []
    for d in prepend_dirs + existing_dirs:
        if d and d not in seen:
            seen.add(d)
            final.append(d)

    env["PATH"] = ":".join(final)
    return env
