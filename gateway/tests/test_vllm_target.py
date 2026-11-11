"""
Per-request inference target: provider=vllm with target omitted/eks uses NLB
discovery, target=onprem uses ONPREM_VLLM_BASE_URL, VLLM_BASE_URL overrides
both, and the caller can only name a logical target, never a URL.
"""

import json

import pytest

import handler
import vllm
from fake_openai_server import FakeOpenAIServer, chat_response
from vllm import VLLMError

EKS_URL = "http://eks-nlb.example:8000"
ONPREM_URL = "http://onprem.example:18082"


@pytest.fixture
def discovered(monkeypatch):
    calls = []
    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: calls.append(1) or EKS_URL)
    return calls


def test_target_omitted_uses_eks_discovery(monkeypatch, discovered):
    monkeypatch.setenv("ONPREM_VLLM_BASE_URL", ONPREM_URL)

    assert vllm.resolve_base_url() == EKS_URL
    assert len(discovered) == 1


def test_target_eks_uses_eks_discovery(monkeypatch, discovered):
    monkeypatch.setenv("ONPREM_VLLM_BASE_URL", ONPREM_URL)

    assert vllm.resolve_base_url(None, "eks") == EKS_URL
    assert len(discovered) == 1


def test_target_onprem_uses_the_environment_url_and_skips_discovery(monkeypatch, discovered):
    monkeypatch.setenv("ONPREM_VLLM_BASE_URL", ONPREM_URL)

    assert vllm.resolve_base_url(None, "onprem") == ONPREM_URL
    assert discovered == []


def test_global_override_wins_over_every_target(monkeypatch, discovered):
    monkeypatch.setenv("VLLM_BASE_URL", "http://override.example:1")
    monkeypatch.setenv("ONPREM_VLLM_BASE_URL", ONPREM_URL)

    for target in ("eks", "onprem"):
        assert vllm.resolve_base_url(None, target) == "http://override.example:1"

    assert discovered == []


def test_invalid_target_is_a_controlled_error_not_an_eks_fallback(monkeypatch, discovered):
    monkeypatch.setenv("ONPREM_VLLM_BASE_URL", ONPREM_URL)

    with pytest.raises(VLLMError, match="vllm_invalid_target"):
        vllm.resolve_base_url(None, "foo")

    assert discovered == []


def test_onprem_without_configuration_is_a_configuration_error(discovered):
    with pytest.raises(VLLMError, match="vllm_onprem_not_configured"):
        vllm.resolve_base_url(None, "onprem")

    assert discovered == []
    assert vllm.ERROR_STATUS["vllm_onprem_not_configured"] == 503
    assert vllm.ERROR_STATUS["vllm_invalid_target"] == 400


def test_onprem_failure_keeps_the_cached_eks_address(monkeypatch, discovered):
    assert vllm.resolve_base_url() == EKS_URL
    monkeypatch.setenv("ONPREM_VLLM_BASE_URL", "http://127.0.0.1:9")

    with pytest.raises(VLLMError, match="vllm_unavailable"):
        vllm.request_chat({"model": "m", "messages": []}, None, "onprem")

    assert vllm._discovered_base_url == EKS_URL


# ---- through the handler ---------------------------------------------------------

def invoke(body):
    response = handler.lambda_handler(
        {"headers": {"authorization": "Bearer x"}, "body": json.dumps(body)},
        None
    )
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def two_servers(monkeypatch, fake_mcp):
    """A fake EKS endpoint (found by discovery) and a fake on-prem endpoint."""
    eks, onprem = FakeOpenAIServer().start(), FakeOpenAIServer().start()
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.setenv("ONPREM_VLLM_BASE_URL", onprem.url)
    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: eks.url)
    eks.reply(chat_response(text="from eks"))
    onprem.reply(chat_response(text="from onprem"))
    yield eks, onprem
    eks.stop()
    onprem.stop()


@pytest.mark.parametrize("extra", [{}, {"target": "eks"}])
def test_request_without_target_or_with_eks_reaches_eks(two_servers, extra):
    eks, onprem = two_servers

    status, body = invoke({"provider": "vllm", "prompt": "hi", **extra})

    assert status == 200
    assert body["response"] == "from eks"
    assert len(eks.requests) == 1 and onprem.requests == []


def test_request_with_target_onprem_reaches_onprem_only(two_servers):
    eks, onprem = two_servers

    status, body = invoke({"provider": "vllm", "target": "onprem", "prompt": "hi"})

    assert status == 200
    assert body["response"] == "from onprem"
    assert len(onprem.requests) == 1 and eks.requests == []


def test_request_with_an_unknown_target_is_a_400(two_servers):
    eks, onprem = two_servers

    status, body = invoke({"provider": "vllm", "target": "foo", "prompt": "hi"})

    assert status == 400
    assert body["error"] == "invalid_target"
    assert body["allowed_targets"] == ["eks", "onprem"]
    assert eks.requests == [] and onprem.requests == []


def test_a_url_is_not_accepted_as_a_target(two_servers):
    eks, onprem = two_servers

    status, body = invoke({"provider": "vllm", "target": onprem.url, "prompt": "hi"})

    assert status == 400 and body["error"] == "invalid_target"
    assert eks.requests == [] and onprem.requests == []


def test_onprem_request_without_configuration_is_a_503(monkeypatch, fake_mcp):
    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: pytest.fail("must not discover"))

    status, body = invoke({"provider": "vllm", "target": "onprem", "prompt": "hi"})

    assert status == 503
    assert body["error"] == "vllm_onprem_not_configured"
    assert body["error_type"] == "ConfigurationError"
