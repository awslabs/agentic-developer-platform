"""Private control reloads durable Product work before calling an executor."""

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.transform.control_host import TransformControlHost
from l5_services.adapters.transform.control_client import TransformControlClient
from l5_services.models import Configuration, ConfigurationVersion, Run, Work
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION
from l5_services.workers.execution import AuthorityRevoked
from test_control_client import FUNCTION_ARN, PrivateFunction, fixtures as client_fixtures
from test_work_authority import Broker, IDENTITY, broker_receipt


NOW = datetime(2026, 10, 29, tzinfo=UTC)


class Executor:
    def __init__(self):
        self.effects = []

    async def launch_provider(self, *, intent, manifest, authority):
        self.effects.append((intent, manifest, authority))
        return None

    async def read_provider(self, *, intent, authority):
        self.effects.append((intent, authority))
        return None

    async def request_stop(self, *, intent, native_job_ref, authority):
        self.effects.append((intent, native_job_ref, authority))
        return None

    async def collect_provider(self, *, intent, authority):
        self.effects.append((intent, authority))
        return None


@pytest.fixture
async def control_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    intent, manifest, authority, _ = client_fixtures()
    scope = authority.scope
    admission = {
        "scope": scope, "source_snapshots": [{"snapshot_id": snapshot}
                                          for snapshot in manifest["source_snapshot_ids"]],
        "transform_settings": {"executor_binding_ref": intent["executor_binding_ref"],
                               "definition_ref": intent["definition_ref"],
                               "read_paths": intent["read_paths"], "write_paths": intent["write_paths"],
                               "excluded_paths": intent["excluded_paths"], "region": "us-east-1"},
        "transform_qualification": {
            "schema_version": "1.0", "interface": "atx-custom-def-exec-v1", "cli_version": "fixture",
            "binary_sha256": intent["binary_sha256"], "image_digest": intent["image_digest"],
            "definition_revision": intent["definition_revision"],
            "region_qualification": "qualified", "executor_qualification": "qualified",
            **{name: {"status": "supported", "reason": "offline fixture", "receipt_sha256": "c" * 64}
               for name in ("tool_isolation", "submit", "read", "resume")},
            "cancel": {"status": "unsupported", "reason": "offline fixture"}},
        "limits": {"agent_minutes": 10, "deadline_seconds": 3600},
        "delegation_id": "grant-a", "delegation_version": 2, "binding_generation": 3,
        "connection_ref": {"record_type": "connection", "tenant_id": scope["tenant_id"],
                           "project_id": scope["project_id"], "source_connection_id": "cred-a",
                           "record_id": "cred-a", "record_revision": "revision-a"},
        "monitoring_policy": {"poll_grace_seconds": 0},
        "provider_identity": dict(IDENTITY),
    }
    admission["transform_settings"]["connection_ref"] = admission["connection_ref"]
    async with sessions() as session:
        session.add(Configuration(tenant_id=scope["tenant_id"], configuration_id="config-a",
                                  product_id=scope["product_id"], project_id=scope["project_id"],
                                  service_id="transform", relationship_revision="revision-a",
                                  owner_id="owner-a", current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id="config-a",
                                         revision=1, settings={}, inherited_fields=[], gaps=[],
                                         state="ready", hash="f" * 64))
        run = Run(tenant_id=scope["tenant_id"], run_id=intent["run_id"],
                  product_id=scope["product_id"], project_id=scope["project_id"],
                  service_id="transform", configuration_id="config-a", configuration_revision=1,
                  binding_id="binding-a", trigger_id="manual-a", binding_generation=3,
                  input={"sources": manifest["sources"], "transform_input": {"goal": intent["goal"]}},
                  admission=admission, manifest=manifest, state="queued", deadline_at=NOW + timedelta(hours=1))
        session.add(run)
        work = Work(tenant_id=scope["tenant_id"], work_id="work-a", run_id=run.run_id,
                    kind="submit", generation=1, step=1, due_at=NOW,
                    lease_owner="worker-a", fence=3, lease_until=NOW + timedelta(minutes=3))
        session.add(work)
        session.add(WorkerAttempt(tenant_id=scope["tenant_id"], attempt_id="attempt-a",
                                  run_id=run.run_id, operation=TRANSFORM_SUBMIT_OPERATION,
                                  first_work_id=work.work_id, first_fence=3,
                                  idempotency_token=intent["attempt_token"], input_hash=intent["input_hash"],
                                  started_at=NOW, status="prepared"))
        await session.commit()
        receipt = broker_receipt(run, SimpleNamespace(work_id=work.work_id, owner=work.lease_owner,
                                                       fence=work.fence, generation=work.generation))
        receipt["attempt_id"] = "work-a"
        receipt["provider_identity"]["region"] = "us-east-1"
    yield sessions, Broker(receipt), intent, manifest, authority
    await engine.dispose()


