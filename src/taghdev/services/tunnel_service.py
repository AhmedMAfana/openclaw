"""Per-instance Cloudflare NAMED tunnel lifecycle.

Spec: specs/001-per-chat-instances/plan.md §Implementation PR 4;
research.md §2 (Cloudflare v4 REST API via httpx with explicit timeouts).
Data-model: instance_tunnels (specs/001-per-chat-instances/data-model.md §2).

This is the rewritten service for `mode='container'` projects. It replaces
the old quick-tunnel path with per-instance named tunnels whose state lives
in Postgres (one row per instance in `instance_tunnels`). No worker-local
process dicts (Principle VI). Every HTTP call has an explicit timeout
(Principle IX).

Backwards compatibility (FR-034):
    Every legacy quick-tunnel symbol (`start_tunnel`, `stop_tunnel`,
    `get_tunnel_url`, `check_tunnel_health`, `ensure_tunnel`,
    `refresh_tunnel`, `verify_tunnel_url`, `sync_project_tunnel`) is
    re-exported from `legacy_tunnel_service` so the 30+ existing
    call sites keep working unchanged until they are migrated in
    PR 12 (bootstrap router flip).

Usage (new code):
    from taghdev.services.tunnel_service import TunnelService
    ts = TunnelService(settings)
    tunnel = await ts.provision(instance_id)
    await ts.destroy(instance_id)
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

# --- legacy re-exports (FR-034; do not remove without migrating callers) ----
from taghdev.services.legacy_tunnel_service import (  # noqa: F401  (re-export)
    check_tunnel_health,
    ensure_tunnel,
    get_tunnel_url,
    refresh_tunnel,
    start_tunnel,
    stop_tunnel,
    sync_project_tunnel,
    verify_tunnel_url,
)
from taghdev.utils.logging import get_logger

if TYPE_CHECKING:  # Avoid import-at-startup side effects in lightweight tests.
    from taghdev.models.instance_tunnel import InstanceTunnel

log = get_logger()

# Cloudflare v4 API base. Overridable for testing.
CF_API_BASE = "https://api.cloudflare.com/client/v4"

# Mandatory per Principle IX. "No timeout" is a bug, not a default.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)


@dataclasses.dataclass(frozen=True)
class CloudflareConfig:
    """Deployment-level Cloudflare settings.

    Loaded from `platform_config` (category='cloudflare') by the caller.
    Kept as a plain dataclass so unit tests can pass fakes without a DB.
    """

    account_id: str
    zone_id: str
    zone_domain: str        # e.g. "dev.example.com"
    api_token: str          # scopes: Account.CF Tunnel:Edit, Zone.DNS:Edit
    api_base: str = CF_API_BASE


@dataclasses.dataclass(frozen=True)
class TunnelProvisionResult:
    """Everything the InstanceService needs after provision() succeeds."""

    cf_tunnel_id: str
    cf_tunnel_name: str
    web_hostname: str
    hmr_hostname: str
    ide_hostname: str | None
    credentials_secret: str   # Docker-secret NAME (not the JSON)
    credentials_blob: str     # the credential JSON itself — caller stores
                              # in a Docker secret and discards from memory


class TunnelServiceError(Exception):
    """Base for TunnelService-raised errors."""


class CloudflareAPIError(TunnelServiceError):
    """CF API returned non-2xx or a malformed payload."""

    def __init__(self, status: int, body: str):
        super().__init__(f"cloudflare api returned {status}: {body[:200]}")
        self.status = status
        self.body = body


class TunnelService:
    """Manage per-instance named Cloudflare tunnels + DNS.

    All methods are idempotent (Principle VI). Callers pass an
    `instance_id`; the service maps it to `instance_tunnels` rows AND to
    Cloudflare resources by querying the CF API by tunnel *name*. This is
    what lets a mid-operation crash recover: the next call re-queries and
    either no-ops or forward-completes.
    """

    def __init__(
        self,
        config: CloudflareConfig,
        *,
        http_client_factory=None,
    ) -> None:
        self._config = config
        # Factory so tests can inject an AsyncClient that hits pytest-httpx.
        self._http_client_factory = http_client_factory or self._default_client

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._config.api_base,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "Authorization": f"Bearer {self._config.api_token}",
                "Content-Type": "application/json",
            },
        )

    # ------------------------------------------------------------------
    # Provision
    # ------------------------------------------------------------------

    async def provision(
        self, instance_id: UUID, instance_slug: str
    ) -> TunnelProvisionResult:
        """Create a named tunnel + DNS records for an instance.

        Idempotent: if a tunnel with the same name already exists at CF,
        this method re-uses it and returns its token. Safe to re-run after
        a mid-provision orchestrator crash (see research.md §4).

        Caller (InstanceService) is responsible for:
          * Persisting the InstanceTunnel row with `status='provisioning'`
            BEFORE calling this (so a crash leaves DB state recoverable)
          * Storing `credentials_blob` in a Docker secret and discarding
            the in-memory copy immediately (Principle IV)
          * Flipping the row to `status='active'` once cloudflared reports
            the connection is registered
        """
        tunnel_name = self._tunnel_name(instance_slug)
        # NOTE — Cloudflare Universal SSL coverage:
        #   zone_domain = "tagh.co.uk"        → "inst-X.tagh.co.uk"        (1 level, covered FREE)
        #   zone_domain = "apps.tagh.co.uk"   → "inst-X.apps.tagh.co.uk"   (2 levels, NOT covered by Universal SSL —
        #                                        needs either an Advanced Certificate ($10/mo) OR `apps.tagh.co.uk`
        #                                        registered as its own Cloudflare zone with separate Universal SSL)
        # Toggle by updating platform_config:
        #   UPDATE platform_config
        #     SET value = jsonb_set(value, '{zone_domain}', '"<new>"')
        #     WHERE category='cloudflare' AND key='settings';
        web_host = f"{instance_slug}.{self._config.zone_domain}"
        hmr_host = f"hmr-{instance_slug}.{self._config.zone_domain}"
        ide_host = f"ide-{instance_slug}.{self._config.zone_domain}"

        async with self._http_client_factory() as client:
            # 1. Idempotent create: try create; if already exists, fetch.
            existing = await self._find_tunnel_by_name(client, tunnel_name)
            if existing is not None:
                cf_tunnel_id = existing["id"]
                token_blob = await self._fetch_tunnel_token(client, cf_tunnel_id)
            else:
                created = await self._create_tunnel(client, tunnel_name)
                cf_tunnel_id = created["id"]
                token_blob = created["token"]

            # 2. Idempotent DNS records (skip if present with same content).
            cname_target = f"{cf_tunnel_id}.cfargotunnel.com"
            await self._ensure_cname(client, web_host, cname_target)
            await self._ensure_cname(client, hmr_host, cname_target)
            await self._ensure_cname(client, ide_host, cname_target)

        log.info(
            "tunnel.provisioned",
            instance_id=str(instance_id),
            slug=instance_slug,
            cf_tunnel_id=cf_tunnel_id,
        )

        return TunnelProvisionResult(
            cf_tunnel_id=cf_tunnel_id,
            cf_tunnel_name=tunnel_name,
            web_hostname=web_host,
            hmr_hostname=hmr_host,
            ide_hostname=ide_host,
            credentials_secret=f"{tunnel_name}-cf",  # Docker-secret NAME
            credentials_blob=token_blob,             # caller stores in secret
        )

    # ------------------------------------------------------------------
    # Destroy
    # ------------------------------------------------------------------

    async def destroy(self, instance_id: UUID, instance_slug: str) -> None:
        """Delete DNS records + tunnel from Cloudflare.

        Idempotent: missing resources are skipped. Safe to re-run.

        Caller (InstanceService) flips `instance_tunnels.status` to
        `'destroyed'` and sets `destroyed_at` AFTER this returns.
        """
        tunnel_name = self._tunnel_name(instance_slug)
        async with self._http_client_factory() as client:
            tunnel = await self._find_tunnel_by_name(client, tunnel_name)
            if tunnel is None:
                log.info("tunnel.destroy.already_gone", slug=instance_slug)
                return
            cf_tunnel_id = tunnel["id"]

            # Delete CNAMEs that point at this tunnel's cfargotunnel host.
            cname_target = f"{cf_tunnel_id}.cfargotunnel.com"
            await self._delete_records_for_target(client, cname_target)

            # Delete the tunnel itself.
            await self._delete_tunnel(client, cf_tunnel_id)

        log.info(
            "tunnel.destroyed",
            instance_id=str(instance_id),
            slug=instance_slug,
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self, instance_slug: str) -> bool:
        """Return True iff CF reports the tunnel as healthy.

        Used by the upstream-degradation banner job (T083) and the
        provision-time readiness check. Conservative: any CF API hiccup
        returns False, caller policy decides whether to retry or escalate.
        """
        tunnel_name = self._tunnel_name(instance_slug)
        try:
            async with self._http_client_factory() as client:
                tunnel = await self._find_tunnel_by_name(client, tunnel_name)
                if tunnel is None:
                    return False
                return tunnel.get("status") == "healthy"
        except (httpx.HTTPError, CloudflareAPIError):
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tunnel_name(slug: str) -> str:
        """`tagh-<slug>` — matches the convention used everywhere else.

        Kept under the CF tunnel-name length limit and safe as a
        Docker-secret name.
        """
        return f"tagh-{slug}"

    async def _find_tunnel_by_name(
        self, client: httpx.AsyncClient, name: str
    ) -> dict | None:
        """GET /accounts/:a/cfd_tunnel?name=<n> → first match or None.

        Idempotency hinge for both provision and destroy.
        """
        r = await client.get(
            f"/accounts/{self._config.account_id}/cfd_tunnel",
            params={"name": name},
        )
        _raise_for_status(r)
        result = r.json().get("result") or []
        return result[0] if result else None

    async def _create_tunnel(
        self, client: httpx.AsyncClient, name: str
    ) -> dict:
        """POST /accounts/:a/cfd_tunnel → {id, token, ...}."""
        r = await client.post(
            f"/accounts/{self._config.account_id}/cfd_tunnel",
            json={"name": name, "config_src": "cloudflare"},
        )
        _raise_for_status(r)
        return r.json()["result"]

    async def _fetch_tunnel_token(
        self, client: httpx.AsyncClient, tunnel_id: str
    ) -> str:
        """GET /accounts/:a/cfd_tunnel/:id/token → token blob.

        Used on the recovery path when `_find_tunnel_by_name` already
        found the tunnel — we still need the creds JSON to re-populate
        the Docker secret if it was lost (defense-in-depth for partial
        failures per research.md §4).
        """
        r = await client.get(
            f"/accounts/{self._config.account_id}/cfd_tunnel/{tunnel_id}/token"
        )
        _raise_for_status(r)
        return r.json()["result"]

    async def _delete_tunnel(
        self, client: httpx.AsyncClient, tunnel_id: str
    ) -> None:
        r = await client.delete(
            f"/accounts/{self._config.account_id}/cfd_tunnel/{tunnel_id}"
        )
        # 404 on teardown retry is fine — already gone.
        if r.status_code == 404:
            return
        _raise_for_status(r)

    async def _ensure_cname(
        self, client: httpx.AsyncClient, name: str, content: str
    ) -> None:
        """Create a CNAME if missing; no-op if it already points to content.

        Idempotent by design. Does NOT support updating an existing record
        to a different target — if the record exists with the wrong target
        we log and leave it alone (operator intervention required to catch
        an accidental zone-level conflict).
        """
        existing = await self._find_dns_record(client, name)
        if existing is None:
            r = await client.post(
                f"/zones/{self._config.zone_id}/dns_records",
                json={
                    "type": "CNAME",
                    "name": name,
                    "content": content,
                    "proxied": True,
                    "ttl": 1,  # CF "automatic"
                },
            )
            _raise_for_status(r)
            return

        if existing.get("content") != content:
            log.warning(
                "tunnel.dns_conflict",
                name=name,
                existing=existing.get("content"),
                wanted=content,
                note="leaving existing record in place; operator review required",
            )

    async def _find_dns_record(
        self, client: httpx.AsyncClient, name: str
    ) -> dict | None:
        r = await client.get(
            f"/zones/{self._config.zone_id}/dns_records",
            params={"name": name},
        )
        _raise_for_status(r)
        result = r.json().get("result") or []
        return result[0] if result else None

    async def _delete_records_for_target(
        self, client: httpx.AsyncClient, target: str
    ) -> None:
        """Delete every CNAME that points at this tunnel's cfargotunnel host.

        Cloudflare is authoritative for DNS; we re-query on teardown
        rather than storing record IDs locally (data-model.md §2.4).
        """
        r = await client.get(
            f"/zones/{self._config.zone_id}/dns_records",
            params={"content": target},
        )
        _raise_for_status(r)
        for rec in r.json().get("result") or []:
            rec_id = rec.get("id")
            if not rec_id:
                continue
            dr = await client.delete(
                f"/zones/{self._config.zone_id}/dns_records/{rec_id}"
            )
            if dr.status_code == 404:
                continue
            _raise_for_status(dr)


def _raise_for_status(r: httpx.Response) -> None:
    """Raise CloudflareAPIError on any non-2xx with useful context."""
    if 200 <= r.status_code < 300:
        return
    raise CloudflareAPIError(r.status_code, r.text or "")
