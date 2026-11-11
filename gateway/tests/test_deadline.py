"""
End-to-end request budget: every downstream call gets
min(component maximum, time left), and nothing starts once the budget is gone.

Time is a fake clock that the fakes advance, so no test really sleeps
(except the two that cancel a real asyncio wait for 0.2 s).
"""

import asyncio
import copy
import json

import pytest
from botocore.exceptions import ReadTimeoutError

import agent
import clients
import deadline as deadline_module
import handler
import mcp_client
import model
import rag
import vllm
from config import (
    BEDROCK_MAX_TIMEOUT_SECONDS,
    LAMBDA_RESERVE_SECONDS,
    MAX_QUEUED_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    VLLM_MAX_TIMEOUT_SECONDS,
)
from conftest import FakeBedrock, FakeMcp, patch_bedrock
from deadline import ComponentTimeout, DeadlineExceeded, RequestDeadline, request_budget
from fake_openai_server import chat_response
from test_handler import RETRIEVAL, converse_text, converse_tool

BUDGET = REQUEST_TIMEOUT_SECONDS


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(deadline_module, "now", fake.now)
    return fake


def invoke(body, event_extra=None, context=None):
    event = {
        "headers": {"authorization": "Bearer x"},
        "body": json.dumps(body),
        **(event_extra or {})
    }

    response = handler.lambda_handler(event, context)

    return response["statusCode"], json.loads(response["body"])


