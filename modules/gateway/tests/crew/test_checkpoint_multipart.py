"""An interrupted S3 multipart upload cannot replace the selected checkpoint."""

import base64
import hashlib

import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.crew.checkpoint_identity import CheckpointHelperIdentity
from src.crew.checkpoint_objects import VersionedCheckpointObjects, checkpoint_s3_client
from src.crew.checkpoint_store import CrewCheckpointStore
from src.crew.targets import CrewOwner
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_executor import CrewExecutor
from tests.crew.test_checkpoint_objects import fixture


@pytest.mark.asyncio
async def test_aborted_multipart_leaves_previous_pointer_until_verified_retry(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'multipart.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(CrewExecutor.__table__.create)
        await connection.run_sync(CrewCheckpoint.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(
            CrewExecutor(
                id="executor",
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
    store = CrewCheckpointStore(factory, retention_seconds=3600)
    with mock_aws():
        s3 = checkpoint_s3_client("us-east-1")  # the route's SigV4 client (#8223 cause 45)
        s3.create_bucket(Bucket="fixture-bucket")
        s3.put_bucket_versioning(Bucket="fixture-bucket", VersioningConfiguration={"Status": "Enabled"})
        storage = VersionedCheckpointObjects(s3, "fixture-bucket")
        archive, manifest = fixture()
        contents = {"archive": archive, "manifest": manifest}
        uploads = {label: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for label, data in contents.items()}

        async def candidate_for(key, predecessor):
            candidate = await store.admit(CrewOwner("tenant-a", "user-a"), key, expected_predecessor_id=predecessor, now=100)
            identity = CheckpointHelperIdentity(
                "pod-uid", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id
            )
            grant = await store.grant(candidate.id, identity, uploads, storage, now=100)
            return candidate, identity, grant

        def put_versions(grant):
            receipts = {}
            for label, data in contents.items():
                version = s3.put_object(
                    Bucket="fixture-bucket",
                    Key=grant[label]["key"],
                    Body=data,
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(uploads[label]["sha256"])).decode(),
                )
                receipts[label] = {**uploads[label], "bucket": "fixture-bucket", "key": grant[label]["key"], "version_id": version["VersionId"]}
            return receipts

        previous, previous_identity, previous_grant = await candidate_for("previous", None)
        previous_receipts = put_versions(previous_grant)
        assert (await store.complete(previous.id, previous_identity, previous_receipts, storage, now=100)).status == "verified"
        candidate, identity, grant = await candidate_for("multipart-attempt", previous.id)
        key = grant["archive"]["key"]
        partial = s3.create_multipart_upload(Bucket="fixture-bucket", Key=key)
        s3.upload_part(Bucket="fixture-bucket", Key=key, UploadId=partial["UploadId"], PartNumber=1, Body=archive[:1024])
        assert s3.list_multipart_uploads(Bucket="fixture-bucket")["Uploads"][0]["UploadId"] == partial["UploadId"]
        s3.abort_multipart_upload(Bucket="fixture-bucket", Key=key, UploadId=partial["UploadId"])
        assert "Uploads" not in s3.list_multipart_uploads(Bucket="fixture-bucket")
        assert "Versions" not in s3.list_object_versions(Bucket="fixture-bucket", Prefix=key)

        incomplete_receipts = {
            label: {**uploads[label], "bucket": "fixture-bucket", "key": grant[label]["key"], "version_id": "uncommitted-multipart"}
            for label in contents
        }
        with pytest.raises(ClientError):
            await store.complete(candidate.id, identity, incomplete_receipts, storage, now=100)
        async with factory() as session:
            assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == previous.id
            assert (await session.scalar(select(CrewCheckpoint).where(CrewCheckpoint.id == candidate.id))).status == "uploading"

        retry = await store.grant(candidate.id, identity, uploads, storage, now=100)
        assert retry["archive"]["key"] == key
        receipts = put_versions(retry)
        verified = await store.complete(candidate.id, identity, receipts, storage, now=100)
        assert verified.status == "verified" and verified.archive_version_id == receipts["archive"]["version_id"]
        async with factory() as session:
            assert await session.scalar(select(CrewExecutor.latest_verified_checkpoint_id)) == candidate.id
    await engine.dispose()
