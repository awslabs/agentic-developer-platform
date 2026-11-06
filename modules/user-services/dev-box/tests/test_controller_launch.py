"""Exercise the canonical gateway operation store with a fake EC2 inventory."""

import importlib.util
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from src.shared.models.base import Base
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import (
    DevBox,
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxPolicy,
    DevBoxResource,
    DevBoxUsage,
    DevBoxTemplate,
)
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
CONTROLLER_PATH = Path(__file__).resolve().parents[1] / "controller" / "launch.py"
SPEC = importlib.util.spec_from_file_location("dev_box_controller_launch", CONTROLLER_PATH)
MODULE = importlib.util.module_from_spec(SPEC)

sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeEC2:
    def __init__(self):
        self.instance = None
        self.visible = False
        self.calls = []
        self.lose_response = True
        self.state = "running"
        self.state_reason = None
        self.terminated = []

    def run_instances(self, **kwargs):
        self.calls.append(kwargs)
        tags = kwargs["TagSpecifications"][0]["Tags"]
        self.instance = {
            "InstanceId": "i-owned-example", "ClientToken": kwargs["ClientToken"],
            "Tags": tags, "State": {"Name": self.state}, "PrivateIpAddress": "10.4.0.3",
        }
        if self.state_reason:
            self.instance["StateReason"] = dict(self.state_reason)
        if self.lose_response:
            raise ConnectionError("the provider response was lost")
        return {"Instances": [self.instance]}

    def describe_instances(self, **kwargs):
        if "InstanceIds" in kwargs:
            found = self.instance if self.instance and self.instance["InstanceId"] in kwargs["InstanceIds"] else None
            return {"Reservations": [{"Instances": [found]}]} if found else {"Reservations": []}
        assert any(item["Name"] == "client-token" for item in kwargs["Filters"])
        return {"Reservations": [{"Instances": [self.instance]}]} if self.visible and self.instance else {"Reservations": []}

    def terminate_instances(self, **kwargs):
        self.terminated.extend(kwargs["InstanceIds"])
        self.instance["State"] = {"Name": "terminated"}


@pytest_asyncio.fixture
async def controller_database(monkeypatch):
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES", "1")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES", "1")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB", "100")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS", "100")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD", "100")
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [Organization.__table__, User.__table__, UserIdentity.__table__, TenantMembership.__table__, DevBoxPolicy.__table__, DevBoxTemplate.__table__,
              DevBox.__table__, DevBoxInstance.__table__, DevBoxResource.__table__, DevBoxUsage.__table__, DevBoxOperation.__table__,
              DevBoxEvent.__table__, CrewExecutor.__table__]  # the stop leg parks the owner's executor (#8223)
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([
            Organization(id="example-org", name="Example Org"),
            User(id="example-owner", org_id="example-org", team_id="example-team", email="owner@example.test", cognito_sub="example-sub"),
        ])
        await session.flush()
        session.add(TenantMembership(user_id="example-owner", tenant_id="example-org", revoked_at=None))
        session.add(DevBoxPolicy(id="policy", org_id="example-org", deployment_id="example-deployment", enabled=True,
                                 max_live_boxes=1, max_running_boxes=1, allowed_sizes=["t3.small"], max_disk_gb=50,
                                 max_lease_minutes=60, max_storage_gb=100, max_running_hours=100,
                                 instance_hour_rate_usd=0.10, disk_gb_hour_rate_usd=0.0001,
                                 snapshot_gb_hour_rate_usd=0.0001, budget_usd=50))
        await session.flush()
        session.add(DevBoxTemplate(id="template", name="assistant", revision="r1", profile="crew", instance_size="t3.small",
                                   disk_gb=40, image_ref="ami-example", approved=True))
        await session.flush()
        session.add(DevBox(id="box", user_id="example-owner", org_id="example-org", deployment_id="example-deployment",
                           template_id="template", template_revision="r1", profile="crew", machine_generation=1,
                           status="requested", desired_state="running", observed_state="unknown"))
        await session.flush()
        session.add(DevBoxOperation(id="operation", box_id="box", user_id="example-owner", org_id="example-org",
                                    deployment_id="example-deployment", machine_generation=1, action="setup", status="pending",
                                    idempotency_key="setup-key", request_digest="digest", actor_user_id="example-owner",
                                    provider_token="a" * 64, progress="admitted", retries=0))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_lost_launch_response_recovers_after_restart_without_a_second_instance(controller_database):
    ec2 = FakeEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "first-worker", launch)
    async with controller_database() as session:
        operation = await session.get(DevBoxOperation, "operation")
        instance = await session.scalar(select(DevBoxInstance))
        assert operation.status == "unknown"
        assert operation.provider_request_ref == "launch:" + "a" * 64
        assert instance.provider_instance_id is None
        assert instance.provider_account_ref == "000000000000"
        assert instance.image_ref == "ami-example"
    # #8037: while inventory shows nothing the launch is re-sent, but only ever with the original ClientToken,
    # so a send that did reach EC2 is de-duplicated by the provider instead of producing a second instance.
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "restarted-worker", launch)
    assert len(ec2.calls) == 2
    assert {call["ClientToken"] for call in ec2.calls} == {"a" * 64}
    ec2.visible = True
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "restarted-worker", launch)
    assert len(ec2.calls) == 2
    async with controller_database() as session:
        operation = await session.get(DevBoxOperation, "operation")
        instance = await session.scalar(select(DevBoxInstance))
        box = await session.get(DevBox, "box")
        assert operation.status == "succeeded"
        assert operation.provider_request_ref == "i-owned-example"
        assert instance.provider_instance_id == "i-owned-example"
        assert instance.provider_account_ref == "000000000000"
        assert instance.private_address == "10.4.0.3"
        assert instance.readiness_class == "machine_running"
        assert box.observed_state == "running"
        assert box.auth_readiness == "unknown"


class RefusingEC2(FakeEC2):
    """RunInstances is rejected synchronously; nothing reaches the provider."""

    def __init__(self, code="UnauthorizedOperation"):
        super().__init__()
        self.code = code

    def run_instances(self, **kwargs):
        from botocore.exceptions import ClientError

        self.calls.append(kwargs)
        raise ClientError({"Error": {"Code": self.code, "Message": "refused"}}, "RunInstances")


class UnreachableEC2(FakeEC2):
    """RunInstances times out before the request reaches the provider until `reachable` is set."""

    def __init__(self):
        super().__init__()
        self.reachable = False

    def run_instances(self, **kwargs):
        if not self.reachable:
            self.calls.append(kwargs)
            raise ConnectionError("connect timeout before the provider saw the request")
        self.lose_response = False
        return super().run_instances(**kwargs)


