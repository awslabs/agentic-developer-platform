"""Versioned S3 receipts are independent of optional transcript logging."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from src.orchestration.provider_quotes import ProviderQuote, TrustedUsage
from src.usage.metering_journal import ImmutableMeteringJournal

# These tests assert evidence semantics, not Moto/thread-pool latency on CI.
_JOURNAL_TEST_TIMEOUT_SECONDS = 10


@pytest.fixture
def s3_journal():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_bucket(Bucket="metering-test")
        client.put_bucket_versioning(Bucket="metering-test", VersioningConfiguration={"Status": "Enabled"})
        yield client, ImmutableMeteringJournal("metering-test", region="us-east-1", client=client)


@pytest.fixture
def quote():
    now = datetime.now(UTC)
    return ProviderQuote(
        provider="bedrock",
        endpoint="/openai/v1/responses",
        capability="bounded_text",
        billing_model_id="openai.gpt-6-astra",
        request_sha256="a" * 64,
        pricing_revision="revision-test",
        currency="USD",
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        max_input_tokens=10,
        max_output_tokens=20,
        max_tool_tokens=0,
        max_cache_write_tokens=0,
        total_usd=Decimal("0.001234"),
        evidence_source="test-snapshot",
    )


async def admit(journal, quote):
    return await journal.write_admission(
        tenant_id="tenant-1",
        request_id="request-1",
        owner_id="user-1",
        quote=quote,
        submitted_sha256="b" * 64,
        deadline=asyncio.get_running_loop().time() + _JOURNAL_TEST_TIMEOUT_SECONDS,
    )


async def test_admission_is_immutable_and_contains_only_binding_evidence(s3_journal, quote):
    client, journal = s3_journal
    admission = await admit(journal, quote)
    stored = client.get_object(Bucket="metering-test", Key=admission.key, VersionId=admission.version_id)["Body"].read()
    payload = json.loads(stored)
    assert payload["request_sha256"] == "a" * 64
    assert payload["submitted_sha256"] == "b" * 64
    assert payload["bound_usd"] == "0.001234"
    assert "prompt" not in stored.decode()
    assert admission == await admit(journal, quote)
    with pytest.raises(ValueError, match="conflicting immutable metering evidence"):
        await journal.write_admission(
            tenant_id="tenant-1",
            request_id="request-1",
            owner_id="user-2",
            quote=quote,
            submitted_sha256="b" * 64,
            deadline=asyncio.get_running_loop().time() + _JOURNAL_TEST_TIMEOUT_SECONDS,
        )
    versions = client.list_object_versions(Bucket="metering-test", Prefix=admission.key)["Versions"]
    assert len(versions) == 1


async def test_final_usage_requires_trusted_evidence_and_cannot_overwrite(s3_journal, quote):
    client, journal = s3_journal
    admission = await admit(journal, quote)
    deadline = asyncio.get_running_loop().time() + _JOURNAL_TEST_TIMEOUT_SECONDS
    with pytest.raises(ValueError, match="trusted measured usage"):
        await journal.write_final(
            admission=admission,
            usage={"input_tokens": 1, "output_tokens": 2},
            trusted=TrustedUsage.unknown("missing"),
            provider_request_id=None,
            deadline=deadline,
        )
    with pytest.raises(ValueError, match="missing or invalid"):
        await journal.write_final(
            admission=admission,
            usage={"input_tokens": 1},
            trusted=TrustedUsage(1, 2, True),
            provider_request_id=None,
            deadline=deadline,
        )
    with pytest.raises(ValueError, match="trusted usage does not match"):
        await journal.write_final(
            admission=admission,
            usage={"input_tokens": 1, "output_tokens": 2},
            trusted=TrustedUsage(1, 3, True),
            provider_request_id=None,
            deadline=deadline,
        )

    assert client.list_objects_v2(Bucket="metering-test", Prefix=admission.key.removesuffix("admission.json") + "final-usage")["KeyCount"] == 0
    measured = {"input_tokens": 1, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 0}}
    final = await journal.write_final(
        admission=admission,
        usage=measured,
        trusted=TrustedUsage(1, 2, True),
        provider_request_id="provider-1",
        deadline=deadline,
    )
    assert final == await journal.write_final(
        admission=admission,
        usage=measured,
        trusted=TrustedUsage(1, 2, True),
        provider_request_id="provider-1",
        deadline=deadline,
    )
    saved = json.loads(client.get_object(Bucket="metering-test", Key=final.key, VersionId=final.version_id)["Body"].read())
    assert saved["admission_sha256"] == admission.sha256
    assert saved["usage"] == measured
    with pytest.raises(ValueError, match="conflicting immutable metering evidence"):
        await journal.write_final(
            admission=admission,
            usage={"input_tokens": 1, "output_tokens": 3},
            trusted=TrustedUsage(1, 3, True),
            provider_request_id="provider-1",
            deadline=deadline,
        )
    versions = client.list_object_versions(Bucket="metering-test", Prefix=final.key)["Versions"]
    assert len(versions) == 1


async def test_unversioned_admission_is_not_acknowledged(s3_journal, quote):
    client, journal = s3_journal
    client.put_bucket_versioning(Bucket="metering-test", VersioningConfiguration={"Status": "Suspended"})
    with pytest.raises(ValueError, match="requires versioned objects"):
        await admit(journal, quote)


async def test_recovery_discovers_only_the_exact_versioned_final_object(s3_journal, quote):
    _client, journal = s3_journal
    admission = await admit(journal, quote)
    deadline = asyncio.get_running_loop().time() + _JOURNAL_TEST_TIMEOUT_SECONDS
    assert await journal.discover_final(admission, deadline=deadline) is None
    final = await journal.write_final(
        admission=admission,
        usage={"input_tokens": 1, "output_tokens": 2},
        trusted=TrustedUsage(1, 2, True),
        provider_request_id="provider-fixture",
        deadline=deadline,
    )
    assert await journal.discover_final(admission, deadline=deadline) == final
    payload = await journal.read_final(final, deadline=deadline)
    assert payload["usage"] == {"input_tokens": 1, "output_tokens": 2}
    assert payload["admission_version_id"] == admission.version_id


async def test_admission_read_requires_exact_versioned_owned_evidence(s3_journal, quote):
    from dataclasses import replace

    _, journal = s3_journal
    admission = await admit(journal, quote)
    deadline = asyncio.get_running_loop().time() + _JOURNAL_TEST_TIMEOUT_SECONDS
    payload = await journal.read_admission(admission, deadline=deadline)
    assert payload["request_id"] == admission.request_id
    assert payload["submitted_sha256"] == "b" * 64
    with pytest.raises(ValueError, match="object reference"):
        await journal.read_admission(replace(admission, key="https://outside.invalid/evidence"), deadline=deadline)
    with pytest.raises(ValueError, match="checksum changed"):
        await journal.read_admission(replace(admission, sha256="0" * 64), deadline=deadline)
    with pytest.raises(ValueError, match="identity changed"):
        await journal.read_admission(replace(admission, owner_id="another-owner"), deadline=deadline)


async def test_proven_unsent_receipt_is_immutable_and_bound_to_admission(s3_journal, quote):
    client, journal = s3_journal
    admission = await admit(journal, quote)
    deadline = asyncio.get_running_loop().time() + _JOURNAL_TEST_TIMEOUT_SECONDS
    arguments = {
        "revision": 3,
        "producer_epoch": 1,
        "request_sha256": quote.request_sha256,
        "submitted_sha256": "b" * 64,
        "quote_sha256": "c" * 64,
        "destination_sha256": "d" * 64,
    }
    receipt = await journal.write_unsent(admission, deadline=deadline, **arguments)
    assert receipt == await journal.discover_unsent(admission, deadline=deadline)
    payload = await journal.read_unsent(receipt, deadline=deadline)
    assert all(payload[key] == value for key, value in arguments.items())
    assert payload["admission_sha256"] == admission.sha256
    assert payload["admission_version_id"] == admission.version_id
    assert receipt == await journal.write_unsent(admission, deadline=deadline, **arguments)
    with pytest.raises(ValueError, match="conflicting immutable metering evidence"):
        await journal.write_unsent(admission, deadline=deadline, **(arguments | {"quote_sha256": "e" * 64}))
    for changed in (replace(receipt, owner_id="another-owner"), replace(receipt, sha256="0" * 64), replace(receipt, version_id="wrong")):
        with pytest.raises(Exception):
            await journal.read_unsent(changed, deadline=deadline)
    assert client.list_object_versions(Bucket="metering-test", Prefix=receipt.key)["Versions"][0]["VersionId"] == receipt.version_id


async def test_lost_unsent_write_ack_can_be_discovered(s3_journal, quote):
    client, journal = s3_journal
    admission = await admit(journal, quote)
    deadline = asyncio.get_running_loop().time() + _JOURNAL_TEST_TIMEOUT_SECONDS

    class LostAck:
        def put_object(self, **kwargs):
            client.put_object(**kwargs)
            raise TimeoutError("ack lost after immutable write")

    journal._client = LostAck()
    with pytest.raises(TimeoutError, match="ack lost"):
        await journal.write_unsent(
            admission,
            revision=3,
            producer_epoch=1,
            request_sha256=quote.request_sha256,
            submitted_sha256="b" * 64,
            quote_sha256="c" * 64,
            destination_sha256="d" * 64,
            deadline=deadline,
        )
    journal._client = client
    receipt = await journal.discover_unsent(admission, deadline=deadline)
    assert (await journal.read_unsent(receipt, deadline=deadline))["revision"] == 3
