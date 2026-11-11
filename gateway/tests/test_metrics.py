"""
CloudWatch EMF metrics: one record per finished request, low-cardinality
dimensions only, and never a reason for a request to fail.
"""

import json

import pytest

import deadline as deadline_module
import handler
import metrics
from conftest import FakeMcp, patch_bedrock
from fake_openai_server import chat_response
from metrics import build_request_metrics
from test_deadline import Clock, SlowBedrock, invoke, slow_mcp
from test_handler import converse_text, converse_tool

ALLOWED_DIMENSIONS = {"Provider", "Status", "Component", "Type", "ErrorType"}


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(deadline_module, "now", fake.now)
    return fake


def emf_records(capsys):
    records = []

    for line in capsys.readouterr().out.splitlines():
        if line.startswith("{") and '"_aws"' in line:
            records.append(json.loads(line))

    return records


def only_record(capsys):
    records = emf_records(capsys)
    assert len(records) == 1
    return records[0]


def assert_valid_emf(record):
    """Every metric and dimension a directive names exists in the record."""

    for directive in record["_aws"]["CloudWatchMetrics"]:
        assert directive["Namespace"] == "EnterpriseAIPlatform/Gateway"

        for metric in directive["Metrics"]:
            assert isinstance(record[metric["Name"]], (int, float))

        for dimension_set in directive["Dimensions"]:
            assert set(dimension_set) <= ALLOWED_DIMENSIONS

            for name in dimension_set:
                assert isinstance(record[name], str)


def metric_names(record):
    return {
        metric["Name"]
        for directive in record["_aws"]["CloudWatchMetrics"]
        for metric in directive["Metrics"]
    }


# ---- the record builder -------------------------------------------------------------------

@pytest.mark.parametrize("code, status", [(200, "success"), (202, "approval_required"),
                                          (504, "timeout"), (500, "error"), (502, "error")])
def test_status_comes_from_the_response_code(code, status):
    record = build_request_metrics("bedrock", code, {"request_id": "r", "error": "x"}, 1200)

    assert record["Status"] == status
    assert record["Provider"] == "bedrock"
    assert record["RequestCount"] == 1
    assert record["RequestLatency"] == 1200
    assert_valid_emf(record)


@pytest.mark.parametrize("code", [400, 401, 404, 429])
def test_client_errors_are_not_counted(code):
    assert build_request_metrics("bedrock", code, {"error": "bad"}, 5) is None


def test_requests_that_never_reached_a_provider_are_not_counted():
    assert build_request_metrics(None, 500, {"error": "x"}, 5) is None
    assert build_request_metrics("something-else", 200, {}, 5) is None


def test_model_latency_only_when_a_model_ran():
    assert "ModelLatency" not in metric_names(build_request_metrics("vllm", 500, {"error": "e"}, 9))

    record = build_request_metrics("vllm", 200, {"request_id": "r"}, 900, model_latency_ms=640)

    assert record["ModelLatency"] == 640
    assert_valid_emf(record)


def test_deadline_timeout_dimensions():
    record = build_request_metrics(
        "bedrock", 504,
        {"error": "request_deadline_exceeded", "component": "agent_loop"}, 24000
    )

    assert (record["Component"], record["Type"]) == ("agent_loop", "deadline")
    assert metric_names(record) == {"RequestCount", "RequestLatency", "TimeoutCount"}
    assert_valid_emf(record)


def test_component_timeout_dimensions():
    record = build_request_metrics("bedrock", 504, {"error": "rag_timeout", "component": "rag"}, 9000)

    assert (record["Component"], record["Type"]) == ("rag", "component")


def test_vllm_timeout_component_is_derived_from_the_code():
    record = build_request_metrics("vllm", 504, {"error": "vllm_timeout"}, 20000)

    assert (record["Component"], record["Type"]) == ("vllm", "component")


def test_error_type_dimension():
    record = build_request_metrics("vllm", 502, {"error": "vllm_unavailable"}, 300)

    assert record["ErrorType"] == "vllm_unavailable"
    assert metric_names(record) == {"RequestCount", "RequestLatency", "ErrorCount"}
    assert_valid_emf(record)


@pytest.mark.parametrize("value", ["Some free text, with spaces!", "x" * 200, None, 7, "UPPER"])
def test_unexpected_values_are_folded_to_other(value):
    record = build_request_metrics("bedrock", 500, {"error": value}, 1)

    assert record["ErrorType"] == "other"


def test_request_id_is_a_property_not_a_dimension():
    record = build_request_metrics("bedrock", 200, {"request_id": "abc-123"}, 1)

    assert record["request_id"] == "abc-123"

    for directive in record["_aws"]["CloudWatchMetrics"]:
        assert "request_id" not in [d for s in directive["Dimensions"] for d in s]


