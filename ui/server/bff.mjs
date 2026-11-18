#!/usr/bin/env node
// Backend-for-frontend for the inference playground. Local use only.
//
// The browser never sees the Cognito client secret or an access token. It sends
// POST /api/invoke here; this process gets a client-credentials token from
// Cognito (cached until shortly before it expires) and calls the existing
// API Gateway /invoke endpoint with it. Responses from the gateway are passed
// back unchanged, with their status code. Errors raised here say source "bff".
//
//   node server/bff.mjs          reads ui/.env.local, listens on 127.0.0.1:8787
//
// It refuses to listen on anything but loopback: it has no user authentication
// of its own, so anyone who could reach it could spend the gateway's budget.

import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const UI_DIR = resolve(fileURLToPath(new URL("..", import.meta.url)));
const FORWARDED_FIELDS = ["prompt", "provider", "target", "model_class", "rag"];
const MAX_BODY_BYTES = 16 * 1024;
const LOOPBACK = new Set(["127.0.0.1", "::1", "localhost"]);
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".json": "application/json",
};

export function loadEnvFile(path) {
  const env = {};
  if (!existsSync(path)) return env;
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m && !line.trimStart().startsWith("#")) env[m[1]] = m[2].replace(/^(['"])(.*)\1$/, "$2");
  }
  return env;
}

export function readConfig(env) {
  return {
    invokeUrl: env.GATEWAY_INVOKE_URL ?? "",
    tokenUrl: env.COGNITO_TOKEN_URL ?? "",
    clientId: env.COGNITO_CLIENT_ID ?? "",
    clientSecret: env.COGNITO_CLIENT_SECRET ?? "",
    scope: env.COGNITO_SCOPE || "enterprise-ai-gateway/invoke",
    port: Number(env.BFF_PORT || 8787),
    host: env.BFF_HOST || "127.0.0.1",
    upstreamTimeoutMs: Number(env.UPSTREAM_TIMEOUT_MS || 29000),
  };
}

export function missingSettings(config) {
  const required = { GATEWAY_INVOKE_URL: config.invokeUrl, COGNITO_TOKEN_URL: config.tokenUrl,
    COGNITO_CLIENT_ID: config.clientId, COGNITO_CLIENT_SECRET: config.clientSecret };
  return Object.entries(required).filter(([, v]) => !v).map(([k]) => k);
}

class BffError extends Error {
  constructor(status, error, extra = {}) {
    super(error);
    this.status = status;
    this.body = { source: "bff", error, ...extra };
  }
}

export function createTokenSource(config, fetchImpl = fetch, now = Date.now) {
  let cached = null;
  return {
    async get() {
      if (cached && now() < cached.expiresAt) return cached.token;
      let res;
      try {
        res = await fetchImpl(config.tokenUrl, {
          method: "POST",
          headers: {
            "content-type": "application/x-www-form-urlencoded",
            authorization: "Basic " + Buffer.from(`${config.clientId}:${config.clientSecret}`).toString("base64"),
          },
          body: new URLSearchParams({ grant_type: "client_credentials", scope: config.scope }).toString(),
          signal: AbortSignal.timeout(10000),
        });
      } catch {
        throw new BffError(502, "token_endpoint_unreachable");
      }
      // Never forward Cognito's body: it is not needed and must not leak details.
      if (!res.ok) throw new BffError(502, "token_request_failed", { token_status: res.status });
      const data = await res.json().catch(() => ({}));
      if (!data.access_token) throw new BffError(502, "token_request_failed", { token_status: res.status });
      const ttlMs = Math.max(0, (Number(data.expires_in) || 3600) - 60) * 1000;
      cached = { token: data.access_token, expiresAt: now() + ttlMs };
      return cached.token;
    },
    forget() {
      cached = null;
    },
  };
}

function allowedOrigin(origin, host) {
  if (!origin) return true;
  try {
    const u = new URL(origin);
    return LOOPBACK.has(u.hostname) && (u.host === host || u.port === "5173");
  } catch {
    return false;
  }
}

function hostIsLoopback(hostHeader) {
  if (!hostHeader) return false;
  const name = hostHeader.startsWith("[") ? hostHeader.slice(1, hostHeader.indexOf("]")) : hostHeader.split(":")[0];
  return LOOPBACK.has(name);
}

async function readJson(req) {
  let size = 0;
  const parts = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new BffError(413, "request_too_large");
    parts.push(chunk);
  }
  try {
    const value = JSON.parse(Buffer.concat(parts).toString("utf8") || "{}");
    if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error();
    return value;
  } catch {
    throw new BffError(400, "invalid_json");
  }
}

