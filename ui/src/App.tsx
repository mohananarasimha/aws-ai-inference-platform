import { useState } from "react";
import { invoke } from "./api";
import { interpret, type InvokeRequest, type View } from "./interpret";

type Demo = { id: string; label: string; prompt: string; rag: boolean; note: string };

// Each demo is an ordinary /invoke request; the gateway decides nothing more
// than it would for any other caller.
const DEMOS: Demo[] = [
  {
    id: "inference",
    label: "Inference",
    prompt: "In two sentences, what does a Kubernetes readiness probe do?",
    rag: false,
    note: "A plain model call on the backend you select.",
  },
  {
    id: "rag",
    label: "RAG",
    prompt: "Who is responsible for initiating a production rollback, and when must it happen?",
    rag: true,
    note: "Retrieves from the Bedrock Knowledge Base (documents in data/) first, then answers.",
  },
  {
    id: "mcp",
    label: "MCP tool",
    prompt: "Show the memory size and runtime of the Lambda function enterprise-ai-platform-dev-gateway.",
    rag: false,
    note: "The model may call the read-only MCP tools on AgentCore. Whether it calls one is the model's choice.",
  },
  {
    id: "approval",
    label: "Approval",
    prompt: "Set the memory of the Lambda function enterprise-ai-platform-dev-gateway to 512 MB.",
    rag: false,
    note:
      "The model can only propose this write. It runs only if a human approves it with scripts/approve.py. Approving really changes the function (Terraform resets it on the next apply); reject to leave it unchanged.",
  },
];

export function App() {
  const [demo, setDemo] = useState<Demo>(DEMOS[0]);
  const [prompt, setPrompt] = useState(DEMOS[0].prompt);
  const [provider, setProvider] = useState<"bedrock" | "vllm">("bedrock");
  const [target, setTarget] = useState<"eks" | "onprem">("eks");
  const [modelClass, setModelClass] = useState<"fast" | "quality">("fast");
  const [rag, setRag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ view: View; request: InvokeRequest; raw: unknown } | null>(null);

  function pick(d: Demo) {
    setDemo(d);
    setPrompt(d.prompt);
    setRag(d.rag);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!prompt.trim() || busy) return;
    const request: InvokeRequest =
      provider === "vllm" ? { prompt, provider, target, rag } : { prompt, provider, model_class: modelClass, rag };
    setBusy(true);
    setResult(null);
    const exchange = await invoke(request);
    setResult({ view: interpret(exchange), request, raw: exchange.kind === "response" ? exchange.body : exchange });
    setBusy(false);
  }

  return (
    <main>
      <header>
        <h1>Inference Playground</h1>
        <p className="muted">
          Sends requests to the platform's existing <code>POST /invoke</code> API through a local proxy that holds the
          Cognito credentials. The backend is whatever you select; nothing is chosen or retried automatically.
        </p>
      </header>

      <form onSubmit={submit}>
        <div className="tabs" role="tablist">
          {DEMOS.map((d) => (
            <button key={d.id} type="button" role="tab" aria-selected={d.id === demo.id} onClick={() => pick(d)}>
              {d.label}
            </button>
          ))}
        </div>
        <p className="note">{demo.note}</p>

        <div className="controls">
          <label>
            Provider
            <select value={provider} onChange={(e) => setProvider(e.target.value as "bedrock" | "vllm")}>
              <option value="bedrock">bedrock</option>
              <option value="vllm">vllm</option>
            </select>
          </label>
          {provider === "vllm" ? (
            <label>
              Target
              <select value={target} onChange={(e) => setTarget(e.target.value as "eks" | "onprem")}>
                <option value="eks">eks</option>
                <option value="onprem">onprem</option>
              </select>
            </label>
          ) : (
            <label>
              Model class
              <select value={modelClass} onChange={(e) => setModelClass(e.target.value as "fast" | "quality")}>
                <option value="fast">fast (Nova Micro)</option>
                <option value="quality">quality (Nova Lite)</option>
              </select>
            </label>
          )}
          <label className="check">
            <input type="checkbox" checked={rag} onChange={(e) => setRag(e.target.checked)} /> RAG
          </label>
        </div>

        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={4} aria-label="Prompt" />
        <button type="submit" disabled={busy || !prompt.trim()}>
          {busy ? "Waiting for the gateway…" : "Send"}
        </button>
        {busy && <p className="muted">The gateway answers within its 24 s budget or returns a timeout error.</p>}
      </form>

      {result && <Result view={result.view} request={result.request} raw={result.raw} />}
    </main>
  );
}

