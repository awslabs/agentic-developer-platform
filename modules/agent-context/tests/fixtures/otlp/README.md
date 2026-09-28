# Ingestion OTLP compatibility fixture (#6122)

`legacy-protobuf-3.20.3.hex` is a serialized
`opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest`, generated on
2026-09-26 in the retained `security25-ingestion:6156` image (Docker config ID
`sha256:23acbf53ffc016a15582e676213376030517b81efe1694888cb14f329ef6b353`).
That image contains protobuf 3.20.3, opentelemetry-proto/exporter 1.15.0,
opentelemetry-sdk 1.45.0, and grpcio 1.84.0. This is an explicitly identified
historical image, not a build of this PR or a claim about deployed artifacts.

The request has one resource group, one scope group, and one span:

- Name: `legacy-ingest`
- Trace ID: bytes `00` through `0f`
- Span ID: bytes `10` through `17`
- Start/end times: 1 / 2 nanoseconds
- Attribute `source.file`: `legacy-π.py`

The hex file SHA-256 is
`66d17310129ea645094b7ea74e97404bc08a818c6a917b39a38d6768fc012a54`.
It contains no actual repository content, tokens, or user data.

Run the test directly with the image's installed dependency tree (use an
absolute checkout path for the mount):

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 \
  --mount type=bind,src=/absolute/checkout/modules/agent-context,dst=/fixture,readonly \
  --entrypoint python security25-ingestion:6156 \
  /fixture/tests/test_otlp_wire_compatibility.py
```

The collector binds only to ephemeral loopback. The tests exercise real gRPC
serialization, legacy wire decoding, SDK span IDs and attributes, permission
denial, unreachable collector, malformed input, actual ingestion tracing setup
and shutdown flush, and tracing disabled. Application setup runs in a fresh
interpreter with a minimal environment so process-global OTel state and ambient
credentials are not inherited. The legacy exporter's outage retry policy takes
about 63 seconds; current exporter versions have a bounded overall timeout.

The same seven tests run in Agent Context CI against its resolved dependency
tree. Local modern validation used protobuf 7.36.2, OTel SDK/proto/exporter
1.45.0, and grpcio 1.84.0. This proves wire compatibility for these tested
combinations; it does not establish compatibility of all ingestion packages
with newer protobuf. In particular, the retained image's codegraphcontext
requires `protobuf>=3.20,<3.21`, and opentelemetry-proto 1.15.0 requires
`protobuf>=3.19,<5`. No dependency constraints were overridden here.

Remaining #6122 acceptance includes resolving these package constraints,
build/startup/indexer verification for all six images, immutable artifact
rescans and complete original finding reconciliation, applicable owner/risk
decisions, and separately authorized publication/rollout. These fixtures do not
close the image-family issue or establish a vulnerability-free image.
