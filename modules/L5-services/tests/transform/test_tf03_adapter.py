"""Synthetic executor receipts test the L5 boundary, not live provider support."""

import asyncio
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.work_store import AttemptContext, TRANSFORM_SUBMIT_OPERATION


SOURCE = {"tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "repo",
          "record_type": "repository_commit", "record_id": "example/widget", "record_revision": "a" * 40}
TEST_POLICY_REF = {"record_id": "policy-a"}
TEST_POLICY = {"test_policy_ref": TEST_POLICY_REF, "image_digest": "sha256:" + "b" * 64,
               "bundle_sha256": "c" * 64, "lock_sha256": "d" * 64, "oracle_version": "oracle-1",
               "commands": [{"argv": ["/oracle/check"], "cwd": ".", "environment": {},
                             "timeout_seconds": 10, "minimum_test_count": 1}]}
SETTINGS = {"executor_binding_ref": {**SOURCE, "record_type": "executor_binding"},
            "read_paths": ["src"], "write_paths": ["src/app.py"], "excluded_paths": [".git"],
            "test_policy_ref": TEST_POLICY_REF}
SCOPE = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"}
VERDICT = {"schema_version": "1.0", "interface": "synthetic", "cli_version": "synthetic",
           "binary_sha256": "a" * 64, "image_digest": "sha256:" + "b" * 64,
           "definition_revision": "synthetic", "region_qualification": "qualified",
           "executor_qualification": "qualified",
           **{name: {"status": "supported", "reason": "synthetic fixture", "receipt_sha256": "c" * 64}
              for name in ("tool_isolation", "submit", "read", "resume")},
           "cancel": {"status": "unsupported", "reason": "no remote stop"}}


class FakeExecutor:
    def __init__(self):
        self.launches = []
        self.reads = []
        self.stops = []
        self.receipt = None
        self.native_ref = None

    async def launch_provider(self, *, intent, manifest, authority):
        assert authority is SESSION
        self.launches.append(intent)
        return self.receipt

    async def read_provider(self, *, intent, authority):
        assert authority is SESSION
        self.reads.append(intent)
        return self.receipt

    async def collect_provider(self, *, intent, authority):
        assert authority is SESSION
        return {"native_job_ref": self.native_ref, "state": "completed", "artifacts": {"patch": "sealed"}}

    async def request_stop(self, *, intent, authority):
        self.stops.append(intent)


SESSION = object()


def pinned():
    run = SimpleNamespace(service_id="transform", tenant_id="tenant-a", project_id="project-a",
                          run_id="run-a", input={"sources": [SOURCE], "transform_input": {
                              "goal": {"kind": "refactor", "summary": "Simplify"}}},
                          admission={"scope": SCOPE, "source_snapshots": [{"snapshot_id": "seal-a"}],
                                     "transform_settings": SETTINGS, "transform_test_policy": TEST_POLICY,
                                     "transform_qualification": VERDICT})
    manifest = {"scope": SCOPE, "sources": [SOURCE], "run_id": "run-a", "source_snapshot_ids": ["seal-a"]}
    input_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    token = sha256(f"tenant-a:run-a:{TRANSFORM_SUBMIT_OPERATION}:{input_hash}".encode()).hexdigest()
    from l5_services.adapters.transform.baseline import pinned_test_policy

    _, policy_sha256 = pinned_test_policy(run.admission)
    attempt = AttemptContext("attempt-a", "run-a", "work-a", 1, token, input_hash, None, True,
                             baseline_policy_sha256=policy_sha256, baseline_receipts=[{}])
    return run, manifest, attempt


