import type { Exchange, InvokeRequest } from "./interpret";

// Longer than the BFF's own upstream limit (29 s), so a slow gateway is reported
// by the BFF with a reason instead of by this generic browser timeout.
export const CLIENT_TIMEOUT_MS = 35000;

export async function invoke(
  request: InvokeRequest,
  timeoutMs = CLIENT_TIMEOUT_MS,
  fetchImpl: typeof fetch = fetch,
): Promise<Exchange> {
  const started = performance.now();
  const elapsed = () => Math.round(performance.now() - started);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl("/api/invoke", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
      signal: controller.signal,
    });
    const text = await res.text();
    let body: unknown;
    try {
      body = JSON.parse(text);
    } catch {
      body = { source: "bff", error: "non_json_response" };
    }
    return { kind: "response", status: res.status, body, clientMs: elapsed() };
  } catch (err) {
    if (controller.signal.aborted) return { kind: "client_timeout", clientMs: elapsed(), timeoutMs };
    return { kind: "network_error", clientMs: elapsed(), message: err instanceof Error ? err.message : String(err) };
  } finally {
    clearTimeout(timer);
  }
}
