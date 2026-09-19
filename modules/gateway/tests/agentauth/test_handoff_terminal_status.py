"""A worker terminal report preserves its committed continuation."""

from sqlalchemy import select, update

from src.orchestration.execution_state import OutcomeKind
from src.orchestration.execution_store import load_execution
from src.orchestration.handoff import current_identity
from src.orchestration.models import OrchestrationWorkClaim
from tests.agentauth import test_handoff_routes as fixtures
from tests.agentauth.test_handoff_routes import CLAIM_ID, HEADERS, ORG, URL

handoff = fixtures.handoff


async def test_terminal_status_after_handoff_preserves_continuation_authority(handoff, monkeypatch):
    monkeypatch.setattr("src.orchestration.work_admission.enabled", lambda: True)
    caller = handoff.runtime.authenticate.return_value[1]
    monkeypatch.setattr("src.agentauth.run_credential.verify_credential", lambda *a, **kw: caller)
    handoff.runtime.store._read.return_value = {"issue_number": {"N": "5144"}}
    async with handoff.sessions() as session:
        await session.execute(
            update(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == CLAIM_ID).values(claim_event_id=handoff.run, active_run_id=handoff.run)
        )
        await session.commit()
    response = await handoff.client.post(URL, headers=HEADERS, json={})
    assert response.status_code == 201, response.text
    async with handoff.sessions() as session:
        identity = await current_identity(session, org_id=ORG, node_id=handoff.node.id)
        assert (await load_execution(session, identity=identity)).kind is OutcomeKind.APPLIED
    response = await handoff.client.post("/internal/v1/agent/self/status", headers=HEADERS, json={"status": "complete"})
    assert response.status_code == 200, response.text
    async with handoff.sessions() as session:
        claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == CLAIM_ID))
        identity = await current_identity(session, org_id=ORG, node_id=handoff.node.id)
        continuation = await load_execution(session, identity=identity)
        assert continuation.kind is OutcomeKind.APPLIED, f"claim={claim.state}, continuation={continuation.kind}, reason={continuation.reason}"
