"""The real dispatch transaction, durable outbox, registration and result reader."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.agentauth.routes import require_agent_transport
from src.agentauth.run_report_routes import router
from src.orchestration.dispatch_pass import publish_pending, run_dispatch_pass
from src.orchestration.models import OrchestrationDecision, OrchestrationNode
from src.orchestration.pr_bindings import MergeEvidence, PullRequestIdentity
from src.orchestration.results import observe_results
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration import test_dispatch_pass as dispatch_fixtures
from tests.orchestration.test_dispatch_pass import (
    FakeSQS,
    _config,
    _make_approval,
    _make_flow,
    _make_node,
    _make_org,
)

engine = dispatch_fixtures.engine
session_factory = dispatch_fixtures.session_factory
session = dispatch_fixtures.session
run_store = dispatch_fixtures.run_store


@pytest.fixture(autouse=True)
def reporting(monkeypatch):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_SHARED_RUN_REPORTING_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-report-signing-material")
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=42))


async def prepare(session):
    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    node = await _make_node(session, flow, node_ref="s1", issue_ref="4196")
    return node


async def test_dispatch_commit_publish_crash_and_authenticated_completion(session, session_factory, monkeypatch):
    node = await prepare(session)
    cfg = _config()
    first = await run_dispatch_pass(session, cfg)
    assert first.dispatched == 1 and len(first.pending) == 1
    envelope = first.pending[0].envelope
    await session.commit()
    publish_pending(first, cfg, client=FakeSQS(fail=True))
    second = await run_dispatch_pass(session, cfg)
    assert len(second.pending) == 1
    assert second.pending[0].envelope == envelope
    assert node.attempts == 1
    decisions = (await session.scalars(select(OrchestrationDecision))).all()
    assert all(envelope["run_report"]["credential"] not in (d.reason or "") for d in decisions)
    sql = await session.get(OrchestrationRunReport, envelope["message_id"])
    assert "credential" not in json.dumps(sql.dispatch_metadata)
    await session.commit()
    sqs = FakeSQS()
    publish_pending(second, cfg, client=sqs)
    assert sqs.envelope() == envelope

    # Worker completion follows the exact published capability; no run ID is in
    # the request body. The same binding powers normal legacy reconciliation.
    identity = PullRequestIdentity(42, "PR_identity", cfg.repo, 77, "a" * 40)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", AsyncMock(return_value=identity))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_agent_transport] = lambda: None
    headers = {"X-Adp-Report-Credential": envelope["run_report"]["credential"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test") as client:
        assert (await client.post("/internal/v1/agent/report/pull-request", headers=headers, json=identity.__dict__)).json()["binding_receipt"]
        assert (await client.post("/internal/v1/agent/report/terminal", headers=headers, json={"outcome": "complete"})).status_code == 200
    await session.rollback()  # reload receipts committed by the independent HTTP transactions
    evidence = SimpleNamespace(
        bound_pull_request=AsyncMock(
            return_value=MergeEvidence(
                merged=True,
                head_sha=identity.head_sha,
                checks_successful=True,
                review_approved=True,
                merge_commit_sha="b" * 40,
                merged_at="2026-09-21T01:00:00Z",
                url="https://github.com/aws-e/adp/pull/77",
                provider_repository_id=42,
                provider_pr_node_id="PR_identity",
            )
        )
    )
    advisory = MagicMock()
    advisory.get.side_effect = RuntimeError("DDB unavailable")
    result = await observe_results(session, evidence=evidence, run_store=advisory)
    assert result.errors == 0
    assert (await session.get(OrchestrationNode, envelope["orchestration"]["node_id"])).state == "passed"
    advisory.get.assert_not_called()


async def test_missing_signing_material_rolls_back_without_publishing(session, monkeypatch):
    node = await prepare(session)
    node_id = node.id
    monkeypatch.delenv("AGENT_RUN_CREDENTIAL_KEY")
    monkeypatch.delenv("AGENT_RUN_CREDENTIAL_KEY_PARAMETER", raising=False)
    result = await run_dispatch_pass(session, _config())
    assert result.errors == 1 and result.pending == []
    await session.refresh(node)
    assert node.id == node_id and node.state == "ready" and node.attempts == 0


async def test_staged_default_off_emits_no_report_contract(session, monkeypatch):
    await prepare(session)
    monkeypatch.delenv("ADP_SHARED_RUN_REPORTING_ENABLED")
    result = await run_dispatch_pass(session, _config())
    assert result.dispatched == 1
    assert "run_report" not in result.pending[0].envelope
    assert (await session.scalars(select(OrchestrationRunReport))).all() == []


async def test_replay_of_inactive_flow_is_reported_as_publish_failure(session):
    from src.orchestration.models import OrchestrationFlow

    node = await prepare(session)
    await run_dispatch_pass(session, _config())
    await session.commit()
    flow = await session.get(OrchestrationFlow, node.flow_id)
    flow.state = "halted"
    await session.commit()
    recovered = await run_dispatch_pass(session, _config())
    assert recovered.pending == [] and recovered.publish_failed == 1


async def test_governed_replay_rechecks_current_policy_before_queueing(session, monkeypatch):
    import sys

    from src.orchestration.review_cycle import CycleBlockedError

    await prepare(session)
    await run_dispatch_pass(session, _config())
    await session.commit()
    authorizer = AsyncMock(side_effect=CycleBlockedError("policy_expired"))
    monkeypatch.setattr(
        "src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=SimpleNamespace(policy=object(), refusal=None))
    )
    monkeypatch.setitem(sys.modules, "src.orchestration.shared_policy", SimpleNamespace(authorize_shared_model=authorizer))
    recovered = await run_dispatch_pass(session, _config())
    assert recovered.pending == [] and recovered.publish_failed == 1
    authorizer.assert_awaited_once()
