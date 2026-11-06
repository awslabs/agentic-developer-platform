"""Mounted API routes publish only after scoped exact-version S3 verification."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agentauth.chat_tls import HOST, SandboxTransport
from src.crew import checkpoint_routes as routes
from src.crew.checkpoint_identity import CheckpointHelperIdentity
from src.crew.checkpoint_objects import VersionedCheckpointObjects
from src.crew.checkpoint_store import CrewCheckpointStore
from src.crew.proxy import current_crew_owner
from src.crew.targets import CrewOwner
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_executor import CrewExecutor, CrewOwnerGeneration
from src.shared.models.dev_box import DevBox
from tests.crew.test_checkpoint_objects import S3Fixture, fixture


@pytest.fixture
async def context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'checkpoint.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(CrewExecutor.__table__.create)
        await connection.run_sync(CrewOwnerGeneration.__table__.create)
        await connection.run_sync(DevBox.__table__.create)
        await connection.run_sync(CrewCheckpoint.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(
            CrewExecutor(
                id="owner-executor",
                tenant_id="tenant-a",
                user_id="user-a",
                owner_key="a" * 52,
                target_kind="eks-pod",
                state="ready",
                generation=1,
                lease_generation=1,
                lease_expires_at=4_000_000_000,
            )
        )
    try:
        yield CrewCheckpointStore(factory, retention_seconds=3600), factory
    finally:
        await engine.dispose()


class Verifier:
    identity = None

    def verify(self, token):
        assert token == "helper-token"
        return self.identity

    def pod_is_gone(self, name, uid):
        return False


async def test_mounted_routes_admit_grant_and_verify_without_foreign_publication(context):
    store, factory = context
    s3 = S3Fixture()
    verifier = Verifier()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-a", "user-a")
    app.dependency_overrides[routes.get_store] = lambda: store
    app.dependency_overrides[routes.get_objects] = lambda: VersionedCheckpointObjects(s3, "fixture-bucket")
    app.dependency_overrides[routes.get_verifier] = lambda: verifier
    app.dependency_overrides[routes.get_restore_verifier] = lambda: verifier
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client,
        AsyncClient(transport=ASGITransport(app=SandboxTransport(app)), base_url=f"https://{HOST}:8443") as helper,
    ):
        admitted = await client.post("/v1/crew/checkpoints", json={"idempotency_key": "fixed-attempt"})
        assert admitted.status_code == 202
        checkpoint_id = admitted.json()["id"]
        repeated_admission = await client.post("/v1/crew/checkpoints", json={"idempotency_key": "fixed-attempt"})
        assert repeated_admission.status_code == 200 and repeated_admission.json()["id"] == checkpoint_id
        pending = await client.get(f"/v1/crew/checkpoints/{checkpoint_id}")
        assert pending.status_code == 200 and pending.json()["status"] == "uploading"
        assert pending.json()["archive_size"] is None
        assert pending.headers["cache-control"] == "no-store"
        pending_list = await client.get("/v1/crew/checkpoints")
        assert pending_list.status_code == 200 and pending_list.json()["checkpoints"] == [pending.json()]
        verifier.identity = CheckpointHelperIdentity(
            "pod", "a" * 52, hashlib.sha256(checkpoint_id.encode()).hexdigest(), "sha256:" + "f" * 64, checkpoint_id
        )
        archive, manifest = fixture()
        grant_body = {
            "owner_key": "a" * 52,
            "crew_version": "0.11.2",
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "archive_size": len(archive),
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "manifest_size": len(manifest),
        }
        base = f"/internal/v1/agent/crew/checkpoints/{checkpoint_id}"
        assert (await helper.post(base + "/upload-grant", json=grant_body)).status_code == 401
        for path in ("/v1/crew/checkpoints", base + "/delete", base + "/upload-grant/", base + "/restore-grant/extra"):
            assert (await helper.post(path, json={})).status_code == 404
        assert (await helper.get(base + "/restore-grant")).status_code == 404
        foreign = await helper.post(
            base + "/upload-grant", json={**grant_body, "owner_key": "b" * 52}, headers={"Authorization": "Bearer helper-token"}
        )
        assert foreign.status_code == 404
        granted = await helper.post(base + "/upload-grant", json=grant_body, headers={"Authorization": "Bearer helper-token"})
        assert granted.status_code == 200
        objects = {}
        for label, content in (("archive", archive), ("manifest", manifest)):
            item = granted.json()[label]
            s3.objects[(item["bucket"], item["key"], "first")] = content
            objects[label] = {
                "bucket": item["bucket"],
                "key": item["key"],
                "version_id": "first",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        s3.objects[(objects["archive"]["bucket"], objects["archive"]["key"], "first")] = b"corrupt"
        failed = await helper.post(
            base + "/complete", json={"owner_key": "a" * 52, "objects": objects}, headers={"Authorization": "Bearer helper-token"}
        )
        assert failed.status_code == 422
        async with factory() as session:
            assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) is None
        async with factory() as session:
            record = await session.scalar(select(CrewCheckpoint))
            assert record.status == "failed" and record.failure_class == "verification_failed"
        failure_status = (await client.get(f"/v1/crew/checkpoints/{checkpoint_id}")).json()
        assert failure_status["failure_class"] == "verification_failed"
        assert failure_status["archive_size"] == len(archive)
        assert failure_status["is_latest_verified"] is False
        assert "archive_key" not in failure_status
        assert (await helper.post(base + "/upload-grant", json=grant_body, headers={"Authorization": "Bearer helper-token"})).status_code == 200
        objects["archive"]["version_id"] = "second"
        s3.objects[(objects["archive"]["bucket"], objects["archive"]["key"], "second")] = archive
        verified = await helper.post(
            base + "/complete", json={"owner_key": "a" * 52, "objects": objects}, headers={"Authorization": "Bearer helper-token"}
        )
        assert verified.status_code == 200 and verified.json() == {"id": checkpoint_id, "status": "verified"}
        selected = await client.get(f"/v1/crew/checkpoints/{checkpoint_id}")
        assert selected.status_code == 200 and selected.json()["is_latest_verified"] is True
        assert selected.json()["archive_size"] == len(archive)
        assert type(selected.json()["archive_size"]) is int
        assert selected.json()["verified_at"] and selected.json()["captured_at"]
        assert "archive_key" not in selected.json() and "owner_key" not in selected.json()
        assert (await client.delete(f"/v1/crew/checkpoints/{checkpoint_id}")).status_code == 409
        listed = await client.get("/v1/crew/checkpoints")
        assert listed.status_code == 200 and [item["id"] for item in listed.json()["checkpoints"]] == [checkpoint_id]
        assert listed.json()["checkpoints"][0]["archive_size"] == len(archive)
        assert listed.json()["checkpoints"] == [selected.json()]
        app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-a", "user-b")
        assert (await client.get(f"/v1/crew/checkpoints/{checkpoint_id}")).status_code == 404
        assert (await client.delete(f"/v1/crew/checkpoints/{checkpoint_id}")).status_code == 404
        assert (await client.get("/v1/crew/checkpoints")).json() == {"checkpoints": []}
        app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-a", "user-a")
        restored = await helper.post(base + "/restore-grant", headers={"Authorization": "Bearer helper-token"})
        assert restored.status_code == 200
        async with factory() as session:
            assert await session.scalar(select(CrewCheckpoint.restore_pins)) == 1
        released = await helper.post(
            base + "/restore-release", json={"restore_token": restored.json()["restore_token"]}, headers={"Authorization": "Bearer helper-token"}
        )
        assert released.status_code == 200
        async with factory() as session:
            assert await session.scalar(select(CrewCheckpoint.restore_pins)) == 0
        retried = await helper.post(
            base + "/complete", json={"owner_key": "a" * 52, "objects": objects}, headers={"Authorization": "Bearer helper-token"}
        )
        assert retried.status_code == 200 and retried.json() == verified.json()
        async with factory() as session:
            assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == checkpoint_id


async def test_disabled_admission_and_missing_helper_token_fail_closed(monkeypatch):
    monkeypatch.delenv("ADP_CREW_CHECKPOINTS_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        disabled = await client.post("/v1/crew/checkpoints", json={"idempotency_key": "attempt"})
        assert disabled.status_code == 503
        app.dependency_overrides[routes.enabled] = lambda: None
        app.dependency_overrides[routes.get_verifier] = lambda: Verifier()
        app.dependency_overrides[routes.get_restore_verifier] = lambda: Verifier()
        unauthorized = await client.post(
            "/internal/v1/agent/crew/checkpoints/unknown/upload-grant",
            json={
                "owner_key": "a" * 52,
                "crew_version": "0.11.2",
                "archive_sha256": "a" * 64,
                "archive_size": 1,
                "manifest_sha256": "b" * 64,
                "manifest_size": 1,
            },
        )
        assert unauthorized.status_code == 401


async def test_owner_delete_only_requests_cleanup_of_retention_expired_nonlatest_versions(context):
    store, factory = context
    first = await store.admit(CrewOwner("tenant-a", "user-a"), "earlier", expected_predecessor_id=None, now=100)
    second = await store.admit(CrewOwner("tenant-a", "user-a"), "current", expected_predecessor_id=None, now=100)
    s3 = S3Fixture()
    archive, manifest = fixture()
    archive_key = f"crew-checkpoints/{first.owner_key}/{first.id}/archive"
    manifest_key = f"crew-checkpoints/{first.owner_key}/{first.id}/manifest"
    s3.objects[("fixture-bucket", archive_key, "first")] = archive
    s3.objects[("fixture-bucket", manifest_key, "first")] = manifest
    async with factory.begin() as session:
        await session.execute(
            update(CrewCheckpoint)
            .where(CrewCheckpoint.id.in_([first.id, second.id]))
            .values(status="verified", retention_until=datetime.now(UTC) - timedelta(days=1))
        )
        await session.execute(
            update(CrewCheckpoint)
            .where(CrewCheckpoint.id == first.id)
            .values(
                archive_bucket="fixture-bucket",
                archive_key=archive_key,
                archive_version_id="first",
                archive_sha256=hashlib.sha256(archive).hexdigest(),
                archive_size=len(archive),
                manifest_bucket="fixture-bucket",
                manifest_key=manifest_key,
                manifest_version_id="first",
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                manifest_size=len(manifest),
            )
        )
        await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id=second.id))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-a", "user-a")
    app.dependency_overrides[routes.get_store] = lambda: store
    app.dependency_overrides[routes.get_objects] = lambda: VersionedCheckpointObjects(s3, "fixture-bucket")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        assert (await client.delete(f"/v1/crew/checkpoints/{second.id}")).status_code == 409
        s3.fail_manifest_delete = True
        failed = await client.delete(f"/v1/crew/checkpoints/{first.id}")
        assert failed.status_code == 503
        async with factory() as session:
            record = await session.scalar(select(CrewCheckpoint).where(CrewCheckpoint.id == first.id))
            assert record.status == "deleting"
            assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == second.id
        assert s3.head_object(Bucket="fixture-bucket", Key=manifest_key, VersionId="first")
        s3.fail_manifest_delete = False
        deleted = await client.delete(f"/v1/crew/checkpoints/{first.id}")
        assert deleted.status_code == 200 and deleted.json() == {"id": first.id, "status": "deleted"}
        assert (await client.delete(f"/v1/crew/checkpoints/{first.id}")).status_code == 200
        assert s3.deletes == [("fixture-bucket", archive_key, "first"), ("fixture-bucket", manifest_key, "first")]
        assert (await client.get(f"/v1/crew/checkpoints/{first.id}")).json()["status"] == "deleted"
        async with factory() as session:
            assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == second.id


async def test_two_owners_publish_concurrently_without_cross_owner_status_or_deletion(context, tmp_path):
    import asyncio

    store, factory = context
    async with factory.begin() as session:
        session.add(
            CrewExecutor(
                id="other-executor",
                tenant_id="tenant-a",
                user_id="user-b",
                owner_key="b" * 52,
                target_kind="eks-pod",
                state="ready",
                generation=1,
                lease_generation=1,
                lease_expires_at=4_000_000_000,
            )
        )
    s3 = S3Fixture()
    apps = []
    verifiers = []
    for tenant, user in (("tenant-a", "user-a"), ("tenant-a", "user-b")):
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.enabled] = lambda: None
        app.dependency_overrides[current_crew_owner] = lambda tenant=tenant, user=user: CrewOwner(tenant, user)
        app.dependency_overrides[routes.get_store] = lambda: store
        app.dependency_overrides[routes.get_objects] = lambda: VersionedCheckpointObjects(s3, "fixture-bucket")
        verifier = Verifier()
        app.dependency_overrides[routes.get_verifier] = lambda verifier=verifier: verifier
        app.dependency_overrides[routes.get_restore_verifier] = lambda verifier=verifier: verifier
        apps.append(app)
        verifiers.append(verifier)
    async with AsyncClient(transport=ASGITransport(app=apps[0]), base_url="https://gateway.fixture") as first_client:
        async with AsyncClient(transport=ASGITransport(app=apps[1]), base_url="https://gateway.fixture") as second_client:
            clients = first_client, second_client
            admitted = await asyncio.gather(*(client.post("/v1/crew/checkpoints", json={"idempotency_key": "same-key"}) for client in clients))
            assert all(response.status_code == 202 for response in admitted)
            ids = [response.json()["id"] for response in admitted]
            for index, client in enumerate(clients):
                assert (await client.get(f"/v1/crew/checkpoints/{ids[1 - index]}")).status_code == 404
                assert (await client.delete(f"/v1/crew/checkpoints/{ids[1 - index]}")).status_code == 404
                assert [item["id"] for item in (await client.get("/v1/crew/checkpoints")).json()["checkpoints"]] == [ids[index]]
                verifiers[index].identity = CheckpointHelperIdentity(
                    f"pod-{index}",
                    ("a" if index == 0 else "b") * 52,
                    hashlib.sha256(ids[index].encode()).hexdigest(),
                    "sha256:" + "f" * 64,
                    ids[index],
                )
            archive, manifest = fixture()
            contents = {"archive": archive, "manifest": manifest}
            grants = await asyncio.gather(
                *(
                    client.post(
                        f"/internal/v1/agent/crew/checkpoints/{ids[index]}/upload-grant",
                        json={
                            "owner_key": ("a" if index == 0 else "b") * 52,
                            "crew_version": "0.11.2",
                            **{
                                f"{label}_{field}": hashlib.sha256(content).hexdigest() if field == "sha256" else len(content)
                                for label, content in contents.items()
                                for field in ("sha256", "size")
                            },
                        },
                        headers={"Authorization": "Bearer helper-token"},
                    )
                    for index, client in enumerate(clients)
                )
            )
            assert all(response.status_code == 200 for response in grants)
            receipts = []
            for index, grant in enumerate(grants):
                objects = {}
                for label, content in contents.items():
                    item = grant.json()[label]
                    assert item["key"].startswith(f"crew-checkpoints/{('a' if index == 0 else 'b') * 52}/")
                    s3.objects[(item["bucket"], item["key"], "exact")] = content
                    objects[label] = {
                        "bucket": item["bucket"],
                        "key": item["key"],
                        "version_id": "exact",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                receipts.append(objects)
            completed = await asyncio.gather(
                *(
                    client.post(
                        f"/internal/v1/agent/crew/checkpoints/{ids[index]}/complete",
                        json={"owner_key": ("a" if index == 0 else "b") * 52, "objects": receipts[index]},
                        headers={"Authorization": "Bearer helper-token"},
                    )
                    for index, client in enumerate(clients)
                )
            )
            assert all(response.status_code == 200 for response in completed)
            for index, client in enumerate(clients):
                selected = await client.get(f"/v1/crew/checkpoints/{ids[index]}")
                assert selected.json()["is_latest_verified"] is True
                assert selected.json()["archive_size"] == len(archive)
                assert (await client.get(f"/v1/crew/checkpoints/{ids[1 - index]}")).status_code == 404
                listing = await client.get("/v1/crew/checkpoints")
                assert listing.status_code == 200
                assert [(row["id"], row["status"]) for row in listing.json()["checkpoints"]] == [(ids[index], "verified")]
                assert listing.json()["checkpoints"] == [selected.json()]
                assert (await client.get("/v1/crew/checkpoints/selection")).json()["checkpoint_id"] == ids[index]
                assert (await client.delete(f"/v1/crew/checkpoints/{ids[index]}")).status_code == 409
                assert (await client.delete(f"/v1/crew/checkpoints/{ids[1 - index]}")).status_code == 404
            async with factory() as session:
                executors = (await session.scalars(select(CrewExecutor).order_by(CrewExecutor.user_id))).all()
                assert [row.latest_verified_checkpoint_id for row in executors] == ids

            from pathlib import Path
            from runpy import run_path

            restore_exact = run_path(str(Path(__file__).resolve().parents[3] / "agent-factory/agent/scripts/crew_checkpoint.py"))["restore_exact"]

            for index, client in enumerate(clients):
                foreign = await client.post(
                    f"/internal/v1/agent/crew/checkpoints/{ids[1 - index]}/restore-grant", headers={"Authorization": "Bearer helper-token"}
                )
                assert foreign.status_code == 404
            grants = await asyncio.gather(
                *(
                    client.post(f"/internal/v1/agent/crew/checkpoints/{ids[index]}/restore-grant", headers={"Authorization": "Bearer helper-token"})
                    for index, client in enumerate(clients)
                )
            )
            assert all(response.status_code == 200 for response in grants)
            assert (
                await first_client.post(
                    f"/internal/v1/agent/crew/checkpoints/{ids[0]}/restore-grant", headers={"Authorization": "Bearer helper-token"}
                )
            ).status_code == 409
            for index, client in enumerate(clients):
                grant = grants[index].json()
                assert grant["crew_version"] == "0.11.2" and grant["schema_version"] == 1
                assert grant["objects"]["archive"]["version_id"] == "exact"
                assert grant["objects"]["archive"]["key"] == receipts[index]["archive"]["key"]
                assert (
                    await clients[1 - index].post(
                        f"/internal/v1/agent/crew/checkpoints/{ids[index]}/restore-release",
                        json={"restore_token": grant["restore_token"]},
                        headers={"Authorization": "Bearer helper-token"},
                    )
                ).status_code == 404
                assert (
                    await client.post(
                        f"/internal/v1/agent/crew/checkpoints/{ids[index]}/restore-release",
                        json={"restore_token": "wrong-token" * 4},
                        headers={"Authorization": "Bearer helper-token"},
                    )
                ).status_code == 404
                mounted_home = tmp_path / f"owner-{index}" / "crew"
                mounted_home.parent.mkdir()

                def fetch(bucket, key, version_id):
                    return s3.objects[(bucket, key, version_id)]

                restore_exact(mounted_home, grant["objects"], fetch)
                assert mounted_home.joinpath("lessons.jsonl").read_bytes() == b'{"provenance":"owner-approved"}\n'
                released = await client.post(
                    f"/internal/v1/agent/crew/checkpoints/{ids[index]}/restore-release",
                    json={"restore_token": grant["restore_token"]},
                    headers={"Authorization": "Bearer helper-token"},
                )
                assert released.status_code == 200
                assert (
                    await client.post(
                        f"/internal/v1/agent/crew/checkpoints/{ids[index]}/restore-release",
                        json={"restore_token": grant["restore_token"]},
                        headers={"Authorization": "Bearer helper-token"},
                    )
                ).status_code == 404
            async with factory() as session:
                pins = (await session.scalars(select(CrewCheckpoint.restore_pins))).all()
                assert pins == [0, 0]


async def test_helper_token_cannot_invoke_owner_deletion(context, monkeypatch):
    import jwt

    from src.auth import dependencies

    class RefusingCognito:
        def validate_token(self, token):
            assert token == "helper-token"
            raise jwt.InvalidTokenError("not a user token")

    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: RefusingCognito())
    store, _ = context
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[routes.get_store] = lambda: store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        owner_deletion = await client.delete("/v1/crew/checkpoints/accepted", headers={"Authorization": "Bearer helper-token"})
        assert owner_deletion.status_code == 401
        assert (
            await client.delete("/internal/v1/agent/crew/checkpoints/accepted/complete", headers={"Authorization": "Bearer helper-token"})
        ).status_code == 405


async def test_restore_helper_mount_pins_versions_during_owner_deletion(context, tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path
    from threading import Event
    from urllib.parse import urlparse

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "agent-factory/agent/scripts"))
    from crew_checkpoint_helper import run_restore

    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    checkpoint = await store.admit(owner, "pinned-mount", expected_predecessor_id=None, now=100)
    archive, manifest = fixture()
    contents = {"archive": archive, "manifest": manifest}
    s3 = S3Fixture()
    async with factory.begin() as session:
        await session.execute(
            update(CrewCheckpoint)
            .where(CrewCheckpoint.id == checkpoint.id)
            .values(
                status="verified",
                crew_version="0.11.2",
                schema_version=1,
                retention_until=datetime.now(UTC) - timedelta(days=1),
                **{
                    f"{label}_{field}": value
                    for label, content in contents.items()
                    for field, value in (
                        ("bucket", "fixture-bucket"),
                        ("key", f"crew-checkpoints/{checkpoint.owner_key}/{checkpoint.id}/{label}"),
                        ("version_id", "exact"),
                        ("sha256", hashlib.sha256(content).hexdigest()),
                        ("size", len(content)),
                    )
                },
            )
        )
        await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id=checkpoint.id))
    for label, content in contents.items():
        s3.objects[("fixture-bucket", f"crew-checkpoints/{checkpoint.owner_key}/{checkpoint.id}/{label}", "exact")] = content
    checkpoint_hash = hashlib.sha256(checkpoint.id.encode()).hexdigest()
    verifier = Verifier()
    verifier.identity = CheckpointHelperIdentity(
        "pod-uid", checkpoint.owner_key, checkpoint_hash, "sha256:" + "f" * 64, checkpoint.id, "chat-restore-" + checkpoint_hash[:12] + "-abcde"
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[current_crew_owner] = lambda: owner
    app.dependency_overrides[routes.get_store] = lambda: store
    app.dependency_overrides[routes.get_objects] = lambda: VersionedCheckpointObjects(s3, "fixture-bucket")
    app.dependency_overrides[routes.get_restore_verifier] = lambda: verifier
    mounted_home = tmp_path / "fresh-mount"
    mounted_home.mkdir()
    paused, resume = Event(), Event()
    loop = asyncio.get_running_loop()

    class Reply:
        def __init__(self, content, headers=None):
            self.content, self.headers = content, headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, size=-1):
            return self.content if size < 0 else self.content[:size]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:

        def opener(request, *, timeout, context):
            if request.full_url.startswith("https://s3.fixture/"):
                paused.set()
                if not resume.wait(5):
                    raise TimeoutError("S3 fetch not resumed")
                label = urlparse(request.full_url).path.split("/")[-1]
                return Reply(contents[label], {"x-amz-version-id": "exact"})
            response = asyncio.run_coroutine_threadsafe(
                client.post(request.full_url, content=request.data, headers=dict(request.headers)), loop
            ).result(5)
            if response.status_code != 200:
                raise ValueError(f"restore route returned {response.status_code}")
            return Reply(response.content)

        monkeypatch.setattr("crew_checkpoint_helper.ssl.create_default_context", lambda **kwargs: object())
        monkeypatch.setattr("crew_checkpoint_helper.Path.read_text", lambda self, **kwargs: "helper-token")
        environment = {
            "ADP_CHECKPOINT_ID": checkpoint.id,
            "ADP_OWNER_KEY": checkpoint.owner_key,
            "ADP_CHAT_DATA_URL": "https://chat-sandbox-gateway.adp-gateway.svc:8443",
            "KIROCREW_HOME": "/mnt/crew",
            "ADP_WORKLOAD_TOKEN_FILE": "/var/run/adp-checkpoint/token",
            "NODE_EXTRA_CA_CERTS": "/var/run/adp-chat-ca/ca.crt",
        }
        task = asyncio.create_task(asyncio.to_thread(run_restore, environment, opener=opener, home=mounted_home))
        try:
            assert await asyncio.to_thread(paused.wait, 5)
            async with factory() as session:
                record = await session.scalar(select(CrewCheckpoint).where(CrewCheckpoint.id == checkpoint.id))
                assert record.restore_pins == 1 and record.restore_pod_uid == "pod-uid"
            async with factory.begin() as session:
                await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id="newer"))
            assert (await client.delete(f"/v1/crew/checkpoints/{checkpoint.id}")).status_code == 409
        finally:
            resume.set()
        assert (await task)["status"] == "restored"
        assert (mounted_home / "lessons.jsonl").read_bytes() == b'{"provenance":"owner-approved"}\n'
        assert (await client.delete(f"/v1/crew/checkpoints/{checkpoint.id}")).status_code == 200
        assert s3.deletes == [
            ("fixture-bucket", f"crew-checkpoints/{checkpoint.owner_key}/{checkpoint.id}/{label}", "exact") for label in ("archive", "manifest")
        ]


async def test_coverage_after_volume_loss_before_next_checkpoint_only_promises_v1(context, tmp_path):
    from shutil import rmtree

    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[current_crew_owner] = lambda: owner
    app.dependency_overrides[routes.get_store] = lambda: store
    volume = tmp_path / "old-volume"
    volume.mkdir()
    state = volume / "lessons.jsonl"
    state.write_bytes(b'{"provenance":"explicit-v1"}\n')
    independent_v1 = hashlib.sha256(state.read_bytes()).hexdigest()
    first = await store.admit(owner, "first-verified", expected_predecessor_id=None, now=100)
    captured_at = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    async with factory.begin() as session:
        await session.execute(
            update(CrewCheckpoint)
            .where(CrewCheckpoint.id == first.id)
            .values(status="verified", captured_at=captured_at, verified_at=captured_at + timedelta(minutes=1))
        )
        await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id=first.id))
    second = await store.admit(owner, "second-unsynced", expected_predecessor_id=first.id, now=100)
    state.write_bytes(b'{"provenance":"unsynced-v2"}\n')
    assert hashlib.sha256(state.read_bytes()).hexdigest() != independent_v1
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        pending = await client.get("/v1/crew/checkpoints/coverage")
        assert pending.status_code == 200 and pending.headers["cache-control"] == "no-store"
        assert pending.json()["coverage"] == "last_verified_capture"
        assert pending.json()["last_verified_checkpoint_id"] == first.id
        assert pending.json()["pending_checkpoint_id"] == second.id
        assert pending.json()["pending_status"] == "uploading"
        assert pending.json()["unsynced_writes_durable"] is False
        assert pending.json()["local_state_examined"] is False
        assert pending.json()["covered_through"] == "2026-10-12T10:00:00+00:00"
        assert pending.json()["verified_at"] == "2026-10-12T10:01:00+00:00"
        rmtree(volume)
        replacement = tmp_path / "replacement-volume"
        replacement.mkdir()
        assert not list(replacement.iterdir())
        assert (await client.get("/v1/crew/checkpoints/coverage")).json() == pending.json()
        async with factory.begin() as session:
            await session.execute(
                update(CrewCheckpoint).where(CrewCheckpoint.id == second.id).values(status="failed", failure_class="verification_failed")
            )
        failed = (await client.get("/v1/crew/checkpoints/coverage")).json()
        assert failed["last_verified_checkpoint_id"] == first.id and failed["pending_status"] == "failed"
        app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-b", "user-b")
        foreign = (await client.get("/v1/crew/checkpoints/coverage")).json()
        assert foreign["coverage"] == "none" and foreign["last_verified_checkpoint_id"] is None
        assert foreign["pending_checkpoint_id"] is None


async def test_container_ec2_container_handoff_keeps_owner_selected_identity(context):
    from src.crew.inventory import CrewExecutorInventory

    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[current_crew_owner] = lambda: owner
    app.dependency_overrides[routes.get_store] = lambda: store
    archive, manifest = fixture()
    objects = {"archive": archive, "manifest": manifest}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        none = await client.get("/v1/crew/checkpoints/selection")
        assert none.status_code == 200 and none.json()["checkpoint_status"] == "none"
        assert none.json()["checkpoint_id"] is None
        first = await store.admit(owner, "eks-capture", expected_predecessor_id=None, now=100)
        captured_at = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
        async with factory.begin() as session:
            await session.execute(
                update(CrewCheckpoint)
                .where(CrewCheckpoint.id == first.id)
                .values(
                    status="verified",
                    crew_version="0.11.2",
                    schema_version=1,
                    captured_at=captured_at,
                    verified_at=captured_at + timedelta(minutes=1),
                    **{
                        f"{label}_{field}": value
                        for label, data in objects.items()
                        for field, value in (
                            ("bucket", "fixture-bucket"),
                            ("key", f"crew-checkpoints/{first.owner_key}/{first.id}/{label}"),
                            ("version_id", "recorded-v1"),
                            ("sha256", hashlib.sha256(data).hexdigest()),
                            ("size", len(data)),
                        )
                    },
                )
            )
            await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id=first.id))
        eks = (await client.get("/v1/crew/checkpoints/selection")).json()
        assert eks["checkpoint_status"] == "verified" and eks["checkpoint_id"] == first.id
        assert eks["source_target_kind"] == eks["current_target_kind"] == "eks-pod"
        assert eks["source_generation"] == eks["current_generation"] == 1
        assert eks["captured_at"] == "2026-10-12T10:00:00+00:00"
        assert eks["activation_verified"] is False
        assert (await client.get("/v1/crew/checkpoints/selection")).headers["cache-control"] == "no-store"
        assert not any(
            secret in eks for secret in ("owner_key", "archive_key", "manifest_key", "archive_version_id", "manifest_version_id", "sha256")
        )
        async with factory.begin() as session:
            await session.execute(update(CrewExecutor).values(state="deleted"))
        replacement = await CrewExecutorInventory(factory).ensure(tenant_id=owner.tenant_id, user_id=owner.user_id)
        assert replacement.latest_verified_checkpoint_id == first.id
        async with factory.begin() as session:
            await session.execute(
                update(CrewExecutor)
                .where(CrewExecutor.id == replacement.id)
                .values(target_kind="ec2-instance", state="ready", lease_generation=2, lease_expires_at=4_000_000_000)
            )
        ec2_pending = await store.admit(owner, "ec2-pending", expected_predecessor_id=first.id, now=100)
        ec2 = (await client.get("/v1/crew/checkpoints/selection")).json()
        assert {
            field: ec2[field] for field in ("checkpoint_id", "sequence", "captured_at", "verified_at", "source_target_kind", "source_generation")
        } == {field: eks[field] for field in ("checkpoint_id", "sequence", "captured_at", "verified_at", "source_target_kind", "source_generation")}
        assert ec2["current_target_kind"] == "ec2-instance" and ec2["current_generation"] == 2
        assert ec2["checkpoint_id"] != ec2_pending.id
        async with factory.begin() as session:
            await session.execute(
                update(CrewCheckpoint).where(CrewCheckpoint.id == ec2_pending.id).values(status="failed", failure_class="verification_failed")
            )
        assert (await client.get("/v1/crew/checkpoints/selection")).json() == ec2
        app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-b", "user-a")
        foreign = (await client.get("/v1/crew/checkpoints/selection")).json()
        assert foreign["checkpoint_id"] is None and foreign["checkpoint_status"] == "none"
        app.dependency_overrides[current_crew_owner] = lambda: owner
        async with factory.begin() as session:
            await session.execute(update(CrewExecutor).where(CrewExecutor.id == replacement.id).values(state="deleted"))
        back_to_eks = await CrewExecutorInventory(factory).ensure(tenant_id=owner.tenant_id, user_id=owner.user_id)
        assert back_to_eks.latest_verified_checkpoint_id == first.id
        selected_again = (await client.get("/v1/crew/checkpoints/selection")).json()
        assert selected_again["checkpoint_id"] == first.id
        assert selected_again["current_target_kind"] == "eks-pod" and selected_again["current_generation"] == 3
        async with factory.begin() as session:
            await session.execute(update(CrewCheckpoint).where(CrewCheckpoint.id == first.id).values(manifest_version_id="null"))
        unavailable = (await client.get("/v1/crew/checkpoints/selection")).json()
        assert unavailable["checkpoint_status"] == "none" and unavailable["checkpoint_id"] is None
        async with factory.begin() as session:
            await session.execute(update(CrewCheckpoint).where(CrewCheckpoint.id == first.id).values(manifest_version_id=None))
        unavailable = (await client.get("/v1/crew/checkpoints/selection")).json()
        assert unavailable["checkpoint_status"] == "none" and unavailable["checkpoint_id"] is None


def test_sandbox_transport_admits_the_platform_tool_routes():
    """#8113: the sandbox calls /v1/chat/data/tools/list and /tools/call; the TLS transport matches exact paths (seen live: denied)."""
    from src.agentauth.chat_tls import POST_PATHS

    assert "/v1/chat/data/tools/list" in POST_PATHS
    assert "/v1/chat/data/tools/call" in POST_PATHS
