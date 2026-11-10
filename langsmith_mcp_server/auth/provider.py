"""
Wires Clerk + FastMCP into a spec-compliant OAuth 2.1 resource server.

Responsibilities of the MCP server under the MCP authorization spec (2026-07-28):

- Serve RFC 9728 Protected Resource Metadata (``/.well-known/oauth-protected-resource/mcp``)
  listing Clerk as the authorization server. FastMCP's ``RemoteAuthProvider`` does this; a
  root-level fallback document is added for clients that probe the root first.
- Reject unauthenticated/invalid requests with ``401`` and a ``WWW-Authenticate: Bearer`` header
  carrying ``resource_metadata`` (and, as a hint, ``scope``), and tokens lacking required scopes
  with ``403 insufficient_scope``. FastMCP's ``RequireAuthMiddleware`` emits these; the
  ``scope`` hint is appended by :class:`ScopeChallengeMiddleware`.
- Validate tokens (signature, issuer, expiry, audience) — :class:`ClerkAccessTokenVerifier`.
- Never pass the client's token through to LangSmith (we use separate LangSmith credentials).

Everything the *authorization server* must do (PKCE S256, CIMD client registration, consent,
issuing audience-bound tokens) is Clerk's job; :func:`check_authorization_server` verifies at
startup that Clerk advertises those capabilities and logs actionable warnings otherwise.
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.dependencies import get_access_token
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from langsmith_mcp_server.auth.clerk import ClerkAccessTokenVerifier, ClerkUserCredentialResolver
from langsmith_mcp_server.auth.settings import OAuthSettings

logger = logging.getLogger(__name__)

# Module-level state for the active configuration (set by build_auth_provider; read by helpers).
_active_settings: Optional[OAuthSettings] = None
_credential_resolver: Optional[ClerkUserCredentialResolver] = None


def get_active_settings() -> Optional[OAuthSettings]:
    return _active_settings


class ClerkResourceServerAuthProvider(RemoteAuthProvider):
    """
    ``RemoteAuthProvider`` that serves the RFC 9728 document with exact, unmodified strings.

    The upstream implementation renders URLs through pydantic ``AnyHttpUrl``, which appends a
    trailing slash to bare origins (``https://clerk.example.com`` → ``https://clerk.example.com/``).
    RFC 8414 requires the advertised authorization server identifier to match the ``issuer``
    value byte-for-byte, so the document is rendered from :func:`build_protected_resource_metadata`.
    """

    def __init__(self, settings: OAuthSettings, token_verifier: ClerkAccessTokenVerifier):
        super().__init__(
            token_verifier=token_verifier,
            authorization_servers=[AnyHttpUrl(settings.issuer)],
            base_url=settings.server_url,
            scopes_supported=list(settings.scopes_supported),
            resource_name="LangSmith MCP Server",
        )
        self.settings = settings
        # Scope enforcement happens in RequireAuthMiddleware (403 insufficient_scope).
        self.required_scopes = list(settings.required_scopes)

    def get_routes(self, mcp_path: Optional[str] = None) -> List[Route]:
        self.set_mcp_path(mcp_path)
        path = urlparse(self.settings.resource_metadata_url).path
        return [
            Route(
                path,
                endpoint=protected_resource_metadata_endpoint(self.settings),
                methods=["GET", "OPTIONS"],
            )
        ]


def build_auth_provider(
    settings: OAuthSettings,
    http_client: Optional[httpx.AsyncClient] = None,
    credential_resolver: Optional[ClerkUserCredentialResolver] = None,
) -> Optional[RemoteAuthProvider]:
    """
    Create the FastMCP auth provider for ``settings`` (``None`` when OAuth is disabled).

    Also registers the per-user credential resolver used by
    :func:`resolve_oauth_user_credentials` when ``CLERK_SECRET_KEY`` is configured.
    """
    global _active_settings, _credential_resolver
    _active_settings = settings
    _credential_resolver = None
    if not settings.oauth_enabled:
        return None
    settings.validate()

    verifier = ClerkAccessTokenVerifier(settings, http_client=http_client)
    provider = ClerkResourceServerAuthProvider(settings, verifier)
    if settings.user_credentials_from_clerk:
        _credential_resolver = credential_resolver or ClerkUserCredentialResolver(
            settings, http_client=http_client
        )
    logger.info(
        "OAuth 2.1 resource server enabled: issuer=%s resource=%s metadata=%s",
        settings.issuer,
        settings.resource_url,
        settings.resource_metadata_url,
    )
    return provider


def build_protected_resource_metadata(settings: OAuthSettings) -> Dict[str, Any]:
    """RFC 9728 document for this server (used for the root-level fallback route)."""
    doc: Dict[str, Any] = {
        "resource": settings.resource_url,
        "authorization_servers": [settings.issuer],
        "scopes_supported": list(settings.scopes_supported),
        "bearer_methods_supported": ["header"],
        "resource_name": "LangSmith MCP Server",
    }
    if settings.resource_documentation:
        doc["resource_documentation"] = settings.resource_documentation
    return doc


def protected_resource_metadata_endpoint(
    settings: OAuthSettings,
) -> Callable[[Request], Awaitable[Response]]:
    """Starlette endpoint serving the protected resource metadata document."""

    async def endpoint(request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204)
        return JSONResponse(
            build_protected_resource_metadata(settings),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return endpoint


# --------------------------------------------------------------------- identity helpers
def get_oauth_subject() -> Optional[str]:
    """Return the authenticated user's ``sub`` for the current request, if any."""
    try:
        token = get_access_token()
    except Exception:
        return None
    if token is None:
        return None
    sub = token.claims.get("sub") if isinstance(token.claims, dict) else None
    return str(sub) if sub else None


async def resolve_oauth_user_credentials() -> Optional[Dict[str, str]]:
    """
    LangSmith credentials for the authenticated Clerk user (from ``private_metadata``).

    Returns ``None`` when OAuth is off, the resolver is not configured, the request is not
    authenticated, or the user has no LangSmith key stored.
    """
    if _credential_resolver is None:
        return None
    sub = get_oauth_subject()
    if not sub:
        return None
    return await _credential_resolver.resolve(sub)


# ------------------------------------------------------------------- scope challenge
class ScopeChallengeMiddleware:
    """
    Append ``scope="..."`` to ``WWW-Authenticate: Bearer`` challenges on 401 responses.

    The MCP spec lets a resource server advertise the scopes a client should request directly
    in the 401 challenge (clients prefer it over ``scopes_supported`` from resource metadata).
    """

    def __init__(self, app: ASGIApp, scopes: List[str]):
        self.app = app
        self.scope_value = " ".join(scopes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.scope_value:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and message.get("status") == 401:
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"www-authenticate":
                        text = value.decode("latin-1")
                        if text.lower().startswith("bearer") and "scope=" not in text.lower():
                            text = f'{text}, scope="{self.scope_value}"'
                        value = text.encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --------------------------------------------------------------- startup self-check
async def check_authorization_server(
    settings: OAuthSettings, http_client: Optional[httpx.AsyncClient] = None
) -> List[str]:
    """
    Fetch Clerk's OAuth authorization server metadata and report compliance problems.

    Returns a list of human-readable warnings (empty when everything looks right). Never raises.
    Checks: issuer match, PKCE ``S256`` advertised, CIMD advertised
    (``client_id_metadata_document_supported``), DCR disabled (no ``registration_endpoint``),
    ``code`` response type, and that ``jwks_uri`` is present when verifying JWTs.
    """
    warnings: List[str] = []
    url = settings.authorization_server_metadata_url
    try:
        if http_client is not None:
            response = await http_client.get(url)
        else:
            async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
                response = await client.get(url)
    except httpx.HTTPError as e:
        return [f"Could not fetch authorization server metadata from {url}: {e}"]
    if response.status_code != 200:
        return [f"Authorization server metadata at {url} returned HTTP {response.status_code}"]
    try:
        meta = response.json()
    except ValueError:
        return [f"Authorization server metadata at {url} is not valid JSON"]
    if not isinstance(meta, dict):
        return [f"Authorization server metadata at {url} is not a JSON object"]

    issuer = str(meta.get("issuer", "")).rstrip("/")
    if issuer != settings.issuer:
        warnings.append(
            f"Issuer mismatch: metadata says {issuer!r} but CLERK_ISSUER is {settings.issuer!r}. "
            "Tokens will be rejected; set CLERK_ISSUER to the metadata issuer."
        )
    methods = meta.get("code_challenge_methods_supported") or []
    if "S256" not in methods:
        warnings.append(
            "Authorization server does not advertise PKCE 'S256' in "
            "code_challenge_methods_supported; MCP requires PKCE S256."
        )
    if meta.get("client_id_metadata_document_supported") is not True:
        warnings.append(
            "Authorization server does not advertise client_id_metadata_document_supported=true. "
            "In the Clerk dashboard: OAuth applications → Settings → Client onboarding → enable "
            "'Publish CIMD support'. Without it, MCP clients cannot register via CIMD."
        )
    if meta.get("registration_endpoint"):
        warnings.append(
            "Authorization server still advertises a Dynamic Client Registration endpoint "
            "(registration_endpoint). DCR is deprecated by the 2026-07-28 MCP spec; disable "
            "'Publish DCR support' in Clerk if you want CIMD-only client registration."
        )
    response_types = meta.get("response_types_supported") or []
    if response_types and "code" not in response_types:
        warnings.append("Authorization server does not support response_type=code.")
    grant_types = meta.get("grant_types_supported")
    if grant_types and "authorization_code" not in grant_types:
        warnings.append("Authorization server does not support the authorization_code grant.")
    if not settings.public_key and not meta.get("jwks_uri"):
        warnings.append(
            "Authorization server metadata has no jwks_uri; JWT access tokens cannot be verified."
        )
    if settings.required_scopes:
        advertised = set(meta.get("scopes_supported") or [])
        missing = [s for s in settings.required_scopes if advertised and s not in advertised]
        if missing:
            warnings.append(
                f"MCP_OAUTH_REQUIRED_SCOPES {missing} are not in the authorization server's "
                "scopes_supported; every token would be rejected with insufficient_scope."
            )
    return warnings


def oauth_lifespan(
    settings: OAuthSettings,
) -> Callable[[Any], AbstractAsyncContextManager[Dict[str, Any]]]:
    """FastMCP lifespan that runs the authorization-server self-check on startup."""

    @asynccontextmanager
    async def lifespan(server: Any) -> AsyncIterator[Dict[str, Any]]:
        if settings.oauth_enabled and settings.startup_check:
            try:
                problems = await check_authorization_server(settings)
            except Exception as e:  # pragma: no cover - defensive
                problems = [f"Authorization server self-check crashed: {e}"]
            if problems:
                for problem in problems:
                    logger.warning("OAuth configuration: %s", problem)
            else:
                logger.info(
                    "OAuth self-check passed: %s advertises PKCE S256 and CIMD, DCR disabled",
                    settings.issuer,
                )
        yield {}

    return lifespan
