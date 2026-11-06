"""Owner-bound concrete queue fixture producer/oracle; no native success inference.

This factory is deliberately not mounted as a public action or installed in the
DevOps runtime. Installation needs the exact fixture policy and a qualified
native asset reader. All operations below use real AWS/GitHub APIs when invoked.
"""

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha1, sha256

from botocore.config import Config
from fastapi import HTTPException
from l5_services.api.configuration import view
from l5_services.devops.verification_targets import verify_observation
from l5_services.models.run import Run
from starlette.concurrency import run_in_threadpool

from src.auth.l5_execution_scope import current_execution_scope
from src.l5_backend_devops import DevopsService
from src.l5_devops_queue_evidence import QueueEvidence
from src.l5_devops_queue_observer import check_selection, observe_queue
from src.l5_devops_reads import github_source
from src.l5_devops_verification import canonical
from src.l5_service_identity import worker_id
from src.shared.identity.product_scope import current_l5_parent
from src.shared.models.vault import CredentialServiceDelegation

SCENARIO = {
    "schema_version": 1,
    "kind": "queue-batch-schema-v1",
    "positive": {"Records": [{"messageId": "valid", "body": '{"invoice_id":"fixture-one","amount_cents":100}'}]},
    "negative": {
        "Records": [
            {"messageId": "valid", "body": '{"invoice_id":"fixture-two","amount_cents":0}'},
            {"messageId": "invalid-json", "body": "{"},
            {"messageId": "invalid-amount", "body": '{"invoice_id":"fixture-three","amount_cents":-1}'},
            {"messageId": "invalid-field", "body": '{"invoice_id":"fixture-four","amount_cents":1,"extra":true}'},
        ]
    },
}
EXPECTED = {
    "positive": {"batchItemFailures": []},
    "negative": {
        "batchItemFailures": [{"itemIdentifier": "invalid-json"}, {"itemIdentifier": "invalid-amount"}, {"itemIdentifier": "invalid-field"}]
    },
}
OUTCOMES = {"positive": "all_accepted", "negative": "only_invalid_rejected"}
SCENARIO_ARTIFACT = {
    "artifact_id": "queue-batch-schema-v1",
    "version": "1",
    "sha256": sha256(canonical(SCENARIO)).hexdigest(),
    "media_type": "application/vnd.adp.release-scenario+json",
    "bytes": len(canonical(SCENARIO)),
}