async def read_operation(controller_database):
    async with controller_database() as session:
        operation = await session.get(DevBoxOperation, "operation")
        box = await session.get(DevBox, "box")
        events = list(await session.scalars(select(DevBoxEvent.event_type).order_by(DevBoxEvent.created_at)))
        return operation, box, events


@pytest.mark.parametrize("code, refused", [
    ("UnauthorizedOperation", True), ("InvalidParameterValue", True), ("InvalidParameterCombination", True),
    ("UnsupportedOperation", True), ("InvalidAMIID.NotFound", True), ("InvalidAMIID.Malformed", True),
    ("InstanceLimitExceeded", True), ("VcpuLimitExceeded", True), ("InsufficientInstanceCapacity", True),
    ("MissingParameter", True),
    ("RequestLimitExceeded", False), ("Throttling", False), ("InternalError", False), ("ServiceUnavailable", False),
    ("IdempotentParameterMismatch", False), ("", False),
    # #8223 cause 53: a full subnet is EC2's synchronous answer; nothing launched.
    ("InsufficientFreeAddressesInSubnet", True), ("InsufficientCidrBlocks", True),
])
def test_provider_refusal_table_classifies_only_definite_rejections(code, refused):
    from botocore.exceptions import ClientError

    error = ClientError({"Error": {"Code": code, "Message": "x"}}, "RunInstances")
    assert MODULE.provider_refusal(error) == (code if refused else None)
    assert MODULE.provider_refusal(ConnectionError("not a client error")) is None


@pytest.mark.asyncio
async def test_refused_launch_fails_operation_once_and_leaves_box_releasable(controller_database, caplog):
    import logging

    ec2 = RefusingEC2("UnauthorizedOperation")
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    with caplog.at_level(logging.INFO, logger=MODULE.__name__):
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    assert len(ec2.calls) == 1
    operation, box, events = await read_operation(controller_database)
    assert operation.status == "failed"
    assert operation.error_class == "provider_refused:UnauthorizedOperation"
    assert operation.progress == "launch_intent"
    assert operation.lease_owner is None
    assert box.status == "requested" and box.desired_state == "running" and box.observed_state == "unknown"
    assert events == ["launch_refused"]
    line = next(record for record in caplog.records if "operation_id=operation" in record.getMessage())
    assert line.levelno == logging.WARNING
    assert "status=failed" in line.getMessage() and "provider_code=UnauthorizedOperation" in line.getMessage()
    # A failed operation is no longer claimable, so the controller never calls RunInstances for it again.
    assert not await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-two", launch)
    assert len(ec2.calls) == 1


@pytest.mark.asyncio
async def test_refusal_error_class_is_bounded_to_the_column_width(controller_database):
    ec2 = RefusingEC2("InvalidParameter" + "X" * 80)
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", MODULE.LaunchTemplate("lt-example", "4", "us-east-1"))
    operation, _, _ = await read_operation(controller_database)
    assert operation.status == "failed"
    assert len(operation.error_class) == 64 and operation.error_class.startswith("provider_refused:InvalidParameter")


@pytest.mark.asyncio
async def test_unconfirmed_launch_is_retried_with_the_same_client_token(controller_database):
    ec2 = UnreachableEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    operation, _, _ = await read_operation(controller_database)
    assert operation.status == "unknown" and operation.error_class == "provider_unknown" and operation.progress == "launch_intent"
    assert len(ec2.calls) == 1
    ec2.reachable = True
    ec2.visible = True
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-two", launch)
    assert len(ec2.calls) == 2
    assert ec2.calls[1]["ClientToken"] == ec2.calls[0]["ClientToken"] == "a" * 64
    assert ec2.calls[1]["LaunchTemplate"] == ec2.calls[0]["LaunchTemplate"]
    operation, box, events = await read_operation(controller_database)
    assert operation.status == "succeeded" and operation.provider_request_ref == "i-owned-example"
    assert box.observed_state == "running"
    assert events == []
    assert not await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-two", launch)
    assert len(ec2.calls) == 2


@pytest.mark.asyncio
async def test_launch_retry_bound_fails_operation_without_another_send(controller_database, monkeypatch):
    monkeypatch.setattr(MODULE, "DEV_BOX_LAUNCH_RETRY_BOUND", 2)
    ec2 = UnreachableEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1")
    for worker in ("tick-one", "tick-two"):
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", worker, launch)
        operation, _, _ = await read_operation(controller_database)
        assert operation.status == "unknown"
    assert len(ec2.calls) == 2
    assert {call["ClientToken"] for call in ec2.calls} == {"a" * 64}
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "tick-three", launch)
    assert len(ec2.calls) == 2
    operation, box, events = await read_operation(controller_database)
    assert operation.status == "failed" and operation.error_class == "launch_unconfirmed" and operation.retries == 3
    assert box.observed_state == "unknown" and box.desired_state == "running"
    assert events == ["launch_unconfirmed"]
    assert not await MODULE.reconcile_setup(controller_database, ec2, "operation", "tick-four", launch)


@pytest.mark.asyncio
async def test_launch_is_not_resent_outside_the_client_token_window(controller_database):
    from datetime import UTC, datetime, timedelta

    ec2 = UnreachableEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "tick-one", launch)
    assert len(ec2.calls) == 1
    async with controller_database() as session:
        operation = await session.get(DevBoxOperation, "operation")
        operation.created_at = datetime.now(UTC) - timedelta(hours=23, minutes=1)
        await session.commit()
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "tick-two", launch)
    assert len(ec2.calls) == 1
    operation, _, events = await read_operation(controller_database)
    assert operation.status == "failed" and operation.error_class == "launch_unconfirmed"
    assert events == ["launch_unconfirmed"]


@pytest.mark.asyncio
async def test_exhausted_launch_stays_unknown_while_inventory_cannot_be_read(controller_database, monkeypatch):
    monkeypatch.setattr(MODULE, "DEV_BOX_LAUNCH_RETRY_BOUND", 1)
    ec2 = UnreachableEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "tick-one", launch)

    def unreadable(**kwargs):
        raise ConnectionError("describe timed out")

    ec2.describe_instances = unreadable
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "tick-two", launch)
    operation, _, events = await read_operation(controller_database)
    assert operation.status == "unknown" and operation.error_class == "provider_unknown"
    assert len(ec2.calls) == 1 and events == []


