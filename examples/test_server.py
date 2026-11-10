"""
Test script for LangSmith MCP Server using MCP streamable HTTP client.

Make sure to:
1. Install dependencies: pip install mcp langchain-mcp-adapters httpx python-dotenv
2. Start the server first: uvicorn langsmith_mcp_server.server:app --host 0.0.0.0 --port 8000
3. Obtain an OAuth access token from the server (authorization code + PKCE via Clerk)
   and set it: export MCP_ACCESS_TOKEN=... or put it in a .env file

The server authenticates clients with OAuth 2.1 only (Authorization: Bearer <token>).
The LangSmith API key, workspace and endpoint are configured on the server side via
LANGSMITH_API_KEY, LANGSMITH_WORKSPACE_ID and LANGSMITH_ENDPOINT.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    print("❌ Error: mcp is not installed")
    print("   Install it with: pip install mcp")
    sys.exit(1)

try:
    from langchain_mcp_adapters.tools import load_mcp_tools
except ImportError:
    print("❌ Error: langchain-mcp-adapters is not installed")
    print("   Install it with: pip install langchain-mcp-adapters")
    sys.exit(1)

try:
    import httpx
except ImportError:
    print("❌ Error: httpx is not installed")
    print("   Install it with: pip install httpx")
    sys.exit(1)


async def test_langsmith_mcp_server():
    """Test the LangSmith MCP server locally."""
    
    # Get OAuth access token from environment
    access_token = os.getenv("MCP_ACCESS_TOKEN")
    
    if not access_token:
        print("⚠️  Warning: Please set your MCP_ACCESS_TOKEN environment variable")
        print("   Complete the OAuth flow against the server (authorization code + PKCE)")
        print("   and export the issued access token:")
        print("     export MCP_ACCESS_TOKEN=...")
        return
    
    base_url = "http://localhost:8000"
    mcp_url = f"{base_url}/mcp"
    
    print("🚀 Connecting to LangSmith MCP Server...")
    print(f"   URL: {mcp_url}")
    print()
    
    # First, check if server is running
    print("📡 Checking if server is running...")
    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(f"{base_url}/health", timeout=5.0)
            if response.status_code == 200:
                print(f"   ✅ Server is running! Health check: {response.text}")
            else:
                print(f"   ⚠️  Server responded with status {response.status_code}")
    except httpx.ConnectError:
        print(f"   ❌ Cannot connect to server at {base_url}")
        print("   Make sure the server is running:")
        print("   uvicorn langsmith_mcp_server.server:app --host 0.0.0.0 --port 8000")
        return
    except Exception as e:
        print(f"   ⚠️  Error checking server: {e}")
    print()
    
    # OAuth bearer token (the only accepted authentication)
    headers = {"Authorization": f"Bearer {access_token}"}
    
    try:
        # Connect using streamable HTTP client
        async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                # Initialize the connection
                print("📋 Initializing MCP session...")
                await session.initialize()
                print("   ✅ Session initialized")
                print()
                
                # Test 1: Get available tools
                print("📋 Test 1: Getting available tools...")
                tools = await load_mcp_tools(session)
                print(f"   ✅ Found {len(tools)} tools:")
                # tools is a list, not a dict
                for tool in tools:
                    tool_name = getattr(tool, 'name', str(tool))
                    print(f"      - {tool_name}")
                print()
                
                # Test 2: List tools directly from session
                print("📋 Test 2: Listing tools from session...")
                try:
                    tools_result = await session.list_tools()
                    print(f"   ✅ Found {len(tools_result.tools)} tools:")
                    for tool in tools_result.tools:
                        print(f"      - {tool.name}: {tool.description[:60]}...")
                except Exception as e:
                    print(f"   ⚠️  Error listing tools: {e}")
                print()
                
                # Test 3: List prompts
                print("📋 Test 3: Listing prompts...")
                try:
                    prompts_result = await session.list_prompts()
                    print(f"   ✅ Found {len(prompts_result.prompts)} prompts:")
                    for prompt in prompts_result.prompts:
                        print(f"      - {prompt.name}")
                except Exception as e:
                    print(f"   ⚠️  Error listing prompts: {e}")
                print()
                
                # Test 4: List resources
                print("📋 Test 4: Listing resources...")
                try:
                    resources_result = await session.list_resources()
                    print(f"   ✅ Found {len(resources_result.resources)} resources:")
                    for resource in resources_result.resources:
                        print(f"      - {resource.uri}")
                except Exception as e:
                    print(f"   ⚠️  Error listing resources: {e}")
                print()
                
                # Test 5: Call a tool (list_prompts)
                print("📋 Test 5: Calling list_prompts tool...")
                try:
                    result = await session.call_tool(
                        "list_prompts",
                        arguments={"limit": 5, "is_public": "false"}
                    )
                    print(f"   ✅ Success! Tool called")
                    if hasattr(result, 'content'):
                        print(f"   📊 Result has {len(result.content)} content items")
                        if result.content:
                            # Try to extract some info from the result
                            first_item = result.content[0]
                            if hasattr(first_item, 'text'):
                                text = first_item.text[:200]
                                print(f"   📄 First result preview: {text}...")
                    else:
                        print(f"   📊 Result type: {type(result)}")
                except Exception as e:
                    print(f"   ❌ Error calling tool: {e}")
                    import traceback
                    traceback.print_exc()
                print()
                
                print("✅ All tests completed!")
                
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 LangSmith MCP Server Test Script")
    print("=" * 60)
    print()
    print("📝 Prerequisites:")
    print("   1. Start the server: uvicorn langsmith_mcp_server.server:app --host 0.0.0.0 --port 8000")
    print("   2. Set LANGSMITH_API_KEY environment variable")
    print()
    print("-" * 60)
    print()
    
    asyncio.run(test_langsmith_mcp_server())

