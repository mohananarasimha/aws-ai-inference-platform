// SIMULATED: every response below is a hand-written copy of a shape the gateway
// (gateway/lambda/handler.py), API Gateway or the BFF produces. No live AWS call.
import { describe, expect, it } from "vitest";
import { interpret } from "./interpret";

const ok = (body: unknown, status = 200) => interpret({ kind: "response", status, body, clientMs: 1234 });

describe("successful inference", () => {
  it("shows the answer, backend, latencies and tokens the gateway reported", () => {
    const v = ok({
      request_id: "r1", provider: "bedrock", model_class: "fast", model: "amazon.nova-micro-v1:0",
      response: "Hello.", agent_status: "completed", agent_steps: 1, latency_ms: 870,
      input_tokens: 12, output_tokens: 3, tool_calls: [], rag_enabled: false,
    });
    expect(v.outcome).toBe("success");
    expect(v.answer).toBe("Hello.");
    expect(v.backend).toEqual({ provider: "bedrock", model: "amazon.nova-micro-v1:0", modelClass: "fast" });
    expect(v.clientMs).toBe(1234);
    expect(v.serverMs).toBe(870);
    expect(v.tokens).toEqual({ input: 12, output: 3 });
    expect(v.tools).toEqual([]);
    expect(v.rag?.enabled).toBe(false);
  });

  it("does not invent tokens or tool calls an older gateway did not send", () => {
    const v = ok({ request_id: "r1", provider: "vllm", model: "Qwen/Qwen2.5-7B-Instruct", response: "x", rag_enabled: false });
    expect(v.tokens).toBeUndefined();
    expect(v.tools).toBeNull();
  });
});

describe("authentication failure", () => {
  it("API Gateway's JWT authorizer rejection", () => {
    const v = ok({ message: "Unauthorized" }, 401);
    expect(v.outcome).toBe("error");
    expect(v.errorSource).toBe("api_gateway");
    expect(v.title).toBe("Authentication failed");
  });

  it("Cognito refused the client credentials (reported by the BFF)", () => {
    const v = ok({ source: "bff", error: "token_request_failed", token_status: 400 }, 502);
    expect(v.errorSource).toBe("bff");
    expect(v.detail).toMatch(/Cognito refused/);
  });
});

describe("throttling", () => {
  it("API Gateway's 429", () => {
    const v = ok({ message: "Too Many Requests" }, 429);
    expect(v.title).toBe("Throttled");
    expect(v.errorSource).toBe("api_gateway");
  });
});

describe("timeouts", () => {
  it("the gateway's request budget ran out", () => {
    const v = ok({
      request_id: "r", error: "request_deadline_exceeded", component: "vllm",
      agent_step: 1, elapsed_ms: 23150, budget_ms: 24000,
    }, 504);
    expect(v.title).toBe("Request budget used up");
    expect(v.detail).toContain("in vllm (agent step 1): 23150 ms of a 24000 ms budget");
    expect(v.serverMs).toBe(23150);
  });

  it("one component's own limit, with budget left", () => {
    const v = ok({ request_id: "r", error: "bedrock_timeout", component: "bedrock", agent_step: 2, elapsed_ms: 20100, budget_ms: 24000 }, 504);
    expect(v.title).toBe("Component timeout");
    expect(v.errorCode).toBe("bedrock_timeout");
  });

  it("the BFF stopped waiting for API Gateway", () => {
    const v = ok({ source: "bff", error: "upstream_timeout", timeout_ms: 29000 }, 504);
    expect(v.title).toBe("Timed out waiting for API Gateway");
  });

  it("the browser gave up", () => {
    const v = interpret({ kind: "client_timeout", clientMs: 35001, timeoutMs: 35000 });
    expect(v.errorSource).toBe("browser");
    expect(v.detail).toContain("35 s");
  });

  it("a Lambda hard timeout surfaces as API Gateway's 503", () => {
    const v = ok({ message: "Service Unavailable" }, 503);
    expect(v.errorSource).toBe("api_gateway");
    expect(v.detail).toMatch(/30 s hard timeout/);
  });
});

