"""Resolve only the current owner-bound private Dev Box instance."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.cloud_desktop.host_identity import store_host_fingerprint
from src.crew.inventory import CrewExecutorInventory, CrewLeaseFencedError
from src.crew.targets import CrewOwner, Ec2InstanceTarget
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxAccessSession, DevBoxDevice, DevBoxInstance, DevBoxPolicy, DevBoxTemplate
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity

NOW = int(datetime.now(UTC).timestamp())


class LeaseTable:
    def __init__(self):
        self.row = {
            "tenantId": "tenant-a",
            "ownerUserId": "user-a",
            "crewLeaseState": "ready",
            "crewLease": {
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "target_kind": "ec2-instance",
                "target_ref": "box-a",
                "generation": Decimal(1),
                "expires_at": Decimal(NOW + 600),
                "endpoint": "https://10.4.0.3:8443",
            },
        }
        self.reads = []

    def get_item(self, **kwargs):
        self.reads.append(kwargs)
        return {"Item": self.row}


@pytest.fixture
async def target(tmp_path, monkeypatch):
    # Collection can precede this fixture by more than the ten-minute lease
    # lifetime in a full shard. Start leases when the test actually executes.
    monkeypatch.setitem(globals(), "NOW", int(datetime.now(UTC).timestamp()))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'host.sqlite'}")
    async with engine.begin() as connection:
        for model in (
            Organization,
            User,
            TenantMembership,
            # The owner resolver fuses identity links (#8108), so the table must exist.
            UserIdentity,
            DevBoxPolicy,
            DevBoxTemplate,
            CrewExecutor,
            DevBox,
            DevBoxInstance,
            DevBoxDevice,
            DevBoxAccessSession,
        ):
            await connection.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(Organization(id="tenant-a", name="Tenant A", settings={"crew": {"allowed_targets": ["ec2-instance"]}}))
        session.add(User(id="user-a", org_id="tenant-a", team_id="team", email="user-a@example.test"))
        session.add(TenantMembership(id="membership-a", user_id="user-a", tenant_id="tenant-a"))
        session.add(
            DevBoxPolicy(
                id="policy-a",
                org_id="tenant-a",
                deployment_id="dep-a",
                enabled=True,
                max_live_boxes=1,
                max_running_boxes=1,
                allowed_sizes=["t3.small"],
                max_disk_gb=40,
            )
        )
        session.add(
            DevBoxTemplate(id="template-a", name="assistant", revision="r1", profile="crew", instance_size="t3.small", disk_gb=40, approved=True)
        )
        session.add(
            CrewExecutor(
                id="executor-a",
                tenant_id="tenant-a",
                user_id="user-a",
                target_kind="ec2-instance",
                state="ready",
                dev_box_id="box-a",
                generation=1,
                lease_generation=1,
                lease_expires_at=NOW + 600,
            )
        )
        session.add(
            DevBox(
                id="box-a",
                org_id="tenant-a",
                user_id="user-a",
                deployment_id="dep-a",
                template_id="template-a",
                template_revision="r1",
                profile="crew",
                status="running",
                desired_state="running",
                observed_state="running",
                machine_generation=1,
                auth_readiness="ready",
            )
        )
        session.add(
            DevBoxInstance(
                id="instance-a",
                box_id="box-a",
                org_id="tenant-a",
                user_id="user-a",
                deployment_id="dep-a",
                machine_generation=1,
                template_revision="r1",
                observed_state="running",
                readiness_class="crew_ready",
                private_address="10.4.0.3",
                certificate_fingerprint="a" * 64,
                provider_instance_id="i-example",
            )
        )
        session.add(
            DevBoxDevice(
                id="device-a",
                box_id="box-a",
                instance_id="instance-a",
                org_id="tenant-a",
                user_id="user-a",
                deployment_id="dep-a",
                machine_generation=1,
                public_key_digest="a" * 64,
            )
        )
        session.add(
            DevBoxAccessSession(
                id="session-a",
                box_id="box-a",
                org_id="tenant-a",
                user_id="user-a",
                deployment_id="dep-a",
                machine_generation=1,
                device_id="device-a",
                expires_at=datetime.fromtimestamp(NOW + 600, UTC),
                provider_session_ref="run-a",
            )
        )
    table = LeaseTable()
    try:
        yield Ec2InstanceTarget(CrewExecutorInventory(factory), table, deployment_id="dep-a"), table, factory
    finally:
        await engine.dispose()


async def test_authenticated_dashboard_write_updates_developer_activity(target, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from src.crew import dashboard, proxy
    from src.crew import websocket as websocket_module
    from src.crew.dashboard import install_crew_dashboard
    from src.crew.tickets import mint_crew_ticket
    from src.shared.database import get_db
    from src.shared.models.dev_box import DevBoxOperation
    from tests.crew.test_dashboard import FakeCrewWebSocket

    resolver, table, factory = target
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(DevBoxOperation.__table__.create, checkfirst=True)
    current = await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)
    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", "https://crew.example.test")
    monkeypatch.setenv("CREW_PROXY_ENABLED", "true")
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "dep-a")
    monkeypatch.setattr(proxy, "_get_context_table", lambda: table)
    monkeypatch.setattr(proxy, "get_session_factory", lambda: factory)

    async def forward(*_args):
        return JSONResponse({"forwarded": True})

    async def db():
        async with factory() as session:
            yield session

    app = FastAPI()
    install_crew_dashboard(app)
    host_app = app.router.routes[0].app
    host_app.dependency_overrides[get_db] = db
    monkeypatch.setattr(dashboard, "crew_rate_and_connection", forward)

    class Lease:
        def remaining(self):
            return 45

        async def renew(self):
            return True

        async def release(self):
            pass

    class Registry:
        async def acquire(self, _tenant, _owner):
            return Lease()

    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: FakeCrewWebSocket())

    async def host_context(_target):
        return None

    monkeypatch.setattr(websocket_module, "crew_host_context", host_context)
    cookie = mint_crew_ticket(CrewOwner("tenant-a", "user-a"), current, now=NOW)
    with TestClient(app, base_url="https://crew.example.test") as client:
        assert client.get("/api/chat/slots", headers={"Cookie": f"adp_crew={cookie}"}).status_code == 200
        async with factory() as session:
            assert (await session.get(DevBox, "box-a")).last_activity_at is None
        assert client.post("/api/chat/slots", headers={"Cookie": f"adp_crew={cookie}"}).status_code == 200
        async with factory() as session:
            box = await session.get(DevBox, "box-a")
            assert box.last_activity_at is not None
            box.last_activity_at = None
            await session.commit()
        with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": f"adp_crew={cookie}"}) as browser:
            browser.send_text("developer-action")
            assert browser.receive_text() == "developer-action"
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect):
                browser.receive_text()
    async with factory() as session:
        assert (await session.get(DevBox, "box-a")).last_activity_at is not None


async def test_current_dashboard_write_records_only_current_box_activity(target):
    from fastapi import HTTPException

    from src.crew.dashboard import record_crew_developer_activity
    from src.shared.models.dev_box import DevBoxOperation

    resolver, table, factory = target
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(DevBoxOperation.__table__.create, checkfirst=True)
    current = await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)
    async with factory.begin() as session:
        assert (await session.get(DevBox, "box-a")).last_activity_at is None
        await record_crew_developer_activity(session, current)
    async with factory() as session:
        assert (await session.get(DevBox, "box-a")).last_activity_at is not None
    async with factory.begin() as session:
        (await session.get(DevBox, "box-a")).machine_generation = 2
    async with factory() as session:
        with pytest.raises(HTTPException) as refused:
            await record_crew_developer_activity(session, current)
        assert refused.value.status_code == 409


@pytest.fixture(autouse=True)
def crew_answers(monkeypatch):
    """#9619: the default probe reaches the box; these tests have no box, so it answers "up"."""
    from src.crew import host_readiness

    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(host_readiness, "crew_host_answers", probe)
    return probe


