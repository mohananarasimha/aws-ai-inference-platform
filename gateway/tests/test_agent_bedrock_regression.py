"""
Pins the Bedrock agent path. These tests were run against the code before
the model adapter existed and must keep passing unchanged.
"""

import asyncio

import agent
from conftest import FakeBedrock, FakeMcp, patch_bedrock


def converse_text(text, input_tokens=7, output_tokens=3):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
        "stopReason": "end_turn"
    }


def converse_tool(tool_use_id, name, arguments, text=None):
    content = [{"text": text}] if text else []
    content.append({"toolUse": {
        "toolUseId": tool_use_id,
        "name": name,
        "input": arguments
    }})

    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "usage": {"inputTokens": 20, "outputTokens": 8},
        "stopReason": "tool_use"
    }


def run(**kwargs):
    return asyncio.run(agent.run_agent(
        model_prompt="hello",
        model_id="amazon.nova-micro-v1:0",
        authorization_header="Bearer x",
        request_id="req-1",
        **kwargs
    ))


def test_plain_answer(monkeypatch, fake_mcp):
    fake = FakeBedrock([converse_text("hi there")])
    patch_bedrock(monkeypatch, fake)

    result = run()

    assert result["status"] == "completed"
    assert result["response"] == "hi there"
    assert result["agent_steps"] == 1
    assert result["input_tokens"] == 7
    assert result["output_tokens"] == 3
    assert result["tool_calls"] == []
    assert len(result["model_calls_ms"]) == 1

    (call,) = fake.calls
    assert call["modelId"] == "amazon.nova-micro-v1:0"
    assert call["messages"] == [
        {"role": "user", "content": [{"text": "hello"}]}
    ]
    assert call["inferenceConfig"] == {"maxTokens": 1000, "temperature": 0}

    names = [t["toolSpec"]["name"] for t in call["toolConfig"]["tools"]]
    assert names == ["get_lambda_configuration", "update_lambda_memory"]


def test_tool_loop(monkeypatch):
    mcp = FakeMcp(results={"get_lambda_configuration": {"MemorySize": 256}})
    mcp.install(monkeypatch)

    fake = FakeBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
        converse_text("It has 256 MB."),
    ])
    patch_bedrock(monkeypatch, fake)

    result = run()

    assert result["status"] == "completed"
    assert result["response"] == "It has 256 MB."
    assert result["agent_steps"] == 2
    assert result["input_tokens"] == 27
    assert result["output_tokens"] == 11
    assert mcp.calls == [("get_lambda_configuration", {"function_name": "f"})]
    assert [c["tool"] for c in result["tool_calls"]] == ["get_lambda_configuration"]
    assert result["tool_calls"][0]["status"] == "success"

    second_messages = fake.calls[1]["messages"]
    assert second_messages[1]["role"] == "assistant"
    assert second_messages[2] == {
        "role": "user",
        "content": [{"toolResult": {
            "toolUseId": "t1",
            "content": [{"json": {"MemorySize": 256}}],
            "status": "success"
        }}]
    }


def test_tool_error_is_returned_to_model(monkeypatch):
    mcp = FakeMcp(error=RuntimeError("AccessDenied")).install(monkeypatch)

    fake = FakeBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
        converse_text("I could not read it."),
    ])
    patch_bedrock(monkeypatch, fake)

    result = run()

    assert result["status"] == "completed"
    assert result["tool_calls"][0]["status"] == "error"
    assert result["tool_calls"][0]["error_type"] == "RuntimeError"
    assert fake.calls[1]["messages"][2]["content"][0]["toolResult"] == {
        "toolUseId": "t1",
        "content": [{"text": "AccessDenied"}],
        "status": "error"
    }


def test_write_tool_goes_to_approval_not_mcp(monkeypatch):
    mcp = FakeMcp().install(monkeypatch)
    started = []

    def fake_start_approval(request_id, tool, arguments, reason, deadline=None):
        started.append((request_id, tool, arguments, reason))
        return "arn:execution"

    monkeypatch.setattr(agent, "start_approval", fake_start_approval)

    fake = FakeBedrock([
        converse_tool(
            "t1", "update_lambda_memory",
            {"function_name": "f", "memory_size": 512},
            text="Raising memory."
        ),
    ])
    patch_bedrock(monkeypatch, fake)

    result = run()

    assert result["status"] == "approval_required"
    assert result["approval"] == {
        "action": {
            "tool": "update_lambda_memory",
            "arguments": {"function_name": "f", "memory_size": 512}
        },
        "execution_arn": "arn:execution"
    }
    assert started == [(
        "req-1", "update_lambda_memory",
        {"function_name": "f", "memory_size": 512},
        "Raising memory."
    )]
    assert mcp.calls == []


def test_max_steps(monkeypatch, fake_mcp):
    fake = FakeBedrock([
        converse_tool(f"t{i}", "get_lambda_configuration", {"function_name": "f"})
        for i in range(5)
    ])
    patch_bedrock(monkeypatch, fake)

    result = run()

    assert result["status"] == "max_steps_reached"
    assert result["agent_steps"] == 5
    assert len(fake.calls) == 5
