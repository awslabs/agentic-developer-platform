"""Owner consent continuation traverses real API, installed worker and broker."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from l5_services.models.setup import BindingOperation, SetupAuthorityContinuation, SetupIntent, SetupWork
from sqlalchemy import func, select

from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_devops_setup_activation import api_setup, l5_owner, setup_consent  # noqa: F401

CREATE_KEY = "devops_original_creation_0001"
CONTINUE_KEY = "devops_explicit_continuation_01"

__all__ = ["api_setup", "l5_owner"]


async def accepted_setup(client, setup, db):
    consent = await setup_consent(client, setup)
    selected = setup.draft["settings"]["devops"]
    body = {"action": "create", "configuration_revision": 1}
    if selected["space_mode"] == "adopt":
        body.update(
            action="link",
            native_resource_ref={
                **setup.fixture.reference,
                "record_type": "agent_space",
                "record_id": selected["existing_space_id"],
                "record_revision": selected["existing_space_id"],
            },
        )
    response = await client.post(setup.path + "/binding-operations", json=body, headers={"Idempotency-Key": CREATE_KEY})
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    intent = await db.get(SetupIntent, (setup.tenant, operation_id))
    return consent, intent


async def renew(client, setup, db, consent, *, key="devops_fresh_consent_0001"):
    grant = await db.get(CredentialServiceDelegation, consent["delegation_id"], populate_existing=True)
    grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    preview = await client.get(setup.path + "/delegation-scope", params={"for_control": "true", "control_action": "devops_setup"})
    assert preview.status_code == 200, preview.text
    result = await client.post(
        setup.path + "/delegations",
        json={
            "scope_digest": preview.json()["scope_digest"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "for_control": True,
            "control_action": "devops_setup",
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": key},
    )
    assert result.status_code == 200, result.text
    return result.json(), preview.json()


def body(intent, fresh, preview):
    return {
        "action": "continue_unstarted_setup",
        "intent_hash": intent.intent_hash,
        "delegation_id": fresh["delegation_id"],
        "delegation_revision": fresh["revision"],
        "scope_digest": preview["scope_digest"],
        "reason": "Consent expired while consumers were paused.",
    }


async def continue_request(client, setup, intent, payload, *, key=CONTINUE_KEY, etag='"1"'):
    return await client.post(
        setup.path + f"/binding-operations/{intent.operation_id}/authority-continuations",
        json=payload,
        headers={"Idempotency-Key": key, "If-Match": etag},
    )


async def prepared(client, setup, db):
    old, intent = await accepted_setup(client, setup, db)
    original = await client.get(setup.path + f"/binding-operations/{intent.operation_id}/authority-continuations")
    assert original.status_code == 200, original.text
    assert original.json() == {
        "operation_id": intent.operation_id,
        "intent_hash": intent.intent_hash,
        "original_request": intent.request,
        "original_idempotency_key": CREATE_KEY,
        "items": [],
    }
    fresh, preview = await renew(client, setup, db, old)
    return intent, fresh, body(intent, fresh, preview)


@pytest.mark.asyncio
async def test_fresh_consent_alone_does_not_continue_original_create(test_client, api_setup, db_session):
    setup = api_setup
    intent, _, _ = await prepared(test_client, setup, db_session)
    response = await test_client.post(
        setup.path + "/binding-operations", json={"action": "create", "configuration_revision": 1}, headers={"Idempotency-Key": CREATE_KEY}
    )
    assert response.status_code == 409, response.text
    assert await db_session.scalar(select(func.count()).select_from(SetupAuthorityContinuation)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SetupIntent)) == 1
    work = await db_session.scalar(select(SetupWork).where(SetupWork.operation_id == intent.operation_id))
    assert len(setup.messages) == 1 and work.fence == 0
    assert setup.native.mock_calls == [] and setup.assumed.call_count == 0


@pytest.mark.asyncio
async def test_former_owner_cannot_continue_or_read_consent(test_client, api_setup, db_session):
    from l5_services.models.configuration import Configuration

    setup = api_setup
    intent, _, payload = await prepared(test_client, setup, db_session)
    configuration = await db_session.get(Configuration, (setup.tenant, intent.configuration_id))
    configuration.owner_id = "another-owner"
    await db_session.commit()
    response = await continue_request(test_client, setup, intent, payload)
    assert response.status_code == 404, response.text
    history = await test_client.get(setup.path + f"/binding-operations/{intent.operation_id}/authority-continuations")
    assert history.status_code == 404, history.text
    assert await db_session.scalar(select(func.count()).select_from(SetupAuthorityContinuation)) == 0
    assert setup.native.mock_calls == [] and setup.assumed.call_count == 0


@pytest.mark.asyncio
async def test_old_consent_cannot_authorize_continued_work(test_client, api_setup, db_session):
    setup = api_setup
    intent, _, payload = await prepared(test_client, setup, db_session)
    response = await continue_request(test_client, setup, intent, payload)
    assert response.status_code == 201, response.text
    actual = setup.worker.binding_authorize
    denied = []

    async def probe_old_grant(original, claim):
        response = await test_client.post(
            "/internal/v1/l5/credential-session",
            json={
                "delegation_id": original.authorization["delegation_id"],
                "delegation_revision": original.authorization["delegation_revision"],
                "operation_id": original.operation_id,
                "attempt_id": claim.work_id,
                "lease_owner": claim.owner,
                "lease_fence": claim.fence,
                "connection_ref": {**setup.fixture.reference, "record_type": "credential"},
            },
        )
        assert response.status_code in {403, 409}, response.text
        assert setup.assumed.call_count == 0
        denied.append(response.status_code)
        return await actual(original, claim)

    setup.worker.binding_authorize = probe_old_grant
    assert await setup.worker.execute(setup.messages[0])
    assert len(denied) == 1 and setup.native.create_agent_space.call_count == 1


@pytest.mark.asyncio
async def test_revocation_between_provider_calls_preserves_uncertain_creation(test_client, api_setup, db_session):
    setup = api_setup
    intent, fresh, payload = await prepared(test_client, setup, db_session)
    response = await continue_request(test_client, setup, intent, payload)
    assert response.status_code == 201, response.text
    loop, create = asyncio.get_running_loop(), setup.native.create_agent_space.side_effect

    async def revoke():
        async with setup.sessions() as session:
            grant = await session.get(CredentialServiceDelegation, fresh["delegation_id"])
            grant.revoked_at = datetime.now(UTC)
            await session.commit()

    def create_then_revoke(**kwargs):
        result = create(**kwargs)
        asyncio.run_coroutine_threadsafe(revoke(), loop).result(timeout=10)
        return result

    setup.native.create_agent_space.side_effect = create_then_revoke
    assert await setup.worker.execute(setup.messages[0])  # processed; durable outcome remains uncertain
    assert setup.native.create_agent_space.call_count == 1
    assert setup.native.get_agent_space.call_count == 0
    operation = await db_session.get(BindingOperation, (setup.tenant, intent.operation_id), populate_existing=True)
    assert operation.state == "outcome_unknown"
    assert operation.native_candidate_id == "new-space"


@pytest.mark.asyncio
@pytest.mark.parametrize("api_setup", ["create", "adopt"], indirect=True)
async def test_continuation_preserves_identity_then_original_worker_completes(test_client, api_setup, db_session):
    setup = api_setup
    intent, fresh, payload = await prepared(test_client, setup, db_session)
    before = {key: deepcopy(getattr(intent, key)) for key in ("operation_id", "authorization", "intent_hash", "request", "idempotency_key")}
    envelope = deepcopy(setup.messages[0])
    response = await continue_request(test_client, setup, intent, payload)
    assert response.status_code == 201, response.text
    assert response.json()["authorization"]["delegation_id"] == fresh["delegation_id"]
    assert response.json()["continuation_provider_effects"] == 0
    assert setup.native.mock_calls == [] and setup.assumed.call_count == 0 and setup.messages == [envelope]
    replay = await continue_request(test_client, setup, intent, payload)
    assert replay.status_code == 200 and replay.json() == response.json()
    changed = await continue_request(test_client, setup, intent, {**payload, "reason": "different"})
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "idempotency_conflict"
    accepted = await test_client.post(setup.path + "/binding-operations", json=intent.request, headers={"Idempotency-Key": CREATE_KEY})
    assert accepted.status_code == 202 and accepted.json()["operation_id"] == intent.operation_id
    changed_create = await test_client.post(
        setup.path + "/binding-operations", json={**intent.request, "configuration_revision": 2}, headers={"Idempotency-Key": CREATE_KEY}
    )
    assert changed_create.status_code in {409, 412}
    assert await setup.worker.execute(envelope)
    readback = await test_client.get(setup.path + "/binding-operations/" + intent.operation_id)
    assert readback.json()["state"] == "succeeded", readback.text
    assert setup.native.create_agent_space.call_count == int(intent.request["action"] == "create")
    if intent.request["action"] == "link":
        assert {call[0] for call in setup.native.mock_calls} == {"get_agent_space", "list_goals"}
    await db_session.refresh(intent)
    assert {key: getattr(intent, key) for key in before} == before
    assert await db_session.scalar(select(func.count()).select_from(SetupIntent)) == 1
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 1
    history = await test_client.get(setup.path + f"/binding-operations/{intent.operation_id}/authority-continuations")
    assert history.status_code == 200 and len(history.json()["items"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["lease", "past_lease", "attempt", "native_candidate", "receipt", "completed", "blocked", "stale_hash", "stale_etag", "scope", "revoked_prior"],
)
async def test_continuation_refuses_started_stale_or_changed_authority(test_client, api_setup, db_session, change):
    setup = api_setup
    intent, fresh, payload = await prepared(test_client, setup, db_session)
    work = await db_session.scalar(select(SetupWork))
    operation = await db_session.get(BindingOperation, (setup.tenant, intent.operation_id))
    if change in {"lease", "past_lease"}:
        work.fence = 1
        work.lease_owner = "worker"
        work.lease_until = datetime.now(UTC) + timedelta(minutes=1 if change == "lease" else -1)
    elif change == "attempt":
        operation.attempts = 1
    elif change == "native_candidate":
        operation.native_candidate_id = "uncertain-space"
    elif change == "receipt":
        operation.creation_receipt = {"uncertain": True}
    elif change == "completed":
        work.completed_at = datetime.now(UTC)
    elif change == "blocked":
        work.blocked_at = datetime.now(UTC)
    elif change == "stale_hash":
        payload["intent_hash"] = "0" * 64
    elif change == "scope":
        grant = await db_session.get(CredentialServiceDelegation, fresh["delegation_id"])
        grant.execution_scope = {**grant.execution_scope, "provider_role_arn": "arn:aws:iam::123456789012:role/other"}
    elif change == "revoked_prior":
        grant = await db_session.get(CredentialServiceDelegation, intent.authorization["delegation_id"])
        grant.revoked_at = datetime.now(UTC)
    await db_session.commit()
    result = await continue_request(test_client, setup, intent, payload, etag='"2"' if change == "stale_etag" else '"1"')
    assert result.status_code in {403, 409, 412}, result.text
    assert await db_session.scalar(select(func.count()).select_from(SetupAuthorityContinuation)) == 0
    assert setup.native.mock_calls == [] and setup.assumed.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "expired", "tampered_receipt", "credential", "configuration"])
async def test_current_authority_is_rechecked_by_worker_and_broker(test_client, api_setup, db_session, change):
    setup = api_setup
    intent, fresh, payload = await prepared(test_client, setup, db_session)
    result = await continue_request(test_client, setup, intent, payload)
    assert result.status_code == 201, result.text
    grant = await db_session.get(CredentialServiceDelegation, fresh["delegation_id"])
    if change == "revoked":
        grant.revoked_at = datetime.now(UTC)
    elif change == "expired":
        grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "tampered_receipt":
        row = await db_session.scalar(select(SetupAuthorityContinuation))
        row.receipt = {**row.receipt, "expires_at": (datetime.now(UTC) + timedelta(days=5)).isoformat()}
    elif change == "credential":
        setup.fixture.credential.aws_verified_version_id = "changed"
    elif change == "configuration":
        from l5_services.models.configuration import Configuration

        config = await db_session.get(Configuration, (setup.tenant, intent.configuration_id))
        config.current_revision = 2
    await db_session.commit()
    assert not await setup.worker.execute(setup.messages[0])
    assert setup.native.mock_calls == [] and setup.assumed.call_count == 0


@pytest.mark.asyncio
async def test_second_expiry_appends_history_without_resetting_work(test_client, api_setup, db_session):
    setup = api_setup
    intent, first, payload = await prepared(test_client, setup, db_session)
    one = await continue_request(test_client, setup, intent, payload)
    assert one.status_code == 201, one.text
    duplicate_authority = await continue_request(test_client, setup, intent, payload, key="devops_other_continuation_01")
    assert duplicate_authority.status_code == 409
    second, preview = await renew(test_client, setup, db_session, first, key="devops_fresh_consent_0002")
    two = await continue_request(test_client, setup, intent, body(intent, second, preview), key="devops_explicit_continuation_02")
    assert two.status_code == 201, two.text
    assert two.json()["previous_receipt_hash"] == one.json()["receipt_hash"]
    work = await db_session.scalar(select(SetupWork))
    assert work.fence == 0 and work.attempts == 1 and work.generation == 1
    assert await setup.worker.execute(setup.messages[0])
    assert setup.native.create_agent_space.call_count == 1
