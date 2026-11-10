"""
HTTP middleware that carries LangSmith credentials from the request into tool execution.

Two authentication modes (see :mod:`langsmith_mcp_server.auth.settings`):

- ``oauth`` (default when ``CLERK_ISSUER`` is set): bearer tokens are validated by FastMCP's
  auth stack (RFC 9728 metadata, 401/403 challenges, Clerk JWT verification). This middleware
  then only extracts the *optional* ``LANGSMITH-*`` headers; LangSmith credentials may also come
  from the Clerk user's private metadata or the server environment (see ``common/helpers.py``).
- ``api_key`` (legacy): the ``LANGSMITH-API-KEY`` header is required and acts as the credential.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Optional
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_405_METHOD_NOT_ALLOWED
from starlette.types import ASGIApp

from langsmith_mcp_server.auth.settings import OAuthSettings
from langsmith_mcp_server.monitoring import set_monitoring_session_id

logger = logging.getLogger(__name__)

# Context variables to store LangSmith config for current request
# These are used to pass config from middleware to FastMCP's session state
api_key_context: ContextVar[str] = ContextVar("api_key", default="")
workspace_id_context: ContextVar[str] = ContextVar("workspace_id", default="")
endpoint_context: ContextVar[str] = ContextVar("endpoint", default="")


def get_api_key() -> str:
    """Get the API key from the current request context."""
    return api_key_context.get("")


class LangSmithCredentialsMiddleware(BaseHTTPMiddleware):
    """
    Extracts ``LANGSMITH-API-KEY`` / ``LANGSMITH-WORKSPACE-ID`` / ``LANGSMITH-ENDPOINT`` headers.

    In ``api_key`` mode the API key header is mandatory (401 otherwise). In ``oauth`` mode it is
    optional and authentication is enforced by the OAuth bearer-token stack instead.
    """

    # MCP endpoint only accepts POST (and DELETE for session teardown); GET returns 405
    _MCP_ALLOWED_METHODS = frozenset({"POST", "DELETE"})
    _PUBLIC_PATHS = frozenset({"/health", "/.well-known/oauth-authorization-server"})

    def __init__(self, app: ASGIApp, settings: Optional[OAuthSettings] = None):
        super().__init__(app)
        self.settings = settings or OAuthSettings()
        self._mcp_path = self.settings.mcp_path

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/") or "/"

        # Public endpoints: health check and OAuth discovery documents (RFC 9728 / RFC 8414)
        if path in self._PUBLIC_PATHS or path.startswith("/.well-known/"):
            return await call_next(request)

        # GET (e.g. opening /mcp in a browser) → 405 Method Not Allowed (standard for MCP HTTP)
        if path == self._mcp_path and request.method not in self._MCP_ALLOWED_METHODS:
            return JSONResponse(
                status_code=HTTP_405_METHOD_NOT_ALLOWED,
                content={"error": "Method Not Allowed", "allow": list(self._MCP_ALLOWED_METHODS)},
                headers={"Allow": ", ".join(sorted(self._MCP_ALLOWED_METHODS))},
            )

        api_key = request.headers.get("LANGSMITH-API-KEY", "")
        if not api_key and not self.settings.oauth_enabled:
            # Legacy mode: the header *is* the credential.
            return JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={"error": "Missing LANGSMITH-API-KEY header"},
            )

        # Get optional headers
        workspace_id = request.headers.get("LANGSMITH-WORKSPACE-ID", "")
        endpoint = request.headers.get("LANGSMITH-ENDPOINT", "")

        # Store in request state and context variables
        # FastMCP will handle storing this in session state via the Context
        request.state.api_key = api_key
        request.state.workspace_id = workspace_id
        request.state.endpoint = endpoint

        api_key_context.set(api_key)
        workspace_id_context.set(workspace_id)
        endpoint_context.set(endpoint)

        # Session/thread id for monitoring: from headers or per-request id
        session_id = (
            request.headers.get("mcp-session-id")
            or request.headers.get("x-session-id")
            or request.headers.get("x-request-id")
            or str(uuid4())
        )
        set_monitoring_session_id(session_id)

        try:
            return await call_next(request)
        finally:
            # Clear context after request
            api_key_context.set("")
            workspace_id_context.set("")
            endpoint_context.set("")


# Backwards-compatible name
APIKeyMiddleware = LangSmithCredentialsMiddleware
