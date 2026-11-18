// Turns one /invoke exchange into what the page shows. Pure functions only.
//
// Every field read here exists in gateway/lambda/handler.py (or in API Gateway's
// own error bodies, or the BFF's). Anything the backend did not send is shown as
// "not reported", never filled in.

export type InvokeRequest = {
  prompt: string;
  provider: "bedrock" | "vllm";
  target?: "eks" | "onprem";
  model_class?: "fast" | "quality";
  rag?: boolean;
};

export type Exchange =
  | { kind: "response"; status: number; body: unknown; clientMs: number }
  | { kind: "client_timeout"; clientMs: number; timeoutMs: number }
  | { kind: "network_error"; clientMs: number; message: string };

export type ToolCall = { tool: string; status: string; latency_ms?: number; error_type?: string };
export type RagSource = { document: string | null; score: number | null };

export type View = {
  outcome: "success" | "approval" | "error";
  title: string;
  detail?: string;
  answer?: string;
  requestId?: string;
  httpStatus?: number;
  errorCode?: string;
  errorSource?: "gateway" | "api_gateway" | "bff" | "browser";
  backend?: { provider?: string; model?: string; modelClass?: string; target?: string };
  clientMs: number;
  serverMs?: number;
  tokens?: { input: number; output: number };
  agentSteps?: number;
  rag?: { enabled: boolean; chunks?: number; retrievalMs?: number; sources: RagSource[] | null };
  tools: ToolCall[] | null;
  approval?: { action: unknown; executionArn?: string };
};

type Body = Record<string, unknown>;

const obj = (v: unknown): Body => (v && typeof v === "object" && !Array.isArray(v) ? (v as Body) : {});
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const str = (v: unknown) => (typeof v === "string" ? v : undefined);

const GATEWAY_ERRORS: Record<string, string> = {
  vllm_endpoint_not_found:
    "vLLM on EKS is not running: no active internal NLB was found. GPU capacity is probably not deployed.",
  vllm_endpoint_ambiguous: "More than one vLLM load balancer matched; the gateway refuses to guess.",
  vllm_onprem_not_configured: "The on-prem vLLM target is not configured on the gateway (ONPREM_VLLM_BASE_URL).",
  vllm_unavailable: "The vLLM backend could not be reached or returned a server error.",
  vllm_request_failed: "The vLLM backend rejected the request (4xx).",
  vllm_invalid_response: "The vLLM backend returned a response the gateway could not parse.",
  vllm_timeout: "The vLLM call hit its own time limit while request budget remained.",
  bedrock_timeout: "The Bedrock call hit its own time limit while request budget remained.",
  rag_timeout: "Knowledge Base retrieval timed out.",
  mcp_timeout: "An MCP tool call timed out.",
  approval_timeout: "Starting the approval workflow timed out.",
  retrieval_failed: "Knowledge Base retrieval failed.",
  agent_invocation_failed: "The agent failed with an unexpected error (for Bedrock, any non-timeout error).",
  approval_start_failed: "The write was proposed but the approval workflow could not be started.",
  invalid_provider: "Unknown provider.",
  invalid_target: "Unknown vLLM target.",
  invalid_model_class: "Unknown model class.",
  "prompt is required": "A prompt is required.",
  "authorization header missing": "The request reached the Lambda without an Authorization header.",
};

const BFF_ERRORS: Record<string, string> = {
  bff_not_configured: "The local BFF is missing settings. Copy ui/.env.example to ui/.env.local and fill it in.",
  token_request_failed: "Cognito refused the client-credentials request (check the client ID, secret and scope).",
  token_endpoint_unreachable: "The Cognito token endpoint could not be reached.",
  upstream_timeout: "The BFF stopped waiting for API Gateway.",
  upstream_unreachable: "API Gateway could not be reached from the BFF.",
  forbidden_origin: "The BFF only accepts requests from this local page.",
  forbidden_host: "The BFF only accepts requests addressed to localhost.",
  invalid_json: "The request body was not valid JSON.",
  request_too_large: "The prompt is too large.",
};

