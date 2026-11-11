"""
Model adapter: one function the agent loop calls, whatever the backend.

The agent loop keeps its history, tool specs and responses in Bedrock
Converse shape. The Bedrock adapter passes them straight through; the vLLM
adapter translates them to the OpenAI chat format and translates the reply
back, so agent.py does not know which backend answered.
"""

import json

from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from clients import aws_client
from config import BEDROCK_MAX_TIMEOUT_SECONDS, VLLM_MAX_TOKENS
from deadline import ComponentTimeout, call_timeout
from vllm import VLLMError, request_chat


def converse(provider, model, messages, tools, deadline=None):
    """
    One model turn. Returns a Bedrock Converse-shaped dict with
    output.message, usage.inputTokens and usage.outputTokens.
    """

    if provider == "bedrock":
        return _bedrock_converse(model, messages, tools, deadline)

    if provider == "vllm":
        return _vllm_converse(model, messages, tools, deadline)

    raise ValueError(f"unknown provider: {provider}")


def _bedrock_converse(model, messages, tools, deadline):

    client = aws_client(
        "bedrock-runtime",
        call_timeout(deadline, BEDROCK_MAX_TIMEOUT_SECONDS, "bedrock")
    )

    try:

        return client.converse(
            modelId=model,
            messages=messages,
            toolConfig={
                "tools": tools
            },
            inferenceConfig={
                "maxTokens": 1000,
                "temperature": 0
            }
        )

    except (ReadTimeoutError, ConnectTimeoutError) as exc:

        raise ComponentTimeout("bedrock") from exc


def _vllm_converse(model, messages, tools, deadline):

    payload = {
        "model": model,
        "messages": messages_to_openai(messages),
        "max_tokens": VLLM_MAX_TOKENS,
        "temperature": 0
    }

    if tools:
        payload["tools"] = tools_to_openai(tools)

    return response_from_openai(
        request_chat(payload, deadline)
    )


def tools_to_openai(tools):
    """Bedrock toolSpecs to OpenAI function tools (same JSON Schema)."""

    return [
        {
            "type": "function",
            "function": {
                "name": tool["toolSpec"]["name"],
                "description": tool["toolSpec"].get("description", ""),
                "parameters": tool["toolSpec"]["inputSchema"]["json"]
            }
        }
        for tool in tools
    ]


def _tool_result_text(tool_result):

    parts = []

    for item in tool_result["content"]:

        if "json" in item:
            parts.append(json.dumps(item["json"], default=str))
        else:
            parts.append(item.get("text", ""))

    return "\n".join(parts)


def messages_to_openai(messages):
    """
    Converse messages to OpenAI messages.

    - user text blocks            -> one user message
    - user toolResult blocks      -> one tool message per result
    - assistant text + toolUse    -> one assistant message with tool_calls
    """

    converted = []

    for message in messages:

        blocks = message["content"]

        if message["role"] == "assistant":

            text = "".join(
                block["text"]
                for block in blocks
                if "text" in block
            )

            assistant = {
                "role": "assistant",
                "content": text or None
            }

            tool_calls = [
                {
                    "id": block["toolUse"]["toolUseId"],
                    "type": "function",
                    "function": {
                        "name": block["toolUse"]["name"],
                        "arguments": json.dumps(block["toolUse"]["input"])
                    }
                }
                for block in blocks
                if "toolUse" in block
            ]

            if tool_calls:
                assistant["tool_calls"] = tool_calls

            converted.append(assistant)
            continue

        for block in blocks:

            if "toolResult" in block:

                converted.append({
                    "role": "tool",
                    "tool_call_id": block["toolResult"]["toolUseId"],
                    "content": _tool_result_text(block["toolResult"])
                })

        text = "".join(
            block["text"]
            for block in blocks
            if "text" in block
        )

        if text:
            converted.append({
                "role": "user",
                "content": text
            })

    return converted


def response_from_openai(body):
    """
    OpenAI chat completion to a Converse-shaped response. Anything that
    does not look like a valid completion raises VLLMError.
    """

    try:

        choice = body["choices"][0]
        message = choice["message"]

        content = []

        if message.get("content"):
            content.append({"text": message["content"]})

        for call in message.get("tool_calls") or []:

            arguments = call["function"]["arguments"]

            if isinstance(arguments, str):
                arguments = json.loads(arguments) if arguments.strip() else {}

            if not isinstance(arguments, dict):
                raise TypeError("tool arguments are not an object")

            content.append({
                "toolUse": {
                    "toolUseId": call["id"],
                    "name": call["function"]["name"],
                    "input": arguments
                }
            })

        usage = body.get("usage") or {}

        has_tool_use = any("toolUse" in block for block in content)

        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": content
                }
            },
            "usage": {
                "inputTokens": usage.get("prompt_tokens", 0),
                "outputTokens": usage.get("completion_tokens", 0)
            },
            "stopReason": "tool_use" if has_tool_use else "end_turn"
        }

    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:

        raise VLLMError("vllm_invalid_response") from exc