async def test_host_whose_crew_is_not_answering_yet_resolves_as_starting(target, crew_answers):
    """#9619: activation proves the ingress; the target stays "starting" until Crew answers."""
    resolver, _, _ = target
    crew_answers.return_value = False
    starting = await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)
    assert starting.health == "starting" and starting.host_run_id == "run-a"
    assert crew_answers.await_args.args[0].health == "ready", "the probe sees the fully resolved target"
    crew_answers.return_value = True
    assert (await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)).health == "ready"


async def test_current_owner_resolves_only_registered_private_host(target):
    resolver, table, _ = target
    result = await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)
    assert result.endpoint == "https://10.4.0.3:8443"
    assert result.lease.target_ref == "box-a" and result.health == "ready"
    assert result.host_run_id == "run-a" and result.host_instance_ref == "i-example"
    assert result.host_fingerprint == "a" * 64
    assert table.reads == [{"Key": {"PK": "crew#tenant-a#user-a", "SK": "executor"}, "ConsistentRead": True}]


@pytest.mark.parametrize("owner", [CrewOwner("tenant-b", "user-a"), CrewOwner("tenant-a", "user-b")])
async def test_foreign_owner_cannot_resolve_host(target, owner):
    resolver, _, _ = target
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(owner, now=NOW)


