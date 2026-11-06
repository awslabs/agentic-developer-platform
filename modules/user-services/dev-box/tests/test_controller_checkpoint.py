"""A stop saves the Crew home once more before the machine stops, and never blocks the stop (#8379).

The controller runs the host's final capture over SSM after the fence and before ``StopInstances``,
waits a bounded time for its JSON verdict and records the outcome as an event on the stop
operation. Proven here: verified/unchanged/failed verdicts and a timeout are recorded and the
stop still completes; a Crew box that never reached ``crew_ready`` records ``skipped``; a
non-Crew box and a controller without SSM record nothing; only the first attempt captures.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from src.shared.models.base import Base
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxEvent, DevBoxInstance, DevBoxOperation, DevBoxPolicy, DevBoxTemplate
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import checkpoint, state  # noqa: E402
from controller.launch import ownership_tags  # noqa: E402


@pytest_asyncio.fixture
async def controller_db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    tables = [Organization.__table__, User.__table__, TenantMembership.__table__, DevBoxPolicy.__table__, DevBoxTemplate.__table__,
              DevBox.__table__, DevBoxInstance.__table__, DevBoxOperation.__table__, DevBoxEvent.__table__, CrewExecutor.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([Organization(id="org", name="Org"),
                         User(id="owner", org_id="org", team_id="team", email="owner@example.test", cognito_sub="sub")])
        await session.flush()
        session.add(TenantMembership(user_id="owner", tenant_id="org", revoked_at=None))
        session.add(DevBoxPolicy(id="policy", org_id="org", deployment_id="dep", enabled=True, max_live_boxes=1, max_running_boxes=1,
                                 allowed_sizes=["t3.small"], max_disk_gb=50, max_lease_minutes=60, max_storage_gb=100,
                                 max_running_hours=100, instance_hour_rate_usd=0.1, disk_gb_hour_rate_usd=0.0001,
                                 snapshot_gb_hour_rate_usd=0.0001, budget_usd=50))
        session.add(DevBoxTemplate(id="template", name="crew", revision="r1", profile="crew", instance_size="t3.small",
                                   disk_gb=40, image_ref="ami", approved=True))
        await session.flush()
        session.add(DevBox(id="box", user_id="owner", org_id="org", deployment_id="dep", template_id="template", template_revision="r1",
                           profile="crew", machine_generation=2, status="running", desired_state="stopped", observed_state="running",
                           auth_readiness="fenced"))
        await session.flush()
        session.add(DevBoxInstance(box_id="box", user_id="owner", org_id="org", deployment_id="dep", machine_generation=2,
                                   template_revision="r1", provider_instance_id="i-owned", observed_state="running",
                                   private_address="10.4.0.3", certificate_fingerprint="a" * 64, readiness_class="crew_ready"))
        session.add(DevBoxOperation(id="stop", box_id="box", user_id="owner", org_id="org", deployment_id="dep", machine_generation=2,
                                    action="stop", status="pending", idempotency_key="stop-key", request_digest="d",
                                    actor_user_id="owner", provider_token="b" * 64, progress="admitted", retries=0))
        session.add(CrewExecutor(id="current", tenant_id="org", user_id="owner", target_kind="ec2-instance", dev_box_id="box", generation=2,
                                 state="ready", owner_key="k" * 52, lease_generation=2, lease_expires_at=4_000_000_000, target_ref="box"))
        await session.commit()
    yield factory
    await engine.dispose()


class EC2:
    """Observes the instance running, then stopped once StopInstances was called."""

    def __init__(self, tags):
        self.tags, self.stopped, self.stop_calls = tags, False, 0

    def describe_instances(self, *, InstanceIds):
        assert InstanceIds == ["i-owned"]
        return {"Reservations": [{"Instances": [{"InstanceId": "i-owned", "State": {"Name": "stopped" if self.stopped else "running"},
                                                 "Tags": [{"Key": k, "Value": v} for k, v in self.tags.items()]}]}]}

    def stop_instances(self, *, InstanceIds):
        self.stop_calls += 1
        self.stopped = True
        return {}


class SSM:
    def __init__(self, verdict, *, status="Success", pending_polls=1, stderr=""):
        self.verdict, self.status, self.pending_polls, self.stderr = verdict, status, pending_polls, stderr
        self.sent, self.polls = [], 0

    def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, *, CommandId, InstanceId):
        assert (CommandId, InstanceId) == ("cmd-1", "i-owned")
        self.polls += 1
        if self.polls <= self.pending_polls:
            return {"Status": "InProgress"}
        return {"Status": self.status, "StandardOutputContent": self.verdict, "StandardErrorContent": self.stderr}


async def events(controller_db):
    async with controller_db() as session:
        return [row[0] for row in (await session.execute(select(DevBoxEvent.event_type).order_by(DevBoxEvent.created_at))).all()]


async def provider(controller_db):
    async with controller_db() as session:
        return EC2(ownership_tags(await session.get(DevBox, "box"), await session.get(DevBoxOperation, "stop")))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def fenced(*_args, **_kwargs):
        return None

    monkeypatch.setattr(state, "require_access_fenced", fenced)
    monkeypatch.setattr(checkpoint, "PRE_STOP_CHECKPOINT_POLL_SECONDS", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict, expected", [
    ('{"status": "verified", "checkpoint_id": "c1", "sequence": 3}', "verified"),
    ('noise line\n{"status": "unchanged", "checkpoint_id": "c1"}', "unchanged"),
    ('{"status": "failed", "error": "CheckpointRefused"}', "failed"),
    ("not json", "failed"),
])
async def test_pre_stop_capture_is_recorded_and_the_stop_completes(controller_db, verdict, expected):
    ec2 = await provider(controller_db)
    ssm = SSM(verdict, stderr="Crew host checkpoint failed: CheckpointRefused" if expected == "failed" else "")
    assert await state.reconcile_state(controller_db, ec2, "stop", "worker", ssm=ssm)
    assert ssm.sent[0]["InstanceIds"] == ["i-owned"]
    assert ssm.sent[0]["Parameters"]["commands"] == ["python3 -I /opt/adp/crew/checkpoint_crew_host.py --mode final"]
    assert int(ssm.sent[0]["Parameters"]["executionTimeout"][0]) > checkpoint.PRE_STOP_CHECKPOINT_TIMEOUT_SECONDS
    assert ec2.stop_calls == 1
    assert await events(controller_db) == [f"pre_stop_checkpoint_{expected}"]
    async with controller_db() as session:
        operation = await session.get(DevBoxOperation, "stop")
        assert operation.status == "succeeded" and operation.progress == "instance_observed"
        assert (await session.get(CrewExecutor, "current")).state == "stopped"


@pytest.mark.asyncio
async def test_a_hung_capture_times_out_and_the_stop_still_proceeds(controller_db, monkeypatch):
    monkeypatch.setattr(checkpoint, "PRE_STOP_CHECKPOINT_TIMEOUT_SECONDS", 0.05)
    ec2 = await provider(controller_db)
    ssm = SSM("", pending_polls=10_000)
    assert await state.reconcile_state(controller_db, ec2, "stop", "worker", ssm=ssm)
    assert ec2.stop_calls == 1 and await events(controller_db) == ["pre_stop_checkpoint_failed"]


@pytest.mark.asyncio
async def test_an_unreachable_ssm_is_a_failed_capture_not_a_failed_stop(controller_db):
    from botocore.exceptions import ClientError

    class Broken(SSM):
        def send_command(self, **kwargs):
            raise ClientError({"Error": {"Code": "InvalidInstanceId", "Message": "offline"}}, "SendCommand")

    ec2 = await provider(controller_db)
    assert await state.reconcile_state(controller_db, ec2, "stop", "worker", ssm=Broken(""))
    assert ec2.stop_calls == 1 and await events(controller_db) == ["pre_stop_checkpoint_failed"]


@pytest.mark.asyncio
async def test_a_never_verified_crew_host_records_skipped_without_an_ssm_command(controller_db):
    async with controller_db() as session:
        instance = await session.scalar(select(DevBoxInstance))
        instance.readiness_class = "machine_running"
        await session.commit()
    ec2 = await provider(controller_db)
    ssm = SSM("")
    assert await state.reconcile_state(controller_db, ec2, "stop", "worker", ssm=ssm)
    assert ssm.sent == [] and ec2.stop_calls == 1 and await events(controller_db) == ["pre_stop_checkpoint_skipped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["no_ssm", "not_crew", "retried_attempt"])
async def test_nothing_is_captured_or_recorded_outside_the_first_crew_stop_attempt(controller_db, reason):
    ssm = SSM('{"status": "verified"}')
    async with controller_db() as session:
        if reason == "not_crew":
            (await session.get(DevBox, "box")).profile = "developer"
        if reason == "retried_attempt":
            (await session.get(DevBoxOperation, "stop")).progress = "stop_intent"
        await session.commit()
    ec2 = await provider(controller_db)
    if reason == "retried_attempt":
        ec2.stopped = True   # the provider call already happened on the first attempt
    assert await state.reconcile_state(controller_db, ec2, "stop", "worker", ssm=None if reason == "no_ssm" else ssm)
    assert ssm.sent == [] and await events(controller_db) == []
    async with controller_db() as session:
        assert (await session.get(DevBoxOperation, "stop")).status == "succeeded"


def test_verdict_parsing_only_trusts_a_clean_json_status():
    assert checkpoint.pre_stop_outcome(None) == "failed"
    assert checkpoint.pre_stop_outcome({"Status": "Failed", "StandardOutputContent": '{"status": "verified"}'}) == "failed"
    assert checkpoint.pre_stop_outcome({"Status": "Success", "StandardOutputContent": ""}) == "failed"
    assert checkpoint.pre_stop_outcome({"Status": "Success", "StandardOutputContent": '{"status": "published"}'}) == "failed"
    assert checkpoint.pre_stop_outcome({"Status": "Success", "StandardOutputContent": json.dumps({"status": "unchanged"})}) == "unchanged"


@pytest.mark.asyncio
async def test_capture_before_stop_renews_the_operation_lease_while_waiting(controller_db):
    """The wait runs inside the operation's maintained lease; a lease callback is honoured each poll."""
    renewals = []

    async def lease():
        renewals.append(1)

    async with controller_db() as session:
        box, instance, operation = await session.get(DevBox, "box"), await session.scalar(select(DevBoxInstance)), await session.get(DevBoxOperation, "stop")
    ssm = SSM('{"status": "verified"}', pending_polls=3)
    outcome = await checkpoint.capture_before_stop(controller_db, ssm, box=box, instance=instance, operation=operation, lease=lease)
    assert outcome == "verified" and len(renewals) == 4 and await events(controller_db) == ["pre_stop_checkpoint_verified"]
    await asyncio.sleep(0)
