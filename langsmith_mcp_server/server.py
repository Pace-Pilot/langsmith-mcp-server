"""
MCP server for LangSmith SDK integration.
This server exposes methods to interact with LangSmith's observability platform:
- get_thread_history: Fetch conversation history for a specific thread
- get_prompts: Fetch prompts from LangSmith with optional filtering
- pull_prompt: Pull a specific prompt by its name

HTTP transport authentication (see langsmith_mcp_server/auth/):
- ``oauth`` mode (default when CLERK_ISSUER is set): OAuth 2.1 resource server. Clerk is the
  authorization server (PKCE authorization-code flow, CIMD client registration). This server
  publishes RFC 9728 protected resource metadata and validates audience-bound bearer tokens.
- ``api_key`` mode (legacy): LANGSMITH-API-KEY header.
"""

import logging
from typing import Any, Optional

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from langsmith_mcp_server.auth.provider import (
    ScopeChallengeMiddleware,
    build_auth_provider,
    oauth_lifespan,
    protected_resource_metadata_endpoint,
)
from langsmith_mcp_server.auth.settings import OAuthSettings, load_auth_settings
from langsmith_mcp_server.middleware import LangSmithCredentialsMiddleware
from langsmith_mcp_server.services import (
    register_prompts,
    register_resources,
    register_tools,
)
from langsmith_mcp_server.streamable_http_patch import (
    apply_streamable_http_client_disconnect_patch,
)

logger = logging.getLogger(__name__)


def create_mcp_server(settings: Optional[OAuthSettings] = None, **auth_kwargs: Any) -> FastMCP:
    """
    Build the FastMCP server with tools registered and (optionally) OAuth protection.

    Args:
        settings: Authentication settings. Defaults to ``load_auth_settings()`` (environment).
        **auth_kwargs: Forwarded to ``build_auth_provider`` (e.g. an ``http_client`` for tests).
    """
    settings = settings or load_auth_settings()
    auth = build_auth_provider(settings, **auth_kwargs)

    mcp = FastMCP(
        "LangSmith API MCP Server",
        auth=auth,
        lifespan=oauth_lifespan(settings) if settings.oauth_enabled else None,
    )
    mcp.auth_settings = settings  # type: ignore[attr-defined]
    logger.info(
        "MCP server instance created: LangSmith API MCP Server (auth mode: %s)",
        settings.mode.value,
    )

    # Register all tools with the server using simplified registration modules
    # Note: Tools resolve LangSmith credentials via common/helpers.get_client_from_context
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

    if settings.oauth_enabled:
        # RFC 9728 §3: the path-based document (/.well-known/oauth-protected-resource/mcp) is
        # registered by FastMCP. Also serve the root-level document for clients that probe it.
        mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])(
            protected_resource_metadata_endpoint(settings)
        )

    return mcp


def create_app(mcp: FastMCP, settings: Optional[OAuthSettings] = None) -> Starlette:
    """Create the ASGI application (streamable HTTP transport) for ``mcp``."""
    settings = settings or getattr(mcp, "auth_settings", None) or load_auth_settings()

    # Define middleware - credentials middleware must run before tools execute
    middleware = []
    if settings.oauth_enabled and settings.scopes_supported:
        # Advertise the scopes to request in the 401 challenge (WWW-Authenticate scope="...")
        middleware.append(Middleware(ScopeChallengeMiddleware, scopes=settings.scopes_supported))
    middleware += [
        Middleware(LangSmithCredentialsMiddleware, settings=settings),
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["mcp-session-id", "WWW-Authenticate"],
        ),
    ]

    app = mcp.http_app(path=settings.mcp_path, middleware=middleware)
    logger.info("ASGI app created (auth mode: %s)", settings.mode.value)
    return app


# Module-level server and ASGI app (used by `uvicorn langsmith_mcp_server.server:app`)
mcp = create_mcp_server()
app = create_app(mcp)

# Handle client disconnect gracefully (avoids ClosedResourceError/BrokenResourceError
# when client or proxy closes the connection before the server finishes).
apply_streamable_http_client_disconnect_patch()
logger.info("Streamable HTTP client-disconnect patch applied")


def main() -> None:
    """Run the LangSmith MCP server."""
    logger.info("Starting LangSmith MCP server (stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
