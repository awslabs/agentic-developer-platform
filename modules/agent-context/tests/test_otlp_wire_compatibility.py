"""Real OTLP transport acceptance for ingestion's legacy protobuf dependency tree.

Run directly inside the ingestion image with --network none: only the local
collector is contacted. No AWS credentials, production collector, or mocks are
used. The same suite also runs against CI's current OpenTelemetry packages.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess
import sys
import unittest

import grpc
from google.protobuf.message import DecodeError
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as messages
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc as service
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExportResult

FIXTURES = Path(__file__).parent / "fixtures" / "otlp"


class Collector(service.TraceServiceServicer):
    def __init__(self):
        self.requests = []
        self.status = None

    def Export(self, request, context):
        self.requests.append(request)
        if self.status is not None:
            context.abort(self.status, "fixture collector rejects export")
        return messages.ExportTraceServiceResponse()


class OtlpWireCompatibility(unittest.TestCase):
    def setUp(self):
        self.collector = Collector()
        self.server = grpc.server(ThreadPoolExecutor(max_workers=1))
        service.add_TraceServiceServicer_to_server(self.collector, self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.assertGreater(port, 0)
        self.server.start()
        self.endpoint = f"127.0.0.1:{port}"
        self.exporter = OTLPSpanExporter(
            endpoint=f"http://127.0.0.1:{port}", insecure=True, timeout=0.2
        )
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": "fixture-ingestion"})
        )
        self.addCleanup(self.provider.shutdown)
        self.addCleanup(self.exporter.shutdown)
        self.addCleanup(lambda: self.server.stop(0).wait())

    def span(self):
        tracer = self.provider.get_tracer("fixture-scip-ingester")
        with tracer.start_as_current_span("ingest", attributes={"fixture.legacy": True}) as span:
            span.add_event("decoded", {"documents": 2})
        return span

    def test_sdk_export_reaches_real_collector_with_wire_identity(self):
        span = self.span()
        self.assertEqual(self.exporter.export([span]), SpanExportResult.SUCCESS)
        self.assertEqual(len(self.collector.requests), 1)
        resource_spans = self.collector.requests[0].resource_spans[0]
        self.assertIn("fixture-ingestion", str(resource_spans.resource))
        received = resource_spans.scope_spans[0].spans[0]
        self.assertEqual(received.name, "ingest")
        self.assertEqual(received.trace_id, span.context.trace_id.to_bytes(16, "big"))
        self.assertEqual(received.span_id, span.context.span_id.to_bytes(8, "big"))
        self.assertEqual(received.events[0].name, "decoded")
        self.assertTrue(received.attributes[0].value.bool_value)

    def test_permission_denied_is_failure_not_success(self):
        self.collector.status = grpc.StatusCode.PERMISSION_DENIED
        self.assertEqual(self.exporter.export([self.span()]), SpanExportResult.FAILURE)
        self.assertEqual(len(self.collector.requests), 1)

    def test_unavailable_collector_is_failure_not_success(self):
        self.server.stop(0).wait()
        self.assertEqual(self.exporter.export([self.span()]), SpanExportResult.FAILURE)
        self.assertEqual(self.collector.requests, [])

    def test_legacy_protobuf_320_request_reaches_current_collector(self):
        wire = bytes.fromhex((FIXTURES / "legacy-protobuf-3.20.3.hex").read_text())
        request = messages.ExportTraceServiceRequest.FromString(wire)
        with grpc.insecure_channel(self.endpoint) as channel:
            response = service.TraceServiceStub(channel).Export(request, timeout=1)
        self.assertIsInstance(response, messages.ExportTraceServiceResponse)
        received = self.collector.requests[0].resource_spans[0].scope_spans[0].spans[0]
        self.assertEqual(received.name, "legacy-ingest")
        self.assertEqual(received.trace_id.hex(), "000102030405060708090a0b0c0d0e0f")
        self.assertEqual(received.span_id.hex(), "1011121314151617")
        self.assertEqual(received.attributes[0].value.string_value, "legacy-π.py")

    def run_ingestion_tracing(self, enabled):
        # A new interpreter isolates OTel's process-global provider from the
        # rest of the test suite. The subprocess receives no inherited tokens.
        result = subprocess.run(
            [sys.executable, "-c", """
import tracing
assert tracing.setup_tracing() is EXPECT_ENABLED
tracer = tracing.get_tracer("fixture.real-ingestion")
with tracer.start_as_current_span("ingestion_run"):
    with tracer.start_as_current_span("scip_decode"):
        pass
tracing.shutdown_tracing()
""".replace("EXPECT_ENABLED", str(enabled))],
            env={
                "PYTHONPATH": str(Path(__file__).parents[1] / "images" / "ingestion"),
                # setup-python needs its matching libpython, not the system copy.
                "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
                "KNOWLEDGE_LAYER_TRACES_ENABLED": str(enabled).lower(),
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{self.endpoint}",
            },
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_ingestion_setup_and_shutdown_flush_span_tree(self):
        self.run_ingestion_tracing(True)
        spans = [
            span for request in self.collector.requests
            for resource in request.resource_spans
            for scope in resource.scope_spans for span in scope.spans
        ]
        self.assertEqual(len(spans), 2)
        by_name = {span.name: span for span in spans}
        root, child = by_name["ingestion_run"], by_name["scip_decode"]
        self.assertEqual(child.trace_id, root.trace_id)
        self.assertEqual(child.parent_span_id, root.span_id)
        self.assertEqual(root.parent_span_id, b"")

    def test_disabled_ingestion_does_not_export(self):
        self.run_ingestion_tracing(False)
        self.assertEqual(self.collector.requests, [])

    def test_truncated_legacy_wire_is_rejected(self):
        wire = bytes.fromhex((FIXTURES / "legacy-protobuf-3.20.3.hex").read_text())
        with self.assertRaises(DecodeError):
            messages.ExportTraceServiceRequest.FromString(wire[:-1])
        self.assertEqual(self.collector.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
