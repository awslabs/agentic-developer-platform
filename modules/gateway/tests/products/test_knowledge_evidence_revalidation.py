"""Independent post-artifact current-human boundary checks."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from src import knowledge_evidence as evidence
from src.auth import native_launch_revocation
from src.orchestration.l5_observation_routes import human_session
from tests.products import test_knowledge_evidence as helpers
from tests.products.test_knowledge_evidence import (
    claimed_knowledge as claimed_knowledge,
)
from tests.products.test_knowledge_evidence import (
    completed_publication as completed_publication,
)
from tests.products.test_knowledge_evidence import (
    consent_fixture as consent_fixture,
)
from tests.products.test_knowledge_evidence import (
    knowledge_fixture as knowledge_fixture,
)
from tests.products.test_knowledge_evidence import (
    knowledge_worker as knowledge_worker,
)
from tests.products.test_knowledge_evidence import (
    l5_owner as l5_owner,
)

__all__ = ["claimed_knowledge", "completed_publication", "consent_fixture", "knowledge_fixture", "knowledge_worker", "l5_owner"]


class Redis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        return True


@pytest.mark.parametrize("revocation", ["logout", "automation_designation"])
@pytest.mark.parametrize("action", ["prepare", "observe", "complete"])
async def test_write_revalidates_current_session_after_artifact_io(
    test_client, app, db_session, completed_publication, l5_owner, monkeypatch, revocation, action
):
    fixture = completed_publication
    redis = Redis()
    monkeypatch.setattr(native_launch_revocation, "_client", lambda: redis)
    monkeypatch.setattr(
        "src.orchestration.l5_observation_routes.require_native_launch_session",
        native_launch_revocation.require_native_launch_session,
    )
    token = jwt.encode({"exp": (datetime.now(UTC) + timedelta(hours=2)).timestamp()}, "independent-local-test-secret")
    monkeypatch.setitem(helpers.AUTH, "Authorization", "Bearer " + token)
    state = SimpleNamespace(designated=False, refused=None)
    runtime = SimpleNamespace(configured=True, store=SimpleNamespace(designation=lambda subject: {} if state.designated else None))
    monkeypatch.setattr(app.state, "native_test_runtime", runtime, raising=False)
    if action in {"observe", "complete"}:
        prepared = await helpers.prepare(test_client, fixture)
        assert prepared.status_code == 200, prepared.text
    if action == "complete":
        observed = await helpers.record(test_client, fixture, prepared.json())
        assert observed.status_code == 200, observed.text
    original = evidence.store.put_artifact

    async def put_then_revoke(document):
        result = await original(document)
        if revocation == "logout":
            await native_launch_revocation.revoke_native_launch(helpers.AUTH["Authorization"], datetime.now(UTC) + timedelta(minutes=30))
        else:
            state.designated = True
        request = SimpleNamespace(headers=Headers(helpers.AUTH), app=app)
        try:
            await human_session(request, l5_owner, db_session)
        except HTTPException as error:
            state.refused = (error.status_code, error.detail["code"])
        return result

    monkeypatch.setattr(evidence.store, "put_artifact", put_then_revoke)
    if action == "prepare":
        response = await helpers.prepare(test_client, fixture)
    elif action == "observe":
        response = await helpers.record(test_client, fixture, prepared.json())
    else:
        response = await helpers.finish(test_client, fixture, prepared.json(), observed.json())
    expected = (401, "session_required") if revocation == "logout" else (403, "security_evidence_interactive_session_required")
    assert state.refused == expected, "The existing framework must recognize the new revocation"
    assert response.status_code in {401, 403}, f"HTTP {response.status_code}; terminal state={response.json().get('state')}"
    await db_session.refresh(fixture.row.operation)
    assert fixture.row.operation.state == "waiting"
    from sqlalchemy import func, select

    from src.products.knowledge_evidence_model import KnowledgeUseEvidence

    assert await db_session.scalar(select(func.count()).select_from(KnowledgeUseEvidence)) == {"prepare": 0, "observe": 1, "complete": 2}[action]


async def test_revoked_history_keeps_private_source_and_human_content_in_artifacts(test_client, db_session, completed_publication):
    fixture = completed_publication
    prompt = "PRIVATE-SCENARIO: what operational procedure should we choose?"
    prepared = await helpers.prepare(test_client, fixture, prompt=prompt, reason="PRIVATE-REASON: review exact policy")
    assert prepared.status_code == 200
    body = helpers.observation(fixture, prepared.json())
    body["transcript"][0]["text"] = prompt
    body["transcript"][-1]["text"] += " PRIVATE-TRANSCRIPT: collected by the current human."
    body["explanation"] = "PRIVATE-EXPLANATION: the response matched the observed source policy."
    observed = await helpers.record(test_client, fixture, prepared.json(), body)
    assert observed.status_code == 200
    completed = await helpers.finish(test_client, fixture, prepared.json(), observed.json())
    assert completed.status_code == 200
    fixture.row.fixture["source_binding"].status = "revoked"
    await db_session.commit()
    inspected = await test_client.get(fixture.path, headers=helpers.AUTH)
    assert inspected.status_code == 200
    assert len(inspected.json()["items"]) == 3
    for private_value in ("PRIVATE-SCENARIO", "PRIVATE-REASON", "PRIVATE-TRANSCRIPT", "PRIVATE-EXPLANATION", '"original"'):
        assert private_value not in inspected.text
    for item in inspected.json()["items"]:
        response = await test_client.get(fixture.path + "/" + item["evidence_id"] + "/artifacts/" + item["kind"], headers=helpers.AUTH)
        assert response.status_code == 403
