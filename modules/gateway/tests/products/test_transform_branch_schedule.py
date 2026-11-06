"""Branch schedules retain one exact source under explicit, current owner consent."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.events.schedules import receive_schedule_tick
from l5_services.models import EventReceipt, Run, Work
from l5_services.models.attempt import WorkerAttempt
from sqlalchemy import delete, func, select

from src.auth.l5_transform_branch_scope import BRANCHES, OCCURRENCE, branch_selections
from src.products.models import ProductSourceBinding
from src.shared.identity.product_scope import current_l5_parent
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation
from tests.products import test_transform_schedule_admission as cases

transform_gateway = cases.transform_gateway
periodic_transform = cases.periodic_transform
PATH = cases.PATH


@pytest.fixture
async def branch_transform(periodic_transform, db_session, monkeypatch):
    state = periodic_transform
    parent = state.template
    source = {**state.version.settings["source_refs"][0], "source_connection_id": "branch-installation", "record_id": "321"}
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.native_id == parent.sources[1]["record_id"]))
    binding.credential_id, binding.installation_id = None, "branch-installation"
    binding.provider, binding.namespace, binding.native_id = "github", "123", "321"
    mapping = ChannelTenantMap(
        id="branch-installation",
        org_id=parent.tenant_id,
        provider="github",
        provider_scope_id="123",
        installation_id="456",
        installed_by_user_id=parent.created_by,
    )
    db_session.add(mapping)
    settings = deepcopy(state.version.settings)
    settings["source_refs"] = [source]
    trigger = {
        **settings["triggers"][0],
        "source_refs": [source],
        "source_policy": "branch",
        "branch_selector": {**source, "record_type": "repository_branch", "record_revision": "release/current"},
    }
    settings["triggers"] = [trigger]
    state.version.settings = settings
    parent.sources = [parent.sources[0], source]
    parent.execution_scope = {
        **parent.execution_scope,
        "sources": deepcopy(parent.sources),
        BRANCHES: branch_selections(settings, parent.tenant_id, parent.project_id),
    }
    await db_session.execute(delete(EventReceipt))
    await db_session.commit()
    receipt = await receive_schedule_tick(
        db_session,
        tenant_id=parent.tenant_id,
        product_id=parent.product_id,
        project_id=parent.project_id,
        trigger_id=trigger["trigger_id"],
        scheduled_at=state.tick,
        now=state.tick,
        service_id="transform",
        defer_transform_branch=True,
    )
    assert receipt.state == "pending" and receipt.run_input is None and "pinned_source" not in receipt.target
    state.receipt, state.source_binding, state.mapping = receipt, binding, mapping
    state.resolve = AsyncMock(return_value={**source, "record_revision": "b" * 40})
    monkeypatch.setattr("src.l5_transform_schedule.resolve_branch", state.resolve)
    return state


async def test_branch_freeze_is_exact_and_replay_ignores_moved_head(branch_transform, test_client, db_session):
    state = branch_transform
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code == 200, response.text
    run = await db_session.get(Run, (state.template.tenant_id, response.json()["run_id"]))
    child = await db_session.get(CredentialServiceDelegation, run.admission["delegation_id"])
    await db_session.refresh(state.receipt)
    frozen = run.input["sources"][0]
    assert frozen["record_revision"] == "b" * 40
    assert child.sources == [state.template.sources[0], frozen]
    assert child.execution_scope[OCCURRENCE] == state.receipt.target[OCCURRENCE]
    assert state.captures[0].source_ref.model_dump() == frozen
    assert state.template.sources[1]["record_revision"] == "a" * 40
    state.resolve.return_value = {**frozen, "record_revision": "c" * 40}
    state.source_binding.observed = {**state.source_binding.observed, "source_revision": "c" * 40}
    await db_session.commit()
    replay = await test_client.post(PATH, json=state.body)
    assert replay.status_code == 200 and replay.json() == response.json(), replay.text
    assert state.resolve.await_count == 1 and state.publish.await_count == 1 and len(state.captures) == 1
    assert await current_l5_parent(db_session, child)


async def test_retained_freeze_survives_admission_failure(branch_transform, test_client, db_session):
    state = branch_transform
    original = deepcopy(state.records)
    state.records[0]["transform_qualification"]["region_qualification"] = "unqualified"
    state.pin_catalog()
    failed = await test_client.post(PATH, json=state.body)
    assert failed.status_code == 409, failed.text
    await db_session.refresh(state.receipt)
    frozen = deepcopy(state.receipt.run_input)
    assert frozen["sources"][0]["record_revision"] == "b" * 40
    state.records[:] = original
    state.pin_catalog()
    state.resolve.return_value = {**frozen["sources"][0], "record_revision": "c" * 40}
    retried = await test_client.post(PATH, json=state.body)
    assert retried.status_code == 200, retried.text
    assert state.resolve.await_count == 1
    run = await db_session.get(Run, (state.template.tenant_id, retried.json()["run_id"]))
    assert run.input == frozen


@pytest.mark.parametrize("change", ["consent", "revoke", "unlink", "personal_owner", "provider", "binding_generation"])
async def test_branch_denies_before_resolution_without_current_scope(branch_transform, test_client, db_session, change):
    state = branch_transform
    if change == "consent":
        state.template.execution_scope = {key: value for key, value in state.template.execution_scope.items() if key != BRANCHES}
    elif change == "revoke":
        state.template.revoked_at = datetime.now(UTC)
    elif change == "unlink":
        state.source_binding.unlinked_at = datetime.now(UTC)
        state.source_binding.status = "unlinked"
    elif change == "personal_owner":
        state.mapping.provider_scope_id, state.mapping.installed_by_user_id = "personal:123:someone-else", "someone-else"
    elif change == "provider":
        state.source_binding.provider = "other"
    else:
        state.binding.generation += 1
    await db_session.commit()
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code in {403, 404, 409}, response.text
    assert state.resolve.await_count == 0 and not state.captures and not state.publish.await_count
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 0


async def test_revocation_during_resolution_cannot_commit_freeze(branch_transform, test_client, db_session):
    state = branch_transform

    async def resolve(*args):
        state.template.revoked_at = datetime.now(UTC)
        await db_session.commit()
        return state.resolve.return_value

    state.resolve.side_effect = resolve
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code == 403, response.text
    await db_session.refresh(state.receipt)
    assert state.receipt.run_input is None and not state.captures and not state.publish.await_count


@pytest.mark.parametrize("change", ["revoke", "unlink", "binding_generation", "proof"])
async def test_frozen_child_cannot_outlive_source_and_owner_authority(branch_transform, test_client, db_session, change):
    state = branch_transform
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code == 200, response.text
    run = await db_session.get(Run, (state.template.tenant_id, response.json()["run_id"]))
    child = await db_session.get(CredentialServiceDelegation, run.admission["delegation_id"])
    if change == "revoke":
        state.template.revoked_at = datetime.now(UTC)
    elif change == "unlink":
        state.source_binding.unlinked_at = datetime.now(UTC)
        state.source_binding.status = "unlinked"
    elif change == "binding_generation":
        state.source_binding.generation += 1
    else:
        await db_session.refresh(state.receipt)
        state.receipt.target = {**state.receipt.target, "pinned_source": {**run.input["sources"][0], "record_revision": "c" * 40}}
    await db_session.commit()
    replay = await test_client.post(PATH, json=state.body)
    assert replay.status_code in {403, 404, 409}, replay.text
    from src.products.source_routes import service_visible_bindings

    assert not await service_visible_bindings(db_session, child)
    assert state.resolve.await_count == 1 and state.publish.await_count == 1 and len(state.captures) == 1


async def test_resolution_failure_leaves_no_source_or_native_effect(branch_transform, test_client, db_session):
    state = branch_transform
    state.resolve.side_effect = HTTPException(503, detail={"code": "branch_resolution_unavailable"})
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code == 503, response.text
    await db_session.refresh(state.receipt)
    assert state.receipt.run_input is None and not state.captures and not state.publish.await_count


async def test_installed_branch_factory_uses_gateway_freeze_once(branch_transform, db_session, app, monkeypatch):
    await cases.exercise_installed_factory(branch_transform, db_session, app, monkeypatch)
    assert branch_transform.resolve.await_count == 1
    run = await db_session.scalar(select(Run))
    assert run.input["sources"][0]["record_revision"] == "b" * 40


async def test_leased_branch_work_receives_exact_broker_policy_and_rechecks_unlink(branch_transform, test_client, db_session):
    from src.internal.l5_credential_routes import TRANSFORM_SUBMIT_OPERATION, CredentialSessionRequest, verify_l5_work

    state = branch_transform
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code == 200, response.text
    run = await db_session.get(Run, (state.template.tenant_id, response.json()["run_id"]))
    child = await db_session.get(CredentialServiceDelegation, run.admission["delegation_id"])
    work = await db_session.scalar(select(Work).where(Work.run_id == run.run_id))
    work.kind = "submit"  # The executor prepares a submit attempt after its initial execute step.
    work.lease_owner, work.fence, work.lease_until = "test-worker", 1, datetime.now(UTC) + timedelta(minutes=5)
    db_session.add(
        WorkerAttempt(
            tenant_id=run.tenant_id,
            attempt_id="test-attempt",
            run_id=run.run_id,
            operation=TRANSFORM_SUBMIT_OPERATION,
            first_work_id=work.work_id,
            first_fence=1,
            idempotency_token="a" * 64,
            input_hash="b" * 64,
            started_at=datetime.now(UTC),
            status="prepared",
        )
    )
    await db_session.commit()
    body = CredentialSessionRequest(
        delegation_id=child.id,
        delegation_revision=child.revision,
        operation_id=run.run_id,
        attempt_id=work.work_id,
        lease_owner="test-worker",
        lease_fence=1,
        connection_ref=child.sources[0],
    )
    permit = await verify_l5_work(db_session, child, body)
    policy = json.loads(permit.session_policy)
    assert policy["Statement"][0]["Action"] == ["transform-custom:GetTransformationPackageUrl", "transform-custom:ExecuteTransformation"]
    assert policy["Statement"][0]["Resource"] == "arn:aws:transform-custom:us-east-1:123456789012:package/definition-a"
    assert {action for statement in policy["Statement"] for action in statement["Action"]} == {
        "transform-custom:GetTransformationPackageUrl",
        "transform-custom:ExecuteTransformation",
        "transform-custom:ConverseStream",
    }
    state.source_binding.unlinked_at, state.source_binding.status = datetime.now(UTC), "unlinked"
    await db_session.commit()
    with pytest.raises(HTTPException):
        await verify_l5_work(db_session, child, body)


async def test_owner_preview_carries_branch_and_digest_changes_with_selection(branch_transform, test_client, db_session, app, source_member):
    from src.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: source_member.model_copy(update={"auth_source": "jwt", "token_use": "access"})
    state = branch_transform
    preview = await test_client.get(state.path + "/delegation-scope", params={"binding_id": state.binding.binding_id})
    assert preview.status_code == 200, preview.text
    assert preview.json()["scope"][BRANCHES] == state.template.execution_scope[BRANCHES]
    settings = deepcopy(state.version.settings)
    settings["triggers"][0]["branch_selector"]["record_revision"] = "release/next"
    state.version.settings = settings
    await db_session.commit()
    updated = await test_client.get(state.path + "/delegation-scope", params={"binding_id": state.binding.binding_id})
    assert updated.status_code == 200, updated.text
    assert updated.json()["scope_digest"] != preview.json()["scope_digest"]
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code in {403, 404, 409} and not state.resolve.await_count
