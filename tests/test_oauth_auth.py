"""
Tests for OAuth 2.1 resource-server behaviour (MCP authorization spec, 2026-07-28).

These run fully offline: tokens are signed with a locally generated RSA key and verified with
the matching PEM public key (``MCP_OAUTH_PUBLIC_KEY`` path), so no JWKS fetch is needed.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from langsmith_mcp_server.auth.clerk import _parse_clerk_verify_response, looks_like_jwt
from langsmith_mcp_server.auth.provider import check_authorization_server
from langsmith_mcp_server.auth.settings import AuthMode, OAuthSettings, load_auth_settings
from langsmith_mcp_server.server import create_app, create_mcp_server

ISSUER = "https://clerk.example.test"
SERVER_URL = "https://mcp.example.test"
RESOURCE = f"{SERVER_URL}/mcp"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture(scope="module")
def key_pair() -> RSAKeyPair:
    return RSAKeyPair.generate()


def make_settings(key_pair: RSAKeyPair, **overrides: Any) -> OAuthSettings:
    settings = OAuthSettings(
        mode=AuthMode.OAUTH,
        issuer=ISSUER,
        server_url=SERVER_URL,
        audiences=[RESOURCE, RESOURCE + "/"],
        public_key=key_pair.public_key,
        startup_check=False,
    )
    for k, v in overrides.items():
        setattr(settings, k, v)
    settings.validate()
    return settings


def make_token(key_pair: RSAKeyPair, **kwargs: Any) -> str:
    params: Dict[str, Any] = {
        "subject": "user_123",
        "issuer": ISSUER,
        "audience": RESOURCE,
        "scopes": ["openid", "profile", "email"],
    }
    params.update(kwargs)
    return key_pair.create_token(**params)


class RunningApp:
    """ASGI app + lifespan + httpx client bundle."""

    def __init__(self, settings: OAuthSettings):
        self.settings = settings
        self.mcp = create_mcp_server(settings)
        self.app = create_app(self.mcp, settings)
        self.client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "RunningApp":
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=SERVER_URL
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self.client is not None
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)

    async def initialize(self, token: Optional[str] = None, **headers: str) -> httpx.Response:
        assert self.client is not None
        hdrs = dict(MCP_HEADERS)
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers)
        return await self.client.post("/mcp", json=INITIALIZE, headers=hdrs)


def parse_rpc_result(response: httpx.Response) -> Dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no data line in SSE body: {response.text!r}")
    return response.json()


def parse_www_authenticate(value: str) -> Dict[str, str]:
    assert value.startswith("Bearer "), value
    params: Dict[str, str] = {}
    for part in value[len("Bearer ") :].split(", "):
        k, _, v = part.partition("=")
        params[k.strip()] = v.strip().strip('"')
    return params


# --------------------------------------------------------------------------- discovery
async def test_protected_resource_metadata_path_based(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        assert running.client is not None
        resp = await running.client.get("/.well-known/oauth-protected-resource/mcp")
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["resource"] == RESOURCE
        assert doc["authorization_servers"] == [ISSUER]
        assert doc["scopes_supported"] == ["openid", "profile", "email"]
        assert doc["bearer_methods_supported"] == ["header"]


async def test_protected_resource_metadata_root_fallback(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        assert running.client is not None
        resp = await running.client.get("/.well-known/oauth-protected-resource")
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["resource"] == RESOURCE
        assert doc["authorization_servers"] == [ISSUER]
        assert doc["bearer_methods_supported"] == ["header"]


async def test_health_is_public(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        assert running.client is not None
        resp = await running.client.get("/health")
        assert resp.status_code == 200


# ------------------------------------------------------------------------- challenges
async def test_missing_token_returns_401_with_resource_metadata(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        resp = await running.initialize()
        assert resp.status_code == 401
        challenge = parse_www_authenticate(resp.headers["www-authenticate"])
        assert challenge["resource_metadata"] == (
            f"{SERVER_URL}/.well-known/oauth-protected-resource/mcp"
        )
        assert challenge["error"] == "invalid_token"
        assert challenge["scope"] == "openid profile email"


async def test_legacy_api_key_header_alone_is_not_enough_in_oauth_mode(
    key_pair: RSAKeyPair,
) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        resp = await running.initialize(**{"LANGSMITH-API-KEY": "lsv2_pt_x"})
        assert resp.status_code == 401


async def test_wrong_audience_is_rejected(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        token = make_token(key_pair, audience="https://other.example.test/mcp")
        resp = await running.initialize(token)
        assert resp.status_code == 401


async def test_missing_audience_is_rejected(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        token = make_token(key_pair, audience=None)
        resp = await running.initialize(token)
        assert resp.status_code == 401


async def test_wrong_issuer_is_rejected(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        token = make_token(key_pair, issuer="https://evil.example.test")
        resp = await running.initialize(token)
        assert resp.status_code == 401


async def test_expired_token_is_rejected(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        token = make_token(key_pair, expires_in_seconds=-60)
        resp = await running.initialize(token)
        assert resp.status_code == 401


async def test_token_signed_by_other_key_is_rejected(key_pair: RSAKeyPair) -> None:
    other = RSAKeyPair.generate()
    async with RunningApp(make_settings(key_pair)) as running:
        token = make_token(other)
        resp = await running.initialize(token)
        assert resp.status_code == 401


async def test_opaque_token_rejected_when_not_enabled(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        resp = await running.initialize("oat_not_a_jwt")
        assert resp.status_code == 401


async def test_insufficient_scope_returns_403(key_pair: RSAKeyPair) -> None:
    settings = make_settings(key_pair, required_scopes=["langsmith:read"])
    async with RunningApp(settings) as running:
        token = make_token(key_pair, scopes=["openid"])
        resp = await running.initialize(token)
        assert resp.status_code == 403
        challenge = parse_www_authenticate(resp.headers["www-authenticate"])
        assert challenge["error"] == "insufficient_scope"
        assert "resource_metadata" in challenge


# ------------------------------------------------------------------------ happy paths
async def test_valid_token_initializes_session(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        resp = await running.initialize(make_token(key_pair))
        assert resp.status_code == 200, resp.text
        result = parse_rpc_result(resp)
        assert result["result"]["serverInfo"]["name"] == "LangSmith API MCP Server"


async def test_trailing_slash_audience_is_accepted(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        resp = await running.initialize(make_token(key_pair, audience=RESOURCE + "/"))
        assert resp.status_code == 200, resp.text


async def test_audience_list_containing_resource_is_accepted(key_pair: RSAKeyPair) -> None:
    async with RunningApp(make_settings(key_pair)) as running:
        token = make_token(key_pair, audience=["https://other.example.test", RESOURCE])
        resp = await running.initialize(token)
        assert resp.status_code == 200, resp.text


# -------------------------------------------------------------------------- legacy mode
async def test_api_key_mode_requires_header() -> None:
    settings = OAuthSettings(mode=AuthMode.API_KEY)
    async with RunningApp(settings) as running:
        resp = await running.initialize()
        assert resp.status_code == 401
        assert resp.json() == {"error": "Missing LANGSMITH-API-KEY header"}
        resp = await running.initialize(**{"LANGSMITH-API-KEY": "lsv2_pt_x"})
        assert resp.status_code == 200, resp.text


async def test_api_key_mode_has_no_resource_metadata() -> None:
    settings = OAuthSettings(mode=AuthMode.API_KEY)
    async with RunningApp(settings) as running:
        assert running.client is not None
        resp = await running.client.get("/.well-known/oauth-protected-resource/mcp")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------- settings
def test_settings_default_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MCP_AUTH_MODE", "CLERK_ISSUER", "MCP_SERVER_URL", "CLERK_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert load_auth_settings().mode == AuthMode.API_KEY

    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.example.test/")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.test/")
    settings = load_auth_settings()
    assert settings.mode == AuthMode.OAUTH
    assert settings.issuer == ISSUER
    assert settings.resource_url == RESOURCE
    assert settings.audiences == [RESOURCE, RESOURCE + "/"]
    assert settings.jwks_url == f"{ISSUER}/.well-known/jwks.json"
    assert settings.resource_metadata_url == (
        f"{SERVER_URL}/.well-known/oauth-protected-resource/mcp"
    )
    assert settings.allow_opaque_tokens is False
    assert settings.user_credentials_from_clerk is False

    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
    settings = load_auth_settings()
    assert settings.allow_opaque_tokens is True
    assert settings.user_credentials_from_clerk is True


def test_settings_require_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.example.test")
    monkeypatch.setenv("MCP_SERVER_URL", "http://mcp.example.test")
    with pytest.raises(ValueError, match="https"):
        load_auth_settings()
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8000")
    assert load_auth_settings().resource_url == "http://localhost:8000/mcp"


def test_settings_oauth_mode_requires_issuer_and_server_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.delenv("CLERK_ISSUER", raising=False)
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    with pytest.raises(ValueError, match="CLERK_ISSUER"):
        load_auth_settings()
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.example.test")
    with pytest.raises(ValueError, match="MCP_SERVER_URL"):
        load_auth_settings()


# ---------------------------------------------------------------------- clerk helpers
def test_looks_like_jwt() -> None:
    assert looks_like_jwt("aaa.bbb.ccc")
    assert not looks_like_jwt("oat_abc")
    assert not looks_like_jwt("a..c")


def test_parse_clerk_verify_response() -> None:
    subject, scopes, client_id, expires_at, aud = _parse_clerk_verify_response(
        {
            "subject": "user_1",
            "scopes": ["openid", "email"],
            "client_id": "https://client.example/cimd.json",
            "expiration": 1_900_000_000_000,
            "aud": RESOURCE,
        }
    )
    assert subject == "user_1"
    assert scopes == ["openid", "email"]
    assert client_id == "https://client.example/cimd.json"
    assert expires_at == 1_900_000_000
    assert aud == RESOURCE


# ------------------------------------------------------------------ AS self-check
def _metadata_client(payload: Any, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{ISSUER}/.well-known/oauth-authorization-server"
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_authorization_server_check_passes_for_compliant_metadata(
    key_pair: RSAKeyPair,
) -> None:
    settings = make_settings(key_pair)
    settings.public_key = None
    settings.jwks_url = f"{ISSUER}/.well-known/jwks.json"
    meta = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth/authorize",
        "token_endpoint": f"{ISSUER}/oauth/token",
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "scopes_supported": ["openid", "profile", "email"],
    }
    async with _metadata_client(meta) as client:
        assert await check_authorization_server(settings, http_client=client) == []


async def test_authorization_server_check_flags_dcr_missing_cimd_and_pkce(
    key_pair: RSAKeyPair,
) -> None:
    settings = make_settings(key_pair)
    meta = {
        "issuer": "https://someone-else.example.test",
        "registration_endpoint": f"{ISSUER}/oauth/register",
        "code_challenge_methods_supported": ["plain"],
        "response_types_supported": ["code"],
    }
    async with _metadata_client(meta) as client:
        problems = await check_authorization_server(settings, http_client=client)
    text = "\n".join(problems)
    assert "Issuer mismatch" in text
    assert "PKCE" in text
    assert "client_id_metadata_document_supported" in text
    assert "Dynamic Client Registration" in text


async def test_authorization_server_check_handles_http_errors(key_pair: RSAKeyPair) -> None:
    settings = make_settings(key_pair)
    async with _metadata_client({}, status=500) as client:
        problems = await check_authorization_server(settings, http_client=client)
    assert problems and "HTTP 500" in problems[0]
