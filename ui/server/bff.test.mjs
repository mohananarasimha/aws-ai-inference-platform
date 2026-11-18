// SIMULATED: a fake Cognito token endpoint and a fake API Gateway run on
// localhost. Real HTTP between real servers, but no AWS service is involved.
import { createServer } from "node:http";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createBff, readConfig } from "./bff.mjs";

const SECRET = "very-secret-client-secret";
let servers = [];

function listen(handler) {
  const server = createServer(handler);
  servers.push(server);
  return new Promise((ok) => server.listen(0, "127.0.0.1", () => ok(`http://127.0.0.1:${server.address().port}`)));
}

async function readBody(req) {
  let s = "";
  for await (const c of req) s += c;
  return s;
}

let cognito; // { calls, status }
let gateway; // { calls, reply }

async function start(overrides = {}) {
  cognito = { calls: [], status: 200 };
  gateway = { calls: [], reply: { status: 200, body: { request_id: "r", response: "hi" } }, delayMs: 0 };
  const tokenUrl = await listen(async (req, res) => {
    cognito.calls.push({ auth: req.headers.authorization, body: await readBody(req) });
    res.writeHead(cognito.status, { "content-type": "application/json" });
    res.end(JSON.stringify(cognito.status === 200
      ? { access_token: `token-${cognito.calls.length}`, expires_in: 3600 }
      : { error: "invalid_client", detail: "secret leak bait" }));
  });
  const invokeUrl = await listen(async (req, res) => {
    gateway.calls.push({ auth: req.headers.authorization, body: JSON.parse(await readBody(req)) });
    await new Promise((r) => setTimeout(r, gateway.delayMs));
    res.writeHead(gateway.reply.status, { "content-type": "application/json" });
    res.end(JSON.stringify(gateway.reply.body));
  });
  const config = readConfig({
    GATEWAY_INVOKE_URL: invokeUrl, COGNITO_TOKEN_URL: tokenUrl,
    COGNITO_CLIENT_ID: "client", COGNITO_CLIENT_SECRET: SECRET, ...overrides,
  });
  const bffUrl = await new Promise((ok) => {
    const bff = createBff(config);
    servers.push(bff);
    bff.listen(0, "127.0.0.1", () => ok(`http://127.0.0.1:${bff.address().port}`));
  });
  return bffUrl;
}

function post(url, body, headers = {}) {
  return fetch(`${url}/api/invoke`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

afterEach(async () => {
  await Promise.all(servers.map((s) => new Promise((r) => s.close(r))));
  servers = [];
});

describe("BFF", () => {
  let bff;
  beforeEach(async () => {
    bff = await start();
  });

  it("gets a client-credentials token and forwards it as a bearer token", async () => {
    const res = await post(bff, { prompt: "hi", provider: "bedrock" });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ request_id: "r", response: "hi" });
    expect(cognito.calls[0].auth).toBe("Basic " + Buffer.from(`client:${SECRET}`).toString("base64"));
    expect(cognito.calls[0].body).toBe("grant_type=client_credentials&scope=enterprise-ai-gateway%2Finvoke");
    expect(gateway.calls[0].auth).toBe("Bearer token-1");
  });

  it("never returns the secret or the token to the browser", async () => {
    const text = await (await post(bff, { prompt: "hi" })).text();
    expect(text).not.toContain(SECRET);
    expect(text).not.toContain("token-1");
  });

  it("caches the token between requests", async () => {
    await post(bff, { prompt: "a" });
    await post(bff, { prompt: "b" });
    expect(cognito.calls).toHaveLength(1);
  });

  it("forwards only the documented request fields", async () => {
    await post(bff, { prompt: "hi", provider: "vllm", target: "onprem", rag: true, model_class: "fast", extra: "x", authorization: "y" });
    expect(gateway.calls[0].body).toEqual({ prompt: "hi", provider: "vllm", target: "onprem", rag: true, model_class: "fast" });
  });

  it("passes gateway errors through with their status", async () => {
    gateway.reply = { status: 503, body: { request_id: "r", error: "vllm_endpoint_not_found" } };
    const res = await post(bff, { prompt: "hi", provider: "vllm" });
    expect(res.status).toBe(503);
    expect((await res.json()).error).toBe("vllm_endpoint_not_found");
  });

  it("passes API Gateway throttling through", async () => {
    gateway.reply = { status: 429, body: { message: "Too Many Requests" } };
    const res = await post(bff, { prompt: "hi" });
    expect(res.status).toBe(429);
  });

  it("drops a token the gateway rejected and fetches a new one next time", async () => {
    gateway.reply = { status: 401, body: { message: "Unauthorized" } };
    expect((await post(bff, { prompt: "hi" })).status).toBe(401);
    gateway.reply = { status: 200, body: { response: "ok" } };
    await post(bff, { prompt: "hi" });
    expect(cognito.calls).toHaveLength(2);
    expect(gateway.calls[1].auth).toBe("Bearer token-2");
  });

  it("reports a Cognito failure without forwarding Cognito's body", async () => {
    cognito.status = 400;
    const res = await post(bff, { prompt: "hi" });
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body).toEqual({ source: "bff", error: "token_request_failed", token_status: 400 });
    expect(gateway.calls).toHaveLength(0);
  });

  it("refuses cross-site origins", async () => {
    const res = await post(bff, { prompt: "hi" }, { origin: "https://evil.example" });
    expect(res.status).toBe(403);
    expect(gateway.calls).toHaveLength(0);
  });

  it("accepts the Vite dev server origin", async () => {
    const res = await post(bff, { prompt: "hi" }, { origin: "http://127.0.0.1:5173" });
    expect(res.status).toBe(200);
  });

  it("requires a JSON content type (forces a CORS preflight for cross-site pages)", async () => {
    const res = await fetch(`${bff}/api/invoke`, { method: "POST", headers: { "content-type": "text/plain" }, body: "{}" });
    expect(res.status).toBe(415);
  });

  it("rejects invalid JSON", async () => {
    const res = await post(bff, "not json");
    expect(res.status).toBe(400);
  });

  it("health says whether it is configured, without values", async () => {
    const body = await (await fetch(`${bff}/api/health`)).json();
    expect(body).toEqual({ configured: true, missing: [] });
  });
});

describe("BFF limits", () => {
  it("stops waiting for a slow gateway and says so", async () => {
    const bff = await start({ UPSTREAM_TIMEOUT_MS: "100" });
    gateway.delayMs = 400;
    const res = await post(bff, { prompt: "hi" });
    expect(res.status).toBe(504);
    expect(await res.json()).toEqual({ source: "bff", error: "upstream_timeout", timeout_ms: 100 });
  });

  it("refuses to call anything when not configured", async () => {
    const bff = await start({ COGNITO_CLIENT_SECRET: "" });
    const res = await post(bff, { prompt: "hi" });
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ source: "bff", error: "bff_not_configured", missing: ["COGNITO_CLIENT_SECRET"] });
    expect(cognito.calls).toHaveLength(0);
  });
});
