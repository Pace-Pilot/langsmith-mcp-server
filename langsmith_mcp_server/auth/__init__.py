"""
OAuth 2.1 resource-server authentication for the LangSmith MCP server (HTTP transport).

Architecture (MCP authorization spec, revision 2026-07-28):

- This MCP server is an **OAuth 2.1 Resource Server** only. It never issues tokens.
- Clerk is the **Authorization Server** (IdP). It handles the PKCE authorization-code
  flow, consent, and client registration via Client ID Metadata Documents (CIMD).
- The server publishes RFC 9728 Protected Resource Metadata at
  ``/.well-known/oauth-protected-resource/mcp`` (and a root fallback) pointing at Clerk,
  answers unauthenticated requests with ``401`` + ``WWW-Authenticate: Bearer
  resource_metadata="..."``, and validates every bearer token's signature, issuer,
  expiry and **audience** (RFC 8707 resource indicator) before serving a tool call.

See :mod:`langsmith_mcp_server.auth.settings` for configuration and
:mod:`langsmith_mcp_server.auth.provider` for how the pieces are wired together.
"""

from langsmith_mcp_server.auth.settings import AuthMode, OAuthSettings, load_auth_settings

__all__ = ["AuthMode", "OAuthSettings", "load_auth_settings"]
