"""Installed fixture phases use existing SQL debt and durable consumer receipts."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import httpx
import pytest
from adp_platform_common.l5_contributions.contract import storage_key
from adp_platform_common.l5_contributions.engine import Consumer, Unavailable
from adp_platform_common.l5_contributions.stores import S3Store
from l5_services.models import Publication, Run, Work
from l5_services.models.base import Base
from l5_services.publication.installed import (
    KINDS,
    installed_publication_client,
    qualification_cases,
)
from l5_services.workers.consumer_fixture import digest, fixture_once
from moto import mock_aws
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture(params=list(KINDS))
async def installed(request, tmp_path, monkeypatch):
    service_id = request.param
    kind = KINDS[service_id] or "security_result"
    scope = dict(
        tenant_id="tenant",
        product_id="product",
        project_id="project",
        owner_id="owner",
        visibility="only_me",
        relationship_revision="1",
    )
    result_ref = dict(
        tenant_id="tenant",
        project_id="project",
        source_connection_id="l5-test",
        record_type=kind,
        record_id="result",
        record_revision="2",
    )
    payload = dict(
        schema_version="1.0",
        contribution_id="publication",
        scope=scope,
        result_ref=result_ref,
        run_id="run",
        source_refs=[],
        evidence_refs=[],
        result={"original": "retained"},
        goal={},
        cost={},
        invalidation_watermark=0,
    )
    if service_id == "security":
        payload.update(
            claims=[{"finding_id": str(i), "evidence": []} for i in range(101)],
            evidence=[],
            classification="confidential",
        )
    else:
        payload["kind"] = kind
    case = dict(
        service_kind=service_id,
        kind=kind,
        destination="product-context",
        schema_version="1.0",
        contribution_id="publication",
        contribution_hash=digest(payload),
        scope=scope,
        result_ref=result_ref,
        run_id="run",
    )
    monkeypatch.setenv("L5_SERVICE_ID", service_id)
    monkeypatch.setenv("L5_CONSUMER_QUALIFICATION_CASES", json.dumps([case]))
    monkeypatch.delenv("L5_CONSUMER_CONTRACTS", raising=False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(
            Run(
                tenant_id="tenant",
                run_id="run",
                product_id="product",
                project_id="project",
                service_id=service_id,
                configuration_id="config",
                configuration_revision=1,
                binding_id="binding",
                binding_generation=1,
                trigger_id="manual",
                input={"sources": []},
                admission={"scope": scope, "delegation_id": "grant"},
                state="succeeded",
                native_job_ref={"id": "native-original"},
                provider_readback_ref={"artifact": "original"},
                deadline_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db.add(
            Publication(
                tenant_id="tenant",
                publication_id="publication",
                result_id="result",
                result_version=2,
                destination="product-context",
                schema_version="1.0",
                payload=payload,
                contribution_hash=case["contribution_hash"],
                state="pending",
                next_due_at=datetime.now(UTC),
            )
        )
        await db.commit()
    authority = {
        **case,
        "storage_key": storage_key(scope, result_ref, "product-context", "1.0"),
    }
    calls = []

    async def allow(_):
        return True

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="consumer-fixture-test")
        durable = S3Store(s3, bucket="consumer-fixture-test", kms_key_id="key")
        consumer = Consumer(durable, allow)

        async def handle(http_request):
            calls.append(
                (http_request.method, http_request.url.path, dict(http_request.headers))
            )
            try:
                if http_request.method == "GET":
                    value = await consumer.read(
                        authority,
                        projection=http_request.url.path.endswith("/projection"),
                    )
                elif http_request.url.path.endswith("/invalidate"):
                    value = await consumer.invalidate(
                        json.loads(http_request.content), {**authority, "watermark": 2}
                    )
                else:
                    value = await consumer.admit(
                        json.loads(http_request.content), authority
                    )
                return httpx.Response(200 if value is not None else 202, json=value)
            except Unavailable:
                return httpx.Response(404, json={"code": "not_found"})
            except ValueError:
                return httpx.Response(422, json={"code": "rejected"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://gateway.test"
        ) as transport:
            client = installed_publication_client(
                service_kind=service_id,
                destination="product-context",
                transport=transport,
                sessions=sessions,
            )
            prefix = "" if service_id == "security" else service_id + "_"
            service = SimpleNamespace(
                service_id=service_id,
                sessions=sessions,
                **{prefix + "context_client": client},
            )
            yield SimpleNamespace(
                service=service,
                case=case,
                client=client,
                calls=calls,
                consumer=consumer,
                authority=authority,
                sessions=sessions,
            )
    await engine.dispose()


def event(installed, phase):
    return {
        "l5_consumer_fixture": 1,
        "phase": phase,
        "cases_digest": digest([installed.case]),
    }


async def test_all_phases_retain_native_work_and_original_receipt(installed):
    original = await fixture_once(installed.service, event(installed, "admit"))
    replay = await fixture_once(installed.service, event(installed, "replay"))
    assert replay["observations"] == original["observations"]
    # Security's second page must use its original per-page idempotency key.
    if installed.service.service_id == "security":
        keys = {
            headers["idempotency-key"]
            for method, path, headers in installed.calls
            if method == "POST"
        }
        assert any(key.endswith("/pages/0") for key in keys)
        assert any(key.endswith("/pages/1") for key in keys)
    async with installed.sessions() as db:
        row = await db.get(Publication, ("tenant", "publication"))
        # Test setup represents the real revocation transaction. The handler
        # itself must refuse to invent this tombstone or change the source.
        row.state = "invalidation_pending"
        row.invalidation_watermark = 2
        row.invalidation = {"source_refs": [], "reason": "source_revoked"}
        await db.commit()
    invalidated = await fixture_once(installed.service, event(installed, "invalidate"))
    assert invalidated["observations"][0]["denied_status"] == 422
    late = await fixture_once(installed.service, event(installed, "late-replay"))
    assert late["observations"][0]["denied_status"] == 404
    assert late["observations"][0]["receipt"] == original["observations"][0]["receipt"]
    assert (
        late["observations"][0]["native_fingerprint"]
        == original["observations"][0]["native_fingerprint"]
    )
    async with installed.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Run)) == 1
        assert await db.scalar(select(func.count()).select_from(Work)) == 0


async def test_different_selection_and_no_revocation_never_issue_requests(installed):
    changed = {**event(installed, "admit"), "cases_digest": "a" * 64}
    with pytest.raises(ValueError):
        await fixture_once(installed.service, changed)
    with pytest.raises(ValueError, match="revocation"):
        await fixture_once(installed.service, event(installed, "invalidate"))
    assert installed.calls == []
    async with installed.sessions() as db:
        row = await db.get(Publication, ("tenant", "publication"))
        row.payload = {
            **row.payload,
            "scope": {**row.payload["scope"], "product_id": "foreign"},
        }
        await db.commit()
    with pytest.raises(ValueError, match="selected durable"):
        await fixture_once(installed.service, event(installed, "admit"))
    assert installed.calls == []


async def test_case_validation_and_scope_filter_do_not_expand(installed, monkeypatch):
    case = deepcopy(installed.case)
    for field in case:
        malformed = {key: value for key, value in case.items() if key != field}
        with pytest.raises(ValueError):
            qualification_cases(
                [malformed], installed.service.service_id, "product-context"
            )
    monkeypatch.setenv("L5_SERVICE_ID", "different-worker")
    with pytest.raises(ValueError, match="installed service"):
        installed_publication_client(
            service_kind=installed.service.service_id,
            destination="product-context",
            transport=None,
            sessions=None,
        )
    async with installed.sessions() as db:
        row = await db.get(Publication, ("tenant", "publication"))
        assert installed.client.can_publish(row)
        row.contribution_hash = "b" * 64
        assert not installed.client.can_publish(row)