class ReleaseEC2(RefusingEC2):
    """Refuses launches; answers inventory by ClientToken or InstanceIds; records terminations."""

    def __init__(self):
        super().__init__()
        self.stray = None
        self.inventory_error = None
        self.describes = []
        self.terminated = []

    def describe_instances(self, **kwargs):
        self.describes.append(kwargs)
        if self.inventory_error:
            raise self.inventory_error
        if "InstanceIds" in kwargs:
            matches = [self.stray] if self.stray and self.stray["InstanceId"] in kwargs["InstanceIds"] else []
        else:
            token = next(item["Values"][0] for item in kwargs["Filters"] if item["Name"] == "client-token")
            matches = [self.stray] if self.stray and self.stray["ClientToken"] == token else []
        return {"Reservations": [{"Instances": matches}]} if matches else {"Reservations": []}

    def terminate_instances(self, **kwargs):
        self.terminated.extend(kwargs["InstanceIds"])
        self.stray["State"] = {"Name": "terminated"}


async def admit_release(controller_database, retention="retain"):
    async with controller_database() as session:
        box = await session.get(DevBox, "box")
        box.desired_state = "released"
        session.add(DevBoxOperation(id="release", box_id="box", user_id="example-owner", org_id="example-org",
                                    deployment_id="example-deployment", machine_generation=1, action="release", status="pending",
                                    idempotency_key="release-key", request_digest="digest", actor_user_id="example-owner",
                                    provider_token="b" * 64, progress="admitted", retention_choice=retention))
        await session.commit()


async def read_release(controller_database):
    async with controller_database() as session:
        release = await session.get(DevBoxOperation, "release")
        launch = await session.get(DevBoxOperation, "operation")
        box = await session.get(DevBox, "box")
        instance = await session.scalar(select(DevBoxInstance))
        events = list(await session.scalars(select(DevBoxEvent.event_type).order_by(DevBoxEvent.created_at, DevBoxEvent.id)))
        return release, launch, box, instance, events


@pytest.mark.asyncio
async def test_release_completes_after_refused_launch_is_proven_absent_without_provider_mutation(controller_database):
    from controller import cleanup

    ec2 = ReleaseEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    operation, _, _ = await read_operation(controller_database)
    assert operation.status == "failed" and operation.provider_request_ref == "launch:" + "a" * 64
    await admit_release(controller_database)
    assert await cleanup.reconcile_release(controller_database, ec2, "release", "worker-two", launch)
    release, failed_launch, box, instance, events = await read_release(controller_database)
    assert release.status == "succeeded" and release.progress == "release_verified" and release.error_class is None
    assert box.status == "released" and box.observed_state == "released"
    assert failed_launch.status == "failed" and failed_launch.provider_request_ref == "launch-absent:" + "a" * 64
    assert instance.provider_instance_id is None and instance.observed_state == "absent" and instance.error_class == "launch_absent"
    assert set(events) >= {"launch_refused", "launch_absent", "release_verified"}
    assert len(ec2.calls) == 1 and ec2.terminated == []
    # The absent launch is settled once: a later release tick does not read inventory for it again.
    assert not await cleanup.reconcile_release(controller_database, ec2, "release", "worker-three", launch)


@pytest.mark.asyncio
async def test_release_retires_the_owners_executor_so_the_next_box_can_bind(controller_database):
    """#8223 cause 23: the executor of a released box fenced every enrollment of the owner's next box."""
    from controller import cleanup
    from src.shared.models.crew_executor import CrewExecutor

    ec2 = ReleaseEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    async with controller_database() as session:
        session.add(CrewExecutor(id="parked", tenant_id="example-org", user_id="example-owner", target_kind="ec2-instance",
                                 dev_box_id="box", generation=1, state="stopped", owner_key="k" * 52, target_ref="box",
                                 latest_verified_checkpoint_id="checkpoint-v1"))
        await session.commit()
    await admit_release(controller_database)
    assert await cleanup.reconcile_release(controller_database, ec2, "release", "worker-two", launch)
    release, _, box, _, events = await read_release(controller_database)
    assert release.status == "succeeded" and box.status == "released"
    assert "host_executor_retired" in events and events.index("host_executor_retired") < events.index("release_verified")
    async with controller_database() as session:
        parked = await session.get(CrewExecutor, "parked")
        assert parked.state == "deleted" and parked.target_ref is None and parked.lease_generation is None
        assert parked.latest_verified_checkpoint_id == "checkpoint-v1"


