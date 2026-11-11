import asyncio
import time

import pytest

import agent
import rag
from conftest import FakeMcp
from fake_openai_server import chat_response
from vllm import VLLMError

MODEL = "Qwen/Qwen2.5-7B-Instruct"


def run(prompt="hello"):
    return asyncio.run(agent.run_agent(
        model_prompt=prompt,
        model_id=MODEL,
        authorization_header="Bearer x",
        request_id="req-1",
        provider="vllm"
    ))


def test_plain_chat(fake_openai, fake_mcp):
    fake_openai.reply(chat_response(text="hi there", prompt_tokens=12, completion_tokens=4))

    result = run()

    assert result["status"] == "completed"
    assert result["response"] == "hi there"
    assert result["agent_steps"] == 1
    assert result["input_tokens"] == 12
    assert result["output_tokens"] == 4
    assert result["tool_calls"] == []

    body = fake_openai.requests[0]["body"]
    assert body["model"] == MODEL
    names = [t["function"]["name"] for t in body["tools"]]
    assert names == ["get_lambda_configuration", "update_lambda_memory"]


def test_tool_call_then_final_answer(fake_openai, monkeypatch):
    mcp = FakeMcp(results={"get_lambda_configuration": {"MemorySize": 256}}).install(monkeypatch)

    fake_openai.reply(chat_response(
        tool_calls=[{"id": "call_1", "name": "get_lambda_configuration",
                     "arguments": {"function_name": "f"}}],
        prompt_tokens=30, completion_tokens=9
    ))
    fake_openai.reply(chat_response(text="It has 256 MB.", prompt_tokens=50, completion_tokens=6))

    result = run()

    assert result["status"] == "completed"
    assert result["response"] == "It has 256 MB."
    assert result["agent_steps"] == 2
    assert result["input_tokens"] == 80
    assert result["output_tokens"] == 15
    assert mcp.calls == [("get_lambda_configuration", {"function_name": "f"})]
    assert result["tool_calls"][0]["status"] == "success"

    second = fake_openai.requests[1]["body"]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "tool"]
    assert second[1]["tool_calls"][0]["id"] == "call_1"
    assert second[1]["tool_calls"][0]["function"]["arguments"] == '{"function_name": "f"}'
    assert second[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"MemorySize": 256}'
    }


def test_two_tool_calls_in_one_turn(fake_openai, monkeypatch):
    mcp = FakeMcp().install(monkeypatch)

    fake_openai.reply(chat_response(tool_calls=[
        {"id": "a", "name": "get_lambda_configuration", "arguments": {"function_name": "f1"}},
        {"id": "b", "name": "get_lambda_configuration", "arguments": {"function_name": "f2"}},
    ]))
    fake_openai.reply(chat_response(text="done"))

    result = run()

    assert result["status"] == "completed"
    assert len(mcp.calls) == 2

    second = fake_openai.requests[1]["body"]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in second[2:]] == ["a", "b"]


def test_tool_error_goes_back_to_model(fake_openai, monkeypatch):
    FakeMcp(error=RuntimeError("AccessDenied")).install(monkeypatch)

    fake_openai.reply(chat_response(tool_calls=[
        {"id": "a", "name": "get_lambda_configuration", "arguments": {"function_name": "f"}}
    ]))
    fake_openai.reply(chat_response(text="I could not read it."))

    result = run()

    assert result["status"] == "completed"
    assert result["tool_calls"][0]["status"] == "error"
    assert fake_openai.requests[1]["body"]["messages"][2]["content"] == "AccessDenied"


def test_rag_context_is_sent_in_messages(fake_openai, fake_mcp):
    retrieval = {"retrievalResults": [
        {"content": {"text": "Rollbacks need approval from the on-call lead."}}
    ]}
    prompt, metrics = rag.build_rag_context("Who approves rollbacks?", retrieval, time.time())

    fake_openai.reply(chat_response(text="The on-call lead."))

    result = run(prompt)

    assert result["response"] == "The on-call lead."
    sent = fake_openai.requests[0]["body"]["messages"][0]["content"]
    assert "Rollbacks need approval from the on-call lead." in sent
    assert "Who approves rollbacks?" in sent
    assert metrics["retrieved_chunks"] == 1


def test_write_tool_goes_to_approval(fake_openai, monkeypatch):
    mcp = FakeMcp().install(monkeypatch)
    started = []

    monkeypatch.setattr(
        agent, "start_approval",
        lambda request_id, tool, arguments, reason, deadline=None: started.append((tool, arguments, reason)) or "arn:x"
    )

    fake_openai.reply(chat_response(
        text="Raising memory.",
        tool_calls=[{"id": "w1", "name": "update_lambda_memory",
                     "arguments": {"function_name": "f", "memory_size": 512}}]
    ))

    result = run()

    assert result["status"] == "approval_required"
    assert result["approval"]["action"] == {
        "tool": "update_lambda_memory",
        "arguments": {"function_name": "f", "memory_size": 512}
    }
    assert started == [("update_lambda_memory", {"function_name": "f", "memory_size": 512}, "Raising memory.")]
    assert mcp.calls == []


def test_max_steps(fake_openai, fake_mcp):
    for i in range(5):
        fake_openai.reply(chat_response(tool_calls=[
            {"id": f"c{i}", "name": "get_lambda_configuration", "arguments": {"function_name": "f"}}
        ]))

    result = run()

    assert result["status"] == "max_steps_reached"
    assert len(fake_openai.requests) == 5


def test_server_error_propagates(fake_openai, fake_mcp):
    fake_openai.reply({"error": "boom"}, status=500)

    with pytest.raises(VLLMError, match="vllm_unavailable"):
        run()


def test_malformed_tool_arguments_propagate(fake_openai, fake_mcp):
    fake_openai.reply(chat_response(tool_calls=[
        {"id": "a", "name": "get_lambda_configuration", "arguments": '{"function_name":'}
    ]))

    with pytest.raises(VLLMError, match="vllm_invalid_response"):
        run()


def test_default_provider_is_bedrock(monkeypatch, fake_mcp):
    from conftest import FakeBedrock, patch_bedrock

    fake = FakeBedrock([{
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1}
    }])
    patch_bedrock(monkeypatch, fake)

    result = asyncio.run(agent.run_agent("hello", "amazon.nova-micro-v1:0", "Bearer x", "req-1"))

    assert result["response"] == "ok"
    assert len(fake.calls) == 1
