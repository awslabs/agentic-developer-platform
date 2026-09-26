"""Durable proposal identity and reviewed human decisions."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app.middleware.auth import create_access_token
from app.routers.research import _check_revision
from app.schemas.research import ResearchProposalResponse
from app.models.research_proposal import ResearchProposal
from tests.test_auth import _seed_two_tenant_research
from tests.conftest import async_session_test


async def test_same_proposal_request_replays_and_changed_payload_conflicts(client):
    seeded = await _seed_two_tenant_research()
    token, _ = create_access_token(seeded["org_a"])
    headers = {"Authorization": "Bearer " + token}
    body = {
        "request_id": str(uuid.uuid4()),
        "workspace_id": str(seeded["workspace_a"]),
        "title": "Reviewed title",
        "objective": "Determine bounded behavior",
        "hypothesis": "A scoped experiment works",
        "source_findings": [str(seeded["finding_a"])],
        "estimated_cost_usd": 0.01,
    }
    first = await client.post("/api/v1/research/proposals", headers=headers, json=body)
    second = await client.post("/api/v1/research/proposals", headers=headers, json=body)
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["revision"] == second.json()["revision"]
    conflict = await client.post(
        "/api/v1/research/proposals",
        headers=headers,
        json={**body, "title": "Different title"},
    )
    assert conflict.status_code == 409
    foreign = await client.post(
        "/api/v1/research/proposals",
        headers=headers,
        json={
            **body,
            "request_id": str(uuid.uuid4()),
            "source_findings": [str(seeded["finding_b"])],
        },
    )
    assert foreign.status_code == 404


async def test_revision_binds_content_and_requires_verified_human():
    seeded = await _seed_two_tenant_research()
    async with async_session_test() as session:
        proposal = await session.get(ResearchProposal, seeded["proposal_a"])
        revision = ResearchProposalResponse.model_validate(proposal).revision
        request = SimpleNamespace(
            state=SimpleNamespace(
                caller=SimpleNamespace(principal=SimpleNamespace(account_type="human"))
            )
        )
        _check_revision(request, proposal, revision)
        proposal.hypothesis = "Changed after operator preview"
        with pytest.raises(HTTPException) as changed:
            _check_revision(request, proposal, revision)
        assert changed.value.status_code == 409
        request.state.caller.principal.account_type = "service"
        with pytest.raises(HTTPException) as denied:
            _check_revision(
                request,
                proposal,
                ResearchProposalResponse.model_validate(proposal).revision,
            )
        assert denied.value.status_code == 403


async def test_legacy_org_token_cannot_use_human_revision_decision(client):
    seeded = await _seed_two_tenant_research()
    token, _ = create_access_token(seeded["org_a"])
    headers = {"Authorization": "Bearer " + token}
    shown = await client.get(
        f"/api/v1/research/proposals/{seeded['proposal_a']}", headers=headers
    )
    result = await client.patch(
        f"/api/v1/research/proposals/{seeded['proposal_a']}/approve",
        headers=headers,
        json={"expected_revision": shown.json()["revision"]},
    )
    assert result.status_code == 403
    after = await client.get(
        f"/api/v1/research/proposals/{seeded['proposal_a']}", headers=headers
    )
    assert after.json()["status"] == "proposed"
