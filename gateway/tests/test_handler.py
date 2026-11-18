"""
Handler-level tests: request routing, RAG, providers and error mapping.
Both providers must go through agent.run_agent.
"""

import json

import pytest

import handler
import vllm
from config import VLLM_MODEL
from conftest import FakeBedrock, FakeMcp, patch_bedrock
from fake_openai_server import chat_response

RETRIEVAL = {"retrievalResults": [
    {"content": {"text": "Rollbacks need approval from the on-call lead."}}
]}


def invoke(body, headers=None):
    headers = {"authorization": "Bearer x"} if headers is None else headers

    response = handler.lambda_handler(
        {"headers": headers, "body": json.dumps(body)},
        None
    )

    return response["statusCode"], json.loads(response["body"])


def converse_text(text, input_tokens=7, output_tokens=3):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens}
    }


def converse_tool(tool_use_id, name, arguments):
    return {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": arguments}}
        ]}},
        "usage": {"inputTokens": 20, "outputTokens": 8}
    }


@pytest.fixture
def retrieval(monkeypatch):
    calls = []

    def fake_retrieve(prompt, deadline=None):
        calls.append(prompt)
        return RETRIEVAL

    monkeypatch.setattr(handler, "retrieve", fake_retrieve)
    return calls


# ---- Bedrock (existing behavior) ---------------------------------------------

def test_bedrock_plain_chat(monkeypatch, fake_mcp):
    fake = FakeBedrock([converse_text("hi there")])
    patch_bedrock(monkeypatch, fake)

    status, body = invoke({"prompt": "hello"})

    assert status == 200
    assert body["response"] == "hi there"
    assert body["provider"] == "bedrock"
    assert body["model_class"] == "fast"
    assert body["model"] == "amazon.nova-micro-v1:0"
    assert body["agent_status"] == "completed"
    assert body["input_tokens"] == 7
    assert fake.calls[0]["modelId"] == "amazon.nova-micro-v1:0"
    assert fake_mcp.sessions_opened == 1


def test_bedrock_quality_model_class(monkeypatch, fake_mcp):
    fake = FakeBedrock([converse_text("ok")])
    patch_bedrock(monkeypatch, fake)

    status, body = invoke({"prompt": "hello", "model_class": "quality"})

    assert status == 200
    assert fake.calls[0]["modelId"] == "amazon.nova-lite-v1:0"


def test_bedrock_rag(monkeypatch, fake_mcp, retrieval):
    fake = FakeBedrock([converse_text("The on-call lead.")])
    patch_bedrock(monkeypatch, fake)

    status, body = invoke({"prompt": "Who approves rollbacks?", "rag": True})

    assert status == 200
    assert retrieval == ["Who approves rollbacks?"]
    assert body["rag_enabled"] is True
    assert body["retrieved_chunks"] == 1

    sent = fake.calls[0]["messages"][0]["content"][0]["text"]
    assert "Rollbacks need approval from the on-call lead." in sent
    assert "Who approves rollbacks?" in sent


def test_bedrock_mcp_tool_loop(monkeypatch):
    mcp = FakeMcp(results={"get_lambda_configuration": {"MemorySize": 256}}).install(monkeypatch)
    fake = FakeBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
        converse_text("256 MB."),
    ])
    patch_bedrock(monkeypatch, fake)

    status, body = invoke({"prompt": "memory of f?"})

    assert status == 200
    assert body["response"] == "256 MB."
    assert body["agent_steps"] == 2
    assert mcp.calls == [("get_lambda_configuration", {"function_name": "f"})]


def test_bedrock_write_tool_needs_approval(monkeypatch):
    import agent

    mcp = FakeMcp().install(monkeypatch)
    monkeypatch.setattr(agent, "start_approval", lambda *a: "arn:execution")
    patch_bedrock(monkeypatch, FakeBedrock([converse_tool(
        "w1", "update_lambda_memory", {"function_name": "f", "memory_size": 512}
    )]))

    status, body = invoke({"prompt": "raise memory"})

    assert status == 202
    assert body["status"] == "approval_required"
    assert body["approval_execution_arn"] == "arn:execution"
    assert mcp.calls == []


def test_bedrock_unexpected_error_is_500(monkeypatch, fake_mcp):
    class Boom:
        def converse(self, **kwargs):
            raise RuntimeError("bedrock down")

    patch_bedrock(monkeypatch, Boom())

    status, body = invoke({"prompt": "hello"})

    assert status == 500
    assert body["error"] == "agent_invocation_failed"


