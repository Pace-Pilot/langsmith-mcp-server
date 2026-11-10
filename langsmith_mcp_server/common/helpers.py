"""Helper functions for the LangSmith MCP server."""

import os
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, List, Optional, Union
from uuid import UUID

from fastmcp.server import Context
from langsmith import Client

DEFAULT_LANGSMITH_ENDPOINT = "https://api.smith.langchain.com"


def get_langsmith_client_from_api_key(
    api_key: str, workspace_id: Optional[str] = None, endpoint: Optional[str] = None
) -> Client:
    """
    Create a LangSmith client from an API key and optional configuration.

    We do not set os.environ (LANGSMITH_API_KEY, etc.) so that concurrent
    HTTP requests do not overwrite each other's state.

    Args:
        api_key: The LangSmith API key (required)
        workspace_id: Optional workspace ID for API keys scoped to multiple workspaces
        endpoint: Optional custom endpoint URL (e.g., for self-hosted installations or EU region)

    Returns:
        LangSmith Client instance
    """  # noqa: W293
    client_kwargs = {"api_key": api_key}
    if workspace_id:
        client_kwargs["workspace_id"] = workspace_id
    if endpoint:
        client_kwargs["api_url"] = endpoint

    return Client(**client_kwargs)


def get_langsmith_credentials_from_env() -> tuple[str, Optional[str], Optional[str]]:
    """
    Read the server's LangSmith credentials from the process environment.

    The server talks to LangSmith with its own API key. MCP clients never send a
    LangSmith API key; over HTTP they authenticate to this server with OAuth (see
    auth.py), over stdio the process environment is the trust boundary.

    Returns:
        Tuple of (api_key, workspace_id or None, endpoint or None).

    Raises:
        ValueError: If LANGSMITH_API_KEY is not set.
    """
    api_key = os.environ.get("LANGSMITH_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "LANGSMITH_API_KEY is not set. The server authenticates to LangSmith with its own "
            "API key from the environment; clients authenticate to the server with OAuth."
        )
    workspace_id = os.environ.get("LANGSMITH_WORKSPACE_ID", "").strip() or None
    endpoint = os.environ.get("LANGSMITH_ENDPOINT", "").strip() or None
    return api_key, workspace_id, endpoint


_CLIENT_STATE_KEY = "__langsmith_client"


async def get_client_from_context(ctx: Context) -> Client:
    """
    Get the LangSmith client for the current request.

    Credentials come from the environment (LANGSMITH_API_KEY, LANGSMITH_WORKSPACE_ID,
    LANGSMITH_ENDPOINT) for both HTTP and stdio transports. The Client is created once
    per request and cached in FastMCP request state so that several tool calls within
    one request share it (less memory churn and connection buildup under load).

    Args:
        ctx: FastMCP context (automatically provided to tools)

    Returns:
        LangSmith Client instance

    Raises:
        ValueError: If LANGSMITH_API_KEY is not set.
    """  # noqa: W293
    cached = await ctx.get_state(_CLIENT_STATE_KEY)
    if cached is not None:
        return cached  # type: ignore[return-value]

    api_key, workspace_id, endpoint = get_langsmith_credentials_from_env()
    client = get_langsmith_client_from_api_key(
        api_key, workspace_id=workspace_id, endpoint=endpoint
    )
    await ctx.set_state(_CLIENT_STATE_KEY, client, serializable=False)
    return client


async def get_api_key_and_endpoint_from_context(ctx: Context) -> tuple[str, str]:
    """
    Get the server's LangSmith API key and endpoint (same source as get_client_from_context).
    Used by tools that call LangSmith REST APIs directly (e.g. billing/usage).

    Args:
        ctx: FastMCP context (automatically provided to tools).

    Returns:
        Tuple of (api_key, endpoint). endpoint is normalized (no trailing slash).

    Raises:
        ValueError: If LANGSMITH_API_KEY is not set.
    """
    api_key, _, endpoint = get_langsmith_credentials_from_env()
    return (api_key, (endpoint or DEFAULT_LANGSMITH_ENDPOINT).rstrip("/"))


def get_langgraph_app_host_name(run_stats: dict) -> Optional[str]:
    """
    Get the langgraph app host name from the run stats

    Args:
        run_stats (dict): The run stats

    Returns:
        str | None: The langgraph app host name
    """  # noqa: W293
    if run_stats and run_stats.get("run_facets"):
        for run_facet in run_stats["run_facets"]:
            try:
                for rfk in run_facet.keys():
                    langgraph_host = re.search(r"http[s]?:\/\/(?P<langgraph_host>[^\/]+)", rfk)
                    if langgraph_host:
                        return langgraph_host.group("langgraph_host")
            except re.error:
                continue
    return None


def _parse_as_of_parameter(as_of: str) -> Union[datetime, str]:
    """
    Parse the as_of parameter, converting ISO timestamps to datetime objects
    while leaving version tags as strings.

    Args:
        as_of: Dataset version tag OR ISO timestamp string

    Returns:
        datetime object if as_of is a valid ISO timestamp, otherwise the original string
    """  # noqa: W293
    try:
        # Try to parse as ISO format datetime
        return datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        # If parsing fails, assume it's a version tag and return as string
        return as_of


