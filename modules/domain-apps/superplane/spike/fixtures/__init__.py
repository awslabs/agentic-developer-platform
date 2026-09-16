"""Source-derived SkyPilot API fixtures, with provenance.

Issue #5040 (U12), EPIC #4910.

Every payload here is derived from the pinned upstream snapshot: the JSON tags
in ``skypilot/types.go`` and the exact literals the upstream Go client tests
serve from their in-process test servers (``skypilot/client_test.go``). None of
it is shaped around what an ADP-side adapter would find convenient to receive,
which is the fixture-provenance rule the story sets.

Why the upstream tests are the right source: those servers are what the real Go
client is asserted against, so a payload that satisfies them is a payload the
baseline client provably accepts. Inventing a "nicer" response would let U19's
adapter pass against a protocol the baseline never spoke.

Deliberately absent: any serve or jobs payload. The baseline client implements
no such endpoint, so a fixture for one would be fabricated evidence for a
capability the controller does not have.

These are JSON/Python literals rather than YAML because the CI lane installs
only ``modules/gateway[dev]``, which has no PyYAML. Where YAML is the evidence
(SkyServe specs, the onboarding ConfigMap) the tests assert against text.
"""

from __future__ import annotations

from ..provenance import Citation

CLIENT_TEST = "src/superplane-controller/skypilot/client_test.go"
TYPES_GO = "src/superplane-controller/skypilot/types.go"

# GET /api/health
HEALTH_RESPONSE: dict[str, str] = {"status": "healthy", "version": "0.12.0"}
HEALTH_CITATION = Citation(
    path=CLIENT_TEST,
    detail=(
        "TestHealth serves HealthResponse and asserts status=healthy and "
        "version=0.12.0."
    ),
)

# POST /launch -> RequestResponse
LAUNCH_RESPONSE: dict[str, str] = {"request_id": "req-abc-123"}
LAUNCH_CITATION = Citation(
    path=CLIENT_TEST,
    detail="TestLaunch serves RequestResponse{RequestID: 'req-abc-123'}.",
)

# POST /status -> []ClusterInfo, one UP cluster with a head IP.
STATUS_RESPONSE_UP: list[dict[str, object]] = [
    {
        "name": "my-cluster",
        "status": "UP",
        "handle": {"cluster_name": "my-cluster", "head_ip": "10.0.0.1"},
        "launched_at": 0,
        "last_use": "",
        "autostop": 0,
        "to_down": False,
    }
]
STATUS_CITATION = Citation(
    path=CLIENT_TEST,
    detail=(
        "TestStatus serves one ClusterInfo{Name: 'my-cluster', Status: UP, "
        "Handle{ClusterName: 'my-cluster', HeadIP: '10.0.0.1'}} and asserts "
        "status=UP and head_ip=10.0.0.1."
    ),
)

# The no-filter status call, used for existing-state enumeration.
STATUS_RESPONSE_EMPTY: list[dict[str, object]] = []
STATUS_EMPTY_CITATION = Citation(
    path=CLIENT_TEST,
    detail=(
        "TestStatus_NoFilter asserts the request body is empty when no cluster "
        "names are given and returns an empty ClusterInfo list."
    ),
)

# A STOPPED cluster. Derived from the ClusterStatus enum in types.go rather than
# a test literal: existing-state enumeration has to account for STOPPED
# clusters, which still hold a handle.
STATUS_RESPONSE_STOPPED: list[dict[str, object]] = [
    {
        "name": "my-cluster",
        "status": "STOPPED",
        "handle": {"cluster_name": "my-cluster"},
        "launched_at": 0,
        "last_use": "",
        "autostop": 120,
        "to_down": False,
    }
]
STATUS_STOPPED_CITATION = Citation(
    path=TYPES_GO,
    detail=(
        "ClusterStatus constants are INIT, UP and STOPPED; ClusterInfo carries "
        "Autostop and ToDown alongside the handle."
    ),
)

# POST /down -> RequestResponse
DOWN_RESPONSE: dict[str, str] = {"request_id": "req-down-456"}
DOWN_CITATION = Citation(
    path=CLIENT_TEST,
    detail="TestDown serves RequestResponse{RequestID: 'req-down-456'}.",
)

# GET /enabled_clouds
ENABLED_CLOUDS_RESPONSE: dict[str, list[dict[str, object]]] = {
    "enabled_clouds": [
        {"name": "aws", "enabled": True},
        {"name": "gcp", "enabled": False},
        {"name": "nebius", "enabled": True},
    ]
}
ENABLED_CLOUDS_CITATION = Citation(
    path=CLIENT_TEST,
    detail=(
        "TestEnabledClouds_Success serves exactly aws enabled, gcp disabled "
        "and nebius enabled, and asserts three clouds with aws enabled."
    ),
)
# What this fixture is NOT for: it does not drive provider selection. The
# baseline consults /enabled_clouds only as a fallback for GPU types absent
# from an adapter's static pricing map (helpers.go isCloudEnabled, reached
# only from dynamicGPULookup and CheckAvailability), so a cloud listed
# disabled here is still selected for a statically priced type. Selection is
# restricted by the NodePool's configured cloud list instead. An earlier
# revision used this fixture to assert exclusion during selection, which
# asserted behavior the baseline does not have.

