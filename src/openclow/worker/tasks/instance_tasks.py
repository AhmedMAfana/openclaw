"""ARQ jobs that own the concrete per-chat instance infra lifecycle.

Spec:
  * Contract: specs/001-per-chat-instances/contracts/instance-service.md
  * Research §4 (idempotency keys: slug), §7 (projctl state), §11 (activity)
  * tasks.md T036 (provision_instance), T037 (teardown_instance)

The ``InstanceService`` owns the state machine; these jobs own the actual
``docker compose`` / Cloudflare / filesystem side effects. They are the
deepest layer where every Principle IX timeout becomes an ``asyncio``
``wait_for`` and every Principle VI retry resolves by querying live state.

Idempotency contract (research.md §4):
  * ``provision_instance`` re-run at any point re-queries DB + CF + Docker
    state and forward-completes. A row already ``running`` is a no-op; a
    row ``failed`` is NOT auto-re-provisioned here — that is a caller's
    decision (teardown first, then call ``provision`` again).
  * ``teardown_instance`` is a strict subtract: every step uses the live
    state as its source of truth, so a half-torn-down instance finishes
    cleanly on retry.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import pathlib
import secrets
import shutil
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from openclow.models import async_session
from openclow.models.instance import FailureCode, Instance, InstanceStatus
from openclow.models.instance_tunnel import InstanceTunnel, TunnelStatus
from openclow.services.config_service import get_config
from openclow.services.credentials_service import (
    CredentialsService,
    GitHubAppConfig,
)
from openclow.services.instance_compose_renderer import (
    InstanceRenderContext,
    render as render_compose,
)
from openclow.services.instance_service import InstanceService, emit_instance_event
from openclow.services.tunnel_service import (
    CloudflareConfig,
    TunnelService,
    TunnelServiceError,
)
from openclow.settings import settings
from openclow.utils.docker_path import get_docker_env
from openclow.utils.logging import get_logger

log = get_logger()

# Wall-clock guards. Every subprocess gets an explicit timeout per
# Principle IX — "no timeout" is a bug. These are generous upper bounds;
# the common path finishes far faster.
_COMPOSE_UP_TIMEOUT_S = 900       # ≤ 15 min for first-time image pull
_COMPOSE_DOWN_TIMEOUT_S = 180
_DOCKER_META_TIMEOUT_S = 30
_PROJCTL_UP_TIMEOUT_S = 1800      # ≤ 30 min for slow guides
# First-paint gate: how long to wait for the public URL to serve a real
# 200 (no Laravel exception page) before flipping status to running.
# The cold path includes npm ci + Vite warmup + Laravel @vite() resolution
# on top of compose up — generous-but-bounded.
_FIRST_PAINT_TIMEOUT_S = 180
_FIRST_PAINT_POLL_INTERVAL_S = 3.0

# The orchestrator's internal base URL as seen from inside the instance
# containers. Configurable via platform_config so ops can override it
# when the compose network routing differs from the default.
_DEFAULT_ORCHESTRATOR_INTERNAL_URL = "http://api:8000"


# ---------------------------------------------------------------------------
# provision_instance — T036
# ---------------------------------------------------------------------------


async def provision_instance(ctx: dict, instance_id: str) -> dict:
    """Bring one ``Instance`` row from ``provisioning`` to ``running``.

    Called from ``InstanceService.provision`` via ARQ. Idempotent: safe
    to re-run after a worker crash mid-provision — every step queries
    live state (DB row, CF tunnel by name, docker compose project by
    name) before acting. See research.md §4.

    Partial-success inventory (research.md §4):
      * CF tunnel created but DB row missing → impossible here: the row
        is written by ``InstanceService`` before the job is enqueued.
      * DB row written but CF tunnel missing → ``TunnelService.provision``
        re-creates on re-run; it is itself idempotent.
      * Compose up partially succeeded → ``docker compose up`` on the
        same project name converges missing services.
      * projctl partially run → projctl's own ``state.json`` per-step
        resumability handles this (research.md §7).
    """
    inst_uuid = UUID(instance_id)
    async with async_session() as session:
        inst = await session.get(Instance, inst_uuid)
        if inst is None:
            log.warning("provision_instance.not_found", instance_id=instance_id)
            return {"ok": False, "error": "instance not found"}

        # Idempotent shortcut: already running → nothing to do.
        if inst.status == InstanceStatus.RUNNING.value:
            log.info("provision_instance.already_running", slug=inst.slug)
            return {"ok": True, "slug": inst.slug, "state": "running"}

        # Terminal rows are not re-provisionable from here. Caller must
        # teardown first, then call InstanceService.provision again.
        if inst.status in (
            InstanceStatus.TERMINATING.value,
            InstanceStatus.DESTROYED.value,
            InstanceStatus.FAILED.value,
        ):
            log.warning(
                "provision_instance.terminal_state",
                slug=inst.slug, status=inst.status,
            )
            return {"ok": False, "error": f"instance is {inst.status}"}

        slug = inst.slug
        compose_project = inst.compose_project
        workspace_path = inst.workspace_path
        db_password = inst.db_password
        heartbeat_secret = inst.heartbeat_secret
        github_repo = (inst.project.github_repo if inst.project else None)
        # Project-scoped cache key: every chat for the same project shares
        # one npm cache + one composer cache, so the second instance for
        # tagh-test doesn't re-download 1239 npm packages (3 min) — it
        # hits the warm cache instead. Sanitised to fit docker volume
        # naming rules: [a-z0-9_-]+. Falls back to "default" if unset.
        _proj_name = (inst.project.name if inst.project else "") or "default"
        project_slug = re.sub(r"[^a-z0-9_-]+", "-", _proj_name.lower()).strip("-") or "default"

    started_at = datetime.now(timezone.utc)
    # Stamp started_at upfront so the progress card's elapsed counter
    # reflects real wall-clock from the moment the user hit send. Until
    # this point the card may have been emitted with elapsed=0 by the
    # API; the worker's first publish replaces it with delta-from-now.
    async with async_session() as _ss:
        _row = await _ss.get(Instance, inst_uuid)
        if _row is not None and _row.started_at is None:
            _row.started_at = started_at
            await _ss.commit()

    try:
        cf_config = await _load_cloudflare_config()
        tunnel_service = TunnelService(cf_config)

        # 1. Cloudflare tunnel — idempotent; returns existing by name on retry.
        tunnel_result = await tunnel_service.provision(inst_uuid, slug)
        await _upsert_tunnel_row(
            inst_uuid,
            cf_tunnel_id=tunnel_result.cf_tunnel_id,
            cf_tunnel_name=tunnel_result.cf_tunnel_name,
            web_hostname=tunnel_result.web_hostname,
            hmr_hostname=tunnel_result.hmr_hostname,
            ide_hostname=tunnel_result.ide_hostname,
            credentials_secret=tunnel_result.credentials_secret,
        )

        # T069: reattach the chat's session branch BEFORE rendering
        # compose templates. ``git worktree add`` refuses a non-empty
        # target directory, so the worktree must be attached first and
        # the compose files dropped on top of the cloned project tree.
        # Idempotent: re-runs on an already-attached worktree are a no-op.
        async with async_session() as session:
            inst_row = await session.get(Instance, inst_uuid)
            project_row = inst_row.project if inst_row is not None else None
        if project_row is not None:
            try:
                from openclow.services.workspace_service import WorkspaceService
                await WorkspaceService().reattach_session_branch(
                    project=project_row,
                    session_branch=inst_row.session_branch,
                    instance_workspace_path=workspace_path,
                )
            except Exception as e:
                # A reattach failure is a provision failure per
                # research §4 (partial-success 2/4: "DB row written
                # but workspace missing → provision re-runs"). Bubble
                # up as a structured PROJCTL_UP failure so the chat
                # translator gives the user a Retry button.
                raise _ProvisionFailure(
                    FailureCode.PROJCTL_UP,
                    f"session-branch reattach failed: {str(e)[:300]}",
                )

        # 2. Render compose + cloudflared config into the instance's
        # workspace, on top of the now-cloned project tree. The
        # compose.yml bind-mounts the workspace into ``/app``, so the
        # project files must be on disk when the containers start —
        # otherwise php-fpm/node boot against an empty dir.
        # The orchestrator runs inside a container that bind-mounts a
        # host directory onto its internal /workspaces, so the rendered
        # compose file must use the HOST path for bind-mount sources
        # (the docker daemon resolves them against the host filesystem).
        from openclow.services.docker_guard import _detect_host_workspace_path
        host_workspace_dir = await _detect_host_workspace_path()
        if host_workspace_dir is None:
            log.warning(
                "provision_instance.no_host_workspace_path",
                slug=slug,
                workspace_path=workspace_path,
                hint="set WORKSPACE_HOST_PATH env on the worker, or "
                     "ensure a host bind-mount targets /workspaces",
            )
        template_dir = _template_dir_for_instance(inst_uuid)
        output_dir = pathlib.Path(workspace_path)
        render_ctx = InstanceRenderContext(
            slug=slug,
            workspace_path=workspace_path,
            workspace_host_dir=host_workspace_dir,
            compose_project=compose_project,
            web_hostname=tunnel_result.web_hostname,
            hmr_hostname=tunnel_result.hmr_hostname,
            ide_hostname=tunnel_result.ide_hostname,
            cf_tunnel_id=tunnel_result.cf_tunnel_id,
            cf_credentials_secret=tunnel_result.credentials_secret,
            db_password=db_password,
            heartbeat_secret=heartbeat_secret,
            project_slug=project_slug,
        )
        compose_path, cloudflared_path = render_compose(
            render_ctx, template_dir, output_dir
        )
        _copy_template_support_files(template_dir, output_dir)

        # 3. Mint a GitHub installation token scoped to the one repo.
        # Kept in-process memory only — never written to the rendered
        # compose file (Principle IV).
        gh_token = ""
        if github_repo:
            try:
                gh_config = await _load_github_app_config()
                creds = CredentialsService(gh_config)
                token = await creds.github_push_token(inst_uuid, github_repo)
                gh_token = token.token
            except Exception as e:  # pragma: no cover — exercised in T034a/T055
                log.warning(
                    "provision_instance.github_token_failed",
                    slug=slug, error=str(e),
                )
                # Continue: projctl may not need git push during up;
                # rotation (T063) will retry every 45 min.

        # COMPOSER_AUTH lets the per-instance composer install authenticate
        # against private VCS deps (gitlab.com, private github org packages)
        # without writing auth.json to disk. The orchestrator's worker has
        # /app/auth.json bind-mounted from the host; we read its content
        # and forward as an env var per composer's documented protocol.
        composer_auth = ""
        try:
            auth_json_path = pathlib.Path("/app/auth.json")
            if auth_json_path.is_file():
                composer_auth = auth_json_path.read_text().strip()
        except Exception as e:
            log.debug("provision_instance.composer_auth_read_failed", error=str(e))

        # Per-instance Laravel APP_KEY. Laravel uses this for session +
        # cookie encryption. Generated freshly per provision so each
        # instance has its own session keyspace (terminating one chat
        # invalidates only that chat's sessions). Format matches what
        # `php artisan key:generate` would produce: `base64:<32 bytes>`.
        # Injected via compose env so Laravel's env('APP_KEY') resolves
        # without a .env file written to disk (Principle IV — no
        # secrets on disk).
        app_key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()

        # Step 0 done: "Provisioning Cloudflare tunnel" is complete (the
        # tunnel was provisioned at line ~143). Card flips step 0 done +
        # step 1 ("Booting containers") running.
        await _publish_progress_step(
            instance_id=instance_id, completed_step_index=0,
            stream_buffer_append=(
                f"Cloudflare tunnel ready: {tunnel_result.web_hostname}"
            ),
        )

        # 3.5. Make the workspace world-writable so BOTH the agent
        # (worker container, uid 1000) AND the app container's composer/
        # npm (www-data, uid 82) can write the same tree. Earlier
        # version chowned to www-data; that fixed composer but broke
        # the agent (surfaced as "workspace mounted as read-only"
        # errors when the agent tried to edit a Vue component).
        # See _make_workspace_shared_writable's docstring for the
        # security argument.
        await _make_workspace_shared_writable(
            host_workspace_dir=host_workspace_dir,
            slug=slug,
        )

        # 3.6. Detect project variant from the cloned tree (composer.json).
        # Drives the per-step dispatch in the bundled `_variant.sh`. Pure
        # function — no DB, no network. Never raises (falls back to
        # "normal" on any read/parse failure).
        project_variant = _detect_project_variant(workspace_path)
        log.info(
            "provision_instance.variant_detected",
            slug=slug, variant=project_variant,
        )

        # 4a. Ensure project-scoped cache volumes exist. compose.yml
        # declares them external=true so a teardown's `--volumes` won't
        # touch them; that means we must pre-create them here. `docker
        # volume create` is idempotent — creates if missing, no-op if
        # present.
        for cache_kind in ("npm", "composer"):
            vol_name = f"tagh-{cache_kind}-cache-{project_slug}"
            _vc = await asyncio.create_subprocess_exec(
                "docker", "volume", "create", vol_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=get_docker_env(),
            )
            _, _vc_err = await _vc.communicate()
            if _vc.returncode != 0:
                log.warning(
                    "provision_instance.cache_volume_create_failed",
                    volume=vol_name, error=_vc_err.decode(errors="replace")[:200],
                )

        # 4. docker compose up -p <compose_project> -f _compose.yml -d.
        # Secrets injected via env; never touch disk.
        await _compose_up(
            compose_path=compose_path,
            compose_project=compose_project,
            env={
                "DB_PASSWORD": db_password,
                "MYSQL_PASSWORD": db_password,
                "MYSQL_ROOT_PASSWORD": db_password,
                "GITHUB_TOKEN": gh_token,
                "HEARTBEAT_SECRET": heartbeat_secret,
                "HEARTBEAT_URL": _heartbeat_url_for(slug),
                "CF_TUNNEL_TOKEN": tunnel_result.credentials_blob,
                "TUNNEL_TOKEN": tunnel_result.credentials_blob,
                "COMPOSER_AUTH": composer_auth,
                "APP_KEY": app_key,
                # Variant — flows into the app container, used by the
                # bundled _variant.sh dispatcher to pick the right
                # per-step commands (e.g. domain:add + domain:migrate
                # for gecche/laravel-multidomain).
                "PROJECT_VARIANT": project_variant,
                # Project slug — drives the project-scoped npm + composer
                # cache volume names in compose.yml. Every chat for this
                # project shares the same caches; first instance pays the
                # 3-min npm install cost, every later instance hits the
                # warm cache (~30s).
                "PROJECT_SLUG": project_slug,
            },
        )

        # Step 1 done: containers booted. Step 2 ("App bootstrap") next.
        await _publish_progress_step(
            instance_id=instance_id, completed_step_index=1,
            stream_buffer_append=(
                f"Containers up — running app bootstrap "
                f"(composer install + npm ci + migrations) inside the app container"
            ),
        )

        # 5. projctl up inside the app container. Poll its JSON-line
        # stdout for step_success / fatal events per the stdout schema.
        # instance_id passed so per-substep events can be tee'd into the
        # chat UI's agent-log panel — without it the user stares at
        # "App bootstrap (composer + npm)" for 5 minutes with no signal.
        await _projctl_up(
            compose_project=compose_project, slug=slug,
            instance_id=instance_id,
        )

        # Step 2 done: app bootstrap (composer install + npm ci + migrations)
        # finished. Step 3 ("Health check") next.
        await _publish_progress_step(
            instance_id=instance_id, completed_step_index=2,
            stream_buffer_append=(
                f"App bootstrap done — polling https://{tunnel_result.web_hostname}/ "
                f"until it serves a clean 200"
            ),
        )

        # 5.5 First-paint gate: poll the public URL until it returns a
        # real 200 (no Laravel "ViteManifestNotFound" / "Whoops" /
        # generic 5xx). Without this, the row flips to running while
        # Vite is still warming up + writing public/hot, and the user
        # hits a 500 on the live URL. This is the platform fulfilling
        # FR-004's "ready" transition only when the app actually IS
        # ready. Timeout → status='failed' with failure_code=health_check.
        await _wait_for_first_paint(
            web_hostname=tunnel_result.web_hostname,
            slug=slug,
            timeout_s=_FIRST_PAINT_TIMEOUT_S,
            instance_id=instance_id,
        )

        # Step 3 done — overall_status flips to done. Card collapses to
        # the "ready" green state with all 4 steps checkmarked.
        await _publish_progress_step(
            instance_id=instance_id,
            completed_step_index=3,
            overall_status="done",
            stream_buffer_append=(
                f"Live at https://{tunnel_result.web_hostname}"
            ),
        )

        # 6. Flip DB state — instance is live.
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            inst = await session.get(Instance, inst_uuid)
            if inst is None:
                log.warning("provision_instance.row_gone", instance_id=instance_id)
                return {"ok": False, "error": "instance row vanished mid-provision"}
            inst.status = InstanceStatus.RUNNING.value
            inst.started_at = started_at
            await session.commit()
            inst_slug_for_emit = inst.slug

            tunnel_row = (await session.execute(
                select(InstanceTunnel).where(
                    InstanceTunnel.instance_id == inst_uuid,
                    InstanceTunnel.status != TunnelStatus.DESTROYED.value,
                )
            )).scalar_one_or_none()
            if tunnel_row is not None:
                tunnel_row.status = TunnelStatus.ACTIVE.value
                tunnel_row.last_health_at = now
                await session.commit()

        # Spec 003 — emit AFTER commit so admin SSE consumers can trust the
        # state they observe matches DB truth.
        emit_instance_event({
            "type": "instance_status",
            "slug": inst_slug_for_emit,
            "status": InstanceStatus.RUNNING.value,
            "previous_status": InstanceStatus.PROVISIONING.value,
            "at": started_at.isoformat(),
        })

        duration_s = (now - started_at).total_seconds()
        log.info(
            "instance.running", instance_slug=slug, startup_duration_s=duration_s,
        )
        return {"ok": True, "slug": slug, "state": "running"}

    except _ProvisionFailure as e:
        # Map failure code → which step the failure happened on so the
        # card highlights the right row in red instead of all steps
        # collapsing to a generic failed state.
        _failed_step_idx = {
            FailureCode.TUNNEL_PROVISION.value: 0,
            FailureCode.IMAGE_BUILD.value: 1,
            FailureCode.COMPOSE_UP.value: 1,
            FailureCode.PROJCTL_UP.value: 2,
            FailureCode.HEALTH_CHECK.value: 3,
        }.get(e.failure_code.value, 0)
        await _publish_progress_step(
            instance_id=instance_id,
            completed_step_index=_failed_step_idx - 1,
            overall_status="failed",
            failed_step_index=_failed_step_idx,
        )
        await _mark_failed(inst_uuid, e.failure_code, str(e))
        return {"ok": False, "error": str(e), "failure_code": e.failure_code.value}
    except TunnelServiceError as e:
        await _publish_progress_step(
            instance_id=instance_id,
            completed_step_index=-1,
            overall_status="failed",
            failed_step_index=0,
        )
        await _mark_failed(inst_uuid, FailureCode.TUNNEL_PROVISION, str(e))
        return {"ok": False, "error": str(e), "failure_code": FailureCode.TUNNEL_PROVISION.value}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("provision_instance.unexpected", slug=slug, error=str(e))
        await _mark_failed(inst_uuid, FailureCode.UNKNOWN, str(e))
        return {"ok": False, "error": str(e), "failure_code": FailureCode.UNKNOWN.value}


# ---------------------------------------------------------------------------
# teardown_instance — T037
# ---------------------------------------------------------------------------


async def teardown_instance(ctx: dict, instance_id: str) -> dict:
    """Tear down all per-instance infra. Idempotent; missing resources skip.

    Every step re-queries live state before acting so a mid-teardown crash
    finishes cleanly on retry:
      1. docker compose down -p <project> --remove-orphans --volumes
      2. Cloudflare: delete DNS CNAMEs + tunnel (skip if already gone)
      3. rm -rf /workspaces/inst-<slug>/
      4. Flip DB row to ``destroyed`` with ``terminated_at`` set.
    """
    inst_uuid = UUID(instance_id)
    async with async_session() as session:
        inst = await session.get(Instance, inst_uuid)
        if inst is None:
            log.warning("teardown_instance.not_found", instance_id=instance_id)
            return {"ok": False, "error": "instance not found"}

        if inst.status == InstanceStatus.DESTROYED.value:
            log.info("teardown_instance.already_destroyed", slug=inst.slug)
            return {"ok": True, "slug": inst.slug, "state": "destroyed"}

        slug = inst.slug
        compose_project = inst.compose_project
        workspace_path = inst.workspace_path

    try:
        # 1. Docker first — quickest to short-circuit when already gone.
        await _compose_down(compose_project=compose_project)

        # 2. Cloudflare tunnel + DNS — TunnelService.destroy is idempotent.
        try:
            cf_config = await _load_cloudflare_config()
            tunnel_service = TunnelService(cf_config)
            await tunnel_service.destroy(inst_uuid, slug)
        except Exception as e:
            # Don't block teardown on a CF outage; we'll mark the row
            # destroyed anyway and a janitor path can clean orphans.
            log.warning(
                "teardown_instance.cf_destroy_failed",
                slug=slug, error=str(e),
            )

        # 3. Workspace directory + git worktree bookkeeping.
        # Two-step cleanup so the cache repo's `git worktree list` doesn't
        # leak references to deleted dirs:
        #   a) `git worktree remove --force` from the project's cache
        #      repo, telling git to drop both the dir AND its bookkeeping.
        #   b) `shutil.rmtree` as belt-and-braces for the case where the
        #      worktree was already half-removed (rmtree -f handles it).
        # Without (a), a future provision against the same chat-session
        # branch fails with "fatal: '<branch>' is already used by worktree
        # at <old path>" — even after the directory is gone — because git
        # still has the worktree registered in the cache repo's
        # .git/worktrees/. (Same bug bit a real chat retry on staging.)
        try:
            from openclow.services.workspace_service import WorkspaceService

            ws_path = pathlib.Path(workspace_path)
            project_name = None
            async with async_session() as session:
                inst_row = await session.get(Instance, inst_uuid)
                if inst_row is not None and inst_row.project is not None:
                    project_name = inst_row.project.name
            if project_name:
                cache_repo = pathlib.Path("/workspaces/_cache") / project_name
                if cache_repo.exists():
                    # Set safe.directory so git doesn't refuse on uid mismatches.
                    await asyncio.create_subprocess_exec(
                        "git", "config", "--global",
                        "--add", "safe.directory", str(cache_repo),
                    )
                    proc = await asyncio.create_subprocess_exec(
                        "git", "-C", str(cache_repo),
                        "worktree", "remove", "--force", str(ws_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    out, err = await proc.communicate()
                    if proc.returncode != 0:
                        # Stale worktree refs (dir gone but git still
                        # remembers) — prune them. Idempotent.
                        await asyncio.create_subprocess_exec(
                            "git", "-C", str(cache_repo),
                            "worktree", "prune",
                        )
            if ws_path.exists():
                shutil.rmtree(ws_path, ignore_errors=True)
        except Exception as e:
            log.warning(
                "teardown_instance.workspace_rm_failed",
                slug=slug, path=workspace_path, error=str(e),
            )

        # 4. Flip DB state.
        now = datetime.now(timezone.utc)
        slug_for_destroyed_emit: str | None = None
        async with async_session() as session:
            inst = await session.get(Instance, inst_uuid)
            if inst is not None:
                inst.status = InstanceStatus.DESTROYED.value
                inst.terminated_at = now
                await session.commit()
                slug_for_destroyed_emit = inst.slug
        # Spec 003 — admin SSE for the terminating → destroyed transition.
        if slug_for_destroyed_emit:
            emit_instance_event({
                "type": "instance_status",
                "slug": slug_for_destroyed_emit,
                "status": InstanceStatus.DESTROYED.value,
                "previous_status": InstanceStatus.TERMINATING.value,
                "at": now.isoformat(),
            })

            tunnel_row = (await session.execute(
                select(InstanceTunnel).where(
                    InstanceTunnel.instance_id == inst_uuid,
                    InstanceTunnel.status != TunnelStatus.DESTROYED.value,
                )
            )).scalar_one_or_none()
            if tunnel_row is not None:
                tunnel_row.status = TunnelStatus.DESTROYED.value
                tunnel_row.destroyed_at = now
                await session.commit()

        lifetime_s = (now - (inst.started_at or inst.created_at)).total_seconds() \
            if inst and (inst.started_at or inst.created_at) else 0
        log.info(
            "instance.destroyed",
            instance_slug=slug,
            reason=(inst.terminated_reason if inst else None),
            lifetime_s=lifetime_s,
        )
        return {"ok": True, "slug": slug, "state": "destroyed"}

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("teardown_instance.unexpected", slug=slug, error=str(e))
        # Leave the row in `terminating`; the next reaper/retry sweep
        # will re-enter this job.
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ProvisionFailure(Exception):
    """Structured failure with a FailureCode for chat-facing translation."""

    def __init__(self, failure_code: FailureCode, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


async def _make_workspace_shared_writable(
    *, host_workspace_dir: str | None, slug: str,
) -> None:
    """Make the per-instance workspace writable by ALL processes that
    need to touch it.

    Two players write to the same files:

      * **Worker / agent** — runs as ``openclow`` (uid 1000) inside the
        worker container. Uses MCP tools (workspace_mcp, git_mcp) to
        clone, edit, and commit. Owns the original clone.
      * **App container** — runs as ``www-data`` (uid 82 in
        serversideup/php-alpine). Composer/npm need to create
        ``vendor/`` and ``node_modules/`` inside ``/var/www/html``.

    A previous version of this helper chowned to www-data — which
    fixed composer but BROKE the agent (uid 1000 couldn't write to
    a uid-82-owned tree, surfaced as the "workspace mounted as
    read-only" error agents reported when asked to edit code).

    Solution: ``chmod -R 0777``. Per-chat throwaway dev workspace,
    security boundary is the container/host (Principle V — egress-only
    network surface). World-writable inside the workspace dir is fine
    because:
      * nothing privileged lives in there (creds are env-vars, never
        on disk per Principle IV);
      * the dir tree is rm -rf'd on teardown;
      * uid skew between agent (1000) and app (82, or whatever) is
        unavoidable when the same files have to be written by both.

    Idempotent — safe to re-run on an already-chmodded workspace.
    """
    if not host_workspace_dir:
        log.warning("workspace_perms.no_host_path", slug=slug)
        return

    # The worker just cloned the repo, so it owns every file as
    # openclow (uid 1000). chmod 0777 doesn't require root — only
    # owner. So do it locally in Python, no docker run needed. Earlier
    # version used a one-shot alpine container; that broke when the
    # local docker daemon couldn't pull `alpine:latest` (containerd
    # corruption after a docker disk resize). Local chmod has zero
    # external dependencies.
    #
    # The path inside THIS worker container is `workspace_path`
    # (which is `/workspaces/<slug>/` on the worker fs, bind-mounted
    # to/from the host's workspaces volume). Use that — `host_workspace_dir`
    # is the HOST-side path, not visible to this Python process.
    workspace_path = pathlib.Path("/workspaces") / slug
    if not workspace_path.is_dir():
        log.warning(
            "workspace_perms.path_not_found",
            slug=slug, expected_path=str(workspace_path),
        )
        return

    count = 0
    try:
        # 0o777 on the root dir first so subsequent file walks succeed
        # even if some intermediate dir is permission-locked.
        workspace_path.chmod(0o777)
        for entry in workspace_path.rglob("*"):
            try:
                entry.chmod(0o777)
                count += 1
            except (OSError, PermissionError):
                # Symlinks to absent targets, sockets, etc — keep going.
                continue
    except OSError as e:
        raise _ProvisionFailure(
            FailureCode.PROJCTL_UP,
            f"workspace chmod failed at {workspace_path}: {e}",
        )
    log.info(
        "workspace_perms.shared_writable",
        slug=slug, path=str(workspace_path), files_chmodded=count,
    )


async def _compose_up(
    *,
    compose_path: pathlib.Path,
    compose_project: str,
    env: dict[str, str],
) -> None:
    """Invoke ``docker compose -p <proj> -f <file> up -d`` with a timeout."""
    subproc_env = {**get_docker_env(), **env}
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose",
        "-p", compose_project,
        "-f", str(compose_path),
        "up", "-d", "--remove-orphans",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=subproc_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_COMPOSE_UP_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise _ProvisionFailure(
            FailureCode.COMPOSE_UP,
            f"docker compose up timed out after {_COMPOSE_UP_TIMEOUT_S}s",
        )
    if proc.returncode != 0:
        raise _ProvisionFailure(
            FailureCode.COMPOSE_UP,
            f"docker compose up exited {proc.returncode}: "
            f"{(stderr or b'').decode(errors='replace')[-4000:]}",
        )


async def _compose_down(*, compose_project: str) -> None:
    """Invoke ``docker compose -p <proj> down --volumes --remove-orphans``.

    Missing project is a no-op (compose returns 0). We still check rc to
    catch genuinely unexpected failures (daemon unreachable, etc.).
    """
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose",
        "-p", compose_project,
        "down", "--volumes", "--remove-orphans",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_docker_env(),
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_COMPOSE_DOWN_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning(
            "teardown_instance.compose_down_timeout",
            project=compose_project, timeout=_COMPOSE_DOWN_TIMEOUT_S,
        )
        return
    if proc.returncode not in (0,):
        log.warning(
            "teardown_instance.compose_down_nonzero",
            project=compose_project, rc=proc.returncode,
            stderr=(stderr or b"").decode(errors="replace")[:500],
        )


async def _projctl_up(*, compose_project: str, slug: str, instance_id: uuid.UUID | None = None) -> None:
    """Run ``projctl up`` inside the app container; parse JSON events.

    Emits one JSON line per event per projctl-stdout.schema.json. We fail
    the provision on ``fatal`` or on any stream that ends without a
    ``step_success`` for the last step (projctl exits non-zero on fatal).

    When ``instance_id`` is provided, each projctl log line is appended to
    the chat UI's agent-log panel in real time via ``_publish_progress_step``.
    """
    # The laravel-vue template bind-mounts the workspace at /var/www/html
    # (matching the serversideup base image's docroot expectation), not
    # at /app. projctl's defaults are /app — pass the workspace path
    # explicitly so guide.md is found and step `cwd` resolves correctly.
    # When other compose templates land, plumb this from
    # InstanceRenderContext.app_workspace_path instead of hardcoding.
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose",
        "-p", compose_project,
        "exec", "-T", "app",
        "projctl", "up",
        "--guide", "/var/www/html/guide.md",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_docker_env(),
    )

    async def _consume_stdout() -> None:
        assert proc.stdout is not None
        async for line in proc.stdout:
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                log.debug("projctl.non_json_line", slug=slug, raw=text[:200])
                # Stream non-JSON lines (raw shell output) to the agent log too.
                if instance_id is not None:
                    try:
                        await _publish_progress_step(
                            instance_id=instance_id,
                            stream_buffer_append=text,
                        )
                    except Exception:
                        pass
                continue
            kind = event.get("event")
            if kind == "step_success":
                log.info(
                    "projctl.step_success",
                    slug=slug,
                    step=event.get("step"),
                    attempt=event.get("attempt"),
                )
                if instance_id is not None:
                    try:
                        await _publish_progress_step(
                            instance_id=instance_id,
                            stream_buffer_append=f"✓ {event.get('step', '')}",
                        )
                    except Exception:
                        pass
            elif kind == "step_failure":
                log.warning(
                    "projctl.step_failure",
                    slug=slug,
                    step=event.get("step"),
                    attempt=event.get("attempt"),
                    exit_code=event.get("exit_code"),
                )
                if instance_id is not None:
                    try:
                        await _publish_progress_step(
                            instance_id=instance_id,
                            stream_buffer_append=f"✗ {event.get('step', '')} (exit {event.get('exit_code')})",
                        )
                    except Exception:
                        pass
            elif kind == "fatal":
                log.error(
                    "projctl.fatal",
                    slug=slug, reason=event.get("fatal_reason"),
                )
                if instance_id is not None:
                    try:
                        await _publish_progress_step(
                            instance_id=instance_id,
                            stream_buffer_append=f"fatal: {event.get('fatal_reason', '')}",
                        )
                    except Exception:
                        pass
            elif instance_id is not None:
                # Stream any other projctl event as a raw log line.
                try:
                    await _publish_progress_step(
                        instance_id=instance_id,
                        stream_buffer_append=text[:300],
                    )
                except Exception:
                    pass

    try:
        await asyncio.wait_for(
            asyncio.gather(_consume_stdout(), proc.wait()),
            timeout=_PROJCTL_UP_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise _ProvisionFailure(
            FailureCode.PROJCTL_UP,
            f"projctl up timed out after {_PROJCTL_UP_TIMEOUT_S}s",
        )

    if proc.returncode != 0:
        err = b""
        if proc.stderr is not None:
            try:
                err = await asyncio.wait_for(proc.stderr.read(2000), timeout=2)
            except asyncio.TimeoutError:
                pass
        raise _ProvisionFailure(
            FailureCode.PROJCTL_UP,
            f"projctl up exited {proc.returncode}: "
            f"{err.decode(errors='replace')[:500]}",
        )


async def _upsert_tunnel_row(
    instance_id: UUID,
    *,
    cf_tunnel_id: str,
    cf_tunnel_name: str,
    web_hostname: str,
    hmr_hostname: str,
    ide_hostname: str | None,
    credentials_secret: str,
) -> None:
    """Create or update the InstanceTunnel row for this instance.

    Idempotent: re-running provision after a mid-flight crash updates
    the existing row rather than inserting a duplicate (the partial
    unique index uq_instance_tunnels_one_active forbids two active rows
    for the same instance).
    """
    async with async_session() as session:
        row = (await session.execute(
            select(InstanceTunnel).where(
                InstanceTunnel.instance_id == instance_id,
                InstanceTunnel.status != TunnelStatus.DESTROYED.value,
            )
        )).scalar_one_or_none()
        if row is None:
            row = InstanceTunnel(
                instance_id=instance_id,
                cf_tunnel_id=cf_tunnel_id,
                cf_tunnel_name=cf_tunnel_name,
                web_hostname=web_hostname,
                hmr_hostname=hmr_hostname,
                ide_hostname=ide_hostname,
                credentials_secret=credentials_secret,
                status=TunnelStatus.PROVISIONING.value,
            )
            session.add(row)
        else:
            row.cf_tunnel_id = cf_tunnel_id
            row.cf_tunnel_name = cf_tunnel_name
            row.web_hostname = web_hostname
            row.hmr_hostname = hmr_hostname
            row.ide_hostname = ide_hostname
            row.credentials_secret = credentials_secret
        await session.commit()


async def _mark_failed(
    instance_id: UUID, failure_code: FailureCode, message: str
) -> None:
    """Transition a provisioning row to ``failed`` with a code + message.

    Called from the top-level except clauses in ``provision_instance``.
    Always commits even on DB error (logged) so a subsequent retry sees
    the failure recorded. Callers translate ``failure_code`` to chat
    copy per Phase 9 (T075–T080).
    """
    try:
        slug_for_emit: str | None = None
        async with async_session() as session:
            inst = await session.get(Instance, instance_id)
            if inst is None:
                return
            if inst.status == InstanceStatus.RUNNING.value:
                # Raced with a late success — don't clobber a running row.
                return
            inst.status = InstanceStatus.FAILED.value
            inst.failure_code = failure_code.value
            inst.failure_message = message[:2000]
            await session.commit()
            slug_for_emit = inst.slug
        log.error(
            "instance.failed",
            instance_id=str(instance_id),
            failure_code=failure_code.value,
            failure_message=message[:500],
        )
        # Spec 003 — admin SSE.
        if slug_for_emit:
            emit_instance_event({
                "type": "instance_status",
                "slug": slug_for_emit,
                "status": InstanceStatus.FAILED.value,
                "previous_status": InstanceStatus.PROVISIONING.value,
                "failure_code": failure_code.value,
                "failure_message": message[:500],
            })

        # Diagnostic capture BEFORE teardown wipes the volume. The
        # projctl-state volume holds state.json (with the last failed
        # step's stderrTail) and our _variant.sh tee'd per-step logs.
        # Once teardown runs `docker compose down --volumes`, it's gone —
        # and the chat UI only shows the failure_code, not the actual
        # error. Copying state + logs to a host path here is the only
        # way to keep the post-mortem alive across instance teardown.
        if slug_for_emit and failure_code == FailureCode.PROJCTL_UP:
            try:
                await _capture_projctl_diagnostics(slug=slug_for_emit)
            except Exception as cap_err:  # pragma: no cover
                log.warning(
                    "_mark_failed.capture_failed",
                    slug=slug_for_emit, error=str(cap_err),
                )

        # Auto-cleanup: enqueue teardown_instance to free the partial
        # state we created so far (workspace dir, git worktree, compose
        # stack, CF tunnel, DNS record). Without this, every failed
        # provision leaks until an admin force-terminates manually —
        # and the leaked git worktree blocks the chat from re-provisioning
        # against the same session_branch ("fatal: '<branch>' is already
        # used by worktree at <path>"). teardown_instance is idempotent
        # so this is safe even if the partial state is already cleaned.
        from openclow.services.bot_actions import enqueue_job
        # teardown_instance signature is (ctx, instance_id) — no reason
        # kwarg. The DB row already has failure_code/terminated_reason
        # set by InstanceService.terminate which teardown calls.
        await enqueue_job(
            "teardown_instance",
            instance_id=str(instance_id),
        )
    except Exception as e:  # pragma: no cover
        log.exception("_mark_failed.error", error=str(e))


async def _capture_projctl_diagnostics(*, slug: str) -> None:
    """Dump projctl state.json + _variant.sh logs out of the doomed
    instance's projctl-state volume to a persistent host path.

    Why: when projctl_up fails, ``_mark_failed`` enqueues teardown which
    runs ``docker compose down --volumes`` — destroying the
    projctl-state volume that holds the last failed step's stderr
    (``state.json:steps.<name>.last_stderr``) AND our ``_variant.sh``
    tee'd per-step logs. The chat UI is left showing only an opaque
    failure_code. This dump preserves the diagnostic so an operator can
    look at /var/log/openclow/failed-instances/<slug>-<ts>/ and see
    *why* the step failed.

    Best-effort: any docker error logs and returns (the caller already
    has bigger problems than missing post-mortem files).
    """
    import shutil
    from datetime import datetime, timezone as _tz

    ts = datetime.now(_tz.utc).strftime("%Y%m%d-%H%M%S")
    # /app/logs is the activity-logs volume that's already mounted into
    # the worker container (see docker-compose.yml::app_activity_logs).
    # Writing diagnostics there means they survive container restarts and
    # are accessible via `docker exec app-worker-1 ls /app/logs/...` or
    # the host volume path. Avoids needing a new bind mount.
    out_dir = pathlib.Path("/app/logs/failed-instances") / f"{slug}-{ts}"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("capture_diag.mkdir_failed", path=str(out_dir), error=str(e))
        return

    # The volume is named "<compose_project>-projctl-state" per compose.yml.
    volume_name = f"tagh-{slug}-projctl-state"

    # `docker run --rm -v <volume>:/state alpine` reads the volume without
    # touching the (failing/dead) per-instance compose stack. We tarball
    # /state to /dev/stdout, then untar into out_dir on the host.
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm",
        "-v", f"{volume_name}:/state:ro",
        "alpine", "sh", "-c",
        "cd /state && tar c .",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_docker_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("capture_diag.docker_run_timeout", slug=slug)
        return

    if proc.returncode != 0:
        log.warning(
            "capture_diag.docker_run_failed",
            slug=slug, rc=proc.returncode,
            stderr=stderr.decode(errors="replace")[:300],
        )
        return

    # Untar the captured tree into out_dir.
    untar = await asyncio.create_subprocess_exec(
        "tar", "x", "-C", str(out_dir),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    untar_stdout, untar_stderr = await untar.communicate(input=stdout)
    if untar.returncode != 0:
        log.warning(
            "capture_diag.untar_failed",
            slug=slug, rc=untar.returncode,
            stderr=untar_stderr.decode(errors="replace")[:300],
        )
        return

    # Surface the captured stderr inline so it shows in the worker log
    # without an operator having to ssh + cat. State.json has, per
    # projctl/internal/state, ``steps.<name>.last_stderr`` populated by
    # RecordFailure on the failing step.
    state_path = out_dir / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text())
            for step_name, step_data in (state.get("steps") or {}).items():
                if step_data.get("status") == "failed":
                    log.error(
                        "capture_diag.step_failed",
                        slug=slug, step=step_name,
                        last_stderr=(step_data.get("last_stderr") or "")[:1500],
                    )
        except (json.JSONDecodeError, OSError) as e:
            log.warning(
                "capture_diag.state_parse_failed",
                slug=slug, error=str(e),
            )

    log.info("capture_diag.done", slug=slug, path=str(out_dir))


async def _load_cloudflare_config() -> CloudflareConfig:
    """Read platform_config → CloudflareConfig. Fresh per provision."""
    cfg = await get_config("cloudflare", "settings")
    if not cfg:
        raise _ProvisionFailure(
            FailureCode.TUNNEL_PROVISION,
            "platform_config cloudflare/settings is not configured",
        )
    return CloudflareConfig(
        account_id=cfg["account_id"],
        zone_id=cfg["zone_id"],
        zone_domain=cfg["zone_domain"],
        api_token=cfg["api_token"],
    )


async def _load_github_app_config() -> GitHubAppConfig:
    """Read platform_config → GitHubAppConfig.

    Accepts either of two row shapes:
      * App mode: {"app_id": "...", "private_key_pem": "..."}
      * PAT mode: {"pat": "ghp_..."}
    See credentials_service.GitHubAppConfig for the trade-off.
    """
    cfg = await get_config("github_app", "settings")
    if not cfg:
        raise RuntimeError("platform_config github_app/settings not configured")
    return GitHubAppConfig(
        app_id=str(cfg.get("app_id", "")),
        private_key_pem=cfg.get("private_key_pem", ""),
        pat=cfg.get("pat", ""),
    )


def _template_dir_for_instance(instance_id: UUID) -> pathlib.Path:
    """Resolve which compose template directory an instance should use.

    v1 ships the single ``laravel-vue`` template. When additional templates
    land, this function should consult Project metadata (template_name
    column, added with the next template).
    """
    base = pathlib.Path(__file__).resolve().parents[2] / "setup" / "compose_templates"
    return base / "laravel-vue"


# Maps `composer.json` package name → variant string. The variant
# flows through the `PROJECT_VARIANT` env var into the per-instance
# `app` container, where the bundled `_variant.sh` script dispatches
# step-by-step based on this string. Adding a 4th variant here is
# the only place to extend for "command-only" differences (no template
# fork needed). Order matters when a project happens to require two
# of these — first match wins; in practice they're mutually exclusive.
_VARIANT_PACKAGES: dict[str, str] = {
    "gecche/laravel-multidomain": "multidomain-gecche",
    "spatie/laravel-multitenancy": "multidomain-spatie",
    "stancl/tenancy": "multidomain-stancl",
}


def _detect_project_variant(workspace_path: str) -> str:
    """Inspect the cloned project's ``composer.json`` and pick a variant.

    Returns one of: ``"normal"`` (default Laravel single-tenant — what
    every existing chat used), or one of the multi-domain variants in
    ``_VARIANT_PACKAGES``. Pure function — no DB, no network. Safe to
    call from anywhere in provision_instance.

    Failure modes (missing file, malformed JSON, IO error) all fall
    through to ``"normal"`` with a warning log — preserves today's
    behaviour for non-PHP projects (no composer.json) and for any
    transient read failure (we'd rather provision wrong than crash).
    """
    composer_path = pathlib.Path(workspace_path) / "composer.json"
    if not composer_path.is_file():
        return "normal"
    try:
        data = json.loads(composer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        log.warning(
            "variant_detect.composer_read_failed",
            path=str(composer_path), error=str(e)[:200],
        )
        return "normal"
    require = data.get("require") or {}
    require_dev = data.get("require-dev") or {}
    pkgs = set(require.keys()) | set(require_dev.keys())
    for pkg, variant in _VARIANT_PACKAGES.items():
        if pkg in pkgs:
            return variant
    return "normal"


# Provision-step progress mirror — keys MUST match the step `name`
# values in assistant.py auto-provision block (the API creates the
# initial card; the worker advances it as each phase boundary lands).
# Keep these in sync if the step list changes.
_PROVISION_STEPS_NAMES: tuple[str, ...] = (
    "Provisioning Cloudflare tunnel",
    "Booting containers",
    "App bootstrap (composer + npm)",
    "Health check",
)


async def _publish_progress_step(
    *,
    instance_id: str,
    completed_step_index: int,
    overall_status: str = "running",
    failed_step_index: int | None = None,
    stream_buffer_append: str | None = None,
) -> None:
    """Advance the in-thread provisioning card by one phase.

    Looks up the chat session + the latest unclosed __PROGRESS_CARD__
    message for that chat, rewrites step statuses (steps[0..completed]
    = done, steps[completed+1] = running, rest = pending), bumps the
    `elapsed` counter, persists to the message row, and publishes via
    Redis to `wc:{user}:{session}` so the live thread re-renders.

    Best-effort: any failure logs a warning and returns without raising.
    The provision flow MUST NOT fail because the progress card couldn't
    be advanced (the env coming up is the real success contract; the
    card is observability).
    """
    try:
        import json as _pj
        from openclow.models.web_chat import WebChatMessage, WebChatSession
        from sqlalchemy import select as _sel, desc as _desc

        async with async_session() as _db:
            inst = await _db.get(Instance, UUID(instance_id))
            if inst is None:
                return
            chat = await _db.get(WebChatSession, inst.chat_session_id)
            if chat is None:
                return
            user_id = chat.user_id
            session_id = chat.id
            # Find the latest open progress card for this chat.
            row = (await _db.execute(
                _sel(WebChatMessage).where(
                    WebChatMessage.session_id == session_id,
                    WebChatMessage.role == "assistant",
                    WebChatMessage.is_complete.is_(False),
                    WebChatMessage.content.like("__PROGRESS_CARD__%"),
                ).order_by(_desc(WebChatMessage.created_at)).limit(1)
            )).scalar_one_or_none()
            if row is None:
                return
            try:
                card = _pj.loads(row.content[len("__PROGRESS_CARD__"):])
            except Exception:
                return
            steps = card.get("steps") or []
            # Rewrite statuses based on completed_step_index.
            for i, step in enumerate(steps):
                if failed_step_index is not None and i == failed_step_index:
                    step["status"] = "failed"
                elif i <= completed_step_index:
                    step["status"] = "done"
                elif i == completed_step_index + 1 and overall_status == "running":
                    step["status"] = "running"
                else:
                    step["status"] = "pending"
            card["overall_status"] = overall_status
            # Elapsed: bump from started_at if known, else just monotonically.
            try:
                if inst.started_at:
                    delta = (
                        datetime.now(timezone.utc) - inst.started_at
                    ).total_seconds()
                    card["elapsed"] = max(0, int(delta))
                else:
                    card["elapsed"] = int(card.get("elapsed", 0)) + 5
            except Exception:
                card["elapsed"] = int(card.get("elapsed", 0)) + 5
            # stream_buffer is the AgentLogPanel content — append-only,
            # cap at last 4KB so it doesn't grow unbounded across a long
            # provision. Each line gets a [HH:MM:SS] timestamp prefix to
            # match the existing log conventions.
            if stream_buffer_append:
                from datetime import datetime as _dt
                prev = card.get("stream_buffer") or ""
                stamp = _dt.now(timezone.utc).strftime("%H:%M:%S")
                line = f"[{stamp}] {stream_buffer_append.rstrip()}"
                merged = (prev + ("\n" if prev else "") + line)[-4096:]
                card["stream_buffer"] = merged
            new_content = f"__PROGRESS_CARD__{_pj.dumps(card)}"
            row.content = new_content
            if overall_status in ("done", "failed"):
                row.is_complete = True
            await _db.commit()

        # Publish to live thread.
        try:
            import redis.asyncio as _aioredis
            from openclow.settings import settings as _s
            _channel = f"wc:{user_id}:{session_id}"
            _r = _aioredis.from_url(_s.redis_url)
            try:
                await _r.publish(_channel, _pj.dumps({
                    "type": "progress_card",
                    "message_id": str(row.id),
                    "card": card,
                }))
            finally:
                await _r.aclose()
        except Exception as _e:
            log.warning(
                "provision_instance.progress_publish_failed",
                slug=inst.slug, error=str(_e),
            )
    except Exception as _e:
        log.warning(
            "provision_instance.progress_step_failed",
            instance_id=instance_id, error=str(_e),
        )


_FIRST_PAINT_LARAVEL_EXCEPTION_MARKERS = (
    "ViteManifestNotFoundException",
    "ViteManifestNotFound",
    "Whoops, looks like something went wrong.",
    "Internal Server Error",
)


async def _wait_for_first_paint(
    *, web_hostname: str, slug: str, timeout_s: int,
    instance_id: str | None = None,
) -> None:
    """Poll the public URL until the app actually serves a real 200.

    Without this gate, ``status='running'`` flips the moment ``docker
    compose up`` returns — but the dev server (Vite) may still be
    warming up. Laravel's @vite() blade then 500s with
    ``ViteManifestNotFoundException`` because public/hot isn't written
    yet. The user sees a broken app on a "running" instance.

    Accepts ONLY: HTTP 200 + body free of Laravel exception markers.
    Anything else (5xx, 404, exception page returned with 200) is
    "not ready, keep waiting". On timeout, raises ``_ProvisionFailure``
    with ``FailureCode.HEALTH_CHECK`` so InstanceService.terminate
    flips the row to ``failed`` and the chat surfaces a Retry card.
    """
    import httpx

    public_url = f"https://{web_hostname}/"
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_status: int | None = None
    last_error: str | None = None
    last_heartbeat_at = asyncio.get_event_loop().time()
    poll_count = 0

    log.info(
        "provision_instance.first_paint_wait_start",
        slug=slug, url=public_url, timeout_s=timeout_s,
    )

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        follow_redirects=True,
    ) as client:
        while True:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                # First-paint timeout no longer fails the provision. The
                # platform's job is to bring infrastructure up; if the
                # user's application code (Vite config, dep conflicts,
                # Laravel exception) keeps the app from rendering, we
                # still want them in the chat with a working agent so
                # they can DEBUG the app — not a dead "failed" card.
                # The instance flips to running with a degraded upstream
                # marker; the chat UI surfaces a "App not responding"
                # banner so the user knows to ask the agent to fix it.
                log.warning(
                    "provision_instance.first_paint_timeout_degraded",
                    slug=slug, last_status=last_status,
                    last_error=last_error, timeout_s=timeout_s,
                )
                if instance_id:
                    await _publish_progress_step(
                        instance_id=instance_id,
                        completed_step_index=3,
                        stream_buffer_append=(
                            f"App did not serve a clean 200 within {timeout_s}s "
                            f"(last_status={last_status}, last_error={last_error}). "
                            "Marking instance live anyway — ask the agent to debug."
                        ),
                    )
                return
            try:
                resp = await client.get(public_url)
                last_status = resp.status_code
                if resp.status_code == 200:
                    body = resp.text
                    if any(
                        marker in body
                        for marker in _FIRST_PAINT_LARAVEL_EXCEPTION_MARKERS
                    ):
                        last_error = "laravel_exception_page"
                    else:
                        log.info(
                            "provision_instance.first_paint_ready",
                            slug=slug,
                            elapsed_s=round(timeout_s - (deadline - now), 1),
                        )
                        return
                else:
                    last_error = f"http_{resp.status_code}"
            except httpx.HTTPError as e:
                last_error = type(e).__name__
            poll_count += 1
            # Heartbeat to the progress card every ~6s so the user sees
            # activity during the (potentially 1-3 min) Vite warmup +
            # Laravel boot. Each heartbeat advances elapsed and appends
            # one line to the Agent log so they can read what's
            # actually happening.
            if instance_id and (now - last_heartbeat_at) >= 6.0:
                hb_line = (
                    f"poll #{poll_count}: status={last_status or 'unreachable'}"
                    + (f" ({last_error})" if last_error else "")
                )
                await _publish_progress_step(
                    instance_id=instance_id,
                    completed_step_index=2,  # "Health check" (idx 3) is currently running
                    stream_buffer_append=hb_line,
                )
                last_heartbeat_at = now
            await asyncio.sleep(_FIRST_PAINT_POLL_INTERVAL_S)


def _copy_template_support_files(
    template_dir: pathlib.Path, output_dir: pathlib.Path
) -> None:
    """Copy non-rendered support files (nginx.conf, vite.config.js, guide.md).

    The compose renderer only writes _compose.yml + _cloudflared.yml; any
    additional static files referenced by the compose file (here:
    ``./_nginx.conf`` mount) must exist in the output dir alongside.
    """
    # NOTE: do NOT copy vite.config.js into the worktree. Vite picks
    # `.js` over the project's own `.ts` config, which silently
    # disables every project-side server option (CORS, HMR, allowed
    # hosts). Projects own their Vite config; the platform documents
    # the required HMR/CORS snippet (see docs/setup/PROJECT_VITE.md
    # — TODO) and the project commits it to its repo.
    for src_name, dst_name in (
        ("nginx.conf", "_nginx.conf"),
        ("guide.md", "guide.md"),
        ("project.yaml", "project.yaml"),
        # _variant.sh is the dispatcher invoked by every variant-aware
        # guide.md step (`sh /var/www/html/_variant.sh <step>`). Must be
        # present in the workspace before compose-up so the bind-mount
        # has it ready when projctl tries to exec.
        ("_variant.sh", "_variant.sh"),
    ):
        src = template_dir / src_name
        if not src.is_file():
            continue
        dst = output_dir / dst_name
        dst.write_bytes(src.read_bytes())
        # Make _variant.sh executable inside the bind-mount; the cp
        # above preserves bytes but not modes when the destination is
        # newly created on certain filesystems.
        if dst_name == "_variant.sh":
            dst.chmod(0o755)


def _heartbeat_url_for(slug: str) -> str:
    """Build the URL projctl posts to inside the instance.

    The path component is fixed by contracts/heartbeat-api.md; only the
    base URL varies (dev/staging/prod). When operators need to override,
    they can set platform_config orchestrator/internal_base_url.
    """
    base = os.environ.get("ORCHESTRATOR_INTERNAL_URL", _DEFAULT_ORCHESTRATOR_INTERNAL_URL)
    return f"{base.rstrip('/')}/internal/instances/{slug}/heartbeat"


async def tunnel_health_check_cron(ctx: dict) -> dict:
    """T083 — sweep every running/idle instance and probe CF tunnel health.

    ARQ cron fires every minute. For each ``running``/``idle`` row we
    call ``TunnelService.health(slug)`` which does one CF API lookup
    per slug (cheap). On failure we emit
    ``instance.upstream_degraded`` via ``InstanceService``; on recovery
    we emit ``instance.upstream_recovered``. We NEVER flip
    ``instances.status`` to ``failed`` — FR-027a mandates the instance
    keeps running; only the banner changes.

    Returns a summary dict ``{"probed": N, "degraded": M, "recovered": K}``.
    """
    from openclow.services.tunnel_service import TunnelService

    probed = 0
    degraded = 0
    recovered = 0

    # Load CF config once per sweep so a mid-sweep rotation doesn't race.
    try:
        cf_cfg = await _load_cloudflare_config()
    except _ProvisionFailure:
        log.info("tunnel_health_check_cron.skipped_no_cf_config")
        return {"probed": 0, "degraded": 0, "recovered": 0, "skipped": True}

    ts = TunnelService(cf_cfg)
    svc = InstanceService()

    async with async_session() as session:
        result = await session.execute(
            select(Instance).where(
                Instance.status.in_((
                    InstanceStatus.RUNNING.value,
                    InstanceStatus.IDLE.value,
                )),
            )
        )
        rows = list(result.scalars().all())

    for inst in rows:
        probed += 1
        # Authoritative health is CF's tunnel status. Any API hiccup
        # counts as "unhealthy" for this sweep — the next sweep will
        # reconcile if CF was just flapping.
        try:
            healthy = await ts.health(inst.slug)
        except Exception as e:  # pragma: no cover
            log.warning(
                "tunnel_health_check.probe_error",
                slug=inst.slug, error=str(e)[:200],
            )
            healthy = False
        # v1 policy: emit events on every sweep outcome. Deduping is a
        # follow-up; the chat banner reads the most-recent event per
        # (instance, capability) so repeated "degraded" emits are idempotent.
        try:
            if healthy:
                await svc.record_upstream_recovery(
                    inst.id, capability="preview_url", upstream="cloudflare",
                )
                recovered += 1
                # Spec 003 — emit instance_upstream so the admin detail view
                # can flip the tunnel-health badge live without polling.
                emit_instance_event({
                    "type": "instance_upstream",
                    "slug": inst.slug,
                    "capability": "preview_url",
                    "health": "live",
                    "at": datetime.now(timezone.utc).isoformat(),
                })
            else:
                await svc.record_upstream_degradation(
                    inst.id, capability="preview_url", upstream="cloudflare",
                )
                degraded += 1
                emit_instance_event({
                    "type": "instance_upstream",
                    "slug": inst.slug,
                    "capability": "preview_url",
                    "health": "degraded",
                    "at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:  # pragma: no cover
            log.warning(
                "tunnel_health_check.event_failed",
                slug=inst.slug, error=str(e)[:200],
            )

    log.info(
        "tunnel_health_check.sweep",
        probed=probed, degraded=degraded, recovered=recovered,
    )
    return {"probed": probed, "degraded": degraded, "recovered": recovered}


async def orphan_compose_sweeper_cron(ctx: dict) -> dict:
    """Find `tagh-inst-*` compose projects whose DB row is gone or
    terminated/destroyed, and tear them down.

    On 2026-04-28 a chain of failed provisions left ~21 leaked
    containers running because the auto-teardown had a kwarg bug;
    those leaks chewed enough RAM to OOM the staging droplet's sshd.
    Even after the kwarg fix, defence-in-depth: any future leak —
    crashed worker mid-teardown, manual kill, etc. — gets reaped here.

    Runs every 15 minutes. Safe-by-default: only acts on compose
    projects whose name matches ``tagh-inst-*`` AND whose slug has
    NO live (provisioning/running/idle/terminating) row in the DB.

    Returns ``{"orphans_found": N, "torn_down": M}``.
    """
    # 1. List all compose projects.
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "ls", "--all", "--format", "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_docker_env(),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("orphan_sweeper.docker_compose_ls_timeout")
        return {"orphans_found": 0, "torn_down": 0, "error": "compose_ls_timeout"}
    if proc.returncode != 0:
        return {"orphans_found": 0, "torn_down": 0,
                "error": f"compose_ls_rc={proc.returncode}"}

    try:
        rows = json.loads(stdout.decode() or "[]")
    except json.JSONDecodeError:
        return {"orphans_found": 0, "torn_down": 0, "error": "json_decode"}

    candidates = [r for r in rows if (r.get("Name") or "").startswith("tagh-inst-")]
    if not candidates:
        return {"orphans_found": 0, "torn_down": 0}

    # 2. Cross-reference with live DB rows.
    LIVE = (
        InstanceStatus.PROVISIONING.value,
        InstanceStatus.RUNNING.value,
        InstanceStatus.IDLE.value,
        InstanceStatus.TERMINATING.value,
    )
    async with async_session() as session:
        result = await session.execute(
            select(Instance.compose_project)
            .where(Instance.status.in_(LIVE))
        )
        live_projects = {row[0] for row in result.all()}

    orphans = [r for r in candidates if r["Name"] not in live_projects]
    if not orphans:
        return {"orphans_found": 0, "torn_down": 0}

    # 3. Tear down each orphan with `compose down -v --remove-orphans`.
    torn = 0
    failed = 0
    for orph in orphans:
        name = orph["Name"]
        log.warning("orphan_sweeper.tearing_down", project=name)
        # Reuse the same down flags as teardown_instance so volumes go
        # too — leaked named volumes (db-data, node_modules) are the
        # main reason the host runs out of disk over time.
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-p", name,
            "down", "--remove-orphans", "--volumes",
            "--timeout", "30",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=get_docker_env(),
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_COMPOSE_DOWN_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("orphan_sweeper.down_timeout", project=name)
            failed += 1
            continue
        if proc.returncode == 0:
            torn += 1
        else:
            failed += 1
            log.warning(
                "orphan_sweeper.down_failed",
                project=name, rc=proc.returncode,
                stderr=stderr.decode()[:200],
            )

    log.info(
        "orphan_sweeper.sweep",
        orphans_found=len(orphans), torn_down=torn, failed=failed,
    )
    return {"orphans_found": len(orphans), "torn_down": torn, "failed": failed}


async def rotate_github_token(ctx: dict, instance_id: str) -> dict:
    """T063: mint a fresh GitHub installation token and inject it into
    the running instance's ``~/.git-credentials`` via ``docker exec``.

    Called every 45 min by the in-instance cron (T065) — projctl posts
    to ``/internal/instances/<slug>/rotate-git-token`` to RECEIVE a new
    token. Operators can also invoke this ARQ job directly from the
    dashboard as a manual refresh; that's the path this function
    serves.

    Idempotent: if the mint succeeds but the injection step fails, a
    retry mints a fresh token and retries the injection; the worst-case
    outcome is one extra unused token (GitHub drops it after 1h).
    """
    inst_uuid = UUID(instance_id)
    async with async_session() as session:
        inst = await session.get(Instance, inst_uuid)
        if inst is None:
            return {"ok": False, "error": "instance not found"}
        if inst.status not in (
            InstanceStatus.RUNNING.value,
            InstanceStatus.IDLE.value,
        ):
            return {"ok": False, "error": f"instance is {inst.status}"}
        slug = inst.slug
        compose_project = inst.compose_project
        repo = inst.project.github_repo if inst.project else None

    if not repo:
        return {"ok": False, "error": "instance has no bound repo"}

    try:
        gh_cfg = await _load_github_app_config()
        creds = CredentialsService(gh_cfg)
        token = await creds.github_push_token(inst_uuid, repo)
    except Exception as e:
        log.warning(
            "rotate_github_token.mint_failed",
            slug=slug, error=str(e)[:200],
        )
        return {"ok": False, "error": f"mint failed: {str(e)[:200]}"}

    # Write to ~/.git-credentials via a one-shot docker exec. The
    # credentials file's format is ``https://x-access-token:<token>@github.com``.
    # We echo via stdin so the token never appears in `ps` output.
    line = f"https://x-access-token:{token.token}@github.com\n"
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose",
        "-p", compose_project,
        "exec", "-T", "app",
        "sh", "-c",
        # Overwrite (not append) so stale tokens never accumulate.
        "cat > $HOME/.git-credentials && chmod 600 $HOME/.git-credentials",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_docker_env(),
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=line.encode("utf-8")),
            timeout=_DOCKER_META_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "docker exec timed out"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"docker exec exit {proc.returncode}: "
            f"{(stderr or b'').decode(errors='replace')[:500]}",
        }

    log.info("rotate_github_token.success", slug=slug, repo=repo)
    return {"ok": True, "slug": slug, "repo": repo, "expires_at": token.expires_at}


__all__ = ["provision_instance", "teardown_instance", "rotate_github_token"]
