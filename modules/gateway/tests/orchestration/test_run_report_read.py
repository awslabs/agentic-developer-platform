"""Human observation of real SQL acknowledgments cannot grant run authority."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.base import utcnow
from tests.orchestration import test_execution_read as base

session = base.session
app_with_router = base.app_with_router


def route(flow):
    return f"/orchestration/flows/{flow.id}/run-reports"


async def seed(session, *, flow=None, node=None, org_id=base.ORG_A):
    flow = flow or await base.seed_flow(session, org_id=org_id, slug=str(uuid4()))
    node = node or await base.seed_node(session, flow, org_id=org_id, node_ref=str(uuid4()))
    node.attempts = 1
    run_id = "orch:" + str(uuid4())
    row = OrchestrationRunReport(
        run_id=run_id,
        credential_hash=hashlib.sha256(run_id.encode()).hexdigest(),
        org_id=org_id,
        flow_id=flow.id,
        node_id=node.id,
        attempt=1,
        persona="developer",
        repo="aws-e/adp",
        installation_id=1,
        provider_repository_id=123,
        dispatch_metadata={"credential": "do-not-expose", "execution_continuation": {"claim_id": "secret-claim"}},
        candidate_pr={"sensitive_candidate_field": "do-not-expose"},
        expires_at=utcnow() + timedelta(days=1),
    )
    session.add(row)
    await session.flush()
    return flow, node, row


def start(row):
    return {"run_id": row.run_id, "attempt": row.attempt, "ownership_nonce": "never-emit-this-ownership-nonce", "recorded_at": utcnow().isoformat()}


def terminal(row, outcome="complete"):
    return {"run_id": row.run_id, "attempt": row.attempt, "contract_version": 1, "outcome": outcome, "recorded_at": utcnow().isoformat()}


def bind(row):
    return {
        "run_id": row.run_id,
        "attempt": row.attempt,
        "node_id": row.node_id,
        "bound": True,
        "repo": row.repo,
        "pr_number": 5500,
        "head_sha": "a" * 40,
        "role": "implementation",
        "state": "active",
        "registered_by": "private-attribution",
    }


async def test_assignment_is_not_a_worker_start_or_terminal_acknowledgment(session, app_with_router):
    flow, _, _ = await seed(session)
    response = base.client_for(app_with_router).get(route(flow))
    assert response.status_code == 200, response.text
    row = response.json()["reports"][0]
    assert row["status"] == "assigned" and row["binding"] is None
    assert not any(receipt["acknowledged"] for receipt in row["receipts"])


@pytest.mark.parametrize("outcome", ["complete", "failed"])
async def test_true_acknowledgments_expose_only_typed_observations(session, app_with_router, outcome):
    flow, _, row = await seed(session)
    row.worker_receipt, row.binding_receipt, row.terminal_receipt = start(row), bind(row), terminal(row, outcome)
    row.review_receipt = {
        "run_id": row.run_id,
        "attempt": row.attempt,
        "contract_version": 1,
        "recorded": True,
        "uri": "s3://private/review",
        "detail": "untrusted prose",
    }
    await session.flush()
    response = base.client_for(app_with_router).get(route(flow))
    assert response.status_code == 200, response.text
    body = response.json()
    observed = body["reports"][0]
    assert observed["status"] == "terminal_acknowledged"
    receipts = {r["kind"]: r for r in observed["receipts"]}
    assert all(r["acknowledged"] for r in receipts.values())
    assert receipts["terminal"]["outcome"] == outcome
    assert receipts["worker_started"]["recorded_at"]
    assert receipts["pull_request_bound"]["recorded_at"] is None
    assert receipts["review"]["recorded_at"] is None
    assert observed["binding"]["head_sha"] == "a" * 40
    for forbidden in (
        "credential",
        "nonce",
        "claim",
        "do-not-expose",
        "private-attribution",
        "s3://",
        "untrusted prose",
        "dispatch_metadata",
        "candidate_pr",
    ):
        assert forbidden not in json.dumps(body)


async def test_start_acknowledgment_is_observable_before_completion(session, app_with_router):
    flow, _, row = await seed(session)
    row.worker_receipt = start(row)
    await session.flush()
    observed = base.client_for(app_with_router).get(route(flow)).json()["reports"][0]
    assert observed["status"] == "worker_started"
    assert not next(r for r in observed["receipts"] if r["kind"] == "terminal")["acknowledged"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "another-run"),
        ("attempt", 2),
        ("attempt", True),
        ("outcome", "APPROVE"),
        ("recorded_at", "invalid"),
        ("recorded_at", "2026-09-21T00:00:00"),
    ],
)
async def test_malformed_or_cross_run_terminal_is_not_acknowledged(session, app_with_router, field, value):
    flow, _, row = await seed(session)
    row.terminal_receipt = terminal(row) | {field: value}
    await session.flush()
    observed = base.client_for(app_with_router).get(route(flow)).json()["reports"][0]
    assert observed["status"] == "unverifiable"
    assert not next(r for r in observed["receipts"] if r["kind"] == "terminal")["acknowledged"]


async def test_unknown_and_other_tenant_flow_have_identical_404(session, app_with_router):
    flow, _, _ = await seed(session, org_id=base.ORG_B)
    client = base.client_for(app_with_router)
    hidden = client.get(route(flow))
    missing = client.get("/orchestration/flows/00000000-0000-0000-0000-000000000000/run-reports")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


async def test_report_and_node_are_both_tenant_and_flow_scoped(session, app_with_router):
    mine, mine_node, mine_row = await seed(session)
    other, other_node, _ = await seed(session, org_id=base.ORG_B)
    # A malformed row with my flow/org but another tenant's node cannot leak
    # through an org-only report query. The composite read join fences it.
    _, _, malformed = await seed(session, flow=mine, node=other_node)
    malformed.flow_id = mine.id
    await seed(session, flow=mine, node=mine_node, org_id=base.ORG_B)
    await session.flush()
    response = base.client_for(app_with_router).get(route(mine))
    assert response.status_code == 200, response.text
    assert [r["run_id"] for r in response.json()["reports"]] == [mine_row.run_id]
    assert response.json()["total"] == 1


async def test_permission_denied_before_any_database_read(session, app_with_router):
    flow, _, _ = await seed(session)
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        response = base.client_for(app_with_router, permitted=False).get(route(flow))
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", capture)
    assert response.status_code == 403
    assert statements == []


async def test_empty_flow_is_not_fabricated_report_success(session, app_with_router):
    flow = await base.seed_flow(session)
    body = base.client_for(app_with_router).get(route(flow)).json()
    assert body["reports"] == [] and body["total"] == 0


async def test_history_marks_previous_attempt_and_paginates_without_writes(session, app_with_router):
    flow, node, first = await seed(session)
    _, _, second = await seed(session, flow=flow, node=node)
    second.attempt = node.attempts = 2
    first.terminal_receipt = terminal(first)
    await session.flush()
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        client = base.client_for(app_with_router)
        one = client.get(route(flow) + "?limit=1&offset=0").json()
        two = client.get(route(flow) + "?limit=1&offset=1").json()
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", capture)
    assert one["total"] == two["total"] == 2
    assert one["reports"][0]["is_current_attempt"] is False
    assert two["reports"][0]["is_current_attempt"] is True
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert not any("credential_hash" in sql or "dispatch_metadata" in sql or "candidate_pr" in sql for sql in statements)
    assert client.get(route(flow) + "?limit=201").status_code == 422
    assert client.post(route(flow), json={}).status_code == 405
    assert client.delete(route(flow)).status_code == 405
