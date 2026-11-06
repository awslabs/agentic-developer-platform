"""Collect independent Transform source and provider observations for qualification."""

import asyncio
import json
import re
from hashlib import sha256

from l5_services.adapters.transform.authority import TransformWorkAuthority
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.provenance import _file_tree, capture_original_patch
from l5_services.adapters.transform.source import ProductSealedSource
from l5_services.adapters.transform.qualification_source import RetainedProductSource, TransformQualificationAuthority
from l5_services.product_client import ProductClient
from l5_services.workers.execution import AuthorityRevoked


class TransformQualificationCollector:
    def __init__(self, product_client, provider_control, *, call_seconds=5):
        if not 0 < call_seconds <= 10:
            raise ValueError("Transform qualification reads must be bounded")
        if (not isinstance(product_client, (ProductClient, RetainedProductSource)) or provider_control is None
                or any(not callable(getattr(provider_control, name, None)) for name in (
                    "read_provider", "collect_provider", "read_original_patch"))):
            raise PermissionError("Transform qualification observation channels unavailable")
        self.source = product_client if isinstance(product_client, RetainedProductSource) else ProductSealedSource(product_client)
        self.provider = provider_control
        self.call_seconds = call_seconds

    async def collect(self, run, attempt, expected_patch_ref, authority):
        scope = getattr(authority, "scope", None)
        request = getattr(authority, "source_request", None)
        manifest = getattr(run, "manifest", None)
        native = getattr(run, "native_job_ref", None)
        identity = getattr(authority, "provider_identity", None)
        admission = getattr(run, "admission", None)
        if (not isinstance(admission, dict)
                or not isinstance(admission.get("transform_settings"), dict)
                or not isinstance(authority, (TransformWorkAuthority, TransformQualificationAuthority))
                or isinstance(authority, TransformQualificationAuthority) != isinstance(self.source, RetainedProductSource)
                or not isinstance(identity, dict)
                or not isinstance(identity.get("account_id"), str)
                or re.fullmatch(r"[0-9]{12}", identity["account_id"]) is None
                or not isinstance(identity.get("role_arn"), str)
                or not identity["role_arn"].startswith(
                    f"arn:aws:iam::{identity['account_id']}:role/")
                or not isinstance(identity.get("region"), str) or not identity["region"]
                or identity["region"] != admission["transform_settings"].get("region")
                or not isinstance(request, dict)
                or (isinstance(authority, TransformWorkAuthority)
                    and request.get("delegation_id") != self.source.client._delegation_id)
                or not isinstance(scope, dict)
                or not isinstance(manifest, dict) or not isinstance(native, dict)
                or run.service_id != "transform" or run.tenant_id != scope.get("tenant_id")
                or run.product_id != scope.get("product_id") or run.project_id != scope.get("project_id")
                or run.admission.get("scope") != scope
                or request.get("operation_id") != run.run_id
                or manifest.get("run_id") != run.run_id or manifest.get("scope") != scope
                or not isinstance(run.input, dict)
                or manifest.get("sources") != run.input.get("sources")
                or not isinstance(admission.get("source_snapshots"), list)
                or not admission["source_snapshots"]
                or any(not isinstance(item, dict) or not isinstance(item.get("snapshot_id"), str)
                       for item in admission["source_snapshots"])
                or manifest.get("source_snapshot_ids") != [
                    item["snapshot_id"] for item in admission["source_snapshots"]]
                or sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                != attempt.input_hash):
            raise AuthorityRevoked("Transform qualification source or work differs")
        if any(not callable(getattr(self.provider, name, None)) for name in (
                "read_provider", "collect_provider", "read_original_patch")):
            raise PermissionError("Transform qualification observation channels unavailable")
        adapter = TransformAdapter(self.provider, call_seconds=self.call_seconds,
                                   require_original_patch=True)
        intent = adapter._intent(run, attempt)
        adapter._check_native(intent, native)
        authority.assert_current()
        files = await asyncio.wait_for(self.source.read_sealed_source(
            manifest=manifest, authority=authority), timeout=self.call_seconds)
        authority.assert_current()
        modes = {item["path"]: item.get("mode", "100644") for item in manifest["files"]}
        if set(files) != set(modes):
            raise ValueError("Transform source file inventory differs from Product snapshot")
        source_tree = _file_tree(files, modes)
        if isinstance(authority, TransformQualificationAuthority):
            receipt = await asyncio.wait_for(self.provider.read_provider(intent=intent, authority=authority),
                                             timeout=self.call_seconds)
            if (not isinstance(receipt, dict)
                    or any(receipt.get(key) != intent[key] for key in (
                        "executor_name", "tenant_id", "product_id", "project_id", "run_id", "attempt_token",
                        "input_hash", "executor_binding_ref", "image_digest", "binary_sha256", "definition_revision"))
                    or receipt.get("executor_uid") != native["executor_uid"]
                    or receipt.get("readback") != {"native_id": native["native_id"], "state": "completed",
                        "attempt_token": native["attempt_token"], "input_hash": native["input_hash"]}):
                raise ValueError("Independent provider readback has no completed native identity")
        else:
            observed = await asyncio.wait_for(adapter.observe(run, native, attempt, authority),
                                              timeout=self.call_seconds)
            if (observed.classification != "completed"
                    or not isinstance(observed.observation, dict)
                    or observed.observation.get("native_job_ref") != native):
                raise ValueError("Independent provider readback has no completed native identity")
        if (not isinstance(expected_patch_ref, dict)
                or set(expected_patch_ref) != {"tenant_id", "project_id", "artifact_id", "version",
                                               "sha256", "bytes", "media_type"}
                or expected_patch_ref.get("tenant_id") != scope["tenant_id"]
                or expected_patch_ref.get("project_id") != scope["project_id"]
                or not isinstance(expected_patch_ref.get("artifact_id"), str)
                or not expected_patch_ref["artifact_id"]
                or not isinstance(expected_patch_ref.get("version"), str)
                or not expected_patch_ref["version"]
                or type(expected_patch_ref.get("bytes")) is not int
                or expected_patch_ref["bytes"] < 0
                or expected_patch_ref.get("media_type") != "text/x-diff"):
            raise ValueError("Provider patch reference is not scoped and versioned")
        authority.assert_current()
        collected = await asyncio.wait_for(self.provider.collect_provider(
            intent=intent, authority=authority), timeout=self.call_seconds)
        if (not isinstance(collected, dict) or collected.get("native_job_ref") != native
                or collected.get("state") != "completed"
                or collected.get("artifacts") != {"original_patch": expected_patch_ref}):
            raise ValueError("Provider patch differs from Product-scoped result")
        authority.assert_current()
        contents = await asyncio.wait_for(self.provider.read_original_patch(
            reference=expected_patch_ref, authority=authority), timeout=self.call_seconds)
        original = capture_original_patch(contents, native,
                                          adapter._qualification(run).model_dump(), manifest)
        if original.sha256 != expected_patch_ref["sha256"] or original.bytes != expected_patch_ref["bytes"]:
            raise ValueError("Original provider patch differs from retained bytes")
        authority.assert_current()
        return {"schema_version": "1.0", "service_id": "transform", "status": "observed",
                "scope": dict(scope), "run_id": run.run_id,
                "source_snapshot_ids": list(manifest["source_snapshot_ids"]),
                "manifest_sha256": attempt.input_hash, "source_tree": source_tree,
                "native_job_ref": dict(native), "provider_pins": dict(original.provider_pins),
                "original_patch": dict(expected_patch_ref)}
