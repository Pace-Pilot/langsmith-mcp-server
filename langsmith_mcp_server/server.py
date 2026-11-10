"""
MCP server for LangSmith SDK integration.
This server exposes methods to interact with LangSmith's observability platform:
- get_thread_history: Fetch conversation history for a specific thread
- get_prompts: Fetch prompts from LangSmith with optional filtering
- pull_prompt: Pull a specific prompt by its name

Transports:
- stdio (``main()``): trust boundary is the process environment.
- HTTP (``app``): protected by OAuth 2.1 (Clerk, CIMD clients, authorization code + PKCE).
  ``app`` is built lazily on first access so that importing this module for stdio use
  does not require the OAuth environment variables. See ``create_http_app``.
"""

import logging
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from langsmith_mcp_server.auth import OAuthConfigError, build_auth_provider_from_env
from langsmith_mcp_server.middleware import RequestContextMiddleware
from langsmith_mcp_server.services import (
    register_prompts,
    register_resources,
    register_tools,
)
from langsmith_mcp_server.streamable_http_patch import (
    apply_streamable_http_client_disconnect_patch,
)

logger = logging.getLogger(__name__)

# Load .env from the working directory (OAuth, LangSmith and monitoring settings)
load_dotenv()

# Create MCP server
mcp = FastMCP("LangSmith API MCP Server")
logger.info("MCP server instance created: LangSmith API MCP Server")

# Register all tools with the server using simplified registration modules
# Note: Tools use the server's LangSmith credentials from the environment
register_tools(mcp)
register_prompts(mcp)
register_resources(mcp)
logger.info("Tools, prompts, and resources registered")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    logger.debug(
        "Health check requested from %s", request.client.host if request.client else "unknown"
    )
    return PlainTextResponse("LangSmith MCP server is running")


# Middleware for the HTTP app. OAuth enforcement is added by FastMCP itself
# (RequireAuthMiddleware on the /mcp route) when ``mcp.auth`` is set.
middleware = [
    Middleware(RequestContextMiddleware),
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id", "www-authenticate"],
    ),
]


def create_http_app() -> Starlette:
    """
    Build the ASGI application for the HTTP (streamable) transport.

    Fails closed: if the OAuth environment is not configured the server refuses to
    start rather than exposing the MCP endpoint without authentication.

    Raises:
        OAuthConfigError: If the Clerk OAuth configuration is missing or incomplete.
    """
    auth = build_auth_provider_from_env()
    if auth is None:
        raise OAuthConfigError(
            "The HTTP transport requires OAuth. Set CLERK_DOMAIN, CLERK_OAUTH_CLIENT_ID, "
            "CLERK_OAUTH_CLIENT_SECRET (or MCP_OAUTH_JWT_SIGNING_KEY) and MCP_BASE_URL. "
            "API key authentication is not supported."
        )
    mcp.auth = auth

    http_app = mcp.http_app(middleware=middleware)
    logger.info(
        "ASGI app created with OAuth (Clerk/CIMD/PKCE), request-context and CORS middleware"
    )

    # Handle client disconnect gracefully (avoids ClosedResourceError/BrokenResourceError
    # when client or proxy closes the connection before the server finishes).
    apply_streamable_http_client_disconnect_patch()
    logger.info("Streamable HTTP client-disconnect patch applied")
    return http_app


_http_app: Starlette | None = None


def __getattr__(name: str) -> Any:
    """Lazily build ``app`` (used by ``uvicorn langsmith_mcp_server.server:app``)."""
    if name == "app":
        global _http_app
        if _http_app is None:
            _http_app = create_http_app()
        return _http_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    """Run the LangSmith MCP server."""
    logger.info("Starting LangSmith MCP server (stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
