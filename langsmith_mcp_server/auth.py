"""
OAuth 2.1 authentication for the HTTP transport.

The HTTP server is protected exclusively by OAuth 2.1 with Clerk as the identity
provider. API key authentication (the former ``LANGSMITH-API-KEY`` header) is not
supported.

What is enabled
---------------
- Authorization code grant with PKCE (S256). This is the only way to obtain a token.
  The MCP SDK rejects ``/authorize`` requests without ``code_challenge`` and rejects
  ``/token`` requests without ``code_verifier``. ``response_type`` must be ``code``.
- Client ID Metadata Documents (CIMD): MCP clients identify themselves with an HTTPS
  URL that serves their client metadata. No shared secrets are involved; clients use
  ``token_endpoint_auth_method`` ``none`` or ``private_key_jwt``.
- Refresh tokens, so an authorized client can renew its access without re-prompting
  the user. Refresh is a continuation of the code grant, not a separate way in.

What is disabled
----------------
- Dynamic Client Registration (RFC 7591). FastMCP's ``OAuthProxy`` always enables a
  local ``/register`` endpoint; :class:`ClerkCIMDOAuthProvider` removes the route, drops
  ``registration_endpoint`` from the discovery document and refuses ``register_client``.
- Passing the upstream Clerk client id directly as the MCP client id. Only CIMD client
  ids are accepted.

How it fits together
--------------------
FastMCP's ``ClerkProvider`` is an OAuth proxy: MCP clients talk OAuth to this server,
and this server completes the upstream flow with a single pre-registered Clerk OAuth
application (PKCE is forwarded upstream as well). Access tokens issued to MCP clients
are verified against Clerk's introspection endpoint on every request.

Configuration is read from environment variables, see :func:`build_auth_provider_from_env`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal, Mapping, Optional

from fastmcp.server.auth.providers.clerk import ClerkProvider
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Environment variable names (kept in one place so docs and code agree)
ENV_CLERK_DOMAIN = "CLERK_DOMAIN"
ENV_CLERK_CLIENT_ID = "CLERK_OAUTH_CLIENT_ID"
ENV_CLERK_CLIENT_SECRET = "CLERK_OAUTH_CLIENT_SECRET"
ENV_BASE_URL = "MCP_BASE_URL"
ENV_JWT_SIGNING_KEY = "MCP_OAUTH_JWT_SIGNING_KEY"
ENV_REQUIRED_SCOPES = "MCP_OAUTH_REQUIRED_SCOPES"
ENV_ALLOWED_CLIENT_REDIRECT_URIS = "MCP_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS"
ENV_REQUIRE_CONSENT = "MCP_OAUTH_REQUIRE_CONSENT"
ENV_REDIRECT_PATH = "MCP_OAUTH_REDIRECT_PATH"

DEFAULT_SCOPES = ["openid", "email", "profile"]

# Grant types the token endpoint accepts. "authorization_code" is the only way to
# obtain authorization; "refresh_token" renews it.
SUPPORTED_GRANT_TYPES = ["authorization_code", "refresh_token"]
SUPPORTED_RESPONSE_TYPES = ["code"]
SUPPORTED_CODE_CHALLENGE_METHODS = ["S256"]
# CIMD clients never hold a shared secret with this server.
SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS = ["none", "private_key_jwt"]


class OAuthConfigError(RuntimeError):
    """Raised when the OAuth environment configuration is missing or inconsistent."""


class ClerkCIMDOAuthProvider(ClerkProvider):
    """
    Clerk OAuth proxy restricted to CIMD clients and the PKCE authorization code flow.

    Differences from the stock ``ClerkProvider``:

    - Dynamic Client Registration is disabled (no ``/register`` route, no
      ``registration_endpoint`` in metadata, ``register_client`` raises).
    - Only URL-based (CIMD) client ids are resolved by ``get_client``. The stock proxy
      also accepts the upstream Clerk client id as a pseudo-client; that is turned off.
    - Discovery metadata advertises exactly what is supported: ``response_types``
      ``["code"]``, ``code_challenge_methods`` ``["S256"]`` and token endpoint auth
      methods ``["none", "private_key_jwt"]``.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs["enable_cimd"] = True
        super().__init__(**kwargs)
        # OAuthProxy forces DCR on because it implements it locally. We do not want it:
        # clients must bring a CIMD client id. Turning the option off here removes the
        # /register route and the registration_endpoint from the SDK-built metadata.
        if self.client_registration_options is None:
            self.client_registration_options = ClientRegistrationOptions()
        self.client_registration_options.enabled = False
        logger.info(
            "OAuth configured: Clerk upstream, CIMD clients only, authorization code + PKCE (S256), "
            "dynamic client registration disabled"
        )

    # ------------------------------------------------------------------
    # Client resolution
    # ------------------------------------------------------------------

    def is_cimd_client_id(self, client_id: str) -> bool:
        """Return True if ``client_id`` is an HTTPS URL usable as a CIMD client id."""
        manager = self._cimd_manager
        return manager is not None and manager.is_cimd_client_id(client_id)

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        """Resolve a client. Only CIMD (URL) client ids are accepted."""
        if not self.is_cimd_client_id(client_id):
            logger.debug(
                "Rejecting non-CIMD client_id %r (DCR and raw client ids disabled)", client_id
            )
            return None
        return await super().get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Dynamic Client Registration is disabled."""
        raise ValueError(
            "Dynamic Client Registration is disabled on this server. "
            "Use a Client ID Metadata Document (HTTPS URL) as client_id."
        )

    # ------------------------------------------------------------------
    # Routes & discovery metadata
    # ------------------------------------------------------------------

    def build_authorization_server_metadata(self) -> OAuthMetadata:
        """Build the RFC 8414 metadata document advertising only the supported features."""
        assert self.base_url is not None
        metadata = build_metadata(
            self.base_url,
            self.service_documentation_url,
            self.client_registration_options or ClientRegistrationOptions(enabled=False),
            self.revocation_options or RevocationOptions(),
        )
        metadata.registration_endpoint = None
        metadata.client_id_metadata_document_supported = True
        metadata.response_types_supported = list(SUPPORTED_RESPONSE_TYPES)
        metadata.grant_types_supported = list(SUPPORTED_GRANT_TYPES)
        metadata.code_challenge_methods_supported = list(SUPPORTED_CODE_CHALLENGE_METHODS)
        metadata.token_endpoint_auth_methods_supported = list(SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS)
        return metadata

    def get_routes(self, mcp_path: Optional[str] = None) -> list[Route]:
        """Return the OAuth routes without ``/register`` and with corrected metadata."""
        routes = super().get_routes(mcp_path)
        metadata_handler = MetadataHandler(self.build_authorization_server_metadata())
        result: list[Route] = []
        for route in routes:
            if not isinstance(route, Route):
                result.append(route)
                continue
            if route.path == "/register":
                # Belt and braces: client_registration_options.enabled=False already
                # prevents the SDK from creating this route.
                continue
            if route.path.startswith("/.well-known/oauth-authorization-server"):
                result.append(
                    Route(
                        path=route.path,
                        endpoint=cors_middleware(metadata_handler.handle, ["GET", "OPTIONS"]),
                        methods=route.methods or ["GET", "OPTIONS"],
                        name=route.name,
                        include_in_schema=route.include_in_schema,
                    )
                )
                continue
            result.append(route)
        return result


# ----------------------------------------------------------------------
# Environment configuration
# ----------------------------------------------------------------------


def _split_list(value: str) -> list[str]:
    """Split a comma- or whitespace-separated environment value into a list."""
    return [item for item in value.replace(",", " ").split() if item]


def _parse_consent(value: str) -> bool | Literal["remember", "external"]:
    normalized = value.strip().lower()
    if normalized in ("", "true", "1", "yes"):
        return True
    if normalized in ("false", "0", "no"):
        return False
    if normalized in ("remember", "external"):
        return normalized  # type: ignore[return-value]
    raise OAuthConfigError(
        f"{ENV_REQUIRE_CONSENT} must be one of true, false, remember, external (got {value!r})"
    )


def is_oauth_configured(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return True if any of the OAuth environment variables is set."""
    source = os.environ if env is None else env
    return any(
        source.get(name, "").strip()
        for name in (ENV_CLERK_DOMAIN, ENV_CLERK_CLIENT_ID, ENV_BASE_URL)
    )