@pytest.mark.asyncio
async def test_bounded_provider_bridge_preserves_native_identity():
    run, manifest, attempt = pinned()
    bridge = FakeExecutor()
    adapter = TransformAdapter(bridge)
    intent = adapter._intent(run, attempt)
    bridge.receipt = {"executor_name": intent["executor_name"], "executor_uid": "uid-a",
                      "readback": {"native_id": "session-a", "attempt_token": attempt.idempotency_token,
                                   "input_hash": attempt.input_hash, "state": "running"}}
    submitted = await adapter.submit(run, manifest, attempt, SESSION)
    native = submitted.native_job_ref
    bridge.native_ref = native
    assert native["executor_uid"] == "uid-a" and native["native_id"] == "session-a"
    assert bridge.launches[0]["image_digest"] == VERDICT["image_digest"]
    assert SESSION not in bridge.launches[0].values()
    assert (await adapter.reconcile_submit(run, attempt, SESSION)).native_job_ref == native
    assert len(bridge.launches) == 1 and len(bridge.reads) == 1
    assert (await adapter.observe(run, native, attempt, SESSION)).classification == "running"
    assert (await adapter.cancel(run, native, attempt, SESSION)).classification == "unknown"
    assert bridge.stops == []
    assert (await adapter.collect(run, native, attempt, SESSION)).result == {"patch": "sealed"}


@pytest.mark.asyncio
async def test_collection_requires_an_original_provider_patch():
    run, _, attempt = pinned()
    bridge = FakeExecutor()
    adapter = TransformAdapter(bridge, require_original_patch=True)
    intent = adapter._intent(run, attempt)
    native = {"tenant_id": run.tenant_id, "project_id": run.project_id,
              "executor_name": intent["executor_name"], "executor_uid": "uid-a",
              "native_id": "session-a", "attempt_token": intent["attempt_token"],
              "input_hash": intent["input_hash"]}
    bridge.native_ref = native
    bridge.receipt = {**{key: intent[key] for key in (
        "tenant_id", "project_id", "run_id", "executor_binding_ref", "image_digest",
        "binary_sha256", "definition_revision")},
        "executor_name": intent["executor_name"], "executor_uid": native["executor_uid"],
        "readback": {"native_id": native["native_id"], "attempt_token": intent["attempt_token"],
                     "input_hash": intent["input_hash"], "state": "completed"}}
    assert (await adapter.collect(run, native, attempt, SESSION)).classification == "unknown"

    async def empty_collection(*, intent, authority):
        return {"native_job_ref": native, "state": "completed", "artifacts": {}}

    bridge.collect_provider = empty_collection
    assert (await adapter.collect(run, native, attempt, SESSION)).classification == "unknown"


@pytest.mark.asyncio
async def test_strict_collection_requires_current_completed_native_readback():
    run, _, attempt = pinned()
    bridge = FakeExecutor()
    adapter = TransformAdapter(bridge, require_original_patch=True)
    intent = adapter._intent(run, attempt)
    native = {"tenant_id": run.tenant_id, "project_id": run.project_id,
              "executor_name": intent["executor_name"], "executor_uid": "uid-a",
              "native_id": "session-a", "attempt_token": intent["attempt_token"],
              "input_hash": intent["input_hash"]}
    bridge.native_ref = native
    bridge.receipt = {**{key: intent[key] for key in (
        "tenant_id", "project_id", "run_id", "executor_binding_ref", "image_digest",
        "binary_sha256", "definition_revision")},
        "executor_name": native["executor_name"], "executor_uid": native["executor_uid"],
        "readback": {"native_id": native["native_id"], "attempt_token": native["attempt_token"],
                     "input_hash": native["input_hash"], "state": "running"}}
    calls = []

    async def collect_provider(*, intent, authority):
        calls.append(intent)
        return {"native_job_ref": native, "state": "completed", "artifacts": {"patch": "fake"}}

    bridge.collect_provider = collect_provider
    assert (await adapter.collect(run, native, attempt, SESSION)).classification == "unknown"
    bridge.receipt["readback"]["state"] = "completed"
    bridge.receipt["executor_uid"] = "replacement-uid"
    assert (await adapter.collect(run, native, attempt, SESSION)).classification == "unknown"
    assert calls == []
    bridge.receipt["executor_uid"] = native["executor_uid"]
    assert (await adapter.collect(run, native, attempt, SESSION)).classification == "unknown"
    assert calls == [intent]


