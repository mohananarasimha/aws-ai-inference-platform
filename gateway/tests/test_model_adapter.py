import json

import pytest

import model
import vllm
from conftest import FakeBedrock, patch_bedrock
from fake_openai_server import chat_response
from vllm import VLLMError

TOOLS = [{
    "toolSpec": {
        "name": "get_lambda_configuration",
        "description": "Read a Lambda function configuration",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"function_name": {"type": "string"}},
            "required": ["function_name"]
        }}
    }
}]

USER = [{"role": "user", "content": [{"text": "hello"}]}]


# ---- conversions -----------------------------------------------------------

def test_tools_to_openai():
    assert model.tools_to_openai(TOOLS) == [{
        "type": "function",
        "function": {
            "name": "get_lambda_configuration",
            "description": "Read a Lambda function configuration",
            "parameters": TOOLS[0]["toolSpec"]["inputSchema"]["json"]
        }
    }]


def test_user_text_message():
    assert model.messages_to_openai(USER) == [
        {"role": "user", "content": "hello"}
    ]


def test_assistant_tool_use_and_tool_results():
    messages = USER + [
        {"role": "assistant", "content": [
            {"text": "Checking."},
            {"toolUse": {"toolUseId": "a", "name": "t1", "input": {"x": 1}}},
            {"toolUse": {"toolUseId": "b", "name": "t2", "input": {}}},
        ]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "a", "status": "success",
                            "content": [{"json": {"MemorySize": 256}}]}},
            {"toolResult": {"toolUseId": "b", "status": "error",
                            "content": [{"text": "AccessDenied"}]}},
        ]},
    ]

    assert model.messages_to_openai(messages) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "t1", "arguments": '{"x": 1}'}},
            {"id": "b", "type": "function",
             "function": {"name": "t2", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": '{"MemorySize": 256}'},
        {"role": "tool", "tool_call_id": "b", "content": "AccessDenied"},
    ]


def test_assistant_without_text_has_null_content():
    messages = [{"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "a", "name": "t", "input": {}}}
    ]}]

    (converted,) = model.messages_to_openai(messages)

    assert converted["content"] is None
    assert converted["tool_calls"][0]["id"] == "a"


def test_response_plain_text():
    result = model.response_from_openai(chat_response(text="hi"))

    assert result["output"]["message"] == {
        "role": "assistant", "content": [{"text": "hi"}]
    }
    assert result["usage"] == {"inputTokens": 10, "outputTokens": 5}
    assert result["stopReason"] == "end_turn"


def test_response_tool_calls_parse_arguments():
    result = model.response_from_openai(chat_response(
        text=None,
        tool_calls=[{"id": "c1", "name": "t", "arguments": {"function_name": "f"}}]
    ))

    assert result["output"]["message"]["content"] == [
        {"toolUse": {"toolUseId": "c1", "name": "t",
                     "input": {"function_name": "f"}}}
    ]
    assert result["stopReason"] == "tool_use"


def test_response_text_and_tool_call_together():
    result = model.response_from_openai(chat_response(
        text="Let me check.",
        tool_calls=[{"id": "c1", "name": "t", "arguments": "{}"}]
    ))

    kinds = [list(block)[0] for block in result["output"]["message"]["content"]]
    assert kinds == ["text", "toolUse"]


def test_response_empty_arguments_become_empty_object():
    result = model.response_from_openai(chat_response(
        tool_calls=[{"id": "c1", "name": "t", "arguments": ""}]
    ))

    assert result["output"]["message"]["content"][0]["toolUse"]["input"] == {}


@pytest.mark.parametrize("arguments", ['{"broken', "[1, 2]", '"text"'])
def test_response_invalid_arguments_raise(arguments):
    with pytest.raises(VLLMError, match="vllm_invalid_response"):
        model.response_from_openai(chat_response(
            tool_calls=[{"id": "c1", "name": "t", "arguments": arguments}]
        ))


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{}]}, None])
def test_response_malformed_body_raises(body):
    with pytest.raises(VLLMError, match="vllm_invalid_response"):
        model.response_from_openai(body)


def test_response_missing_usage_is_zero():
    body = chat_response(text="hi")
    del body["usage"]

    assert model.response_from_openai(body)["usage"] == {
        "inputTokens": 0, "outputTokens": 0
    }


