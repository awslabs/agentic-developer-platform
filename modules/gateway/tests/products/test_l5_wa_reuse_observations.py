"""Mounted post-cleanup evidence without publication, native work or UI actions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.publication_retry import PublicationRetryReceipt
from l5_services.models.wa_periodic import WAPeriodicCheckpoint, WATransportObservation
from l5_services.models.wa_usage import WellArchitectedUsage
from sqlalchemy import func, select

from src.auth.dependencies import get_current_user
from tests.products.test_l5_wa_reuse_authorization import contribution, retained, valid_token_context  # noqa: F401

pytestmark = pytest.mark.parametrize("contribution", ["transform"], indirect=True)


@pytest.fixture
async def reuse(retained, db_session, monkeypatch):  # noqa: F811
    monkeypatch.setenv("L5_WA_QUALIFICATION_OBSERVATIONS_ENABLED", "true")
    no_provider = Mock(side_effect=AssertionError("checkpoint must not call a provider"))
    monkeypatch.setattr("boto3.client", no_provider)
    state = retained
    version = await db_session.get(ConfigurationVersion, (state.run.tenant_id, state.run.configuration_id, state.run.configuration_revision))
    # This older shared contribution fixture predates the original admission hash.
    state.run.admission = {**state.run.admission, "configuration_hash": version.hash}
    await db_session.commit()
    state.etag = '"' + version.hash + '"'
    state.observations_path = state.path.split("/results/")[0] + "/qualification-experiments"
    state.selection = {
        "result_id": "fixture-result",
        "result_revision": 1,
        "task_id": "later-task",
        "context_retry_key": "original_context_retry_01",
        "insights_retry_key": "original_insights_retry_01",
    }
    state.no_provider = no_provider
    return state


async def checkpoint(client, state, key="original_reuse_checkpoint_before"):
    return await client.post(
        state.observations_path + "/reuse-checkpoints", json=state.selection, headers={"If-Match": state.etag, "Idempotency-Key": key}
    )


async def insert_receipts(state, db):
    scope = state.run
    for publication in state.publications:
        key = state.selection["context_retry_key" if publication.destination == "product-context" else "insights_retry_key"]
        db.add(
            PublicationRetryReceipt(
                tenant_id=scope.tenant_id,
                product_id=scope.product_id,
                project_id=scope.project_id,
                service_id="well_architected",
                actor_id=state.actor.user_id,
                idempotency_key=key,
                publication_id=publication.publication_id,
                request_hash="a" * 64,
                response={"state": "published"},
            )
        )
    context = next(item for item in state.publications if item.destination == "product-context")
    db.add(
        WellArchitectedUsage(
            tenant_id=scope.tenant_id,
            publication_id=context.publication_id,
            task_id="later-task",
            result_id="fixture-result",
            actor_id=state.actor.user_id,
            observed_at=datetime.now(UTC),
            retrieval_receipt={"receipt_id": "retrieved"},
            usage_receipt={"receipt_id": "used"},
        )
    )
    await db.commit()


async def test_post_cleanup_checkpoint_replays_original_without_any_native_or_consumer_action(reuse, test_client, db_session):
    first = await checkpoint(test_client, reuse)
    assert first.status_code == 200, first.text
    snapshot = first.json()["snapshot"]
    assert snapshot["scope"]["cleanup"]["operation_id"] == "fixture-cleanup"
    assert snapshot["runtime"]["replays"] == [] and snapshot["runtime"]["usage"] is None
    assert snapshot["runtime"]["runs"][0]["run_id"] == reuse.run.run_id
    assert first.json()["execution_authorized"] is first.json()["native_qualified"] is False
    await insert_receipts(reuse, db_session)
    replay = await checkpoint(test_client, reuse)
    assert replay.json() == first.json()
    after = await checkpoint(test_client, reuse, "original_reuse_checkpoint_after")
    assert after.status_code == 200, after.text
    assert len(after.json()["snapshot"]["runtime"]["replays"]) == 2
    assert after.json()["snapshot"]["runtime"]["usage"]["task_id"] == "later-task"
    assert await db_session.scalar(select(func.count()).select_from(WAPeriodicCheckpoint)) == 2
    reuse.no_provider.assert_not_called()


async def test_ready_owned_profile_can_be_observed_without_weakening_cleanup_consent(reuse, test_client, db_session):
    binding = await db_session.get(Binding, (reuse.run.tenant_id, reuse.run.binding_id))
    binding.state, binding.generation = "ready", reuse.run.binding_generation
    await db_session.commit()
    response = await checkpoint(test_client, reuse)
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["scope"]["binding_state"] == "ready"
    assert response.json()["snapshot"]["scope"]["cleanup"]["state"] == "ready"
    # Observation readiness never becomes permission for retained-copy consent.
    consent_scope = await test_client.get(reuse.path + "/reuse-scope")
    assert consent_scope.status_code == 409 and consent_scope.json()["detail"]["code"] == "owned_cleanup_unverified"
    reuse.no_provider.assert_not_called()


@pytest.mark.parametrize("changed", ["adopted", "generation", "provider", "receipt", "foreign-profile"])
async def test_ready_observations_require_original_owned_profile_provenance(reuse, test_client, db_session, changed):
    binding = await db_session.get(Binding, (reuse.run.tenant_id, reuse.run.binding_id))
    binding.state, binding.generation = "ready", reuse.run.binding_generation
    if changed == "adopted":
        binding.ownership = "customer_linked"
    elif changed == "generation":
        binding.generation += 1
    elif changed == "provider":
        binding.provider_identity = {**binding.provider_identity, "account_id": "000000000000"}
    elif changed == "receipt":
        binding.creation_receipt = {**binding.creation_receipt, "intent_hash": "f" * 64}
    else:
        binding.native_resource_ref = {**binding.native_resource_ref, "record_id": "foreign-profile"}
    await db_session.commit()
    response = await checkpoint(test_client, reuse)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "owned_profile_unverified"
    reuse.no_provider.assert_not_called()


async def test_interval_reads_only_scoped_audit_and_rechecks_authority(reuse, test_client, db_session, monkeypatch):
    first = await checkpoint(test_client, reuse)
    assert first.status_code == 200, first.text
    await insert_receipts(reuse, db_session)
    last = await checkpoint(test_client, reuse, "original_reuse_checkpoint_after")
    reader = AsyncMock(return_value={"source": "independent-cloudtrail-read", "events": []})
    monkeypatch.setattr("src.l5_wa_reuse_observations.read_native", reader)
    response = await test_client.post(
        reuse.observations_path + "/reuse-intervals/read",
        json={"before_id": first.json()["checkpoint_id"], "after_id": last.json()["checkpoint_id"], "read_native_audit": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["contract"] == "wa-reuse-interval/v1"
    assert reader.await_count == 1 and "interval" in reader.await_args.kwargs
    assert "generation_ids" not in reader.await_args.kwargs
    reuse.no_provider.assert_not_called()


@pytest.mark.parametrize("change", ["owner", "actor", "service", "corrupt", "source", "cleanup", "result"])
async def test_retained_checkpoint_is_opaque_or_refuses_changed_authority(reuse, test_client, db_session, app, change):
    first = await checkpoint(test_client, reuse)
    assert first.status_code == 200, first.text
    path = reuse.observations_path + "/reuse-checkpoints/" + first.json()["checkpoint_id"]
    if change == "owner":
        config = await db_session.get(Configuration, (reuse.run.tenant_id, reuse.run.configuration_id))
        config.owner_id = "another-owner"
    elif change in {"actor", "service"}:
        changed = {"user_id": "foreign-owner"} if change == "actor" else {"account_type": "service"}
        app.dependency_overrides[get_current_user] = lambda: reuse.actor.model_copy(update=changed)
    elif change == "corrupt":
        row = await db_session.get(WAPeriodicCheckpoint, (reuse.run.tenant_id, first.json()["checkpoint_id"]))
        row.snapshot_digest = "f" * 64
    elif change == "source":
        reuse.run.input = {**reuse.run.input, "sources": []}
    elif change == "cleanup":
        reuse.cleanup.cleanup_receipt = {**reuse.cleanup.cleanup_receipt, "absent": False}
    else:
        reuse.run.provider_state = "running"
    await db_session.commit()
    response = await test_client.get(path)
    assert response.status_code in {403, 404, 409}, response.text
    assert "snapshot" not in response.json()


async def test_checkpoint_contracts_and_keys_cannot_be_substituted(reuse, test_client, db_session):
    first = await checkpoint(test_client, reuse)
    assert first.status_code == 200, first.text
    identifier = first.json()["checkpoint_id"]
    wrong_route = await test_client.get(reuse.observations_path + "/periodic-checkpoints/" + identifier)
    assert wrong_route.status_code == 404
    reuse.selection = {**reuse.selection, "task_id": "different-task"}
    assert (await checkpoint(test_client, reuse)).status_code == 409
    reversed_interval = await test_client.post(
        reuse.observations_path + "/reuse-intervals/read", json={"before_id": identifier, "after_id": identifier}
    )
    assert reversed_interval.status_code == 409


async def test_transport_snapshot_covers_other_configuration_revisions(reuse, test_client, db_session):
    db_session.add(
        WATransportObservation(
            observation_id="another-revision",
            send_id="uncertain-send",
            tenant_id=reuse.run.tenant_id,
            configuration_id=reuse.run.configuration_id,
            configuration_revision=2,
            run_id="another-run",
            attempt_id="another-attempt",
            observed_at=datetime.now(UTC),
            phase="before_send",
            facts={"contract": "wa-start-transport/v1"},
        )
    )
    await db_session.commit()
    response = await checkpoint(test_client, reuse)
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["runtime"]["transport"][0]["configuration_revision"] == 2


async def test_revocation_during_audit_never_returns_retained_facts(reuse, test_client, db_session, monkeypatch):
    first = await checkpoint(test_client, reuse)
    assert first.status_code == 200, first.text
    last = await checkpoint(test_client, reuse, "original_reuse_checkpoint_after")

    async def audit(*args, **kwargs):
        config = await db_session.get(Configuration, (reuse.run.tenant_id, reuse.run.configuration_id))
        config.owner_id = "replacement-owner"
        await db_session.commit()
        return {"source": "independent-cloudtrail-read", "events": []}

    monkeypatch.setattr("src.l5_wa_reuse_observations.read_native", audit)
    response = await test_client.post(
        reuse.observations_path + "/reuse-intervals/read",
        json={"before_id": first.json()["checkpoint_id"], "after_id": last.json()["checkpoint_id"], "read_native_audit": True},
    )
    assert response.status_code in {403, 404}
    assert "before" not in response.json()