@pytest.mark.asyncio
async def test_strict_submission_rejects_missing_callbacks_before_launch():
    run, manifest, attempt = pinned()
    bridge = FakeExecutor()
    adapter = TransformAdapter(bridge, detached_runner=object(), retention_store=object(),
                               require_original_patch=True)
    with pytest.raises(PermissionError, match="callbacks"):
        await adapter.submit(run, manifest, attempt, SESSION)
    assert bridge.launches == []


@pytest.mark.asyncio
async def test_strict_readback_requires_admitted_executor_and_provider_pins():
    run, _, attempt = pinned()
    bridge = FakeExecutor()
    adapter = TransformAdapter(bridge, require_original_patch=True)
    intent = adapter._intent(run, attempt)
    bridge.receipt = {"executor_name": intent["executor_name"], "executor_uid": "uid-a",
                      "readback": {"native_id": "session-a", "attempt_token": attempt.idempotency_token,
                                   "input_hash": attempt.input_hash, "state": "running"}}
    assert (await adapter.reconcile_submit(run, attempt, SESSION)).classification == "unknown"
    bridge.receipt.update({key: intent[key] for key in (
        "tenant_id", "project_id", "run_id", "executor_binding_ref", "image_digest",
        "binary_sha256", "definition_revision")})
    assert (await adapter.reconcile_submit(run, attempt, SESSION)).classification == "accepted"
    for key in ("tenant_id", "project_id", "run_id", "executor_binding_ref", "image_digest",
                "binary_sha256", "definition_revision"):
        original = bridge.receipt[key]
        bridge.receipt[key] = "substituted"
        assert (await adapter.reconcile_submit(run, attempt, SESSION)).classification == "unknown"
        bridge.receipt[key] = original


@pytest.mark.asyncio
async def test_unqualified_or_mismatched_receipts_never_launch_or_claim_success():
    run, manifest, attempt = pinned()
    bridge = FakeExecutor()
    adapter = TransformAdapter(bridge)
    run.admission["transform_qualification"] = {**VERDICT, "tool_isolation": {
        "status": "unsupported", "reason": "no isolation"}}
    with pytest.raises(PermissionError):
        await adapter.submit(run, manifest, attempt, SESSION)
    run.admission["transform_qualification"] = VERDICT
    with pytest.raises(ValueError):
        await adapter.submit(run, {**manifest, "sources": []}, attempt, SESSION)
    assert bridge.launches == []
    bad_attempt = AttemptContext("attempt-a", "run-a", "work-a", 1, "0" * 64,
                                 attempt.input_hash, None, True)
    with pytest.raises(ValueError):
        await adapter.submit(run, manifest, bad_attempt, SESSION)
    assert (await adapter.reconcile_submit(run, attempt, SESSION)).classification == "unknown"
    intent = adapter._intent(run, attempt)
    bridge.receipt = {"executor_name": intent["executor_name"], "executor_uid": "uid-a",
                      "readback": {"native_id": "session-a", "attempt_token": "wrong",
                                   "input_hash": attempt.input_hash, "state": "running"}}
    assert (await adapter.reconcile_submit(run, attempt, SESSION)).classification == "unknown"
    assert bridge.launches == []


@pytest.mark.asyncio
async def test_slow_bridge_is_bounded_and_cannot_be_confused_with_failure():
    run, manifest, attempt = pinned()

    class SlowExecutor(FakeExecutor):
        async def launch_provider(self, *, intent, manifest, authority):
            await asyncio.sleep(0.1)

    adapter = TransformAdapter(SlowExecutor(), call_seconds=0.001)
    with pytest.raises(TimeoutError):
        await adapter.submit(run, manifest, attempt, SESSION)
    with pytest.raises(ValueError):
        TransformAdapter(SlowExecutor(), call_seconds=11)