class SlowBedrock(FakeBedrock):
    """Bedrock stand-in whose calls take fake time, and can time out."""

    def __init__(self, responses, clock, cost=0, fail_on=None):
        super().__init__(responses)
        self.clock = clock
        self.cost = cost
        self.fail_on = fail_on

    def converse(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        self.clock.advance(self.cost)

        if self.fail_on == len(self.calls):
            raise ReadTimeoutError(endpoint_url="https://secret-url", error="slow")

        return self.responses.pop(0)


def slow_mcp(monkeypatch, clock, seconds, error=None):
    """MCP tool calls that take fake time; records the deadline each one received."""
    seen = []

    async def call_tool(session, name, arguments, authorization_header=None,
                        metrics=None, deadline=None):
        seen.append(deadline.remaining())
        clock.advance(seconds)

        if error:
            raise error

        return {"ok": True}

    monkeypatch.setattr(agent, "call_mcp_tool", call_tool)

    return seen


@pytest.fixture
def urlopen_timeouts(monkeypatch):
    seen = []
    real = vllm.urllib.request.urlopen

    def spy(request, timeout=None):
        seen.append(timeout)
        return real(request, timeout=timeout)

    monkeypatch.setattr(vllm.urllib.request, "urlopen", spy)

    return seen


# ---- the budget object ------------------------------------------------------------------

def test_timeout_is_min_of_component_maximum_and_remaining(clock):
    d = RequestDeadline(20)

    assert d.timeout(8, "x") == 8
    clock.advance(15)
    assert d.timeout(8, "x") == 5


def test_no_call_starts_with_under_a_second_left(clock):
    d = RequestDeadline(10)
    clock.advance(9.5)

    assert d.expired()

    with pytest.raises(DeadlineExceeded) as excinfo:
        d.timeout(8, "bedrock")

    assert excinfo.value.component == "bedrock"


def test_budget_is_capped_by_what_the_lambda_has_left():
    class Context:
        def get_remaining_time_in_millis(self):
            return 15000

    assert request_budget(Context()).budget == 15 - LAMBDA_RESERVE_SECONDS
    assert request_budget(None).budget == BUDGET


def test_time_before_the_handler_ran_is_taken_off_the_budget():
    assert request_budget(None, queued_seconds=7).budget == BUDGET - 7
    assert request_budget(None, queued_seconds=999).budget == BUDGET - MAX_QUEUED_SECONDS


def test_queued_seconds_reads_the_api_gateway_timestamp(monkeypatch):
    monkeypatch.setattr(handler.time, "time", lambda: 5000.0)

    assert handler.queued_seconds({"requestContext": {"timeEpoch": 4994000}}) == 6
    assert handler.queued_seconds({"requestContext": {"timeEpoch": 6000000}}) == 0
    assert handler.queued_seconds({}) == 0


def test_cold_start_time_shrinks_the_budget_seen_by_the_handler(monkeypatch, clock):
    monkeypatch.setattr(handler.time, "time", lambda: 5000.0)
    fake = SlowBedrock([converse_text("hi")], clock)
    patch_bedrock(monkeypatch, fake)
    FakeMcp().install(monkeypatch)

    status, body = invoke(
        {"prompt": "hello"},
        event_extra={"requestContext": {"timeEpoch": 4992000}}
    )

    assert status == 200
    # 8 s were already spent before the handler ran: 16 s left, under the 20 s maximum.
    assert fake.timeouts == [min(BEDROCK_MAX_TIMEOUT_SECONDS, BUDGET - 8)]


def test_aws_client_timeouts_add_up_to_the_budget(monkeypatch):
    seen = []
    monkeypatch.setattr(clients, "_clients", {})
    monkeypatch.setattr(clients.boto3, "client", lambda name, **kw: seen.append(kw["config"]) or object())

    clients.aws_client("bedrock-runtime", 9.7)

    config = seen[0]
    assert config.connect_timeout + config.read_timeout == 9
    assert config.retries == {"total_max_attempts": 1}


# ---- 1/2. plain request inside the budget -------------------------------------------------

def test_plain_bedrock_request_within_budget(monkeypatch, clock, capsys):
    fake = SlowBedrock([converse_text("hi")], clock, cost=2)
    patch_bedrock(monkeypatch, fake)
    FakeMcp().install(monkeypatch)

    status, body = invoke({"prompt": "hello"})

    assert status == 200
    assert fake.timeouts == [BEDROCK_MAX_TIMEOUT_SECONDS]

    log = next(
        json.loads(line) for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and "request_budget_ms" in line
    )
    assert log["request_budget_ms"] == BUDGET * 1000
    assert log["remaining_budget_ms"] == (BUDGET - 2) * 1000


def test_plain_vllm_request_within_budget(fake_openai, fake_mcp, clock, urlopen_timeouts):
    fake_openai.reply(chat_response(text="hi"))

    status, body = invoke({"provider": "vllm", "prompt": "hello"})

    assert status == 200
    assert urlopen_timeouts == [VLLM_MAX_TIMEOUT_SECONDS]


# ---- 3. RAG uses part of the budget ---------------------------------------------------------

class SlowRetrieve:
    def __init__(self, clock, seconds):
        self.clock = clock
        self.seconds = seconds

    def retrieve(self, **kwargs):
        self.clock.advance(self.seconds)
        return RETRIEVAL


def test_rag_time_is_taken_off_what_the_model_gets(monkeypatch, clock):
    rag_timeouts = []
    kb = SlowRetrieve(clock, 6)
    monkeypatch.setattr(rag, "aws_client", lambda service, timeout: rag_timeouts.append(timeout) or kb)
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb")
    fake = SlowBedrock([converse_text("answer")], clock)
    patch_bedrock(monkeypatch, fake)
    FakeMcp().install(monkeypatch)

    status, body = invoke({"prompt": "q", "rag": True})

    assert status == 200
    assert rag_timeouts == [8]
    assert fake.timeouts == [BUDGET - 6]


def test_rag_time_is_taken_off_what_vllm_gets(monkeypatch, fake_openai, fake_mcp, clock, urlopen_timeouts):
    monkeypatch.setattr(rag, "aws_client", lambda service, timeout: SlowRetrieve(clock, 6))
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb")
    fake_openai.reply(chat_response(text="answer"))

    status, body = invoke({"provider": "vllm", "prompt": "q", "rag": True})

    assert status == 200
    assert urlopen_timeouts == [min(VLLM_MAX_TIMEOUT_SECONDS, BUDGET - 6)]


def test_rag_timeout_is_a_controlled_504(monkeypatch, clock):
    class Boom:
        def retrieve(self, **kwargs):
            raise ReadTimeoutError(endpoint_url="https://secret-url", error="slow")

    monkeypatch.setattr(rag, "aws_client", lambda service, timeout: Boom())
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb")

    status, body = invoke({"prompt": "q", "rag": True})

    assert status == 504
    assert body["error"] == "rag_timeout"
    assert body["component"] == "rag"


# ---- 4. a tool call uses part of the budget ---------------------------------------------------

def test_next_model_call_gets_less_after_a_slow_tool(monkeypatch, clock):
    fake = SlowBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
        converse_text("done")
    ], clock, cost=2)
    patch_bedrock(monkeypatch, fake)
    FakeMcp().install(monkeypatch)
    seen = slow_mcp(monkeypatch, clock, 7)

    status, body = invoke({"prompt": "check f"})

    assert status == 200
    # t=0: 24 left -> 20. Model 2 s + tool 7 s = 9 s spent -> 15 left.
    assert fake.timeouts == [20, BUDGET - 9]
    assert seen == [BUDGET - 2]


# ---- 5. a component times out while budget remains ----------------------------------------------