@pytest.mark.asyncio
async def test_release_adopts_and_terminates_a_stray_instance_found_by_the_launch_token(controller_database):
    from controller import cleanup

    ec2 = ReleaseEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    async with controller_database() as session:
        box = await session.get(DevBox, "box")
        operation = await session.get(DevBoxOperation, "operation")
        tags = MODULE.ownership_tags(box, operation)
    ec2.stray = {
        "InstanceId": "i-stray", "ClientToken": "a" * 64, "State": {"Name": "running"}, "PrivateIpAddress": "10.4.0.9",
        "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    }
    await admit_release(controller_database)
    assert await cleanup.reconcile_release(controller_database, ec2, "release", "worker-two", launch)
    release, failed_launch, box, instance, events = await read_release(controller_database)
    assert release.status == "succeeded" and box.status == "released"
    assert ec2.terminated == ["i-stray"]
    assert failed_launch.status == "failed" and failed_launch.provider_request_ref == "i-stray"
    assert instance.provider_instance_id == "i-stray" and instance.observed_state == "terminated"
    assert instance.availability_zone == "us-east-1a" and instance.provider_account_ref == "000000000000"
    assert set(events) >= {"launch_refused", "launch_adopted", "instance_terminated", "release_verified"}
    assert len(ec2.calls) == 1


@pytest.mark.parametrize("status", ["pending", "unknown", "succeeded"])
@pytest.mark.asyncio
async def test_release_keeps_an_unsettled_launch_unresolved_without_reading_inventory(controller_database, status):
    from controller import cleanup
    from sqlalchemy import text

    ec2 = ReleaseEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1")
    async with controller_database() as session:
        # Gateway admission never lets a release coexist with an open launch; drop the guard index here to
        # exercise the controller's own fence against releasing before the launch outcome is settled.
        await session.execute(text("DROP INDEX uq_dev_box_active_operation"))
        open_launch = await session.get(DevBoxOperation, "operation")
        open_launch.status = status
        open_launch.progress = "launch_intent"
        open_launch.provider_request_ref = "launch:" + "a" * 64
        await session.commit()
    await admit_release(controller_database)
    assert await cleanup.reconcile_release(controller_database, ec2, "release", "worker", launch)
    release, open_launch, box, _, events = await read_release(controller_database)
    assert release.status == "unknown" and release.error_class == "launch_inventory_unresolved"
    assert open_launch.status == status and open_launch.provider_request_ref == "launch:" + "a" * 64
    assert box.status == "requested" and events == []
    assert ec2.describes == [] and ec2.terminated == []


@pytest.mark.asyncio
async def test_release_stays_unresolved_while_launch_inventory_cannot_be_read(controller_database):
    from controller import cleanup

    ec2 = ReleaseEC2()
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    await admit_release(controller_database, retention="delete")
    ec2.inventory_error = ConnectionError("describe timed out")
    assert await cleanup.reconcile_release(controller_database, ec2, "release", "worker-two", launch)
    release, failed_launch, box, _, events = await read_release(controller_database)
    assert release.status == "unknown" and release.error_class == "launch_inventory_unresolved"
    assert box.status == "requested" and failed_launch.provider_request_ref == "launch:" + "a" * 64
    assert "launch_absent" not in events and ec2.terminated == []
    ec2.inventory_error = None
    assert await cleanup.reconcile_release(controller_database, ec2, "release", "worker-three", launch)
    release, failed_launch, box, _, events = await read_release(controller_database)
    assert release.status == "succeeded" and box.status == "released"
    assert failed_launch.provider_request_ref == "launch-absent:" + "a" * 64 and "launch_absent" in events


KMS_REASON = {"Code": "Client.InvalidKMSKey.InvalidState", "Message": "The KMS key provided is in an incorrect state"}


def launched_ec2(state, state_reason=None):
    ec2 = FakeEC2()
    ec2.lose_response = False
    ec2.visible = True
    ec2.state = state
    ec2.state_reason = state_reason
    return ec2


@pytest.mark.parametrize("state, state_reason, error_class", [
    ("terminated", KMS_REASON, "provider_refused:Client.InvalidKMSKey.InvalidState"),
    ("shutting-down", KMS_REASON, "provider_refused:Client.InvalidKMSKey.InvalidState"),
    ("terminated", None, "instance_terminated_at_launch"),
    ("stopped", {"Code": "Server.InternalError", "Message": "internal error"}, "provider_refused:Server.InternalError"),
])
@pytest.mark.asyncio
async def test_instance_ended_by_ec2_at_launch_fails_with_its_state_reason(controller_database, caplog, state, state_reason, error_class):
    import logging

    ec2 = launched_ec2(state, state_reason)
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    with caplog.at_level(logging.INFO, logger=MODULE.__name__):
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    operation, box, events = await read_operation(controller_database)
    async with controller_database() as session:
        instance = await session.scalar(select(DevBoxInstance))
    assert operation.status == "failed" and operation.error_class == error_class
    assert operation.progress == "instance_observed" and operation.provider_request_ref == "i-owned-example"
    assert instance.provider_instance_id == "i-owned-example" and instance.observed_state == state
    assert instance.error_class == error_class and instance.readiness_class is None
    assert box.status == "requested" and box.observed_state == state and box.desired_state == "running"
    assert events == ["launch_terminated"]
    line = next(record for record in caplog.records if "operation_id=operation" in record.getMessage())
    assert line.levelno == logging.WARNING and "status=failed" in line.getMessage()
    if state_reason:
        assert f"provider_code={state_reason['Code']}" in line.getMessage() and state_reason["Message"] in line.getMessage()
    # A failed operation is no longer claimable and the machine is never launched again.
    assert not await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-two", launch)
    assert len(ec2.calls) == 1


@pytest.mark.asyncio
async def test_pending_instance_keeps_waiting_until_it_runs(controller_database):
    ec2 = launched_ec2("pending")
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    operation, box, events = await read_operation(controller_database)
    async with controller_database() as session:
        instance = await session.scalar(select(DevBoxInstance))
    assert operation.status == "pending" and operation.progress == "instance_observed" and operation.error_class is None
    assert instance.provider_instance_id == "i-owned-example" and instance.observed_state == "pending"
    assert box.observed_state == "pending" and events == []
    ec2.instance["State"] = {"Name": "running"}
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-two", launch)
    operation, box, events = await read_operation(controller_database)
    assert operation.status == "succeeded" and box.status == "running" and box.observed_state == "running"
    assert len(ec2.calls) == 1 and events == []


@pytest.mark.asyncio
async def test_owner_initiated_stop_right_after_launch_is_not_a_launch_failure(controller_database):
    ec2 = launched_ec2("stopped", {"Code": MODULE.USER_INITIATED_SHUTDOWN, "Message": "Client.UserInitiatedShutdown"})
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    operation, box, events = await read_operation(controller_database)
    assert operation.status == "pending" and operation.error_class is None and events == []
    assert box.observed_state == "stopped"


@pytest.mark.asyncio
async def test_release_after_ec2_terminated_the_launch_skips_the_already_terminated_machine(controller_database):
    from controller import cleanup

    ec2 = launched_ec2("terminated", KMS_REASON)
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    operation, _, _ = await read_operation(controller_database)
    assert operation.status == "failed" and operation.provider_request_ref == "i-owned-example"
    await admit_release(controller_database)
    assert await cleanup.reconcile_release(controller_database, ec2, "release", "worker-two", launch)
    release, failed_launch, box, instance, events = await read_release(controller_database)
    assert release.status == "succeeded" and release.progress == "release_verified"
    assert box.status == "released" and box.observed_state == "released"
    assert ec2.terminated == [] and len(ec2.calls) == 1
    assert instance.provider_instance_id == "i-owned-example" and instance.observed_state == "terminated"
    assert failed_launch.provider_request_ref == "i-owned-example"
    assert events == ["launch_terminated", "instance_terminated", "release_verified"]
    # A machine EC2 has already forgotten is handled the same way.
    async with controller_database() as session:
        await session.execute(DevBoxOperation.__table__.update().where(DevBoxOperation.id == "release").values(status="pending", progress="admitted"))
        box = await session.get(DevBox, "box")
        box.status = "requested"
        box.observed_state = "terminated"
        await session.commit()
    ec2.instance = None
    assert await cleanup.reconcile_release(controller_database, ec2, "release", "worker-three", launch)
    release, _, box, _, _ = await read_release(controller_database)
    assert release.status == "succeeded" and box.status == "released" and ec2.terminated == []


@pytest.mark.asyncio
async def test_lease_renewal_fences_other_workers_and_old_epochs(controller_database):
    from controller.fencing import claim, renew

    lease = await claim(controller_database, "operation", "worker-one", ("setup",), lease_seconds=1)
    assert lease is not None
    async with controller_database() as session:
        before = (await session.get(DevBoxOperation, "operation")).lease_expires_at
    assert await renew(controller_database, "operation", "worker-one", lease, lease_seconds=60)
    assert not await renew(controller_database, "operation", "worker-one", lease - 1, lease_seconds=60)
    assert await claim(controller_database, "operation", "worker-two", ("setup",)) is None
    async with controller_database() as session:
        operation = await session.get(DevBoxOperation, "operation")
        assert operation.lease_expires_at > before
        assert operation.lease_owner == "worker-one"


@pytest.mark.asyncio
async def test_owner_revocation_blocks_delayed_setup_before_ec2(controller_database):
    from datetime import UTC, datetime

    async with controller_database() as session:
        membership = await session.scalar(select(TenantMembership))
        membership.revoked_at = datetime.now(UTC)
        await session.commit()
    ec2 = FakeEC2()
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", MODULE.LaunchTemplate("lt-example", "4", "us-east-1"))
    assert ec2.calls == []
    async with controller_database() as session:
        operation = await session.get(DevBoxOperation, "operation")
        assert operation.error_class == "owner_membership_revoked"
        assert operation.status == "unknown"


@pytest.mark.asyncio
async def test_restart_refuses_overcommitted_running_capacity_before_provider_call(controller_database):
    ec2 = FakeEC2()
    async with controller_database() as session:
        policy = await session.get(DevBoxPolicy, "policy")
        policy.max_running_boxes = 0
        await session.commit()
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "restarted-worker", MODULE.LaunchTemplate("lt-example", "4", "us-east-1"))
    assert ec2.calls == []
    async with controller_database() as session:
        operation = await session.get(DevBoxOperation, "operation")
        assert operation.error_class == "quota_changed"
        assert operation.status == "unknown"


