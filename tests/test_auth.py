"""Tests for the OAuth (Clerk + CIMD, PKCE-only) provider and its configuration."""

from __future__ import annotations

import pytest
from fastmcp.server.auth.cimd import CIMDDocument
from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from langsmith_mcp_server.auth import (
    ENV_BASE_URL,
    ENV_CLERK_CLIENT_ID,
    ENV_CLERK_CLIENT_SECRET,
    ENV_CLERK_DOMAIN,
    ENV_JWT_SIGNING_KEY,
    ENV_REQUIRE_CONSENT,
    ENV_REQUIRED_SCOPES,
    ClerkCIMDOAuthProvider,
    OAuthConfigError,
    build_auth_provider_from_env,
    is_oauth_configured,
)

BASE_URL = "https://mcp.example.com"
UPSTREAM_CLIENT_ID = "clerk-oauth-app-client-id"
CIMD_CLIENT_ID = "https://client.example/oauth/client-metadata.json"
REDIRECT_URI = "http://localhost:3000/callback"

FULL_ENV = {
    ENV_CLERK_DOMAIN: "example.clerk.accounts.dev",
    ENV_CLERK_CLIENT_ID: UPSTREAM_CLIENT_ID,
    ENV_CLERK_CLIENT_SECRET: "super-secret",
    ENV_BASE_URL: BASE_URL,
}


def make_provider(**overrides) -> ClerkCIMDOAuthProvider:
    kwargs = dict(
        domain="example.clerk.accounts.dev",
        client_id=UPSTREAM_CLIENT_ID,
        client_secret="super-secret",
        base_url=BASE_URL,
        client_storage=MemoryStore(),
    )
    kwargs.update(overrides)
    return ClerkCIMDOAuthProvider(**kwargs)


def make_cimd_client(client_id: str = CIMD_CLIENT_ID) -> ProxyDCRClient:
    doc = CIMDDocument(
        client_id=client_id,
        client_name="Test MCP client",
        redirect_uris=["http://localhost:*/callback"],
    )
    return ProxyDCRClient(
        client_id=client_id,
        client_secret=None,
        redirect_uris=None,
        grant_types=doc.grant_types,
        scope="openid email profile",
        token_endpoint_auth_method="none",
        cimd_document=doc,
    )


def make_client(provider: ClerkCIMDOAuthProvider) -> TestClient:
    routes = provider.get_routes(mcp_path="/mcp")
    return TestClient(Starlette(routes=routes), base_url=BASE_URL)


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------


def test_env_not_configured_returns_none():
    assert is_oauth_configured({}) is False
    assert build_auth_provider_from_env({}) is None


@pytest.mark.parametrize("missing", [ENV_CLERK_DOMAIN, ENV_CLERK_CLIENT_ID, ENV_BASE_URL])
def test_env_partial_configuration_raises(missing):
    env = {k: v for k, v in FULL_ENV.items() if k != missing}
    with pytest.raises(OAuthConfigError, match=missing):
        build_auth_provider_from_env(env)


def test_env_requires_secret_or_signing_key():
    env = {k: v for k, v in FULL_ENV.items() if k != ENV_CLERK_CLIENT_SECRET}
    with pytest.raises(OAuthConfigError, match=ENV_JWT_SIGNING_KEY):
        build_auth_provider_from_env(env)
    env[ENV_JWT_SIGNING_KEY] = "a-signing-key"
    provider = build_auth_provider_from_env(env, client_storage=MemoryStore())
    assert isinstance(provider, ClerkCIMDOAuthProvider)


def test_env_full_configuration_builds_provider():
    env = dict(FULL_ENV)
    env[ENV_REQUIRED_SCOPES] = "openid, email profile"
    env[ENV_REQUIRE_CONSENT] = "external"
    provider = build_auth_provider_from_env(env, client_storage=MemoryStore())
    assert isinstance(provider, ClerkCIMDOAuthProvider)
    assert provider.required_scopes == ["openid", "email", "profile"]
    assert str(provider.base_url).rstrip("/") == BASE_URL
    assert provider._upstream_client_id == UPSTREAM_CLIENT_ID
    assert provider._upstream_authorization_endpoint == (
        "https://example.clerk.accounts.dev/oauth/authorize"
    )


def test_env_invalid_consent_value_raises():
    env = dict(FULL_ENV)
    env[ENV_REQUIRE_CONSENT] = "maybe"
    with pytest.raises(OAuthConfigError, match=ENV_REQUIRE_CONSENT):
        build_auth_provider_from_env(env, client_storage=MemoryStore())


# ---------------------------------------------------------------------------
# Dynamic Client Registration is disabled, CIMD is the only client model
# ---------------------------------------------------------------------------


def test_no_register_route():
    provider = make_provider()
    paths = [r.path for r in provider.get_routes(mcp_path="/mcp") if isinstance(r, Route)]
    assert "/register" not in paths
    assert "/authorize" in paths
    assert "/token" in paths
    assert "/.well-known/oauth-authorization-server" in paths
    assert "/.well-known/oauth-protected-resource/mcp" in paths


