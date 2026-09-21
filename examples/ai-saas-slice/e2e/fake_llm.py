"""Scripted OpenAI-compatible chat endpoint for the vertical-slice end-to-end run.

Policy, driven only by the request so runs are reproducible:
- a request with `response_format` (Broker flows) gets a JSON object built from the
  last user message;
- otherwise every offered tool matching FAKE_LLM_TOOL_ORDER (comma list, in that order)
  is called once per conversation;
- then end_task (when offered) or a final text quoting every tool result, or plain
  text when no tool ran.
Every request is appended to FAKE_LLM_LOG (JSON lines) so the harness can assert on what
the model was shown.
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("FAKE_LLM_LOG", "")
ORDER = [s for s in os.environ.get("FAKE_LLM_TOOL_ORDER", "").split(",") if s]


def _log(entry: dict) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _decide(body: dict) -> dict:
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    if body.get("response_format"):
        text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content")
                text = content if isinstance(content, str) else json.dumps(content)
                break
        return {"text": json.dumps({"summary": "summary of: " + text[:80]})}
    called = {
        call.get("function", {}).get("name")
        for message in messages if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    }
    offered = [t.get("function", {}).get("name") for t in body.get("tools") or []]
    for wanted in ORDER:
        for name in offered:
            if name and wanted in name and name not in called:
                # Tool-call ids must be unique within a conversation; a reused id reads
                # to the runtime as a call it already executed.
                return {"tool": name, "arguments": {}, "id": f"call_{len(messages)}_{name}"}
    results = [str(m.get("content")) for m in messages if m.get("role") == "tool"]
    if results and "end_task" in offered and "end_task" not in called:
        # The agent runtime refuses a plain-text finish once tools have run; a real
        # model follows its completion gate and closes with end_task.
        return {"tool": "end_task", "id": f"call_{len(messages)}_end_task",
                "arguments": {"summary": "Triage complete. Tool results: "
                              + " || ".join(r[:300] for r in results), "outcome": "completed"}}
    if results:
        return {"text": "Tool results: " + " || ".join(r[:600] for r in results)}
    return {"text": "Hello from the fake model."}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        decision = _decide(body)
        _log({"ts": time.time(), "path": self.path, "request": body, "decision": decision})
        usage = {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            chunks = []
            if "tool" in decision:
                chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": decision["id"], "type": "function",
                    "function": {"name": decision["tool"],
                                 "arguments": json.dumps(decision["arguments"])}}]}}]})
                chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            else:
                chunks.append({"choices": [{"index": 0, "delta": {"content": decision["text"]}}]})
                chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            chunks.append({"choices": [], "usage": usage, "model": body.get("model")})
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        message: dict = {"role": "assistant", "content": decision.get("text")}
        finish = "stop"
        if "tool" in decision:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": decision["id"], "type": "function",
                "function": {"name": decision["tool"], "arguments": json.dumps(decision["arguments"])}}]}
            finish = "tool_calls"
        payload = json.dumps({"id": "fake", "object": "chat.completion", "model": body.get("model"),
                              "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                              "usage": usage}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()
