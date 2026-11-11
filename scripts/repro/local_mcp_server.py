"""
Minimal local MCP server (one tool) for the timeout-gap reproductions.

    python3 scripts/repro/local_mcp_server.py        # listens on 127.0.0.1:18765

Needs the gateway's Python environment (mcp, uvicorn). Local only: no AWS.
"""
import uvicorn
from mcp.server.mcpserver import MCPServer

server = MCPServer("repro")


@server.tool()
def ping() -> str:
    "ping"
    return "pong"


uvicorn.run(server.streamable_http_app(), host="127.0.0.1", port=18765, log_level="warning")
