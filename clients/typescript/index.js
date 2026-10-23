// fairserp client for Node 18+, Deno, Bun and browsers. The base URL is required, so a
// misconfigured client fails instead of sending your key and query to another service.

export class FairserpError extends Error {
  constructor(status, body) {
    const message = typeof body?.error === "object" ? body.error.message : body?.error ?? body;
    super(`HTTP ${status}: ${message}`);
    this.status = status;
    this.body = body;
  }
}

export class Fairserp {
  constructor({ baseUrl, apiKey, fetch: fetchImpl = globalThis.fetch, timeoutMs = 30000 } = {}) {
    if (!baseUrl || !/^https?:\/\//.test(baseUrl)) {
      throw new Error("baseUrl is required, e.g. 'http://localhost:8000'");
    }
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
    this.fetch = fetchImpl;
    this.timeoutMs = timeoutMs;
  }

  async #request(path, init = {}) {
    const response = await this.fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${this.apiKey}`, ...(init.headers ?? {}) },
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new FairserpError(response.status, body);
    return body;
  }

  /** Native search: returns a SearchOutcome with provenance on every result. */
  search(q, params = {}) {
    return this.#request("/v1/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q, ...params }),
    });
  }

  /** SerpApi-shaped search on the migration endpoint. */
  serpapi(params) {
    return this.#request(`/search.json?${new URLSearchParams(params)}`);
  }

  engines() {
    return this.#request("/v1/engines");
  }
}
