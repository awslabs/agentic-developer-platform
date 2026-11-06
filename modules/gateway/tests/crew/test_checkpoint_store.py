"""Checkpoint helper candidates bind to a current owner generation and pod identity."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.crew import checkpoint_routes
from src.crew.checkpoint_identity import CheckpointHelperIdentity
from src.crew.checkpoint_store import CheckpointConflictError, CheckpointMissingError, CrewCheckpointStore
from src.crew.targets import CrewOwner
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_executor import CrewExecutor


@pytest.fixture
async def context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'checkpoint.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(CrewExecutor.__table__.create)
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


async def test_configured_retention_expires_through_normal_publication(context, monkeypatch):
    from src.crew.checkpoint_objects import VersionedCheckpointObjects
    from tests.crew.test_checkpoint_objects import S3Fixture, fixture

    _, factory = context
    monkeypatch.setenv("ADP_CREW_CHECKPOINT_RETENTION_SECONDS", "3600")
    monkeypatch.setattr(checkpoint_routes, "get_session_factory", lambda: factory)
    monkeypatch.setattr("src.crew.checkpoint_store.utcnow", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    store = checkpoint_routes.get_store()
    owner = CrewOwner("tenant-a", "user-a")
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    archive, manifest = fixture()
    records = []
    for sequence in range(2):
        candidate = await store.admit(owner, f"capture-{sequence}", expected_predecessor_id=records[-1].id if records else None, now=100)
        identity = CheckpointHelperIdentity("pod", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
        objects = {
            label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for label, content in (("archive", archive), ("manifest", manifest))
        }
        grants = await store.grant(candidate.id, identity, objects, storage, now=100)
        for label, content in (("archive", archive), ("manifest", manifest)):
            objects[label].update(bucket=grants[label]["bucket"], key=grants[label]["key"], version_id="first")
            s3.objects[(objects[label]["bucket"], objects[label]["key"], "first")] = content
        record = await store.complete(candidate.id, identity, objects, storage, now=100)
        assert record.retention_until == record.verified_at + timedelta(seconds=3600)
        records.append(record)
        if sequence == 0:
            _, token = await store.start_restore(owner, record.id, owner_key="a" * 52)
    expiry = records[0].retention_until
    assert expiry is not None
    with pytest.raises(CheckpointConflictError):
        await store.request_delete(owner, records[0].id, now=expiry)
    await store.end_restore(owner, records[0].id, token)
    with pytest.raises(CheckpointConflictError):
        await store.request_delete(owner, records[0].id, now=expiry - timedelta(seconds=1))
    with pytest.raises(CheckpointConflictError):
        await store.request_delete(owner, records[1].id, now=expiry + timedelta(days=1))
    monkeypatch.setenv("ADP_CREW_CHECKPOINT_RETENTION_SECONDS", "7200")
    retried = await checkpoint_routes.get_store().complete(records[1].id, identity, objects, storage, now=101)
    assert retried.retention_until.replace(tzinfo=UTC) == records[1].retention_until
    assert await store.request_delete(owner, records[0].id, now=expiry) == "deleting"
    assert await store.reconcile_delete(owner, records[0].id, storage) == "deleted"
    assert await store.reconcile_delete(owner, records[0].id, storage) == "deleted"
    assert all(item[1].split("/")[2] == records[0].id for item in s3.deletes)
    assert len(s3.deletes) == 2


async def test_missing_retention_policy_blocks_new_admission_but_not_reads(context, monkeypatch):
    _, factory = context
    monkeypatch.delenv("ADP_CREW_CHECKPOINT_RETENTION_SECONDS", raising=False)
    monkeypatch.setattr(checkpoint_routes, "get_session_factory", lambda: factory)
    store = checkpoint_routes.get_store()
    owner = CrewOwner("tenant-a", "user-a")
    with pytest.raises(CheckpointConflictError, match="retention"):
        await store.admit(owner, "no-policy", expected_predecessor_id=None, now=100)
    assert await store.list_for_owner(owner) == []


@pytest.mark.parametrize("value", ["", "0", "-1", "invalid", "1.5", "9" * 100])
async def test_invalid_operator_retention_is_unavailable(context, monkeypatch, value):
    from fastapi import HTTPException

    _, factory = context
    monkeypatch.setattr(checkpoint_routes, "get_session_factory", lambda: factory)
    monkeypatch.setenv("ADP_CREW_CHECKPOINT_RETENTION_SECONDS", value)
    with pytest.raises(HTTPException) as failure:
        checkpoint_routes.get_store()
    assert failure.value.status_code == 503
    assert failure.value.detail == {"error": "checkpoint_retention_unavailable"}


async def test_idempotent_admission_and_owner_monotonic_sequence(context):
    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    first = await store.admit(owner, "attempt-one", expected_predecessor_id=None, now=100)
    repeated = await store.admit(owner, "attempt-one", expected_predecessor_id=None, now=100)
    second = await store.admit(owner, "attempt-two", expected_predecessor_id=None, now=100)
    assert (first.id, first.sequence, first.owner_key, first.status) == (repeated.id, 1, "a" * 52, "uploading")
    assert second.sequence == 2
    async with factory() as session:
        assert len((await session.scalars(select(CrewCheckpoint))).all()) == 2


async def test_admission_refuses_foreign_predecessor_stale_and_unbound_owner(context):
    store, factory = context
    with pytest.raises(CheckpointConflictError):
        await store.admit(CrewOwner("tenant-a", "user-b"), "try", expected_predecessor_id=None, now=100)
    with pytest.raises(CheckpointConflictError):
        await store.admit(CrewOwner("tenant-a", "user-a"), "try", expected_predecessor_id="other", now=100)
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).values(lease_generation=2))
    with pytest.raises(CheckpointConflictError):
        await store.admit(CrewOwner("tenant-a", "user-a"), "try", expected_predecessor_id=None, now=100)
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).values(lease_generation=1, owner_key=None))
    with pytest.raises(CheckpointConflictError):
        await store.admit(CrewOwner("tenant-a", "user-a"), "try", expected_predecessor_id=None, now=100)


async def test_helper_candidate_binds_full_pod_digest_and_current_generation(context):
    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    candidate = await store.admit(owner, "try", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod-uid", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    assert (await store.helper_candidate(candidate.id, identity, now=100)).id == candidate.id
    changed_hash = identity.checkpoint_hash[:-1] + ("0" if identity.checkpoint_hash[-1] != "0" else "1")
    with pytest.raises(CheckpointMissingError):
        await store.helper_candidate(
            candidate.id, CheckpointHelperIdentity("pod-uid", identity.owner_key, changed_hash, identity.image_digest, candidate.id), now=100
        )
    with pytest.raises(CheckpointMissingError):
        await store.helper_candidate(
            candidate.id, CheckpointHelperIdentity("pod-uid", "b" * 52, identity.checkpoint_hash, identity.image_digest, candidate.id), now=100
        )
    with pytest.raises(CheckpointMissingError):
        await store.helper_candidate(
            candidate.id, CheckpointHelperIdentity("pod-uid", identity.owner_key, identity.checkpoint_hash, identity.image_digest, "other"), now=100
        )
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).values(lease_generation=2))
    with pytest.raises(CheckpointConflictError):
        await store.helper_candidate(candidate.id, identity, now=100)


async def test_verified_exact_versions_publish_one_owner_pointer(context):
    from src.crew.checkpoint_objects import InvalidCheckpointObjectsError, VersionedCheckpointObjects
    from tests.crew.test_checkpoint_objects import S3Fixture, fixture

    store, factory = context
    candidate = await store.admit(CrewOwner("tenant-a", "user-a"), "capture", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod-uid", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    archive, manifest = fixture()
    objects = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }
    grants = await store.grant(candidate.id, identity, objects, storage, now=100)
    for label, content in (("archive", archive), ("manifest", manifest)):
        objects[label].update(bucket=grants[label]["bucket"], key=grants[label]["key"], version_id="first")
        s3.objects[(objects[label]["bucket"], objects[label]["key"], "first")] = content
    s3.objects[(objects["archive"]["bucket"], objects["archive"]["key"], "first")] = b"corrupt"
    with pytest.raises(InvalidCheckpointObjectsError):
        await store.complete(candidate.id, identity, objects, storage, now=100)
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) is None
        record = await session.scalar(select(CrewCheckpoint))
        assert record.status == "failed" and record.failure_class == "verification_failed" and record.retry_count == 1
    with pytest.raises(CheckpointConflictError):
        await store.complete(candidate.id, identity, objects, storage, now=100)
    await store.grant(
        candidate.id,
        identity,
        {
            label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for label, content in (("archive", archive), ("manifest", manifest))
        },
        storage,
        now=100,
    )
    objects["archive"]["version_id"] = "second"
    s3.objects[(objects["archive"]["bucket"], objects["archive"]["key"], "second")] = archive
    result = await store.complete(candidate.id, identity, objects, storage, now=100)
    assert result.status == "verified" and result.archive_version_id == "second" and result.manifest_version_id == "first"
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == candidate.id
    repeated = await store.complete(candidate.id, identity, objects, storage, now=100)
    assert repeated.id == candidate.id and repeated.status == "verified"


async def test_stale_owner_fence_after_upload_cannot_publish(context):
    from src.crew.checkpoint_objects import VersionedCheckpointObjects
    from tests.crew.test_checkpoint_objects import S3Fixture, fixture

    store, factory = context
    candidate = await store.admit(CrewOwner("tenant-a", "user-a"), "capture", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod-uid", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    archive, manifest = fixture()
    objects = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }
    grant = await store.grant(candidate.id, identity, objects, storage, now=100)
    for label, content in (("archive", archive), ("manifest", manifest)):
        objects[label].update(bucket=grant[label]["bucket"], key=grant[label]["key"], version_id="first")
        s3.objects[(objects[label]["bucket"], objects[label]["key"], "first")] = content
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).values(lease_generation=2))
    with pytest.raises(CheckpointConflictError):
        await store.complete(candidate.id, identity, objects, storage, now=100)
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) is None


async def test_interrupted_upload_never_advances_pointer_and_grant_can_retry(context):
    from src.crew.checkpoint_objects import VersionedCheckpointObjects
    from tests.crew.test_checkpoint_objects import S3Fixture, fixture

    store, factory = context
    candidate = await store.admit(CrewOwner("tenant-a", "user-a"), "interrupted", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod-uid", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    storage = VersionedCheckpointObjects(S3Fixture(), "fixture-bucket")
    archive, manifest = fixture()
    uploads = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }
    grant = await store.grant(candidate.id, identity, uploads, storage, now=100)
    storage.s3.objects[(grant["archive"]["bucket"], grant["archive"]["key"], "partial-attempt")] = archive[:20]
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) is None
        assert await session.scalar(select(CrewCheckpoint.status)) == "uploading"
    repeated_grant = await store.grant(candidate.id, identity, uploads, storage, now=100)
    assert {label: repeated_grant[label]["key"] for label in uploads} == {label: grant[label]["key"] for label in uploads}
    receipts = {}
    for label, content in (("archive", archive), ("manifest", manifest)):
        key = grant[label]["key"]
        storage.s3.objects[(grant[label]["bucket"], key, "complete-attempt")] = content
        receipts[label] = {**uploads[label], "bucket": grant[label]["bucket"], "key": key, "version_id": "complete-attempt"}
    record = await store.complete(candidate.id, identity, receipts, storage, now=100)
    assert record.status == "verified" and record.archive_version_id == "complete-attempt"
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == candidate.id


async def test_uploaded_receipts_survive_db_publication_failure_and_retry(context, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from src.crew.checkpoint_objects import VersionedCheckpointObjects
    from src.crew.inventory import CrewExecutorInventory
    from tests.crew.test_checkpoint_objects import S3Fixture, fixture

    store, factory = context
    candidate = await store.admit(CrewOwner("tenant-a", "user-a"), "reconcile", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod-uid", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    storage = VersionedCheckpointObjects(S3Fixture(), "fixture-bucket")
    archive, manifest = fixture()
    uploads = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }
    grant = await store.grant(candidate.id, identity, uploads, storage, now=100)
    receipts = {}
    for label, content in (("archive", archive), ("manifest", manifest)):
        storage.s3.objects[(grant[label]["bucket"], grant[label]["key"], "uploaded-version")] = content
        receipts[label] = {**uploads[label], "bucket": grant[label]["bucket"], "key": grant[label]["key"], "version_id": "uploaded-version"}
    original = CrewExecutorInventory.lock_current_generation
    attempts = 0

    async def fail_first_publication(inventory, session, binding, *, now):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SQLAlchemyError("injected database publication failure")
        return await original(inventory, session, binding, now=now)

    monkeypatch.setattr(CrewExecutorInventory, "lock_current_generation", fail_first_publication)
    with pytest.raises(SQLAlchemyError, match="injected"):
        await store.complete(candidate.id, identity, receipts, storage, now=100)
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) is None
        assert await session.scalar(select(CrewCheckpoint.status)) == "uploading"
    first = await store.complete(candidate.id, identity, receipts, storage, now=100)
    repeated = await store.complete(candidate.id, identity, receipts, storage, now=100)
    assert attempts == 2 and first.id == repeated.id and repeated.status == "verified"
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == candidate.id
        assert len((await session.scalars(select(CrewCheckpoint))).all()) == 1


async def test_grant_rechecks_generation_after_identity_check(context, tmp_path):
    import sqlite3

    from src.crew.checkpoint_objects import VersionedCheckpointObjects
    from src.crew.inventory import CrewLeaseFencedError
    from tests.crew.test_checkpoint_objects import S3Fixture, fixture

    store, factory = context
    candidate = await store.admit(CrewOwner("tenant-a", "user-a"), "lease-race", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod-uid", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    archive, manifest = fixture()
    uploads = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }

    class RevokingStorage(VersionedCheckpointObjects):
        def grant(self, checkpoint, objects):
            result = super().grant(checkpoint, objects)
            with sqlite3.connect(tmp_path / "checkpoint.sqlite") as connection:
                connection.execute("UPDATE crew_executors SET lease_generation = 2 WHERE id = ?", (candidate.executor_id,))
            return result

    with pytest.raises(CrewLeaseFencedError):
        await store.grant(candidate.id, identity, uploads, RevokingStorage(S3Fixture(), "fixture-bucket"), now=100)
    async with factory() as session:
        record = await session.scalar(select(CrewCheckpoint).where(CrewCheckpoint.id == candidate.id))
        assert record.archive_key is None and record.status == "uploading"


async def test_out_of_order_completion_and_old_generation_cannot_publish(context):
    from src.crew.checkpoint_objects import VersionedCheckpointObjects
    from src.crew.inventory import CrewLeaseFencedError
    from tests.crew.test_checkpoint_objects import S3Fixture, fixture

    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    first = await store.admit(owner, "sequence-one", expected_predecessor_id=None, now=100)
    second = await store.admit(owner, "sequence-two", expected_predecessor_id=None, now=100)
    assert second.sequence == first.sequence + 1
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    archive, manifest = fixture()
    uploads = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }

    async def uploaded(candidate):
        identity = CheckpointHelperIdentity(
            "pod-uid", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id
        )
        grant = await store.grant(candidate.id, identity, uploads, storage, now=100)
        receipt = {}
        for label, content in (("archive", archive), ("manifest", manifest)):
            s3.objects[(grant[label]["bucket"], grant[label]["key"], "v1")] = content
            receipt[label] = {**uploads[label], "bucket": grant[label]["bucket"], "key": grant[label]["key"], "version_id": "v1"}
        return identity, receipt

    first_identity, first_receipts = await uploaded(first)
    second_identity, second_receipts = await uploaded(second)
    assert (await store.complete(second.id, second_identity, second_receipts, storage, now=100)).status == "verified"
    with pytest.raises(CheckpointConflictError):
        await store.complete(first.id, first_identity, first_receipts, storage, now=100)
    with pytest.raises(CheckpointConflictError):
        await store.grant(first.id, first_identity, uploads, storage, now=100)
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == second.id
        assert (await session.scalar(select(CrewCheckpoint).where(CrewCheckpoint.id == first.id))).status == "uploading"
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).values(owner_key="b" * 52, generation=2, lease_generation=2))
    with pytest.raises(CheckpointConflictError):
        await store.complete(second.id, second_identity, second_receipts, storage, now=100)
    with pytest.raises(CheckpointMissingError):
        await store.restore_owner(second.id, second_identity)
    with pytest.raises((CheckpointConflictError, CrewLeaseFencedError)):
        await store.grant(first.id, first_identity, uploads, storage, now=100)
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == second.id


async def test_incompatible_checkpoint_metadata_cannot_pin_restore(context):
    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    candidate = await store.admit(owner, "incompatible", expected_predecessor_id=None, now=100)
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id=candidate.id))
        await session.execute(
            update(CrewCheckpoint)
            .where(CrewCheckpoint.id == candidate.id)
            .values(
                status="verified",
                crew_version="0.7.2",
                schema_version=1,
                archive_bucket="fixture-bucket",
                archive_key="archive",
                archive_version_id="version",
                archive_sha256="a" * 64,
                archive_size=10,
                manifest_bucket="fixture-bucket",
                manifest_key="manifest",
                manifest_version_id="version",
                manifest_sha256="b" * 64,
                manifest_size=10,
            )
        )
    with pytest.raises(CheckpointConflictError, match="cannot be restored"):
        await store.start_restore(owner, candidate.id, owner_key="a" * 52)
    async with factory.begin() as session:
        await session.execute(update(CrewCheckpoint).where(CrewCheckpoint.id == candidate.id).values(crew_version="0.11.2", schema_version=2))
    with pytest.raises(CheckpointConflictError, match="cannot be restored"):
        await store.start_restore(owner, candidate.id, owner_key="a" * 52)
    async with factory() as session:
        record = await session.scalar(select(CrewCheckpoint).where(CrewCheckpoint.id == candidate.id))
        assert record.restore_pins == 0 and record.restore_token_hash is None


async def test_restore_pin_survives_pointer_advance_and_fences_retention(context):
    from datetime import UTC, datetime, timedelta

    from src.crew.checkpoint_objects import VersionedCheckpointObjects
    from tests.crew.test_checkpoint_objects import S3Fixture, fixture

    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    first = await store.admit(owner, "pinned", expected_predecessor_id=None, now=100)
    archive, manifest = fixture()
    s3 = S3Fixture()
    for label, content in (("archive", archive), ("manifest", manifest)):
        s3.objects[("fixture-bucket", f"crew-checkpoints/{first.owner_key}/{first.id}/{label}", "v1")] = content
    async with factory.begin() as session:
        await session.execute(
            update(CrewCheckpoint)
            .where(CrewCheckpoint.id == first.id)
            .values(
                status="verified",
                crew_version="0.11.2",
                schema_version=1,
                retention_until=datetime.now(UTC) - timedelta(days=1),
                archive_bucket="fixture-bucket",
                archive_key=f"crew-checkpoints/{first.owner_key}/{first.id}/archive",
                archive_version_id="v1",
                archive_sha256=hashlib.sha256(archive).hexdigest(),
                archive_size=len(archive),
                manifest_bucket="fixture-bucket",
                manifest_key=f"crew-checkpoints/{first.owner_key}/{first.id}/manifest",
                manifest_version_id="v1",
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                manifest_size=len(manifest),
            )
        )
        await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id=first.id))
    _, token = await store.start_restore(owner, first.id, owner_key="a" * 52)
    second = await store.admit(owner, "newer", expected_predecessor_id=first.id, now=100)
    async with factory.begin() as session:
        await session.execute(update(CrewCheckpoint).where(CrewCheckpoint.id == second.id).values(status="verified"))
        await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id=second.id))
    with pytest.raises(CheckpointConflictError):
        await store.request_delete(owner, first.id)
    identity = CheckpointHelperIdentity("pod-uid", "a" * 52, hashlib.sha256(first.id.encode()).hexdigest(), "sha256:" + "f" * 64, first.id)
    assert await store.restore_owner(first.id, identity, require_selected=False) == owner
    with pytest.raises(CheckpointMissingError):
        await store.restore_owner(first.id, identity)
    await store.end_restore(owner, first.id, token)
    assert await store.request_delete(owner, first.id) == "deleting"
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    assert await store.reconcile_delete(owner, first.id, storage) == "deleted"
    with pytest.raises(CheckpointMissingError):
        await store.complete(first.id, identity, {"archive": {}, "manifest": {}}, storage, now=100)
    async with factory() as session:
        assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == second.id


async def test_only_confirmed_dead_restore_pod_can_recover_a_lost_pin(context):
    from src.crew.checkpoint_identity import CheckpointIdentityUnavailableError

    store, factory = context
    owner = CrewOwner("tenant-a", "user-a")
    candidate = await store.admit(owner, "restore-crash", expected_predecessor_id=None, now=100)
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).values(latest_verified_checkpoint_id=candidate.id))
        await session.execute(
            update(CrewCheckpoint)
            .where(CrewCheckpoint.id == candidate.id)
            .values(
                status="verified",
                crew_version="0.11.2",
                schema_version=1,
                archive_bucket="fixture-bucket",
                archive_key="archive",
                archive_version_id="v1",
                archive_sha256="a" * 64,
                archive_size=10,
                manifest_bucket="fixture-bucket",
                manifest_key="manifest",
                manifest_version_id="v1",
                manifest_sha256="b" * 64,
                manifest_size=10,
            )
        )
    old_pod = "chat-restore-" + hashlib.sha256(candidate.id.encode()).hexdigest()[:12] + "-abcde"
    _, first_token = await store.start_restore(owner, candidate.id, owner_key="a" * 52, pod_uid="pod-old", pod_name=old_pod)
    checks = []

    def still_running(name, uid):
        checks.append((name, uid))
        return False

    with pytest.raises(CheckpointConflictError, match="pin remains"):
        await store.start_restore(owner, candidate.id, owner_key="a" * 52, pod_uid="pod-new", pod_name=old_pod, pod_is_gone=still_running)
    assert checks == [(old_pod, "pod-old")]
    with pytest.raises(CheckpointConflictError, match="pin remains"):
        await store.start_restore(owner, candidate.id, owner_key="a" * 52)

    def unavailable(name, uid):
        raise CheckpointIdentityUnavailableError("kubernetes unavailable")

    with pytest.raises(CheckpointIdentityUnavailableError):
        await store.start_restore(owner, candidate.id, owner_key="a" * 52, pod_uid="pod-new", pod_name=old_pod, pod_is_gone=unavailable)
    _, new_token = await store.start_restore(
        owner,
        candidate.id,
        owner_key="a" * 52,
        pod_uid="pod-new",
        pod_name=old_pod,
        pod_is_gone=lambda name, uid: (name, uid) == (old_pod, "pod-old"),
    )
    assert first_token != new_token
    with pytest.raises(CheckpointMissingError):
        await store.end_restore(owner, candidate.id, first_token)
    async with factory() as session:
        record = await session.scalar(select(CrewCheckpoint).where(CrewCheckpoint.id == candidate.id))
        assert record.restore_pins == 1 and record.restore_pod_uid == "pod-new"
    await store.end_restore(owner, candidate.id, new_token)
    async with factory() as session:
        record = await session.scalar(select(CrewCheckpoint).where(CrewCheckpoint.id == candidate.id))
        assert record.restore_pins == 0 and record.restore_pod_uid is None and record.restore_pod_name is None
