"""Tests for the request-context middleware (405 on GET /mcp, monitoring session id)."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from langsmith_mcp_server.middleware import RequestContextMiddleware
from langsmith_mcp_server.monitoring import monitoring_session_id_context


def _app() -> Starlette:
    async def echo_session(request: Request) -> PlainTextResponse:
        return PlainTextResponse(monitoring_session_id_context.get(""))

    return Starlette(
        routes=[Route("/mcp", echo_session, methods=["GET", "POST", "DELETE"])],
        middleware=[Middleware(RequestContextMiddleware)],
    )


def test_get_mcp_returns_405():
    client = TestClient(_app())
    response = client.get("/mcp")
    assert response.status_code == 405
    assert response.headers["Allow"] == "DELETE, POST"
    assert response.json()["allow"] == ["DELETE", "POST"]


def test_post_mcp_passes_through_without_api_key_header():
    """The middleware no longer requires (or reads) LANGSMITH-API-KEY."""
    client = TestClient(_app())
    response = client.post("/mcp", headers={"mcp-session-id": "session-123"})
    assert response.status_code == 200
    assert response.text == "session-123"


def test_session_id_fallback_headers_and_generated():
    client = TestClient(_app())
    assert client.post("/mcp", headers={"x-session-id": "s-2"}).text == "s-2"
    assert client.post("/mcp", headers={"x-request-id": "r-3"}).text == "r-3"
    generated = client.post("/mcp").text
    assert len(generated) == 36  # uuid4
