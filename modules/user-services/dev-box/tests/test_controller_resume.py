"""A stopped-and-started box is re-verified on the same generation (#8223 causes 18/19).

The gateway's lease activation leaves the owner's CrewExecutor ``ready``; readiness, the host
restore context and activation itself only act on ``provisioning``. Proven here: an observed
stop parks the executor (``stopped``), the enrollment that follows a start returns it to
``provisioning`` (also from a stale ``ready``), other generations and owners are untouched, and
the readiness command carries the Crew edge origin to the host only when the controller has it.
"""

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from src.shared.models.base import Base
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxInstance, DevBoxOperation, DevBoxPolicy, DevBoxTemplate
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import state  # noqa: E402
from controller.identity import crew_bootstrap_command, crew_dashboard_origin, resume_host_executor  # noqa: E402
from controller.launch import LaunchTemplate, ownership_tags  # noqa: E402


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    tables = [Organization.__table__, User.__table__, TenantMembership.__table__, DevBoxPolicy.__table__, DevBoxTemplate.__table__,
              DevBox.__table__, DevBoxInstance.__table__, DevBoxOperation.__table__, CrewExecutor.__table__]
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
                           profile="crew", machine_generation=2, status="running", desired_state="stopped", observed_state="running"))
        await session.flush()
        session.add(DevBoxInstance(box_id="box", user_id="owner", org_id="org", deployment_id="dep", machine_generation=2,
                                   template_revision="r1", provider_instance_id="i-owned", observed_state="running",
                                   private_address="10.4.0.3", certificate_fingerprint="a" * 64))
        session.add(DevBoxOperation(id="stop", box_id="box", user_id="owner", org_id="org", deployment_id="dep", machine_generation=2,
                                    action="stop", status="pending", idempotency_key="stop-key", request_digest="d",
                                    actor_user_id="owner", provider_token="b" * 64, progress="admitted", retries=0))
        session.add_all([
            CrewExecutor(id="current", tenant_id="org", user_id="owner", target_kind="ec2-instance", dev_box_id="box", generation=2,
                         state="ready", owner_key="k" * 52, lease_generation=2, lease_expires_at=4_000_000_000, target_ref="box"),
            CrewExecutor(id="retired", tenant_id="org", user_id="owner", target_kind="ec2-instance", dev_box_id="box", generation=1,
                         state="deleted", owner_key="j" * 52),
            CrewExecutor(id="neighbour", tenant_id="org", user_id="other", target_kind="ec2-instance", dev_box_id="other-box",
                         generation=1, state="ready", owner_key="h" * 52, lease_generation=1, lease_expires_at=4_000_000_000),
        ])
        await session.commit()
    yield factory
    await engine.dispose()


async def executor_states(database):
    async with database() as session:
        rows = (await session.execute(select(CrewExecutor.id, CrewExecutor.state, CrewExecutor.lease_generation))).all()
    return {row[0]: (row[1], row[2]) for row in rows}


@pytest.mark.asyncio
async def test_observed_stop_parks_the_generation_executor(database, monkeypatch):
    async def fenced(*_args, **_kwargs):
        return None

    monkeypatch.setattr(state, "require_access_fenced", fenced)

    class StoppedEC2:
        def describe_instances(self, *, InstanceIds):
            assert InstanceIds == ["i-owned"]
            return {"Reservations": [{"Instances": [{"InstanceId": "i-owned", "State": {"Name": "stopped"},
                                                     "Tags": [{"Key": k, "Value": v} for k, v in self.tags.items()]}]}]}

    provider = StoppedEC2()
    async with database() as session:
        provider.tags = ownership_tags(await session.get(DevBox, "box"), await session.get(DevBoxOperation, "stop"))

    assert await state.reconcile_state(database, provider, "stop", "worker")
    states = await executor_states(database)
    assert states["current"] == ("stopped", None), states
    assert states["neighbour"] == ("ready", 1) and states["retired"] == ("deleted", None)
    async with database() as session:
        executor = await session.get(CrewExecutor, "current")
        assert executor.last_stopped_at is not None and executor.lease_expires_at is None
        operation = await session.get(DevBoxOperation, "stop")
        assert operation.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["stopped", "ready"])
