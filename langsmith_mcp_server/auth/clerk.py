"""
Clerk-specific token verification and (optional) per-user credential lookup.

Clerk is the OAuth 2.1 authorization server; this module only *validates* what Clerk
issued. Two token formats are handled:

1. **JWT access tokens** (Clerk's default for new instances, RFC 9068 ``at+jwt``): verified
   locally against Clerk's JWKS. Signature, ``iss``, ``exp`` and ``aud`` are all checked.
   ``aud`` is populated by Clerk from the RFC 8707 ``resource`` parameter the MCP client sends,
   once the instance's *audience claim* setting is enabled in the Clerk dashboard.
2. **Opaque access tokens** (``oat_...``, legacy OAuth applications): verified remotely through
   Clerk's Backend API, which requires ``CLERK_SECRET_KEY``. Disabled unless configured.

Both paths fail closed on a missing or mismatching audience, as the MCP spec requires the
resource server to reject tokens that were not issued for it.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier

from langsmith_mcp_server.auth.settings import OAuthSettings

logger = logging.getLogger(__name__)

_USER_AGENT = "langsmith-mcp-server"


def looks_like_jwt(token: str) -> bool:
    """Cheap structural check: three base64url segments separated by dots."""
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _peek_jwt_claims(token: str) -> Dict[str, Any]:
    """Decode the JWT payload WITHOUT verifying it. Only used for diagnostics/logging."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _audience_matches(aud: Any, accepted: List[str]) -> bool:
    if not accepted:
        return True
    if isinstance(aud, str):
        return aud in accepted
    if isinstance(aud, (list, tuple)):
        return any(isinstance(a, str) and a in accepted for a in aud)
    return False


