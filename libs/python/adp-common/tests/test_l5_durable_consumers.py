"""Real PostgreSQL + independent S3 clients exercise the installed storage adapters."""

import asyncio
import copy
import shutil
import socket
import subprocess

import boto3
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from moto import mock_aws
from psycopg2.pool import ThreadedConnectionPool

from adp_platform_common.l5_contributions.assertions import HEADER, sign, verify
from adp_platform_common.l5_contributions.contract import canonical, digest, storage_key
from adp_platform_common.l5_contributions.engine import Conflict, Consumer, Unavailable
from adp_platform_common.l5_contributions.http import receiver_router
from adp_platform_common.l5_contributions.stores import PostgresStore, S3Store


@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("PostgreSQL executables required for consumer durable integration")
    directory = tmp_path_factory.mktemp("l5-consumer-postgres")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    subprocess.run([initdb, "-D", str(directory), "-A", "trust", "-U", "test", "--no-locale"],
                   check=True, capture_output=True)
    subprocess.run([pg_ctl, "-D", str(directory), "-l", str(directory / "server.log"),
                    "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"], check=True, capture_output=True)
    dsn = f"host=127.0.0.1 port={port} user=test dbname=postgres"
    try:
        pool = ThreadedConnectionPool(1, 24, dsn)
        connection = pool.getconn()
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE l5_contribution_records (record_key varchar(256) PRIMARY KEY, "
                           "document jsonb NOT NULL, revision bigint NOT NULL CHECK (revision > 0))")
        connection.commit()
        pool.putconn(connection)
        pool.closeall()
        yield dsn
    finally:
        subprocess.run([pg_ctl, "-D", str(directory), "-m", "immediate", "-w", "stop"],
                       check=True, capture_output=True)


@pytest.fixture(params=["postgres", "s3"])
def stores(request):
    if request.param == "postgres":
        dsn = request.getfixturevalue("postgres")
        pools = []

        def factory():
            pool = ThreadedConnectionPool(1, 24, dsn)
            pools.append(pool)
            return PostgresStore(pool)

        first = factory()
        connection = first.pool.getconn()
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE l5_contribution_records")
        connection.commit()
        first.pool.putconn(connection)
        yield factory
        for pool in pools:
            pool.closeall()
    else:
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="consumer-durable-test")
            client.put_bucket_versioning(Bucket="consumer-durable-test", VersioningConfiguration={"Status": "Enabled"})
            yield lambda: S3Store(boto3.client("s3", region_name="us-east-1"),
                                 bucket="consumer-durable-test", kms_key_id="test-kms-key")


def example(kind="transform_result", destination="product-context"):
    payload = {"schema_version": "1.0", "kind": kind, "contribution_id": "contribution-1",
        "scope": {"tenant_id": "tenant-1", "product_id": "product-1", "project_id": "project-1",
                  "relationship_revision": "1", "owner_id": "owner-1", "visibility": "only_me"},
        "result_ref": {"tenant_id": "tenant-1", "project_id": "project-1", "source_connection_id": "l5-transform",
                       "record_type": kind, "record_id": "result-1", "record_revision": "2"},
        "run_id": "run-1", "result": {"original": "frozen"}, "source_refs": [{"revision": "pinned"}],
        "evidence_refs": [{"sha256": "immutable"}]}
    if kind == "security_result":
        payload.pop("kind")
        payload.update(claims=[{"finding_id": str(index), "evidence": [{"revision": str(index)}]}
                               for index in range(101)], manifest_ref={"revision": "captured"})
        payload["evidence"] = [item for claim in payload["claims"] for item in claim["evidence"]]
    authority = {key: copy.deepcopy(payload[key]) for key in ("scope", "result_ref", "contribution_id", "run_id")}
    authority.update(kind=kind, contribution_hash=digest(payload), destination=destination, action="admit",
                     storage_key=storage_key(payload["scope"], payload["result_ref"], destination, "1.0"))
    return payload, authority


