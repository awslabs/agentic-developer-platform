"""An EC2 Crew host uploads verified checkpoints of its own owner's home, and nothing else (#8379).

The host proves its instance identity at the edge; the gateway maps it to one running box, its
owner and the owner's ``ready`` executor, grants direct-to-S3 uploads, verifies the exact
versions and advances the owner's pointer. A foreign instance, a non-ready executor, a stale
lease or a retired generation is refused and the pointer never moves. The binding deliberately
does not need the live run: both stop paths fence host authority before the final capture.
"""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.crew import checkpoint_routes as routes
from src.crew.checkpoint_objects import VersionedCheckpointObjects
from src.crew.checkpoint_store import CrewCheckpointStore
from src.proxy.routes import get_token_context
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_executor import CrewExecutor, CrewOwnerGeneration
from src.shared.models.dev_box import DevBox, DevBoxInstance, DevBoxPolicy, DevBoxTemplate
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext
from tests.crew.test_checkpoint_objects import S3Fixture, fixture

INSTANCE = "i-0123456789abcdef0"
CALLER = f"arn:aws:sts::000000000000:assumed-role/adp-dev-box-guest/{INSTANCE}"


def host_context(**overrides):
    values = {
        "user_id": "dev-box-guest",
        "org_id": "__platform__",
        "team_id": "",
        "department_id": "",
        "account_type": "service",
        "expires_at": datetime(2100, 1, 1, tzinfo=UTC),
        "auth_source": "iam",
        "requires_host_identity": True,
    }
    values.update(overrides)
    return TokenContext(**values)


@pytest.fixture
async def host(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'host-checkpoint.sqlite'}")
    async with engine.begin() as connection:
        for model in (Organization, User, DevBoxPolicy, DevBoxTemplate, CrewExecutor, CrewOwnerGeneration, DevBox, DevBoxInstance, CrewCheckpoint):
            await connection.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(Organization(id="tenant-a", name="Tenant A", settings={"crew": {"allowed_targets": ["ec2-instance"]}}))
        session.add(User(id="user-a", org_id="tenant-a", team_id="team", email="owner@example.test"))
        session.add(DevBoxTemplate(id="template", name="assistant", revision="r1", profile="crew", instance_size="small", disk_gb=30, approved=True))
        session.add(
            CrewExecutor(
                id="executor-one",
                tenant_id="tenant-a",
                user_id="user-a",
                owner_key="b" * 52,
                target_kind="ec2-instance",
                dev_box_id="owned-box",
                state="ready",
                generation=1,
                lease_generation=1,
                lease_expires_at=4_000_000_000,
                target_ref="owned-box",
            )
        )
        session.add(
            DevBox(
                id="owned-box",
                user_id="user-a",
                org_id="tenant-a",
                deployment_id="deployment",
                template_id="template",
                template_revision="r1",
                profile="crew",
                status="running",
                desired_state="stopped",  # an owner stop is admitted: the final capture runs post-fence
                observed_state="running",
                machine_generation=1,
                auth_readiness="fenced",
            )
        )
        session.add(
            DevBoxInstance(
                id="instance-one",
                box_id="owned-box",
                user_id="user-a",
                org_id="tenant-a",
                deployment_id="deployment",
                machine_generation=1,
                template_revision="r1",
                observed_state="running",
                provider_instance_id=INSTANCE,
            )
        )
    try:
        yield CrewCheckpointStore(factory, retention_seconds=3600), factory
    finally:
        await engine.dispose()


def application(store, s3, monkeypatch, *, caller=CALLER, context=None):
    monkeypatch.setattr(routes, "verified_caller_identity", lambda _request: caller)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[routes.get_store] = lambda: store
    app.dependency_overrides[routes.get_objects] = lambda: VersionedCheckpointObjects(s3, "fixture-bucket")
    app.dependency_overrides[get_token_context] = lambda: context or host_context()
    return app


def begin_body(archive, manifest, key="host:1:" + "c" * 40):
    return {
        "idempotency_key": key,
        "crew_version": "0.11.2",
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "archive_size": len(archive),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "manifest_size": len(manifest),
    }


