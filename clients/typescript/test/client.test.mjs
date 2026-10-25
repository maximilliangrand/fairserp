import assert from "node:assert/strict";
import { test } from "node:test";
import { Fairserp } from "../index.js";
import { recordingServer } from "./server.mjs";

test("every request goes to baseUrl with bearer auth", async () => {
  const server = await recordingServer();
  try {
    const client = new Fairserp({ baseUrl: server.url, apiKey: "k" });
    const outcome = await client.search("rust", { freshness: "m" });
    await client.serpapi({ engine: "google", q: "rust" });
    assert.equal(outcome.results[0].sources[0].engine, "brave");
    assert.deepEqual(server.requests.map((r) => r.url.split("?")[0]), ["/v1/search", "/search.json"]);
    for (const request of server.requests) {
      assert.equal(request.host, `127.0.0.1:${server.port}`);
      assert.equal(request.authorization, "Bearer k");
      assert.ok(!request.url.includes("api_key"));
    }
  } finally {
    server.close();
  }
});

test("baseUrl is mandatory", () => {
  assert.throws(() => new Fairserp({ apiKey: "k" }), /baseUrl is required/);
  assert.throws(() => new Fairserp({ baseUrl: "serpapi.com", apiKey: "k" }), /baseUrl/);
});
