"""Bounded L5 boundary for a separately qualified, isolated Transform executor."""

import asyncio
import json
from hashlib import sha256
from typing import Protocol

from fastapi import HTTPException
from httpx import HTTPStatusError

from l5_services.adapters.protocol import AdapterOutcome
from l5_services.adapters.transform.qualification import NativeReadback, ProviderQualification, reconcile_attempt
from l5_services.adapters.transform.provenance import capture_original_patch, verify_original_patch
from l5_services.adapters.transform.dependency_oracle import compare_dependency_upgrade
from l5_services.adapters.transform.scope_oracle import check_write_scope
from l5_services.adapters.transform.verification import verify_detached_checks
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION


class TransformExecutorBridge(Protocol):
    """Trusted L5 transport; never reuse a validation Task identity for these calls."""

    async def launch_provider(self, *, intent: dict, manifest: dict, authority) -> dict | None: ...

    async def read_provider(self, *, intent: dict, authority) -> dict | None: ...

    async def collect_provider(self, *, intent: dict, authority) -> dict | None: ...

    async def request_stop(self, *, intent: dict, native_job_ref: dict, authority) -> dict | None: ...

    async def read_original_patch(self, *, reference: dict, authority) -> bytes: ...

    async def read_verified_trees(self, *, manifest: dict, native_job_ref: dict,
                                  original_patch: dict, authority) -> dict: ...

    async def read_test_baseline(self, *, manifest: dict, authority) -> list[dict] | None: ...

    async def read_sealed_source(self, *, manifest: dict, authority) -> dict[str, bytes]: ...


