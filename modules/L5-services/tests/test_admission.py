"""Independent SQL readback of admission commits and failed commits."""

import unittest
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import HTTPException
from jsonschema import Draft202012Validator, FormatChecker
from l5_services.admission import admit_run, persist_admission, submit_and_dispatch
from l5_services.adapters.transform.authority import TransformWorkAuthorizer
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run, view_run
from l5_services.models.work import Work
from l5_services.models.preparation import AdmissionPreparation
from l5_services.work_store import claim_work, finish_work
from l5_services.workers.execution import AuthorityRevoked
from l5_services.product_client import ProductClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class AtomicAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enforce_foreign_keys(connection, connection_record):
            connection.execute("PRAGMA foreign_keys=ON")

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(Configuration(
                tenant_id="tenant-a", configuration_id="configuration-a", product_id="product-a",
                project_id="project-a", service_id="security", relationship_revision="1", owner_id="owner-a",
                current_revision=1, handoff_state="linked",
            ))
            session.add(ConfigurationVersion(
                tenant_id="tenant-a", configuration_id="configuration-a", revision=1, settings={},
                inherited_fields=[], gaps=[], state="ready", hash="f" * 64,
            ))
            await session.commit()
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def admission(self, key="key-1234567890123456"):
        run_id = str(uuid4())
        inputs = {"configuration_revision": 1, "binding_id": "binding-a", "capability": "threat_model",
                  "sources": [{"record_revision": "document-1"}]}
        snapshot = {"source_snapshots": [{"source_ref": inputs["sources"][0],
                                         "snapshot_id": "captured-a", "expires_at": "2026-01-02T00:00:00Z"}],
                    "binding_generation": 3, "delegation_id": "delegation-a",
                    "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300}}
        run = Run(tenant_id="tenant-a", run_id=run_id, product_id="product-a", project_id="project-a",
                  service_id="security", configuration_id="configuration-a", configuration_revision=1,
                  binding_id="binding-a", trigger_id="manual-a", binding_generation=3,
                  input=inputs, admission=snapshot,
                  deadline_at=self.now + timedelta(hours=1))
        receipt = RunReceipt(tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                             service_id="security", actor_id="owner-a", operation="submit", idempotency_key=key,
                             request_hash="a" * 64, run_id=run_id, expires_at=self.now + timedelta(days=1))
        work = Work(tenant_id="tenant-a", work_id=str(uuid4()), run_id=run_id, kind="execute",
                    generation=1, step=0, due_at=self.now)
        return run, receipt, work

    async def counts(self):
        async with self.sessions() as session:
            return [await session.scalar(select(func.count()).select_from(model))
                    for model in (Run, RunReceipt, Work)]

    async def test_run_receipt_and_work_commit_together(self):
        run, receipt, work = self.admission()
        async with self.sessions() as session:
            await persist_admission(session, run, receipt, work)
        async with self.sessions() as session:
            stored = await session.get(Run, ("tenant-a", run.run_id))
            owed = await session.get(Work, ("tenant-a", work.work_id))
            self.assertEqual(stored.admission["source_snapshots"], run.admission["source_snapshots"])
            self.assertEqual(stored.input, run.input)
            self.assertEqual((stored.binding_generation, stored.state, owed.run_id), (3, "admitted", run.run_id))
            self.assertEqual((await session.get(RunReceipt, (
                "tenant-a", "product-a", "project-a", "security", "owner-a", "submit", receipt.idempotency_key,
            ))).run_id, run.run_id)
        self.assertEqual(await self.counts(), [1, 1, 1])

    async def test_receipt_conflict_rolls_back_run_and_work(self):
        async with self.sessions() as session:
            await persist_admission(session, *self.admission())
        async with self.sessions() as session:
            with self.assertRaises(IntegrityError):
                await persist_admission(session, *self.admission())
        self.assertEqual(await self.counts(), [1, 1, 1])

    async def test_mismatched_work_cannot_persist(self):
        run, receipt, work = self.admission()
        work.tenant_id = "tenant-b"
        async with self.sessions() as session:
            with self.assertRaises(ValueError):
                await persist_admission(session, run, receipt, work)
        self.assertEqual(await self.counts(), [0, 0, 0])

    def submission(self):
        source = {"tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "connection-a",
                  "record_type": "document", "record_id": "source-a", "record_revision": "revision-1"}
        run_input = {"configuration_revision": 1, "binding_id": "binding-a", "capability": "threat_model",
                     "sources": [source]}
        verified = {
            "scope": {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
                      "relationship_revision": "1", "owner_id": "owner-a", "visibility": "only_me"},
            "service_id": "security", "actor_id": "owner-a", "actor_type": "human",
            "configuration_id": "configuration-a",
            "configuration_revision": 1, "binding_id": "binding-a", "binding_generation": 3,
            "delegation_id": "delegation-a", "delegation_version": 2,
            "configuration_enabled": True, "binding_state": "ready", "delegation_active": True,
            "profile_ref": {"tenant_id": "tenant-a", "project_id": "project-a",
                            "source_connection_id": "adp.profile", "record_type": "product",
                            "record_id": "product-a", "record_revision": "1"},
            "connection_ref": {"tenant_id": "tenant-a", "project_id": "project-a",
                               "source_connection_id": "connection-a", "record_type": "connection",
                               "record_id": "connection-a", "record_revision": "1"},
            "binding_connection_ref": {"tenant_id": "tenant-a", "project_id": "project-a",
                                       "source_connection_id": "connection-a", "record_type": "connection",
                                       "record_id": "connection-a", "record_revision": "1"},
            "trigger": {"trigger_id": "manual-a", "kind": "manual", "enabled": True,
                        "capability": "threat_model", "binding_id": "binding-a", "source_refs": [source]},
            "native_space_ref": {"tenant_id": "tenant-a", "project_id": "project-a",
                                 "source_connection_id": "connection-a", "record_type": "security_space",
                                 "record_id": "space-a", "record_revision": "2026-01-01T00:00:00+00:00"},
            "native_model_ref": {"tenant_id": "tenant-a", "project_id": "project-a",
                                 "source_connection_id": "connection-a", "record_type": "threat_model",
                                 "record_id": "model-a", "record_revision": "2026-01-01T00:00:00+00:00"},
            "provider_identity": {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                                  "region": "us-east-1"},
            "source_registration": {"service_role": "arn:aws:iam::123456789012:role/SourceReader",
                                    "bucket": "example-source-staging"},
            "configuration_hash": "f" * 64, "adapter_revision": "adapter-v1",
            "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 20,
                       "concurrency": 1, "max_cost": "1.00", "currency": "USD",
                       "policy_ref": {"tenant_id": "tenant-a", "project_id": "project-a",
                                      "source_connection_id": "policy", "record_type": "policy",
                                      "record_id": "run-budget", "record_revision": "1"}},
            "visibility": "only_me", "classification": "internal", "causal_origin": "manual",
            "max_bytes": 1048576, "max_files": 100,
            "exclusions": ["generated"], "monitoring_policy": {"poll_interval_seconds": 600,
                                                           "poll_grace_seconds": 300},
        }
        return run_input, verified

    async def test_replay_only_never_captures_or_republishes_existing_work(self):
        from hashlib import sha256
        from unittest.mock import AsyncMock

        inputs, verified = self.submission()
        run, receipt, work = self.admission()
        run.input = inputs
        receipt.request_hash = sha256(json.dumps({"input": inputs, "event_id": None, "exclusions": verified["exclusions"]},
                                               sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.sessions() as session:
            await persist_admission(session, run, receipt, work)
        source, publish = AsyncMock(), AsyncMock()
        async with self.sessions() as session:
            returned = await submit_and_dispatch(session, self.sessions, source, publish, inputs, verified,
                                                 receipt.idempotency_key, now=self.now, replay_only=True)
        self.assertEqual(returned.run_id, run.run_id)
        source.pin_source.assert_not_awaited()
        publish.assert_not_awaited()
        self.assertEqual(await self.counts(), [1, 1, 1])

    async def test_replay_only_missing_receipt_cannot_create_native_work(self):
        from unittest.mock import AsyncMock

        inputs, verified = self.submission()
        source, publish = AsyncMock(), AsyncMock()
        async with self.sessions() as session:
            with self.assertRaises(HTTPException) as missing:
                await submit_and_dispatch(session, self.sessions, source, publish, inputs, verified,
                                          "missing-key-00001", now=self.now, replay_only=True)
        self.assertEqual(missing.exception.detail["code"], "l5_effect_existing_replay_required")
        source.pin_source.assert_not_awaited()
        publish.assert_not_awaited()
        self.assertEqual(await self.counts(), [0, 0, 0])

    async def test_finops_admitted_runs_match_mcp_output_schemas(self):
        catalogue = Path(__file__).resolve().parents[3] / "docs/l5-services/contracts/v1/mcp-tools.json"
        tools = {tool["x-http-operation"]: tool for tool in json.loads(catalogue.read_text())}
        inputs, verified = self.submission()
        source = inputs.pop("sources")[0]
        inputs.update(service_id="finops", capability="cost_inquiry", question="What was spent in December?",
                      query={"start": "2025-12-01", "end": "2026-01-01", "metric": "UnblendedCost",
                             "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD"})
        for field in ("native_space_ref", "native_model_ref", "provider_identity", "source_registration"):
            verified.pop(field)
        verified.update(service_id="finops", native_qualified=True,
                        provider_identity={"account_id": "000000000000",
                                           "role_arn": "arn:aws:iam::000000000000:role/finops-example",
                                           "region": "us-east-1"},
                        native_agent_ref={"tenant_id": "tenant-a", "project_id": "project-a",
                                          "record_type": "finops_agent", "record_id": "agent-example"},
                        billing_scope={"scope_generation": 3})
        verified["trigger"]["capability"] = "cost_inquiry"
        async with self.sessions() as session:
            configuration = await session.get(Configuration, ("tenant-a", "configuration-a"))
            configuration.service_id = "finops"
            await session.commit()

        class Capturer:
            async def pin_source(self, product_id, project_id, request, *, idempotency_key):
                artifact = {"artifact_id": "snapshot-example", "version": "1", "sha256": "a" * 64,
                            "media_type": "application/json", "bytes": 10}
                return {"snapshot_id": "snapshot-example", "source_ref": request["source_ref"],
                        "content": artifact, "file_manifest": artifact, "file_count": 1,
                        "exclusions": request["exclusions"], "expires_at": request["retain_until"]}

        async def budget_guard(session, limits, scope, now, replay):
            return await replay()

        for with_context in (False, True):
            if with_context:
                inputs["sources"] = [source]
                verified["native_agent_ref"].update(source_connection_id="connection-a", record_revision="1")
                verified["billing_scope"].update(
                    account_ids=["000000000000"], view_ref="view-example", filter_digest="a" * 64,
                    role_policy_digest="b" * 64, scope_digest="c" * 64, observed_at="2026-01-01T00:00:00Z",
                    snapshot_artifact={"artifact_id": "billing-example", "version": "1", "sha256": "d" * 64})
            async with self.sessions() as session:
                admitted = await admit_run(session, Capturer(), inputs, verified,
                                           f"finops-key-example-{with_context}", now=self.now,
                                           budget_guard=budget_guard)
                accepted = view_run(admitted)
            async with self.sessions() as session:
                stored = view_run(await session.get(Run, ("tenant-a", admitted.run_id)))
                listed = [view_run(item) for item in (await session.scalars(
                    select(Run).where(Run.service_id == "finops"))).all()]
            self.assertEqual(accepted, stored)
            self.assertEqual(len(stored["admission"]["source_snapshots"]), int(with_context))
            for operation, status, data in (("submitRun", 202, accepted), ("getRun", 200, stored),
                                            ("listRuns", 200, {"items": listed})):
                validator = Draft202012Validator(tools[operation]["outputSchema"], format_checker=FormatChecker())
                envelope = {"data": data, "http_status": status}
                validator.validate(envelope)
                if operation == "listRuns":
                    continue
                for field in ("native_agent_ref", "billing_scope"):
                    invalid = deepcopy(envelope)
                    del invalid["data"]["admission"][field]
                    self.assertFalse(validator.is_valid(invalid), field)
                invalid = deepcopy(envelope)
                invalid["data"]["admission"]["billing_scope"]["provider_session"] = "forbidden"
                self.assertFalse(validator.is_valid(invalid))
                invalid = deepcopy(envelope)
                invalid["data"]["admission"]["native_agent_ref"]["record_type"] = "devops_space"
                self.assertFalse(validator.is_valid(invalid))
                invalid = deepcopy(envelope)
                invalid["data"]["input"] = self.submission()[0]
                self.assertFalse(validator.is_valid(invalid))
                legacy = deepcopy(envelope)
                legacy["data"]["input"] = self.submission()[0]
                legacy["data"]["admission"].pop("service_id")
                legacy["data"]["admission"].pop("native_agent_ref")
                legacy["data"]["admission"].pop("billing_scope")
                legacy["data"]["admission"].pop("provider_identity")
                self.assertEqual(validator.is_valid(legacy), with_context)
                if not with_context:
                    devops = deepcopy(legacy["data"]["admission"])
                    devops["service_id"] = "devops"
                    Draft202012Validator({"$ref": "#/$defs/Admission",
                                          "$defs": tools[operation]["outputSchema"]["$defs"]}).validate(devops)

    async def test_transform_admits_pinned_inputs_and_dispatches_only_committed_work(self):
        async with self.sessions() as session:
            configuration = await session.get(Configuration, ("tenant-a", "configuration-a"))
            configuration.service_id = "transform"
            await session.commit()
        source = {**self.submission()[0]["sources"][0], "record_type": "repository_commit"}
        connection = self.submission()[1]["connection_ref"]
        executor = {**source, "source_connection_id": "executor-control", "record_type": "executor_binding"}
        policy = {**source, "record_type": "test_policy"}
        qualification = {
            "schema_version": "1.0", "interface": "synthetic", "cli_version": "test-only",
            "binary_sha256": "a" * 64, "image_digest": "sha256:" + "b" * 64,
            "definition_revision": "test-only", "region_qualification": "qualified",
            "executor_qualification": "qualified",
            **{name: {"status": "supported", "reason": "synthetic fixture",
                      "receipt_sha256": "c" * 64}
               for name in ("tool_isolation", "submit", "read", "resume")},
            "cancel": {"status": "unsupported", "reason": "no remote cancel"},
        }
        captures, messages = [], []

        class Capturer:
            async def pin_source(self, product_id, project_id, request, *, idempotency_key):
                captures.append(request)
                return {"snapshot_id": "pinned", "source_ref": request["source_ref"],
                        "exclusions": request["exclusions"], "expires_at": request["retain_until"],
                        "content": {"artifact_id": "sealed", "version": "1", "sha256": "a" * 64,
                                    "media_type": "application/octet-stream", "bytes": 2},
                        "file_manifest": {"artifact_id": "manifest", "version": "1", "sha256": "b" * 64,
                                          "media_type": "application/json", "bytes": 3},
                        "file_count": 1}

        async def publish(envelope, delay):
            async with self.sessions() as session:
                work = await session.get(Work, ("tenant-a", envelope["work_id"]))
                self.assertIsNotNone(work)
                self.assertIsNotNone(await session.get(Run, ("tenant-a", work.run_id)))
            messages.append(envelope)

        async def budget_guard(session, limits, scope, now, replay):
            self.assertEqual(scope["tenant_id"], "tenant-a")
            return None

        for capability in ("refactor", "dependency_upgrade"):
            with self.subTest(capability=capability):
                goal = {"kind": capability, "summary": "Pinned goal"}
                if capability == "dependency_upgrade":
                    goal["target"] = {"kind": "dependency", "name": "example", "version": "2.0"}
                run_input = {"service_id": "transform", "configuration_revision": 1,
                             "binding_id": "binding-a", "capability": capability,
                             "sources": [source], "transform_input": {"goal": goal}}
                _, verified = self.submission()
                verified.update(service_id="transform", source_refs=[source], capabilities=[capability],
                                provider_identity={"account_id": "123456789012",
                                                   "role_arn": "arn:aws:iam::123456789012:role/example",
                                                   "region": "example-region"},
                                transform_qualification=qualification, qualified_executor_ref=executor,
                                transform_settings={"connection_ref": connection,
                                                    "executor_binding_ref": executor, "test_policy_ref": policy,
                                                    "region": "example-region", "read_paths": ["src"],
                                                    "write_paths": ["src/app.py"], "excluded_paths": [".git"],
                                                    "goal_defaults": goal})
                verified["transform_test_policy"] = {
                    "test_policy_ref": policy, "image_digest": "sha256:" + "b" * 64,
                    "bundle_sha256": "c" * 64, "lock_sha256": "d" * 64, "oracle_version": "oracle-1",
                    "commands": [{"argv": ["/oracle/check"], "cwd": ".", "environment": {},
                                  "timeout_seconds": 10, "minimum_test_count": 1}]}
                if capability == "dependency_upgrade":
                    verified["transform_artifact_hashes"] = ["sha256:" + "b" * 64]
                    verified["transform_settings"]["write_paths"] = ["pyproject.toml", "uv.lock"]
                verified["trigger"] = {**verified["trigger"], "capability": capability, "source_refs": [source]}
                key = "transform-" + capability
                for unavailable in (None, {**verified["transform_test_policy"],
                                           "test_policy_ref": {"record_id": "other"}}):
                    incomplete = {**verified, "transform_test_policy": unavailable}
                    async with self.sessions() as session:
                        with self.assertRaises(HTTPException) as error:
                            await admit_run(session, Capturer(), run_input, incomplete,
                                            key + "-invalid", now=self.now)
                    self.assertEqual(error.exception.detail["code"], "transform_test_policy_unavailable")
                    self.assertEqual(len(captures), 0 if capability == "refactor" else 1)
                for identity in (None, {}, {**verified["provider_identity"], "account_id": "invalid"},
                                 {**verified["provider_identity"], "role_arn": "arn:aws:iam::000000000000:role/other"},
                                 {**verified["provider_identity"], "region": "other-region"}):
                    async with self.sessions() as session:
                        with self.assertRaises(HTTPException) as error:
                            await admit_run(session, Capturer(), run_input,
                                            {**verified, "provider_identity": identity},
                                            key + "-invalid-identity", now=self.now)
                    self.assertEqual(error.exception.detail["code"], "native_unqualified")
                    self.assertEqual(len(captures), 0 if capability == "refactor" else 1)
                async with self.sessions() as session:
                    run = await submit_and_dispatch(session, self.sessions, Capturer(), publish,
                        run_input, verified, key, now=self.now, budget_guard=budget_guard)
                self.assertEqual(run.admission["source_snapshots"][0]["source_ref"], source)
                self.assertEqual(run.admission["transform_settings"]["goal_defaults"], goal)
                async with self.sessions() as session:
                    stored = await session.get(Run, (run.tenant_id, run.run_id))
                self.assertEqual(stored.admission["provider_identity"], verified["provider_identity"])
                claim = await claim_work(self.sessions, messages[-1]["work_id"],
                                         messages[-1]["generation"], now=self.now)
                submit_id = await finish_work(self.sessions, claim, state="queued",
                                              next_kind="submit", now=self.now)
                claim = await claim_work(self.sessions, submit_id, claim.generation, now=self.now)
                async with self.sessions() as session:
                    stored = await session.get(Run, (run.tenant_id, run.run_id))
                broker_requests = []

                async def broker(request):
                    broker_requests.append(json.loads(request.content))
                    return httpx.Response(200, json={
                        "operation_id": stored.run_id, "attempt_id": claim.work_id,
                        "delegation_revision": stored.admission["delegation_version"],
                        "provider_identity": verified["provider_identity"],
                        "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
                        "access_key_id": "fixture-access", "secret_access_key": "fixture-secret",
                        "session_token": "fixture-token"})

                async with httpx.AsyncClient(transport=httpx.MockTransport(broker),
                                             base_url="https://example.invalid") as transport:
                    authorizer = TransformWorkAuthorizer(transport, clock=lambda: self.now)
                    authority = await authorizer(stored, claim, "submit")
                    self.assertEqual(authority.provider_identity, verified["provider_identity"])
                    self.assertEqual(authority.provider_credentials()["session_token"], "fixture-token")
                    for field, replacement in (("account_id", "000000000000"),
                                                ("role_arn", "arn:aws:iam::123456789012:role/other"),
                                                ("region", "other-region")):
                        original = verified["provider_identity"][field]
                        verified["provider_identity"][field] = replacement
                        with self.assertRaises(AuthorityRevoked):
                            await authorizer(stored, claim, "submit")
                        verified["provider_identity"][field] = original
                self.assertEqual(broker_requests[0]["lease_fence"], claim.fence)
                self.assertEqual(broker_requests[0]["lease_owner"], claim.owner)
                self.assertEqual(run.input, run_input)
                self.assertEqual(messages[-1]["schema_version"], "1.0")
                self.assertEqual(set(messages[-1]), {"schema_version", "work_id", "generation"})
                if capability == "dependency_upgrade":
                    async with self.sessions() as session:
                        stored = await session.get(Run, (run.tenant_id, run.run_id))
                    self.assertEqual(stored.admission["transform_artifact_hashes"],
                                     verified["transform_artifact_hashes"])
                    await self.assert_admitted_dependency_collection(stored)
                    verified["transform_artifact_hashes"] = ["sha256:" + "f" * 64]
                async with self.sessions() as session:
                    replay = await submit_and_dispatch(session, self.sessions, Capturer(), publish,
                        run_input, verified, key, now=self.now, budget_guard=budget_guard)
                self.assertEqual(replay.run_id, run.run_id)
                if capability == "dependency_upgrade":
                    self.assertEqual(replay.admission["transform_artifact_hashes"], ["sha256:" + "b" * 64])
        self.assertEqual(len(captures), 2)
        self.assertEqual(len(messages), 2)
        self.assertEqual(await self.counts(), [2, 2, 4])

    async def assert_admitted_dependency_collection(self, run):
        import difflib
        from hashlib import sha256
        from types import SimpleNamespace
        from unittest.mock import patch

        from l5_services.adapters.transform.dependency_oracle import compare_dependency_upgrade
        from l5_services.adapters.transform.execution import TransformAdapter
        from l5_services.work_store import AttemptContext, TRANSFORM_SUBMIT_OPERATION

        def files(version, digest):
            return {"pyproject.toml": f'[project]\ndependencies = ["example=={version}"]\n'.encode(),
                    "uv.lock": (f'version = 1\n[[package]]\nname = "example"\nversion = "{version}"\n'
                                f'sdist = {{hash = "{digest}"}}\n').encode()}

        base = files("1.0", "sha256:" + "a" * 64)
        candidate = files("2.0", "sha256:" + "b" * 64)
        raw_patch = "".join("".join(difflib.unified_diff(
            base[path].decode().splitlines(True), candidate[path].decode().splitlines(True),
            fromfile=f"a/{path}", tofile=f"b/{path}")) for path in base).encode()
        run.manifest = {"run_id": run.run_id, "sources": run.input["sources"],
                        "source_snapshot_ids": [entry["snapshot_id"] for entry in run.admission["source_snapshots"]],
                        "files": [{"path": path, "sha256": sha256(contents).hexdigest(), "bytes": len(contents)}
                                  for path, contents in base.items()]}
        input_hash = sha256(json.dumps(run.manifest, sort_keys=True).encode()).hexdigest()
        token = sha256(f"{run.tenant_id}:{run.run_id}:{TRANSFORM_SUBMIT_OPERATION}:{input_hash}".encode()).hexdigest()
        attempt = AttemptContext("attempt", run.run_id, "work", 1, token, input_hash, self.now, True)
        reference = {"tenant_id": run.tenant_id, "project_id": run.project_id, "artifact_id": "patch",
                     "version": "1", "media_type": "text/x-diff", "sha256": sha256(raw_patch).hexdigest(),
                     "bytes": len(raw_patch)}

        async def collect_provider(**kwargs):
            return {"native_job_ref": native, "state": "completed", "artifacts": {"original_patch": reference}}

        async def read_original_patch(**kwargs):
            return raw_patch

        async def read_verified_trees(**kwargs):
            return {"base_files": base, "candidate_files": candidate,
                    "base_modes": {path: "100644" for path in base},
                    "candidate_modes": {path: "100644" for path in candidate}}

        adapter = TransformAdapter(SimpleNamespace(collect_provider=collect_provider,
                                   read_original_patch=read_original_patch, read_verified_trees=read_verified_trees))
        intent = adapter._intent(run, attempt)
        native = {key: intent[key] for key in ("tenant_id", "project_id", "executor_name", "attempt_token", "input_hash")}
        native.update(executor_uid="executor", native_id="session")
        with patch("l5_services.adapters.transform.execution.compare_dependency_upgrade",
                   wraps=compare_dependency_upgrade) as parser:
            result = (await adapter.collect(run, native, attempt, object())).result
        parser.assert_called_once_with(base, candidate, run.input["transform_input"]["goal"]["target"],
                                       ["sha256:" + "b" * 64])
        self.assertEqual(result["dependency_comparison"]["status"], "passed")

    async def test_exact_capture_preserves_source_order_and_replay_never_recaptures(self):
        inputs, verified = self.submission()
        second = {**inputs["sources"][0], "record_id": "source-b", "record_revision": "revision-2"}
        inputs["sources"].append(second)
        verified["trigger"]["source_refs"] = inputs["sources"]
        captures = []

        class Capturer:
            async def pin_source(self, product_id, project_id, request, *, idempotency_key):
                captures.append((product_id, project_id, request, idempotency_key))
                return {
                    "snapshot_id": f"snapshot-{len(captures)}", "source_ref": request["source_ref"],
                    "exclusions": request["exclusions"], "expires_at": request["retain_until"],
                    "content": {"artifact_id": "content", "version": "v1", "sha256": "a" * 64,
                                "media_type": "application/octet-stream", "bytes": 5},
                    "file_manifest": {"artifact_id": "manifest", "version": "v1", "sha256": "b" * 64,
                                      "media_type": "application/json", "bytes": 10},
                    "file_count": 1,
                }

        async with self.sessions() as session:
            first = await admit_run(session, Capturer(), inputs, verified, "key-1234567890123456", now=self.now)
        async with self.sessions() as session:
            repeated = await admit_run(session, Capturer(), inputs, verified, "key-1234567890123456", now=self.now)
        self.assertEqual(repeated.run_id, first.run_id)
        self.assertEqual([item["source_ref"] for item in repeated.admission["source_snapshots"]], inputs["sources"])
        self.assertEqual([entry[2]["source_ref"] for entry in captures], inputs["sources"])
        self.assertEqual(captures[0][2]["retain_until"], "2026-01-01T01:00:00Z")
        self.assertEqual(await self.counts(), [1, 1, 1])

    async def test_changed_payload_conflicts_without_new_capture(self):
        inputs, verified = self.submission()
        captures = []

        class Capturer:
            async def pin_source(self, product_id, project_id, request, *, idempotency_key):
                captures.append(request)
                return {
                    "snapshot_id": "snapshot-a", "source_ref": request["source_ref"],
                    "exclusions": request["exclusions"], "expires_at": request["retain_until"],
                    "content": {"artifact_id": "content", "version": "v1", "sha256": "a" * 64,
                                "media_type": "application/octet-stream", "bytes": 5},
                    "file_manifest": {"artifact_id": "manifest", "version": "v1", "sha256": "b" * 64,
                                      "media_type": "application/json", "bytes": 10},
                    "file_count": 1,
                }

        async with self.sessions() as session:
            await admit_run(session, Capturer(), inputs, verified, "key-1234567890123456", now=self.now)
        changed = {**inputs, "sources": [{**inputs["sources"][0], "record_revision": "revision-2"}]}
        async with self.sessions() as session:
            with self.assertRaisesRegex(Exception, "idempotency_conflict"):
                await admit_run(session, Capturer(), changed, verified, "key-1234567890123456", now=self.now)
        self.assertEqual(len(captures), 1)
        self.assertEqual(await self.counts(), [1, 1, 1])

    async def test_mismatched_capture_never_admits(self):
        inputs, verified = self.submission()

        class BadCapturer:
            async def pin_source(self, product_id, project_id, request, *, idempotency_key):
                return {"snapshot_id": "snapshot-a", "source_ref": {**request["source_ref"],
                        "record_revision": "wrong-revision"}, "exclusions": request["exclusions"],
                        "expires_at": request["retain_until"]}

        async with self.sessions() as session:
            with self.assertRaisesRegex(Exception, "immutable_source_required"):
                await admit_run(session, BadCapturer(), inputs, verified, "key-1234567890123456", now=self.now)
        self.assertEqual(await self.counts(), [0, 0, 0])

    async def test_moving_alias_is_rejected_before_capture(self):
        inputs, verified = self.submission()
        inputs["sources"][0]["record_revision"] = "latest"

        class ForbiddenCapturer:
            async def pin_source(self, *args, **kwargs):
                raise AssertionError("moving alias must not reach Product")

        async with self.sessions() as session:
            with self.assertRaisesRegex(Exception, "validation_error"):
                await admit_run(session, ForbiddenCapturer(), inputs, verified,
                                "key-1234567890123456", now=self.now)
        self.assertEqual(await self.counts(), [0, 0, 0])

    async def capture_recovery(self, source_count, fail_at):
        inputs, verified = self.submission()
        if source_count == 2:
            inputs["sources"].append({**inputs["sources"][0], "record_id": "source-b"})
            verified["trigger"]["source_refs"] = inputs["sources"]
        stored, attempts = {}, []

        def product_capture(request):
            body = json.loads(request.content)
            key = request.headers["Idempotency-Key"]
            attempts.append((key, body))
            if key in stored and stored[key][0] != body:
                return httpx.Response(409, json={"code": "idempotency_conflict"})
            snapshot = {
                "snapshot_id": f"snapshot-{body['source_ref']['record_id']}", "source_ref": body["source_ref"],
                "exclusions": body["exclusions"], "expires_at": body["retain_until"], "file_count": 1,
                "content": {"artifact_id": "content", "version": "1", "sha256": "a" * 64,
                            "media_type": "application/octet-stream", "bytes": 5},
                "file_manifest": {"artifact_id": "manifest", "version": "1", "sha256": "b" * 64,
                                  "media_type": "application/json", "bytes": 10},
            }
            stored[key] = (body, snapshot)
            if len(attempts) == fail_at:
                raise httpx.ReadTimeout("capture response lost", request=request)
            return httpx.Response(200, json=stored[key][1])

        async with httpx.AsyncClient(transport=httpx.MockTransport(product_capture),
                                     base_url="http://product.test") as transport:
            client = ProductClient(transport, verified["delegation_id"])
            async with self.sessions() as session:
                with self.assertRaises(httpx.ReadTimeout):
                    await admit_run(session, client, inputs, verified, "capture-recovery-01", now=self.now)
            self.assertEqual(await self.counts(), [0, 0, 0])
            async with self.sessions() as independent:
                preparation = (await independent.scalars(select(AdmissionPreparation))).one()
                self.assertEqual(preparation.deadline_at.replace(tzinfo=UTC), self.now + timedelta(hours=1))
            async with self.sessions() as session:
                recovered = await admit_run(session, client, inputs, verified, "capture-recovery-01",
                                             now=self.now + timedelta(seconds=1))
            async with self.sessions() as session:
                replay = await admit_run(session, client, inputs, verified, "capture-recovery-01",
                                          now=self.now + timedelta(seconds=2))
            self.assertEqual(replay.run_id, recovered.run_id)
        self.assertEqual(attempts[0], attempts[fail_at])
        self.assertEqual(len(stored), source_count)
        self.assertEqual(await self.counts(), [1, 1, 1])
        async with self.sessions() as independent:
            run = await independent.get(Run, ("tenant-a", recovered.run_id))
            self.assertEqual(run.deadline_at.replace(tzinfo=UTC), self.now + timedelta(hours=1))
            self.assertEqual([snapshot["source_ref"] for snapshot in run.admission["source_snapshots"]],
                             inputs["sources"])

    async def test_lost_capture_response_reuses_persisted_request(self):
        await self.capture_recovery(1, 1)

    async def test_later_source_failure_reuses_all_capture_requests(self):
        await self.capture_recovery(2, 2)

    async def test_invalid_run_input_never_prepares_or_captures(self):
        inputs, verified = self.submission()
        invalid = [
            {**inputs, "configuration_revision": True}, {**inputs, "configuration_revision": "1"},
            {**inputs, "configuration_revision": 1.0}, {**inputs, "configuration_revision": 0},
            {**inputs, "unexpected": "value"}, {**inputs, "binding_id": 42},
            {**inputs, "parent_task_ref": None}, {**inputs, "parent_task_ref": {"record_id": "task"}},
            {**inputs, "pr_base": "revision-1", "pr_head": "revision-1"},
            {**inputs, "sources": []},
            {**inputs, "sources": [{**inputs["sources"][0], "record_id": "x" * 256}]},
        ]

        class ForbiddenCapturer:
            async def pin_source(self, *args, **kwargs):
                raise AssertionError("invalid input must never reach Product")

        for malformed in invalid:
            with self.subTest(input=malformed):
                async with self.sessions() as session:
                    with self.assertRaises(HTTPException) as rejected:
                        await admit_run(session, ForbiddenCapturer(), malformed, verified,
                                        "invalid-input-001", now=self.now)
                    self.assertEqual(rejected.exception.status_code, 422)
        self.assertEqual(await self.counts(), [0, 0, 0])
        async with self.sessions() as independent:
            self.assertEqual(await independent.scalar(select(func.count()).select_from(AdmissionPreparation)), 0)

    async def test_incomplete_capture_cannot_change_parameters_or_extend_deadline(self):
        inputs, verified = self.submission()
        calls = []

        class FailedCapturer:
            async def pin_source(self, *args, **kwargs):
                calls.append(args)
                raise TimeoutError("capture outcome unknown")

        async with self.sessions() as session:
            with self.assertRaises(TimeoutError):
                await admit_run(session, FailedCapturer(), inputs, verified, "stable-prepare-001", now=self.now)
        for changed, clock, status in (
            ({**verified, "max_files": verified["max_files"] + 1}, self.now, 409),
            (verified, self.now + timedelta(hours=1), 410),
        ):
            async with self.sessions() as session:
                with self.assertRaises(HTTPException) as rejected:
                    await admit_run(session, FailedCapturer(), inputs, changed, "stable-prepare-001", now=clock)
                self.assertEqual(rejected.exception.status_code, status)
        self.assertEqual(len(calls), 1)
        self.assertEqual(await self.counts(), [0, 0, 0])

    async def test_expired_capture_never_admits(self):
        inputs, verified = self.submission()

        class ExpiredCapturer:
            async def pin_source(self, product_id, project_id, request, *, idempotency_key):
                return {
                    "snapshot_id": "snapshot-a", "source_ref": request["source_ref"],
                    "exclusions": request["exclusions"], "expires_at": "2026-01-01T00:01:00Z",
                    "content": {"artifact_id": "content", "version": "v1", "sha256": "a" * 64,
                                "media_type": "application/octet-stream", "bytes": 5},
                    "file_manifest": {"artifact_id": "manifest", "version": "v1", "sha256": "b" * 64,
                                      "media_type": "application/json", "bytes": 10},
                    "file_count": 1,
                }

        async with self.sessions() as session:
            with self.assertRaisesRegex(Exception, "immutable_source_required"):
                await admit_run(session, ExpiredCapturer(), inputs, verified,
                                "key-1234567890123456", now=self.now)
        self.assertEqual(await self.counts(), [0, 0, 0])

    async def test_submit_route_returns_durable_run_and_replays(self):
        import httpx
        from fastapi import FastAPI
        from l5_services.api.router import create_router
        from l5_services.admission import submit_and_dispatch

        inputs, verified = self.submission()
        captures = []
        delivered = []
        owner = self

        class Capturer:
            async def pin_source(self, product_id, project_id, request, *, idempotency_key):
                captures.append(request)
                return {
                    "snapshot_id": "snapshot-a", "source_ref": request["source_ref"],
                    "exclusions": request["exclusions"], "expires_at": request["retain_until"],
                    "content": {"artifact_id": "content", "version": "v1", "sha256": "a" * 64,
                                "media_type": "application/octet-stream", "bytes": 5},
                    "file_manifest": {"artifact_id": "manifest", "version": "v1", "sha256": "b" * 64,
                                      "media_type": "application/json", "bytes": 10},
                    "file_count": 1,
                }

        async def publish(envelope, delay):
            async with self.sessions() as independent:
                assert await independent.scalar(select(func.count()).select_from(Work)) == 1
            delivered.append(envelope)

        class VerifiedBackend:
            async def submit(self, product_id, project_id, service_id, run_input, idempotency_key):
                assert (product_id, project_id, service_id) == ("product-a", "project-a", "security")
                async with owner.sessions() as session:
                    return await submit_and_dispatch(session, owner.sessions, Capturer(), publish,
                                                     run_input, verified, idempotency_key, now=owner.now)

        app = FastAPI()
        app.include_router(create_router(lambda: VerifiedBackend()))
        path = "/v1/products/product-a/projects/project-a/services/security/runs"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            first = await client.post(path, json=inputs, headers={"Idempotency-Key": "key-1234567890123456"})
            replay = await client.post(path, json=inputs, headers={"Idempotency-Key": "key-1234567890123456"})
            changed = await client.post(path, json={**inputs, "binding_id": "binding-b"},
                                        headers={"Idempotency-Key": "key-1234567890123456"})
            missing_key = await client.post(path, json=inputs)
            verified["configuration_enabled"] = False
            disabled_replay = await client.post(path, json=inputs,
                                                headers={"Idempotency-Key": "key-1234567890123456"})
        self.assertEqual(disabled_replay.status_code, 409)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(first.headers["etag"], '"1"')
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(missing_key.status_code, 428)
        self.assertEqual(len(captures), 1)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(await self.counts(), [1, 1, 1])

    async def test_failed_send_remains_owed_and_restart_recovers_same_work(self):
        from l5_services.dispatcher import dispatch_work

        run, receipt, work = self.admission()
        async with self.sessions() as session:
            await persist_admission(session, run, receipt, work)
        attempts = []

        async def failed_publish(envelope, delay):
            attempts.append((envelope, delay))
            raise ConnectionError("offline transport")

        self.assertFalse(await dispatch_work(self.sessions, "tenant-a", work.work_id, failed_publish, now=self.now))
        async with self.sessions() as independent:
            owed = await independent.get(Work, ("tenant-a", work.work_id))
            self.assertIsNone(owed.last_enqueued_at)
            self.assertIsNone(owed.dispatch_until)
            self.assertEqual(owed.last_error, "publication_failed")
            self.assertEqual(owed.attempts, 1)
            self.assertIsNotNone(await independent.get(RunReceipt, (
                "tenant-a", "product-a", "project-a", "security", "owner-a", "submit", receipt.idempotency_key,
            )))
        delivered = []

        async def publish(envelope, delay):
            delivered.append((envelope, delay))

        restarted_sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.assertTrue(await dispatch_work(restarted_sessions, "tenant-a", work.work_id, publish, now=self.now))
        expected = {"schema_version": "1.0", "work_id": work.work_id, "generation": 1}
        self.assertEqual(attempts, [(expected, 0)])
        self.assertEqual(delivered, [(expected, 0)])
        async with restarted_sessions() as independent:
            owed = await independent.get(Work, ("tenant-a", work.work_id))
            self.assertEqual(owed.attempts, 2)
            self.assertEqual(owed.last_enqueued_at.replace(tzinfo=UTC), self.now)
            self.assertIsNone(owed.completed_at)
        self.assertEqual(await self.counts(), [1, 1, 1])

    async def test_future_work_is_delayed_and_not_redispatched_before_bound(self):
        from l5_services.dispatcher import dispatch_work

        run, receipt, work = self.admission()
        work.due_at = self.now + timedelta(minutes=10)
        async with self.sessions() as session:
            await persist_admission(session, run, receipt, work)
        sent = []

        async def publish(envelope, delay):
            sent.append((envelope, delay))

        self.assertTrue(await dispatch_work(self.sessions, "tenant-a", work.work_id, publish, now=self.now))
        self.assertEqual(sent[0][1], 600)
        self.assertFalse(await dispatch_work(self.sessions, "tenant-a", work.work_id, publish,
                                             now=self.now + timedelta(minutes=11)))
        self.assertEqual(len(sent), 1)
        self.assertTrue(await dispatch_work(self.sessions, "tenant-a", work.work_id, publish,
                                            now=self.now + timedelta(minutes=26)))
        self.assertEqual(sent[0][0], sent[1][0])

    async def test_post_commit_failure_is_repaired_without_resubmission(self):
        from l5_services.admission import submit_and_dispatch
        from l5_services.dispatcher import repair_owed_work

        inputs, verified = self.submission()
        captures = []
        delivered = []

        class Capturer:
            async def pin_source(self, product_id, project_id, request, *, idempotency_key):
                captures.append(request)
                return {
                    "snapshot_id": "snapshot-a", "source_ref": request["source_ref"],
                    "exclusions": request["exclusions"], "expires_at": request["retain_until"],
                    "content": {"artifact_id": "content", "version": "v1", "sha256": "a" * 64,
                                "media_type": "application/octet-stream", "bytes": 5},
                    "file_manifest": {"artifact_id": "manifest", "version": "v1", "sha256": "b" * 64,
                                      "media_type": "application/json", "bytes": 10},
                    "file_count": 1,
                }

        async def failed_publish(envelope, delay):
            async with self.sessions() as independent:
                assert await independent.scalar(select(func.count()).select_from(Run)) == 1
                assert await independent.scalar(select(func.count()).select_from(RunReceipt)) == 1
                assert await independent.scalar(select(func.count()).select_from(Work)) == 1
            delivered.append((envelope, delay))
            raise ConnectionError("simulated queue outage")

        async with self.sessions() as session:
            run = await submit_and_dispatch(session, self.sessions, Capturer(), failed_publish, inputs, verified,
                                            "key-1234567890123456", now=self.now)
        self.assertEqual(run.state, "admitted")
        self.assertEqual(len(delivered), 1)
        async with self.sessions() as independent:
            work = await independent.scalar(select(Work))
            self.assertEqual(work.last_error, "publication_failed")
            self.assertIsNone(work.last_enqueued_at)

        async def publish(envelope, delay):
            delivered.append((envelope, delay))

        restarted = async_sessionmaker(self.engine, expire_on_commit=False)
        self.assertEqual(await repair_owed_work(restarted, publish, now=self.now), 1)
        self.assertEqual(len(delivered), 2)
        self.assertEqual(delivered[0][0], delivered[1][0])
        self.assertEqual(await repair_owed_work(restarted, publish, now=self.now + timedelta(minutes=1)), 0)
        self.assertEqual(len(delivered), 2)
        self.assertEqual(await repair_owed_work(restarted, publish, now=self.now + timedelta(minutes=16)), 1)
        self.assertEqual(delivered[0][0], delivered[2][0])
        self.assertEqual(len(captures), 1)
        self.assertEqual(await self.counts(), [1, 1, 1])

    async def test_disabled_or_wrong_trigger_creates_no_work(self):
        inputs, verified = self.submission()

        class ForbiddenCapturer:
            async def pin_source(self, *args, **kwargs):
                raise AssertionError("refused submissions cannot capture sources")

        for override, expected in (
            ({"configuration_enabled": False}, "configuration_disabled"),
            ({"trigger": {**verified["trigger"], "enabled": False}}, "trigger_disabled"),
            ({"trigger": {**verified["trigger"], "binding_id": "other-binding"}}, "not_found"),
            ({"binding_state": "blocked"}, "binding_or_delegation_unavailable"),
            ({"binding_connection_ref": {"kind": "credential", "id": "different"}},
             "binding_or_delegation_unavailable"),
        ):
            async with self.sessions() as session:
                with self.subTest(expected=expected), self.assertRaisesRegex(Exception, expected):
                    await admit_run(session, ForbiddenCapturer(), inputs, {**verified, **override},
                                    "key-1234567890123456", now=self.now)
        event_trigger = {**verified["trigger"], "kind": "schedule", "enabled": False}
        async with self.sessions() as session:
            with self.assertRaisesRegex(Exception, "trigger_disabled"):
                await admit_run(session, ForbiddenCapturer(), inputs, {
                    **verified, "trigger": event_trigger, "actor_type": "service", "event_id": "event-a",
                }, "event-1234567890123456", now=self.now)
        self.assertEqual(await self.counts(), [0, 0, 0])

    async def test_run_reads_hide_private_and_foreign_scope_without_side_effects(self):
        from l5_services.run_access import RunStore

        run, receipt, work = self.admission()
        run.admission = {"scope": {"tenant_id": "tenant-a", "product_id": "product-a",
                                   "project_id": "project-a", "owner_id": "owner-a", "visibility": "only_me"}}
        async with self.sessions() as session:
            await persist_admission(session, run, receipt, work)
        before = await self.counts()
        async with self.sessions() as session:
            owner = RunStore(session, "tenant-a", "product-a", "project-a", "security", "owner-a")
            member = RunStore(session, "tenant-a", "product-a", "project-a", "security", "member-a")
            foreign = RunStore(session, "tenant-b", "product-a", "project-a", "security", "owner-a")
            wrong_project = RunStore(session, "tenant-a", "product-a", "project-b", "security", "owner-a")
            self.assertEqual((await owner.get(run.run_id)).run_id, run.run_id)
            for denied in (member, foreign, wrong_project):
                with self.assertRaisesRegex(Exception, "not_found"):
                    await denied.get(run.run_id)
                self.assertEqual(await denied.list(), [])
            self.assertEqual([item.run_id for item in await owner.list()], [run.run_id])
        self.assertEqual(await self.counts(), before)
        async with self.sessions() as session:
            unchanged = await session.get(Run, ("tenant-a", run.run_id))
            self.assertEqual((unchanged.revision, unchanged.state), (1, "admitted"))

    async def test_queued_cancellation_fences_work_and_repair_ignores_it(self):
        from l5_services.dispatcher import dispatch_work, repair_owed_work
        from l5_services.run_access import RunStore

        run, receipt, work = self.admission()
        run.admission = {"scope": {"tenant_id": "tenant-a", "product_id": "product-a",
                                   "project_id": "project-a", "owner_id": "owner-a", "visibility": "product_team"}}
        async with self.sessions() as session:
            await persist_admission(session, run, receipt, work)
        async with self.sessions() as session:
            owner = RunStore(session, "tenant-a", "product-a", "project-a", "security", "owner-a")
            member = RunStore(session, "tenant-a", "product-a", "project-a", "security", "member-a")
            self.assertEqual((await member.get(run.run_id)).run_id, run.run_id)
            with self.assertRaisesRegex(Exception, "not_found"):
                await member.cancel_queued(run.run_id, "stop", "cancel-key-000001", now=self.now)
            stopped = await owner.cancel_queued(run.run_id, "stop", "cancel-key-000001", now=self.now)
            self.assertEqual((stopped["state"], stopped["revision"]), ("cancelled", 2))
            self.assertEqual(await owner.cancel_queued(run.run_id, "stop", "cancel-key-000001", now=self.now), stopped)
        sent = []

        async def publish(envelope, delay):
            sent.append(envelope)

        self.assertFalse(await dispatch_work(self.sessions, "tenant-a", work.work_id, publish, now=self.now))
        self.assertEqual(await repair_owed_work(self.sessions, publish, now=self.now), 0)
        self.assertEqual(sent, [])
        async with self.sessions() as session:
            fenced = await session.get(Work, ("tenant-a", work.work_id))
            self.assertEqual(fenced.fence, 1)
            self.assertIsNotNone(fenced.completed_at)
        self.assertEqual(await self.counts(), [1, 2, 1])

    async def test_active_execution_lease_cannot_be_cancelled_as_queued(self):
        from l5_services.run_access import RunStore

        run, receipt, work = self.admission()
        run.admission = {"scope": {"tenant_id": "tenant-a", "product_id": "product-a",
                                   "project_id": "project-a", "owner_id": "owner-a", "visibility": "only_me"}}
        work.lease_until = self.now + timedelta(minutes=5)
        work.lease_owner = "worker-a"
        async with self.sessions() as session:
            await persist_admission(session, run, receipt, work)
        async with self.sessions() as session:
            owner = RunStore(session, "tenant-a", "product-a", "project-a", "security", "owner-a")
            with self.assertRaisesRegex(Exception, "not_queued"):
                await owner.cancel_queued(run.run_id, "stop", "cancel-key-000001", now=self.now)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Run, ("tenant-a", run.run_id))).state, "admitted")
            self.assertIsNone((await session.get(Work, ("tenant-a", work.work_id))).completed_at)
