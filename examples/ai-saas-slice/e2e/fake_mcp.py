"""Minimal streamable-HTTP MCP server with one tool, for the end-to-end run.

It answers JSON-RPC over POST with a plain JSON body (the spec allows JSON or SSE
responses), requires `Authorization: Bearer <FAKE_MCP_TOKEN>` when that env is set, and
logs each call to FAKE_MCP_LOG.
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("FAKE_MCP_TOKEN", "")
LOG = os.environ.get("FAKE_MCP_LOG", "")
TOOLS = [{
    "name": "search_docs",
    "description": "Search the knowledge base.",
    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
    "annotations": {"readOnlyHint": True},
}]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status: int, payload: dict | None, headers: dict | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}")
        auth_ok = not TOKEN or self.headers.get("Authorization") == f"Bearer {TOKEN}"
        if LOG:
            with open(LOG, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": time.time(), "auth_ok": auth_ok,
                                         "message": message}) + "\n")
        if not auth_ok:
            self._send(401, {"error": "unauthorized"})
            return
        method = message.get("method")
        if "id" not in message:  # notification
            self._send(202, None)
            return
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-kb", "version": "1.0.0"}}
            self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": result},
                       {"Mcp-Session-Id": "fake-session"})
        elif method == "tools/list":
            self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": TOOLS}})
        elif method == "tools/call":
            args = message.get("params", {}).get("arguments") or {}
            text = f"kb hit for {args.get('q', '')!r}: reset your password from the account page"
            self._send(200, {"jsonrpc": "2.0", "id": message["id"],
                             "result": {"content": [{"type": "text", "text": text}], "isError": False}})
        else:
            self._send(200, {"jsonrpc": "2.0", "id": message["id"],
                             "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()
