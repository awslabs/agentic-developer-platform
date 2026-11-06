"""Product source and native provider observations must agree before qualification."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest

from l5_services.adapters.transform.authority import TransformWorkAuthority
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.qualification_driver import TransformQualificationCollector
from l5_services.product_client import ProductClient
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION
from l5_services.workers.execution import AuthorityRevoked
from test_executor_source import fixture as sealed_source
from test_tf03_adapter import pinned


NOW = datetime(2026, 10, 29, tzinfo=UTC)


class NativeObserver:
    def __init__(self, native, intent, patch, patch_ref):
        self.calls = []
        self.native = native
        self.intent = intent
        self.patch = patch
        self.patch_ref = patch_ref
        self.readback = {**{key: intent[key] for key in (
            "tenant_id", "project_id", "run_id", "executor_binding_ref", "image_digest",
            "binary_sha256", "definition_revision")},
            "executor_name": native["executor_name"], "executor_uid": native["executor_uid"],
            "readback": {"native_id": native["native_id"], "attempt_token": native["attempt_token"],
                         "input_hash": native["input_hash"], "state": "completed"}}

    async def read_provider(self, *, intent, authority):
        authority.assert_current()
        self.calls.append("read")
        assert intent == self.intent
        return self.readback

    async def collect_provider(self, *, intent, authority):
        authority.assert_current()
        self.calls.append("collect")
        assert intent == self.intent
        return {"native_job_ref": self.native, "state": "completed",
                "artifacts": {"original_patch": self.patch_ref}}

    async def read_original_patch(self, *, reference, authority):
        authority.assert_current()
        self.calls.append("patch")
        assert reference == self.patch_ref
        return self.patch


@pytest.fixture
async def observations():
    source, manifest, work, _ = sealed_source()
    manifest = deepcopy(manifest)
    manifest["archive"]["version"] = "v1"
    run, _, attempt = pinned()
    run.admission = deepcopy(run.admission)
    run.input = deepcopy(run.input)
    run.product_id = manifest["scope"]["product_id"]
    run.input["sources"] = manifest["sources"]
    run.admission["scope"] = manifest["scope"]
    run.admission["transform_settings"]["region"] = "us-east-1"
    run.admission["transform_settings"]["write_paths"] = ["src/example.py"]
    run.admission["source_snapshots"] = [{"snapshot_id": manifest["source_snapshot_ids"][0]}]
    run.manifest = manifest
    input_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    token = sha256(f"{run.tenant_id}:{run.run_id}:{TRANSFORM_SUBMIT_OPERATION}:{input_hash}".encode()).hexdigest()
    attempt = replace(attempt, input_hash=input_hash, idempotency_token=token)
    intent = TransformAdapter(object())._intent(run, attempt)
    native = {"tenant_id": run.tenant_id, "project_id": run.project_id,
              "executor_name": intent["executor_name"], "executor_uid": "pod-uid-a",
              "native_id": "session-a", "attempt_token": token, "input_hash": input_hash}
    run.native_job_ref = native
    patch = b"diff --git a/src/example.py b/src/example.py\n--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-original\n+changed\n"
    patch_ref = {"tenant_id": run.tenant_id, "project_id": run.project_id,
                 "artifact_id": "provider-patch-a", "version": "v1", "sha256": sha256(patch).hexdigest(),
                 "bytes": len(patch), "media_type": "text/x-diff"}
    observer = NativeObserver(native, intent, patch, patch_ref)
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/example",
                "region": "us-east-1"}
    authority = TransformWorkAuthority(scope=manifest["scope"], source_request=work.source_request,
                                       capture_limits=work.capture_limits, credentials=None, identity=identity,
                                       expires_at=NOW + timedelta(minutes=5), clock=lambda: NOW)
    source_archive = b"".join([part async for part in source.stream_source(
        "product-a", "project-a", "sealed-a", work.source_request)])
    inventory = {"source_ref": manifest["sources"][0], "exclusions": [],
                 "files": [{key: item[key] for key in ("path", "sha256", "bytes")}
                           for item in manifest["files"]]}
    inventory_bytes = json.dumps(inventory).encode()
    product_requests = []

    def respond(request):
        product_requests.append(request)
        expected = "/internal/v1/products/product-a/projects/project-a/source-snapshots/sealed-a/"
        assert request.method == "POST" and request.url.path.startswith(expected)
        assert request.headers["X-L5-Delegation-ID"] == "delegation-a"
        assert json.loads(request.content) == work.source_request
        resource = request.url.path.removeprefix(expected)
        if resource == "read":
            return httpx.Response(200, json={"snapshot_id": "sealed-a", "source_ref": manifest["sources"][0],
                                              "content": manifest["archive"], "exclusions": [],
                                              "file_manifest": {"bytes": len(inventory_bytes), "version": "v1",
                                                                "sha256": sha256(inventory_bytes).hexdigest()},
                                              "file_count": 1})
        if resource in {"manifest", "content"}:
            value = inventory_bytes if resource == "manifest" else source_archive
            version = "v1" if resource == "manifest" else manifest["archive"]["version"]
            assert request.headers["Range"] == f"bytes=0-{len(value) - 1}"
            return httpx.Response(206, content=value, headers={
                "Content-Range": f"bytes 0-{len(value) - 1}/{len(value)}",
                "X-Artifact-Sha256": sha256(value).hexdigest(), "X-Artifact-Version": version,
                "X-Artifact-Size": str(len(value))})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://example.invalid") as client:
        yield (TransformQualificationCollector(ProductClient(client, "delegation-a"), observer),
               run, attempt, patch_ref, authority, observer, product_requests)


@pytest.mark.asyncio
async def test_scoped_product_archive_and_independent_provider_patch_are_observed(observations):
    collector, run, attempt, reference, authority, native, requests = observations
    result = await collector.collect(run, attempt, reference, authority)
    assert result["status"] == "observed" and "passed" not in result.values()
    assert result["manifest_sha256"] == attempt.input_hash
    assert result["source_snapshot_ids"] == run.manifest["source_snapshot_ids"]
    assert result["native_job_ref"] == run.native_job_ref
    assert result["original_patch"] == reference
    assert result["provider_pins"]["image_digest"] == run.admission["transform_qualification"]["image_digest"]
    assert len(result["source_tree"]) == 40
    assert native.calls == ["read", "collect", "patch"]
    assert len(requests) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "foreign_product", "stale_work", "wrong_revision", "changed_source", "unqualified",
    "missing_readback", "wrong_native", "still_running", "foreign_collection",
    "wrong_patch_revision", "corrupt_patch", "missing_provider", "foreign_region", "expired_grant",
    "invalid_patch_ref", "missing_patch", "missing_admission",
])
async def test_missing_or_falsified_provider_source_does_not_qualify(observations, change):
    collector, run, attempt, reference, authority, native, requests = observations
    if change == "foreign_product":
        authority.scope = {**authority.scope, "product_id": "foreign"}
    elif change == "stale_work":
        authority.source_request["operation_id"] = "other-run"
    elif change == "wrong_revision":
        run.manifest["sources"][0]["record_revision"] = "b" * 40
    elif change == "changed_source":
        run.manifest["files"][0]["sha256"] = "a" * 64
    elif change == "unqualified":
        run.admission["transform_qualification"]["read"]["status"] = "unqualified"
        run.admission["transform_qualification"]["read"]["receipt_sha256"] = None
    elif change == "foreign_region":
        authority.provider_identity["region"] = "another-region"
    elif change == "expired_grant":
        authority._clock = lambda: NOW + timedelta(minutes=6)
    elif change == "missing_readback":
        native.readback = None
    elif change == "wrong_native":
        native.readback["readback"]["native_id"] = "foreign-session"
    elif change == "still_running":
        native.readback["readback"]["state"] = "running"
    elif change == "foreign_collection":
        native.native = {**native.native, "executor_uid": "other-uid"}
    elif change == "wrong_patch_revision":
        reference = {**reference, "version": "v2"}
    elif change == "invalid_patch_ref":
        reference = {**reference, "bytes": True}
    elif change == "missing_patch":
        reference = None
    elif change == "missing_admission":
        run.admission = None
    elif change == "corrupt_patch":
        native.patch = b"forged patch bytes"
    else:
        collector.provider = object()
    with pytest.raises((AuthorityRevoked, ValueError, PermissionError, AttributeError)):
        await collector.collect(run, attempt, reference, authority)
    if change in {"foreign_product", "stale_work", "wrong_revision", "changed_source",
                  "unqualified", "missing_provider", "foreign_region", "expired_grant",
                  "missing_admission"}:
        assert native.calls == []
        assert requests == []