def test_register_endpoint_returns_404():
    client = make_client(make_provider())
    response = client.post("/register", json={"redirect_uris": [REDIRECT_URI]})
    assert response.status_code == 404


async def test_register_client_raises():
    provider = make_provider()
    with pytest.raises(ValueError, match="Dynamic Client Registration is disabled"):
        await provider.register_client(make_cimd_client())


async def test_get_client_rejects_non_cimd_ids():
    provider = make_provider()
    # The stock OAuthProxy synthesizes a client for the upstream client id; we do not.
    assert await provider.get_client(UPSTREAM_CLIENT_ID) is None
    assert await provider.get_client("some-random-id") is None
    assert await provider.get_client("http://insecure.example/client.json") is None


async def test_get_client_resolves_cimd_ids(monkeypatch):
    provider = make_provider()
    stub = make_cimd_client()
    fetched: list[str] = []

    async def fake_fetch(client_id: str):
        fetched.append(client_id)
        return stub

    monkeypatch.setattr(provider._cimd_manager, "get_client", fake_fetch)
    client = await provider.get_client(CIMD_CLIENT_ID)
    assert client is not None
    assert client.client_id == CIMD_CLIENT_ID
    assert fetched == [CIMD_CLIENT_ID]


# ---------------------------------------------------------------------------
# Discovery metadata advertises exactly: code + PKCE S256, CIMD, no DCR
# ---------------------------------------------------------------------------


def test_authorization_server_metadata():
    client = make_client(make_provider())
    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata.get("registration_endpoint") is None
    assert metadata["client_id_metadata_document_supported"] is True
    assert metadata["response_types_supported"] == ["code"]
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["none", "private_key_jwt"]
    assert metadata["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert metadata["token_endpoint"] == f"{BASE_URL}/token"


def test_protected_resource_metadata():
    client = make_client(make_provider())
    response = client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == f"{BASE_URL}/mcp"
    assert metadata["authorization_servers"] == [f"{BASE_URL}/"]
    assert metadata["bearer_methods_supported"] == ["header"]


# ---------------------------------------------------------------------------
# Authorization endpoint: PKCE required, only response_type=code, CIMD clients only
# ---------------------------------------------------------------------------


@pytest.fixture
def provider_with_cimd_client(monkeypatch) -> ClerkCIMDOAuthProvider:
    provider = make_provider()
    stub = make_cimd_client()

    async def fake_fetch(client_id: str):
        return stub if client_id == CIMD_CLIENT_ID else None

    monkeypatch.setattr(provider._cimd_manager, "get_client", fake_fetch)
    return provider


def authorize_params(**overrides) -> dict:
    params = {
        "client_id": CIMD_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
        "state": "xyz",
    }
    params.update(overrides)
    return {k: v for k, v in params.items() if v is not None}


def test_authorize_rejects_upstream_client_id(provider_with_cimd_client):
    client = make_client(provider_with_cimd_client)
    response = client.get(
        "/authorize",
        params=authorize_params(client_id=UPSTREAM_CLIENT_ID),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_authorize_requires_pkce(provider_with_cimd_client):
    client = make_client(provider_with_cimd_client)
    response = client.get(
        "/authorize",
        params=authorize_params(code_challenge=None, code_challenge_method=None),
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(REDIRECT_URI)
    assert "error=invalid_request" in location
    assert "code_challenge" in location


def test_authorize_rejects_plain_pkce_method(provider_with_cimd_client):
    client = make_client(provider_with_cimd_client)
    response = client.get(
        "/authorize",
        params=authorize_params(code_challenge_method="plain"),
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "error=invalid_request" in response.headers["location"]


def test_authorize_rejects_implicit_flow(provider_with_cimd_client):
    client = make_client(provider_with_cimd_client)
    response = client.get(
        "/authorize", params=authorize_params(response_type="token"), follow_redirects=False
    )
    assert response.status_code == 302
    assert "error=unsupported_response_type" in response.headers["location"]


def test_authorize_valid_request_starts_transaction(provider_with_cimd_client):
    client = make_client(provider_with_cimd_client)
    response = client.get("/authorize", params=authorize_params(), follow_redirects=False)
    assert response.status_code == 302
    # With consent enabled, the user is sent to the consent page before Clerk.
    assert response.headers["location"].startswith(f"{BASE_URL}/consent?txn_id=")


# ---------------------------------------------------------------------------
# Token endpoint: only authorization_code (+refresh_token), code_verifier required
# ---------------------------------------------------------------------------


def test_token_rejects_other_grant_types(provider_with_cimd_client):
    client = make_client(provider_with_cimd_client)
    for grant_type in ("client_credentials", "password", "implicit"):
        response = client.post(
            "/token", data={"grant_type": grant_type, "client_id": CIMD_CLIENT_ID}
        )
        assert response.status_code == 400, grant_type
        assert response.json()["error"] in ("invalid_request", "unsupported_grant_type")


def test_token_requires_code_verifier(provider_with_cimd_client):
    client = make_client(provider_with_cimd_client)
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CIMD_CLIENT_ID,
            "code": "some-code",
            "redirect_uri": REDIRECT_URI,
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_request"
    assert "code_verifier" in body["error_description"]
