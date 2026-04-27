"""Seed host-mode sample projects into TAGH Dev's `projects` table so the dev
can click into them from the web chat and exercise the full flow.

Usage:
    python dev-sandbox/seed_sim_projects.py

Idempotent — re-running updates the existing rows instead of inserting dupes.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def main() -> int:
    from sqlalchemy import select
    from openclow.models import Project, async_session
    from openclow.services.config_service import set_host_setting

    # Point the "host.projects_base" at the sandbox's sample-apps dir so host_guard
    # considers the project_dir valid. In production this is set in the admin UI.
    sandbox_projects = str((ROOT / "dev-sandbox" / "sample-apps").resolve())
    await set_host_setting("projects_base", sandbox_projects)
    await set_host_setting("mode_default", "host")
    await set_host_setting("auto_clone", True)
    print(f"[seed] host.projects_base = {sandbox_projects}")

    samples = [
        {
            "name": "sim-fastapi",
            "github_repo": "openclow/sim-fastapi",
            "default_branch": "main",
            "description": "[SANDBOX] FastAPI starter — port 8101",
            "tech_stack": "Python/FastAPI",
            "mode": "host",
            "project_dir": os.path.join(sandbox_projects, "sim-fastapi"),
            "install_guide_path": "README.md",
            "setup_commands": "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
            "start_command": ".venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8101",
            "app_port": 8101,
            # host.docker.internal resolves to the macOS/Linux host from inside
            # the worker container (extra_hosts: host-gateway is set in override).
            "health_url": "http://host.docker.internal:8101/",
            "process_manager": "manual",
            "is_dockerized": False,
            "status": "active",
        },
    ]

    async with async_session() as session:
        for spec in samples:
            result = await session.execute(
                select(Project).where(Project.name == spec["name"])
            )
            existing = result.scalar_one_or_none()
            if existing:
                for k, v in spec.items():
                    setattr(existing, k, v)
                print(f"[seed] updated {spec['name']}")
            else:
                session.add(Project(**spec))
                print(f"[seed] inserted {spec['name']}")
        await session.commit()

    print("[seed] done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