async def test_enrollment_returns_a_parked_or_stale_executor_to_provisioning(database, previous):
    async with database() as session:
        executor = await session.get(CrewExecutor, "current")
        executor.state = previous
        await session.commit()
    async with database() as session:
        box = await session.get(DevBox, "box")
        assert await resume_host_executor(session, box) is True
        await session.commit()
    states = await executor_states(database)
    assert states["current"] == ("provisioning", None)
    assert states["neighbour"] == ("ready", 1) and states["retired"] == ("deleted", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("untouched", ["provisioning", "ending", "deleted"])
async def test_only_ready_or_stopped_executors_are_reset(database, untouched):
    async with database() as session:
        executor = await session.get(CrewExecutor, "current")
        executor.state = untouched
        await session.commit()
    async with database() as session:
        box = await session.get(DevBox, "box")
        assert await resume_host_executor(session, box) is False
    assert (await executor_states(database))["current"][0] == untouched


@pytest.mark.asyncio
async def test_a_box_whose_generation_moved_on_does_not_reset_the_old_executor(database):
    async with database() as session:
        box = await session.get(DevBox, "box")
        box.machine_generation = 3  # replaced: generation 2's executor is no longer this box's
        assert await resume_host_executor(session, box) is False
    assert (await executor_states(database))["current"] == ("ready", 2)


def test_bootstrap_command_carries_the_edge_origin_only_when_the_controller_has_one():
    template = LaunchTemplate("lt", "4", "us-east-1", gateway_execute_arn="arn:aws:execute-api:us-east-1:000000000000:abc/prod",
                              crew_model_id="us.anthropic.example-v1:0")
    plain = crew_bootstrap_command(template)
    assert plain.startswith("/opt/adp/crew/current/host/node /opt/adp/crew/current/host/crew-host-bootstrap.js --gateway-arn ")
    assert "ADP_CREW_DASHBOARD_ORIGIN" not in plain
    with_origin = crew_bootstrap_command(template, dashboard_origin="https://d1example.cloudfront.net")
    assert with_origin == "ADP_CREW_DASHBOARD_ORIGIN=https://d1example.cloudfront.net " + plain
    assert crew_dashboard_origin({"CREW_DASHBOARD_ORIGIN": "https://d1example.cloudfront.net/"}) == "https://d1example.cloudfront.net"
    for bad in ({}, {"CREW_DASHBOARD_ORIGIN": ""}, {"CREW_DASHBOARD_ORIGIN": "http://plain.example"},
                {"CREW_DASHBOARD_ORIGIN": "https://edge.example/path"}, {"CREW_DASHBOARD_ORIGIN": "https://Edge.Example"}):
        assert crew_dashboard_origin(bad) is None


@pytest.mark.asyncio
async def test_release_retires_every_live_executor_of_the_box_and_keeps_its_checkpoint(database):
    """#8223 cause 23: a released box's executor stayed live and fenced the owner's next box."""
    async with database() as session:
        current = await session.get(CrewExecutor, "current")
        current.state, current.latest_verified_checkpoint_id = "stopped", "checkpoint-v2"
        box = await session.get(DevBox, "box")
        assert await state.retire_host_executors(session, box) == 1
        await session.commit()
    states = await executor_states(database)
    assert states["current"] == ("deleted", None), states
    assert states["neighbour"] == ("ready", 1) and states["retired"] == ("deleted", None)
    async with database() as session:
        retired = await session.get(CrewExecutor, "current")
        assert retired.target_ref is None and retired.lease_expires_at is None
        assert retired.latest_verified_checkpoint_id == "checkpoint-v2"
        assert await state.retire_host_executors(session, await session.get(DevBox, "box")) == 0