# ---- vLLM adapter against the fake server ----------------------------------

def test_vllm_plain_chat_request_and_response(fake_openai):
    fake_openai.reply(chat_response(text="hi there"))

    result = model.converse("vllm", "Qwen/Qwen2.5-7B-Instruct", USER, [])

    assert result["output"]["message"]["content"] == [{"text": "hi there"}]

    (request,) = fake_openai.requests
    assert request["path"] == "/v1/chat/completions"
    assert request["body"] == {
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 512,
        "temperature": 0
    }


def test_vllm_sends_tools_when_present(fake_openai):
    fake_openai.reply(chat_response(text="ok"))

    model.converse("vllm", "m", USER, TOOLS)

    sent = fake_openai.requests[0]["body"]["tools"]
    assert sent[0]["function"]["name"] == "get_lambda_configuration"


def test_vllm_tool_call_reply(fake_openai):
    fake_openai.reply(chat_response(
        tool_calls=[{"id": "c1", "name": "get_lambda_configuration",
                     "arguments": {"function_name": "f"}}]
    ))

    result = model.converse("vllm", "m", USER, TOOLS)

    assert result["stopReason"] == "tool_use"
    assert result["output"]["message"]["content"][0]["toolUse"]["input"] == {
        "function_name": "f"
    }


def test_vllm_server_error_is_unavailable(fake_openai):
    fake_openai.reply({"error": "boom"}, status=500)

    with pytest.raises(VLLMError, match="vllm_unavailable"):
        model.converse("vllm", "m", USER, [])


def test_vllm_client_error_is_request_failed(fake_openai):
    fake_openai.reply({"error": "auto tool choice requires --enable-auto-tool-choice"}, status=400)

    with pytest.raises(VLLMError, match="vllm_request_failed"):
        model.converse("vllm", "m", USER, TOOLS)


def test_vllm_invalid_json_body(fake_openai):
    fake_openai.reply(b"not json")

    with pytest.raises(VLLMError, match="vllm_invalid_response"):
        model.converse("vllm", "m", USER, [])


def test_vllm_timeout(fake_openai, monkeypatch):
    monkeypatch.setattr(vllm, "VLLM_MAX_TIMEOUT_SECONDS", 0.3)
    fake_openai.reply(chat_response(text="late"), delay=1.5)

    with pytest.raises(VLLMError, match="vllm_timeout"):
        model.converse("vllm", "m", USER, [])


def test_vllm_connection_refused(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:9")

    with pytest.raises(VLLMError, match="vllm_unavailable"):
        model.converse("vllm", "m", USER, [])


def test_vllm_endpoint_not_found(monkeypatch):
    with pytest.raises(VLLMError, match="vllm_endpoint_not_found"):
        model.converse("vllm", "m", USER, [])


def test_error_message_never_contains_prompt(fake_openai):
    fake_openai.reply({"error": "boom"}, status=500)

    secret = [{"role": "user", "content": [{"text": "SECRET-PROMPT"}]}]

    with pytest.raises(VLLMError) as excinfo:
        model.converse("vllm", "m", secret, [])

    assert "SECRET-PROMPT" not in str(excinfo.value)


# ---- Bedrock adapter and dispatch -------------------------------------------

def test_bedrock_adapter_passes_request_through(monkeypatch):
    reply = {"output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
             "usage": {"inputTokens": 1, "outputTokens": 1}}
    fake = FakeBedrock([reply])
    patch_bedrock(monkeypatch, fake)

    result = model.converse("bedrock", "amazon.nova-micro-v1:0", USER, TOOLS)

    assert result is reply
    assert fake.calls == [{
        "modelId": "amazon.nova-micro-v1:0",
        "messages": USER,
        "toolConfig": {"tools": TOOLS},
        "inferenceConfig": {"maxTokens": 1000, "temperature": 0}
    }]


def test_unknown_provider():
    with pytest.raises(ValueError, match="unknown provider"):
        model.converse("openai", "m", USER, [])


def test_bedrock_is_not_called_for_vllm(monkeypatch, fake_openai):
    fake = FakeBedrock([])
    patch_bedrock(monkeypatch, fake)
    fake_openai.reply(chat_response(text="hi"))

    model.converse("vllm", "m", USER, [])

    assert fake.calls == []
