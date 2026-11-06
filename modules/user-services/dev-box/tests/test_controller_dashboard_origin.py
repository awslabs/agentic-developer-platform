"""Readiness never configures a loopback Crew dashboard behind the edge (#8223 cause 20).

The controller passes ``CREW_DASHBOARD_ORIGIN`` to the host's ``--configure``; it reads that
value from the shared gateway ConfigMap, which until this change carried no such key (the
origin was rendered only into the gateway Deployment env). A fresh box on dev 2026-10-20 was
therefore configured with ``dashboard.url = http://127.0.0.1:5476`` and the browser got Kiro
Crew's "Host header not allowed". Proven here: with the dashboard feature on and no valid
origin, the readiness tick refuses by name, records one ``crew_dashboard_origin_missing``
event on the generation's operation (not one per tick) and sends no host command; with the
feature off, the tick proceeds past the guard without an event.
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
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.dev_box import (
    DevBox,
    DevBoxAccessSession,
    DevBoxDevice,
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxPolicy,
    DevBoxResource,
    DevBoxTemplate,
)
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import identity  # noqa: E402
from controller.identity import (  # noqa: E402
    CREW_DASHBOARD_ORIGIN_MISSING,
    crew_dashboard_feature_enabled,
    dashboard_origin_refusal,
    reconcile_host_readiness,
)
from controller.launch import LaunchTemplate  # noqa: E402

ORIGIN = "https://d1example.cloudfront.net"


def test_the_feature_switch_accepts_only_the_literal_true():
    assert crew_dashboard_feature_enabled({"FEATURE_CREW_DASHBOARD": "true"})
    for off in (
        {},
        {"FEATURE_CREW_DASHBOARD": ""},
        {"FEATURE_CREW_DASHBOARD": "false"},
        {"FEATURE_CREW_DASHBOARD": "True"},
        {"FEATURE_CREW_DASHBOARD": "1"},
    ):
        assert not crew_dashboard_feature_enabled(off)


def test_refusal_names_a_missing_or_malformed_origin_only_while_the_dashboard_is_on():
    assert (
        dashboard_origin_refusal({"FEATURE_CREW_DASHBOARD": "true"})
        == CREW_DASHBOARD_ORIGIN_MISSING
    )
    assert (
        dashboard_origin_refusal(
            {
                "FEATURE_CREW_DASHBOARD": "true",
                "CREW_DASHBOARD_ORIGIN": "http://127.0.0.1:5476",
            }
        )
        == CREW_DASHBOARD_ORIGIN_MISSING
    )
    assert (
        dashboard_origin_refusal(
            {"FEATURE_CREW_DASHBOARD": "true", "CREW_DASHBOARD_ORIGIN": ORIGIN}
        )
        is None
    )
    assert dashboard_origin_refusal({"FEATURE_CREW_DASHBOARD": "false"}) is None
    assert dashboard_origin_refusal({}) is None
    assert len(CREW_DASHBOARD_ORIGIN_MISSING) <= 32  # DevBoxEvent.event_type width


@pytest_asyncio.fixture
async def origin_db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [
        Organization.__table__,
        User.__table__,
        TenantMembership.__table__,
        DevBoxPolicy.__table__,
        DevBoxTemplate.__table__,
        DevBox.__table__,
        DevBoxInstance.__table__,
        DevBoxOperation.__table__,
        DevBoxEvent.__table__,
        DevBoxAccessSession.__table__,
        DevBoxDevice.__table__,
        DevBoxResource.__table__,
        CrewExecutor.__table__,
        CrewCheckpoint.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Organization(id="org", name="Org"),
                User(
                    id="owner",
                    org_id="org",
                    team_id="team",
                    email="owner@example.test",
                    cognito_sub="sub",
                ),
            ]
        )
        await session.flush()
        session.add(TenantMembership(user_id="owner", tenant_id="org", revoked_at=None))
        session.add(
            DevBoxPolicy(
                id="policy",
                org_id="org",
                deployment_id="dep",
                enabled=True,
                max_live_boxes=1,
                max_running_boxes=1,
                allowed_sizes=["t3.small"],
                max_disk_gb=50,
                max_lease_minutes=60,
                max_storage_gb=100,
                max_running_hours=100,
                instance_hour_rate_usd=0.1,
                disk_gb_hour_rate_usd=0.0001,
                snapshot_gb_hour_rate_usd=0.0001,
                budget_usd=50,
            )
        )
        session.add(
            DevBoxTemplate(
                id="template",
                name="crew",
                revision="r1",
                profile="crew",
                instance_size="t3.small",
                disk_gb=40,
                image_ref="ami",
                approved=True,
            )
        )
        await session.flush()
        session.add(
            DevBox(
                id="box",
                user_id="owner",
                org_id="org",
                deployment_id="dep",
                template_id="template",
                template_revision="r1",
                profile="crew",
                machine_generation=3,
                status="running",
                desired_state="running",
                observed_state="running",
                auth_readiness="pending",
            )
        )
        await session.flush()
        session.add(
            DevBoxInstance(
                id="inst",
                box_id="box",
                user_id="owner",
                org_id="org",
                deployment_id="dep",
                machine_generation=3,
                template_revision="r1",
                provider_instance_id="i-owned",
                observed_state="running",
                private_address="10.4.0.3",
                certificate_fingerprint="a" * 64,
            )
        )
        session.add(
            DevBoxOperation(
                id="setup",
                box_id="box",
                user_id="owner",
                org_id="org",
                deployment_id="dep",
                machine_generation=3,
                action="setup",
                status="succeeded",
                idempotency_key="setup-key",
                request_digest="d",
                actor_user_id="owner",
                provider_token="b" * 64,
                progress="instance_observed",
                retries=0,
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


class RefusingSsm:
    """Fails the test if readiness reaches the host."""

    def send_command(self, **kwargs):
        raise AssertionError(
            "no host command may be sent while the dashboard origin is missing"
        )


async def _events(factory):
    async with factory() as session:
        rows = (
            await session.execute(
                select(DevBoxEvent.event_type, DevBoxEvent.operation_id)
            )
        ).all()
    return [tuple(row) for row in rows]


def _template():
    return LaunchTemplate(
        template_id="lt-1",
        version="7",
        profile_revision="crew-r",
        source_revision="s" * 40,
        gateway_execute_arn="arn:aws:execute-api:us-east-1:1:api/dev",
        crew_model_id="model",
        image_id="ami",
        region="us-east-1",
    )


def _pass_through_checks(monkeypatch):
    async def allowed(db, org_id):
        return True

    monkeypatch.setattr(
        identity, "release_image_matches", lambda box, instance, template: True
    )
    monkeypatch.setattr(identity, "ec2_crew_allowed", allowed)


@pytest.mark.asyncio
async def test_a_missing_origin_refuses_readiness_once_and_sends_no_host_command(
    origin_db, monkeypatch
):
    _pass_through_checks(monkeypatch)
    monkeypatch.setenv("FEATURE_CREW_DASHBOARD", "true")
    monkeypatch.delenv("CREW_DASHBOARD_ORIGIN", raising=False)
    for _ in range(2):
        assert (
            await reconcile_host_readiness(
                origin_db,
                ec2=None,
                ssm=RefusingSsm(),
                store=None,
                instance_id="inst",
                launch_template=_template(),
            )
            is False
        )
    assert await _events(origin_db) == [(CREW_DASHBOARD_ORIGIN_MISSING, "setup")]
    async with origin_db() as session:
        box = await session.get(DevBox, "box")
        instance = await session.get(DevBoxInstance, "inst")
    assert box.auth_readiness == "pending" and instance.readiness_class is None


@pytest.mark.asyncio
async def test_a_loopback_origin_is_refused_like_a_missing_one(origin_db, monkeypatch):
    _pass_through_checks(monkeypatch)
    monkeypatch.setenv("FEATURE_CREW_DASHBOARD", "true")
    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", "http://127.0.0.1:5476")
    assert (
        await reconcile_host_readiness(
            origin_db,
            ec2=None,
            ssm=RefusingSsm(),
            store=None,
            instance_id="inst",
            launch_template=_template(),
        )
        is False
    )
    assert await _events(origin_db) == [(CREW_DASHBOARD_ORIGIN_MISSING, "setup")]


@pytest.mark.asyncio
async def test_with_the_dashboard_off_the_tick_passes_the_guard_without_an_event(
    origin_db, monkeypatch
):
    _pass_through_checks(monkeypatch)
    monkeypatch.delenv("FEATURE_CREW_DASHBOARD", raising=False)
    monkeypatch.delenv("CREW_DASHBOARD_ORIGIN", raising=False)
    # No access session exists yet, so the tick returns False further down — but past the guard.
    assert (
        await reconcile_host_readiness(
            origin_db,
            ec2=None,
            ssm=RefusingSsm(),
            store=None,
            instance_id="inst",
            launch_template=_template(),
        )
        is False
    )
    assert await _events(origin_db) == []


@pytest.mark.asyncio
async def test_with_a_valid_origin_the_guard_is_silent(origin_db, monkeypatch):
    _pass_through_checks(monkeypatch)
    monkeypatch.setenv("FEATURE_CREW_DASHBOARD", "true")
    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", ORIGIN)
    assert (
        await reconcile_host_readiness(
            origin_db,
            ec2=None,
            ssm=RefusingSsm(),
            store=None,
            instance_id="inst",
            launch_template=_template(),
        )
        is False
    )
    assert await _events(origin_db) == []