export function interpret(ex: Exchange): View {
  if (ex.kind === "client_timeout") {
    return {
      outcome: "error", errorSource: "browser", clientMs: ex.clientMs, tools: null,
      title: "No response in time",
      detail: `The browser gave up after ${Math.round(ex.timeoutMs / 1000)} s. The gateway's own budget is 24 s and API Gateway stops at 30 s, so the request may still have finished server-side.`,
    };
  }
  if (ex.kind === "network_error") {
    return {
      outcome: "error", errorSource: "browser", clientMs: ex.clientMs, tools: null,
      title: "Network error", detail: `The local BFF could not be reached (${ex.message}). Is \`npm run bff\` running?`,
    };
  }

  const body = obj(ex.body);
  const base = {
    clientMs: ex.clientMs,
    httpStatus: ex.status,
    requestId: str(body.request_id),
  };

  if (ex.status === 200) {
    const tools = Array.isArray(body.tool_calls)
      ? body.tool_calls.map((t) => obj(t)).map((t) => ({
          tool: str(t.tool) ?? "unknown",
          status: str(t.status) ?? "unknown",
          latency_ms: num(t.latency_ms),
          error_type: str(t.error_type),
        }))
      : null;
    const ragEnabled = body.rag_enabled === true;
    return {
      ...base,
      outcome: "success",
      title: "Answer",
      answer: str(body.response) ?? "",
      backend: { provider: str(body.provider), model: str(body.model), modelClass: str(body.model_class) },
      serverMs: num(body.latency_ms),
      tokens:
        num(body.input_tokens) !== undefined && num(body.output_tokens) !== undefined
          ? { input: num(body.input_tokens)!, output: num(body.output_tokens)! }
          : undefined,
      agentSteps: num(body.agent_steps),
      rag: {
        enabled: ragEnabled,
        chunks: num(body.retrieved_chunks),
        retrievalMs: num(body.retrieval_latency_ms),
        sources: Array.isArray(body.rag_sources)
          ? body.rag_sources.map((s) => obj(s)).map((s) => ({
              document: str(s.document) ?? null,
              score: num(s.score) ?? null,
            }))
          : null,
      },
      tools,
    };
  }

  if (ex.status === 202 && body.status === "approval_required") {
    return {
      ...base,
      outcome: "approval",
      title: "Write proposed: waiting for human approval",
      detail:
        "Nothing has changed yet. The proposal is queued in the Step Functions approval workflow. Approve or reject it from a terminal with scripts/approve.py; this page cannot approve and does not poll the execution.",
      approval: { action: body.action, executionArn: str(body.approval_execution_arn) },
      tools: null,
    };
  }

  const error: View = { ...base, outcome: "error", tools: null, title: `HTTP ${ex.status}` };

  if (body.source === "bff") {
    const code = str(body.error) ?? "unknown";
    return {
      ...error, errorSource: "bff", errorCode: code,
      title: code === "upstream_timeout" ? "Timed out waiting for API Gateway" : "Local BFF error",
      detail: BFF_ERRORS[code] ?? code,
    };
  }

  // API Gateway's own responses carry {"message": ...} and no request_id.
  const message = str(body.message);
  if (message !== undefined && body.error === undefined) {
    const known: Record<number, [string, string]> = {
      401: ["Authentication failed", "API Gateway rejected the access token (JWT authorizer)."],
      403: ["Forbidden", "API Gateway refused the request (token scope or route)."],
      429: ["Throttled", "API Gateway's stage rate limit was hit. Wait a moment and retry."],
      503: [
        "Gateway unavailable",
        "API Gateway got no usable answer from the Lambda. This is what a Lambda that reaches its 30 s hard timeout looks like.",
      ],
      504: ["Gateway timeout", "API Gateway's integration timed out."],
    };
    const [title, detail] = known[ex.status] ?? [`HTTP ${ex.status}`, message];
    return { ...error, errorSource: "api_gateway", errorCode: message, title, detail };
  }

  const code = str(body.error) ?? "unknown_error";
  const view: View = { ...error, errorSource: "gateway", errorCode: code, detail: GATEWAY_ERRORS[code] ?? code };

  if (ex.status === 504) {
    const component = str(body.component);
    const elapsed = num(body.elapsed_ms);
    const budget = num(body.budget_ms);
    view.title = code === "request_deadline_exceeded" ? "Request budget used up" : "Component timeout";
    const where = component ? ` in ${component}${body.agent_step ? ` (agent step ${body.agent_step})` : ""}` : "";
    const timing = elapsed !== undefined && budget !== undefined ? `: ${elapsed} ms of a ${budget} ms budget` : "";
    view.detail =
      (code === "request_deadline_exceeded" ? "The gateway stopped before the Lambda limit" : GATEWAY_ERRORS[code] ?? code) +
      where + timing + ".";
    view.serverMs = elapsed;
  } else if (ex.status === 503 || ex.status === 502) {
    view.title = "Backend unavailable";
  } else if (ex.status === 400) {
    view.title = "Rejected request";
    const allowed = body.allowed_providers ?? body.allowed_targets ?? body.allowed_model_classes;
    if (Array.isArray(allowed)) view.detail += ` Allowed: ${allowed.join(", ")}.`;
  } else if (ex.status === 401) {
    view.title = "Authentication failed";
  }

  if (str(body.provider) || str(body.model)) view.backend = { provider: str(body.provider), model: str(body.model) };
  return view;
}