@pytest.mark.parametrize(
    "model,fields",
    [
        (DevBox, {"machine_generation": 2}),
        (DevBox, {"auth_readiness": "fenced"}),
        (DevBox, {"deployment_id": "other"}),
        (DevBox, {"status": "stopped"}),
        (DevBoxInstance, {"private_address": "203.0.113.1"}),
        (DevBoxInstance, {"observed_state": "stopped"}),
        (DevBoxInstance, {"readiness_class": "installing_assistant"}),
        (DevBoxAccessSession, {"terminated_at": datetime.fromtimestamp(NOW - 10, UTC)}),
        (DevBoxAccessSession, {"provider_session_ref": None}),
        (DevBoxDevice, {"revoked_at": datetime.fromtimestamp(NOW - 10, UTC)}),
        (DevBoxPolicy, {"enabled": False}),
        (DevBoxTemplate, {"approved": False}),
        (TenantMembership, {"revoked_at": datetime.fromtimestamp(NOW - 10, UTC)}),
        (User, {"is_shadow": True}),
        (CrewExecutor, {"state": "stopped"}),
    ],
)
async def test_stale_or_foreign_host_is_fenced(target, model, fields):
    resolver, _, factory = target
    async with factory.begin() as session:
        await session.execute(update(model).values(**fields))
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)


async def test_tenant_consent_and_endpoint_mismatch_are_fenced(target):
    resolver, table, factory = target
    table.row["crewLease"]["endpoint"] = "https://203.0.113.1:8443"
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)
    table.row["crewLease"]["endpoint"] = "https://10.4.0.3:8443"
    async with factory.begin() as session:
        await session.execute(update(Organization).values(settings={"crew": {"allowed_targets": ["eks-pod"]}}))
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)


async def test_revoked_or_expired_host_lease_is_fenced(target):
    resolver, table, _ = target
    owner = CrewOwner("tenant-a", "user-a")
    table.row["crewLeaseState"] = "revoked"
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(owner, now=NOW)
    table.row["crewLeaseState"] = "ready"
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(owner, now=NOW + 600)


async def test_missing_tenant_policy_fails_closed(target):
    resolver, _, factory = target
    async with factory.begin() as session:
        await session.execute(update(Organization).values(settings=None))
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)


