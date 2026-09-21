"""Authenticated shared-role reporting through the real route and binding store."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.agentauth.routes import require_agent_transport
from src.agentauth.run_report_routes import router
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import OrchestrationDecision, OrchestrationFlow, OrchestrationNode, OrchestrationPullRequestBinding
from src.orchestration.pr_bindings import PullRequestIdentity
from src.orchestration.pr_identity import PrIdentityError
from src.orchestration.run_reports import OrchestrationRunReport, RunReportError, prepare_run_report
from src.shared.models.organization import Organization

URL = "/internal/v1/agent/report"
PR = PullRequestIdentity(42, "PR_known", "org/repo", 52, "a" * 40)


@pytest.fixture
async def reports(db_session_factory, monkeypatch):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-key-for-shared-role-reports")
    async with db_session_factory() as session:
        session.add(Organization(id="org", name="org", github_installation_ids=["123"]))
        flow = OrchestrationFlow(org_id="org", slug="flow", title="flow", state="running")
        session.add(flow)
        await session.flush()
        node = OrchestrationNode(
            org_id="org",
            flow_id=flow.id,
            epic_ref="e",
            wave_ref="w",
            node_ref="n",
            kind="story",
            state="running",
            title="story",
            issue_ref="50",
            attempts=1,
        )
        session.add(node)
        await session.flush()
        run_id = attempt_run_id(node.id, 1)
        envelope = {
            "message_id": run_id,
            "tenant_id": "org",
            "persona": "developer",
            "pr_binding_required": True,
            "orchestration": {"node_id": node.id, "flow_id": flow.id, "attempt": 1},
            "source_ref": {"repo": PR.repo, "installation_id": 123, "provider_repository_id": 42},
        }
        session.add(
            OrchestrationDecision(
                org_id="org",
                flow_id=flow.id,
                node_id=node.id,
                kind="node_dispatched",
                actor_id="engine",
                actor_role="engine",
                actor_kind="service",
                reason=json.dumps(
                    {"run_id": run_id, "attempt": 1, "repo": PR.repo, "issue": 50, "installation_id": 123, "pr_binding_required": True}
                ),
            )
        )
        await prepare_run_report(session, envelope)
        await session.commit()
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: db_session_factory)
    provider = AsyncMock(return_value=PR)
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", provider)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_agent_transport] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield SimpleNamespace(
            client=client,
            provider=provider,
            sessions=db_session_factory,
            node=node,
            envelope=envelope,
            headers={"X-Adp-Report-Credential": envelope["run_report"]["credential"]},
        )


async def test_authority_off_lost_reply_readback_and_terminal(reports):
    r = reports
    first = await r.client.post(URL + "/pull-request", headers=r.headers, json=PR.__dict__)
    assert first.status_code == 200, first.text
    assert first.json()["binding_receipt"]["head_sha"] == PR.head_sha
    retry = await r.client.post(URL + "/pull-request", headers=r.headers, json=PR.__dict__)
    readback = await r.client.get(URL, headers=r.headers)
    assert retry.json()["binding_receipt"] == readback.json()["binding_receipt"]
    assert r.provider.await_count == 1
    done = await r.client.post(URL + "/terminal", headers=r.headers, json={"outcome": "complete"})
    assert done.json()["terminal_receipt"]["outcome"] == "complete"
    async with r.sessions() as session:
        assert len(list(await session.scalars(select(OrchestrationPullRequestBinding)))) == 1
        row = await session.get(OrchestrationRunReport, r.envelope["message_id"])
        assert "credential" not in json.dumps(row.dispatch_metadata)
        assert row.credential_hash != r.headers["X-Adp-Report-Credential"]


async def test_provider_outage_persists_candidate_for_new_worker(reports):
    r = reports
    r.provider.side_effect = PrIdentityError("unavailable")
    response = await r.client.post(URL + "/pull-request", headers=r.headers, json=PR.__dict__)
    assert response.json()["block_code"] == "pr_identity_unavailable"
    assert response.json()["candidate_pr"]["head_sha"] == PR.head_sha
    assert response.json()["retryable"] is True
    r.provider.side_effect = None
    recovered = await r.client.post(URL + "/pull-request/retry", headers=r.headers, json={})
    assert recovered.json()["binding_receipt"]["bound"] is True


@pytest.mark.parametrize(
    "change,code",
    [
        ({"head_sha": "b" * 40}, "head_moved"),
        ({"provider_pr_node_id": "PR_wrong"}, "incomplete_identity"),
        ({"provider_repository_id": 43}, "repository_mismatch"),
    ],
)
async def test_wrong_exact_evidence_stays_typed_block(reports, change, code):
    response = await reports.client.post(URL + "/pull-request", headers=reports.headers, json={**PR.__dict__, **change})
    assert response.json()["block_code"] == code
    assert response.json()["binding_receipt"] is None
    assert response.json()["retryable"] is False


async def test_no_self_asserted_run_or_tenant_auth(reports):
    r = reports
    assert (await r.client.get(URL, headers={"X-Agent-RunId": "run"})).status_code == 404
    assert (await r.client.post(URL + "/pull-request", headers=r.headers, json={**PR.__dict__, "run_id": "victim"})).status_code == 422
    assert (await r.client.post(URL + "/terminal", headers=r.headers, json={"outcome": "complete"})).status_code == 409
    async with r.sessions() as session:
        node = await session.get(OrchestrationNode, r.node.id)
        node.attempts = 2
        await session.commit()
    assert (await r.client.get(URL, headers=r.headers)).status_code == 404


async def test_required_developer_cannot_acknowledge_a_reviewer_artifact(reports):
    response = await reports.client.post(URL + "/pull-request", headers=reports.headers, json={**PR.__dict__, "reviewer_artifact": True})
    assert response.status_code == 409 and response.json()["detail"] == "implementation_binding_required"
    reports.provider.assert_not_awaited()
    async with reports.sessions() as session:
        row = await session.get(OrchestrationRunReport, reports.envelope["message_id"])
        assert row.candidate_pr is None and row.binding_receipt is None
        assert await session.scalar(select(OrchestrationPullRequestBinding)) is None


@pytest.mark.parametrize("persona,required", [("reviewer", True), ("agent-codex-reviewer", True), ("developer", False)])
async def test_reviewer_and_nonrequired_assignments_keep_artifact_reporting(reports, persona, required):
    async with reports.sessions() as session:
        row = await session.get(OrchestrationRunReport, reports.envelope["message_id"])
        row.persona = persona
        row.dispatch_metadata = {**row.dispatch_metadata, "persona": persona, "pr_binding_required": required}
        await session.commit()
    bound = await reports.client.post(URL + "/pull-request", headers=reports.headers, json={**PR.__dict__, "reviewer_artifact": True})
    assert bound.status_code == 200 and bound.json()["binding_receipt"]["role"] == "reviewer_artifact"
    assert bound.json()["binding_receipt"]["state"] == "active"
    assert (await reports.client.post(URL + "/terminal", headers=reports.headers, json={"outcome": "complete"})).status_code == 200


@pytest.mark.parametrize(
    "receipt",
    [None, {}, {"arbitrary": "value"}, [], "malformed", {"bound": True}],
)
async def test_malformed_binding_receipt_never_completes_developer(reports, receipt):
    async with reports.sessions() as session:
        row = await session.get(OrchestrationRunReport, reports.envelope["message_id"])
        row.binding_receipt = receipt
        await session.commit()
    response = await reports.client.post(URL + "/terminal", headers=reports.headers, json={"outcome": "complete"})
    assert response.status_code == 409 and response.json()["detail"] == "pr_binding_unacknowledged"
    # Failure reporting remains possible without claiming successful delivery.
    assert (await reports.client.post(URL + "/terminal", headers=reports.headers, json={"outcome": "failed"})).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"bound": False},
        {"run_id": "other-run"},
        {"attempt": 2},
        {"node_id": "other-node"},
        {"repo": "other/repo"},
        {"provider_repository_id": 99},
        {"provider_pr_node_id": ""},
        {"head_sha": "short"},
        {"role": "reviewer_artifact"},
        {"state": "superseded"},
    ],
)
async def test_terminal_requires_active_implementation_receipt_for_exact_assignment(reports, change):
    result = await reports.client.post(URL + "/pull-request", headers=reports.headers, json=PR.__dict__)
    assert result.status_code == 200
    async with reports.sessions() as session:
        row = await session.get(OrchestrationRunReport, reports.envelope["message_id"])
        row.binding_receipt = {**row.binding_receipt, **change}
        await session.commit()
    response = await reports.client.post(URL + "/terminal", headers=reports.headers, json={"outcome": "complete"})
    assert response.status_code == 409 and response.json()["detail"] == "pr_binding_unacknowledged"


async def test_report_token_reconstructs_after_dispatch_publish_crash(reports, monkeypatch):
    r = reports
    envelope = {k: v for k, v in r.envelope.items() if k != "run_report"}
    async with r.sessions() as session:
        await prepare_run_report(session, envelope)
    assert envelope["run_report"] == r.envelope["run_report"]
    monkeypatch.delenv("AGENT_RUN_CREDENTIAL_KEY")
    async with r.sessions() as session:
        with pytest.raises(RunReportError, match="report_signing_key_unavailable"):
            await prepare_run_report(session, envelope)


@pytest.mark.parametrize("condition", ["cancelled", "expired", "foreign_flow"])
async def test_cancel_expiry_and_tenant_scope_revoke_reports(reports, condition):
    from datetime import timedelta

    from src.shared.models.base import utcnow

    r = reports
    async with r.sessions() as session:
        row = await session.get(OrchestrationRunReport, r.envelope["message_id"])
        if condition == "expired":
            row.expires_at = utcnow() - timedelta(seconds=1)
        else:
            flow = await session.get(OrchestrationFlow, row.flow_id)
            if condition == "cancelled":
                flow.state = "cancelled"
            else:
                flow.org_id = "other"
        await session.commit()
    assert (await r.client.get(URL, headers=r.headers)).status_code == 404


async def test_operator_recovery_block_is_typed_and_complete_requires_review(reports):
    r = reports
    block = await r.client.post(URL + "/block", headers=r.headers, json={"code": "delivery_recovery_required"})
    assert block.json()["block_code"] == "delivery_recovery_required"
    assert block.json()["retryable"] is False
    async with r.sessions() as session:
        row = await session.get(OrchestrationRunReport, r.envelope["message_id"])
        row.dispatch_metadata = {**row.dispatch_metadata, "review_expect": {"head_sha": PR.head_sha}, "pr_binding_required": False}
        await session.commit()
    result = await r.client.post(URL + "/terminal", headers=r.headers, json={"outcome": "complete"})
    assert result.status_code == 409
    assert result.json()["detail"] == "review_result_unacknowledged"


async def test_pending_flow_can_accept_current_assignment_and_start_only_once(reports):
    r = reports
    async with r.sessions() as session:
        flow = await session.get(OrchestrationFlow, r.node.flow_id)
        flow.state = "pending"  # Live legacy Superplane shape.
        await session.commit()
    assert (await r.client.get(URL, headers=r.headers)).status_code == 200
    first = await r.client.post(URL + "/started", headers=r.headers, json={"ownership_nonce": "a" * 32})
    replay = await r.client.post(URL + "/started", headers=r.headers, json={"ownership_nonce": "a" * 32})
    rival = await r.client.post(URL + "/started", headers=r.headers, json={"ownership_nonce": "b" * 32})
    assert first.json()["worker_receipt"] == replay.json()["worker_receipt"]
    assert rival.status_code == 409


@pytest.mark.parametrize("block", ["execution_assignment_unverifiable", "delivery_recovery_required", "report_spool_unavailable"])
@pytest.mark.parametrize("existing_receipt", [False, True])
async def test_start_clears_only_resolved_assignment_or_new_delivery_recovery_block(reports, block, existing_receipt):
    body = {"ownership_nonce": "a" * 32}
    if existing_receipt:
        assert (await reports.client.post(URL + "/started", headers=reports.headers, json=body)).status_code == 200
    async with reports.sessions() as session:
        row = await session.get(OrchestrationRunReport, reports.envelope["message_id"])
        row.block_code, row.retryable = block, True
        await session.commit()
    response = await reports.client.post(URL + "/started", headers=reports.headers, json=body)
    assert response.status_code == 200
    assert response.json()["worker_receipt"]
    resolved = block == "execution_assignment_unverifiable" or (block == "delivery_recovery_required" and not existing_receipt)
    assert response.json()["block_code"] == (None if resolved else block)
    assert response.json()["retryable"] is not resolved


async def test_real_observer_requires_sql_handoff_and_ignores_advisory_completion(reports):
    from unittest.mock import MagicMock

    from src.orchestration.results import observe_results

    r = reports
    store = MagicMock()
    store.get.return_value = {"status": "complete"}
    async with r.sessions() as session:
        result = await observe_results(session, run_store=store)
        assert result.waiting == 1
        assert (await session.get(OrchestrationNode, r.node.id)).state == "running"
    store.get.assert_not_called()


async def test_real_observer_completes_after_authenticated_report_despite_ddb_outage(reports):
    from unittest.mock import MagicMock

    from src.orchestration.pr_bindings import MergeEvidence
    from src.orchestration.results import observe_results

    r = reports
    assert (await r.client.post(URL + "/pull-request", headers=r.headers, json=PR.__dict__)).json()["binding_receipt"]
    assert (await r.client.post(URL + "/terminal", headers=r.headers, json={"outcome": "complete"})).status_code == 200
    store = MagicMock()
    store.get.side_effect = RuntimeError("DDB unavailable")
    source = SimpleNamespace(
        bound_pull_request=AsyncMock(
            return_value=MergeEvidence(
                merged=True,
                head_sha=PR.head_sha,
                checks_successful=True,
                review_approved=True,
                merge_commit_sha="b" * 40,
                merged_at="2026-09-21T01:00:00Z",
                url="https://github.com/org/repo/pull/52",
                provider_repository_id=PR.provider_repository_id,
                provider_pr_node_id=PR.provider_pr_node_id,
            )
        )
    )
    async with r.sessions() as session:
        result = await observe_results(session, run_store=store, evidence=source)
        assert result.errors == 0
        assert (await session.get(OrchestrationNode, r.node.id)).state == "passed"
    store.get.assert_not_called()


async def test_real_observer_rejects_receipt_identity_disagreement(reports):
    from unittest.mock import MagicMock

    from src.orchestration.results import observe_results

    r = reports
    async with r.sessions() as session:
        row = await session.get(OrchestrationRunReport, r.envelope["message_id"])
        row.terminal_receipt = {
            "contract_version": 1,
            "run_id": row.run_id,
            "attempt": 2,
            "outcome": "complete",
            "recorded_at": "2026-09-21T01:00:00Z",
        }
        await session.commit()
        store = MagicMock()
        result = await observe_results(session, run_store=store)
        assert result.errors == 1
        assert (await session.get(OrchestrationNode, r.node.id)).state == "running"
        store.get.assert_not_called()


async def test_worker_cannot_start_after_governed_authority_expires(reports, monkeypatch):
    import sys

    from src.orchestration.review_cycle import CycleBlockedError

    authorizer = AsyncMock(side_effect=CycleBlockedError("policy_expired"))
    monkeypatch.setattr(
        "src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=SimpleNamespace(policy=object(), refusal=None))
    )
    monkeypatch.setitem(sys.modules, "src.orchestration.shared_policy", SimpleNamespace(authorize_shared_model=authorizer))
    response = await reports.client.post(URL + "/started", headers=reports.headers, json={"ownership_nonce": "a" * 32})
    assert response.status_code == 409 and response.json()["detail"] == "policy_expired"
    readback = await reports.client.get(URL, headers=reports.headers)
    assert readback.json()["worker_receipt"] is None and readback.json()["block_code"] == "policy_expired"
