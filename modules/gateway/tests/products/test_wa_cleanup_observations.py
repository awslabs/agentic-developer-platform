"""Cleanup proof reads exact owned resources under current Product authority."""

import pytest
from l5_services.api.bindings import _intent_hash
from l5_services.models.setup import BindingOperation, SetupIntent

from tests.products.test_i01 import HEADERS
from tests.products.test_wa_backend_authority import completed_wa, installed_wa  # noqa: F401


@pytest.fixture
async def cleaned_wa(completed_wa, db_session):  # noqa: F811
    state = completed_wa
    common = dict(
        tenant_id=state.run.tenant_id,
        product_id=state.product_id,
        project_id=state.project_id,
        service_id="well_architected",
        actor_id=state.scope["owner_id"],
        scope=state.scope,
        configuration_id=state.run.configuration_id,
        authorization={"actor_id": state.scope["owner_id"]},
    )
    creation = SetupIntent(
        **common,
        operation_id="original-create",
        idempotency_key="original-create-key",
        request={"action": "create", "configuration_revision": 1},
        binding_generation=None,
    )
    creation.intent_hash = _intent_hash(creation)
    receipt = {
        "operation_id": creation.operation_id,
        "intent_hash": creation.intent_hash,
        "native_resource_ref": state.binding.native_resource_ref,
        "provider_identity": state.binding.provider_identity,
    }
    cleanup = SetupIntent(
        **common,
        operation_id="terminal-cleanup",
        idempotency_key="terminal-cleanup-key",
        request={"action": "cleanup", "configuration_revision": 1, "binding_id": state.binding.binding_id, "owned_cleanup_confirmed": True},
        binding_generation=2,
        owned_resource_receipt=receipt,
    )
    cleanup.intent_hash = _intent_hash(cleanup)
    operation = BindingOperation(
        tenant_id=state.run.tenant_id,
        operation_id=cleanup.operation_id,
        binding_id=state.binding.binding_id,
        action="cleanup",
        state="succeeded",
        revision=2,
        gaps=[],
        cleanup_receipt={
            "schema_version": 1,
            "source": "installed-worker",
            "absent": True,
            "operation_id": cleanup.operation_id,
            "intent_hash": cleanup.intent_hash,
            "creation_receipt": receipt,
            "native_resource_ref": state.binding.native_resource_ref,
            "provider_identity": state.binding.provider_identity,
        },
    )
    state.binding.state, state.binding.ownership = "cleaned", "adp_created"
    state.binding.generation, state.binding.revision, state.binding.creation_receipt = 2, 3, receipt
    db_session.add_all([creation, cleanup])
    await db_session.flush()
    db_session.add(operation)
    await db_session.commit()
    state.cleanup, state.cleanup_operation, state.creation = cleanup, operation, creation
    state.cleanup_path = state.observation_path.replace("native-observations", "cleanup-observations")
    state.profile_error = "ResourceNotFoundException"
    return state


async def test_cleaned_binding_can_prove_absence_without_reviving_native_generation(cleaned_wa, test_client):
    state = cleaned_wa
    response = await test_client.get(state.cleanup_path, headers=HEADERS)
    assert response.status_code == 200, response.text
    observed = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert observed["cleanup"] == {
        "owned_absent": True,
        "result_retained": True,
        "source": "independent-native-read",
        "creation_operation_id": "original-create",
        "cleanup_operation_id": "terminal-cleanup",
        "cleanup_intent_sha256": state.cleanup.intent_hash,
        "native_profile_arn": state.native["arn"],
    }
    actions = {action for policy in state.policies for item in policy["Statement"] for action in item["Action"]}
    assert actions == {"sts:GetCallerIdentity", "wellarchitected:GetAgentProfile"}
    assert not state.native_reads
    assert "offline-secret" not in response.text and "authorization" not in response.text
    # The ordinary native endpoint retains its ready-binding requirement.
    assert (await test_client.get(state.observation_path, headers=HEADERS)).status_code == 404


@pytest.mark.parametrize("change", ["receipt", "hash", "adopted", "source", "grant", "intent"])
async def test_foreign_revoked_or_unverified_cleanup_is_denied_before_native_read(cleaned_wa, test_client, db_session, change):
    from datetime import UTC, datetime

    state = cleaned_wa
    if change == "receipt":
        state.cleanup_operation.cleanup_receipt = None
    elif change == "hash":
        state.creation.intent_hash = "f" * 64
    elif change == "adopted":
        state.binding.ownership = "customer_linked"
    elif change == "source":
        state.source_binding.status = "revoked"
    elif change == "grant":
        state.template.revoked_at = datetime.now(UTC)
    else:
        state.cleanup.request = {**state.cleanup.request, "binding_id": "another-binding"}
    await db_session.commit()
    response = await test_client.get(state.cleanup_path, headers=HEADERS)
    assert response.status_code in {403, 404, 409}, response.text
    assert not state.calls and not state.policies


@pytest.mark.parametrize("provider_error,expected", [(None, 409), ("AccessDeniedException", 503), ("ThrottlingException", 503)])
async def test_provider_denial_or_existing_profile_never_means_absent(cleaned_wa, test_client, provider_error, expected):
    cleaned_wa.profile_error = provider_error
    response = await test_client.get(cleaned_wa.cleanup_path, headers=HEADERS)
    assert response.status_code == expected, response.text
    assert "owned_absent" not in response.text


async def test_cleanup_authority_is_rechecked_after_aws_read(cleaned_wa, test_client, db_session, monkeypatch):
    from src import l5_wa_cleanup_observations

    original = l5_wa_cleanup_observations.run_in_threadpool

    async def changed(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        cleaned_wa.cleanup_operation.cleanup_receipt = None
        await db_session.commit()
        return result

    monkeypatch.setattr(l5_wa_cleanup_observations, "run_in_threadpool", changed)
    response = await test_client.get(cleaned_wa.cleanup_path, headers=HEADERS)
    assert response.status_code == 409, response.text
    assert "owned_absent" not in response.text
