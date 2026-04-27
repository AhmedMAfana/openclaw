"""T022: unit tests for CredentialsService (GitHub App + generators).

Covers:
  * JWT format (RS256, 9-min TTL, iss=AppID)
  * Installation-ID lookup + memoisation
  * Installation-token scoped to ONE repo (FR-023)
  * Error surfacing on 4xx from GitHub
  * Heartbeat + DB password generators return sufficient entropy
"""
from __future__ import annotations

import time
import uuid

import httpx
import jwt
import pytest

from openclow.services.credentials_service import (
    APP_JWT_TTL_SECONDS,
    CredentialsService,
    GitHubAppConfig,
    GitHubAppError,
    INSTALLATION_TOKEN_TTL_SECONDS,
)

# RSA key is expensive to generate; do it once per session.
try:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
except ImportError:  # pragma: no cover - cryptography is required in prod
    rsa = None


@pytest.fixture(scope="session")
def rsa_pem() -> str:
    if rsa is None:
        pytest.skip("cryptography not installed")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def svc(rsa_pem: str) -> CredentialsService:
    return CredentialsService(GitHubAppConfig(app_id="12345", private_key_pem=rsa_pem))


def test_app_jwt_uses_rs256_and_app_id(svc: CredentialsService, rsa_pem: str) -> None:
    token = svc._sign_app_jwt()
    # Verifying with the public key gives back our claims.
    pub = (
        serialization.load_pem_private_key(rsa_pem.encode(), password=None)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    claims = jwt.decode(token, pub, algorithms=["RS256"])
    assert claims["iss"] == "12345"
    now = int(time.time())
    assert abs(claims["exp"] - now - APP_JWT_TTL_SECONDS) <= 2, claims
    # `iat` is backdated 30s for clock-skew tolerance.
    assert claims["iat"] <= now


@pytest.mark.asyncio
async def test_github_push_token_happy_path(svc: CredentialsService) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/acme/app/installation":
            return httpx.Response(200, json={"id": 777})
        if request.method == "POST" and request.url.path == "/app/installations/777/access_tokens":
            body = request.read().decode()
            assert '"repositories": ["app"]' in body, body  # FR-023: repo-scoped
            assert '"contents": "write"' in body, body
            assert '"pull_requests": "write"' in body, body
            return httpx.Response(201, json={"token": "ghs_abcdef123"})
        return httpx.Response(500)

    svc._http_client_factory = lambda: httpx.AsyncClient(
        base_url=svc._config.api_base,
        transport=httpx.MockTransport(handler),
    )
    tok = await svc.github_push_token(uuid.uuid4(), "acme/app")
    assert tok.token == "ghs_abcdef123"
    assert tok.repo == "acme/app"
    # Expiry is 1h in the future (give or take test execution time).
    assert abs(tok.expires_at - (time.time() + INSTALLATION_TOKEN_TTL_SECONDS)) <= 2


@pytest.mark.asyncio
async def test_installation_id_is_memoised(svc: CredentialsService) -> None:
    """Repeat calls for the same repo must hit the installation-id endpoint once."""
    calls = {"installation": 0, "access_tokens": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/installation":
            calls["installation"] += 1
            return httpx.Response(200, json={"id": 777})
        if request.url.path == "/app/installations/777/access_tokens":
            calls["access_tokens"] += 1
            return httpx.Response(201, json={"token": f"ghs_call{calls['access_tokens']}"})
        return httpx.Response(500)

    svc._http_client_factory = lambda: httpx.AsyncClient(
        base_url=svc._config.api_base,
        transport=httpx.MockTransport(handler),
    )
    await svc.github_push_token(uuid.uuid4(), "acme/app")
    await svc.github_push_token(uuid.uuid4(), "acme/app")
    assert calls["installation"] == 1, calls
    assert calls["access_tokens"] == 2, calls


@pytest.mark.asyncio
async def test_github_app_error_surfaces_on_404(svc: CredentialsService) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='{"message":"Not Found"}')

    svc._http_client_factory = lambda: httpx.AsyncClient(
        base_url=svc._config.api_base,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GitHubAppError, match="installation lookup failed"):
        await svc.github_push_token(uuid.uuid4(), "acme/missing")


def test_heartbeat_secret_entropy():
    a = CredentialsService.generate_heartbeat_secret()
    b = CredentialsService.generate_heartbeat_secret()
    assert a != b  # Two calls, different values.
    # 32 bytes URL-safe-b64 → 43 chars (no padding).
    assert len(a) >= 40, len(a)
    # URL-safe alphabet only.
    import string
    allowed = set(string.ascii_letters + string.digits + "-_")
    assert set(a).issubset(allowed), a


def test_db_password_entropy():
    a = CredentialsService.generate_db_password()
    b = CredentialsService.generate_db_password()
    assert a != b
    # 16 bytes URL-safe-b64 → 22 chars.
    assert 20 <= len(a) <= 24, len(a)


@pytest.mark.asyncio
async def test_bad_repo_format_raises_before_http(svc: CredentialsService) -> None:
    called = False

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    svc._http_client_factory = lambda: httpx.AsyncClient(
        base_url=svc._config.api_base,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match="owner/name"):
        await svc.github_push_token(uuid.uuid4(), "not-a-valid-repo")
    assert not called, "must fail-fast before hitting GitHub"
