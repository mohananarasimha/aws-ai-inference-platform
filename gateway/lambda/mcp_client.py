import json
import os
import time
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import quote

import anyio
import httpx2

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from config import (
    MCP_CONNECT_TIMEOUT_SECONDS,
    MCP_MAX_TIMEOUT_SECONDS,
    MCP_SESSION_MAX_TIMEOUT_SECONDS,
    REGION,
)
from deadline import ComponentTimeout, DeadlineExceeded, call_timeout


@contextmanager
def mcp_timeout(deadline, component):
    """
    Wall-clock limit for one MCP operation: min(MCP maximum, time left).
    The httpx timeouts only bound single network waits, so each operation
    is also cancelled here.
    """

    seconds = call_timeout(deadline, MCP_MAX_TIMEOUT_SECONDS, component)

    try:

        with anyio.fail_after(seconds):
            yield

    except TimeoutError as exc:

        raise ComponentTimeout("mcp") from exc


def build_agentcore_mcp_url():
    """
    Build the AgentCore MCP invocation URL from the raw Runtime ARN.

    Terraform only needs to inject:
        AGENTCORE_RUNTIME_ARN

    We do not manually URL-encode the ARN.
    """

    runtime_arn = os.environ["AGENTCORE_RUNTIME_ARN"]

    region = os.environ.get(
        "AWS_REGION",
        REGION
    )

    encoded_arn = quote(
        runtime_arn,
        safe=""
    )

    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/"
        f"runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )


def mcp_tools_to_bedrock(mcp_tools):
    """
    MCP and Bedrock describe tools using slightly different schemas.

    This converts:
        MCP tool
            name
            description
            input_schema

    into:
        Bedrock toolSpec
    """

    bedrock_tools = []

    for tool in mcp_tools:
        bedrock_tools.append({
            "toolSpec": {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": {
                    "json": tool.input_schema
                }
            }
        })

    return bedrock_tools


def mcp_result_to_json(result):
    """
    Normalize an MCP tool result into something Bedrock's
    toolResult content can consume.
    """

    if result.structured_content is not None:
        return result.structured_content

    return {
        "content": [
            getattr(
                item,
                "text",
                str(item)
            )
            for item in result.content
        ]
    }


# Lives as long as this Lambda execution environment. Holds only plain
# data (no event-loop-bound objects): the AgentCore MCP session id and
# the Bedrock-converted tool list.
_cache = {
    "session_id": None,
    "tools": None,
    "output_schemas": None,
}


def invalidate_mcp_cache():
    _cache["session_id"] = None
    _cache["tools"] = None
    _cache["output_schemas"] = None


def _seed_output_schemas(session):
    """
    The MCP SDK validates tool results against each tool's output schema
    and, if the session has not seen the tool yet, silently calls
    list_tools() first (one extra round trip). Pre-fill the session's
    schema cache from our cached tool list so that does not happen.

    Uses a private SDK attribute (mcp is pinned in requirements.txt).
    If it is missing, nothing is seeded and the SDK behaves as before.
    """

    schemas = getattr(session, "_tool_output_schemas", None)

    if schemas is not None and _cache["output_schemas"] is not None:
        schemas.update(_cache["output_schemas"])


async def _remember_session_id(response):
    session_id = response.headers.get("mcp-session-id")

    if session_id:
        _cache["session_id"] = session_id


