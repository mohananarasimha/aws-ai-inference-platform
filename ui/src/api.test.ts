// SIMULATED: fetch is replaced; nothing leaves the process.
import { describe, expect, it } from "vitest";
import { invoke } from "./api";

const req = { prompt: "hi", provider: "bedrock" as const };

describe("browser client", () => {
  it("returns status and parsed body", async () => {
    const fake = (async () => new Response(JSON.stringify({ response: "ok" }), { status: 200 })) as typeof fetch;
    const ex = await invoke(req, 1000, fake);
    expect(ex).toMatchObject({ kind: "response", status: 200, body: { response: "ok" } });
  });

  it("aborts after its own timeout", async () => {
    const hang = ((_: RequestInfo | URL, init?: RequestInit) =>
      new Promise((_, reject) => init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))))) as typeof fetch;
    const ex = await invoke(req, 50, hang);
    expect(ex).toMatchObject({ kind: "client_timeout", timeoutMs: 50 });
  });

  it("reports a network failure", async () => {
    const down = (async () => {
      throw new TypeError("Failed to fetch");
    }) as typeof fetch;
    const ex = await invoke(req, 1000, down);
    expect(ex).toMatchObject({ kind: "network_error", message: "Failed to fetch" });
  });

  it("sends only JSON to the local /api/invoke", async () => {
    let seen: { url: string; init?: RequestInit } | undefined;
    const spy = (async (url: RequestInfo | URL, init?: RequestInit) => {
      seen = { url: String(url), init };
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    await invoke(req, 1000, spy);
    expect(seen?.url).toBe("/api/invoke");
    expect((seen?.init?.headers as Record<string, string>)["content-type"]).toBe("application/json");
    expect(JSON.parse(String(seen?.init?.body))).toEqual(req);
  });
});