def find_in_dict(data, key):
    """
    Recursively search for a key in a nested dictionary or list.

    This helper function traverses nested data structures to find a specific key,
    searching through dictionaries and lists at any depth level.

    ---
    ⚙️ PARAMETERS
    -------------
    data : dict | list | Any
        The data structure to search in. Can be a dictionary, list, or any nested
        combination of these types.

    key : str
        The key to search for in the data structure.

    ---
    📤 RETURNS
    ----------
    Any | None
        The value associated with the key if found, otherwise None.
        Returns the first occurrence found during depth-first traversal.

    ---
    🧪 EXAMPLES
    ------------
    ```python
    data = {
        "a": 1,
        "b": {
            "c": {"deployment_id": "123-456"}
        }
    }
    result = find_in_dict(data, "deployment_id")  # Returns "123-456"
    ```
    """  # noqa: W293
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            result = find_in_dict(value, key)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = find_in_dict(item, key)
            if result is not None:
                return result
    return None


def convert_uuids_to_strings(obj: Any) -> Any:
    """
    Recursively convert UUID, datetime, and Decimal objects to strings in dictionaries, lists, and other structures.
    """  # noqa: W293
    if isinstance(obj, UUID):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, Decimal):
        return str(obj)
    elif isinstance(obj, dict):
        return {key: convert_uuids_to_strings(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_uuids_to_strings(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(convert_uuids_to_strings(item) for item in obj)
    else:
        return obj


def count_characters(obj: Any) -> int:
    """
    Recursively count the total number of characters in a data structure.
    """  # noqa: W293
    if isinstance(obj, str):
        return len(obj)
    elif isinstance(obj, dict):
        return sum(count_characters(value) for value in obj.values())
    elif isinstance(obj, (list, tuple)):
        return sum(count_characters(item) for item in obj)
    else:
        # For other types, convert to string and count
        return len(str(obj))


def count_fields(obj: Any) -> int:
    """
    Recursively count the total number of fields/keys in a data structure.
    """  # noqa: W293
    if isinstance(obj, dict):
        return len(obj) + sum(count_fields(value) for value in obj.values())
    elif isinstance(obj, (list, tuple)):
        return sum(count_fields(item) for item in obj)
    else:
        return 0


def filter_fields(run_dict: dict, select: Optional[List[str]]) -> dict:
    """
    Filter a run dictionary to only include selected fields.
    If select is None or empty, returns the full dictionary.
    """  # noqa: W293
    if not select:
        return run_dict

    filtered = {}
    for field in select:
        if field in run_dict:
            filtered[field] = run_dict[field]
    return filtered


def build_trace_tree(run_dict: dict, depth: int = 0) -> dict:
    """
    Build a simplified trace tree structure showing top-level fields with metrics for nested content.

    Args:
        run_dict: The dictionary to build a tree from
        depth: How many levels deep to show actual content before summarizing.
               0 = summarize all nested structures (default)
               1 = show one level deep, then summarize
               2 = show two levels deep, then summarize
               etc.
    """  # noqa: W293
    tree = {}
    for key, value in run_dict.items():
        if isinstance(value, dict):
            if len(value) == 0:
                # Empty dictionary - just return empty dict
                tree[key] = {}
            elif depth > 0:
                # Show one level of content, then summarize deeper
                tree[key] = build_trace_tree(value, depth - 1)
            else:
                # For dictionaries, show metrics
                field_count = count_fields(value)
                if field_count == 0:
                    # Empty dictionary - just return empty dict
                    tree[key] = {}
                else:
                    tree[key] = {
                        "_type": "dict",
                        "_field_count": field_count,
                        "_character_count": count_characters(value),
                        "_keys": list(value.keys())[:10],  # Show first 10 keys as preview
                    }
        elif isinstance(value, list):
            # For lists, show metrics
            if len(value) == 0:
                tree[key] = []
            elif depth > 0:
                # Show one level of content
                processed_items = []
                for item in value:
                    if isinstance(item, (dict, list)):
                        # Recursively process nested structures
                        if isinstance(item, dict):
                            processed_items.append(build_trace_tree(item, depth - 1))
                        else:  # list
                            processed_items.append(
                                [
                                    build_trace_tree(subitem, depth - 1)
                                    if isinstance(subitem, dict)
                                    else subitem
                                    for subitem in item
                                ]
                            )
                    else:
                        processed_items.append(item)
                tree[key] = processed_items
            else:
                # For lists, show metrics and a small preview
                preview = []
                for item in value[:2]:  # Take first 2 items
                    if isinstance(item, dict):
                        # For dict items, show just keys
                        preview.append({"_type": "dict", "_keys": list(item.keys())[:5]})
                    elif isinstance(item, list):
                        # For list items, show length
                        preview.append({"_type": "list", "_length": len(item)})
                    else:
                        # For primitive items, show the value (but limit length)
                        str_val = str(item)
                        preview.append(str_val[:100] if len(str_val) > 100 else str_val)

                tree[key] = {
                    "_type": "list",
                    "_length": len(value),
                    "_field_count": count_fields(value),
                    "_character_count": count_characters(value),
                    "_preview": preview,
                }
        else:
            # For primitive values, show directly
            tree[key] = value
    return tree