async def upload(s3, granted, archive, manifest, *, version="first"):
    objects = {}
    for label, content in (("archive", archive), ("manifest", manifest)):
        item = granted[label]
        assert item["headers"]["x-amz-checksum-sha256"] and item["url"].startswith("https://")
        s3.objects[(item["bucket"], item["key"], version)] = content
        objects[label] = {
            "bucket": item["bucket"],
            "key": item["key"],
            "version_id": version,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    return objects


async def latest_pointer(factory):
    async with factory() as session:
        return await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id).where(CrewExecutor.id == "executor-one"))


async def test_host_begin_upload_complete_publishes_the_owner_pointer_and_replays_by_key(host, monkeypatch):
    store, factory = host
    s3 = S3Fixture()
    app = application(store, s3, monkeypatch)
    archive, manifest = fixture()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        begun = await client.post("/crew/host-checkpoint/begin", json=begin_body(archive, manifest), headers={"x-agent-runid": "run-one"})
        assert begun.status_code == 200, begun.text
        assert begun.headers["cache-control"] == "no-store"
        granted = begun.json()
        assert granted["status"] == "uploading" and granted["sequence"] == 1
        checkpoint_id = granted["id"]
        assert granted["archive"]["key"] == f"crew-checkpoints/{'b' * 52}/{checkpoint_id}/archive"
        assert "owner_key" not in granted
        objects = await upload(s3, granted, archive, manifest)
        # A corrupt upload is a visible failed capture and never moves the pointer.
        s3.objects[(objects["archive"]["bucket"], objects["archive"]["key"], "first")] = b"corrupt"
        failed = await client.post("/crew/host-checkpoint/complete", json={"checkpoint_id": checkpoint_id, "objects": objects})
        assert failed.status_code == 422
        assert await latest_pointer(factory) is None
        # The same key re-grants the failed candidate; a good upload verifies and publishes.
        regranted = await client.post("/crew/host-checkpoint/begin", json=begin_body(archive, manifest))
        assert regranted.status_code == 200 and regranted.json()["id"] == checkpoint_id
        objects = await upload(s3, regranted.json(), archive, manifest, version="second")
        verified = await client.post("/crew/host-checkpoint/complete", json={"checkpoint_id": checkpoint_id, "objects": objects})
        assert verified.status_code == 200 and verified.json() == {"id": checkpoint_id, "status": "verified"}
        assert await latest_pointer(factory) == checkpoint_id
        async with factory() as session:
            record = await session.get(CrewCheckpoint, checkpoint_id)
            assert (record.status, record.target_kind, record.generation, record.owner_key) == ("verified", "ec2-instance", 1, "b" * 52)
            assert record.archive_version_id == "second" and record.manifest_version_id == "second"
            assert record.retention_until is not None and record.captured_at is not None
        # A lost `complete` response: the host replays `begin` with the stable key and learns it is done.
        replayed = await client.post("/crew/host-checkpoint/begin", json=begin_body(archive, manifest))
        assert replayed.status_code == 200 and replayed.json() == {"id": checkpoint_id, "status": "verified", "sequence": 1}
        # A new capture chains on the published predecessor.
        second = await client.post("/crew/host-checkpoint/begin", json=begin_body(archive, manifest, key="host:1:" + "d" * 40))
        assert second.status_code == 200 and second.json()["sequence"] == 2
        async with factory() as session:
            successor = await session.get(CrewCheckpoint, second.json()["id"])
            assert successor.predecessor_id == checkpoint_id


async def test_incomplete_receipts_and_foreign_candidates_are_refused(host, monkeypatch):
    store, factory = host
    s3 = S3Fixture()
    app = application(store, s3, monkeypatch)
    archive, manifest = fixture()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        begun = await client.post("/crew/host-checkpoint/begin", json=begin_body(archive, manifest))
        objects = await upload(s3, begun.json(), archive, manifest)
        only_archive = await client.post(
            "/crew/host-checkpoint/complete", json={"checkpoint_id": begun.json()["id"], "objects": {"archive": objects["archive"]}}
        )
        assert only_archive.status_code == 404
        unknown = await client.post("/crew/host-checkpoint/complete", json={"checkpoint_id": "someone-elses-candidate", "objects": objects})
        assert unknown.status_code == 404
        extra = await client.post("/crew/host-checkpoint/begin", json={**begin_body(archive, manifest), "owner_key": "b" * 52})
        assert extra.status_code == 422
        assert await latest_pointer(factory) is None