def _coerce_epoch_seconds(value: Any) -> Optional[int]:
    """Accept seconds or milliseconds since epoch (Clerk's Backend API often uses ms)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1e12:  # milliseconds
        number /= 1000.0
    return int(number)


class ClerkAccessTokenVerifier(JWTVerifier):
    """
    Verifies Clerk-issued OAuth access tokens for this resource server.

    Extends FastMCP's :class:`JWTVerifier` (JWKS-backed, RS256) with:

    - actionable logging when a Clerk JWT has no ``aud`` claim (dashboard setting off),
    - optional verification of opaque ``oat_`` tokens via Clerk's Backend API.
    """

    def __init__(self, settings: OAuthSettings, http_client: Optional[httpx.AsyncClient] = None):
        if settings.public_key:
            key_kwargs: Dict[str, Any] = {"public_key": settings.public_key}
        else:
            key_kwargs = {"jwks_uri": settings.jwks_url}
        # NOTE: required scopes are deliberately NOT enforced here. A valid token that lacks a
        # scope must produce ``403 insufficient_scope`` (enforced by FastMCP's
        # RequireAuthMiddleware), not ``401 invalid_token``.
        super().__init__(
            issuer=settings.issuer,
            audience=list(settings.audiences) or None,
            algorithm="RS256",
            required_scopes=None,
            base_url=settings.server_url or None,
            **key_kwargs,
        )
        self.settings = settings
        self._http_client = http_client

    # ------------------------------------------------------------------ JWT path
    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        access_token = await super().load_access_token(token)
        if access_token is not None:
            return access_token
        # Diagnostics only: explain the most common Clerk misconfiguration.
        claims = _peek_jwt_claims(token)
        if claims and "aud" not in claims and self.audience:
            logger.warning(
                "Rejected a Clerk access token without an 'aud' claim. Enable the audience "
                "(aud) claim for OAuth access tokens in the Clerk dashboard so tokens carry the "
                "RFC 8707 resource indicator (%s).",
                self.settings.resource_url,
            )
        elif claims and claims.get("iss") and claims.get("iss") != self.issuer:
            logger.warning(
                "Rejected token from unexpected issuer %r (expected %r). Check CLERK_ISSUER.",
                claims.get("iss"),
                self.issuer,
            )
        return None

    # --------------------------------------------------------------- dispatcher
    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if looks_like_jwt(token):
            return await self.load_access_token(token)
        if self.settings.allow_opaque_tokens and self.settings.clerk_secret_key:
            return await self._verify_opaque_token(token)
        logger.info(
            "Rejected non-JWT bearer token: opaque Clerk tokens are not enabled "
            "(set CLERK_SECRET_KEY and MCP_OAUTH_ALLOW_OPAQUE_TOKENS=true, or switch the Clerk "
            "OAuth application to JWT access tokens)."
        )
        return None

    # ----------------------------------------------------------- opaque path
    async def _verify_opaque_token(self, token: str) -> Optional[AccessToken]:
        """
        Verify an opaque Clerk OAuth access token (``oat_...``) via the Backend API.

        ``POST {CLERK_API_URL}/v1/oauth_applications/access_tokens/verify`` with the instance
        secret key. Response field names are read defensively; a token is only accepted when the
        response identifies a subject and an audience that includes this resource.
        """
        url = f"{self.settings.clerk_api_url}/v1/oauth_applications/access_tokens/verify"
        headers = {
            "Authorization": f"Bearer {self.settings.clerk_secret_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        try:
            client, owned = self._get_http_client()
            try:
                response = await client.post(url, json={"access_token": token}, headers=headers)
            finally:
                if owned:
                    await client.aclose()
        except httpx.HTTPError as e:
            logger.warning("Clerk opaque token verification failed (network): %s", e)
            return None

        if response.status_code != 200:
            logger.info("Clerk rejected opaque access token (HTTP %s)", response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning("Clerk verify endpoint returned non-JSON body")
            return None
        if not isinstance(data, dict):
            return None

        subject, scopes, client_id, expires_at, aud = _parse_clerk_verify_response(data)
        if not subject:
            logger.warning(
                "Clerk verify response had no subject; keys were %s", sorted(data.keys())
            )
            return None
        if expires_at is not None and expires_at < int(time.time()):
            logger.info("Opaque Clerk token expired")
            return None
        if aud is None:
            logger.warning(
                "Opaque Clerk token has no audience information; rejecting (MCP servers must "
                "only accept tokens issued for them). Prefer JWT access tokens with the aud "
                "claim enabled in Clerk."
            )
            return None
        if not _audience_matches(aud, list(self.settings.audiences)):
            logger.warning(
                "Opaque Clerk token audience mismatch (got %r, expected one of %r)",
                aud,
                self.settings.audiences,
            )
            return None
        claims: Dict[str, Any] = {
            "sub": subject,
            "iss": self.settings.issuer,
            "aud": aud,
            "scope": " ".join(scopes),
            "token_format": "opaque",
            "clerk_verify_response": data,
        }
        return AccessToken(
            token=token,
            client_id=str(client_id or subject),
            scopes=scopes,
            expires_at=expires_at,
            claims=claims,
        )

    def _get_http_client(self) -> Tuple[httpx.AsyncClient, bool]:
        if self._http_client is not None:
            return self._http_client, False
        return httpx.AsyncClient(timeout=self.settings.http_timeout_seconds), True


def _parse_clerk_verify_response(
    data: Dict[str, Any],
) -> Tuple[Optional[str], List[str], Optional[str], Optional[int], Any]:
    """Extract (subject, scopes, client_id, expires_at, aud) from a Clerk verify response."""
    subject = None
    for key in ("subject", "sub", "user_id"):
        if isinstance(data.get(key), str) and data[key]:
            subject = data[key]
            break

    raw_scopes = data.get("scopes", data.get("scope"))
    scopes: List[str] = []
    if isinstance(raw_scopes, str):
        scopes = raw_scopes.split()
    elif isinstance(raw_scopes, list):
        scopes = [str(s) for s in raw_scopes]

    client_id = data.get("client_id") or data.get("azp")
    client_id = str(client_id) if client_id else None

    expires_at = None
    for key in ("expiration", "exp", "expires_at", "expire_at"):
        if data.get(key) is not None:
            expires_at = _coerce_epoch_seconds(data[key])
            break

    aud = None
    for key in ("aud", "audience", "resource", "resources"):
        if data.get(key):
            aud = data[key]
            break
    return subject, scopes, client_id, expires_at, aud


# --------------------------------------------------------------------------- credentials
class ClerkUserCredentialResolver:
    """
    Optional: map an authenticated Clerk user to LangSmith credentials.

    Reads the user's ``private_metadata`` from Clerk's Backend API and returns
    ``langsmith_api_key`` (required), ``langsmith_workspace_id`` and ``langsmith_endpoint``
    (optional). Results are cached in memory per user for ``cache_ttl_seconds``.

    Store the key on the user with the Clerk dashboard or Backend API, e.g.::

        PATCH /v1/users/{user_id}/metadata
        {"private_metadata": {"langsmith_api_key": "lsv2_pt_..."}}
    """

    def __init__(
        self,
        settings: OAuthSettings,
        http_client: Optional[httpx.AsyncClient] = None,
        cache_ttl_seconds: int = 300,
    ):
        self.settings = settings
        self._http_client = http_client
        self._cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Tuple[float, Optional[Dict[str, str]]]] = {}

    async def resolve(self, user_id: str) -> Optional[Dict[str, str]]:
        if not user_id or not self.settings.clerk_secret_key:
            return None
        now = time.monotonic()
        cached = self._cache.get(user_id)
        if cached and cached[0] > now:
            return cached[1]

        creds = await self._fetch(user_id)
        self._cache[user_id] = (now + self._cache_ttl, creds)
        return creds

    async def _fetch(self, user_id: str) -> Optional[Dict[str, str]]:
        url = f"{self.settings.clerk_api_url}/v1/users/{user_id}"
        headers = {
            "Authorization": f"Bearer {self.settings.clerk_secret_key}",
            "User-Agent": _USER_AGENT,
        }
        if self._http_client is not None:
            client, owned = self._http_client, False
        else:
            client, owned = httpx.AsyncClient(timeout=self.settings.http_timeout_seconds), True
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("Clerk user lookup failed for %s: %s", user_id, e)
            return None
        finally:
            if owned:
                await client.aclose()
        if response.status_code != 200:
            logger.info("Clerk user lookup for %s returned HTTP %s", user_id, response.status_code)
            return None
        try:
            user = response.json()
        except ValueError:
            return None
        metadata = user.get("private_metadata") if isinstance(user, dict) else None
        if not isinstance(metadata, dict):
            return None
        api_key = metadata.get("langsmith_api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        creds = {"api_key": api_key.strip()}
        for src, dst in (
            ("langsmith_workspace_id", "workspace_id"),
            ("langsmith_endpoint", "endpoint"),
        ):
            value = metadata.get(src)
            if isinstance(value, str) and value.strip():
                creds[dst] = value.strip()
        return creds
