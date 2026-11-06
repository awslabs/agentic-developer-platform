"""Native release testing against a separately qualified deployment."""

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256

from fastapi import HTTPException

from l5_services.adapters.protocol import AdapterOutcome
from l5_services.devops.test_profiles import ADAPTER_REVISION, QUALIFICATION_GAPS
from l5_services.devops.verification_targets import (
    verification_description,
    verify_observation,
)


class ReleaseVerificationAdapter:
    def __init__(self, evidence_reader, native_results_reader=None, *, clock=None):
        if native_results_reader is evidence_reader:
            raise ValueError("native results and independent observations require distinct readers")
        self.reader = evidence_reader
        self.native_results_reader = native_results_reader
        self.clock = clock or (lambda: datetime.now(UTC))

    def _scope(self, run, session):
        admission = run.admission
        scope = admission["scope"]
        space = admission["native_space_ref"]
        identity = admission["provider_identity"]
        options = run.input["devops_input"]
        if (run.service_id != "devops" or run.input.get("capability") != "release_verification"
                or (run.tenant_id, run.product_id, run.project_id) != (
                    scope["tenant_id"], scope["product_id"], scope["project_id"])
                or (space["tenant_id"], space["project_id"]) != (run.tenant_id, run.project_id)
                or space["source_connection_id"] != admission["connection_ref"]["source_connection_id"]
                or space["record_type"] != "agent_space" or not space["record_id"]
                or (session.account_id, session.region, session.role_arn) != (
                    identity["account_id"], identity["region"], identity["role_arn"])
                or run.manifest.get("verification") != admission.get("verification")
                or run.manifest.get("test_profile_preparation") != admission.get("test_profile_preparation")
                or run.manifest.get("scenarios") != options["scenarios"]
                or run.manifest.get("scope") != scope
                or run.manifest.get("run_id") != run.run_id):
            raise ValueError("verification does not match pinned provider scope")
        return space["record_id"], session.client("devops-agent"), options

    def _description(self, run, attempt):
        manifest_hash = sha256(json.dumps(run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        token = sha256(f"{run.tenant_id}:{run.run_id}:{manifest_hash}".encode()).hexdigest()
        if (attempt.run_id != run.run_id or attempt.input_hash != manifest_hash
                or attempt.idempotency_token != token):
            raise ValueError("verification attempt does not match pinned manifest")
        profile = run.input["devops_input"].get("test_profile_ref")
        if profile is not None and (
                run.admission.get("adapter_revision") != ADAPTER_REVISION
                or run.manifest.get("adapter_revision") != ADAPTER_REVISION
                or run.manifest.get("test_profile_ref") != profile):
            raise ValueError("verification profile contract differs from pinned manifest")
        return verification_description(
            run.admission["scope"], run.admission["verification"], run.manifest["scenarios"],
            run_id=run.run_id, attempt_id=attempt.attempt_id, token=token, test_profile_ref=profile)

    def _outcome(self, run, space_id, task, description):
        if (task.get("agentSpaceId") != space_id or task.get("taskType") != "RELEASE_TESTING"
                or task.get("description") != description or not task.get("taskId")
                or not isinstance(task.get("executionId"), str) or not 0 < len(task["executionId"]) <= 128):
            return AdapterOutcome("unknown")
        execution_id = task["executionId"]
        return AdapterOutcome("accepted", native_job_ref={
            "tenant_id": run.tenant_id, "project_id": run.project_id,
            "source_connection_id": run.admission["connection_ref"]["source_connection_id"],
            "record_type": "native_job", "record_id": execution_id,
            "record_revision": execution_id})

    async def submit(self, run, manifest, attempt, provider_session):
        _, _, options = self._scope(run, provider_session)
        if manifest != run.manifest:
            raise ValueError("verification manifest changed")
        observation = await self.reader.read_deployment(options["deployment_ref"], options["target_ref"])
        specs = await self.reader.read_scenarios(options["scenarios"])
        if not isinstance(observation, dict):
            return AdapterOutcome("definitive_failure",
                                  observation={"code": "verification_target_unverified"})
        try:
            verify_observation(options, run.admission["scope"],
                               {**observation, "scenario_specs": specs}, now=self.clock(),
                               deadline=run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC))
        except HTTPException as error:
            return AdapterOutcome("definitive_failure", observation={"code": error.detail["code"]})
        pinned = run.admission["verification"]
        if (specs != pinned["scenario_specs"] or any(observation[key] != pinned[key] for key in (
                "source_ref", "deployment_ref", "target_ref", "credential_ref", "deployed_sha",
                "isolated", "admin", "network_scope", "deployment_adapter"))):
            return AdapterOutcome("definitive_failure",
                                  observation={"code": "verification_deployment_changed"})
        # Both historical and new contracts may recover accepted effects, but
        # neither may start another ID-only test until profile/version/report
        # qualification exists. A request or reader cannot override this gate.
        return AdapterOutcome("definitive_failure", observation={
            "code": (QUALIFICATION_GAPS[0] if options.get("test_profile_ref")
                     else "native_test_profile_required"), "gaps": list(QUALIFICATION_GAPS)})

    async def reconcile_submit(self, run, attempt, provider_session):
        space_id, client, _ = self._scope(run, provider_session)
        description = self._description(run, attempt)
        token = None
        seen = set()
        matches = []
        while True:
            page = await asyncio.to_thread(client.list_backlog_tasks, agentSpaceId=space_id, limit=100,
                                           **({"nextToken": token} if token else {}))
            if not isinstance(page.get("tasks"), list):
                return AdapterOutcome("unknown")
            matches.extend(task for task in page["tasks"] if task.get("description") == description)
            token = page.get("nextToken")
            if not token:
                break
            if token in seen or len(seen) >= 100:
                return AdapterOutcome("unknown")
            seen.add(token)
        if len(matches) != 1 or not matches[0].get("taskId"):
            return AdapterOutcome("unknown")
        task = (await asyncio.to_thread(client.get_backlog_task, agentSpaceId=space_id,
                                        taskId=matches[0]["taskId"])).get("task") or {}
        if task.get("taskId") != matches[0]["taskId"]:
            return AdapterOutcome("unknown")
        return self._outcome(run, space_id, task, description)

    async def _task(self, run, native, session, attempt):
        space_id, client, _ = self._scope(run, session)
        if (native.get("tenant_id") != run.tenant_id or native.get("project_id") != run.project_id
                or native.get("source_connection_id") != run.admission["connection_ref"]["source_connection_id"]
                or native.get("record_type") != "native_job" or not native.get("record_id")):
            raise ValueError("native execution differs from pinned scope")
        token = None
        seen = set()
        matches = []
        while True:
            page = await asyncio.to_thread(client.list_backlog_tasks, agentSpaceId=space_id, limit=100,
                                           **({"nextToken": token} if token else {}))
            if not isinstance(page.get("tasks"), list):
                raise ValueError("native task readback unavailable")
            matches.extend(item for item in page["tasks"] if item.get("executionId") == native["record_id"])
            token = page.get("nextToken")
            if not token:
                break
            if token in seen or len(seen) >= 100:
                raise ValueError("native task readback unbounded")
            seen.add(token)
        if len(matches) != 1 or not matches[0].get("taskId"):
            raise ValueError("native task readback ambiguous")
        task = (await asyncio.to_thread(client.get_backlog_task, agentSpaceId=space_id,
                                        taskId=matches[0]["taskId"])).get("task") or {}
        if (task.get("agentSpaceId") != space_id or task.get("taskType") != "RELEASE_TESTING"
                or task.get("taskId") != matches[0]["taskId"]
                or task.get("executionId") != native["record_id"]
                or task.get("description") != self._description(run, attempt)):
            raise ValueError("native task differs from pinned verification")
        return task

    async def observe(self, run, native_job_ref, attempt, provider_session):
        task = await self._task(run, native_job_ref, provider_session, attempt)
        outcome = {"IN_PROGRESS": "running", "CREATED": "running", "COMPLETED": "completed",
                   "FAILED": "failed", "TIMED_OUT": "failed", "CANCELED": "stopped"}.get(
                       task.get("status"), "unknown")
        observation = {"native_job_ref": native_job_ref, "outcome": outcome,
                       "native_status": task.get("status", "unknown"),
                       "observed_at": self.clock().isoformat().replace("+00:00", "Z")}
        return AdapterOutcome(outcome, observation=observation)

    async def collect(self, run, native_job_ref, attempt, provider_session):
        task = await self._task(run, native_job_ref, provider_session, attempt)
        if task.get("status") not in {"COMPLETED", "FAILED", "TIMED_OUT"}:
            return AdapterOutcome("unknown")
        native_failed = task["status"] != "COMPLETED" or run.provider_state == "failed"
        cleanup = await self._cleanup(run, native_job_ref) if native_failed else None
        if self.native_results_reader is None and not native_failed:
            raise ValueError("qualified native scenario-results reader required")
        space_id = run.admission["native_space_ref"]["record_id"]
        native_receipt = None
        if self.native_results_reader is not None:
            native_receipt = await self.native_results_reader.read_native_results(
                space_id, task["taskId"], native_job_ref["record_id"])
        if native_receipt is None and native_failed:
            native_results = []
        elif (not isinstance(native_receipt, dict) or native_receipt.get("task_id") != task["taskId"]
                or native_receipt.get("execution_id") != native_job_ref["record_id"]
                or native_receipt.get("source_ref") != run.input["devops_input"]["source_ref"]
                or native_receipt.get("deployment_ref") != run.input["devops_input"]["deployment_ref"]):
            raise ValueError("native scenario receipt does not match pinned execution")
        else:
            native_results = native_receipt.get("scenario_results")
        independent = await self.reader.read_results(run.input["devops_input"]["deployment_ref"],
                                                     native_job_ref["record_id"])
        if independent is None and native_failed:
            independent = []
        expected = {scenario["artifact_id"] for scenario in run.manifest["scenarios"]}
        if (not isinstance(native_results, list) or not isinstance(independent, list)
                or not native_failed and (len(native_results) != len(expected) or len(independent) != len(expected))):
            raise ValueError("individual scenario outcomes unavailable")
        native_by_id = {row.get("scenario_id"): row for row in native_results if isinstance(row, dict)}
        independent_by_id = {row.get("scenario_id"): row for row in independent if isinstance(row, dict)}
        if (not set(native_by_id) <= expected or not set(independent_by_id) <= expected
                or len(native_by_id) != len(native_results) or len(independent_by_id) != len(independent)):
            raise ValueError("scenario readback differs from pinned specification")
        specifications = {spec["artifact"]["artifact_id"]: spec
                          for spec in run.admission["verification"]["scenario_specs"]}
        for native in native_results:
            if (native.get("status") not in {"passed", "failed"}
                    or not isinstance(native.get("evidence_ref"), dict)
                    or not native["evidence_ref"].get("sha256")):
                raise ValueError("native scenario outcome unavailable")
        for scenario_id, oracle in independent_by_id.items():
            spec = specifications[scenario_id]
            positive = oracle.get("positive")
            negative = oracle.get("negative")
            if (not isinstance(positive, dict) or not isinstance(negative, dict)
                    or any(not isinstance(check.get("evidence_ref"), dict)
                           or not check["evidence_ref"].get("sha256")
                           or not isinstance(check.get("observed_outcome"), str)
                           or not 1 <= len(check["observed_outcome"]) <= 128
                           or check.get("expected_outcome") != spec[key]
                           for check, key in ((positive, "positive_expected"),
                                              (negative, "negative_expected")))
                    or oracle.get("scenario_id") != scenario_id
                    or oracle.get("deployment_ref") != run.admission["verification"]["deployment_ref"]
                    or oracle.get("deployed_sha") != run.admission["verification"]["deployed_sha"]
                    or not isinstance(oracle.get("evidence_ref"), dict)
                    or not oracle["evidence_ref"].get("sha256")):
                raise ValueError("independent scenario observation differs from native result")
            independently_passed = (positive["observed_outcome"] == spec["positive_expected"]
                                    and negative["observed_outcome"] == spec["negative_expected"])
            if oracle.get("status") != ("passed" if independently_passed else "failed") or (
                    scenario_id in native_by_id and oracle["status"] != native_by_id[scenario_id]["status"]):
                raise ValueError("independent scenario outcome disagrees with provider")
        if cleanup is None:
            cleanup = await self._cleanup(run, native_job_ref)
        failed = native_failed or any(row["status"] == "failed" for row in native_results)
        coverage = ("complete" if set(native_by_id) == set(independent_by_id) == expected else
                    "partial" if native_results or independent else "unknown")
        result = {"native_job_ref": native_job_ref, "findings": [], "finding_count": 0,
                  "coverage": coverage, "coverage_mode": "deployment_verification",
                  "provider_outcome": "failed" if failed else "completed", "scenarios": native_results,
                  "deployment_ref": run.input["devops_input"]["deployment_ref"],
                  "native_task_id": task["taskId"],
                  "source_ref": run.input["devops_input"]["source_ref"],
                  "deployed_sha": run.admission["verification"]["deployed_sha"],
                  "independent_observations": independent, "cleanup": cleanup,
                  "costs": [{"kind": "unknown", "currency": "USD"}]}
        return AdapterOutcome("completed", result=result)

    async def _cleanup(self, run, native_job_ref):
        options = run.input["devops_input"]
        observations = []
        seen = set()
        for spec in run.admission["verification"]["scenario_specs"]:
            reference = spec["cleanup_ref"]
            identifier = (reference["source_connection_id"], reference["record_id"])
            if identifier in seen:
                raise ValueError("duplicate owned fixture cleanup reference")
            seen.add(identifier)
            token = sha256(f"{run.tenant_id}:{run.run_id}:{reference['record_id']}:cleanup".encode()).hexdigest()
            before = await self.reader.read_fixture_data(reference)
            if before == {"fixture_ref": reference, "state": "present", "owner_run_id": run.run_id,
                          "execution_id": native_job_ref["record_id"]}:
                await self.reader.delete_fixture_data(reference, owner_run_id=run.run_id,
                                                      idempotency_key=token)
            elif (not isinstance(before, dict) or before.get("fixture_ref") != reference or before.get("state") != "absent"
                  or before.get("owner_run_id") != run.run_id
                  or before.get("deletion_token") != token):
                raise ValueError("fixture cleanup ownership unverified")
            after = await self.reader.read_fixture_data(reference)
            if (not isinstance(after, dict) or after.get("fixture_ref") != reference or after.get("state") != "absent"
                    or after.get("owner_run_id") != run.run_id
                    or after.get("deletion_token") != token):
                raise ValueError("owned fixture deletion not independently verified")
            observations.append(after)
        deployment = await self.reader.read_deployment(options["deployment_ref"], options["target_ref"])
        pinned = run.admission["verification"]
        if not isinstance(deployment, dict) or any(deployment.get(key) != pinned[key] for key in (
                "source_ref", "deployment_ref", "target_ref", "credential_ref", "deployed_sha",
                "isolated", "admin", "network_scope", "deployment_adapter")):
            raise ValueError("deployment changed during fixture cleanup")
        return observations

    async def cancel(self, run, native_job_ref, attempt, provider_session):
        return AdapterOutcome("unknown")
