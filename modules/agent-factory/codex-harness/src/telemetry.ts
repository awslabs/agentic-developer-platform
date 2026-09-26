import { context, metrics, trace, TraceFlags, isSpanContextValid, ROOT_CONTEXT, SpanStatusCode } from "@opentelemetry/api";
import { randomBytes } from "node:crypto";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { MeterProvider, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import { resourceFromAttributes } from "@opentelemetry/resources";

export const traceparentPattern = /^00-(?!0{32}-)[a-f0-9]{32}-(?!0{16}-)[a-f0-9]{16}-0[01]$/;
export function activeTraceparent(): string | undefined {
  const span = trace.getSpanContext(context.active());
  return span && isSpanContextValid(span) ? `00-${span.traceId}-${span.spanId}-${span.traceFlags & TraceFlags.SAMPLED ? "01" : "00"}` : undefined;
}

/** Trusted runtime configuration only. No automatic instrumentation, environment
 * resource discovery, content capture, exporter headers or arbitrary attributes. */
export function startTelemetry(options: { endpoint?: string; traceparent?: string; runId: string; persona: string }) {
  let tracerProvider: NodeTracerProvider | undefined;
  let meterProvider: MeterProvider | undefined;
  if (options.endpoint) {
    const endpoint = new URL(options.endpoint);
    if (!["http:", "https:"].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
      throw new Error("Invalid host telemetry endpoint");
    }
    const base = endpoint.toString().replace(/\/$/, "");
    const resource = resourceFromAttributes({ "service.name": "adp-codex-harness", "service.namespace": "adp", "adp.harness.revision": "codex-sdk-0.155.1/adp-v1" });
    tracerProvider = new NodeTracerProvider({ resource, idGenerator: {
      generateTraceId: () => Math.floor(Date.now() / 1000).toString(16).padStart(8, "0") + randomBytes(12).toString("hex"),
      generateSpanId: () => randomBytes(8).toString("hex"),
    }, spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({ url: base + "/v1/traces", timeoutMillis: 500 }), {
      maxQueueSize: 256, maxExportBatchSize: 32, scheduledDelayMillis: 1000, exportTimeoutMillis: 600,
    })] });
    tracerProvider.register();
    meterProvider = new MeterProvider({ resource, readers: [new PeriodicExportingMetricReader({
      exporter: new OTLPMetricExporter({ url: base + "/v1/metrics", timeoutMillis: 500 }),
      exportIntervalMillis: 1000, exportTimeoutMillis: 600,
    })] });
    metrics.setGlobalMeterProvider(meterProvider);
  }
  let parent = ROOT_CONTEXT;
  if (options.traceparent !== undefined) {
    if (!traceparentPattern.test(options.traceparent)) throw new Error("Invalid admitted trace context");
    const [, traceId, spanId, flags] = options.traceparent.split("-");
    parent = trace.setSpanContext(parent, { traceId: traceId!, spanId: spanId!, traceFlags: flags === "01" ? TraceFlags.SAMPLED : TraceFlags.NONE, isRemote: true });
  }
  let closed = false;
  return {
    run<T>(callback: () => Promise<T>): Promise<T> {
      return context.with(parent, () => trace.getTracer("adp.codex-harness").startActiveSpan("adp.codex.run", {
        attributes: { "adp.run.id": options.runId, "adp.persona.key": options.persona },
      }, async span => {
        try { const value = await callback(); span.setStatus({ code: SpanStatusCode.OK }); return value; }
        catch (error) { span.setStatus({ code: SpanStatusCode.ERROR }); span.setAttribute("error.type", "run_failed"); throw error; }
        finally { span.end(); }
      }));
    },
    async shutdown(): Promise<void> {
      if (closed) return;
      closed = true;
      // Exporter outages never become a Task outcome or hold abort/cleanup open.
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          Promise.allSettled([tracerProvider?.shutdown(), meterProvider?.shutdown({ timeoutMillis: 650 })]),
          new Promise<void>(resolve => { timer = setTimeout(resolve, 750); }),
        ]);
      } finally { if (timer) clearTimeout(timer); }
    },
  };
}