@pytest.mark.parametrize("wrap", [False, True], ids=["plain", "task-group-wrapped"])
def test_bedrock_timeout_before_the_deadline(monkeypatch, clock, wrap):
    fake = SlowBedrock([], clock, cost=3, fail_on=1)
    patch_bedrock(monkeypatch, fake)
    FakeMcp(wrap_errors=wrap).install(monkeypatch)

    status, body = invoke({"prompt": "hello"})

    assert status == 504
    assert body["error"] == "bedrock_timeout"
    assert body["component"] == "bedrock"
    assert body["agent_step"] == 1
    assert body["elapsed_ms"] == 3000
    assert body["budget_ms"] == BUDGET * 1000


def test_vllm_timeout_stays_distinguishable(monkeypatch, fake_openai, fake_mcp, clock):
    monkeypatch.setattr(vllm, "VLLM_MAX_TIMEOUT_SECONDS", 0.3)
    fake_openai.reply(chat_response(text="late"), delay=1.5)

    status, body = invoke({"provider": "vllm", "prompt": "hello"})

    assert status == 504
    assert body["error"] == "vllm_timeout"


def test_vllm_timeout_that_used_up_the_budget_is_a_deadline_error(monkeypatch, fake_mcp, clock):
    def burn(payload, deadline=None, target="eks"):
        clock.advance(BUDGET)
        raise vllm.VLLMError("vllm_timeout")

    monkeypatch.setattr(model, "request_chat", burn)

    status, body = invoke({"provider": "vllm", "prompt": "hello"})

    assert status == 504
    assert body["error"] == "request_deadline_exceeded"
    assert body["component"] == "vllm"


# ---- 6. MCP times out before the deadline -------------------------------------------------------

def test_slow_mcp_tool_is_cancelled_after_its_maximum(monkeypatch):
    monkeypatch.setattr(mcp_client, "MCP_MAX_TIMEOUT_SECONDS", 0.2)

    class Session:
        calls = 0

        async def call_tool(self, name, arguments):
            Session.calls += 1
            await asyncio.sleep(5)

    with pytest.raises(ComponentTimeout) as excinfo:
        asyncio.run(mcp_client.call_mcp_tool(
            Session(), "t", {}, "Bearer x",
            {"mcp_session_reused": True}, RequestDeadline(24)
        ))

    assert excinfo.value.component == "mcp"
    assert Session.calls == 1  # a timeout is not retried on a fresh session


def test_mcp_operation_never_gets_more_than_the_budget_left(monkeypatch, clock):
    seen = []

    class FakeScope:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mcp_client.anyio, "fail_after", lambda seconds: seen.append(seconds) or FakeScope())

    class Session:
        async def call_tool(self, name, arguments):
            class Result:
                structured_content = {"ok": True}
            return Result()

    d = RequestDeadline(24)
    clock.advance(16)
    asyncio.run(mcp_client.call_mcp_tool(Session(), "t", {}, None, None, d))

    assert seen == [8]  # min(MCP maximum 15, 8 left)


def test_mcp_http_timeouts_come_from_the_remaining_budget(monkeypatch, clock):
    captured = {}

    class Stop(Exception):
        pass

    def fake_client(**kwargs):
        captured.update(kwargs)
        raise Stop

    monkeypatch.setattr(mcp_client.httpx2, "AsyncClient", fake_client)
    monkeypatch.setenv("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    d = RequestDeadline(24)
    clock.advance(21)

    async def open_session():
        async with mcp_client.open_mcp_session("Bearer x", deadline=d):
            pass

    with pytest.raises(Stop):
        asyncio.run(open_session())

    timeout = captured["timeout"]
    assert timeout.read == 3
    assert timeout.connect == 3
    assert timeout.write == 3
    assert timeout.pool == 3


def test_mcp_timeout_is_reported_to_the_model_and_the_loop_continues(monkeypatch, clock):
    fake = SlowBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
        converse_text("could not read it")
    ], clock)
    patch_bedrock(monkeypatch, fake)
    FakeMcp().install(monkeypatch)
    slow_mcp(monkeypatch, clock, 4, error=ComponentTimeout("mcp"))

    status, body = invoke({"prompt": "check f"})

    assert status == 200
    assert body["agent_steps"] == 2
    tool_result = fake.calls[1]["messages"][-1]["content"][0]["toolResult"]
    assert tool_result["status"] == "error"
    assert tool_result["content"][0]["text"] == "mcp_timeout"
    assert fake.timeouts == [20, BUDGET - 4]


# ---- 7/8. the deadline runs out between steps ------------------------------------------------------