# GET /api/stream?request_id=... — raw SSE frames, byte-for-byte as the upstream
# test server writes them, including the blank-line frame separators.
SSE_LAUNCH_SUCCESS: str = (
    "id: 1\nevent: message\ndata: Launching cluster...\n\n"
    "id: 2\nevent: message\ndata: Provisioning resources...\n\n"
    "id: 3\nevent: complete\ndata: Cluster is ready\n\n"
)
SSE_SUCCESS_CITATION = Citation(
    path=CLIENT_TEST,
    detail=(
        "TestStreamProgress_Success writes exactly these three frames and "
        "asserts three events are received, the last being 'complete'."
    ),
)

# An error-terminated stream. The event type comes from
# StreamEventTypeError in types.go.
SSE_LAUNCH_ERROR: str = (
    "id: 1\nevent: message\ndata: Launching cluster...\n\n"
    "id: 2\nevent: error\ndata: no capacity in region\n\n"
)
SSE_ERROR_CITATION = Citation(
    path=TYPES_GO,
    detail=(
        "StreamEventTypeError = 'error' and StreamEventTypeComplete = "
        "'complete' are the terminal event types the onboarder branches on."
    ),
)

# A frame carrying two data lines. Byte-for-byte the sequence upstream's own
# regression test writes, kept as a named fixture because it pins a contract a
# single-data-line stream cannot express: data lines accumulate, they do not
# overwrite. Expected parse: first event data == "line1\nline2".
SSE_MULTILINE_DATA: str = (
    "event: message\ndata: line1\ndata: line2\n\nevent: complete\ndata: done\n\n"
)
SSE_MULTILINE_CITATION = Citation(
    path=CLIENT_TEST,
    detail=(
        "TestStreamProgress_MultilineData writes exactly this sequence and "
        "asserts events[0].Data == 'line1\\nline2', pinning parseSSE's "
        "accumulation of repeated data fields."
    ),
)

# Static pricing rows, transcribed from the adapters' in-source pricing maps.
# Ordering here is intentionally NOT sorted: selection parity must prove the
# implementation orders these, not that the fixture was pre-sorted.
GPU_PRICING_H100: tuple[dict[str, object], ...] = (
    {
        "cloud": "aws",
        "region": "us-east-1",
        "gpu_type": "H100",
        "gpu_count": 8,
        "instance_type": "p5.48xlarge",
        "hourly_cost": 98.32,
        "spot_cost": 40.00,
        "available": True,
    },
    {
        "cloud": "nebius",
        "region": "eu-north1",
        "gpu_type": "H100",
        "gpu_count": 1,
        "instance_type": "gpu-h100-sxm",
        "hourly_cost": 2.95,
        "spot_cost": 0.0,
        "available": True,
    },
    {
        "cloud": "lambda",
        "region": "europe-central-1",
        "gpu_type": "H100",
        "gpu_count": 1,
        "instance_type": "gpu_1x_h100_pcie",
        "hourly_cost": 2.86,
        "spot_cost": 0.0,
        "available": True,
    },
)
PRICING_CITATION = Citation(
    path="src/superplane-controller/adapters/aws.go",
    detail=(
        "awsGPUPricing lists H100 p5.48xlarge at 98.32/hr (spot 40.00) in "
        "us-east-1 and us-west-2. Nebius eu-north1 H100 at 2.95/hr and Lambda "
        "europe-central-1 H100 PCIe at 2.86/hr are the figures recorded in "
        "infra/skypilot-models/qwen35-35b-a3b-serve-eu.yaml's cost header."
    ),
)

# Every fixture paired with its citation, so a test can assert that no fixture
# ships without provenance.
FIXTURE_PROVENANCE: dict[str, Citation] = {
    "HEALTH_RESPONSE": HEALTH_CITATION,
    "LAUNCH_RESPONSE": LAUNCH_CITATION,
    "STATUS_RESPONSE_UP": STATUS_CITATION,
    "STATUS_RESPONSE_EMPTY": STATUS_EMPTY_CITATION,
    "STATUS_RESPONSE_STOPPED": STATUS_STOPPED_CITATION,
    "DOWN_RESPONSE": DOWN_CITATION,
    "ENABLED_CLOUDS_RESPONSE": ENABLED_CLOUDS_CITATION,
    "SSE_LAUNCH_SUCCESS": SSE_SUCCESS_CITATION,
    "SSE_LAUNCH_ERROR": SSE_ERROR_CITATION,
    "SSE_MULTILINE_DATA": SSE_MULTILINE_CITATION,
    "GPU_PRICING_H100": PRICING_CITATION,
}
