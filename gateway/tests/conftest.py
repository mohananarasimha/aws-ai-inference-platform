import copy
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

LAMBDA_DIR = os.environ.get(
    "LAMBDA_DIR",
    str(Path(__file__).resolve().parents[1] / "lambda")
)

sys.path.insert(0, LAMBDA_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

from fake_openai_server import FakeOpenAIServer  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_discovery(monkeypatch):
    """Tests never call AWS: discovery finds nothing unless a test replaces it."""
    import vllm

    def not_found(timeout=None):
        raise vllm.VLLMError("vllm_endpoint_not_found")

    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("ONPREM_VLLM_BASE_URL", raising=False)
    monkeypatch.setattr(vllm, "discover_base_url", not_found)
    monkeypatch.setattr(vllm, "_discovered_base_url", None)


@pytest.fixture
def fake_openai(monkeypatch):
    server = FakeOpenAIServer().start()
    monkeypatch.setenv("VLLM_BASE_URL", server.url)
    yield server
    server.stop()


class FakeBedrock:
    """Scripted stand-in for the boto3 bedrock-runtime client."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


def patch_bedrock(monkeypatch, fake):
    """Replace the boto3 client factory; fake.timeouts records each call's timeout."""
    import model

    fake.timeouts = []

    def client(service, timeout):
        fake.timeouts.append(timeout)
        return fake

    monkeypatch.setattr(model, "aws_client", client)


class FakeMcp:
    """Replaces the AgentCore MCP calls made by agent.run_agent."""

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

    def __init__(self, results=None, error=None, wrap_errors=False):
        self.results = results or {}
        self.error = error
        self.wrap_errors = wrap_errors
        self.calls = []
        self.sessions_opened = 0

    def install(self, monkeypatch):
        import agent

        @asynccontextmanager
        async def open_session(authorization_header, metrics=None, reuse=True, deadline=None):
            self.sessions_opened += 1

            try:
                yield object()
            except Exception as exc:
                # The real session runs in an anyio task group, which wraps
                # whatever the loop body raises.
                if not self.wrap_errors:
                    raise

                raise ExceptionGroup(
                    "unhandled errors in a TaskGroup",
                    [ExceptionGroup("unhandled errors in a TaskGroup", [exc])]
                ) from None

        async def list_tools(session, metrics=None, deadline=None):
            return list(self.TOOLS)

        async def call_tool(session, name, arguments, authorization_header=None, metrics=None, deadline=None):
            self.calls.append((name, arguments))

            if self.error:
                raise self.error

            return self.results.get(name, {"ok": True})

        monkeypatch.setattr(agent, "open_mcp_session", open_session)
        monkeypatch.setattr(agent, "list_bedrock_tools", list_tools)
        monkeypatch.setattr(agent, "call_mcp_tool", call_tool)

        return self


@pytest.fixture
def fake_mcp(monkeypatch):
    return FakeMcp().install(monkeypatch)
