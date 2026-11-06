"""Knowledge admission pins Product receipts and cannot activate native writes."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from l5_services.api.configuration import view
from l5_services.models.binding import Binding
from l5_services.models.preparation import AdmissionPreparation
from l5_services.models.setup import SetupIntent, SetupWork
from l5_services.workers.bindings import execute_setup_work
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.products.models import ProductSourceBinding, ProductSourceSnapshot
from src.products.source_snapshots import ArtifactRef, CapturedSource
from src.shared.identity.product_scope import current_l5_parent
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["consent_fixture", "l5_owner"]


@pytest.fixture
async def knowledge_fixture(consent_fixture, db_session, l5_owner, monkeypatch):
    fixture = consent_fixture
    config, version = fixture["configurations"]["devops"]
    source_credential = UserCredential(
        id=str(uuid4()),
        org_id=l5_owner.org_id,
        user_id=l5_owner.user_id,
        service="document",
        credential_type="api_key",
        label="Source fixture",
        secret_arn="offline-test-source",
    )
    reference = {
        **fixture["reference"],
        "source_connection_id": source_credential.id,
        "record_type": "document",
        "record_id": "document-a",
        "record_revision": "r1",
    }
    policy = {
        "source_refs": [reference],
        "visibility": "product_team",
        "publish_results": False,
        "refresh": "manual",
        "publications": [
            {"source_ref": reference, "kind": "instruction", "scope": "agent", "active": True, "native_metadata": {"agent_type": "CHAT"}}
        ],
    }
    version.settings = {
        **version.settings,
        "enabled": False,
        "visibility": "product_team",
        "devops": {**version.settings["devops"], "knowledge_policy": policy},
    }
    binding = Binding(
        tenant_id=l5_owner.org_id,
        binding_id=str(uuid4()),
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id="devops",
        configuration_id=config.configuration_id,
        scope=view(config, version)["scope"],
        revision=3,
        generation=2,
        state="ready",
        ownership="customer_linked",
        connection_ref=fixture["reference"],
        connection_key="a" * 64,
        native_key="b" * 64,
        gaps=[],
        readiness=[],
        native_resource_ref={**fixture["reference"], "record_type": "agent_space", "record_id": "native-space"},
        provider_identity={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Fixture", "region": "us-east-1"},
    )
    source_binding = ProductSourceBinding(
        tenant_id=l5_owner.org_id,
        project_id=fixture["project_id"],
        credential_id=source_credential.id,
        kind="document",
        provider="document",
        namespace="examples",
        native_id="document-a",
        desired={"source_revision": "r1"},
        observed={"source_revision": "r1"},
        status="healthy",
    )
    db_session.add(source_credential)
    await db_session.flush()
    db_session.add_all((binding, source_binding))
    await db_session.commit()

    async def capture(_binding, _mapping, request, _objects):
        assert request.source_ref.model_dump() == reference
        return CapturedSource(
            source_ref=request.source_ref,
            exclusions=[],
            file_count=1,
            expires_at=request.retain_until,
            content=ArtifactRef(
                artifact_id="captured-content", version="original-version", sha256=sha256(b"original").hexdigest(), media_type="text/plain", bytes=8
            ),
            file_manifest=ArtifactRef(
                artifact_id="captured-manifest", version="original-version", sha256="b" * 64, media_type="application/json", bytes=20
            ),
        )

    captured = AsyncMock(side_effect=capture)
    monkeypatch.setattr("src.products.l5_admission_capture.capture_source", captured)
    monkeypatch.setattr("src.products.l5_admission_capture.get_snapshot_objects", lambda: object())
    return {
        **fixture,
        "path": fixture["path"] + "/devops",
        "binding": binding,
        "source_binding": source_binding,
        "source_ref": reference,
        "policy": policy,
        "capture": captured,
        "version": version,
    }


async def consent(client, fixture):
    params = {"binding_id": fixture["binding"].binding_id, "for_control": "true", "control_action": "devops_knowledge_publication"}
    preview = await client.get(fixture["path"] + "/delegation-scope", params=params)
    assert preview.status_code == 200, preview.text
    response = await client.post(
        fixture["path"] + "/delegations",
        json={
            **params,
            "for_control": True,
            "scope_digest": preview.json()["scope_digest"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "knowledge_consent_001"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def request(fixture):
    return {
        "action": "sync_knowledge",
        "configuration_revision": 1,
        "binding_id": fixture["binding"].binding_id,
        "binding_revision": 3,
        "binding_generation": 2,
    }


async def submit(client, fixture, body=None, key="knowledge_request_001", etag='"3"'):
    return await client.post(
        fixture["path"] + "/binding-operations",
        json=body or request(fixture),
        headers={"Idempotency-Key": key, **({"If-Match": etag} if etag is not None else {})},
    )


async def test_admits_original_receipts_once_with_actionable_block_and_no_provider_calls(test_client, db_session, test_engine, knowledge_fixture):
    fixture = knowledge_fixture
    grant_view = await consent(test_client, fixture)
    assert "credential:session" not in grant_view["operations"]
    grant = await db_session.get(CredentialServiceDelegation, grant_view["delegation_id"])
    assert await current_l5_parent(db_session, grant)
    response = await submit(test_client, fixture)
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["state"] == "blocked" and result["gaps"] == ["knowledge_worker_unqualified"]
    assert "original operation" in result["next_action"]
    intent = await db_session.get(SetupIntent, (grant.tenant_id, result["operation_id"]))
    admission = intent.authorization["knowledge_admission"]
    snapshots = (await db_session.scalars(select(ProductSourceSnapshot))).all()
    assert len(snapshots) == 1 and admission["source_snapshots"] == [snapshots[0].receipt]
    assert admission["publications"] == fixture["policy"]["publications"]
    assert admission["removals"] == [] and intent.binding_generation == 3
    assert fixture["capture"].await_count == 1
    again = await submit(test_client, fixture)
    assert again.status_code == 202 and again.json() == result
    assert fixture["capture"].await_count == 1
    conflict = await submit(test_client, fixture, {**request(fixture), "binding_revision": 4})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    status = await test_client.get(fixture["path"] + "/binding-operations/" + result["operation_id"])
    assert status.status_code == 200 and status.json() == result
    reconcile = await test_client.post(
        fixture["path"] + "/binding-operations/" + result["operation_id"] + "/reconcile",
        json={"reason": "Inspect original admission"},
        headers={"Idempotency-Key": "knowledge_reconcile_001"},
    )
    assert reconcile.status_code == 403 and reconcile.json()["detail"]["code"] == "knowledge_native_consent_required"
    work = await db_session.scalar(select(SetupWork).where(SetupWork.operation_id == intent.operation_id))
    assert work.blocked_at is not None and work.last_enqueued_at is None
    # Even an erroneously unblocked/redelivered envelope cannot reach credentials.
    work.blocked_at = None
    await db_session.commit()
    authorize = AsyncMock(side_effect=AssertionError("native authorization must not run"))
    assert not await execute_setup_work(
        async_sessionmaker(test_engine, expire_on_commit=False),
        {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation},
        authorize,
    )
    authorize.assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["foreign_binding", "stale_generation", "stale_revision", "missing_etag", "extra_cleanup", "source_revoked", "grant_revoked"]
)
async def test_denies_changed_or_unentitled_admission_before_capture(test_client, db_session, knowledge_fixture, change):
    fixture = knowledge_fixture
    grant = await consent(test_client, fixture)
    body, etag = request(fixture), '"3"'
    if change == "foreign_binding":
        body["binding_id"] = str(uuid4())
    elif change == "stale_generation":
        body["binding_generation"] = 1
    elif change == "stale_revision":
        body["configuration_revision"] = 2
    elif change == "missing_etag":
        etag = None
    elif change == "extra_cleanup":
        body["removals"] = []
    elif change == "source_revoked":
        fixture["source_binding"].status = "revoked"
    else:
        row = await db_session.get(CredentialServiceDelegation, grant["delegation_id"])
        row.revoked_at = datetime.now(UTC)
    await db_session.commit()
    result = await submit(test_client, fixture, body, etag=etag)
    assert result.status_code in {403, 404, 409, 412, 422, 428}, result.text
    fixture["capture"].assert_not_awaited()
    assert await db_session.scalar(select(func.count()).select_from(SetupIntent)) == 0


@pytest.mark.parametrize("change", ["private", "context", "empty", "on_change", "foreign_source", "duplicate_target", "duplicate_source_revision"])
async def test_unqualified_or_ambiguous_policy_cannot_receive_consent(test_client, db_session, knowledge_fixture, change):
    fixture = knowledge_fixture
    policy = deepcopy(fixture["policy"])
    if change == "private":
        policy["visibility"] = "only_me"
    elif change == "context":
        policy["publications"][0]["kind"] = "context"
    elif change == "empty":
        policy["publications"] = []
    elif change == "on_change":
        policy["refresh"] = "on_change"
    elif change == "foreign_source":
        policy["publications"][0]["source_ref"]["project_id"] = "foreign"
    else:
        other = deepcopy(policy["publications"][0])
        if change == "duplicate_source_revision":
            other["source_ref"]["record_revision"] = "r2"
            other["native_metadata"]["agent_type"] = "INCIDENT_RCA"
        else:
            other["source_ref"]["record_id"] = "second-document"
        policy["source_refs"].append(other["source_ref"])
        policy["publications"].append(other)
    fixture["version"].settings = {**fixture["version"].settings, "devops": {**fixture["version"].settings["devops"], "knowledge_policy": policy}}
    await db_session.commit()
    response = await test_client.get(
        fixture["path"] + "/delegation-scope",
        params={"binding_id": fixture["binding"].binding_id, "for_control": "true", "control_action": "devops_knowledge_publication"},
    )
    assert response.status_code == 422, response.text
    fixture["capture"].assert_not_awaited()
    assert await db_session.scalar(select(func.count()).select_from(AdmissionPreparation)) == 0


@pytest.mark.parametrize(
    "change", ["source_revoked", "grant_revoked", "wrong_receipt", "oversize_instruction", "config_changed", "generation_changed"]
)
async def test_capture_boundary_rechecks_authority_and_native_content_bound(test_client, db_session, knowledge_fixture, change):
    fixture = knowledge_fixture
    grant = await consent(test_client, fixture)
    original = fixture["capture"].side_effect

    async def changed_capture(*args):
        result = await original(*args)
        if change == "source_revoked":
            fixture["source_binding"].status = "revoked"
            await db_session.commit()
        elif change == "grant_revoked":
            row = await db_session.get(CredentialServiceDelegation, grant["delegation_id"])
            row.revoked_at = datetime.now(UTC)
            await db_session.commit()
        elif change == "config_changed":
            config, _ = fixture["configurations"]["devops"]
            config.current_revision = 2
            await db_session.commit()
        elif change == "generation_changed":
            fixture["binding"].generation += 1
            fixture["binding"].revision += 1
            await db_session.commit()
        elif change == "wrong_receipt":
            result = result.model_copy(update={"source_ref": result.source_ref.model_copy(update={"record_revision": "wrong"})})
        else:
            result = result.model_copy(update={"content": result.content.model_copy(update={"bytes": 25001})})
        return result

    fixture["capture"].side_effect = changed_capture
    response = await submit(test_client, fixture)
    assert response.status_code in {403, 404, 409, 422}, response.text
    assert await db_session.scalar(select(func.count()).select_from(SetupIntent)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AdmissionPreparation)) == 1


def copied_grant(grant):
    return CredentialServiceDelegation(
        id=str(uuid4()),
        **{
            field: deepcopy(getattr(grant, field))
            for field in (
                "tenant_id",
                "credential_id",
                "service_id",
                "product_id",
                "project_id",
                "created_by",
                "operations",
                "sources",
                "capabilities",
                "visibility",
                "revision",
                "binding_generation",
                "execution_scope",
                "expires_at",
            )
        },
    )


@pytest.mark.parametrize("unrelated", ["legacy_parent", "other_binding"])
async def test_admission_selects_exact_grant_despite_preceding_current_parent(test_client, db_session, knowledge_fixture, unrelated):
    fixture = knowledge_fixture
    consent_view = await consent(test_client, fixture)
    earlier = await db_session.get(CredentialServiceDelegation, consent_view["delegation_id"])
    selected = copied_grant(earlier)
    if unrelated == "legacy_parent":
        earlier.execution_scope = None
        earlier.operations = ["credential:session", "product:read"]
        earlier.capabilities = ["investigation"]
    else:
        original = fixture["binding"]
        other = Binding(
            tenant_id=original.tenant_id,
            binding_id=str(uuid4()),
            **{
                field: deepcopy(getattr(original, field))
                for field in (
                    "configuration_id",
                    "product_id",
                    "project_id",
                    "service_id",
                    "scope",
                    "revision",
                    "generation",
                    "state",
                    "ownership",
                    "connection_ref",
                    "connection_key",
                    "provider_identity",
                    "readiness",
                    "gaps",
                )
            },
            native_key="c" * 64,
            native_resource_ref={**original.native_resource_ref, "record_id": "other-space"},
        )
        db_session.add(other)
        scope = deepcopy(earlier.execution_scope)
        scope["binding_id"] = other.binding_id
        scope["knowledge_selection"]["native_space_ref"] = other.native_resource_ref
        earlier.execution_scope = scope
    db_session.add(selected)
    await db_session.commit()
    assert await current_l5_parent(db_session, earlier)
    assert await current_l5_parent(db_session, selected)
    response = await submit(test_client, fixture)
    assert response.status_code == 202, response.text
    intent = await db_session.get(SetupIntent, (selected.tenant_id, response.json()["operation_id"]))
    assert intent.authorization["delegation_id"] == selected.id


async def test_ambiguous_exact_grants_deny_new_admission_but_cannot_replace_replay_consent(test_client, db_session, knowledge_fixture):
    fixture = knowledge_fixture
    consent_view = await consent(test_client, fixture)
    original = await db_session.get(CredentialServiceDelegation, consent_view["delegation_id"])
    other = copied_grant(original)
    db_session.add(other)
    await db_session.commit()
    refused = await submit(test_client, fixture)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "knowledge_publication_consent_ambiguous"
    fixture["capture"].assert_not_awaited()
    other.revoked_at = datetime.now(UTC)
    await db_session.commit()
    first = await submit(test_client, fixture)
    assert first.status_code == 202, first.text
    other.revoked_at = None
    await db_session.commit()
    replay = await submit(test_client, fixture)
    assert replay.status_code == 202 and replay.json() == first.json()
    original.revoked_at = datetime.now(UTC)
    await db_session.commit()
    denied = await submit(test_client, fixture)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "knowledge_publication_consent_required"
    assert fixture["capture"].await_count == 1
