"""DynamoDB lease and SQL inventory must agree before returning an endpoint."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.auth.dependencies import get_current_user
from src.crew import proxy as crew_proxy
from src.crew.inventory import CrewExecutorInventory
from src.crew.proxy import current_crew_owner, get_target_resolver, router
from src.crew.targets import CrewLeaseFencedError, CrewOwner, CrewTargetPolicy, EksPodTarget, ResolvedCrewTarget
from src.shared.database import get_db
from src.shared.models.crew_executor import CrewExecutor, CrewOwnerGeneration
from src.shared.models.dev_box import DevBox
from src.shared.schemas.auth import TokenContext


class LeaseTable:
    def __init__(self):
        self.item = None
        self.reads = []

    def get_item(self, **kwargs):
        self.reads.append((kwargs["Key"], kwargs["ConsistentRead"]))
        return {"Item": self.item} if self.item else {}


@pytest.fixture
async def target(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(CrewExecutor.__table__.create)
        await connection.run_sync(CrewOwnerGeneration.__table__.create)
        await connection.run_sync(DevBox.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    table = LeaseTable()
    resolver = EksPodTarget(CrewExecutorInventory(factory), table)
    owner = CrewOwner("tenant-a", "user-a")
    executor = await resolver.ensure(owner)
    assert executor.state == "provisioning" and executor.target_ref is None
    table.item = {
        "PK": "crew#tenant-a#user-a",
        "SK": "executor",
        "tenantId": "tenant-a",
        "ownerUserId": "user-a",
        "crewLease": {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "target_kind": "eks-pod",
            "target_ref": "pod-a",
            "generation": Decimal(1),
            "expires_at": Decimal(200),
            "endpoint": "http://crew-pod.example.test",
        },
    }
    await resolver.record_ready(owner, generation=executor.generation, pod_uid="pod-a", now=100)
    try:
        yield resolver, owner, table, factory
    finally:
        await engine.dispose()


async def test_matching_lease_resolves_and_expired_lease_refuses(target):
    resolver, owner, table, _ = target
    found = await resolver.resolve(owner, now=100)
    assert found.endpoint == "http://crew-pod.example.test"
    assert found.health == "ready" and found.lease.generation == 1
    assert table.reads[-1] == ({"PK": "crew#tenant-a#user-a", "SK": "executor"}, True)
    with pytest.raises(CrewLeaseFencedError, match="lease_fenced"):
        await resolver.resolve(owner, now=200)


async def test_provisional_owner_lease_cannot_resolve(target):
    resolver, owner, table, _ = target
    for state in ("provisioning", "revoked", "unknown"):
        table.item["crewLeaseState"] = state
        with pytest.raises(CrewLeaseFencedError, match="lease_fenced"):
            await resolver.resolve(owner, now=100)
    table.item["crewLeaseState"] = "ready"
    assert (await resolver.resolve(owner, now=100)).health == "ready"


async def test_ready_row_tracks_current_pod_and_declines_stale_replacement(target):
    resolver, owner, table, factory = target
    async with factory() as session:
        row = await session.scalar(select(CrewExecutor).where(CrewExecutor.tenant_id == owner.tenant_id))
        assert (row.user_id, row.state, row.target_kind, row.target_ref, row.generation) == (owner.user_id, "ready", "eks-pod", "pod-a", 1)
        assert row.last_started_at is not None
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).where(CrewExecutor.tenant_id == owner.tenant_id).values(state="stopped"))
    table.item["crewLease"]["target_ref"] = "pod-old"
    with pytest.raises(CrewLeaseFencedError):
        await resolver.record_ready(owner, generation=1, pod_uid="pod-a", now=110)
    with pytest.raises(CrewLeaseFencedError):
        await resolver.record_ready(owner, generation=2, pod_uid="pod-old", now=110)
    async with factory() as session:
        row = await session.scalar(select(CrewExecutor).where(CrewExecutor.tenant_id == owner.tenant_id))
        assert row.state == "stopped"


async def test_default_policy_allows_eks_but_ec2_only_policy_denies_it(target):
    resolver, owner, table, factory = target
    assert resolver.policy.allowed_targets == frozenset({"eks-pod"})
    assert CrewTargetPolicy.from_tenant_settings({}).allowed_targets == frozenset({"eks-pod"})
    assert CrewTargetPolicy.from_tenant_settings({"crew": {"allowed_targets": ["ec2-instance"]}}).allowed_targets == frozenset({"ec2-instance"})
    with pytest.raises(ValidationError):
        CrewTargetPolicy.from_tenant_settings({"crew": {"allowed_targets": ["unknown"]}})
    ec2_only = EksPodTarget(CrewExecutorInventory(factory), table, policy=CrewTargetPolicy(allowed_targets=frozenset({"ec2-instance"})))
    with pytest.raises(CrewLeaseFencedError, match="target_not_allowed"):
        await ec2_only.ensure(CrewOwner("tenant-b", "user-b"))
    with pytest.raises(CrewLeaseFencedError, match="target_not_allowed"):
        await ec2_only.resolve(owner, now=100)


@pytest.mark.parametrize("field,value", [("generation", 2), ("target_ref", "pod-old"), ("target_kind", "ec2-instance")])
async def test_mismatched_lease_refuses(target, field, value):
    resolver, owner, table, _ = target
    table.item["crewLease"][field] = value
    with pytest.raises(CrewLeaseFencedError, match="lease_fenced"):
        await resolver.resolve(owner, now=100)


async def test_absent_or_other_owner_lease_refuses(target):
    resolver, owner, table, _ = target
    table.item = None
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(owner, now=100)
    table.item = {"tenantId": "tenant-b", "ownerUserId": "user-a", "crewLease": {}}
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(owner, now=100)


async def test_fenced_lease_returns_proxy_409(target):
    resolver, _, table, _ = target
    table.item["crewLease"]["generation"] = Decimal(2)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_target_resolver] = lambda: resolver
    app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-a", "user-a")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/crew/target")
    assert response.status_code == 409
    assert response.json() == {"detail": {"error": "lease_fenced"}}
    assert response.headers["cache-control"] == "no-store"


async def test_end_fences_current_generation_and_not_replacement(target):
    resolver, owner, _, factory = target
    assert await resolver.end(owner, generation=2) is False
    async with factory() as session:
        assert (await session.get(CrewOwnerGeneration, (owner.tenant_id, owner.user_id))).generation == 1
    assert await resolver.end(owner, generation=1) is True
    async with factory() as session:
        row = await session.scalar(select(CrewExecutor).where(CrewExecutor.tenant_id == owner.tenant_id))
        assert row.state == "ending" and row.lease_generation is None and row.lease_expires_at is None
        assert (await session.get(CrewOwnerGeneration, (owner.tenant_id, owner.user_id))).generation == 2
    assert await resolver.end(owner, generation=1) is False
    async with factory() as session:
        assert (await session.get(CrewOwnerGeneration, (owner.tenant_id, owner.user_id))).generation == 2
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(owner, now=100)


async def test_proxy_target_requires_adp_identity_membership_and_own_active_lease(target, monkeypatch):
    resolver, _, table, _ = target
    monkeypatch.setattr(crew_proxy.time, "time", lambda: 100)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_target_resolver] = lambda: resolver
    app.dependency_overrides[get_db] = lambda: object()
    allowed = True

    async def check_member(db, tenant_id, subject, *, username=""):
        if allowed and tenant_id == "tenant-a" and subject == "user-a":
            return SimpleNamespace(id=subject)
        raise ChatAuthorizationRefusedError("chat_principal_not_tenant_member")

    monkeypatch.setattr(crew_proxy, "tenant_member_principal", check_member)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthenticated = await client.get("/crew/target", headers={"Cookie": "mc_token_5476=someone-elses-session"})
        assert unauthenticated.status_code == 401
        app.dependency_overrides[get_current_user] = lambda: TokenContext(
            user_id="user-b",
            org_id="tenant-a",
            team_id="",
            department_id="",
            account_type="human",
            expires_at=datetime.now(UTC),
        )
        reads_before = len(table.reads)
        other_user = await client.get("/crew/target", headers={"Cookie": "mc_token_5476=owner-session"})
        assert other_user.status_code == 404
        assert len(table.reads) == reads_before
        app.dependency_overrides[get_current_user] = lambda: TokenContext(
            user_id="user-a",
            org_id="tenant-a",
            team_id="",
            department_id="",
            account_type="human",
            expires_at=datetime.now(UTC),
        )
        active = await client.get("/crew/target")
        assert active.status_code == 200
        assert active.json()["endpoint"] == "http://crew-pod.example.test"
        allowed = False
        reads_before = len(table.reads)
        revoked = await client.get("/crew/target")
        assert len(table.reads) == reads_before
        assert revoked.status_code == 404
        allowed = True
        table.item = None
        missing = await client.get("/crew/target")
        assert missing.status_code == 404
        assert missing.headers["cache-control"] == "no-store"
        assert all("endpoint" not in response.text for response in (unauthenticated, other_user, revoked, missing))


async def test_proxy_target_rejects_resolver_result_for_different_owner(target, monkeypatch):
    resolver, _, _, _ = target
    found = await resolver.resolve(CrewOwner("tenant-a", "user-a"), now=100)
    monkeypatch.setattr(crew_proxy.time, "time", lambda: 100)

    class OtherOwnerResolver:
        async def resolve(self, owner, *, now):
            return ResolvedCrewTarget(found.endpoint, found.lease.model_copy(update={"user_id": "user-b"}), "ready")

    async def check_member(db, tenant_id, subject, *, username=""):
        return SimpleNamespace(id=subject)

    monkeypatch.setattr(crew_proxy, "tenant_member_principal", check_member)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_target_resolver] = lambda: OtherOwnerResolver()
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id="user-a",
        org_id="tenant-a",
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/crew/target")
    assert response.status_code == 404
    assert "endpoint" not in response.text