# ---- vLLM through the same agent loop ---------------------------------------------

def test_vllm_plain_chat_runs_through_agent(fake_openai, fake_mcp, monkeypatch):
    patch_bedrock(monkeypatch, FakeBedrock([]))
    fake_openai.reply(chat_response(text="hi there", prompt_tokens=12, completion_tokens=4))

    status, body = invoke({"provider": "vllm", "prompt": "hello"})

    assert status == 200
    assert body["response"] == "hi there"
    assert body["provider"] == "vllm"
    assert body["model"] == VLLM_MODEL
    assert "model_class" not in body
    assert body["agent_status"] == "completed"
    assert body["agent_steps"] == 1
    assert body["input_tokens"] == 12
    assert body["output_tokens"] == 4

    # Same orchestration as Bedrock: MCP session opened, tools sent to the model.
    assert fake_mcp.sessions_opened == 1
    sent = fake_openai.requests[0]["body"]
    assert sent["model"] == VLLM_MODEL
    assert [t["function"]["name"] for t in sent["tools"]] == [
        "get_lambda_configuration", "update_lambda_memory"
    ]


def test_vllm_model_comes_from_config(fake_openai, fake_mcp, monkeypatch):
    monkeypatch.setattr(handler, "VLLM_MODEL", "other/model")
    fake_openai.reply(chat_response(text="ok"))

    status, body = invoke({"provider": "vllm", "prompt": "hello"})

    assert body["model"] == "other/model"
    assert fake_openai.requests[0]["body"]["model"] == "other/model"


def test_vllm_ignores_model_class(fake_openai, fake_mcp):
    fake_openai.reply(chat_response(text="ok"))

    status, body = invoke({"provider": "vllm", "prompt": "hello", "model_class": "nonsense"})

    assert status == 200


def test_vllm_rag_sends_augmented_context(fake_openai, fake_mcp, retrieval, monkeypatch):
    fake = FakeBedrock([])
    patch_bedrock(monkeypatch, fake)
    fake_openai.reply(chat_response(text="The on-call lead."))

    status, body = invoke({"provider": "vllm", "prompt": "Who approves rollbacks?", "rag": True})

    assert status == 200
    assert body["response"] == "The on-call lead."
    assert body["rag_enabled"] is True
    assert body["retrieved_chunks"] == 1
    assert retrieval == ["Who approves rollbacks?"]

    sent = fake_openai.requests[0]["body"]["messages"][0]["content"]
    assert "Rollbacks need approval from the on-call lead." in sent
    assert "Who approves rollbacks?" in sent
    assert fake.calls == []


def test_vllm_tool_call_then_final_answer(fake_openai, monkeypatch):
    mcp = FakeMcp(results={"get_lambda_configuration": {"MemorySize": 256}}).install(monkeypatch)
    fake_openai.reply(chat_response(tool_calls=[
        {"id": "call_1", "name": "get_lambda_configuration", "arguments": {"function_name": "f"}}
    ]))
    fake_openai.reply(chat_response(text="It has 256 MB."))

    status, body = invoke({"provider": "vllm", "prompt": "memory of f?"})

    assert status == 200
    assert body["response"] == "It has 256 MB."
    assert body["agent_steps"] == 2
    assert mcp.calls == [("get_lambda_configuration", {"function_name": "f"})]
    assert fake_openai.requests[1]["body"]["messages"][2] == {
        "role": "tool", "tool_call_id": "call_1", "content": '{"MemorySize": 256}'
    }


def test_vllm_rag_and_tools_together(fake_openai, retrieval, monkeypatch):
    mcp = FakeMcp(results={"get_lambda_configuration": {"MemorySize": 256}}).install(monkeypatch)
    fake_openai.reply(chat_response(tool_calls=[
        {"id": "c1", "name": "get_lambda_configuration", "arguments": {"function_name": "f"}}
    ]))
    fake_openai.reply(chat_response(text="done"))

    status, body = invoke({"provider": "vllm", "prompt": "Check f", "rag": True})

    assert status == 200
    assert body["retrieved_chunks"] == 1
    assert len(mcp.calls) == 1
    first_user = fake_openai.requests[1]["body"]["messages"][0]["content"]
    assert "Rollbacks need approval from the on-call lead." in first_user