class TransformAdapter:
    def __init__(self, bridge: TransformExecutorBridge | None, *, call_seconds=5,
                 detached_runner=None, retention_store=None, require_original_patch=False):
        if not 0 < call_seconds <= 10:
            raise ValueError("Transform control calls must be bounded")
        self.bridge = bridge
        self.call_seconds = call_seconds
        self.detached_runner = detached_runner
        self.retention_store = retention_store
        self.require_original_patch = require_original_patch

    def _qualification(self, run):
        qualification = ProviderQualification.model_validate(run.admission["transform_qualification"])
        if self.bridge is None or not qualification.ready_for_submission:
            raise PermissionError("qualified L5 Transform executor is unavailable")
        return qualification

    def _intent(self, run, attempt):
        qualification = self._qualification(run)
        expected_token = sha256(
            f"{run.tenant_id}:{run.run_id}:{TRANSFORM_SUBMIT_OPERATION}:{attempt.input_hash}".encode()
        ).hexdigest()
        if (run.service_id != "transform" or attempt.run_id != run.run_id
                or attempt.idempotency_token != expected_token):
            raise ValueError("Transform run and attempt differ")
        return {"executor_name": "tf-" + sha256(
                    f"{run.tenant_id}:{run.run_id}:{attempt.idempotency_token}".encode()).hexdigest()[:40],
                "tenant_id": run.tenant_id, "product_id": run.admission["scope"]["product_id"], "project_id": run.project_id, "run_id": run.run_id,
                "attempt_token": attempt.idempotency_token, "input_hash": attempt.input_hash,
                "executor_binding_ref": run.admission["transform_settings"]["executor_binding_ref"],
                "image_digest": qualification.image_digest, "binary_sha256": qualification.binary_sha256,
                "definition_revision": qualification.definition_revision,
                "goal": run.input["transform_input"]["goal"],
                "read_paths": run.admission["transform_settings"]["read_paths"],
                "write_paths": run.admission["transform_settings"]["write_paths"],
                "excluded_paths": run.admission["transform_settings"]["excluded_paths"],
                "definition_ref": run.admission["transform_settings"].get("definition_ref")}

    def _native(self, intent, receipt, qualification, attempt):
        if not isinstance(receipt, dict) or receipt.get("executor_name") != intent["executor_name"]:
            return None, None
        if self.require_original_patch and any(receipt.get(key) != intent[key] for key in (
                "tenant_id", "project_id", "run_id", "executor_binding_ref", "image_digest",
                "binary_sha256", "definition_revision")):
            return None, None
        uid = receipt.get("executor_uid")
        if not isinstance(uid, str) or not uid:
            return None, None
        try:
            readback = NativeReadback.model_validate(receipt.get("readback"))
        except (ValueError, TypeError):
            return None, None
        verdict = reconcile_attempt(attempt, qualification, readback)
        if verdict.status != "observed":
            return None, None
        native = {"tenant_id": intent["tenant_id"], "project_id": intent["project_id"],
                  "executor_name": intent["executor_name"], "executor_uid": uid,
                  "native_id": verdict.native_id, "attempt_token": intent["attempt_token"],
                  "input_hash": intent["input_hash"]}
        return native, verdict.provider_state

    def _check_native(self, intent, native):
        if not isinstance(native, dict) or any(native.get(key) != intent[key] for key in (
                "tenant_id", "project_id", "executor_name", "attempt_token", "input_hash")):
            raise ValueError("native identity differs from committed attempt")
        if not native.get("executor_uid") or not native.get("native_id"):
            raise ValueError("native identity is incomplete")

    async def submit(self, run, manifest, attempt, session):
        from l5_services.adapters.transform.baseline import pinned_test_policy

        if self.require_original_patch and (self.detached_runner is None or self.retention_store is None
                or self.bridge is None or any(not callable(getattr(self.bridge, method, None))
                for method in ("launch_provider", "read_provider", "collect_provider", "request_stop",
                               "read_original_patch", "read_verified_trees", "read_test_baseline",
                               "read_sealed_source"))):
            raise PermissionError("qualified Transform execution callbacks are unavailable")
        policy, policy_sha256 = pinned_test_policy(run.admission)
        if (attempt.baseline_policy_sha256 != policy_sha256
                or not isinstance(attempt.baseline_receipts, list)
                or len(attempt.baseline_receipts) != len(policy["commands"])):
            raise ValueError("provider submission requires a committed detached baseline")
        intent = self._intent(run, attempt)
        expected = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (attempt.input_hash != expected or manifest.get("scope") != run.admission["scope"]
                or manifest.get("sources") != run.input["sources"]
                or manifest.get("run_id") != run.run_id
                or manifest.get("source_snapshot_ids") != [
                    snapshot["snapshot_id"] for snapshot in run.admission["source_snapshots"]]):
            raise ValueError("provider launch differs from sealed source and attempt")
        if self.require_original_patch:
            from l5_services.adapters.transform.provider_job import finite_provider_job

            intent["provider_job"] = finite_provider_job(
                intent, manifest, run.admission["transform_qualification"],
                run.admission.get("limits"))
        receipt = await asyncio.wait_for(self.bridge.launch_provider(
            intent=intent, manifest=manifest, authority=session), timeout=self.call_seconds)
        native, _ = self._native(intent, receipt, self._qualification(run), attempt)
        return AdapterOutcome("accepted", native_job_ref=native) if native else AdapterOutcome("unknown")

    async def _read_provider(self, intent, session):
        try:
            return await asyncio.wait_for(self.bridge.read_provider(
                intent=intent, authority=session), timeout=self.call_seconds)
        except HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            return None
        except HTTPException as error:
            if error.status_code != 404:
                raise
            return None
        except FileNotFoundError:
            return None

    async def reconcile_submit(self, run, attempt, session):
        intent = self._intent(run, attempt)
        receipt = await self._read_provider(intent, session)
        native, _ = self._native(intent, receipt, self._qualification(run), attempt)
        return AdapterOutcome("accepted", native_job_ref=native) if native else AdapterOutcome("unknown")

    async def observe(self, run, native_job_ref, attempt, session):
        intent = self._intent(run, attempt)
        self._check_native(intent, native_job_ref)
        receipt = await self._read_provider(intent, session)
        native, state = self._native(intent, receipt, self._qualification(run), attempt)
        if native != native_job_ref or state is None:
            return AdapterOutcome("unknown")
        return AdapterOutcome(state, observation={"native_job_ref": native, "outcome": state,
                                                  "executor_uid": native["executor_uid"]})

    async def collect(self, run, native_job_ref, attempt, session):
        intent = self._intent(run, attempt)
        self._check_native(intent, native_job_ref)
        if self.require_original_patch:
            observed = await self._read_provider(intent, session)
            native, state = self._native(intent, observed, self._qualification(run), attempt)
            if native != native_job_ref or state != "completed":
                return AdapterOutcome("unknown")
        receipt = await asyncio.wait_for(self.bridge.collect_provider(
            intent=intent, authority=session), timeout=self.call_seconds)
        if (not isinstance(receipt, dict) or receipt.get("native_job_ref") != native_job_ref
                or receipt.get("state") != "completed" or not isinstance(receipt.get("artifacts"), dict)):
            return AdapterOutcome("unknown")
        artifacts = receipt["artifacts"]
        if "original_patch" not in artifacts:
            return (AdapterOutcome("unknown") if self.require_original_patch
                    else AdapterOutcome("completed", result=artifacts))
        if set(artifacts) != {"original_patch"}:
            raise ValueError("original patch collection contains unverified artifacts")
        reference = artifacts["original_patch"]
        if (not isinstance(reference, dict) or set(reference) != {
                "tenant_id", "project_id", "artifact_id", "version", "sha256", "bytes", "media_type"}
                or reference["tenant_id"] != run.tenant_id or reference["project_id"] != run.project_id
                or not all(isinstance(reference[field], str) and reference[field] for field in (
                    "artifact_id", "version")) or reference["media_type"] != "text/x-diff"
                or type(reference["bytes"]) is not int or reference["bytes"] < 0
                or not isinstance(reference["sha256"], str) or len(reference["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in reference["sha256"])):
            raise ValueError("original patch reference is not scoped and versioned")
        manifest = getattr(run, "manifest", None)
        if (not isinstance(manifest, dict) or manifest.get("run_id") != run.run_id
                or manifest.get("sources") != run.input["sources"]
                or manifest.get("source_snapshot_ids") != [
                    snapshot["snapshot_id"] for snapshot in run.admission["source_snapshots"]]):
            raise ValueError("original patch differs from sealed source manifest")
        contents = await asyncio.wait_for(self.bridge.read_original_patch(
            reference=reference, authority=session), timeout=self.call_seconds)
        qualification = self._qualification(run)
        original = capture_original_patch(contents, native_job_ref, qualification.model_dump(), manifest)
        if original.sha256 != reference["sha256"] or original.bytes != reference["bytes"]:
            raise ValueError("original patch readback differs from retained bytes")
        result = {"original_patch": {
            **reference, "native_job_ref": dict(original.native_job_ref),
            "provider_pins": dict(original.provider_pins),
            "base_manifest_sha256": sha256(original.base_manifest).hexdigest(),
        }, "verification_status": "partial", "tests_before": [], "tests_after": []}
        if not hasattr(self.bridge, "read_verified_trees"):
            result["limitations"] = ["independent_candidate_tree_unavailable"]
            return AdapterOutcome("completed", result=result)
        trees = await asyncio.wait_for(self.bridge.read_verified_trees(
            manifest=manifest, native_job_ref=native_job_ref, original_patch=reference,
            authority=session), timeout=self.call_seconds)
        if not isinstance(trees, dict) or set(trees) != {
                "base_files", "candidate_files", "base_modes", "candidate_modes"}:
            result["limitations"] = ["independent_candidate_tree_unavailable"]
            return AdapterOutcome("completed", result=result)
        base = trees["base_files"]
        if (not isinstance(base, dict) or not isinstance(trees["candidate_files"], dict)
                or not isinstance(manifest.get("files"), list) or not manifest["files"]
                or set(base) != {entry["path"] for entry in manifest["files"]}
                or any(not isinstance(base[entry["path"]], bytes)
                       or sha256(base[entry["path"]]).hexdigest() != entry["sha256"]
                       or len(base[entry["path"]]) != entry["bytes"]
                       for entry in manifest["files"])):
            raise ValueError("detached baseline differs from sealed source files")
        try:
            if trees["base_modes"] != {entry["path"]: entry.get("mode", "100644")
                                      for entry in manifest["files"]}:
                raise ValueError("base file modes differ from sealed source")
            scope = check_write_scope(base, trees["candidate_files"],
                                      base_modes=trees["base_modes"],
                                      candidate_modes=trees["candidate_modes"],
                                      write_paths=run.admission["transform_settings"]["write_paths"],
                                      excluded_paths=run.admission["transform_settings"]["excluded_paths"])
            verify_original_patch(base, scope["candidate_tree"], original, trees["base_modes"])
        except ValueError:
            result["scope_status"] = "failed"
            result["limitations"] = ["candidate_scope_or_patch_rejected"]
            return AdapterOutcome("completed", result=result)
        result["source_trees"] = {key: scope[key] for key in (
            "base_tree", "candidate_tree", "changed_files")}
        result["scope_status"] = "passed"
        goal = run.input["transform_input"]["goal"]
        if goal["kind"] == "dependency_upgrade":
            pins = run.admission.get("transform_artifact_hashes")
            if pins is None:
                result["dependency_comparison"] = {"status": "inconclusive"}
                result["limitations"] = ["target_artifact_pins_unavailable"]
                return AdapterOutcome("completed", result=result)
            try:
                comparison = compare_dependency_upgrade(base, trees["candidate_files"], goal["target"], pins)
            except ValueError:
                result["dependency_comparison"] = {"status": "failed"}
                result["limitations"] = ["dependency_scope_rejected"]
                return AdapterOutcome("completed", result=result)
            result["dependency_comparison"] = {"status": "passed", **comparison}
        else:
            result["dependency_comparison"] = {"status": "not_applicable"}
        policy = run.admission.get("transform_test_policy")
        if policy is None or self.detached_runner is None:
            result["limitations"] = ["detached_verification_not_run"]
            return AdapterOutcome("completed", result=result)
        baseline = attempt.baseline_receipts
        if baseline is None:
            baseline = await asyncio.wait_for(self.bridge.read_test_baseline(
                manifest=manifest, authority=session), timeout=self.call_seconds)
        if baseline is None:
            result["limitations"] = ["pre_submit_baseline_not_run"]
            return AdapterOutcome("completed", result=result)
        checks = await verify_detached_checks(self.detached_runner, policy, base,
                                              trees["candidate_files"], session,
                                              baseline_receipts=baseline,
                                              submitted_at=attempt.started_at,
                                              base_modes=trees["base_modes"],
                                              candidate_modes=trees["candidate_modes"])
        result.update(checks)
        result["checks_status"] = ("passed" if all(
            check["status"] == "passed" for check in checks["tests_before"] + checks["tests_after"])
                                   else "partial")
        return AdapterOutcome("completed", result=result)

    async def cancel(self, run, native_job_ref, attempt, session):
        intent = self._intent(run, attempt)
        self._check_native(intent, native_job_ref)
        if self._qualification(run).cancel.status != "supported":
            return AdapterOutcome("unknown")
        request = {"intent": intent, "authority": session}
        if self.require_original_patch:
            request["native_job_ref"] = native_job_ref
        await asyncio.wait_for(self.bridge.request_stop(**request),
                               timeout=self.call_seconds)
        return await self.observe(run, native_job_ref, attempt, session)
