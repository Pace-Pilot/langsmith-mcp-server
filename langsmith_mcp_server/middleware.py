"""Per-request HTTP middleware (no authentication; OAuth is handled by FastMCP).

Authentication for the HTTP transport is OAuth 2.1 only, see
[auth.py](auth.py). FastMCP's ``RequireAuthMiddleware`` protects the ``/mcp`` route and
answers unauthenticated requests with ``401`` plus a ``WWW-Authenticate`` header that
points clients at the protected-resource metadata.

This middleware only does housekeeping that is independent of authentication:

1. Reject non-POST/DELETE requests to ``/mcp`` with ``405`` (e.g. opening the URL in a
   browser) instead of letting them reach the transport.
2. Derive a session id for monitoring from ``mcp-session-id``, ``x-session-id`` or
   ``x-request-id`` (falling back to a fresh UUID per request).
"""

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_405_METHOD_NOT_ALLOWED

from langsmith_mcp_server.monitoring import set_monitoring_session_id


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Reject unsupported methods on the MCP endpoint and set the monitoring session id."""

    # MCP endpoint only accepts POST (and DELETE for session teardown); GET returns 405
    _MCP_PATH = "/mcp"
    _MCP_ALLOWED_METHODS = frozenset({"POST", "DELETE"})

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/") or "/"
        if path == self._MCP_PATH and request.method not in self._MCP_ALLOWED_METHODS:
            return JSONResponse(
                status_code=HTTP_405_METHOD_NOT_ALLOWED,
                content={"error": "Method Not Allowed", "allow": sorted(self._MCP_ALLOWED_METHODS)},
                headers={"Allow": ", ".join(sorted(self._MCP_ALLOWED_METHODS))},
            )

        # Session/thread id for monitoring: from headers or per-request id
        session_id = (
            request.headers.get("mcp-session-id")
            or request.headers.get("x-session-id")
            or request.headers.get("x-request-id")
            or str(uuid4())
        )
        set_monitoring_session_id(session_id)
        return await call_next(request)