@pytest.mark.parametrize("wrap", [False, True], ids=["plain", "task-group-wrapped"])
def test_deadline_between_steps_stops_before_another_model_call(monkeypatch, clock, wrap):
    fake = SlowBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
        converse_text("never reached")
    ], clock, cost=10)
    patch_bedrock(monkeypatch, fake)
    mcp = FakeMcp(wrap_errors=wrap).install(monkeypatch)
    slow_mcp(monkeypatch, clock, 14)

    status, body = invoke({"prompt": "check f"})

    assert status == 504
    assert body["error"] == "request_deadline_exceeded"
    assert body["component"] == "agent_loop"
    assert body["agent_step"] == 2
    assert len(fake.calls) == 1  # step 2 never called the model
    assert fake.timeouts == [20]


def test_deadline_before_the_tool_call_skips_the_tool(monkeypatch, clock):
    fake = SlowBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"})
    ], clock, cost=BUDGET)
    patch_bedrock(monkeypatch, fake)
    FakeMcp().install(monkeypatch)
    seen = slow_mcp(monkeypatch, clock, 1)

    status, body = invoke({"prompt": "check f"})

    assert status == 504
    assert body["component"] == "mcp_tool"
    assert seen == []


def test_deadline_before_approval_does_not_start_it(monkeypatch, clock):
    started = []
    monkeypatch.setattr(agent, "start_approval", lambda *a: started.append(a) or "arn")
    fake = SlowBedrock([
        converse_tool("w1", "update_lambda_memory", {"function_name": "f", "memory_size": 512})
    ], clock, cost=BUDGET - 0.5)
    patch_bedrock(monkeypatch, fake)
    FakeMcp().install(monkeypatch)

    status, body = invoke({"prompt": "raise memory"})

    assert status == 504
    assert started == []


# ---- 9. MAX_AGENT_STEPS is independent --------------------------------------------------------------

def test_max_steps_still_ends_the_loop_with_budget_left(monkeypatch, clock):
    monkeypatch.setattr(agent, "MAX_AGENT_STEPS", 2)
    fake = SlowBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
        converse_tool("t2", "get_lambda_configuration", {"function_name": "f"})
    ], clock, cost=1)
    patch_bedrock(monkeypatch, fake)
    FakeMcp().install(monkeypatch)

    status, body = invoke({"prompt": "loop"})

    assert status == 200
    assert body["agent_status"] == "max_steps_reached"
    assert len(fake.calls) == 2


# ---- 10. the diagnostic keeps its own short timeout ----------------------------------------------------

def test_connectivity_diagnostic_keeps_its_own_timeout(fake_openai, clock, urlopen_timeouts):
    fake_openai.reply({"data": [{"id": "m"}]})

    status, body = invoke({"provider": "vllm_test"})

    assert status == 200
    assert urlopen_timeouts == [vllm.DIAGNOSTIC_TIMEOUT_SECONDS]
    assert vllm.DIAGNOSTIC_TIMEOUT_SECONDS < vllm.VLLM_MAX_TIMEOUT_SECONDS


# ---- discovery shares the budget -----------------------------------------------------------------------

def test_vllm_discovery_gets_what_is_left_of_the_budget(monkeypatch, clock):
    seen = []
    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: seen.append(timeout) or "http://127.0.0.1:9")

    d = RequestDeadline(24)
    clock.advance(21)
    vllm.resolve_base_url(d)

    assert seen == [3]


# ---- 11. nothing sensitive in timeout errors --------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["bedrock_timeout", "deadline", "rag_timeout"])
def test_timeout_responses_and_logs_do_not_leak(monkeypatch, clock, capsys, scenario):
    secret = "SECRET-PROMPT-XYZ"
    FakeMcp().install(monkeypatch)

    if scenario == "bedrock_timeout":
        patch_bedrock(monkeypatch, SlowBedrock([], clock, fail_on=1))
    elif scenario == "deadline":
        patch_bedrock(monkeypatch, SlowBedrock([
            converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
            converse_text("x")
        ], clock, cost=BUDGET - 0.5))
    else:
        class Boom:
            def retrieve(self, **kwargs):
                raise ReadTimeoutError(endpoint_url="https://secret-url", error="slow")

        monkeypatch.setattr(rag, "aws_client", lambda service, timeout: Boom())
        monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb")

    status, body = invoke({"prompt": secret, "rag": scenario == "rag_timeout"})

    out = capsys.readouterr().out
    assert status == 504
    for text in (json.dumps(body), out):
        assert secret not in text
        assert "secret-url" not in text
        assert "Bearer" not in text
        assert "Traceback" not in text
