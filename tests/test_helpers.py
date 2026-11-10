"""Tests for server-side LangSmith credential handling."""

import pytest
from langsmith import Client

from langsmith_mcp_server.common.helpers import (
    get_api_key_and_endpoint_from_context,
    get_client_from_context,
    get_langsmith_credentials_from_env,
)


class FakeContext:
    """Minimal stand-in for FastMCP's Context state API."""

    def __init__(self) -> None:
        self.state: dict = {}

    async def get_state(self, key: str):
        return self.state.get(key)

    async def set_state(self, key: str, value, serializable: bool = True) -> None:
        self.state[key] = value


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("LANGSMITH_API_KEY", "LANGSMITH_WORKSPACE_ID", "LANGSMITH_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_missing_api_key_raises(clean_env):
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY is not set"):
        get_langsmith_credentials_from_env()


async def test_client_from_env_is_cached_per_request(clean_env):
    clean_env.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    clean_env.setenv("LANGSMITH_ENDPOINT", "https://eu.api.smith.langchain.com/")
    ctx = FakeContext()
    client = await get_client_from_context(ctx)
    assert isinstance(client, Client)
    assert await get_client_from_context(ctx) is client

    api_key, endpoint = await get_api_key_and_endpoint_from_context(ctx)
    assert api_key == "lsv2_pt_test"
    assert endpoint == "https://eu.api.smith.langchain.com"


async def test_default_endpoint(clean_env):
    clean_env.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    _, endpoint = await get_api_key_and_endpoint_from_context(FakeContext())
    assert endpoint == "https://api.smith.langchain.com"
