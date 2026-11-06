"""Verified restore pinning never confuses source provenance with destination ownership."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from runpy import run_path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.crew.checkpoint_objects import COMPATIBLE_CREW_VERSION, VersionedCheckpointObjects
from src.crew.checkpoint_store import CheckpointConflictError, CheckpointMissingError, CrewCheckpointStore
from src.crew.targets import CrewOwner
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxInstance, DevBoxPolicy, DevBoxTemplate
from src.shared.models.organization import Organization, User
from tests.crew.test_checkpoint_objects import S3Fixture, fixture


@pytest.fixture
async def host(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'host-restore.sqlite'}")
    async with engine.begin() as connection:
        for model in (Organization, User, DevBoxPolicy, DevBoxTemplate, CrewExecutor, DevBox, DevBoxInstance, CrewCheckpoint):
            await connection.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    archive, manifest = fixture()
    s3 = S3Fixture()
    async with factory.begin() as session:
        session.add(Organization(id="tenant-a", name="Tenant A", settings={"crew": {"allowed_targets": ["ec2-instance"]}}))
        session.add(
            DevBoxPolicy(
                id="policy",
                org_id="tenant-a",
                deployment_id="deployment",
                enabled=True,
                max_live_boxes=1,
                max_running_boxes=1,
                allowed_sizes=["small"],
                max_disk_gb=30,
            )
        )
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
                state="provisioning",
                generation=1,
                latest_verified_checkpoint_id="verified-one",
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
                status="starting",
                desired_state="running",
                observed_state="running",
                machine_generation=1,
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
                provider_instance_id="i-example-one",
            )
        )
        session.add(
            CrewCheckpoint(
                id="verified-one",
                executor_id="executor-one",
                tenant_id="tenant-a",
                user_id="user-a",
                owner_key="a" * 52,
                generation=1,
                target_kind="eks-pod",
                sequence=1,
                idempotency_key="capture",
                status="verified",
                crew_version=COMPATIBLE_CREW_VERSION,
                schema_version=1,
                verified_at=datetime.now(UTC),
                archive_bucket="fixture-bucket",
                archive_key=f"crew-checkpoints/{'a' * 52}/verified-one/archive",
                archive_version_id="archive-v1",
                archive_sha256=hashlib.sha256(archive).hexdigest(),
                archive_size=len(archive),
                manifest_bucket="fixture-bucket",
                manifest_key=f"crew-checkpoints/{'a' * 52}/verified-one/manifest",
                manifest_version_id="manifest-v1",
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                manifest_size=len(manifest),
            )
        )
    for label, data, version in (("archive", archive, "archive-v1"), ("manifest", manifest, "manifest-v1")):
        s3.objects[("fixture-bucket", f"crew-checkpoints/{'a' * 52}/verified-one/{label}", version)] = data
    try:
        yield CrewCheckpointStore(factory), factory, s3
    finally:
        await engine.dispose()


async def test_pin_survives_restart_and_replacement_without_rewriting_verified_source(host, tmp_path):
    store, factory, s3 = host
    owner = CrewOwner("tenant-a", "user-a")
    scope = {"box_id": "owned-box", "generation": 1, "deployment_id": "deployment"}
    source, token = await store.start_host_restore(owner, "verified-one", **scope)
    assert (source.target_kind, source.generation, source.archive_version_id, source.manifest_version_id) == (
        "eks-pod",
        1,
        "archive-v1",
        "manifest-v1",
    )
    async with factory() as session:
        record = await session.get(CrewCheckpoint, "verified-one")
        assert record.restore_pins == 1
        assert (record.restore_box_id, record.restore_machine_generation) == ("owned-box", 1)
        assert token not in record.restore_token_hash
    grant = VersionedCheckpointObjects(s3, "fixture-bucket").grant_read(source)
    restore_empty_mount = run_path(str(Path(__file__).resolve().parents[3] / "agent-factory/agent/scripts/crew_checkpoint.py"))["restore_empty_mount"]
    mounted = tmp_path / "first-machine"
    mounted.mkdir()
    restore_empty_mount(mounted, grant, lambda bucket, key, version: s3.objects[(bucket, key, version)])
    assert (mounted / "lessons.jsonl").read_text() == '{"provenance":"owner-approved"}\n'
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "verified-one", **scope)
    with pytest.raises(CheckpointMissingError):
        await store.end_restore(owner, "verified-one", token)
    with pytest.raises(CheckpointMissingError):
        await store.end_host_restore(owner, "verified-one", box_id="owned-box", generation=2, token=token)
    await store.end_host_restore(owner, "verified-one", box_id="owned-box", generation=1, token=token)
    async with factory.begin() as session:
        box = await session.get(DevBox, "owned-box")
        box.status = "stopped"
        box.observed_state = "stopped"
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "verified-one", **scope)
    async with factory.begin() as session:
        box = await session.get(DevBox, "owned-box")
        box.status = "starting"
        box.observed_state = "running"
    _, restarted_token = await store.start_host_restore(owner, "verified-one", **scope)
    assert (mounted / "lessons.jsonl").read_text() == '{"provenance":"owner-approved"}\n'
    await store.end_host_restore(owner, "verified-one", box_id="owned-box", generation=1, token=restarted_token)
    async with factory.begin() as session:
        previous = await session.get(CrewExecutor, "executor-one")
        previous.state = "deleted"
        box = await session.get(DevBox, "owned-box")
        box.machine_generation = 2
        session.add(
            CrewExecutor(
                id="executor-two",
                tenant_id="tenant-a",
                user_id="user-a",
                owner_key="c" * 52,
                target_kind="ec2-instance",
                dev_box_id="owned-box",
                state="provisioning",
                generation=2,
                latest_verified_checkpoint_id="verified-one",
            )
        )
        session.add(
            DevBoxInstance(
                id="instance-two",
                box_id="owned-box",
                user_id="user-a",
                org_id="tenant-a",
                deployment_id="deployment",
                machine_generation=2,
                template_revision="r1",
                observed_state="running",
                provider_instance_id="i-example-two",
            )
        )
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "verified-one", **scope)
    replacement, token = await store.start_host_restore(owner, "verified-one", box_id="owned-box", generation=2, deployment_id="deployment")
    assert replacement.id == source.id
    assert (replacement.target_kind, replacement.generation, replacement.archive_version_id, replacement.manifest_version_id) == (
        "eks-pod",
        1,
        "archive-v1",
        "manifest-v1",
    )
    replacement_mount = tmp_path / "replacement-machine"
    replacement_mount.mkdir()
    restore_empty_mount(
        replacement_mount,
        VersionedCheckpointObjects(s3, "fixture-bucket").grant_read(replacement),
        lambda bucket, key, version: s3.objects[(bucket, key, version)],
    )
    assert (replacement_mount / "lessons.jsonl").read_text() == (mounted / "lessons.jsonl").read_text()
    async with factory() as session:
        assert await session.scalar(select(CrewCheckpoint.restore_machine_generation).where(CrewCheckpoint.id == "verified-one")) == 2
    await store.end_host_restore(owner, "verified-one", box_id="owned-box", generation=2, token=token)


@pytest.mark.parametrize("candidate_status", ["uploading", "failed"])
async def test_interrupted_candidate_cannot_replace_verified_host_restore(host, candidate_status):
    store, factory, s3 = host
    owner = CrewOwner("tenant-a", "user-a")
    scope = {"box_id": "owned-box", "generation": 1, "deployment_id": "deployment"}
    async with factory.begin() as session:
        session.add(
            CrewCheckpoint(
                id="newer-candidate",
                executor_id="executor-one",
                tenant_id="tenant-a",
                user_id="user-a",
                owner_key="b" * 52,
                generation=1,
                target_kind="ec2-instance",
                sequence=2,
                idempotency_key="interrupted",
                status=candidate_status,
                crew_version=COMPATIBLE_CREW_VERSION,
                schema_version=1,
            )
        )

    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "newer-candidate", **scope)

    source, token = await store.start_host_restore(owner, "verified-one", **scope)
    grant = VersionedCheckpointObjects(s3, "fixture-bucket").grant_read(source)
    assert [(grant[label]["version_id"], grant[label]["key"]) for label in ("archive", "manifest")] == [
        ("archive-v1", f"crew-checkpoints/{'a' * 52}/verified-one/archive"),
        ("manifest-v1", f"crew-checkpoints/{'a' * 52}/verified-one/manifest"),
    ]
    async with factory() as session:
        executor = await session.get(CrewExecutor, "executor-one")
        candidate = await session.get(CrewCheckpoint, "newer-candidate")
        assert executor.latest_verified_checkpoint_id == "verified-one"
        assert (candidate.status, candidate.restore_pins) == (candidate_status, 0)
    await store.end_host_restore(owner, "verified-one", box_id="owned-box", generation=1, token=token)


async def test_nonempty_host_mount_refuses_restore_before_fetch_without_erasing_local_work(host, tmp_path):
    store, factory, s3 = host
    owner = CrewOwner("tenant-a", "user-a")
    source, token = await store.start_host_restore(owner, "verified-one", box_id="owned-box", generation=1, deployment_id="deployment")
    grant = VersionedCheckpointObjects(s3, "fixture-bucket").grant_read(source)
    checkpoint_script = run_path(str(Path(__file__).resolve().parents[3] / "agent-factory/agent/scripts/crew_checkpoint.py"))
    restore_empty_mount = checkpoint_script["restore_empty_mount"]
    mount = tmp_path / "owned-workspace"
    mount.mkdir()
    local_work = mount / "newer-local-work.txt"
    local_work.write_bytes(b"unsaved owner work")
    fetched = []

    def fetch(bucket, key, version):
        fetched.append((bucket, key, version))
        return s3.objects[(bucket, key, version)]

    with pytest.raises(checkpoint_script["InvalidCheckpoint"], match="empty mounted home"):
        restore_empty_mount(mount, grant, fetch)
    assert fetched == []
    assert local_work.read_bytes() == b"unsaved owner work"
    assert list(mount.iterdir()) == [local_work]
    async with factory() as session:
        executor = await session.get(CrewExecutor, "executor-one")
        assert executor.state == "provisioning"
    await store.end_host_restore(owner, "verified-one", box_id="owned-box", generation=1, token=token)


async def test_foreign_owner_with_current_box_cannot_pin_or_release_another_owners_checkpoint(host):
    store, factory, _ = host
    scope = {"box_id": "owned-box", "generation": 1, "deployment_id": "deployment"}
    foreign_scope = {**scope, "box_id": "foreign-box"}
    owner = CrewOwner("tenant-a", "user-a")
    foreign_owner = CrewOwner("tenant-a", "user-b")
    async with factory.begin() as session:
        session.add(User(id="user-b", org_id="tenant-a", team_id="team", email="foreign@example.test"))
        session.add(
            DevBox(
                id="foreign-box",
                user_id="user-b",
                org_id="tenant-a",
                deployment_id="deployment",
                template_id="template",
                template_revision="r1",
                profile="crew",
                status="starting",
                desired_state="running",
                observed_state="running",
                machine_generation=1,
            )
        )
        session.add(
            DevBoxInstance(
                id="foreign-instance",
                box_id="foreign-box",
                user_id="user-b",
                org_id="tenant-a",
                deployment_id="deployment",
                machine_generation=1,
                template_revision="r1",
                observed_state="running",
                provider_instance_id="i-example-foreign",
            )
        )
        session.add(
            CrewExecutor(
                id="foreign-executor",
                tenant_id="tenant-a",
                user_id="user-b",
                owner_key="c" * 52,
                target_kind="ec2-instance",
                dev_box_id="foreign-box",
                state="provisioning",
                generation=1,
                latest_verified_checkpoint_id="verified-one",
            )
        )

    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(foreign_owner, "verified-one", **foreign_scope)
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(foreign_owner, "verified-one", **scope)
    async with factory() as session:
        source = await session.get(CrewCheckpoint, "verified-one")
        assert (source.restore_pins, source.archive_version_id, source.manifest_version_id) == (0, "archive-v1", "manifest-v1")

    _, token = await store.start_host_restore(owner, "verified-one", **scope)
    with pytest.raises(CheckpointMissingError):
        await store.end_host_restore(foreign_owner, "verified-one", box_id="foreign-box", generation=1, token=token)
    async with factory() as session:
        source = await session.get(CrewCheckpoint, "verified-one")
        assert (source.restore_pins, source.restore_box_id, source.restore_machine_generation) == (1, "owned-box", 1)
    await store.end_host_restore(owner, "verified-one", box_id="owned-box", generation=1, token=token)


@pytest.mark.parametrize("retirement", ["executor_deleted", "box_released", "generation_advanced", "instance_stopped"])
async def test_retired_host_cannot_repin_previous_checkpoint(host, retirement):
    store, factory, _ = host
    owner = CrewOwner("tenant-a", "user-a")
    scope = {"box_id": "owned-box", "generation": 1, "deployment_id": "deployment"}
    _, token = await store.start_host_restore(owner, "verified-one", **scope)
    await store.end_host_restore(owner, "verified-one", box_id="owned-box", generation=1, token=token)
    async with factory.begin() as session:
        executor = await session.get(CrewExecutor, "executor-one")
        box = await session.get(DevBox, "owned-box")
        instance = await session.get(DevBoxInstance, "instance-one")
        if retirement == "executor_deleted":
            executor.state = "deleted"
        elif retirement == "box_released":
            box.status = "released"
        elif retirement == "generation_advanced":
            box.machine_generation = 2
        else:
            instance.observed_state = "stopped"

    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "verified-one", **scope)
    if retirement == "generation_advanced":
        with pytest.raises(CheckpointConflictError):
            await store.start_host_restore(owner, "verified-one", **{**scope, "generation": 2})
    async with factory() as session:
        source = await session.get(CrewCheckpoint, "verified-one")
        assert (source.restore_pins, source.restore_box_id, source.restore_machine_generation) == (0, None, None)
        assert (source.archive_version_id, source.manifest_version_id) == ("archive-v1", "manifest-v1")
        assert (await session.get(CrewExecutor, "executor-one")).latest_verified_checkpoint_id == "verified-one"


async def test_pin_refuses_foreign_owner_unverified_candidate_and_unobserved_host(host):
    store, factory, _ = host
    owner = CrewOwner("tenant-a", "user-a")
    scope = {"box_id": "owned-box", "generation": 1, "deployment_id": "deployment"}
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(CrewOwner("tenant-a", "user-b"), "verified-one", **scope)
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "verified-one", **{**scope, "deployment_id": "other"})
    async with factory.begin() as session:
        record = await session.get(CrewCheckpoint, "verified-one")
        record.status = "uploading"
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "verified-one", **scope)
    async with factory.begin() as session:
        record = await session.get(CrewCheckpoint, "verified-one")
        record.status = "verified"
        instance = await session.get(DevBoxInstance, "instance-one")
        instance.observed_state = "unknown"
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "verified-one", **scope)
    async with factory.begin() as session:
        instance = await session.get(DevBoxInstance, "instance-one")
        instance.observed_state = "running"
        instance.template_revision = "mismatched"
    with pytest.raises(CheckpointConflictError):
        await store.start_host_restore(owner, "verified-one", **scope)


def test_host_restore_routes_are_declared_without_the_edge_prefix():
    """The REST API's ``/agent/{proxy+}`` integration strips ``/agent`` before the gateway sees
    the path (``/agent/model/*/invoke`` -> ``/model/...``). Declared with the prefix, the routes
    were unreachable from every Crew host: dev 2026-10-19, 22 bootstraps -> 404 (#8223)."""
    from src.crew import checkpoint_routes

    paths = sorted(route.path for route in checkpoint_routes.router.routes if "host-restore" in route.path)
    assert paths == ["/crew/host-restore/context", "/crew/host-restore/grant", "/crew/host-restore/release"]
    assert not any(route.path.startswith("/agent/") for route in checkpoint_routes.router.routes)