async def allow(_):
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["security_result", "devops_result", "finops_cost_result", "transform_result", "well_architected_result"])
async def test_restart_lost_ack_returns_original_receipt_and_exact_projection(stores, kind):
    payload, authority = example(kind)
    first = Consumer(stores(), allow)
    receipt = await first.admit(payload, authority)
    # A distinct client and pool reads after the original caller loses its ack.
    replacement = Consumer(stores(), allow)
    assert await replacement.admit(payload, authority) == receipt
    assert await replacement.read(authority) == receipt
    assert await replacement.read(authority, projection=True) == {**payload, "contribution_hash": digest(payload)}
    assert receipt["record_refs"] == [payload["result_ref"]]
    assert receipt["claim_count"] == len(payload.get("claims", []))


@pytest.mark.asyncio
async def test_independent_concurrent_admissions_make_one_receipt_and_conflicts_cannot_replace(stores):
    payload, authority = example()
    receipts = await asyncio.gather(*(Consumer(stores(), allow).admit(payload, authority) for _ in range(8)))
    assert all(value == receipts[0] for value in receipts)
    foreign_payload, foreign = copy.deepcopy(payload), copy.deepcopy(authority)
    foreign_payload["contribution_id"] = foreign["contribution_id"] = "alternate-id"
    foreign["contribution_hash"] = digest(foreign_payload)
    with pytest.raises(Conflict):
        await Consumer(stores(), allow).admit(foreign_payload, foreign)
    assert await Consumer(stores(), allow).read(authority, projection=True) == {**payload, "contribution_hash": digest(payload)}


@pytest.mark.asyncio
async def test_security_partial_pages_are_durable_but_invisible_until_whole_digest_matches(stores):
    payload, authority = example("security_result")
    pages = [{**payload, "claims": payload["claims"][start:end],
              "evidence": [item for claim in payload["claims"][start:end] for item in claim["evidence"]],
              "claim_page": {"index": index, "count": 2, "total_claims": 101, "contribution_hash": digest(payload)}}
             for index, (start, end) in enumerate(((0, 100), (100, 101)))]
    assert await Consumer(stores(), allow).admit(pages[0], authority) is None
    with pytest.raises(Unavailable):
        await Consumer(stores(), allow).read(authority, projection=True)
    assert await Consumer(stores(), allow).admit(pages[0], authority) is None
    receipt = await Consumer(stores(), allow).admit(pages[1], authority)
    assert receipt["claim_count"] == 101
    assert receipt["contribution_hash"] == digest(payload)
    assert await Consumer(stores(), allow).read(authority, projection=True) == {**payload, "contribution_hash": digest(payload)}


@pytest.mark.asyncio
async def test_tombstone_precedes_admission_and_monotonic_invalidation_never_resurrects(stores):
    payload, authority = example()
    await Consumer(stores(), allow).admit(payload, authority)
    for watermark in (3, 1, 3, 2):
        receipt = await Consumer(stores(), allow).invalidate({"invalidation_watermark": watermark},
            {**authority, "watermark": watermark, "action": "invalidate"})
        assert receipt["applied_watermark"] == 3
    with pytest.raises(Unavailable):
        await Consumer(stores(), allow).admit(payload, authority)
    with pytest.raises(Unavailable):
        await Consumer(stores(), allow).read(authority, projection=True)
    foreign = {**authority, "contribution_id": "another-contribution", "watermark": 4}
    with pytest.raises(Unavailable):
        await Consumer(stores(), allow).invalidate({"invalidation_watermark": 4}, foreign)


@pytest.mark.asyncio
async def test_legacy_security_single_large_claim_and_empty_first_page(stores):
    payload, authority = example("security_result")
    payload["claims"] = [{"finding_id": "large-claim", "evidence": payload["evidence"]}]
    authority["contribution_hash"] = digest(payload)
    pages = [{**payload, "claims": [] if index == 0 else payload["claims"],
              "evidence": [] if index == 0 else payload["evidence"],
              "claim_page": {"index": index, "count": 2, "total_claims": 1, "contribution_hash": digest(payload)}}
             for index in range(2)]
    assert await Consumer(stores(), allow).admit(pages[0], authority) is None
    receipt = await Consumer(stores(), allow).admit(pages[1], authority)
    assert receipt["claim_count"] == 1 and receipt["contribution_hash"] == digest(payload)