def test_vllm_write_tool_needs_approval(fake_openai, monkeypatch):
    import agent

    mcp = FakeMcp().install(monkeypatch)
    started = []
    monkeypatch.setattr(agent, "start_approval", lambda *a: started.append(a) or "arn:execution")
    fake_openai.reply(chat_response(
        text="Raising memory.",
        tool_calls=[{"id": "w1", "name": "update_lambda_memory",
                     "arguments": {"function_name": "f", "memory_size": 512}}]
    ))

    status, body = invoke({"provider": "vllm", "prompt": "raise memory"})

    assert status == 202
    assert body["status"] == "approval_required"
    assert body["action"] == {
        "tool": "update_lambda_memory",
        "arguments": {"function_name": "f", "memory_size": 512}
    }
    assert len(started) == 1
    assert mcp.calls == []


def test_vllm_max_steps(fake_openai, fake_mcp):
    for i in range(5):
        fake_openai.reply(chat_response(tool_calls=[
            {"id": f"c{i}", "name": "get_lambda_configuration", "arguments": {"function_name": "f"}}
        ]))

    status, body = invoke({"provider": "vllm", "prompt": "loop"})

    assert status == 200
    assert body["agent_status"] == "max_steps_reached"
    assert len(fake_openai.requests) == 5


# ---- vLLM error mapping (still controlled, also through exception groups) ----------

@pytest.mark.parametrize("wrap", [False, True], ids=["plain", "task-group-wrapped"])
class TestVllmErrors:

    def install(self, monkeypatch, wrap):
        return FakeMcp(wrap_errors=wrap).install(monkeypatch)

    def test_server_error(self, fake_openai, monkeypatch, wrap):
        self.install(monkeypatch, wrap)
        fake_openai.reply({"error": "boom"}, status=500)

        status, body = invoke({"provider": "vllm", "prompt": "hello"})

        assert status == 502
        assert body["error"] == "vllm_unavailable"
        assert body["error_type"] == "HTTPError"
        assert body["http_status"] == 500
        assert body["provider"] == "vllm"
        assert body["model"] == VLLM_MODEL

    def test_client_error(self, fake_openai, monkeypatch, wrap):
        self.install(monkeypatch, wrap)
        fake_openai.reply({"error": "bad"}, status=400)

        status, body = invoke({"provider": "vllm", "prompt": "hello"})

        assert status == 502
        assert body["error"] == "vllm_request_failed"
        assert body["http_status"] == 400

    def test_invalid_response(self, fake_openai, monkeypatch, wrap):
        self.install(monkeypatch, wrap)
        fake_openai.reply({"choices": []})

        status, body = invoke({"provider": "vllm", "prompt": "hello"})

        assert status == 502
        assert body["error"] == "vllm_invalid_response"
        assert body["error_type"] == "InvalidResponse"

    def test_malformed_tool_arguments(self, fake_openai, monkeypatch, wrap):
        self.install(monkeypatch, wrap)
        fake_openai.reply(chat_response(tool_calls=[
            {"id": "a", "name": "get_lambda_configuration", "arguments": '{"function_name":'}
        ]))

        status, body = invoke({"provider": "vllm", "prompt": "hello"})

        assert status == 502
        assert body["error"] == "vllm_invalid_response"

    def test_timeout(self, fake_openai, monkeypatch, wrap):
        self.install(monkeypatch, wrap)
        monkeypatch.setattr(vllm, "VLLM_MAX_TIMEOUT_SECONDS", 0.3)
        fake_openai.reply(chat_response(text="late"), delay=1.5)

        status, body = invoke({"provider": "vllm", "prompt": "hello"})

        assert status == 504
        assert body["error"] == "vllm_timeout"

    def test_unreachable(self, monkeypatch, wrap):
        self.install(monkeypatch, wrap)
        monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:9")

        status, body = invoke({"provider": "vllm", "prompt": "hello"})

        assert status == 502
        assert body["error"] == "vllm_unavailable"

    def test_endpoint_not_found(self, monkeypatch, wrap):
        self.install(monkeypatch, wrap)

        status, body = invoke({"provider": "vllm", "prompt": "hello"})

        assert status == 503
        assert body["error"] == "vllm_endpoint_not_found"
        assert body["error_type"] == "ConfigurationError"

    def test_error_response_never_contains_prompt(self, fake_openai, monkeypatch, wrap):
        self.install(monkeypatch, wrap)
        fake_openai.reply({"error": "boom"}, status=500)

        status, body = invoke({"provider": "vllm", "prompt": "SECRET-PROMPT"})

        assert "SECRET-PROMPT" not in json.dumps(body)