export async function invoke(config, tokens, input, fetchImpl = fetch) {
  const payload = {};
  for (const key of FORWARDED_FIELDS) if (key in input) payload[key] = input[key];
  const token = await tokens.get();
  const started = Date.now();
  let res;
  try {
    res = await fetchImpl(config.invokeUrl, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(config.upstreamTimeoutMs),
    });
  } catch (err) {
    if (err?.name === "TimeoutError" || err?.name === "AbortError") {
      throw new BffError(504, "upstream_timeout", { timeout_ms: config.upstreamTimeoutMs });
    }
    throw new BffError(502, "upstream_unreachable");
  }
  if (res.status === 401) tokens.forget(); // a stale or revoked token: fetch a new one next time
  const text = await res.text();
  return { status: res.status, text, upstreamMs: Date.now() - started };
}

function send(res, status, body, headers = {}) {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", ...headers });
  res.end(typeof body === "string" ? body : JSON.stringify(body));
}

async function serveStatic(req, res) {
  const dist = join(UI_DIR, "dist");
  const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  const file = normalize(join(dist, path === "/" ? "index.html" : path));
  if (!file.startsWith(dist + sep)) return send(res, 404, { source: "bff", error: "not_found" });
  try {
    if (!(await stat(file)).isFile()) throw new Error();
    res.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
    res.end(await readFile(file));
  } catch {
    send(res, 404, { source: "bff", error: "not_found", hint: "run `npm run dev`, or `npm run build` first" });
  }
}

export function createBff(config, { fetchImpl = fetch, tokens = createTokenSource(config, fetchImpl) } = {}) {
  return createServer(async (req, res) => {
    try {
      // DNS-rebinding and cross-site protection: only loopback hosts and origins.
      if (!hostIsLoopback(req.headers.host)) throw new BffError(403, "forbidden_host");
      if (!allowedOrigin(req.headers.origin, req.headers.host)) throw new BffError(403, "forbidden_origin");

      const path = new URL(req.url, "http://x").pathname;
      if (path === "/api/health" && req.method === "GET") {
        const missing = missingSettings(config);
        return send(res, 200, { configured: missing.length === 0, missing });
      }
      if (path === "/api/invoke") {
        if (req.method !== "POST") throw new BffError(405, "method_not_allowed");
        // Requiring JSON forces a CORS preflight, which this server never answers.
        if (!(req.headers["content-type"] ?? "").startsWith("application/json")) {
          throw new BffError(415, "json_required");
        }
        const missing = missingSettings(config);
        if (missing.length) throw new BffError(500, "bff_not_configured", { missing });
        const input = await readJson(req);
        const out = await invoke(config, tokens, input, fetchImpl);
        const isJson = (() => { try { JSON.parse(out.text); return true; } catch { return false; } })();
        return send(res, out.status, isJson ? out.text : { source: "gateway", error: "non_json_response" },
          { "x-upstream-ms": String(out.upstreamMs) });
      }
      if (path.startsWith("/api/")) throw new BffError(404, "not_found");
      if (req.method !== "GET") throw new BffError(405, "method_not_allowed");
      return serveStatic(req, res);
    } catch (err) {
      if (err instanceof BffError) return send(res, err.status, err.body);
      console.error(JSON.stringify({ event: "bff_error", error: String(err?.name ?? "Error") }));
      return send(res, 500, { source: "bff", error: "internal_error" });
    }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const config = readConfig({ ...loadEnvFile(join(UI_DIR, ".env.local")), ...process.env });
  if (!LOOPBACK.has(config.host)) {
    console.error(`refusing to listen on ${config.host}: the playground BFF is local-only (no user authentication)`);
    process.exit(1);
  }
  const missing = missingSettings(config);
  if (missing.length) console.warn(`warning: missing ${missing.join(", ")} (copy .env.example to .env.local)`);
  createBff(config).listen(config.port, config.host, () => {
    console.log(`playground BFF on http://${config.host}:${config.port} -> ${config.invokeUrl || "(not configured)"}`);
  });
}