@pytest.mark.asyncio
async def test_task_receipt_is_durable_and_revocation_rechecks_cached_receipts(stores):
    payload, authority = example()
    await Consumer(stores(), allow).admit(payload, authority)
    usage = {key: copy.deepcopy(authority[key]) for key in ("scope", "result_ref", "contribution_id")}
    usage.update(task_id="later-task", actor_id="owner-1", citation="transform_result:result-1@2")
    authority = {**authority, "action": "use", "task_id": "later-task", "actor_id": "owner-1", "usage": usage}
    with pytest.raises(Unavailable):
        await Consumer(stores(), allow).use(usage, authority, read_only=True)
    receipt = await Consumer(stores(), allow).use(usage, authority)
    assert await Consumer(stores(), allow).use(usage, authority, read_only=True) == receipt

    async def revoked(_):
        return False

    with pytest.raises(Unavailable):
        await Consumer(stores(), revoked).use(usage, authority, read_only=True)
    with pytest.raises(Unavailable):
        await Consumer(stores(), allow).use({**usage, "actor_id": "foreign"}, authority)
    await Consumer(stores(), allow).invalidate({"invalidation_watermark": 1}, {**authority, "watermark": 1})
    with pytest.raises(Unavailable):
        await Consumer(stores(), allow).use(usage, authority, read_only=True)


@pytest.mark.asyncio
async def test_revoked_after_storage_read_never_releases_private_projection(stores):
    payload, authority = example()
    await Consumer(stores(), allow).admit(payload, authority)
    calls = 0

    async def revoked_during_read(_):
        nonlocal calls
        calls += 1
        return calls == 1

    with pytest.raises(Unavailable):
        await Consumer(stores(), revoked_during_read).read(authority, projection=True)


def signing():
    key = Ed25519PrivateKey.generate()
    return key, {"test": key.public_key().public_bytes(serialization.Encoding.PEM,
                  serialization.PublicFormat.SubjectPublicKeyInfo).decode()}


def test_assertions_reject_forgery_different_body_path_audience_and_expiry():
    payload, claims = example()
    key, keys = signing()
    token = sign(claims, key=key, key_id="test", method="POST", path="/receiver",
                 body=canonical(payload), destination="product-context", now=100)
    assert verify(token, keys, destination="product-context", method="POST", path="/receiver",
                  body=canonical(payload), now=110)["scope"] == payload["scope"]
    for override in ({"body": b"forged"}, {"path": "/another"}, {"destination": "engineering-insights"}, {"now": 130}):
        arguments = dict(destination="product-context", method="POST", path="/receiver", body=canonical(payload), now=110)
        arguments.update(override)
        with pytest.raises(ValueError):
            verify(token, keys, **arguments)


@pytest.mark.asyncio
async def test_real_http_receiver_requires_signature_and_fresh_authority(stores):
    payload, claims = example()
    key, keys = signing()
    app = FastAPI()
    revoked = False

    def authority(_):
        async def check(_):
            return not revoked
        return check

    app.include_router(receiver_router(consumer="context", store_factory=stores,
                                       keys_factory=lambda: keys, authority_factory=authority))
    base = "/internal/v1/context/l5-transform-contributions"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(base, json=payload)).status_code == 401
        token = sign(claims, key=key, key_id="test", method="POST", path=base,
                     body=canonical(payload), destination="product-context")
        admitted = await client.post(base, content=canonical(payload), headers={HEADER: token})
        assert admitted.status_code == 200
        revoked = True
        replay = await client.post(base, content=canonical(payload), headers={HEADER: token})
        assert replay.status_code == 404 and "scope" not in replay.text
