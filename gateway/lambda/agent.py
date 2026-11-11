import json
import time

from approval import (
    WRITE_TOOL_NAMES,
    proposal_reason,
    start_approval,
    write_tool_specs,
)
from config import MAX_AGENT_STEPS
from deadline import DeadlineExceeded, request_budget
from mcp_client import (
    call_mcp_tool,
    list_bedrock_tools,
    open_mcp_session,
)
from model import converse


async def run_agent(
    model_prompt,
    model_id,
    authorization_header,
    request_id,
    provider="bedrock",
    deadline=None
):
    """
    This is the actual agent loop.

    Flow:

    1. Connect to AgentCore MCP
    2. Discover tools
    3. Give those tool schemas to Nova
    4. Nova either:
       - answers directly
       - requests MCP tools
    5. Execute requested tools
    6. Send tool results back to Nova
    7. Repeat until final answer

    Every downstream call gets min(its maximum, time left in `deadline`).
    When less than MIN_CALL_SECONDS is left, no new step or call starts and
    DeadlineExceeded is raised.
    """

    if deadline is None:
        deadline = request_budget()

    mcp_metrics = {
        "mcp_reconnected": False
    }

    async with open_mcp_session(
        authorization_header,
        mcp_metrics,
        deadline=deadline
    ) as session:

        bedrock_tools = await list_bedrock_tools(
            session,
            mcp_metrics,
            deadline=deadline
        )

        # Write tools are defined here, never executed through MCP.
        bedrock_tools = [
            tool
            for tool in bedrock_tools
            if tool["toolSpec"]["name"] not in WRITE_TOOL_NAMES
        ] + write_tool_specs()

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "text": model_prompt
                    }
                ]
            }
        ]

        total_input_tokens = 0
        total_output_tokens = 0

        model_calls_ms = []
        tool_calls = []

        def build_result(status, response, steps, **extra):
            return {
                "status": status,
                "response": response,
                "agent_steps": steps,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "model_calls_ms": model_calls_ms,
                "total_model_latency_ms": sum(model_calls_ms),
                "tool_calls": tool_calls,
                "mcp_metrics": mcp_metrics,
                **extra
            }

        for step in range(MAX_AGENT_STEPS):

            deadline.step = step + 1
            deadline.check("agent_loop")

            model_start = time.time()

            response = converse(
                provider,
                model_id,
                messages,
                bedrock_tools,
                deadline
            )

            model_calls_ms.append(
                int((time.time() - model_start) * 1000)
            )

            usage = response.get(
                "usage",
                {}
            )

            total_input_tokens += usage.get(
                "inputTokens",
                0
            )

            total_output_tokens += usage.get(
                "outputTokens",
                0
            )

            model_message = (
                response["output"]["message"]
            )

            messages.append(
                model_message
            )

            # Find every toolUse block Nova requested
            tool_uses = [
                block["toolUse"]
                for block in model_message["content"]
                if "toolUse" in block
            ]

            # No tool requested = Nova produced final answer
            if not tool_uses:

                output_text = ""

                for block in model_message["content"]:
                    if "text" in block:
                        output_text += block["text"]

                return build_result(
                    "completed",
                    output_text,
                    step + 1
                )

            # Write tools are never executed here: hand the exact
            # proposed action to the approval workflow and stop.
            write_use = next(
                (
                    tool_use
                    for tool_use in tool_uses
                    if tool_use["name"] in WRITE_TOOL_NAMES
                ),
                None
            )

            if write_use is not None:

                deadline.check("approval")

                try:

                    execution_arn = start_approval(
                        request_id,
                        write_use["name"],
                        write_use["input"],
                        proposal_reason(
                            model_message,
                            write_use["name"]
                        ),
                        deadline
                    )

                except DeadlineExceeded:

                    raise

                except Exception as exc:

                    print(json.dumps({
                        "event": "approval_start_failed",
                        "request_id": request_id,
                        "tool": write_use["name"],
                        "error": repr(exc)
                    }))

                    return build_result(
                        "approval_failed",
                        "Could not start the approval workflow.",
                        step + 1
                    )

                print(json.dumps({
                    "event": "approval_requested",
                    "request_id": request_id,
                    "tool": write_use["name"],
                    "arguments": write_use["input"],
                    "approval_execution_arn": execution_arn
                }))

                return build_result(
                    "approval_required",
                    "Approval required before this action can run.",
                    step + 1,
                    approval={
                        "action": {
                            "tool": write_use["name"],
                            "arguments": write_use["input"]
                        },
                        "execution_arn": execution_arn
                    }
                )

            # Nova requested one or more tools
            tool_result_blocks = []

            for tool_use in tool_uses:

                tool_name = tool_use["name"]
                tool_input = tool_use["input"]
                tool_use_id = tool_use["toolUseId"]

                print(json.dumps({
                    "event": "tool_requested",
                    "tool": tool_name,
                    "arguments": tool_input
                }))

                tool_start = time.time()

                try:

                    deadline.check("mcp_tool")

                    # Actual MCP tool execution
                    tool_result_data = await call_mcp_tool(
                        session,
                        tool_name,
                        tool_input,
                        authorization_header,
                        mcp_metrics,
                        deadline
                    )

                    tool_calls.append({
                        "tool": tool_name,
                        "latency_ms": int(
                            (time.time() - tool_start) * 1000
                        ),
                        "status": "success"
                    })

                    print(json.dumps({
                        "event": "tool_result",
                        "tool": tool_name,
                        "result": tool_result_data
                    }, default=str))

                    tool_result_blocks.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [
                                {
                                    "json": tool_result_data
                                }
                            ],
                            "status": "success"
                        }
                    })

                except DeadlineExceeded:

                    raise

                except Exception as exc:

                    tool_calls.append({
                        "tool": tool_name,
                        "latency_ms": int(
                            (time.time() - tool_start) * 1000
                        ),
                        "status": "error",
                        "error_type": type(exc).__name__
                    })

                    print(json.dumps({
                        "event": "tool_error",
                        "tool": tool_name,
                        "error": repr(exc)
                    }))

                    tool_result_blocks.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [
                                {
                                    "text": str(exc)
                                }
                            ],
                            "status": "error"
                        }
                    })

            # IMPORTANT:
            # all tool results from this model turn
            # go back in one user message
            messages.append({
                "role": "user",
                "content": tool_result_blocks
            })

        return build_result(
            "max_steps_reached",
            (
                "Agent reached the maximum number "
                "of tool/reasoning steps."
            ),
            MAX_AGENT_STEPS
        )
