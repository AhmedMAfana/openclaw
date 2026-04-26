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

    # Lock file → install command mapping (order matters for detection priority)
    _LOCK_FILE_COMMANDS: list[tuple[str, str]] = [
        ("composer.lock", "composer install --no-interaction --no-progress"),
        ("package-lock.json", "npm install"),
        ("yarn.lock", "yarn install"),
        ("pnpm-lock.yaml", "pnpm install"),
        ("bun.lockb", "bun install"),
        ("Pipfile.lock", "pipenv install"),
        ("poetry.lock", "poetry install"),
        ("requirements.txt", "pip install -r requirements.txt"),
    ]

    def _dep_hashes(self, repo_path: str) -> dict:
        """Get hashes of dependency lock files."""
        return {
            lock_file: self._hash_file(os.path.join(repo_path, lock_file))
            for lock_file, _ in self._LOCK_FILE_COMMANDS
        }

    async def _install_deps(self, workspace: str):
        """Install dependencies by detecting package managers from lock files.

        Lock files are more reliable than manifest files (e.g. package.json
        could mean npm, yarn, pnpm, or bun — but only one lock file exists).
        """
        # Copy auth.json for Composer (GitLab deploy tokens, etc.)
        # Mounted at /app/auth.json via docker-compose volume
        if os.path.exists(os.path.join(workspace, "composer.lock")):
            src_auth = "/app/auth.json"
            dest_auth = os.path.join(workspace, "auth.json")
            if os.path.exists(src_auth) and not os.path.exists(dest_auth):
                import shutil
                shutil.copy2(src_auth, dest_auth)
                log.info("workspace.auth_json_copied", workspace=workspace)

        import shlex
        for lock_file, install_cmd in self._LOCK_FILE_COMMANDS:
            if os.path.exists(os.path.join(workspace, lock_file)):
                await git_ops.run_exec(*shlex.split(install_cmd), cwd=workspace)

    async def prepare_host(self, project: Project) -> Workspace:
        """Host-mode prep: project_dir IS the workspace. No cache, no clone.

        Verifies the dir exists inside the worker container (so /srv/projects
        must be mounted), then best-effort git pull to pick up any external
        commits. Skipping the docker workspace cache machinery entirely is the
        whole point — these projects already live on the host filesystem.
        """
        if not project.project_dir:
            raise RuntimeError(
                f"Project {project.name!r} mode=host but project_dir is empty — "
                f"set it via Settings → Projects."
            )
        if not os.path.isdir(project.project_dir):
            raise RuntimeError(
                f"Project dir {project.project_dir!r} not found inside this container. "
                f"Mount it via docker-compose.prod.yml (worker.volumes + api.volumes)."
            )
        if os.path.isdir(os.path.join(project.project_dir, ".git")):
            try:
                # run_exec returns stdout as a str; raises on non-zero unless
                # ignore_errors=True. We want best-effort: log on failure, never
                # block the task — staging often has dirty working trees.
                await git_ops.run_exec(
                    "git", "pull", "--ff-only", "--quiet",
                    cwd=project.project_dir, ignore_errors=True,
                )
            except Exception as e:
                log.warning("workspace.host_pull_failed",
                            project=project.name, error=str(e)[:200])
        log.info("workspace.host_ready", path=project.project_dir, project=project.name)
        return Workspace(path=project.project_dir, from_cache=True, deps_changed=False)

    async def prepare(self, project: Project, task_id: str) -> Workspace:
        """Prepare a workspace for a task. Uses cache + git worktree for speed."""
        cache = self._project_cache_path(project.name)
        await self._get_lock(project.name)
        try:
            work = self._task_work_path(task_id)
            deps_changed = False

            os.makedirs(self.cache_path, exist_ok=True)

            cache_is_git_repo = os.path.isdir(os.path.join(cache, ".git"))
            if cache_is_git_repo:
                log.info("workspace.cache_hit", project=project.name)

                # Update cache
                old_hashes = self._dep_hashes(cache)
                await git_ops.fetch_and_reset(cache, project.default_branch)
                new_hashes = self._dep_hashes(cache)

                deps_changed = old_hashes != new_hashes

                # Create worktree (instant — hardlinks .git objects)
                await git_ops.worktree_add(cache, work)

                # Handle dependencies — SKIP for Dockerized projects
                # Docker containers have their own deps (installed during build)
                # Running composer/npm on the host fails and wastes time
                if project.is_dockerized:
                    log.info("workspace.skip_deps_dockerized", project=project.name)
                elif deps_changed or project.force_fresh_install:
                    log.info("workspace.deps_changed", project=project.name)
                    await self._install_deps(work)
                else:
                    # Symlink deps from cache (fast)
                    for dep_dir in ["vendor", "node_modules", ".venv", "venv", "__pypackages__"]:
                        cache_dep = os.path.join(cache, dep_dir)
                        work_dep = os.path.join(work, dep_dir)
                        if os.path.exists(cache_dep) and not os.path.exists(work_dep):
                            os.symlink(cache_dep, work_dep)

                from_cache = True
            else:
                log.info("workspace.first_clone", project=project.name, repo=project.github_repo)

                # An empty/half-cloned dir would defeat clone_repo (it
                # refuses non-empty targets). Wipe it so the clone is
                # idempotent on retry after a previous failed provision.
                if os.path.exists(cache):
                    shutil.rmtree(cache, ignore_errors=True)

                # First time: full clone (uses configured git provider)
                from openclow.providers import factory
                git = await factory.get_git()
                await git.clone_repo(project.github_repo, cache)
                # Skip deps for Dockerized projects — containers have their own
                if not project.is_dockerized:
                    await self._install_deps(cache)

                # Create worktree from cache
                await git_ops.worktree_add(cache, work)

                # Symlink deps
                for dep_dir in ["vendor", "node_modules", ".venv", "venv", "__pypackages__"]:
                    cache_dep = os.path.join(cache, dep_dir)
                    work_dep = os.path.join(work, dep_dir)
                    if os.path.exists(cache_dep) and not os.path.exists(work_dep):
                        os.symlink(cache_dep, work_dep)

                from_cache = False

            # Run project-specific setup commands
            if project.setup_commands:
                import shlex as _shlex
                for cmd in project.setup_commands.strip().split("\n"):
                    cmd = cmd.strip()
                    if cmd:
                        await git_ops.run_exec(*_shlex.split(cmd), cwd=work, ignore_errors=True)

            # Docker: DON'T start containers per-task.
            # Containers are already running from bootstrap/Open App.
            # Starting them here wastes time and can cause port conflicts.
            # The agent can start them via MCP tools if needed.

            log.info("workspace.ready", path=work, from_cache=from_cache, deps_changed=deps_changed)
            return Workspace(path=work, from_cache=from_cache, deps_changed=deps_changed)
        finally:
            await self._release_lock(project.name)

    def get_path(self, task_id: str) -> str:
        """Get workspace path for a task."""
        return self._task_work_path(task_id)

    async def reattach_session_branch(
        self,
        project,
        session_branch: str,
        instance_workspace_path: str,
    ) -> None:
        """T068: attach a chat's session branch as a worktree under ``<path>``.

        Called by ``InstanceService.get_or_resume`` (T069) when a chat
        reconnects after its prior instance was destroyed. The same
        cache+worktree pattern used by ``prepare()`` — just re-scoped
        to the per-instance path instead of the per-task path
        (constitution: "re-use the cache, re-scope the worktree").

        Behaviour:
          1. Ensure the per-project cache exists (first-time chats
             clone the repo here; returning chats hit cache).
          2. ``git fetch`` so the branch ref is current.
          3. If the cache has ``<session_branch>`` locally, worktree-add
             from that branch into the instance workspace. If not,
             create the branch off ``origin/<session_branch>`` if the
             remote has it, otherwise branch from the project default.
          4. If an old worktree is already mounted at ``instance_workspace_path``,
             prune it first so the attach is idempotent on retry.

        No-op shortcut: if ``instance_workspace_path`` already contains
        a ``.git`` whose HEAD is on ``session_branch``, return directly —
        a previous provision already attached it.
        """
        cache = self._project_cache_path(project.name)
        await self._get_lock(project.name)
        try:
            os.makedirs(self.cache_path, exist_ok=True)

            # 1. Ensure cache is a real git repo. An empty placeholder
            # directory (left behind by a previously-failed provision)
            # would otherwise defeat `os.path.exists` and skip the clone,
            # making the subsequent `git fetch` blow up with
            # "fatal: not a git repository".
            if not os.path.isdir(os.path.join(cache, ".git")):
                if os.path.exists(cache):
                    shutil.rmtree(cache, ignore_errors=True)
                log.info(
                    "workspace.instance_first_clone",
                    project=project.name,
                    repo=project.github_repo,
                )
                from openclow.providers import factory
                git = await factory.get_git()
                await git.clone_repo(project.github_repo, cache)

            # 2. Refresh so we see new remote refs.
            await git_ops.fetch_and_reset(cache, project.default_branch)

            # 3. Idempotent short-circuit.
            dot_git = os.path.join(instance_workspace_path, ".git")
            if os.path.exists(dot_git):
                try:
                    head_name = (await git_ops.run_exec(
                        "git", "rev-parse", "--abbrev-ref", "HEAD",
                        cwd=instance_workspace_path,
                    )).strip()
                except Exception:
                    head_name = ""
                if head_name == session_branch:
                    log.info(
                        "workspace.session_branch_already_attached",
                        branch=session_branch,
                        path=instance_workspace_path,
                    )
                    return
                # Stale worktree — prune before re-attaching.
                try:
                    await git_ops.worktree_remove(cache, instance_workspace_path)
                except Exception:
                    # If git refuses (detached, missing), remove the dir
                    # and let git forget its own state via `worktree prune`.
                    shutil.rmtree(instance_workspace_path, ignore_errors=True)
                    await git_ops.run_exec(
                        "git", "worktree", "prune", cwd=cache,
                        ignore_errors=True,
                    )

            # 4. Decide where <session_branch> comes from.
            # Prefer a local branch in cache → remote origin branch →
            # fall back to branching from project default.
            branch_source = None
            try:
                await git_ops.run_exec(
                    "git", "show-ref", "--verify", "--quiet",
                    f"refs/heads/{session_branch}",
                    cwd=cache,
                )
                branch_source = session_branch
            except Exception:
                try:
                    await git_ops.run_exec(
                        "git", "show-ref", "--verify", "--quiet",
                        f"refs/remotes/origin/{session_branch}",
                        cwd=cache,
                    )
                    branch_source = f"origin/{session_branch}"
                except Exception:
                    branch_source = project.default_branch

            os.makedirs(os.path.dirname(instance_workspace_path) or "/", exist_ok=True)
            if branch_source == session_branch:
                # Local branch already exists — worktree_add checks it out.
                await git_ops.worktree_add(
                    cache, instance_workspace_path, session_branch
                )
            else:
                # Create a new worktree with a fresh branch tracking
                # `branch_source`. -b creates the local branch atomically
                # with the worktree; safer than a two-step.
                await git_ops.run_exec(
                    "git", "worktree", "add",
                    "-b", session_branch,
                    instance_workspace_path,
                    branch_source,
                    cwd=cache,
                )

            log.info(
                "workspace.session_branch_attached",
                project=project.name,
                branch=session_branch,
                source=branch_source,
                path=instance_workspace_path,
            )
        finally:
            await self._release_lock(project.name)

    async def cleanup(self, task_id: str, project_name: str | None = None):
        """Stop project Docker stack and remove workspace (keep cache)."""
        work = self._task_work_path(task_id)
        if not os.path.exists(work):
            return

        await self._get_lock(project_name or "unknown")
        try:
            # Stop project Docker containers if running
            docker_project = f"openclow-{project_name or 'unknown'}"
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
                try:
                    shutil.rmtree(work)
                except Exception as e:
                    log.warning("workspace.cleanup_failed", path=work, error=str(e))

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
                try:
                    shutil.rmtree(path)
                except Exception as e:
                    log.warning("workspace.stale_cleanup_failed", path=path, error=str(e))
                log.info("workspace.stale_cleaned", path=path, age_hours=round(age_hours, 1))