@pytest.mark.asyncio
async def test_private_host_reloads_work_and_authorizes_one_launch(control_db):
    sessions, broker, intent, manifest, authority = control_db
    executor = Executor()
    host = TransformControlHost(sessions, broker, executor, clock=lambda: NOW)
    event = {"schema_version": "1.0", "kind": "l5-transform-control-v1", "request_id": "nonce-a",
             "operation": "launch", "intent": intent, "scope": authority.scope,
             "work": authority.source_request, "expires_at": authority.expires_at.isoformat(),
             "native_job_ref": None, "snapshot_ids": manifest["source_snapshot_ids"],
             "manifest_sha256": sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    result = await host(event)
    assert result["work_id"] == "work-a" and result["result"] is None
    assert len(executor.effects) == 1
    assert len(broker.requests) == 1
    assert executor.effects[0][2].provider_credentials()["access_key_id"] == "fixture-access"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["foreign_scope", "stale_fence", "expired_lease", "revoked",
                                        "forged_job", "foreign_attempt", "missing_host", "already_accepted",
                                        "source_changed"])
async def test_private_host_rejects_changes_before_executor_effect(control_db, change):
    sessions, broker, intent, manifest, authority = control_db
    executor = Executor()
    if change == "foreign_scope":
        authority.scope = {**authority.scope, "product_id": "foreign"}
    elif change == "expired_lease":
        async with sessions() as session:
            work = await session.get(Work, (intent["tenant_id"], "work-a"))
            work.lease_until = NOW - timedelta(seconds=1)
            await session.commit()
    elif change == "revoked":
        async with sessions() as session:
            run = await session.get(Run, (intent["tenant_id"], intent["run_id"]))
            run.admission = {**run.admission, "delegation_version": 4}
            await session.commit()
    elif change == "forged_job":
        intent["provider_job"]["argv"] = ["/bin/sh"]
    elif change == "foreign_attempt":
        intent["attempt_token"] = "a" * 64
    elif change == "missing_host":
        executor = object()
    elif change == "already_accepted":
        async with sessions() as session:
            attempt = await session.get(WorkerAttempt, (intent["tenant_id"], "attempt-a"))
            attempt.status = "accepted"
            await session.commit()
    elif change == "source_changed":
        async with sessions() as session:
            run = await session.get(Run, (intent["tenant_id"], intent["run_id"]))
            run.manifest = {**run.manifest, "source_snapshot_ids": ["another-snapshot"]}
            await session.commit()
    if change == "stale_fence":
        authority.source_request["lease_fence"] = 2
    event = {"schema_version": "1.0", "kind": "l5-transform-control-v1", "request_id": "nonce-a",
             "operation": "launch", "intent": intent, "scope": authority.scope,
             "work": authority.source_request, "expires_at": authority.expires_at.isoformat(),
             "native_job_ref": None, "snapshot_ids": manifest["source_snapshot_ids"],
             "manifest_sha256": sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    with pytest.raises((AuthorityRevoked, PermissionError, ValueError)):
        await TransformControlHost(sessions, broker, executor, clock=lambda: NOW)(event)
    assert not getattr(executor, "effects", [])


@pytest.mark.asyncio
async def test_private_client_and_sql_host_interoperate_without_task_authority(control_db):
    sessions, broker, intent, manifest, authority = control_db
    executor = Executor()
    host = TransformControlHost(sessions, broker, executor, clock=lambda: NOW)

    class PrivateHost(PrivateFunction):
        def invoke(self, *, FunctionName, InvocationType, Payload):
            self.calls.append((FunctionName, InvocationType, Payload))
            response = asyncio.run(host(json.loads(Payload)))
            return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(response).encode())}

    private = PrivateHost()
    client = TransformControlClient(private, function_arn=FUNCTION_ARN)
    assert await client.launch_provider(intent=intent, manifest=manifest, authority=authority) is None
    read_intent = {key: value for key, value in intent.items() if key != "provider_job"}
    assert await client.read_provider(intent=read_intent, authority=authority) is None
    assert len(executor.effects) == len(broker.requests) == len(private.calls) == 2
    assert all("fixture-secret" not in call[2].decode() for call in private.calls)
