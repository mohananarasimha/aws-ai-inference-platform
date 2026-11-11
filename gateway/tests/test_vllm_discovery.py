import pytest

import vllm
from vllm import VLLMError

ARN = "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/net/k8s-default-vllm-1/abc"
DNS = "k8s-default-vllm-1-abc.elb.us-east-1.amazonaws.com"


class FakeTagging:
    def __init__(self, arns, error=None):
        self.arns = arns
        self.error = error
        self.calls = []

    def get_resources(self, **kwargs):
        self.calls.append(kwargs)

        if self.error:
            raise self.error

        return {"ResourceTagMappingList": [{"ResourceARN": a} for a in self.arns]}


class FakeElbv2:
    def __init__(self, state="active"):
        self.state = state
        self.calls = []

    def describe_load_balancers(self, **kwargs):
        self.calls.append(kwargs)

        return {"LoadBalancers": [{"DNSName": DNS, "State": {"Code": self.state}}]}


def install(monkeypatch, tagging, elbv2):
    clients = {"resourcegroupstaggingapi": tagging, "elbv2": elbv2}

    monkeypatch.setattr(vllm.boto3, "client", lambda name, **kw: clients[name])
    return clients


# Captured at import, before conftest's autouse fixture replaces it per test.
REAL_DISCOVER = vllm.discover_base_url


@pytest.fixture
def real_discovery(monkeypatch):
    monkeypatch.setattr(vllm, "discover_base_url", REAL_DISCOVER)


def test_discovers_nlb_by_service_tag(monkeypatch, real_discovery):
    tagging, elbv2 = FakeTagging([ARN]), FakeElbv2()
    install(monkeypatch, tagging, elbv2)

    assert vllm.discover_base_url() == f"http://{DNS}:8000"
    assert tagging.calls[0]["TagFilters"] == [
        {"Key": "service.k8s.aws/stack", "Values": ["default/vllm"]}
    ]
    assert elbv2.calls[0] == {"LoadBalancerArns": [ARN]}


def test_port_comes_from_environment(monkeypatch, real_discovery):
    install(monkeypatch, FakeTagging([ARN]), FakeElbv2())
    monkeypatch.setenv("VLLM_PORT", "9000")

    assert vllm.discover_base_url().endswith(":9000")


def test_no_nlb_is_endpoint_not_found(monkeypatch, real_discovery):
    install(monkeypatch, FakeTagging([]), FakeElbv2())

    with pytest.raises(VLLMError, match="vllm_endpoint_not_found"):
        vllm.discover_base_url()


def test_two_nlbs_is_ambiguous(monkeypatch, real_discovery):
    install(monkeypatch, FakeTagging([ARN, ARN + "2"]), FakeElbv2())

    with pytest.raises(VLLMError, match="vllm_endpoint_ambiguous"):
        vllm.discover_base_url()


def test_provisioning_nlb_is_not_found(monkeypatch, real_discovery):
    install(monkeypatch, FakeTagging([ARN]), FakeElbv2(state="provisioning"))

    with pytest.raises(VLLMError, match="vllm_endpoint_not_found"):
        vllm.discover_base_url()


def test_aws_api_failure_is_discovery_failed(monkeypatch, real_discovery):
    install(monkeypatch, FakeTagging([], error=TimeoutError("router down")), FakeElbv2())

    with pytest.raises(VLLMError, match="vllm_discovery_failed") as excinfo:
        vllm.discover_base_url()

    assert isinstance(excinfo.value.__cause__, TimeoutError)


def test_override_skips_discovery(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://override:1")
    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: pytest.fail("discovered"))

    assert vllm.resolve_base_url() == "http://override:1"


def test_discovered_url_is_cached_per_container(monkeypatch):
    calls = []
    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: calls.append(1) or "http://a:8000")

    assert vllm.resolve_base_url() == "http://a:8000"
    assert vllm.resolve_base_url() == "http://a:8000"
    assert len(calls) == 1


def test_connection_failure_drops_cached_url(monkeypatch):
    urls = iter(["http://127.0.0.1:9", "http://127.0.0.1:10"])
    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: next(urls))

    with pytest.raises(VLLMError, match="vllm_unavailable"):
        vllm.request_chat({"model": "m", "messages": []})

    assert vllm._discovered_base_url is None
    assert vllm.resolve_base_url() == "http://127.0.0.1:10"


def test_failed_discovery_is_not_cached(monkeypatch):
    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: (_ for _ in ()).throw(VLLMError("vllm_endpoint_not_found")))

    with pytest.raises(VLLMError):
        vllm.resolve_base_url()

    monkeypatch.setattr(vllm, "discover_base_url", lambda timeout=None: "http://later:8000")

    assert vllm.resolve_base_url() == "http://later:8000"