async def test_authenticated_gateway_selects_only_current_host(target, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.auth.dependencies import get_current_user
    from src.crew import proxy
    from src.shared.database import get_db
    from src.shared.schemas.auth import TokenContext

    resolver, table, factory = target
    monkeypatch.setattr(proxy, "_get_context_table", lambda: table)
    monkeypatch.setattr(proxy, "get_session_factory", lambda: factory)
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "dep-a")
    app = FastAPI()
    app.include_router(proxy.router)

    async def db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/crew/target")
        assert unauthorized.status_code == 401
        reads = len(table.reads)
        app.dependency_overrides[get_current_user] = lambda: TokenContext(
            user_id="user-b",
            org_id="tenant-a",
            team_id="",
            department_id="",
            account_type="human",
            expires_at=datetime.now(UTC),
        )
        denied = await client.get("/crew/target")
        assert denied.status_code == 404 and len(table.reads) == reads
        app.dependency_overrides[get_current_user] = lambda: TokenContext(
            user_id="user-a",
            org_id="tenant-a",
            team_id="",
            department_id="",
            account_type="human",
            expires_at=datetime.now(UTC),
        )
        accepted = await client.get("/crew/target")
        assert accepted.status_code == 200
        assert accepted.json()["endpoint"] == "https://10.4.0.3:8443"
        assert accepted.json()["lease"]["target_kind"] == "ec2-instance"
        table.row["crewLeaseState"] = "revoked"
        fenced = await client.get("/crew/target")
        assert fenced.status_code == 409 and fenced.json() == {"detail": {"error": "lease_fenced"}}


@pytest.mark.parametrize("retirement", ["revoked_lease", "stopped_instance", "revoked_device"])
async def test_retired_host_and_foreign_owner_cannot_reach_gateway_ingress(target, monkeypatch, retirement):
    from dataclasses import replace

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from starlette.testclient import WebSocketDenialResponse

    from src.agentauth.chat_authority import current_chat_member
    from src.crew import dashboard as dashboard_module
    from src.crew import handoff as handoff_module
    from src.crew import proxy
    from src.crew import websocket as websocket_module
    from src.crew.dashboard import install_crew_dashboard
    from src.crew.tickets import mint_crew_ticket
    from src.shared.database import get_db
    from src.shared.schemas.auth import TokenContext

    resolver, table, factory = target
    async with factory.begin() as session:
        session.add(User(id="user-b", org_id="tenant-a", team_id="team", email="user-b@example.test"))
        session.add(TenantMembership(id="membership-b", user_id="user-b", tenant_id="tenant-a"))
    async with factory() as session:
        assert await current_chat_member(session, "tenant-a", "user-b", "")
    current = await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)
    owner_cookie = mint_crew_ticket(CrewOwner("tenant-a", "user-a"), current, now=NOW)
    foreign = replace(current, lease=current.lease.model_copy(update={"user_id": "user-b"}))
    foreign_cookie = mint_crew_ticket(CrewOwner("tenant-a", "user-b"), foreign, now=NOW)

    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", "https://crew.example.test")
    monkeypatch.setenv("CREW_PROXY_ENABLED", "true")
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "dep-a")
    monkeypatch.setattr(proxy, "_get_context_table", lambda: table)
    monkeypatch.setattr(proxy, "get_session_factory", lambda: factory)

    async def verified_user(request, authorization):
        if authorization not in ("Bearer owner", "Bearer foreign"):
            raise HTTPException(401, detail={"error": "invalid_adp_session"})
        return TokenContext(
            user_id="user-a" if authorization == "Bearer owner" else "user-b",
            org_id="tenant-a",
            team_id="",
            department_id="",
            account_type="human",
            expires_at=datetime.now(UTC),
        )

    async def db():
        async with factory() as session:
            yield session

    def never_connect(*_args, **_kwargs):
        pytest.fail("fenced or foreign host must not reach private transport")

    app = FastAPI()
    install_crew_dashboard(app)
    host_app = app.router.routes[0].app
    host_app.dependency_overrides[get_db] = db
    monkeypatch.setattr(dashboard_module, "get_current_user", verified_user)
    monkeypatch.setattr(dashboard_module, "crew_host_client", never_connect)
    monkeypatch.setattr(handoff_module, "crew_host_client", never_connect)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", never_connect)

    with TestClient(app, base_url="https://crew.example.test") as client:
        foreign_handoff = client.get("/crew/handoff", headers={"Authorization": "Bearer foreign"})
        foreign_dashboard = client.get("/api/chat/slots", headers={"Cookie": f"adp_crew={foreign_cookie}"})
        assert foreign_handoff.status_code == foreign_dashboard.status_code == 404
        with pytest.raises(WebSocketDenialResponse) as foreign_socket:
            with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": f"adp_crew={foreign_cookie}"}):
                pass
        assert foreign_socket.value.status_code == 404

        if retirement == "revoked_lease":
            table.row["crewLeaseState"] = "revoked"
        else:
            model, values = (
                (DevBoxInstance, {"observed_state": "stopped"})
                if retirement == "stopped_instance"
                else (DevBoxDevice, {"revoked_at": datetime.now(UTC)})
            )
            async with factory.begin() as session:
                await session.execute(update(model).values(**values))

        retired_handoff = client.get("/crew/handoff", headers={"Authorization": "Bearer owner"})
        retired_dashboard = client.get("/api/chat/slots", headers={"Cookie": f"adp_crew={owner_cookie}"})
        assert retired_handoff.status_code == retired_dashboard.status_code == 409
        assert retired_handoff.json() == retired_dashboard.json() == {"detail": {"error": "lease_fenced"}}
        assert retired_handoff.headers["cache-control"] == retired_dashboard.headers["cache-control"] == "no-store"
        with pytest.raises(WebSocketDenialResponse) as retired_socket:
            with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": f"adp_crew={owner_cookie}"}):
                pass
        assert retired_socket.value.status_code == 409


