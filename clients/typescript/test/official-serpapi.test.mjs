// SerpApi's npm client (`serpapi`) has no base-URL option. A custom https.Agent can make it
// connect elsewhere; this test proves where its requests actually land. Skipped unless the
// package is installed (npm install in clients/typescript).
import assert from "node:assert/strict";
import net from "node:net";
import https from "node:https";
import { test } from "node:test";
import { recordingServer } from "./server.mjs";

let serpapi;
try {
  serpapi = await import("serpapi");
} catch {
  serpapi = null;
}

test("official serpapi npm client via a redirecting agent", { skip: !serpapi && "serpapi not installed" },
  async () => {
    const server = await recordingServer();
    try {
      // The client always targets serpapi.com:443. Overriding createConnection returns a
      // plain TCP socket to the local server instead of a TLS one to SerpApi.
      class LocalAgent extends https.Agent {
        createConnection() {
          return net.connect(server.port, "127.0.0.1");
        }
      }
      serpapi.config.api_key = "k";
      serpapi.config.requestOptions = { agent: new LocalAgent() };
      serpapi.config.timeout = 2000;
      const json = await serpapi.getJson({ engine: "google", q: "rust" });
      assert.equal(json.organic_results[0].link, "https://rust-lang.org");
      assert.equal(server.requests.length, 1);
      // It reached the local server, but the Host header still says serpapi.com, so a
      // fairserp behind a reverse proxy must not route on Host for this client.
      assert.equal(server.requests[0].host, "serpapi.com");
      assert.match(server.requests[0].url, /^\/search\?/);
    } finally {
      server.close();
    }
  });