# ---- handler: exactly one record per finished request -----------------------------------------

def test_bedrock_success_emits_one_record(monkeypatch, clock, capsys):
    patch_bedrock(monkeypatch, SlowBedrock([converse_text("hi")], clock, cost=2))
    FakeMcp().install(monkeypatch)

    status, body = invoke({"prompt": "hello"})
    record = only_record(capsys)

    assert status == 200
    assert (record["Provider"], record["Status"]) == ("bedrock", "success")
    assert record["request_id"] == body["request_id"]
    assert metric_names(record) == {"RequestCount", "RequestLatency", "ModelLatency"}
    assert_valid_emf(record)


def test_vllm_success_emits_vllm_provider(fake_openai, fake_mcp, capsys):
    fake_openai.reply(chat_response(text="hi"))

    status, _ = invoke({"provider": "vllm", "prompt": "hello"})
    record = only_record(capsys)

    assert status == 200
    assert (record["Provider"], record["Status"]) == ("vllm", "success")


def test_deadline_exceeded_emits_a_timeout_record(monkeypatch, clock, capsys):
    patch_bedrock(monkeypatch, SlowBedrock([
        converse_tool("t1", "get_lambda_configuration", {"function_name": "f"}),
        converse_text("never")
    ], clock, cost=10))
    FakeMcp().install(monkeypatch)
    slow_mcp(monkeypatch, clock, 14)

    status, _ = invoke({"prompt": "check"})
    record = only_record(capsys)

    assert status == 504
    assert (record["Status"], record["Type"], record["Component"]) == ("timeout", "deadline", "agent_loop")
    assert record["TimeoutCount"] == 1
    assert_valid_emf(record)


def test_component_timeout_emits_the_component(monkeypatch, clock, capsys):
    patch_bedrock(monkeypatch, SlowBedrock([], clock, fail_on=1))
    FakeMcp().install(monkeypatch)

    status, _ = invoke({"prompt": "hello"})
    record = only_record(capsys)

    assert status == 504
    assert (record["Component"], record["Type"]) == ("bedrock", "component")


def test_failed_retrieval_emits_an_error_record(monkeypatch, capsys):
    def failing(prompt, deadline=None):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(handler, "retrieve", failing)

    status, _ = invoke({"prompt": "q", "rag": True})
    record = only_record(capsys)

    assert status == 500
    assert (record["Status"], record["ErrorType"]) == ("error", "retrieval_failed")
    assert "secret internal detail" not in json.dumps(record)


def test_approval_required_is_its_own_status(monkeypatch, clock, capsys):
    import agent

    monkeypatch.setattr(agent, "start_approval", lambda *a: "arn:execution")
    patch_bedrock(monkeypatch, SlowBedrock([
        converse_tool("w1", "update_lambda_memory", {"function_name": "f", "memory_size": 512})
    ], clock))
    FakeMcp().install(monkeypatch)

    status, _ = invoke({"prompt": "raise memory"})

    assert status == 202
    assert only_record(capsys)["Status"] == "approval_required"


@pytest.mark.parametrize("request_body, headers_missing", [
    ({"prompt": "x", "provider": "nope"}, False),
    ({"prompt": "x", "model_class": "nope"}, False),
    ({}, False),
    ({"prompt": "x"}, True),
])
def test_client_errors_emit_nothing(request_body, headers_missing, capsys):
    event = {"headers": {} if headers_missing else {"authorization": "Bearer x"},
             "body": json.dumps(request_body)}

    response = handler.lambda_handler(event, None)

    assert response["statusCode"] in (400, 401)
    assert emf_records(capsys) == []


def test_diagnostic_path_emits_nothing(fake_openai, fake_mcp, capsys):
    fake_openai.reply({"data": [{"id": "m"}]})

    invoke({"provider": "vllm_test"})

    assert emf_records(capsys) == []


def test_a_metrics_failure_never_breaks_the_request(monkeypatch, clock):
    patch_bedrock(monkeypatch, SlowBedrock([converse_text("hi")], clock))
    FakeMcp().install(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("emf broke")

    monkeypatch.setattr(metrics, "build_request_metrics", boom)

    status, body = invoke({"prompt": "hello"})

    assert status == 200
    assert body["response"] == "hi"


def test_emf_never_contains_the_prompt_or_credentials(monkeypatch, clock, capsys):
    secret = "SECRET-PROMPT-XYZ"
    patch_bedrock(monkeypatch, SlowBedrock([], clock, fail_on=1))
    FakeMcp().install(monkeypatch)

    invoke({"prompt": secret})
    record = only_record(capsys)

    assert secret not in json.dumps(record)
    assert "Bearer" not in json.dumps(record)