@pytest.mark.parametrize("address, expected", [
    ("10.4.0.3", "10.4.0.3"), ("172.16.1.2", "172.16.1.2"),
    ("192.168.1.2", "192.168.1.2"), ("203.0.113.3", None),
    ("169.254.169.254", None), ("0.0.0.0", None), (None, None), ("not-an-ip", None),
])
def test_observed_private_address_never_admits_public_or_missing_address(address, expected):
    assert MODULE.private_host_address({"PrivateIpAddress": address}) == expected
    assert MODULE.private_host_address({}) is None


@pytest.mark.asyncio
async def test_wake_rebinds_private_address_and_stop_removes_old_route(controller_database, monkeypatch):
    from controller import state

    async def mounted_workspace(*args, **kwargs):
        return None

    monkeypatch.setattr(state, "workspace", mounted_workspace)

    class OwnedEC2:
        address = "10.4.0.3"
        observed_state = "running"

        def describe_instances(self, *, InstanceIds):
            assert InstanceIds == ["i-owned-example"]
            return {"Reservations": [{"Instances": [{
                "InstanceId": "i-owned-example",
                "PrivateIpAddress": self.address,
                "State": {"Name": self.observed_state},
                "Tags": [{"Key": key, "Value": value} for key, value in self.tags.items()],
            }]}]}

    provider = OwnedEC2()
    async with controller_database() as session:
        box = await session.get(DevBox, "box")
        operation = await session.get(DevBoxOperation, "operation")
        operation.action = "start"
        box.status = "stopped"
        box.observed_state = "stopped"
        provider.tags = MODULE.ownership_tags(box, operation)
        session.add(DevBoxInstance(box_id=box.id, user_id=box.user_id, org_id=box.org_id,
                                   deployment_id=box.deployment_id, machine_generation=1,
                                   template_revision="r1", provider_instance_id="i-owned-example",
                                   observed_state="stopped", private_address="10.4.0.2",
                                   enrollment_public_key=b"stale-key", certificate_fingerprint="a" * 64))
        await session.commit()

    assert await state.reconcile_state(controller_database, provider, "operation", "worker")
    async with controller_database() as session:
        instance = await session.scalar(select(DevBoxInstance))
        assert instance.private_address == "10.4.0.3"
        assert instance.readiness_class == "machine_running"
        # The host's /run/adp (tmpfs) is empty after a start: both the enrollment key and the
        # certificate pin must be forgotten so the controller re-provisions them (#8223).
        assert instance.enrollment_public_key is None
        assert instance.certificate_fingerprint is None
        operation = await session.get(DevBoxOperation, "operation")
        operation.action = "stop"
        operation.status = "pending"
        operation.progress = "admitted"
        box = await session.get(DevBox, "box")
        box.desired_state = "stopped"
        await session.commit()

    provider.observed_state = "stopped"
    assert await state.reconcile_state(controller_database, provider, "operation", "worker")
    async with controller_database() as session:
        instance = await session.scalar(select(DevBoxInstance))
        assert instance.private_address is None
        assert instance.readiness_class is None
        operation = await session.get(DevBoxOperation, "operation")
        assert operation.status == "succeeded"


