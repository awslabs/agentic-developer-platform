"""Same-operation manual use requires current humans, actual artifacts and exact source."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from l5_services.devops.knowledge_sync import source_key
from sqlalchemy import func, select

from src import knowledge_evidence as evidence
from src.products.knowledge_evidence_model import KnowledgeUseEvidence
from src.products.models import ProductSourceSnapshot
from src.products.source_snapshots import ArtifactRef
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_effects import knowledge_worker as knowledge_worker
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["knowledge_fixture", "knowledge_worker", "claimed_knowledge", "consent_fixture", "l5_owner"]
AUTH = {"Authorization": "Bearer separate-human-session"}


class Objects:
    def __init__(self):
        self.values = {}

    def put(self, content, media_type):
        key = f"products/source-snapshots/{len(self.values)}"
        self.values[key] = content
        return ArtifactRef(artifact_id=key, version="v1", sha256=sha256(content).hexdigest(), media_type=media_type, bytes=len(content))

    def get(self, ref):
        value = self.values[ref.artifact_id]
        assert sha256(value).hexdigest() == ref.sha256
        return value


@pytest.fixture
async def completed_publication(knowledge_worker, db_session, l5_owner, monkeypatch):
    row, worker, native = knowledge_worker
    await worker.execute(row.intent, row.claim)
    await db_session.refresh(row.operation)
    objects = Objects()
    monkeypatch.setattr("src.knowledge_evidence_store.get_snapshot_objects", lambda: objects)
    monkeypatch.setattr("src.orchestration.l5_observation_routes.require_native_launch_session", AsyncMock())
    now = datetime.now(UTC)
    receipt = {
        "service_id": "devops",
        "subject": "native_ui",
        "observer_id": l5_owner.user_id,
        "expires_at": (now + timedelta(minutes=20)).isoformat(),
        "snapshot": {
            "selection": {
                "tenant_id": row.intent.tenant_id,
                "product_id": row.intent.product_id,
                "project_id": row.intent.project_id,
                "configuration_id": row.intent.configuration_id,
                "configuration_revision": row.intent.request["configuration_revision"],
                "binding_id": row.operation.binding_id,
                "binding_generation": row.intent.binding_generation,
            }
        },
        "observation_receipt": {
            "actor_id": l5_owner.user_id,
            "observed": {"observation": "displayed"},
            "provenance": "authenticated-human-observation",
        },
    }
    read = AsyncMock(return_value=receipt)
    monkeypatch.setattr(evidence, "read_human_observation", read)
    mutable = SimpleNamespace(now=now)
    monkeypatch.setattr(evidence, "clock", lambda: mutable.now)
    return SimpleNamespace(
        row=row,
        native=native,
        objects=objects,
        time=mutable,
        read=read,
        receipt=receipt,
        path=row.fixture["path"] + "/binding-operations/" + row.intent.operation_id + "/knowledge-evidence",
    )


def scenario(fixture):
    return {
        "source_key": source_key(fixture.row.fixture["source_ref"]),
        "flow_id": "flow-one",
        "challenge_id": "challenge-one",
        "prompt": "What operational policy should this team follow?",
        "expected_behavior": "original",
        "source_excerpt": "original",
        "reason": "Verify the selected source changes a native decision",
    }


async def prepare(client, fixture, **changes):
    body = {**scenario(fixture), **changes}
    return await client.post(
        fixture.path + "/prepare",
        json=body,
        headers={**AUTH, "Idempotency-Key": "prepare_knowledge_01", "If-Match": f'"{fixture.row.operation.revision}"'},
    )


def observation(fixture, prepared):
    fixture.time.now += timedelta(seconds=3)
    asset = fixture.native.assets["1"]
    before = {
        "asset_id": "1",
        "version": 1,
        "metadata": asset["metadata"],
        "content": "original",
        "observed_at": (fixture.time.now - timedelta(seconds=2)).isoformat(),
    }
    return {
        "preparation_hash": prepared["event_hash"],
        "native_session_id": "real-native-session-one",
        "native_agent_type": "CHAT",
        "session_started_at": (fixture.time.now - timedelta(seconds=1)).isoformat(),
        "before": before,
        "after": {**before, "observed_at": fixture.time.now.isoformat()},
        "transcript": [
            {"role": "user", "text": scenario(fixture)["prompt"]},
            {"role": "assistant", "text": "Follow the original policy from document-a and asset 1."},
        ],
        "response_quote": "original policy",
        "source_citation_quote": "document-a",
        "explanation": "The response selected the policy from the exact source instead of accepting the generic prompt.",
    }


async def record(client, fixture, prepared, body=None):
    return await client.post(fixture.path + "/" + prepared["evidence_id"] + "/observe", json=body or observation(fixture, prepared), headers=AUTH)


async def finish(client, fixture, prepared, observed, **changes):
    body = {
        "reviews": [
            {
                "evidence_id": prepared["evidence_id"],
                "observation_hash": observed["event_hash"],
                "source_effect_explanation": "I reviewed the source-dependent policy choice in the actual native conversation.",
            }
        ],
        **changes,
    }
    return await client.post(
        fixture.path + "/complete",
        json=body,
        headers={**AUTH, "Idempotency-Key": "complete_knowledge_01", "If-Match": f'"{fixture.row.operation.revision}"'},
    )


async def test_human_evidence_completes_same_operation_without_native_or_dispatch_effects(test_client, db_session, completed_publication):
    fixture = completed_publication
    row = fixture.row
    original = (row.intent.intent_hash, row.work.work_id, row.work.generation, row.work.fence, row.work.attempts)
    native_calls = list(fixture.native.calls)
    prepared = await prepare(test_client, fixture)
    assert prepared.status_code == 200, prepared.text
    assert (await prepare(test_client, fixture)).json() == prepared.json()
    observed = await record(test_client, fixture, prepared.json())
    assert observed.status_code == 200, observed.text
    fixture.time.now += timedelta(minutes=21)  # historical human window is not current authentication
    completed = await finish(test_client, fixture, prepared.json(), observed.json())
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "succeeded"
    qualification = completed.json()["readback_ref"]["knowledge_qualification"]
    assert qualification["method"] == "authenticated-human-observation-and-owner-review"
    assert qualification["release_check_completed"] is False and qualification["provider_attested_native_use"] is False
    assert (await finish(test_client, fixture, prepared.json(), observed.json())).json() == completed.json()
    await db_session.refresh(row.work)
    assert (row.intent.intent_hash, row.work.work_id, row.work.generation, row.work.fence, row.work.attempts) == original
    assert fixture.native.calls == native_calls and row.fixture["capture"].await_count == 1
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeUseEvidence)) == 3
    inspected = await test_client.get(fixture.path, headers=AUTH)
    assert inspected.status_code == 200 and len(inspected.json()["items"]) == 3


@pytest.mark.parametrize(
    "fault", ["changed_asset", "old_session", "prompt_answer", "missing_citation", "wrong_agent", "foreign_observer", "foreign_binding"]
)
async def test_false_or_wrong_scope_observations_cannot_be_recorded(test_client, completed_publication, fault):
    fixture = completed_publication
    prepared = await prepare(test_client, fixture)
    assert prepared.status_code == 200, prepared.text
    body = observation(fixture, prepared.json())
    if fault == "changed_asset":
        body["after"]["version"] = 2
    elif fault == "old_session":
        body["session_started_at"] = (fixture.time.now - timedelta(hours=1)).isoformat()
    elif fault == "prompt_answer":
        body["transcript"].insert(1, {"role": "user", "text": "Please respond using the original policy"})
    elif fault == "missing_citation":
        body["source_citation_quote"] = "invented-source"
    elif fault == "wrong_agent":
        body["native_agent_type"] = "INVESTIGATION"
    elif fault == "foreign_observer":
        fixture.receipt["observer_id"] = "other-user"
    else:
        fixture.receipt["snapshot"]["selection"]["binding_id"] = "other-binding"
    response = await record(test_client, fixture, prepared.json(), body)
    assert response.status_code in {403, 409}, response.text
    assert response.json()["detail"]["next_action"]


@pytest.mark.parametrize("fault", ["source_revoked", "source_expired", "owner_changed", "altered_hash"])
async def test_completion_rechecks_current_rights_and_immutable_evidence(test_client, db_session, completed_publication, fault):
    fixture = completed_publication
    prepared = await prepare(test_client, fixture)
    assert prepared.status_code == 200, prepared.text
    observed = await record(test_client, fixture, prepared.json())
    assert observed.status_code == 200, observed.text
    if fault == "source_revoked":
        fixture.row.fixture["source_binding"].status = "revoked"
    elif fault == "source_expired":
        snapshot = await db_session.scalar(select(ProductSourceSnapshot))
        snapshot.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif fault == "owner_changed":
        fixture.row.fixture["configurations"]["devops"][0].owner_id = "foreign-owner"
    await db_session.commit()
    data = observed.json()
    if fault == "altered_hash":
        data["event_hash"] = "0" * 64
    response = await finish(test_client, fixture, prepared.json(), data)
    assert response.status_code in {403, 404, 409}, response.text
    await db_session.refresh(fixture.row.operation)
    assert fixture.row.operation.state == "waiting"


async def test_expired_native_write_consent_does_not_prevent_local_human_review(test_client, db_session, completed_publication):
    fixture = completed_publication
    fixture.row.grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    prepared = await prepare(test_client, fixture)
    assert prepared.status_code == 200, prepared.text
    observed = await record(test_client, fixture, prepared.json())
    assert observed.status_code == 200, observed.text
    result = await finish(test_client, fixture, prepared.json(), observed.json())
    assert result.status_code == 200, result.text
    await db_session.refresh(fixture.row.grant)
    assert fixture.row.grant.expires_at.replace(tzinfo=UTC) < datetime.now(UTC)


async def test_human_session_uses_interactive_credential_boundary(test_client, completed_publication, monkeypatch):
    from fastapi import HTTPException

    boundary = AsyncMock(side_effect=HTTPException(403, detail={"code": "security_evidence_interactive_session_required"}))
    monkeypatch.setattr("src.auth.security_fixture_credential.require_interactive_credential", boundary)
    response = await prepare(test_client, completed_publication)
    assert response.status_code == 403 and boundary.await_count == 1
    assert not completed_publication.objects.values


async def test_revoked_source_prevents_artifact_read_but_history_remains_visible(test_client, db_session, completed_publication, monkeypatch):
    fixture = completed_publication
    prepared = await prepare(test_client, fixture)
    assert prepared.status_code == 200, prepared.text
    body = observation(fixture, prepared.json())
    read_bytes = AsyncMock(wraps=evidence.store.artifact)
    monkeypatch.setattr(evidence.store, "artifact", read_bytes)
    fixture.row.fixture["source_binding"].status = "revoked"
    fixture.row.fixture["binding"].generation += 1
    await db_session.commit()
    response = await record(test_client, fixture, prepared.json(), body)
    assert response.status_code == 409 and read_bytes.await_count == 0
    history = await test_client.get(fixture.path, headers=AUTH)
    assert history.status_code == 200 and len(history.json()["items"]) == 1
    assert "prompt" not in history.text and "source_excerpt" not in history.text


@pytest.mark.parametrize("fault", ["service", "refresh", "expired"])
async def test_non_current_human_cannot_collect_or_review(test_client, app, completed_publication, l5_owner, fault):
    from src.auth.dependencies import get_current_user

    fields = {
        "service": {"account_type": "service", "auth_source": "iam"},
        "refresh": {"token_use": "refresh"},
        "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    }[fault]
    app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update=fields)
    response = await prepare(test_client, completed_publication)
    assert response.status_code in {401, 403, 404}, response.text
    assert completed_publication.read.await_count == 0 and not completed_publication.objects.values


async def test_record_replay_conflict_and_reused_session_are_refused(test_client, completed_publication):
    fixture = completed_publication
    prepared = await prepare(test_client, fixture)
    body = observation(fixture, prepared.json())
    observed = await record(test_client, fixture, prepared.json(), body)
    assert observed.status_code == 200, observed.text
    assert (await record(test_client, fixture, prepared.json(), body)).json() == observed.json()
    changed = {**body, "explanation": "This is a changed explanation after the original evidence was retained."}
    assert (await record(test_client, fixture, prepared.json(), changed)).status_code == 409
    other = await test_client.post(
        fixture.path + "/prepare",
        json=scenario(fixture),
        headers={**AUTH, "Idempotency-Key": "prepare_knowledge_02", "If-Match": f'"{fixture.row.operation.revision}"'},
    )
    assert other.status_code == 200, other.text
    response = await record(test_client, fixture, other.json())
    assert response.status_code == 409 and response.json()["detail"]["code"] == "knowledge_evidence_session_reused"