async def test_host_cannot_be_ended_by_pod_lifecycle(target):
    from src.crew.targets import OwnerTargetResolver

    resolver, table, factory = target
    routed = OwnerTargetResolver(CrewExecutorInventory(factory), table, deployment_id="dep-a")
    with pytest.raises(CrewLeaseFencedError, match="target_not_allowed"):
        await routed.end(CrewOwner("tenant-a", "user-a"), generation=1)


async def test_attested_fingerprint_binds_only_current_generation(target):
    _, _, factory = target
    scope = dict(box_id="box-a", generation=1, provider_instance_id="i-example", user_id="user-a", org_id="tenant-a", deployment_id="dep-a")
    async with factory.begin() as session:
        assert await store_host_fingerprint(session, **scope, fingerprint="a" * 64)
        assert await store_host_fingerprint(session, **scope, fingerprint="a" * 64)
        assert not await store_host_fingerprint(session, **scope, fingerprint="b" * 64)
        assert not await store_host_fingerprint(session, **scope, fingerprint="secret")
        assert not await store_host_fingerprint(session, **{**scope, "user_id": "other"}, fingerprint="a" * 64)
        assert not await store_host_fingerprint(session, **{**scope, "generation": 2}, fingerprint="a" * 64)
        assert (await session.get(DevBoxInstance, "instance-a")).certificate_fingerprint == "a" * 64
    async with factory.begin() as session:
        box = await session.get(DevBox, "box-a")
        box.machine_generation = 2
    async with factory.begin() as session:
        assert not await store_host_fingerprint(session, **scope, fingerprint="a" * 64)


async def test_missing_host_fingerprint_cannot_resolve(target):
    resolver, _, factory = target
    async with factory.begin() as session:
        (await session.get(DevBoxInstance, "instance-a")).certificate_fingerprint = None
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=NOW)