def test_controller_carries_only_approved_image_profile_revision():
    import hashlib
    import json

    from controller.worker import validate_release

    manifest = {"component": "dev-box", "deployment_id": "example-deployment",
                "account_id": "000000000000",
                "image_id": "ami-example", "source_revision": "a" * 40,
                "template_revision": "r1", "dashboard_enabled": True,
                "crew_profile_revision": "crew-r1", "region": "us-east-1",
                "instance_size": "t3.small", "workspace_disk_gb": 40,
                "kms_key_arn": "arn:example"}
    outputs = {field: {"value": value} for field, value in {
        "image_id": "ami-example", "source_revision": "a" * 40,
        "template_revision": "r1", "launch_template_id": "lt-example",
        "launch_template_version": 4,
    }.items()}
    def seal():
        manifest["manifest_sha256"] = hashlib.sha256(json.dumps({
            field: value for field, value in manifest.items() if field != "manifest_sha256"
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    seal()
    assert validate_release(manifest, outputs, "example-deployment").profile_revision == "crew-r1"
    assert validate_release(manifest, outputs, "example-deployment").account_id == "000000000000"
    manifest["gateway_execute_arn"] = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
    seal()
    with pytest.raises(ValueError, match="gateway bootstrap is not pinned"):
        validate_release(manifest, outputs, "example-deployment")
    manifest["crew_model_id"] = "us.anthropic.example-v1:0"
    seal()
    assert validate_release(manifest, outputs, "example-deployment").crew_model_id == manifest["crew_model_id"]
    manifest.pop("gateway_execute_arn")
    manifest.pop("crew_model_id")
    manifest.pop("crew_profile_revision")
    seal()
    with pytest.raises(ValueError, match="profile is not pinned"):
        validate_release(manifest, outputs, "example-deployment")


def test_launch_tags_instance_volume_and_primary_interface(tmp_path):
    """The controller policy requires ownership request tags on instance, volume and ENI."""
    template = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", instance_size="t3.small")
    arguments = MODULE.run_instances_arguments(template, "a" * 64, [{"Key": "adp:box-id", "Value": "box"}], ("t3.small", None, None))
    assert [item["ResourceType"] for item in arguments["TagSpecifications"]] == ["instance", "volume", "network-interface"]
    assert "InstanceType" not in arguments and "NetworkInterfaces" not in arguments


# --- Capacity fallback (#8423) -----------------------------------------------------------------

PLAN = MODULE.LaunchTemplate(
    "lt-example", "4", "us-east-1", account_id="000000000000", instance_size="t3.small",
    fallback_sizes=("t3.small", "t3a.small"), subnets=(("subnet-a", "us-east-1a"), ("subnet-b", "us-east-1b")),
    security_group_id="sg-guest", instance_hour_rates_usd={"t3.small": "0.10", "t3a.small": "0.09"},
)


def attempt_of(call):
    """(InstanceType override, SubnetId override) of one recorded RunInstances call; None = the template's own."""
    interfaces = call.get("NetworkInterfaces")
    return call.get("InstanceType"), interfaces[0]["SubnetId"] if interfaces else None


class CapacityEC2(FakeEC2):
    """RunInstances refuses the listed (type, subnet) attempts with InsufficientInstanceCapacity; any other launches."""

    def __init__(self, refuse, code="InsufficientInstanceCapacity"):
        super().__init__()
        self.refuse = set(refuse)
        self.code = code
        self.lose_response = False

    def run_instances(self, **kwargs):
        from botocore.exceptions import ClientError

        if attempt_of(kwargs) in self.refuse:
            self.calls.append(kwargs)
            raise ClientError({"Error": {"Code": self.code, "Message": "no capacity"}}, "RunInstances")
        self.visible = True
        return super().run_instances(**kwargs)


def test_launch_attempts_vary_size_within_a_subnet_before_the_next_subnet_and_honour_the_disk_zone():
    assert MODULE.launch_attempts(PLAN) == [
        ("t3.small", "subnet-a", "us-east-1a"), ("t3a.small", "subnet-a", "us-east-1a"),
        ("t3.small", "subnet-b", "us-east-1b"), ("t3a.small", "subnet-b", "us-east-1b"),
    ]
    # A retained workspace disk pins its zone: a replace never leaves it.
    assert MODULE.launch_attempts(PLAN, zone="us-east-1b") == [("t3.small", "subnet-b", "us-east-1b"), ("t3a.small", "subnet-b", "us-east-1b")]
    assert MODULE.launch_attempts(PLAN, zone="us-east-1c") == []
    # A release without a plan is exactly the template's own launch, whatever the zone.
    bare = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", instance_size="t3.small")
    assert MODULE.launch_attempts(bare) == [("t3.small", None, None)]
    assert MODULE.launch_attempts(bare, zone="us-east-1a") == [("t3.small", None, None)]


def test_attempt_tokens_are_distinct_and_derived_from_the_operation_token():
    base = "a" * 64
    tokens = [MODULE.attempt_token(base, index) for index in range(4)]
    assert tokens[0] == base and len(set(tokens)) == 4 and all(len(token) == 64 for token in tokens)
    reference = type("Operation", (), {"provider_token": base, "provider_request_ref": f"launch:{base}#2"})()
    assert MODULE.launch_reference(reference) == (base, 2)
    reference.provider_request_ref = "i-observed"
    assert MODULE.launch_reference(reference) == (base, 0)


def test_run_instances_arguments_override_only_what_differs_from_the_template():
    tag_list = [{"Key": "adp:box-id", "Value": "box"}]
    first = MODULE.run_instances_arguments(PLAN, "t" * 64, tag_list, ("t3.small", "subnet-a", "us-east-1a"))
    assert "InstanceType" not in first and "NetworkInterfaces" not in first and first["ClientToken"] == "t" * 64
    other_size = MODULE.run_instances_arguments(PLAN, "t" * 64, tag_list, ("t3a.small", "subnet-a", "us-east-1a"))
    assert other_size["InstanceType"] == "t3a.small" and "NetworkInterfaces" not in other_size
    other_subnet = MODULE.run_instances_arguments(PLAN, "t" * 64, tag_list, ("t3.small", "subnet-b", "us-east-1b"))
    assert other_subnet["NetworkInterfaces"] == [{"DeviceIndex": 0, "SubnetId": "subnet-b", "Groups": ["sg-guest"], "AssociatePublicIpAddress": False}]
    assert "InstanceType" not in other_subnet


@pytest.mark.asyncio
async def test_capacity_refusal_falls_back_to_the_next_admitted_size_with_a_fresh_token(controller_database, caplog):
    import logging

    ec2 = CapacityEC2(refuse={(None, None)})
    with caplog.at_level(logging.INFO, logger=MODULE.__name__):
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", PLAN)
    assert [attempt_of(call) for call in ec2.calls] == [(None, None), ("t3a.small", None)]
    base = "a" * 64
    assert ec2.calls[0]["ClientToken"] == base
    assert ec2.calls[1]["ClientToken"] == MODULE.attempt_token(base, 1) != base
    operation, box, events = await read_operation(controller_database)
    assert operation.status == "succeeded" and operation.error_class is None
    assert operation.provider_token == MODULE.attempt_token(base, 1)
    assert operation.provider_request_ref == "i-owned-example"
    assert box.status == "running"
    assert events == ["launch_capacity_fallback"]
    async with controller_database() as session:
        instance = await session.scalar(select(DevBoxInstance))
        assert instance.instance_size == "t3a.small" and instance.provider_instance_id == "i-owned-example"
    line = next(record for record in caplog.records if "operation_id=operation" in record.getMessage())
    assert "status=succeeded" in line.getMessage() and "launched=True" in line.getMessage()


@pytest.mark.asyncio
async def test_fresh_box_falls_back_to_the_next_approved_subnet_once_every_size_in_the_first_is_refused(controller_database):
    ec2 = CapacityEC2(refuse={(None, None), ("t3a.small", None)})
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", PLAN)
    assert [attempt_of(call) for call in ec2.calls] == [(None, None), ("t3a.small", None), (None, "subnet-b")]
    assert ec2.calls[2]["NetworkInterfaces"][0]["Groups"] == ["sg-guest"]
    assert len({call["ClientToken"] for call in ec2.calls}) == 3
    operation, _, events = await read_operation(controller_database)
    assert operation.status == "succeeded"
    assert events == ["launch_capacity_fallback", "launch_capacity_fallback"]
    async with controller_database() as session:
        instance = await session.scalar(select(DevBoxInstance))
        assert instance.instance_size == "t3.small"


@pytest.mark.asyncio
async def test_exhausted_plan_fails_once_naming_every_refused_attempt(controller_database, caplog):
    import logging

    ec2 = CapacityEC2(refuse={(None, None), ("t3a.small", None), (None, "subnet-b"), ("t3a.small", "subnet-b")})
    with caplog.at_level(logging.INFO, logger=MODULE.__name__):
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", PLAN)
    assert len(ec2.calls) == 4 and len({call["ClientToken"] for call in ec2.calls}) == 4
    operation, box, events = await read_operation(controller_database)
    assert operation.status == "failed"
    assert operation.error_class == "provider_refused:InsufficientInstanceCapacity"
    assert box.status == "requested" and box.observed_state == "unknown"
    assert events == ["launch_capacity_fallback"] * 3 + ["launch_refused"]
    line = next(record for record in caplog.records if "status=failed" in record.getMessage())
    message = line.getMessage()
    assert "EC2 refused every approved launch" in message and "t3a.small in us-east-1b (InsufficientInstanceCapacity)" in message
    # Terminal: never claimed or sent again.
    assert not await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-two", PLAN)
    assert len(ec2.calls) == 4


@pytest.mark.asyncio
async def test_other_refusals_do_not_fall_back(controller_database):
    ec2 = CapacityEC2(refuse={(None, None)}, code="UnauthorizedOperation")
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", PLAN)
    assert len(ec2.calls) == 1
    operation, _, events = await read_operation(controller_database)
    assert operation.status == "failed" and operation.error_class == "provider_refused:UnauthorizedOperation"
    assert events == ["launch_refused"]


@pytest.mark.asyncio
async def test_a_lost_response_after_a_fallback_is_re_sent_with_the_rotated_token_only(controller_database):
    """#8037 invariant after #8423: the attempt that may have reached EC2 is re-sent with ITS token, never a new one."""

    class LosingAfterFallback(CapacityEC2):
        def run_instances(self, **kwargs):
            if attempt_of(kwargs) == ("t3a.small", None) and len([call for call in self.calls if attempt_of(call) == ("t3a.small", None)]) == 0:
                self.calls.append(kwargs)
                FakeEC2.run_instances(self, **{**kwargs})  # EC2 accepted it; the response is lost
                raise ConnectionError("the provider response was lost")
            return super().run_instances(**kwargs)

    ec2 = LosingAfterFallback(refuse={(None, None)})
    ec2.lose_response = False
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", PLAN)
    operation, _, events = await read_operation(controller_database)
    base = "a" * 64
    assert operation.status == "unknown" and operation.error_class == "provider_unknown"
    assert operation.provider_token == MODULE.attempt_token(base, 1) and operation.provider_request_ref == f"launch:{base}#1"
    assert events == ["launch_capacity_fallback"]
    # Next tick: the same rotated token goes out again; EC2 de-duplicates and the instance is observed.
    ec2.visible = True
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", PLAN)
    assert [attempt_of(call) for call in ec2.calls] == [(None, None), ("t3a.small", None), ("t3a.small", None)]
    assert ec2.calls[1]["ClientToken"] == ec2.calls[2]["ClientToken"] == MODULE.attempt_token(base, 1)
    operation, _, _ = await read_operation(controller_database)
    assert operation.status == "succeeded"


def test_instance_rate_prices_the_launched_size_and_falls_back_to_the_policy_rate():
    from decimal import Decimal
    from types import SimpleNamespace

    from controller.usage import instance_rate

    policy = SimpleNamespace(instance_hour_rate_usd=Decimal("0.10"))
    assert instance_rate(policy, SimpleNamespace(instance_size="t3a.small"), PLAN) == Decimal("0.09")
    assert instance_rate(policy, SimpleNamespace(instance_size="t3.small"), PLAN) == Decimal("0.10")
    assert instance_rate(policy, SimpleNamespace(instance_size="t3.large"), PLAN) == Decimal("0.10")
    assert instance_rate(policy, SimpleNamespace(instance_size=None), PLAN) == Decimal("0.10")
    bare = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", instance_size="t3.small")
    assert instance_rate(policy, SimpleNamespace(instance_size="t3a.small"), bare) == Decimal("0.10")


def test_controller_release_carries_the_launch_plan_and_refuses_a_plan_that_disagrees_with_the_template():
    import hashlib
    import json

    from controller.worker import validate_release

    manifest = {"component": "dev-box", "deployment_id": "example-deployment", "account_id": "000000000000",
                "image_id": "ami-example", "source_revision": "a" * 40, "template_revision": "r1", "dashboard_enabled": False,
                "region": "us-east-1", "instance_size": "t3.small", "workspace_disk_gb": 40, "kms_key_arn": "arn:example",
                "allowed_sizes": ["t3.small", "t3a.small"], "private_subnet_id": "subnet-a",
                "fallback_sizes": ["t3.small", "t3a.small"], "instance_hour_rates_usd": {"t3.small": "0.10", "t3a.small": "0.09"},
                "private_subnets": [{"subnet_id": "subnet-a", "availability_zone": "us-east-1a"}, {"subnet_id": "subnet-b", "availability_zone": "us-east-1b"}]}
    outputs = {field: {"value": value} for field, value in {
        "image_id": "ami-example", "source_revision": "a" * 40, "template_revision": "r1",
        "launch_template_id": "lt-example", "launch_template_version": 4, "guest_security_group_id": "sg-guest",
    }.items()}

    def seal():
        manifest["manifest_sha256"] = hashlib.sha256(json.dumps({
            field: value for field, value in manifest.items() if field != "manifest_sha256"
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    seal()
    template = validate_release(manifest, outputs, "example-deployment")
    assert template.fallback_sizes == ("t3.small", "t3a.small")
    assert template.subnets == (("subnet-a", "us-east-1a"), ("subnet-b", "us-east-1b"))
    assert template.security_group_id == "sg-guest" and template.instance_hour_rates_usd == {"t3.small": "0.10", "t3a.small": "0.09"}
    for change in (
        {"fallback_sizes": ["t3a.small", "t3.small"]},
        {"fallback_sizes": ["t3.small", "t3.large"]},
        {"private_subnets": [{"subnet_id": "subnet-b", "availability_zone": "us-east-1b"}]},
        {"instance_hour_rates_usd": {"t3.small": "0.10"}},
    ):
        tampered = dict(manifest)
        tampered.update(change)
        tampered.pop("manifest_sha256")
        tampered["manifest_sha256"] = hashlib.sha256(json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with pytest.raises(ValueError, match="launch plan"):
            validate_release(tampered, outputs, "example-deployment")
    # A subnet fallback without the guest group has nothing to put on the overriding interface.
    outputs.pop("guest_security_group_id")
    with pytest.raises(ValueError, match="guest security group"):
        validate_release(manifest, outputs, "example-deployment")


@pytest.mark.asyncio
async def test_a_launch_records_the_release_its_generation_was_provisioned_from(controller_database):
    """#8223 cause 34: readiness after a stop/start verifies the box against THESE pins, never
    against whatever release is current by then."""
    ec2 = FakeEC2()
    launch = MODULE.LaunchTemplate("lt-example", "9", "us-east-1", account_id="000000000000", image_id="ami-example",
                                   revision="r1", profile_revision="crew-r1", source_revision="a" * 40,
                                   crew_bootstrap="user-data", crew_bundle_sha256="c" * 64, crew_source_revision="b" * 40)
    assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "first-worker", launch)
    async with controller_database() as session:
        instance = await session.scalar(select(DevBoxInstance))
        assert instance.profile_revision == "crew-r1"
        assert instance.source_revision == "b" * 40, "user-data mode pins the bundle's own source commit"
        assert instance.crew_bundle_sha256 == "c" * 64
        assert instance.launch_template_version == "9"
    pins = MODULE.release_pins(launch)
    assert (pins.profile_revision, pins.source_revision, pins.crew_bundle_sha256, pins.crew_bootstrap) == (
        "crew-r1", "b" * 40, "c" * 64, "user-data")
    image_mode = MODULE.release_pins(MODULE.LaunchTemplate("lt-example", "9", "us-east-1", profile_revision="crew-r1",
                                                           source_revision="a" * 40))
    assert (image_mode.source_revision, image_mode.crew_bundle_sha256, image_mode.crew_bootstrap) == ("a" * 40, None, "image")



@pytest.mark.parametrize("code, retryable", [
    ("RequestLimitExceeded", True), ("Throttling", True), ("ThrottlingException", True), ("InternalError", True),
    ("ServiceUnavailable", True), ("RequestExpired", True),
    ("UnauthorizedOperation", False), ("InsufficientFreeAddressesInSubnet", False), ("IdempotentParameterMismatch", False), ("", False),
])
def test_provider_retryable_table_names_throttles_and_service_faults_only(code, retryable):
    from botocore.exceptions import ClientError
    error = ClientError({"Error": {"Code": code, "Message": "x"}}, "RunInstances")
    assert MODULE.provider_retryable(error) == (code if retryable else None)
    assert MODULE.provider_retryable(ConnectionError("transport")) is None
    assert MODULE.provider_error_detail(error) == (code or "ClientError", "x")
    assert MODULE.provider_error_detail(ConnectionError("transport")) == ("ConnectionError", "transport")


@pytest.mark.asyncio
async def test_full_subnet_fails_a_fresh_launch_fast_and_the_log_names_the_code_and_message(controller_database, caplog):
    """#8223 causes 52/53: InsufficientFreeAddressesInSubnet was read as provider_unknown, retried 13 times with the
    code and message dropped, and ended launch_unconfirmed. It is a definite refusal: one send, a failed operation
    naming the code, and the provider's words in the log."""
    import logging
    ec2 = RefusingEC2("InsufficientFreeAddressesInSubnet")
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    with caplog.at_level(logging.INFO, logger=MODULE.__name__):
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker-one", launch)
    assert len(ec2.calls) == 1
    operation, box, events = await read_operation(controller_database)
    assert operation.status == "failed" and operation.error_class == "provider_refused:InsufficientFreeAddressesInSubnet"
    assert events == ["launch_refused"]
    # A fresh box (setup) has no previous machine to retreat to; it stays releasable as before.
    assert box.status == "requested" and box.machine_generation == 1
    line = next(r.getMessage() for r in caplog.records if "operation_id=operation" in r.getMessage() and "status=failed" in r.getMessage())
    assert "provider_code=InsufficientFreeAddressesInSubnet" in line and "provider_message='refused'" in line


class ThrottlingEC2(FakeEC2):
    """RunInstances is throttled `throttles` times (never processed), then launches normally."""
    def __init__(self, throttles):
        super().__init__()
        self.throttles = throttles
        self.lose_response = False

    def run_instances(self, **kwargs):
        from botocore.exceptions import ClientError
        if self.throttles > 0:
            self.throttles -= 1
            self.calls.append(kwargs)
            raise ClientError({"Error": {"Code": "RequestLimitExceeded", "Message": "Request limit exceeded."}}, "RunInstances")
        self.visible = True
        return super().run_instances(**kwargs)


@pytest.mark.asyncio
async def test_throttled_launch_backs_off_keeps_its_token_and_gets_a_longer_budget(controller_database, caplog, monkeypatch):
    """#8223 cause 52: a throttled send never reached EC2, so it is re-sent with the same ClientToken after a held
    lease (backoff) and under the longer throttle bound, with the code in the log; a lost response keeps the
    shorter #8037 bound."""
    import logging
    from datetime import UTC, datetime
    monkeypatch.setattr(MODULE, "DEV_BOX_LAUNCH_RETRY_BOUND", 2)
    monkeypatch.setattr(MODULE, "DEV_BOX_LAUNCH_THROTTLE_RETRY_BOUND", 6)
    ec2 = ThrottlingEC2(throttles=4)
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    with caplog.at_level(logging.INFO, logger=MODULE.__name__):
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker", launch)
    operation, _, events = await read_operation(controller_database)
    assert operation.status == "unknown" and operation.error_class == "provider_throttled" and operation.progress == "launch_intent"
    assert operation.lease_owner is None
    held_until = operation.lease_expires_at.replace(tzinfo=UTC) if operation.lease_expires_at else None
    assert held_until is not None and held_until > datetime.now(UTC), "the lease is held as backoff"
    line = next(r.getMessage() for r in caplog.records if "operation_id=operation" in r.getMessage())
    assert "provider_code=RequestLimitExceeded" in line and "Request limit exceeded" in line
    assert events == []
    # Past the lost-response bound (2) but inside the throttle bound (6): still re-sent, same token.
    async with controller_database() as session:
        operation = await session.get(DevBoxOperation, "operation")
        operation.lease_expires_at = None
        await session.commit()
    for _ in range(4):
        async with controller_database() as session:
            op = await session.get(DevBoxOperation, "operation")
            op.lease_expires_at = None
            await session.commit()
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker", launch)
    assert len(ec2.calls) == 5 and {call["ClientToken"] for call in ec2.calls} == {"a" * 64}
    operation, box, _ = await read_operation(controller_database)
    assert operation.status == "succeeded" and box.observed_state == "running"


@pytest.mark.asyncio
async def test_unknown_provider_answer_stays_unconfirmed_but_is_logged_with_its_code(controller_database, caplog):
    import logging
    ec2 = RefusingEC2("IdempotentParameterMismatch")
    launch = MODULE.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000")
    with caplog.at_level(logging.INFO, logger=MODULE.__name__):
        assert await MODULE.reconcile_setup(controller_database, ec2, "operation", "worker", launch)
    operation, _, _ = await read_operation(controller_database)
    assert operation.status == "unknown" and operation.error_class == "provider_unknown"
    assert operation.lease_expires_at is None, "no backoff: this answer is uncertain, not a throttle"
    line = next(r.getMessage() for r in caplog.records if "operation_id=operation" in r.getMessage())
    assert "provider_code=IdempotentParameterMismatch" in line and "provider_message='refused'" in line