describe("backend unavailable", () => {
  it("no vLLM NLB (GPU capacity not deployed)", () => {
    const v = ok({
      request_id: "r", provider: "vllm", model: "Qwen/Qwen2.5-7B-Instruct",
      error: "vllm_endpoint_not_found", error_type: "ConfigurationError", http_status: null, latency_ms: 900,
    }, 503);
    expect(v.title).toBe("Backend unavailable");
    expect(v.detail).toMatch(/GPU capacity is probably not deployed/);
    expect(v.backend?.provider).toBe("vllm");
  });

  it("the BFF itself is not running", () => {
    const v = interpret({ kind: "network_error", clientMs: 3, message: "Failed to fetch" });
    expect(v.title).toBe("Network error");
  });
});

describe("RAG", () => {
  it("keeps retrieved evidence separate from the generated answer", () => {
    const v = ok({
      request_id: "r", provider: "bedrock", model: "m", response: "The on-call engineer.",
      rag_enabled: true, retrieved_chunks: 2, retrieval_latency_ms: 310,
      rag_sources: [{ document: "deployment-policy.md", score: 0.7345 }, { document: null, score: null }],
      tool_calls: [],
    });
    expect(v.answer).toBe("The on-call engineer.");
    expect(v.rag).toEqual({
      enabled: true, chunks: 2, retrievalMs: 310,
      sources: [{ document: "deployment-policy.md", score: 0.7345 }, { document: null, score: null }],
    });
  });

  it("reports sources as not available when the gateway does not send them", () => {
    const v = ok({ request_id: "r", response: "x", rag_enabled: true, retrieved_chunks: 1, retrieval_latency_ms: 200 });
    expect(v.rag?.sources).toBeNull();
    expect(v.rag?.chunks).toBe(1);
  });
});

describe("MCP tools", () => {
  it("lists the tool calls the gateway reported", () => {
    const v = ok({
      request_id: "r", provider: "bedrock", model: "m", response: "256 MB.", agent_steps: 2, rag_enabled: false,
      tool_calls: [
        { tool: "get_lambda_configuration", latency_ms: 412, status: "success" },
        { tool: "get_lambda_errors", latency_ms: 15000, status: "error", error_type: "ComponentTimeout" },
      ],
    });
    expect(v.tools).toEqual([
      { tool: "get_lambda_configuration", status: "success", latency_ms: 412, error_type: undefined },
      { tool: "get_lambda_errors", status: "error", latency_ms: 15000, error_type: "ComponentTimeout" },
    ]);
  });
});

describe("approval", () => {
  it("a proposed write is pending, not done", () => {
    const action = { tool: "update_lambda_memory", arguments: { function_name: "f", memory_size: 512 } };
    const v = ok({
      request_id: "r", status: "approval_required", action,
      approval_execution_arn: "arn:aws:states:us-east-1:111122223333:execution:approval:r",
    }, 202);
    expect(v.outcome).toBe("approval");
    expect(v.approval?.action).toEqual(action);
    expect(v.detail).toMatch(/Nothing has changed yet/);
    expect(v.detail).toMatch(/scripts\/approve\.py/);
  });

  it("an approval workflow that could not start is an error", () => {
    const v = ok({ request_id: "r", error: "approval_start_failed" }, 500);
    expect(v.outcome).toBe("error");
    expect(v.detail).toMatch(/could not be started/);
  });
});

describe("rejected requests", () => {
  it("lists the allowed values", () => {
    const v = ok({ request_id: "r", error: "invalid_target", allowed_targets: ["eks", "onprem"] }, 400);
    expect(v.detail).toBe("Unknown vLLM target. Allowed: eks, onprem.");
  });
});
