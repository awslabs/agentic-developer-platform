import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { logs } from "@opentelemetry/api-logs";
import { metrics, trace } from "@opentelemetry/api";
import { startTelemetry, activeTraceparent, observeOperation } from "./telemetry.js";

test("OTLP exports correlated spans and bounded metrics without task content", async () => {
  const documents: { path: string; body: string }[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      documents.push({ path: request.url!, body: Buffer.concat(chunks).toString() });
      response.setHeader("content-type", "application/json"); response.end("{}");
    });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address(); assert.ok(address && typeof address !== "string");
  const parent = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01";
  const telemetry = startTelemetry({ endpoint: `http://127.0.0.1:${address.port}`, traceparent: parent, runId: "run-test", persona: "gpt-developer" });
  try {
    await telemetry.run(async () => {
      assert.ok(activeTraceparent()?.startsWith("00-1234567890abcdef1234567890abcdef-"));
      await trace.getTracer("test").startActiveSpan("adp.codex.turn", async span => {
        await Promise.resolve(); assert.ok(activeTraceparent()); span.end();
      });
      await observeOperation("model", async () => {});
      metrics.getMeter("test").createCounter("adp.codex.turns").add(1, { outcome: "completed" });
    });
    await telemetry.shutdown();
    const spans = documents.filter(d => d.path === "/v1/traces").flatMap(d => JSON.parse(d.body).resourceSpans.flatMap((r: any) => r.scopeSpans.flatMap((s: any) => s.spans)));
    const run = spans.find((span: any) => span.name === "adp.codex.run");
    const turn = spans.find((span: any) => span.name === "adp.codex.turn");
    assert.equal(run.traceId, parent.split("-")[1]);
    assert.equal(run.parentSpanId, parent.split("-")[2]);
    assert.equal(turn.parentSpanId, run.spanId);
    assert.ok(documents.some(d => d.path === "/v1/metrics"));
    const records = documents.filter(d => d.path === "/v1/logs").flatMap(d => JSON.parse(d.body).resourceLogs.flatMap((r: any) => r.scopeLogs.flatMap((s: any) => s.logRecords)));
    assert.ok(records.some((record: any) => record.body.stringValue === "adp.codex.model.settled" && record.traceId === run.traceId && record.spanId));
    assert.doesNotMatch(JSON.stringify(documents), /authorization|prompt|api_key|access_token/i);
    assert.equal(activeTraceparent(), undefined);
  } finally {
    await telemetry.shutdown();
    await new Promise<void>(resolve => server.close(() => resolve()));
    trace.disable(); metrics.disable(); logs.disable();
  }
});

test("unresponsive collector cannot delay shutdown beyond its bound", async () => {
  const server = createServer(() => {});
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address(); assert.ok(address && typeof address !== "string");
  const telemetry = startTelemetry({ endpoint: `http://127.0.0.1:${address.port}`, runId: "run-test", persona: "gpt-developer" });
  try {
    await telemetry.run(async () => {});
    const start = performance.now(); await telemetry.shutdown();
    assert.ok(performance.now() - start < 1200);
  } finally {
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
    trace.disable(); metrics.disable(); logs.disable();
  }
});
