import { createServer, type IncomingHttpHeaders } from "node:http";

import { expect, test } from "@playwright/test";

// The production server is started with CDA_API_URL pointing at this stub.
const STUB_PORT = 3199;

// Both tests use the stub port, so they must not run at the same time.
test.describe.configure({ mode: "serial" });

test("proxy sets X-Demo-User from the cookie and forwards method, query and body", async ({
  request,
}) => {
  const seen: { method?: string; url?: string; headers: IncomingHttpHeaders; body: string }[] =
    [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, body });
      res.writeHead(409, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "duplicate_pack", pack_id: "p1" }));
    });
  });
  await new Promise<void>((resolve) => server.listen(STUB_PORT, "127.0.0.1", resolve));
  try {
    const alex = await request.post("/api/packs?x=1", {
      headers: { cookie: "cda_user=alex", "Idempotency-Key": "k1", "X-Demo-User": "sarah" },
      data: { company: "A" },
    });
    expect(alex.status()).toBe(409);
    expect(await alex.json()).toEqual({ error: "duplicate_pack", pack_id: "p1" });
    const other = await request.get("/api/me", { headers: { cookie: "cda_user=mallory" } });
    expect(other.status()).toBe(409);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
  expect(seen[0].method).toBe("POST");
  expect(seen[0].url).toBe("/packs?x=1");
  // The header comes from the cookie, never from the browser's own X-Demo-User.
  expect(seen[0].headers["x-demo-user"]).toBe("alex");
  expect(seen[0].headers["idempotency-key"]).toBe("k1");
  expect(JSON.parse(seen[0].body)).toEqual({ company: "A" });
  // Unknown cookie values fall back to sarah.
  expect(seen[1].url).toBe("/me");
  expect(seen[1].headers["x-demo-user"]).toBe("sarah");
});

test("proxy reports an unreachable API as 502", async ({ request }) => {
  const response = await request.get("/api/packs");
  expect(response.status()).toBe(502);
  expect((await response.json()).error).toBe("api_unreachable");
});
