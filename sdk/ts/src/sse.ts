/**
 * A minimal, robust parser for the SSE stream contract section 3.4 defines
 * (`event: <name>\ndata: <json>\n\n`), built on the WHATWG SSE field grammar rather than
 * assuming every event is exactly three lines: a `data:` field can repeat (its values are
 * joined with `\n`, not concatenated or overwritten), a line starting with `:` is a
 * comment and ignored, and `id`/`retry` fields are accepted (so a well-formed stream never
 * trips the parser) but not acted on -- reconnect is documented in the contract as not
 * required for this slice.
 */

export interface SseEvent {
  event: string;
  data: unknown;
}

async function* toLines(stream: AsyncIterable<Uint8Array>): AsyncGenerator<string> {
  let buffer = "";
  for await (const chunk of stream) {
    buffer += Buffer.from(chunk).toString("utf8");
    let index: number;
    while ((index = buffer.indexOf("\n")) !== -1) {
      const rawLine = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      yield rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    }
  }
  if (buffer) yield buffer;
}

export async function* parseSse(stream: AsyncIterable<Uint8Array>): AsyncGenerator<SseEvent> {
  let eventName = "message";
  let dataLines: string[] = [];

  function* flush(): Generator<SseEvent> {
    if (dataLines.length) {
      const raw = dataLines.join("\n");
      let data: unknown = raw;
      try {
        data = JSON.parse(raw);
      } catch {
        // Not every event body is guaranteed to be JSON; hand the caller the raw text
        // instead of failing the whole stream over one malformed line.
      }
      yield { event: eventName, data };
    }
    eventName = "message";
    dataLines = [];
  }

  for await (const line of toLines(stream)) {
    if (line === "") {
      yield* flush();
      continue;
    }
    if (line.startsWith(":")) continue; // comment
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") {
      eventName = value;
    } else if (field === "data") {
      dataLines.push(value);
    }
    // id / retry are part of the grammar and intentionally ignored (see module doc).
  }
  yield* flush();
}