function Result({ view, request, raw }: { view: View; request: InvokeRequest; raw: unknown }) {
  return (
    <section className={`result ${view.outcome}`} aria-live="polite">
      <h2>{view.title}</h2>
      {view.detail && <p>{view.detail}</p>}

      <dl className="metrics">
        <dt>Status</dt>
        <dd>
          {view.httpStatus ?? "no response"}
          {view.errorCode && <> · {view.errorCode}</>}
          {view.errorSource && <> · from {view.errorSource.replace("_", " ")}</>}
        </dd>
        <dt>Backend</dt>
        <dd>
          {view.backend?.provider ?? request.provider}
          {view.backend?.model && <> · {view.backend.model}</>}
          {request.provider === "vllm" && <> · target {request.target} (requested)</>}
        </dd>
        <dt>End-to-end latency</dt>
        <dd>{view.clientMs} ms (measured in this browser, includes the local proxy)</dd>
        {view.serverMs !== undefined && (
          <>
            <dt>Gateway latency</dt>
            <dd>{view.serverMs} ms (reported by the Lambda)</dd>
          </>
        )}
        {view.tokens && (
          <>
            <dt>Tokens</dt>
            <dd>
              {view.tokens.input} in · {view.tokens.output} out
            </dd>
          </>
        )}
        {view.agentSteps !== undefined && (
          <>
            <dt>Agent steps</dt>
            <dd>{view.agentSteps}</dd>
          </>
        )}
        {view.requestId && (
          <>
            <dt>Request ID</dt>
            <dd>
              <code>{view.requestId}</code>
            </dd>
          </>
        )}
      </dl>

      {view.answer !== undefined && (
        <div className="block">
          <h3>Generated answer (model output)</h3>
          <pre className="answer">{view.answer}</pre>
        </div>
      )}

      {view.rag?.enabled && (
        <div className="block evidence">
          <h3>Retrieved evidence (from the Knowledge Base, not generated)</h3>
          <p className="muted">
            {view.rag.chunks ?? "?"} chunk(s) retrieved
            {view.rag.retrievalMs !== undefined && <> in {view.rag.retrievalMs} ms</>}.
          </p>
          {view.rag.sources === null ? (
            <p className="muted">This gateway version does not report source documents.</p>
          ) : (
            <ul>
              {view.rag.sources.map((s, i) => (
                <li key={i}>
                  {s.document ?? "source not reported"}
                  {s.score !== null && <span className="muted"> · score {s.score}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {view.outcome === "success" && (
        <div className="block">
          <h3>MCP tool calls</h3>
          {view.tools === null ? (
            <p className="muted">This gateway version does not report tool calls.</p>
          ) : view.tools.length === 0 ? (
            <p className="muted">The model did not call a tool.</p>
          ) : (
            <ul>
              {view.tools.map((t, i) => (
                <li key={i}>
                  <code>{t.tool}</code> · {t.status}
                  {t.latency_ms !== undefined && <> · {t.latency_ms} ms</>}
                  {t.error_type && <> · {t.error_type}</>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {view.approval && (
        <div className="block">
          <h3>Proposed action (exactly as the gateway returned it)</h3>
          <pre>{JSON.stringify(view.approval.action, null, 2)}</pre>
          {view.approval.executionArn && (
            <p className="muted">
              Execution: <code>{view.approval.executionArn}</code>
            </p>
          )}
          <p className="muted">Next: run scripts/approve.py in a terminal to approve or reject.</p>
        </div>
      )}

      <p className="muted">Time to first token and other serving metrics are not available: the API is not streaming.</p>

      <details>
        <summary>Raw response</summary>
        <pre>{JSON.stringify(raw, null, 2)}</pre>
      </details>
    </section>
  );
}