@pytest.mark.parametrize(
    "refusal",
    ["foreign_instance", "jwt_caller", "no_host_identity", "executor_provisioning", "lease_expired", "generation_moved_on", "tenant_disallowed"],
)
async def test_unbound_or_fenced_hosts_cannot_begin_or_complete(host, monkeypatch, refusal):
    store, factory = host
    s3 = S3Fixture()
    archive, manifest = fixture()
    caller, context = CALLER, host_context()
    expected = 409
    if refusal == "foreign_instance":
        caller, expected = "arn:aws:sts::000000000000:assumed-role/adp-dev-box-guest/i-0fedcba9876543210", 403
    elif refusal == "jwt_caller":
        context, expected = host_context(auth_source="jwt", account_type="human"), 403
    elif refusal == "no_host_identity":
        context, expected = host_context(requires_host_identity=False), 403
    else:
        async with factory.begin() as session:
            executor = await session.get(CrewExecutor, "executor-one")
            box = await session.get(DevBox, "owned-box")
            if refusal == "executor_provisioning":
                executor.state = "provisioning"
            elif refusal == "lease_expired":
                executor.lease_expires_at = 1
            elif refusal == "generation_moved_on":
                box.machine_generation = 2  # replaced: this instance's generation is retired
            elif refusal == "tenant_disallowed":
                organization = await session.get(Organization, "tenant-a")
                organization.settings = {"crew": {"allowed_targets": ["eks-pod"]}}
    app = application(store, s3, monkeypatch, caller=caller, context=context)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        begun = await client.post("/crew/host-checkpoint/begin", json=begin_body(archive, manifest))
        assert begun.status_code == expected, begun.text
        assert begun.headers["cache-control"] == "no-store" and "url" not in begun.text
        completed = await client.post(
            "/crew/host-checkpoint/complete",
            json={
                "checkpoint_id": "candidate",
                "objects": {
                    label: {"bucket": "fixture-bucket", "key": label, "version_id": "v", "sha256": "0" * 64, "size": 1}
                    for label in ("archive", "manifest")
                },
            },
        )
        assert completed.status_code == expected
    assert await latest_pointer(factory) is None
    async with factory() as session:
        assert (await session.scalars(select(CrewCheckpoint))).first() is None


async def test_publication_is_fenced_when_the_generation_moves_on_mid_upload(host, monkeypatch):
    """Upload granted on generation 1; the box is replaced before `complete`: nothing is published."""
    store, factory = host
    s3 = S3Fixture()
    app = application(store, s3, monkeypatch)
    archive, manifest = fixture()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        begun = await client.post("/crew/host-checkpoint/begin", json=begin_body(archive, manifest))
        objects = await upload(s3, begun.json(), archive, manifest)
        async with factory.begin() as session:
            executor = await session.get(CrewExecutor, "executor-one")
            executor.state = "deleted"
            executor.lease_generation = None
            executor.lease_expires_at = None
        completed = await client.post("/crew/host-checkpoint/complete", json={"checkpoint_id": begun.json()["id"], "objects": objects})
        assert completed.status_code == 409
    async with factory() as session:
        record = await session.get(CrewCheckpoint, begun.json()["id"])
        assert record.status == "uploading" and record.archive_version_id is None


def test_host_checkpoint_routes_are_declared_without_the_edge_prefix():
    """The edge strips ``/agent``; the guest grant names ``/agent/crew/host-checkpoint/*`` (#8223)."""
    paths = {route.path for route in routes.router.routes}
    assert {"/crew/host-checkpoint/begin", "/crew/host-checkpoint/complete"} <= paths
    assert not any(path.startswith("/agent/") for path in paths)
    body = json.dumps(routes.HostCheckpointBegin.model_json_schema())
    assert "owner_key" not in body
