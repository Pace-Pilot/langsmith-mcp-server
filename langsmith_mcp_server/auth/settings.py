"""Environment-driven configuration for HTTP authentication."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
from urllib.parse import urlparse

DEFAULT_MCP_PATH = "/mcp"
DEFAULT_SCOPES_SUPPORTED = ["openid", "profile", "email"]
DEFAULT_CLERK_API_URL = "https://api.clerk.com"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class AuthMode(str, Enum):
    """How HTTP requests to ``/mcp`` are authenticated."""

    OAUTH = "oauth"  # OAuth 2.1 bearer tokens issued by Clerk (spec-compliant default)
    API_KEY = "api_key"  # Legacy: LANGSMITH-API-KEY header (no OAuth)


def _env(name: str, *aliases: str, default: str = "") -> str:
    for key in (name, *aliases):
        value = os.environ.get(key)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _split_list(raw: str) -> List[str]:
    """Split a comma- or space-separated env value into a list (order preserved, deduped)."""
    items: List[str] = []
    for chunk in raw.replace(",", " ").split():
        if chunk and chunk not in items:
            items.append(chunk)
    return items


def _normalize_url(url: str) -> str:
    return url.rstrip("/")


@dataclass
class OAuthSettings:
    """
    Resolved authentication settings.

    Environment variables (HTTP transport only; stdio is unaffected):

    - ``MCP_AUTH_MODE``: ``oauth`` (default when ``CLERK_ISSUER`` is set) or ``api_key``.
    - ``CLERK_ISSUER`` (alias ``CLERK_FRONTEND_API_URL``): Clerk Frontend API URL, e.g.
      ``https://clerk.example.com`` or ``https://xyz.clerk.accounts.dev``. This is the OAuth
      issuer and the only entry in the protected resource metadata's ``authorization_servers``.
    - ``MCP_SERVER_URL``: public base URL of this server (``https://mcp.example.com``). The
      canonical resource identifier is ``MCP_SERVER_URL + /mcp``; tokens must carry it in ``aud``.
    - ``MCP_OAUTH_AUDIENCE``: optional override of the accepted ``aud`` values (comma-separated).
    - ``MCP_OAUTH_SCOPES_SUPPORTED``: scopes advertised in resource metadata and in the 401
      challenge (default ``openid profile email``).
    - ``MCP_OAUTH_REQUIRED_SCOPES``: scopes every token must carry (default: none).
    - ``MCP_OAUTH_RESOURCE_DOCUMENTATION``: optional docs URL for resource metadata.
    - ``MCP_OAUTH_JWKS_URL``: JWKS URL (default ``CLERK_ISSUER/.well-known/jwks.json``).
    - ``MCP_OAUTH_PUBLIC_KEY``: PEM public key used instead of JWKS (offline/testing).
    - ``CLERK_SECRET_KEY``: optional Clerk Backend API key. Enables verification of opaque
      (``oat_``) access tokens and per-user LangSmith credential lookup from Clerk user
      ``private_metadata``.
    - ``CLERK_API_URL``: Clerk Backend API base (default ``https://api.clerk.com``).
    - ``MCP_OAUTH_ALLOW_OPAQUE_TOKENS``: accept opaque tokens via Clerk's verify endpoint
      (default: true when ``CLERK_SECRET_KEY`` is set).
    - ``MCP_OAUTH_USER_CREDENTIALS_FROM_CLERK``: resolve LangSmith credentials from the
      authenticated user's Clerk ``private_metadata`` (default: true when secret key set).
    - ``MCP_OAUTH_STARTUP_CHECK``: at startup, fetch Clerk's authorization server metadata and
      warn when PKCE S256 / CIMD are not advertised or DCR is still enabled (default: true).
    """

    mode: AuthMode = AuthMode.API_KEY
    issuer: str = ""
    server_url: str = ""
    mcp_path: str = DEFAULT_MCP_PATH
    audiences: List[str] = field(default_factory=list)
    scopes_supported: List[str] = field(default_factory=lambda: list(DEFAULT_SCOPES_SUPPORTED))
    required_scopes: List[str] = field(default_factory=list)
    resource_documentation: Optional[str] = None
    jwks_url: str = ""
    public_key: Optional[str] = None
    clerk_secret_key: Optional[str] = None
    clerk_api_url: str = DEFAULT_CLERK_API_URL
    allow_opaque_tokens: bool = False
    user_credentials_from_clerk: bool = False
    startup_check: bool = True
    http_timeout_seconds: float = 10.0

    @property
    def oauth_enabled(self) -> bool:
        return self.mode == AuthMode.OAUTH

    @property
    def resource_url(self) -> str:
        """Canonical resource identifier (RFC 8707 §2): scheme + host + MCP path, no trailing slash."""
        return f"{_normalize_url(self.server_url)}{self.mcp_path}"

    @property
    def resource_metadata_url(self) -> str:
        """RFC 9728 §3.1 path-based metadata URL for the resource."""
        return (
            f"{_normalize_url(self.server_url)}/.well-known/oauth-protected-resource{self.mcp_path}"
        )

    @property
    def authorization_server_metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/oauth-authorization-server"

    def validate(self) -> None:
        """Raise ``ValueError`` for configurations that cannot be spec-compliant."""
        if not self.oauth_enabled:
            return
        if not self.issuer:
            raise ValueError(
                "MCP_AUTH_MODE=oauth requires CLERK_ISSUER (your Clerk Frontend API URL, e.g. "
                "https://clerk.example.com or https://<slug>.clerk.accounts.dev)."
            )
        if not self.server_url:
            raise ValueError(
                "MCP_AUTH_MODE=oauth requires MCP_SERVER_URL (the public base URL of this server, "
                "e.g. https://mcp.example.com). It is used as the token audience and in "
                "protected resource metadata."
            )
        for label, url in (("CLERK_ISSUER", self.issuer), ("MCP_SERVER_URL", self.server_url)):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"{label} must be an absolute http(s) URL, got {url!r}")
            if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
                raise ValueError(
                    f"{label} must use https (OAuth 2.1 / MCP require TLS for all "
                    f"authorization endpoints); got {url!r}"
                )
            if parsed.query or parsed.fragment:
                raise ValueError(f"{label} must not contain a query string or fragment: {url!r}")
        if not self.jwks_url and not self.public_key:
            raise ValueError("Either MCP_OAUTH_JWKS_URL or MCP_OAUTH_PUBLIC_KEY must be set")
        if self.allow_opaque_tokens and not self.clerk_secret_key:
            raise ValueError("MCP_OAUTH_ALLOW_OPAQUE_TOKENS requires CLERK_SECRET_KEY")
        if self.user_credentials_from_clerk and not self.clerk_secret_key:
            raise ValueError("MCP_OAUTH_USER_CREDENTIALS_FROM_CLERK requires CLERK_SECRET_KEY")


def load_auth_settings() -> OAuthSettings:
    """Build :class:`OAuthSettings` from the process environment and validate them."""
    issuer = _normalize_url(_env("CLERK_ISSUER", "CLERK_FRONTEND_API_URL"))
    server_url = _normalize_url(_env("MCP_SERVER_URL", "MCP_PUBLIC_URL"))

    raw_mode = _env("MCP_AUTH_MODE").lower()
    if raw_mode:
        try:
            mode = AuthMode(raw_mode)
        except ValueError as e:
            raise ValueError(
                f"Invalid MCP_AUTH_MODE={raw_mode!r}; expected one of {[m.value for m in AuthMode]}"
            ) from e
    else:
        mode = AuthMode.OAUTH if issuer else AuthMode.API_KEY

    mcp_path = "/" + _env("MCP_PATH", default=DEFAULT_MCP_PATH).strip("/")
    settings = OAuthSettings(mode=mode, issuer=issuer, server_url=server_url, mcp_path=mcp_path)

    if settings.oauth_enabled:
        resource = settings.resource_url
        audiences = _split_list(_env("MCP_OAUTH_AUDIENCE"))
        if not audiences and server_url:
            # Accept the canonical form and the trailing-slash form: both identify this server.
            audiences = [resource, resource + "/"]
        settings.audiences = audiences

        scopes_supported = _split_list(_env("MCP_OAUTH_SCOPES_SUPPORTED"))
        if scopes_supported:
            settings.scopes_supported = scopes_supported
        settings.required_scopes = _split_list(_env("MCP_OAUTH_REQUIRED_SCOPES"))
        settings.resource_documentation = _env("MCP_OAUTH_RESOURCE_DOCUMENTATION") or None

        public_key = _env("MCP_OAUTH_PUBLIC_KEY") or None
        settings.public_key = public_key.replace("\\n", "\n") if public_key else None
        settings.jwks_url = (
            ""
            if settings.public_key
            else _env("MCP_OAUTH_JWKS_URL") or f"{issuer}/.well-known/jwks.json"
        )

        secret = _env("CLERK_SECRET_KEY") or None
        settings.clerk_secret_key = secret
        settings.clerk_api_url = _normalize_url(
            _env("CLERK_API_URL", default=DEFAULT_CLERK_API_URL)
        )
        settings.allow_opaque_tokens = _env_bool("MCP_OAUTH_ALLOW_OPAQUE_TOKENS", bool(secret))
        settings.user_credentials_from_clerk = _env_bool(
            "MCP_OAUTH_USER_CREDENTIALS_FROM_CLERK", bool(secret)
        )
        settings.startup_check = _env_bool("MCP_OAUTH_STARTUP_CHECK", True)

    settings.validate()
    return settings
