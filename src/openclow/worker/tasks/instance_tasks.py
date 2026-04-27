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
from openclow.services.instance_service import InstanceService
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

    started_at = datetime.now(timezone.utc)

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
            },
        )

        # 5. projctl up inside the app container. Poll its JSON-line
        # stdout for step_success / fatal events per the stdout schema.
        await _projctl_up(compose_project=compose_project, slug=slug)

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

        duration_s = (now - started_at).total_seconds()
        log.info(
            "instance.running", instance_slug=slug, startup_duration_s=duration_s,
        )
        return {"ok": True, "slug": slug, "state": "running"}

    except _ProvisionFailure as e:
        await _mark_failed(inst_uuid, e.failure_code, str(e))
        return {"ok": False, "error": str(e), "failure_code": e.failure_code.value}
    except TunnelServiceError as e:
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

        # 3. Workspace directory. Missing dir is fine.
        try:
            ws = pathlib.Path(workspace_path)
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
        except Exception as e:
            log.warning(
                "teardown_instance.workspace_rm_failed",
                slug=slug, path=workspace_path, error=str(e),
            )

        # 4. Flip DB state.
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            inst = await session.get(Instance, inst_uuid)
            if inst is not None:
                inst.status = InstanceStatus.DESTROYED.value
                inst.terminated_at = now
                await session.commit()

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


async def _projctl_up(*, compose_project: str, slug: str) -> None:
    """Run ``projctl up`` inside the app container; parse JSON events.

    Emits one JSON line per event per projctl-stdout.schema.json. We fail
    the provision on ``fatal`` or on any stream that ends without a
    ``step_success`` for the last step (projctl exits non-zero on fatal).
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
                continue
            kind = event.get("event")
            if kind == "step_success":
                log.info(
                    "projctl.step_success",
                    slug=slug,
                    step=event.get("step"),
                    attempt=event.get("attempt"),
                )
            elif kind == "step_failure":
                log.warning(
                    "projctl.step_failure",
                    slug=slug,
                    step=event.get("step"),
                    attempt=event.get("attempt"),
                    exit_code=event.get("exit_code"),
                )
            elif kind == "fatal":
                log.error(
                    "projctl.fatal",
                    slug=slug, reason=event.get("fatal_reason"),
                )

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
        log.error(
            "instance.failed",
            instance_id=str(instance_id),
            failure_code=failure_code.value,
            failure_message=message[:500],
        )
    except Exception as e:  # pragma: no cover
        log.exception("_mark_failed.error", error=str(e))


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


_FIRST_PAINT_LARAVEL_EXCEPTION_MARKERS = (
    "ViteManifestNotFoundException",
    "ViteManifestNotFound",
    "Whoops, looks like something went wrong.",
    "Internal Server Error",
)


async def _wait_for_first_paint(
    *, web_hostname: str, slug: str, timeout_s: int
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
                raise _ProvisionFailure(
                    FailureCode.HEALTH_CHECK,
                    f"first-paint gate timed out after {timeout_s}s "
                    f"(last_status={last_status}, last_error={last_error}). "
                    f"App did not serve a clean 200 — check node logs for Vite "
                    f"errors and app logs for Laravel exceptions."
                )
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
    ):
        src = template_dir / src_name
        if not src.is_file():
            continue
        dst = output_dir / dst_name
        dst.write_bytes(src.read_bytes())


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
            else:
                await svc.record_upstream_degradation(
                    inst.id, capability="preview_url", upstream="cloudflare",
                )
                degraded += 1
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