@asynccontextmanager
async def open_mcp_session(
    authorization_header,
    metrics=None,
    reuse=True,
    deadline=None
):
    """
    Connect to AgentCore MCP with the caller's Authorization header
    and yield an MCP session.

    Cold (no cached session id): full MCP handshake, and the session
    id returned by AgentCore is cached.

    Warm (cached session id): the id is sent as the Mcp-Session-Id
    header and the handshake is skipped.

    If a metrics dict is passed, timings and whether the session was
    reused are written into it.
    """

    connect_start = time.time()

    mcp_url = build_agentcore_mcp_url()

    headers = {
        "Authorization": authorization_header
    }

    session_reused = (
        reuse
        and _cache["session_id"] is not None
    )

    if session_reused:
        headers["Mcp-Session-Id"] = _cache["session_id"]
    else:
        invalidate_mcp_cache()

    # Never longer than the request has left. The read timeout is the whole
    # remaining budget, not the MCP maximum: the SDK keeps an idle stream
    # open and a short read timeout would cut it. Each operation below is
    # limited separately by mcp_timeout.
    budget = call_timeout(deadline, MCP_SESSION_MAX_TIMEOUT_SECONDS, "mcp_connect")
    short = min(MCP_CONNECT_TIMEOUT_SECONDS, budget)

    http_client = httpx2.AsyncClient(
        headers=headers,
        event_hooks={
            "response": [_remember_session_id]
        },
        timeout=httpx2.Timeout(
            connect=short,
            read=budget,
            write=short,
            pool=short,
        ),
    )

    async with http_client:

        async with streamable_http_client(
            mcp_url,
            http_client=http_client,
            terminate_on_close=False,
        ) as streams:

            read_stream = streams[0]
            write_stream = streams[1]

            async with ClientSession(
                read_stream,
                write_stream,
            ) as session:

                initialize_start = time.time()

                if not session_reused:
                    # MCP handshake
                    with mcp_timeout(deadline, "mcp_initialize"):
                        await session.initialize()

                if metrics is not None:
                    metrics["mcp_session_reused"] = session_reused
                    metrics["mcp_connect_latency_ms"] = int(
                        (initialize_start - connect_start) * 1000
                    )
                    metrics["mcp_initialize_latency_ms"] = (
                        0 if session_reused
                        else int((time.time() - initialize_start) * 1000)
                    )

                yield session


async def list_bedrock_tools(session, metrics=None, deadline=None):
    """
    Discover tools exposed by the MCP server and return them
    as Bedrock toolSpecs. Uses the cached tool list when available.
    """

    if _cache["tools"] is not None:

        _seed_output_schemas(session)

        if metrics is not None:
            metrics["mcp_list_tools_latency_ms"] = 0

        print(json.dumps({
            "event": "mcp_tools_discovered",
            "cached": True,
            "tools": [
                tool["toolSpec"]["name"]
                for tool in _cache["tools"]
            ]
        }))

        return _cache["tools"]

    list_tools_start = time.time()

    with mcp_timeout(deadline, "mcp_list_tools"):
        tool_response = await session.list_tools()

    if metrics is not None:
        metrics["mcp_list_tools_latency_ms"] = int(
            (time.time() - list_tools_start) * 1000
        )

    mcp_tools = tool_response.tools

    print(json.dumps({
        "event": "mcp_tools_discovered",
        "tools": [
            tool.name
            for tool in mcp_tools
        ]
    }))

    # Convert MCP schemas to Bedrock tool schemas
    bedrock_tools = mcp_tools_to_bedrock(
        mcp_tools
    )

    _cache["tools"] = bedrock_tools
    _cache["output_schemas"] = {
        tool.name: tool.output_schema
        for tool in mcp_tools
    }

    return bedrock_tools


async def call_mcp_tool(
    session,
    tool_name,
    tool_input,
    authorization_header=None,
    metrics=None,
    deadline=None
):
    """
    Execute one MCP tool and return its normalized result.

    If the call fails on a reused session, discard the cache, open one
    fresh session and retry once. At most one reconnect per request.
    """

    try:

        with mcp_timeout(deadline, "mcp_tool"):
            result = await session.call_tool(
                tool_name,
                arguments=tool_input
            )

    except (ComponentTimeout, DeadlineExceeded):

        # A slow tool is not a broken session: no reconnect, no second wait.
        raise

    except Exception:

        can_retry = (
            metrics is not None
            and metrics.get("mcp_session_reused")
            and not metrics.get("mcp_reconnected")
            and authorization_header is not None
        )

        if not can_retry:
            raise

        metrics["mcp_reconnected"] = True

        print(json.dumps({
            "event": "mcp_reconnect",
            "tool": tool_name
        }))

        invalidate_mcp_cache()

        async with open_mcp_session(
            authorization_header,
            reuse=False,
            deadline=deadline
        ) as fresh_session:

            with mcp_timeout(deadline, "mcp_tool"):
                result = await fresh_session.call_tool(
                    tool_name,
                    arguments=tool_input
                )

    return mcp_result_to_json(
        result
    )
