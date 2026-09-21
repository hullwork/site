// Minimal Node server for the ai-saas-slice fixture: node:http plus @hull/sdk, no
// framework. Exposes /api/tickets (list, RLS-scoped through db.withUser), /api/summarize
// (the "summarize_ticket" flow), and /api/triage (SSE passthrough of the "triage" agent).
//
// This is a fixture, not a product: it does not depend on the `pg` package. A real app
// plugs a real driver in -- see `createDbClient` below -- because this file is meant to
// run under `node --test` without installing anything beyond @hull/sdk.
import { createServer } from "node:http";
import { createHull } from "@hull/sdk";

export function createApp(hull, dbClient) {
  return createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://localhost");

      if (req.method === "GET" && url.pathname === "/api/tickets") {
        const user = await hull.auth.requireUser(req);
        if (!(await hull.can(user, "tickets.read"))) {
          respondJson(res, 403, { error: "forbidden" });
          return;
        }
        const rows = await hull.db.withUser(dbClient, user, (client) =>
          client.query("SELECT id, title, status FROM tickets ORDER BY created_at DESC"),
        );
        respondJson(res, 200, rows);
        return;
      }

      if (req.method === "POST" && url.pathname === "/api/summarize") {
        const user = await hull.auth.requireUser(req);
        const body = await readJson(req);
        const result = await hull.flow("summarize_ticket", body, { user });
        respondJson(res, 200, result);
        return;
      }

      if (req.method === "POST" && url.pathname === "/api/triage") {
        const user = await hull.auth.requireUser(req);
        const body = await readJson(req);
        res.writeHead(200, { "content-type": "text/event-stream" });
        for await (const event of hull.agent("triage").stream({
          user,
          input: body.input,
          threadId: body.threadId,
        })) {
          res.write(`event: ${event.event}\ndata: ${JSON.stringify(event.data)}\n\n`);
        }
        res.end();
        return;
      }

      respondJson(res, 404, { error: "not_found" });
    } catch (err) {
      const status = typeof err?.status === "number" ? err.status : 500;
      respondJson(res, status, { error: err?.message ?? "internal error" });
    }
  });
}

function respondJson(res, status, body) {
  const raw = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(raw);
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
}

// A real deployment plugs a real driver in here, e.g.:
//   import pg from "pg";
//   const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
//   const dbClient = { query: (text, params) => pool.query(text, params) };
// This fixture never imports `pg`, so it throws instead of silently returning fake rows;
// tests exercising the routes above pass in their own in-memory client.
function unconfiguredDbClient() {
  return {
    query() {
      throw new Error(
        "no database client configured; plug in a pg-compatible client (see the comment above)",
      );
    },
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const hull = createHull({});
  createApp(hull, unconfiguredDbClient()).listen(process.env.PORT ?? 8080);
}
