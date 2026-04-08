"""Smart workspace management with git worktree + dependency caching."""
import asyncio
import hashlib
import os
import shutil
from dataclasses import dataclass

import redis.asyncio as aioredis

from openclow.models import Project
from openclow.settings import settings
from openclow.utils.logging import get_logger
from openclow.worker.tasks import git_ops

log = get_logger()


@dataclass
class Workspace:
    path: str
    from_cache: bool
    deps_changed: bool


class WorkspaceService:
    def __init__(self):
        self.base_path = settings.workspace_base_path
        self.cache_path = os.path.join(self.base_path, "_cache")

    def _project_cache_path(self, project_name: str) -> str:
        return os.path.join(self.cache_path, project_name)

    def _task_work_path(self, task_id: str) -> str:
        return os.path.join(self.base_path, f"task-{str(task_id)[:8]}")

    async def _get_lock(self, project_name: str, ttl: int = 900) -> bool:
        """Acquire a per-project Redis lock."""
        r = aioredis.from_url(settings.redis_url)
        try:
            lock = r.lock(f"openclow:workspace:{project_name}", timeout=ttl)
            acquired = await lock.acquire(blocking=True, blocking_timeout=60)
            if acquired:
                self._lock = lock
                self._lock_redis = r
            else:
                await r.aclose()
            return acquired
        except Exception:
            await r.aclose()
            raise

    async def _release_lock(self, project_name: str):
        """Release the per-project Redis lock."""
        try:
            if hasattr(self, '_lock') and self._lock:
                await self._lock.release()
                self._lock = None
        except Exception as e:
            log.warning("workspace.lock_release_failed", error=str(e))
        finally:
            if hasattr(self, '_lock_redis') and self._lock_redis:
                await self._lock_redis.aclose()
                self._lock_redis = None

    def _hash_file(self, path: str) -> str | None:
        """Hash a lock file for dependency change detection."""
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    def _dep_hashes(self, repo_path: str) -> dict:
        """Get hashes of dependency lock files."""
        return {
            "composer": self._hash_file(os.path.join(repo_path, "composer.lock")),
            "npm": self._hash_file(os.path.join(repo_path, "package-lock.json")),
        }

    async def _install_deps(self, workspace: str):
        """Install composer and npm dependencies."""
        if os.path.exists(os.path.join(workspace, "composer.json")):
            await git_ops.run_cmd("composer install --no-interaction --no-progress", cwd=workspace)
        if os.path.exists(os.path.join(workspace, "package.json")):
            await git_ops.run_cmd("npm install", cwd=workspace)

    async def prepare(self, project: Project, task_id: str) -> Workspace:
        """Prepare a workspace for a task. Uses cache + git worktree for speed."""
        cache = self._project_cache_path(project.name)
        await self._get_lock(project.name)
        try:
            work = self._task_work_path(task_id)
            deps_changed = False

            os.makedirs(self.cache_path, exist_ok=True)

            if os.path.exists(cache):
                log.info("workspace.cache_hit", project=project.name)

                # Update cache
                old_hashes = self._dep_hashes(cache)
                await git_ops.fetch_and_reset(cache, project.default_branch)
                new_hashes = self._dep_hashes(cache)

                deps_changed = old_hashes != new_hashes

                # Create worktree (instant — hardlinks .git objects)
                await git_ops.worktree_add(cache, work)

                # Handle dependencies
                if deps_changed or project.force_fresh_install:
                    log.info("workspace.deps_changed", project=project.name)
                    await self._install_deps(work)
                else:
                    # Symlink deps from cache (fast)
                    for dep_dir in ["vendor", "node_modules"]:
                        cache_dep = os.path.join(cache, dep_dir)
                        work_dep = os.path.join(work, dep_dir)
                        if os.path.exists(cache_dep) and not os.path.exists(work_dep):
                            os.symlink(cache_dep, work_dep)

                from_cache = True
            else:
                log.info("workspace.first_clone", project=project.name, repo=project.github_repo)

                # First time: full clone (uses configured git provider)
                from openclow.providers import factory
                git = await factory.get_git()
                await git.clone_repo(project.github_repo, cache)
                await self._install_deps(cache)

                # Create worktree from cache
                await git_ops.worktree_add(cache, work)

                # Symlink deps
                for dep_dir in ["vendor", "node_modules"]:
                    cache_dep = os.path.join(cache, dep_dir)
                    work_dep = os.path.join(work, dep_dir)
                    if os.path.exists(cache_dep) and not os.path.exists(work_dep):
                        os.symlink(cache_dep, work_dep)

                from_cache = False

            # Run project-specific setup commands
            if project.setup_commands:
                for cmd in project.setup_commands.strip().split("\n"):
                    cmd = cmd.strip()
                    if cmd:
                        await git_ops.run_cmd(cmd, cwd=work, ignore_errors=True)

            # Start the project's Docker stack if it's dockerized
            if project.is_dockerized:
                compose_file = project.docker_compose_file or "docker-compose.yml"
                compose_path = os.path.join(work, compose_file)
                if os.path.exists(compose_path):
                    project_name = f"openclow-{project.name}-{task_id[:8]}"
                    log.info("workspace.docker_up", project=project.name, compose=compose_file)
                    await git_ops.run_exec(
                        "docker", "compose", "-f", compose_file, "-p", project_name, "up", "-d",
                        cwd=work, ignore_errors=True,
                    )
                    # Wait for containers to be ready
                    await asyncio.sleep(5)

            log.info("workspace.ready", path=work, from_cache=from_cache, deps_changed=deps_changed)
            return Workspace(path=work, from_cache=from_cache, deps_changed=deps_changed)
        finally:
            await self._release_lock(project.name)

    def get_path(self, task_id: str) -> str:
        """Get workspace path for a task."""
        return self._task_work_path(task_id)

    async def cleanup(self, task_id: str, project_name: str | None = None):
        """Stop project Docker stack and remove workspace (keep cache)."""
        work = self._task_work_path(task_id)
        if not os.path.exists(work):
            return

        await self._get_lock(project_name or "unknown")
        try:
            # Stop project Docker containers if running
            docker_project = f"openclow-{project_name or 'unknown'}-{task_id[:8]}"
            await git_ops.run_exec(
                "docker", "compose", "-p", docker_project, "down", "-v", "--remove-orphans",
                cwd=work, ignore_errors=True,
            )

            # Try to remove via git worktree first
            for cache_dir in os.listdir(self.cache_path):
                cache_path = os.path.join(self.cache_path, cache_dir)
                if os.path.isdir(cache_path):
                    await git_ops.worktree_remove(cache_path, work)

            # Fallback: force remove
            if os.path.exists(work):
                shutil.rmtree(work, ignore_errors=True)

            log.info("workspace.cleaned", task_id=task_id)
        finally:
            await self._release_lock(project_name or "unknown")

    async def cleanup_stale(self, max_age_hours: int = 24):
        """Remove workspaces older than max_age_hours."""
        import time

        now = time.time()
        for entry in os.listdir(self.base_path):
            if not entry.startswith("task-"):
                continue
            path = os.path.join(self.base_path, entry)
            age_hours = (now - os.path.getmtime(path)) / 3600
            if age_hours > max_age_hours:
                shutil.rmtree(path, ignore_errors=True)
                log.info("workspace.stale_cleaned", path=path, age_hours=round(age_hours, 1))
