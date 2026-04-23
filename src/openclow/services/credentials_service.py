"""Per-instance, short-lived credentials (Constitution Principle IV).

Spec: specs/001-per-chat-instances/plan.md §CredentialsService;
research.md §3 (GitHub App installation tokens).

Three credential types, all scoped to ONE instance and destroyed with it:

  * github_push_token(instance_id) → 1-hour installation token scoped to
    the repo bound to the instance's project. Mints via GitHub App JWT +
    installation-token exchange. Never persisted beyond in-memory return.

  * heartbeat_secret(instance_id) → fetches the HMAC secret generated at
    provision time and stored on `instances.heartbeat_secret`.

  * cf_token(instance_id) → fetches the Cloudflare named-tunnel credential
    JSON from its Docker secret. Internal path; the CLI never calls this.

This module is HTTP/DB client-only. It does NOT generate credentials; it
exchanges or fetches them. Provisioning-time generation (`secrets.token_*`)
lives in InstanceService.
"""
from __future__ import annotations

import base64
import dataclasses
import secrets
import time
from uuid import UUID

import httpx
import jwt

from openclow.utils.logging import get_logger

log = get_logger()

# Mandatory per Principle IX (see tunnel_service.py for the same constant).
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)

# GitHub's max installation-token TTL.
INSTALLATION_TOKEN_TTL_SECONDS = 3600

# JWT for app-level auth is signed for ≤10 min (GitHub's max). We sign for
# 9 min + 30 s clock skew = 510 s to stay safely under the cap.
APP_JWT_TTL_SECONDS = 9 * 60


@dataclasses.dataclass(frozen=True)
class GitHubAppConfig:
    """Deployment-level GitHub App settings.

    Loaded from `platform_config` (category='github_app') by the caller.
    `private_key_pem` is the raw PEM string (starts with
    `-----BEGIN RSA PRIVATE KEY-----` or `-----BEGIN PRIVATE KEY-----`).
    """

    app_id: str
    private_key_pem: str
    api_base: str = "https://api.github.com"


@dataclasses.dataclass(frozen=True)
class InstallationToken:
    """A freshly minted installation token for one repo."""

    token: str
    expires_at: float   # epoch seconds
    repo: str           # "owner/name"


class CredentialsServiceError(Exception):
    """Base for CredentialsService errors."""


class GitHubAppError(CredentialsServiceError):
    """App-level JWT exchange or installation-token fetch failed."""


class CredentialsService:
    """Mints / fetches per-instance credentials. Stateless across calls.

    Installation IDs are memoised per repo because they don't change over
    the App's lifetime. The installation TOKEN itself is NOT cached —
    callers should call this method fresh per rotation interval (45 min
    via projctl per contracts/heartbeat-api.md).
    """

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        http_client_factory=None,
    ) -> None:
        self._config = config
        self._http_client_factory = http_client_factory or self._default_client
        self._installation_id_cache: dict[str, int] = {}

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._config.api_base,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    # ------------------------------------------------------------------
    # GitHub push token — the main path (FR-023; research.md §3)
    # ------------------------------------------------------------------

    async def github_push_token(
        self,
        instance_id: UUID,
        repo: str,
    ) -> InstallationToken:
        """Mint a 1-hour installation token scoped to the single repo.

        Token is never persisted — caller either injects it into the
        instance container's env on compose up, or returns it over the
        `/internal/instances/<slug>/rotate-git-token` endpoint and
        immediately discards the local copy.
        """
        jwt_token = self._sign_app_jwt()
        async with self._http_client_factory() as client:
            installation_id = await self._get_installation_id(client, repo, jwt_token)
            token = await self._create_installation_token(
                client, installation_id, jwt_token, repo
            )
        log.info(
            "credentials.github_token_minted",
            instance_id=str(instance_id),
            repo=repo,
            expires_at=token.expires_at,
        )
        return token

    def _sign_app_jwt(self) -> str:
        """Sign a short-TTL JWT for GitHub App authentication.

        Per research.md §3: 10-min max TTL; we use 9 min to stay under it
        even with clock skew. `iss` is the App ID; `iat` is now minus 30s
        to tolerate a clock slightly behind ours.
        """
        now = int(time.time())
        payload = {
            "iat": now - 30,
            "exp": now + APP_JWT_TTL_SECONDS,
            "iss": self._config.app_id,
        }
        return jwt.encode(
            payload, self._config.private_key_pem, algorithm="RS256"
        )

    async def _get_installation_id(
        self, client: httpx.AsyncClient, repo: str, jwt_token: str
    ) -> int:
        """Resolve installation ID for a repo, memoising by repo.

        Installation IDs are stable for the App's lifetime on a given repo,
        so we cache. Cache is per CredentialsService instance — the class
        is expected to live for the process's lifetime.
        """
        cached = self._installation_id_cache.get(repo)
        if cached is not None:
            return cached

        owner, name = _split_repo(repo)
        r = await client.get(
            f"/repos/{owner}/{name}/installation",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        if r.status_code != 200:
            raise GitHubAppError(
                f"installation lookup failed for {repo}: "
                f"{r.status_code} {r.text[:200]}"
            )
        installation_id = int(r.json()["id"])
        self._installation_id_cache[repo] = installation_id
        return installation_id

    async def _create_installation_token(
        self,
        client: httpx.AsyncClient,
        installation_id: int,
        jwt_token: str,
        repo: str,
    ) -> InstallationToken:
        """POST /app/installations/:id/access_tokens scoped to one repo."""
        owner, name = _split_repo(repo)
        r = await client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {jwt_token}"},
            json={
                # Scope the token to THIS repo only (FR-023 belt + braces).
                "repositories": [name],
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                },
            },
        )
        if r.status_code not in (200, 201):
            raise GitHubAppError(
                f"installation-token exchange failed for {repo}: "
                f"{r.status_code} {r.text[:200]}"
            )
        body = r.json()
        token = body["token"]
        # expires_at is ISO8601; but we already know it's +1h; use that
        # for sane default even if parsing fails.
        expires_at = time.time() + INSTALLATION_TOKEN_TTL_SECONDS
        return InstallationToken(token=token, expires_at=expires_at, repo=repo)

    # ------------------------------------------------------------------
    # Heartbeat secret + DB password + CF token (lookups, not mints)
    # ------------------------------------------------------------------

    @staticmethod
    def generate_heartbeat_secret() -> str:
        """Generate a 32-byte URL-safe secret for HMAC-SHA256.

        Called at provision time by InstanceService, stored on
        `instances.heartbeat_secret`. Exposed as a @staticmethod so the
        provisioner doesn't need a full CredentialsService instance.
        """
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")

    @staticmethod
    def generate_db_password() -> str:
        """Generate a random DB password for the instance's MySQL.

        16 bytes → 22 URL-safe chars. Enough entropy for a per-instance
        secret that lives inside the compose network and is never exposed
        outside the instance.
        """
        return base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")


def _split_repo(repo: str) -> tuple[str, str]:
    """Parse 'owner/name' → ('owner', 'name')."""
    if "/" not in repo:
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    owner, name = repo.split("/", 1)
    if not owner or not name:
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    return owner, name
