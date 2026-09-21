import { test } from "node:test";
import assert from "node:assert/strict";

import { parseSse } from "../src/sse.js";

async function* chunks(...parts: string[]): AsyncGenerator<Uint8Array> {
  for (const part of parts) yield Buffer.from(part, "utf8");
}

async function collect(stream: AsyncGenerator<{ event: string; data: unknown }>) {
  const events: Array<{ event: string; data: unknown }> = [];
  for await (const event of stream) events.push(event);
  return events;
}

test("parses a simple event", async () => {
  const events = await collect(parseSse(chunks('event: run.started\ndata: {"runId":"r1"}\n\n')));
  assert.deepEqual(events, [{ event: "run.started", data: { runId: "r1" } }]);
});

test("joins multi-line data fields with newlines (SSE field grammar)", async () => {
  const events = await collect(
    parseSse(chunks('event: message.delta\ndata: line one\ndata: line two\n\n')),
  );
  assert.deepEqual(events, [{ event: "message.delta", data: "line one\nline two" }]);
});

test("ignores comment lines", async () => {
  const events = await collect(
    parseSse(chunks(': keep-alive\nevent: run.finished\ndata: {"usage":{"credits":1}}\n\n')),
  );
  assert.deepEqual(events, [{ event: "run.finished", data: { usage: { credits: 1 } } }]);
});

test("defaults to the message event name when none is given", async () => {
  const events = await collect(parseSse(chunks('data: "hello"\n\n')));
  assert.deepEqual(events, [{ event: "message", data: "hello" }]);
});

test("handles an event split across multiple chunks", async () => {
  const events = await collect(
    parseSse(chunks('event: message.d', 'elta\ndata: {"tex', 't":"hi"}\n\n')),
  );
  assert.deepEqual(events, [{ event: "message.delta", data: { text: "hi" } }]);
});

test("falls back to raw text when the data is not JSON", async () => {
  const events = await collect(parseSse(chunks("event: run.error\ndata: not json here\n\n")));
  assert.deepEqual(events, [{ event: "run.error", data: "not json here" }]);
});

test("parses several events back to back, including a trailing one with no final blank line", async () => {
  const events = await collect(
    parseSse(
      chunks(
        'event: run.started\ndata: {"runId":"r1"}\n\n',
        'event: tool.started\ndata: {"callId":"c1"}\n\n',
        'event: run.finished\ndata: {"usage":{"credits":2}}\n',
      ),
    ),
  );
  assert.deepEqual(events, [
    { event: "run.started", data: { runId: "r1" } },
    { event: "tool.started", data: { callId: "c1" } },
    { event: "run.finished", data: { usage: { credits: 2 } } },
  ]);
});
