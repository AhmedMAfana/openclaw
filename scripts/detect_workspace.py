#!/usr/bin/env python3
"""Detect the host filesystem path that corresponds to /workspaces inside the container.

Run this from inside a container that has /workspaces bind-mounted from the host.
It inspects the container's own mounts via the Docker socket and prints the host path.
On Docker Desktop Mac it strips the /host_mnt/ prefix automatically.

Usage:
    WS_PATH=$(python3 scripts/detect_workspace.py 2>/dev/null || echo /workspaces)
"""
import json
import socket
import subprocess
import sys


def detect() -> str | None:
    try:
        container_id = socket.gethostname()
        result = subprocess.run(
            ["docker", "inspect", container_id, "--format", "{{json .Mounts}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        mounts = json.loads(result.stdout)
        for mount in mounts:
            if mount.get("Destination") == "/workspaces":
                source = mount.get("Source", "")
                # Docker Desktop Mac adds /host_mnt/ prefix — strip it
                if source.startswith("/host_mnt/"):
                    source = source[len("/host_mnt"):]  # keeps leading /
                return source
    except Exception:
        pass
    return None


if __name__ == "__main__":
    path = detect()
    if path:
        print(path)
        sys.exit(0)
    sys.exit(1)