class QueueFixture:
    def __init__(self, backend, product_id, project_id, *, session, run_id=None, validate_authority=None):
        self.backend, self.product_id, self.project_id, self.run_id = backend, product_id, project_id, run_id
        raw = os.environ.get("L5_DEVOPS_QUEUE_FIXTURE")
        if not raw:
            raise ValueError("install an exact queue fixture selection before producing evidence")
        self.selection = json.loads(raw)
        check_selection(self.selection)
        if session is None:
            raise ValueError("a scoped owner-provider session is required for fixture evidence")
        self.session = session
        self.validate_authority = validate_authority
        self.evidence = QueueEvidence(self.session, self.selection)
        self.options = {key: self.selection[key] for key in ("source_ref", "deployment_ref", "target_ref", "credential_ref")}
        self.options.update(kind="release_verification", scenarios=[SCENARIO_ARTIFACT])

    async def current(self, *, execution=False):
        if self.validate_authority is not None:
            await self.validate_authority()
        if json.loads(os.environ.get("L5_DEVOPS_QUEUE_FIXTURE", "null")) != self.selection:
            raise ValueError("installed fixture selection changed; rebuild the owner-bound reader")
        configuration, version = await DevopsService(self.backend).current(self.product_id, self.project_id)
        scope = view(configuration, version)["scope"]
        if any(
            (ref["tenant_id"], ref["project_id"]) != (scope["tenant_id"], scope["project_id"])
            for key, ref in self.options.items()
            if key.endswith("_ref")
        ) or self.options["source_ref"] not in version.settings.get("source_refs", []):
            raise HTTPException(404, detail={"code": "not_found"})
        run = None
        if self.run_id is not None:
            run = await self.backend.db.get(Run, (scope["tenant_id"], self.run_id), populate_existing=True)
            if (
                run is None
                or run.service_id != "devops"
                or run.product_id != self.product_id
                or run.project_id != self.project_id
                or run.origin != "adp_submitted"
                or run.configuration_id != configuration.configuration_id
                or run.configuration_revision != version.revision
                or run.input.get("devops_input") != self.options
                or run.input.get("capability") != "release_verification"
                or not run.admission
                or not run.manifest
                or run.admission.get("scope") != scope
                or run.admission.get("configuration_hash") != version.hash
                or run.manifest.get("verification") != run.admission.get("verification")
                or run.manifest.get("run_id") != run.run_id
                or run.manifest.get("scope") != scope
                or run.manifest.get("scenarios") != self.options["scenarios"]
                or (run.admission.get("verification") or {}).get("scenario_specs") != self.specs()
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            binding = await DevopsService(self.backend).binding(configuration, version, run.binding_id)
            grant = await self.backend.db.get(CredentialServiceDelegation, run.admission.get("delegation_id"), populate_existing=True)
            if (
                binding.generation != run.binding_generation
                or run.admission.get("binding_generation") != binding.generation
                or run.admission.get("native_space_ref") != binding.native_resource_ref
                or run.admission.get("provider_identity") != binding.provider_identity
                or grant is None
                or grant.revision != run.admission.get("delegation_version")
                or grant.revoked_at is not None
                or grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC) <= datetime.now(UTC)
                or (grant.tenant_id, grant.product_id, grant.project_id, grant.created_by, grant.service_id)
                != (run.tenant_id, run.product_id, run.project_id, self.backend.actor.user_id, worker_id("devops"))
                or not isinstance(grant.execution_scope, dict)
                or grant.execution_scope.get("configuration_id") != run.configuration_id
                or "release_verification" not in grant.capabilities
                or not await current_execution_scope(
                    self.backend.db, grant, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation
                )
                or not await current_l5_parent(self.backend.db, grant)
            ):
                raise HTTPException(404, detail={"code": "not_found"})
        if execution and (
            run is None
            or not run.native_job_ref
            or run.native_job_ref.get("record_type") != "native_job"
            or (run.native_job_ref.get("tenant_id"), run.native_job_ref.get("project_id")) != (scope["tenant_id"], scope["project_id"])
            or not run.native_job_ref.get("record_id")
            or run.native_job_ref.get("source_connection_id") != binding.connection_ref["source_connection_id"]
            or run.native_job_ref.get("record_revision") != run.native_job_ref.get("record_id")
        ):
            raise ValueError("an actual persisted native execution is required for the independent oracle")
        return configuration, version, scope, run

    def specs(self):
        cleanup = {
            **self.selection["target_ref"],
            "record_type": "fixture_data",
            "record_id": "queue-batch-schema-v1",
            "record_revision": SCENARIO_ARTIFACT["sha256"],
        }
        return [
            {
                "artifact": SCENARIO_ARTIFACT,
                "max_requests": 2,
                "timeout_seconds": 90,
                "cleanup_ref": cleanup,
                "network_scope": self.selection["target_ref"]["record_id"],
                "positive_expected": OUTCOMES["positive"],
                "negative_expected": OUTCOMES["negative"],
            }
        ]

    async def read_deployment(self, deployment_ref, target_ref):
        if deployment_ref != self.options["deployment_ref"] or target_ref != self.options["target_ref"]:
            raise ValueError("deployment selection differs from installed fixture")
        configuration, version, scope, run = await self.current()
        expected = configuration.configuration_id, version.revision, version.hash
        async with github_source(self.backend, self.project_id, self.options["source_ref"]) as (_, read, _):
            response = await read("contents/index.mjs?ref=" + self.options["source_ref"]["record_revision"])
            if response.get("type") != "file" or response.get("encoding") != "base64" or response.get("path") != "index.mjs":
                raise ValueError("selected immutable worker source unavailable")
            source = base64.b64decode(response["content"].replace("\n", ""), validate=True)
            if (
                response.get("size") != len(source)
                or not 1 <= len(source) <= 32768
                or sha1(b"blob " + str(len(source)).encode() + b"\0" + source, usedforsecurity=False).hexdigest() != response.get("sha")
            ):
                raise ValueError("GitHub source blob integrity changed")
            observed = await run_in_threadpool(observe_queue, self.session, self.selection, source)
        latest, current, _, _ = await self.current()
        if expected != (latest.configuration_id, current.revision, current.hash):
            raise ValueError("configuration changed during deployment observation")
        now = datetime.now(UTC)
        expires = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC) if run else now + timedelta(hours=2)
        if not now < expires <= now + timedelta(days=1):
            raise ValueError("fixture observation requires a current bounded deadline")
        self.observed = observed
        return {
            **{key: self.options[key] for key in ("source_ref", "deployment_ref", "target_ref", "credential_ref")},
            "deployed_sha": self.options["source_ref"]["record_revision"],
            "isolated": True,
            "admin": False,
            "network_scope": self.options["target_ref"]["record_id"],
            "deployment_adapter": "aws-lambda-queue-v1",
            "checked_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }

    async def read_scenarios(self, artifacts):
        await self.current()
        if artifacts != [SCENARIO_ARTIFACT]:
            raise ValueError("only the bounded installed queue schema scenario is qualified")
        return self.specs()

    async def publish_admission(self):
        if self.run_id is not None or not self.backend.admission_key or self.backend.admission_operation not in {"submit", "retry"}:
            raise ValueError("producer requires the exact upcoming owner admission")
        observation = await self.read_deployment(self.options["deployment_ref"], self.options["target_ref"])
        configuration, version, scope, _ = await self.current()
        observation["scenario_specs"] = self.specs()
        now = datetime.now(UTC)
        verify_observation(self.options, scope, observation, now=now, deadline=now + timedelta(minutes=5))
        payload = {
            "schema_version": 1,
            "producer": self.selection["producer"],
            "scope": {key: scope[key] for key in ("tenant_id", "product_id", "project_id")},
            "configuration": {"id": configuration.configuration_id, "revision": version.revision, "hash": version.hash},
            "admission": {"operation": self.backend.admission_operation, "idempotency_key": self.backend.admission_key},
            "issued_at": observation["checked_at"],
            "expires_at": observation["expires_at"],
            "observation": observation,
        }
        # Preserve actual provider/source observations separately from the strict
        # admission envelope so they remain reviewable without expanding its schema.
        await run_in_threadpool(self.evidence.create, "observations/" + sha256(canonical(payload)).hexdigest(), self.observed)
        await self.current()
        reference = await run_in_threadpool(self.evidence.publish, payload)
        await self.current()
        return reference

    async def identity(self, reference=None):
        _, _, _, run = await self.current(execution=True)
        expected_ref = self.specs()[0]["cleanup_ref"]
        if reference is not None and reference != expected_ref:
            raise ValueError("fixture-data selection is outside the pinned scenario")
        execution_id = run.native_job_ref["record_id"]
        owner = {"owner_run_id": run.run_id, "execution_id": execution_id, "fixture_ref": expected_ref}
        name = "runs/" + sha256(canonical({"tenant_id": run.tenant_id, **owner})).hexdigest()
        token = sha256(f"{run.tenant_id}:{run.run_id}:{expected_ref['record_id']}:cleanup".encode()).hexdigest()
        return run, owner, name, token

    async def run_oracle(self):
        run, owner, name, token = await self.identity()
        self._allow_invocation(run)
        observed = await self.read_deployment(self.options["deployment_ref"], self.options["target_ref"])
        pinned = run.admission["verification"]
        if any(observed.get(key) != pinned.get(key) for key in observed if key not in {"checked_at", "expires_at"}):
            raise ValueError("oracle deployment differs from the actual run admission")
        manifest_row = await run_in_threadpool(self.evidence.read, name + "/owned")
        payload = {**owner, "requests": {key: SCENARIO[key] for key in ("positive", "negative")}}
        if manifest_row is None:
            _, payload_ref = await run_in_threadpool(self.evidence.create, name + "/payload", payload)
            manifest = {**owner, "payload": payload, "payload_ref": payload_ref, "deletion_token": token}
            await run_in_threadpool(self.evidence.create, name + "/owned", manifest)
        elif any(manifest_row[0].get(key) != value for key, value in owner.items()):
            raise ValueError("owned fixture manifest belongs to another run")
        else:
            manifest = manifest_row[0]
        checks = {}
        client = self.session.client(
            "lambda", region_name=self.selection["region"], config=Config(connect_timeout=3, read_timeout=35, retries={"max_attempts": 0})
        )
        for kind in ("positive", "negative"):
            current, _, _, _ = await self.identity()
            saved = await run_in_threadpool(self.evidence.read, name + "/" + kind + "/result")
            if saved is not None:
                checks[kind] = saved[0]
                continue
            retained = await run_in_threadpool(self.evidence.read, name + "/" + kind + "/response")
            if retained is None:
                self._allow_invocation(current)
                latest = await self.read_deployment(self.options["deployment_ref"], self.options["target_ref"])
                if any(latest.get(key) != pinned.get(key) for key in latest if key not in {"checked_at", "expires_at"}):
                    raise ValueError("fixture changed before bounded invocation")
                stored_payload = await run_in_threadpool(self.evidence.read_payload, name, manifest)
                if stored_payload is None:
                    raise ValueError("fixture data was cleaned; this run cannot start another invocation")
                request = {**owner, "function_version_arn": self.selection["function_version_arn"], "input": stored_payload["requests"][kind]}
                first, _ = await run_in_threadpool(self.evidence.create, name + "/" + kind + "/started", request)
                if not first:
                    raise ValueError("queue_oracle_outcome_unknown: inspect retained invocation evidence; do not reinvoke this run")
                current, _, _, _ = await self.identity()
                self._allow_invocation(current)
                response = await run_in_threadpool(
                    client.invoke,
                    FunctionName=self.selection["function_version_arn"],
                    InvocationType="RequestResponse",
                    LogType="None",
                    Payload=canonical(request["input"]),
                )
                try:
                    raw = await run_in_threadpool(response["Payload"].read, 32769)
                finally:
                    response["Payload"].close()
                if len(raw) > 32768:
                    raise ValueError("oracle response exceeded its bound")
                actual = {
                    "status_code": response.get("StatusCode"),
                    "executed_version": response.get("ExecutedVersion"),
                    "function_error": response.get("FunctionError"),
                    "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
                    "payload_base64": base64.b64encode(raw).decode(),
                    "function_version_arn": self.selection["function_version_arn"],
                    **owner,
                }
                _, evidence = await run_in_threadpool(self.evidence.create, name + "/" + kind + "/response", actual)
            else:
                actual, evidence = retained
                if any(actual.get(key) != value for key, value in owner.items()):
                    raise ValueError("retained invocation response belongs to another run")
                raw = base64.b64decode(actual["payload_base64"], validate=True)
            try:
                decoded = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                decoded = None
            passed = (
                actual["status_code"] == 200
                and not actual["function_error"]
                and actual["executed_version"] == self.selection["function_version_arn"].rsplit(":", 1)[1]
                and bool(actual["request_id"])
                and decoded == EXPECTED[kind]
            )
            check = {
                "expected_outcome": OUTCOMES[kind],
                "observed_outcome": OUTCOMES[kind] if passed else "unexpected_result",
                "evidence_ref": evidence,
            }
            await run_in_threadpool(self.evidence.create, name + "/" + kind + "/result", check)
            checks[kind] = check
        row = {
            "scenario_id": SCENARIO_ARTIFACT["artifact_id"],
            "deployment_ref": self.options["deployment_ref"],
            "deployed_sha": self.options["source_ref"]["record_revision"],
            **checks,
            "status": "passed" if all(item["observed_outcome"] == item["expected_outcome"] for item in checks.values()) else "failed",
        }
        _, reference = await run_in_threadpool(self.evidence.create, name + "/result", row)
        await self.identity()
        return [{**row, "evidence_ref": reference}]

    @staticmethod
    def _allow_invocation(run):
        if (
            run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC) <= datetime.now(UTC) + timedelta(seconds=90)
            or run.cancel_reason
            or run.state in {"cancel_requested", "cancelled", "expired", "blocked", "failed", "succeeded"}
        ):
            raise ValueError("the persisted run no longer admits bounded fixture invocation")

    async def read_results(self, deployment_ref, execution_id):
        _, owner, name, _ = await self.identity()
        if deployment_ref != self.options["deployment_ref"] or execution_id != owner["execution_id"]:
            raise ValueError("oracle result differs from the persisted native execution")
        result = await run_in_threadpool(self.evidence.read, name + "/result")
        return None if result is None else [{**result[0], "evidence_ref": result[1]}]

    async def read_fixture_data(self, reference):
        _, owner, name, token = await self.identity(reference)
        stored = await run_in_threadpool(self.evidence.read, name + "/owned")
        if stored is None or any(stored[0].get(key) != value for key, value in owner.items()) or stored[0].get("deletion_token") != token:
            raise ValueError("owned fixture-data receipt unavailable")
        present = await run_in_threadpool(self.evidence.payload_present, name, stored[0])
        if present:
            return {**owner, "state": "present"}
        return {"fixture_ref": reference, "state": "absent", "owner_run_id": owner["owner_run_id"], "deletion_token": token}

    async def delete_fixture_data(self, reference, *, owner_run_id, idempotency_key):
        _, owner, name, token = await self.identity(reference)
        if owner_run_id != owner["owner_run_id"] or idempotency_key != token:
            raise ValueError("fixture deletion differs from its run-bound cleanup token")
        stored = await run_in_threadpool(self.evidence.read, name + "/owned")
        if stored is None or any(stored[0].get(key) != value for key, value in owner.items()) or stored[0].get("deletion_token") != token:
            raise ValueError("owned fixture-data receipt unavailable")
        await self.identity(reference)
        await run_in_threadpool(self.evidence.delete_owned, name, stored[0], token)
        await self.read_deployment(self.options["deployment_ref"], self.options["target_ref"])


def build_queue_fixture(backend, product_id, project_id, *, session, run_id=None, validate_authority=None):
    return QueueFixture(backend, product_id, project_id, session=session, run_id=run_id, validate_authority=validate_authority)
