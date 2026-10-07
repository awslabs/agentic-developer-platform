import test from "node:test";
import assert from "node:assert/strict";
import { readModelResponse } from "./model-response.js";

const signal = () => new AbortController().signal;
const result = { id: "fixture", status: "completed", output: [{ text: "Full report 😀" }], usage: { input_tokens: 20, output_tokens: 9000 } };
function stream(text: string, chunkSize = 1) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize));
    controller.close();
  } }), { headers: { "content-type": "text/event-stream; charset=utf-8" } });
}

test("stream completion preserves full output and usage across UTF-8 and CRLF boundaries", async () => {
  const payload = JSON.stringify({ type: "response.completed", response: result }, null, 2).split("\n").map(line => `data: ${line}`).join("\r\n");
  const response = stream(`: keepalive\r\nevent: response.created\r\ndata: {"type":"response.created"}\r\n\r\nevent: response.completed\r\n${payload}\r\n\r\n`);
  assert.deepEqual(await readModelResponse(response, signal()), result);
});

test("partial, failed, incomplete and malformed streams never confirm a model operation", async () => {
  for (const body of [
    'event: response.created\ndata: {"type":"response.created"}\n\n',
    'event: response.failed\ndata: {"type":"response.failed"}\n\n',
    'event: response.incomplete\ndata: {"type":"response.incomplete"}\n\n',
    'event: error\ndata: {"type":"error"}\n\n',
    'event: response.completed\ndata: {"type":"response.created"}\n\n',
    'event: response.completed\ndata: {"type":"response.completed","response":{"status":"incomplete"}}\n\n',
    'event: response.completed\ndata: not-json\n\n',
  ]) await assert.rejects(readModelResponse(stream(body), signal()));
});

test("stream permits large completed reports and opaque provider state", async () => {
  const large = { ...result, output: [{ text: "complete report ".repeat(10000) }] };
  assert.deepEqual(await readModelResponse(stream(`data: ${JSON.stringify({ type: "response.completed", response: large })}\n\n`, 4096), signal()), large);
});

test("run cancellation interrupts a provider stream that has no data", async () => {
  let cancelled = false;
  const controller = new AbortController();
  const response = new Response(new ReadableStream({ cancel() { cancelled = true; } }), { headers: { "content-type": "text/event-stream" } });
  const pending = readModelResponse(response, controller.signal);
  controller.abort(new Error("Run stopped"));
  await assert.rejects(pending, /Run stopped/);
  assert.equal(cancelled, true);
});

test("compatible JSON responses remain intact", async () => {
  assert.deepEqual(await readModelResponse(new Response(JSON.stringify(result)), signal()), result);
});

test('stream failures expose static diagnostics without provider content', async () => {
  for (const [type, code] of [['response.failed', 'model_stream_failed'], ['response.incomplete', 'model_stream_incomplete'], ['response.created', 'model_stream_interrupted']]) {
    await assert.rejects(readModelResponse(stream(`data: ${JSON.stringify({type, error: {message: 'private provider content'}})}\n\n`), signal()),
      (error: unknown) => error instanceof Error && error.message === code);
  }
});