def build_auth_provider_from_env(
    env: Optional[Mapping[str, str]] = None,
    **provider_overrides: Any,
) -> Optional[ClerkCIMDOAuthProvider]:
    """
    Build the OAuth provider from environment variables.

    Required:
        CLERK_DOMAIN              Clerk instance (Frontend API) domain, e.g.
                                  ``verb-noun-12.clerk.accounts.dev`` or ``clerk.example.com``.
        CLERK_OAUTH_CLIENT_ID     Client ID of the OAuth application created in Clerk.
        MCP_BASE_URL              Public URL of this server, e.g. ``https://mcp.example.com``.
        CLERK_OAUTH_CLIENT_SECRET Client secret of the Clerk OAuth application. May be
                                  omitted for a public (PKCE-only) Clerk app, in which case
                                  MCP_OAUTH_JWT_SIGNING_KEY is required.

    Optional:
        MCP_OAUTH_JWT_SIGNING_KEY            Secret used to sign the tokens this server issues.
                                             Defaults to a key derived from the client secret.
        MCP_OAUTH_REQUIRED_SCOPES            Space/comma separated Clerk scopes
                                             (default: ``openid email profile``).
        MCP_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS  Comma separated redirect URI patterns MCP
                                             clients may use (default: any URI listed in the
                                             client's CIMD document).
        MCP_OAUTH_REQUIRE_CONSENT            ``true`` (default), ``false``, ``remember`` or
                                             ``external``.
        MCP_OAUTH_REDIRECT_PATH              Callback path registered in Clerk
                                             (default: ``/auth/callback``).

    Returns:
        The provider, or ``None`` when none of the OAuth variables is set.

    Raises:
        OAuthConfigError: when the configuration is present but incomplete.
    """
    source = os.environ if env is None else env

    def get(name: str) -> str:
        return source.get(name, "").strip()

    if not is_oauth_configured(source):
        return None

    domain = get(ENV_CLERK_DOMAIN)
    client_id = get(ENV_CLERK_CLIENT_ID)
    base_url = get(ENV_BASE_URL)
    missing = [
        name
        for name, value in (
            (ENV_CLERK_DOMAIN, domain),
            (ENV_CLERK_CLIENT_ID, client_id),
            (ENV_BASE_URL, base_url),
        )
        if not value
    ]
    if missing:
        raise OAuthConfigError(
            "Incomplete OAuth configuration; missing environment variables: " + ", ".join(missing)
        )

    client_secret = get(ENV_CLERK_CLIENT_SECRET) or None
    jwt_signing_key = get(ENV_JWT_SIGNING_KEY) or None
    if not client_secret and not jwt_signing_key:
        raise OAuthConfigError(
            f"Set {ENV_CLERK_CLIENT_SECRET} (confidential Clerk OAuth app) or "
            f"{ENV_JWT_SIGNING_KEY} (public Clerk OAuth app)."
        )

    scopes_raw = get(ENV_REQUIRED_SCOPES)
    required_scopes = _split_list(scopes_raw) if scopes_raw else list(DEFAULT_SCOPES)

    allowed_redirects_raw = get(ENV_ALLOWED_CLIENT_REDIRECT_URIS)
    allowed_client_redirect_uris = (
        _split_list(allowed_redirects_raw) if allowed_redirects_raw else None
    )

    kwargs: dict[str, Any] = {
        "domain": domain,
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": base_url,
        "jwt_signing_key": jwt_signing_key,
        "required_scopes": required_scopes,
        "allowed_client_redirect_uris": allowed_client_redirect_uris,
        "require_authorization_consent": _parse_consent(get(ENV_REQUIRE_CONSENT)),
    }
    redirect_path = get(ENV_REDIRECT_PATH)
    if redirect_path:
        kwargs["redirect_path"] = redirect_path
    kwargs.update(provider_overrides)

    return ClerkCIMDOAuthProvider(**kwargs)