def test_unexpected_error_inside_task_group_stays_500(fake_openai, monkeypatch):
    import model

    FakeMcp(wrap_errors=True).install(monkeypatch)

    def broken(payload):
        raise RuntimeError("not a vLLM error")

    monkeypatch.setattr(model, "request_chat", broken)

    status, body = invoke({"provider": "vllm", "prompt": "hello"})

    assert status == 500
    assert body["error"] == "agent_invocation_failed"


# ---- request validation ---------------------------------------------------------------

def test_invalid_provider(fake_mcp):
    status, body = invoke({"provider": "openai", "prompt": "hello"})

    assert status == 400
    assert body["error"] == "invalid_provider"
    assert body["allowed_providers"] == ["bedrock", "vllm"]


def test_invalid_model_class_still_rejected_for_bedrock(fake_mcp):
    status, body = invoke({"prompt": "hello", "model_class": "nonsense"})

    assert status == 400
    assert body["error"] == "invalid_model_class"


def test_missing_authorization(fake_mcp):
    status, body = invoke({"prompt": "hello"}, headers={})

    assert status == 401


def test_missing_prompt(fake_mcp):
    status, body = invoke({"provider": "vllm"})

    assert status == 400
    assert body["error"] == "prompt is required"


@pytest.mark.parametrize("provider", [None, "vllm"])
def test_retrieval_failure_is_controlled(monkeypatch, fake_mcp, provider):
    def failing_retrieve(prompt, deadline=None):
        raise RuntimeError("kb down")

    monkeypatch.setattr(handler, "retrieve", failing_retrieve)

    request = {"prompt": "hello", "rag": True}

    if provider:
        request["provider"] = provider

    status, body = invoke(request)

    assert status == 500
    assert body["error"] == "retrieval_failed"


# ---- diagnostic path stays separate -------------------------------------------------------

def test_vllm_connectivity_diagnostic_is_separate(fake_openai, fake_mcp):
    fake_openai.reply({"data": [{"id": VLLM_MODEL}]})

    status, body = invoke({"provider": "vllm_test"})

    assert status == 200
    assert body["status"] == "ok"
    assert body["models"] == [VLLM_MODEL]
    assert fake_openai.requests[0]["method"] == "GET"
    assert fake_openai.requests[0]["path"] == "/v1/models"
    assert fake_mcp.sessions_opened == 0


def test_vllm_connectivity_diagnostic_reports_missing_endpoint(fake_mcp):
    status, body = invoke({"provider": "vllm_test"})

    assert status == 503
    assert body["error"] == "vllm_endpoint_not_found"


# ---- fields the playground UI reads -------------------------------------------

def test_rag_sources_name_the_document_and_score_only(monkeypatch, fake_mcp):
    monkeypatch.setattr(handler, "retrieve", lambda prompt, deadline=None: {"retrievalResults": [
        {"content": {"text": "Rollbacks need approval."},
         "location": {"type": "S3", "s3Location": {"uri": "s3://private-bucket/docs/deployment-policy.md"}},
         "score": 0.734512},
        {"content": {"text": "No location in this one."}},
        {"content": {}},
    ]})
    patch_bedrock(monkeypatch, FakeBedrock([converse_text("The on-call lead.")]))

    status, body = invoke({"prompt": "Who approves rollbacks?", "rag": True})

    assert status == 200
    assert body["retrieved_chunks"] == 2
    assert body["rag_sources"] == [
        {"document": "deployment-policy.md", "score": 0.7345},
        {"document": None, "score": None},
    ]
    assert "private-bucket" not in json.dumps(body)


def test_tool_calls_report_name_status_and_latency_but_no_data(monkeypatch):
    FakeMcp(results={"get_lambda_configuration": {"MemorySize": 256}}).install(monkeypatch)
    patch_bedrock(monkeypatch, FakeBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "secret-fn"}),
        converse_text("256 MB."),
    ]))

    status, body = invoke({"prompt": "memory of secret-fn?"})

    assert status == 200
    assert [(c["tool"], c["status"]) for c in body["tool_calls"]] == [("get_lambda_configuration", "success")]
    assert isinstance(body["tool_calls"][0]["latency_ms"], int)
    assert "MemorySize" not in json.dumps(body["tool_calls"])
    assert "secret-fn" not in json.dumps(body["tool_calls"])


def test_plain_request_reports_no_tool_calls_and_no_rag_fields(monkeypatch, fake_mcp):
    patch_bedrock(monkeypatch, FakeBedrock([converse_text("hi")]))

    status, body = invoke({"prompt": "hello"})

    assert status == 200
    assert body["tool_calls"] == []
    assert body["rag_enabled"] is False and "rag_sources" not in body
