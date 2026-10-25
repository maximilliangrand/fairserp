import http from "node:http";

// Records every request and answers like a fairserp/SerpApi server would.
export async function recordingServer() {
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push({ host: req.headers.host, url: req.url, method: req.method,
                    authorization: req.headers.authorization });
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({
      search_metadata: { id: "abc", status: "Success" },
      status: "success",
      results: [{ rank: 1, url: "https://rust-lang.org", sources: [{ engine: "brave", rank: 1 }] }],
      organic_results: [{ position: 1, link: "https://rust-lang.org" }],
    }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return { requests, port, url: `http://127.0.0.1:${port}`, close: () => server.close() };
}
