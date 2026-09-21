"""Delivery-loop regression: real API controls, SQL transitions and worker contract.

External execution and GitHub evidence are simulated. These tests establish engine
behavior, never claim that the Superplane cloud acceptance criteria were executed.
"""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from src.activity.schemas import InvocationChainResponse
from src.activity.service import ActivityService
from src.admin.access_control import AdminRole
from src.auth.dependencies import get_current_user
from src.orchestration import controls, routes
from src.orchestration.dispatch_pass import publish_pending, run_dispatch_pass
from src.orchestration.models import DecisionKind, OrchestrationDecision, OrchestrationNode
from src.orchestration.proposal import LoopProposal
from src.orchestration.registration import register_draft_proposal
from src.orchestration.results import observe_results
from src.orchestration.run_store import EngineRunStore
from src.orchestration.stall import detect_stalls
from src.orchestration.tick import run_tick
from src.shared.database import get_db
from tests.orchestration import test_registration as fixtures
from tests.orchestration.test_dispatch_pass import FakeSQS
from tests.orchestration.test_registration import (
    HUMAN_USER_ID,
    ORG_A,
    author_gated_proposal,
    dispatch_config,
    seed_org,
    seed_principal,
    token_context,
)

session = fixtures.session
registrar = fixtures.registrar
access = fixtures.access


class MemoryTable:
    def __init__(self):
        self.items = {}

    def put_item(self, *, Item, ConditionExpression, ReturnValuesOnConditionCheckFailure="NONE"):  # noqa: N803
        from boto3.dynamodb.types import TypeSerializer
        from botocore.exceptions import ClientError

        key = Item["event_id"], Item["arrived_at"]
        if key in self.items:
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            if ReturnValuesOnConditionCheckFailure == "ALL_OLD":
                response["Item"] = {k: TypeSerializer().serialize(v) for k, v in self.items[key].items()}
            raise ClientError(response, "PutItem")
        self.items[key] = deepcopy(Item)

    def get_item(self, *, Key, ConsistentRead):  # noqa: N803
        return {"Item": deepcopy(self.items.get((Key["event_id"], Key["arrived_at"])))}


class Evidence:
    def __init__(self):
        self.merged = False
        self.calls = []

    async def merged_story(self, **kwargs):
        self.calls.append(kwargs)
        return "https://github.com/aws-e/adp/pull/9999" if self.merged else None

    async def bound_pull_request(self, **kwargs):
        from src.orchestration.pr_bindings import MergeEvidence

        self.calls.append(kwargs)
        return MergeEvidence(
            merged=self.merged,
            head_sha="a" * 40,
            checks_successful=True,
            provider_repository_id=12345,
            provider_pr_node_id=f"PR_{kwargs['pr_number']}",
            review_approved=True,
            merge_commit_sha="b" * 40,
            merged_at="2026-09-17T09:00:00Z" if self.merged else None,
            url=f"https://github.com/{kwargs['repo']}/pull/{kwargs['pr_number']}",
        )


@pytest.fixture
async def api(session, access, monkeypatch):
    # Execution and GitHub evidence use local doubles below; the optional
    # display-history read must also stay offline. Completed stories now retain
    # history, so a full topology walk otherwise repeats real DynamoDB queries
    # on every graph refresh (and eventually times out with fixture credentials).
    activity = Mock(spec=ActivityService)

    def empty_chain(*, correlation_id, tenant_id):
        assert tenant_id == ORG_A
        return InvocationChainResponse(correlation_id=correlation_id, items=[], total_count=0)

    activity.get_chain.side_effect = empty_chain
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: activity)

    # Retried stories now revalidate and retain their provider PR identity.
    # Keep that read on the same fixture provider as initial registration.
    async def resolve_pr(**kwargs):
        from src.orchestration.pr_bindings import PullRequestIdentity

        number = kwargs["pr_number"]
        return PullRequestIdentity(12345, f"PR_{number}", kwargs["repo"], number, "a" * 40)

    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", resolve_pr)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(controls.router)
    context = token_context(ORG_A, user_id=HUMAN_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: context
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[routes.get_access_control] = lambda: access
    app.dependency_overrides[controls.get_access_control] = lambda: access
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield client


async def setup_plan(session, registrar, proposal=None):
    await seed_org(session)
    await seed_principal(session, org_id=ORG_A, role=AdminRole.ORG_ADMIN.value)
    proposal = proposal or author_gated_proposal()
    # These are fixture-only issue refs for already-materialized eval tasks.
    for i, node in enumerate(proposal.nodes):
        if node.kind == "eval":
            node.issue_ref = str(9000 + i)
    result, _ = await register_draft_proposal(session, proposal, registrar)
    await session.commit()
    return result.flow_id


async def graph(api, flow_id, *, by_id=False):
    response = await api.get(f"/orchestration/flows/{flow_id}")
    assert response.status_code == 200, response.text
    return {node["id"] if by_id else node["node_ref"]: node for node in response.json()["nodes"]}


async def approve(api, node):
    response = await api.post(f"/orchestration/gates/{node['id']}/approve", json={"reason": "Reviewed fixture evidence"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"


async def tick_dispatch(session, store):
    await run_tick(session)
    report = await run_dispatch_pass(session, dispatch_config())
    assert report.errors == 0
    await session.commit()
    sqs = FakeSQS()
    publish_pending(report, dispatch_config(), client=sqs, run_store=store)
    assert report.publish_failed == 0
    envelopes = [sqs.envelope(i) for i in range(len(sqs.calls))]
    # Simulate the worker's registration boundary with provider-issued identity.
    from src.orchestration.pr_bindings import PullRequestIdentity, register_binding, resolve_registration_target
    from src.orchestration.state import ActorKind

    for envelope in envelopes:
        if envelope.get("pr_binding_required"):
            target = await resolve_registration_target(session, run_id=envelope["message_id"])
            number = int(target.node_id.replace("-", "")[:7], 16)
            await register_binding(
                session,
                target=target,
                actor_id=target.run_id,
                actor_kind=ActorKind.SERVICE,
                pr=PullRequestIdentity(12345, f"PR_{number}", "aws-e/adp", number, "a" * 40),
            )
    await session.commit()
    return envelopes


def finish(table, envelope, status="complete", **updates):
    row = table.items[envelope["message_id"], envelope["arrived_at"]]
    row.update(status=status, **updates)


async def test_two_wave_flow_via_ui_controls(session, registrar, api):
    flow_id = await setup_plan(session, registrar)
    table, evidence = MemoryTable(), Evidence()
    store = EngineRunStore(table)
    nodes = await graph(api, flow_id)
    assert await tick_dispatch(session, store) == []
    await approve(api, next(n for n in nodes.values() if n["state"] == "awaiting_gate"))
    (first,) = await tick_dispatch(session, store)
    assert first["persona"] == "developer"
    assert first["actor"]["user_id"] == f"user-{HUMAN_USER_ID}"
    assert first["cognito_sub"] == HUMAN_USER_ID
    assert first["message_id"].startswith("orch:")
    finish(table, first)
    observed = await observe_results(session, run_store=store, evidence=evidence)
    assert observed.advanced == 1
    assert (await graph(api, flow_id))["story-a"]["state"] == "awaiting_merge"
    # Waiting for review is not a worker stall, even after the job deadline.
    stalls = await detect_stalls(session, now=datetime.now(UTC) + timedelta(days=2))
    assert stalls.stalls_detected == 0
    assert await tick_dispatch(session, store) == []
    evidence.merged = True
    completed = await observe_results(session, run_store=store, evidence=evidence)
    assert completed.advanced == 1
    repeated = await observe_results(session, run_store=store, evidence=evidence)
    assert repeated.advanced == 0
    (evaluation,) = await tick_dispatch(session, store)
    assert await tick_dispatch(session, store) == []
    assert evaluation["persona"] == "operations"
    finish(table, evaluation)
    await observe_results(session, run_store=store, evidence=evidence)
    assert (await graph(api, flow_id))["eval-w1"]["state"] == "running"
    finish(table, evaluation, transcript_key="fixture/evaluation.txt")
    await observe_results(session, run_store=store, evidence=evidence)
    nodes = await graph(api, flow_id)
    assert nodes["eval-w1"]["state"] == "awaiting_gate"
    assert nodes["eval-w1"]["run_id"] == evaluation["message_id"]
    assert nodes["eval-w1"]["issue_url"].endswith("/issues/9001")
    assert await tick_dispatch(session, store) == []
    await approve(api, nodes["eval-w1"])
    assert await tick_dispatch(session, store) == []
    nodes = await graph(api, flow_id)
    assert nodes["my-gate"]["state"] == "awaiting_gate"
    assert nodes["my-gate"]["attempts"] == 0
    rejected = await api.post(f"/orchestration/gates/{nodes['my-gate']['id']}/reject", json={"reason": "Fixture change requested"})
    assert rejected.status_code == 200
    assert await tick_dispatch(session, store) == []
    resumed = await api.post(f"/orchestration/nodes/{nodes['my-gate']['id']}/resume", json={"reason": "Fixture resolved"})
    assert resumed.status_code == 200, resumed.text
    assert await tick_dispatch(session, store) == []
    await approve(api, (await graph(api, flow_id))["my-gate"])
    (second,) = await tick_dispatch(session, store)
    assert second["source_ref"]["issue"] == 4529
    finish(table, second)
    await observe_results(session, run_store=store, evidence=evidence)
    (final_eval,) = await tick_dispatch(session, store)
    finish(table, final_eval, transcript_key="fixture/final.txt")
    await observe_results(session, run_store=store, evidence=evidence)
    await approve(api, (await graph(api, flow_id))["eval-w2"])
    assert all(n["state"] == "passed" for n in (await graph(api, flow_id)).values())
    assert await tick_dispatch(session, store) == []


@pytest.mark.parametrize("tamper", ["tenant", "attempt", "missing", "skipped", "failure"])
async def test_result_identity_and_failure(session, registrar, api, tamper):
    flow_id = await setup_plan(session, registrar)
    table, evidence = MemoryTable(), Evidence()
    evidence.merged = True
    store = EngineRunStore(table)
    await approve(api, next(n for n in (await graph(api, flow_id)).values() if n["state"] == "awaiting_gate"))
    (envelope,) = await tick_dispatch(session, store)
    finish(table, envelope)
    row = table.items[envelope["message_id"], envelope["arrived_at"]]
    if tamper == "tenant":
        row["tenant_id"] = "other-tenant"
    if tamper == "attempt":
        row["engine_attempt"] = 99
    if tamper == "missing":
        table.items.clear()
    if tamper == "skipped":
        row["status"] = "skipped"
    if tamper == "failure":
        row["status"] = "failed"
    report = await observe_results(session, run_store=store, evidence=evidence)
    assert report.errors == (1 if tamper in {"tenant", "attempt"} else 0)
    node = (await graph(api, flow_id))["story-a"]
    assert node["state"] == ("failed" if tamper == "failure" else "running")
    assert not evidence.calls
    if tamper == "failure":
        resumed = await api.post(f"/orchestration/nodes/{node['id']}/resume", json={"reason": "Retry fixture"})
        assert resumed.status_code == 200
        (retry,) = await tick_dispatch(session, store)
        assert retry["message_id"] != envelope["message_id"]
        assert retry["orchestration"]["attempt"] == 2
        assert (await graph(api, flow_id))["story-a"]["result_summary"] is None


async def test_run_registration_is_idempotent_and_before_publish(session, registrar, api):
    flow_id = await setup_plan(session, registrar)
    table = MemoryTable()
    store = EngineRunStore(table)
    await approve(api, next(n for n in (await graph(api, flow_id)).values() if n["state"] == "awaiting_gate"))
    (envelope,) = await tick_dispatch(session, store)
    finish(table, envelope)
    store.register(envelope)
    assert store.get(envelope["message_id"], envelope["arrived_at"])["status"] == "complete"
    conflicting = deepcopy(envelope)
    conflicting["tenant_id"] = "other-tenant"
    with pytest.raises(RuntimeError, match="identity conflicts"):
        store.register(conflicting)


async def test_actual_superplane_topology_reaches_every_gate(session, registrar, api):
    payload = json.loads((Path(__file__).parent / "fixtures/superplane-4910-r2.json").read_text())
    payload["org_id"] = ORG_A
    flow_id = await setup_plan(session, registrar, LoopProposal.model_validate(payload))
    table, evidence = MemoryTable(), Evidence()
    evidence.merged = True
    store = EngineRunStore(table)
    dispatched = []
    for _ in range(80):
        nodes = await graph(api, flow_id, by_id=True)
        if all(n["state"] == "passed" for n in nodes.values()):
            break
        for node in nodes.values():
            if node["state"] == "awaiting_gate":
                await approve(api, node)
        envelopes = await tick_dispatch(session, store)
        dispatched.extend(envelopes)
        for envelope in envelopes:
            finish(table, envelope, transcript_key="fixture/evaluation.txt")
        report = await observe_results(session, run_store=store, evidence=evidence)
        assert report.errors == 0
    else:
        pytest.fail("Superplane graph did not finish within bounded fixture cycles")
    assert len(dispatched) == 31  # 27 stories + four evaluations; gates use no worker.
    assert sum(e["persona"] == "operations" for e in dispatched) == 4
    assert all(n["attempts"] == 0 for n in nodes.values() if n["kind"] == "gate")


async def test_no_message_is_sent_if_run_registration_fails(session, registrar, api):
    from unittest.mock import Mock

    flow_id = await setup_plan(session, registrar)
    await approve(api, next(n for n in (await graph(api, flow_id)).values() if n["state"] == "awaiting_gate"))
    await run_tick(session)
    report = await run_dispatch_pass(session, dispatch_config())
    await session.commit()
    sqs, store = FakeSQS(), Mock()
    store.register.side_effect = RuntimeError("DynamoDB unavailable")
    publish_pending(report, dispatch_config(), client=sqs, run_store=store)
    assert report.publish_failed == 1
    assert sqs.calls == []


async def test_recovered_ready_gate_still_checks_predecessors(session, registrar, api):
    flow_id = await setup_plan(session, registrar)
    nodes = await graph(api, flow_id)
    gate = await session.get(OrchestrationNode, nodes["my-gate"]["id"])
    gate.state = "ready"  # Existing pre-fix node.
    await session.flush()
    await run_tick(session)
    assert (await graph(api, flow_id))["my-gate"]["state"] == "ready"
    predecessor = await session.get(OrchestrationNode, nodes["eval-w1"]["id"])
    predecessor.state = "passed"
    await session.flush()
    await run_tick(session)
    await run_tick(session)
    presented = (await graph(api, flow_id))["my-gate"]
    assert presented["state"] == "awaiting_gate"
    assert presented["attempts"] == 0
    decisions = (
        (
            await session.execute(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == gate.id,
                    OrchestrationDecision.kind == DecisionKind.GATE_PRESENTED.value,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(decisions) == 1


@pytest.mark.parametrize("case", ["valid", "open", "manual_close", "not_merged", "old_merge", "pending_checks", "no_checks", "api_error"])
async def test_github_evidence_requires_merged_code_and_successful_checks(monkeypatch, case):
    from unittest.mock import AsyncMock, Mock

    from src.admin.connections import github_client
    from src.knowledge import github_app_service
    from src.orchestration.results import GitHubEvidenceSource

    monkeypatch.setattr(github_app_service, "resolve_tenant_app_credentials", AsyncMock(return_value=("app", "fixture-key")))
    app = Mock()
    app.get_installation_token = AsyncMock(return_value="fixture-token")
    app.aclose = AsyncMock()
    monkeypatch.setattr(github_client, "GitHubAppClient", Mock(return_value=app))
    closer = {
        "__typename": "PullRequest",
        "url": "https://github.com/aws-e/adp/pull/99",
        "merged": True,
        "mergedAt": "2026-09-13T12:00:00Z",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
    }
    issue = {"state": "CLOSED", "stateReason": "COMPLETED", "timelineItems": {"nodes": [{"closer": closer}]}}
    if case == "open":
        issue["state"] = "OPEN"
    if case == "manual_close":
        issue["timelineItems"]["nodes"].append({"closer": None})
    if case == "not_merged":
        closer["merged"] = False
    if case == "old_merge":
        closer["mergedAt"] = "2026-09-12T12:00:00Z"
    if case == "pending_checks":
        closer["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "PENDING"
    if case == "no_checks":
        closer["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
    payload = {"data": {"repository": {"issue": issue}}}
    if case == "api_error":
        payload = {"errors": [{"message": "Unavailable"}]}
    real_client = httpx.AsyncClient

    def handle(request):
        body = json.loads(request.content)
        assert body["variables"] == {"owner": "aws-e", "name": "adp", "issue": 11}
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    source = GitHubEvidenceSource()
    if case == "api_error":
        with pytest.raises(RuntimeError):
            await source.merged_story(org_id=ORG_A, installation_id=55, repo="aws-e/adp", issue=11, since="2026-09-13T11:00:00Z")
    else:
        result = await source.merged_story(org_id=ORG_A, installation_id=55, repo="aws-e/adp", issue=11, since="2026-09-13T11:00:00Z")
        assert result == (closer["url"] if case == "valid" else None)
    app.aclose.assert_awaited_once()


async def test_human_can_retry_work_awaiting_merge_without_reusing_old_result(session, registrar, api):
    flow_id = await setup_plan(session, registrar)
    table, evidence = MemoryTable(), Evidence()
    store = EngineRunStore(table)
    await approve(api, next(n for n in (await graph(api, flow_id)).values() if n["state"] == "awaiting_gate"))
    (original,) = await tick_dispatch(session, store)
    finish(table, original)
    await observe_results(session, run_store=store, evidence=evidence)
    node = (await graph(api, flow_id))["story-a"]
    assert node["state"] == "awaiting_merge"
    response = await api.post(f"/orchestration/nodes/{node['id']}/resume", json={"reason": "Correct the implementation"})
    assert response.status_code == 200, response.text
    (retry,) = await tick_dispatch(session, store)
    assert retry["message_id"] != original["message_id"]
    evidence.merged = True
    # A terminal old run never passes the new attempt; its row is still queued.
    await observe_results(session, run_store=store, evidence=evidence)
    node = (await graph(api, flow_id))["story-a"]
    assert node["state"] == "running"
    assert node["result_summary"] is None


async def accepted_document(api, flow_id):
    response = await api.get(f"/orchestration/flows/{flow_id}/plans")
    assert response.status_code == 200, response.text
    return response.json()[-1]["plan_document"]


async def test_amendment_reconciles_executable_plan_and_runs_to_completion(session, registrar, api):
    flow_id = await setup_plan(session, registrar)
    original = await accepted_document(api, flow_id)
    amended = deepcopy(original)
    amended["spec_revision"] += "-amended"
    amended["title"] = "Amended delivery loop"
    removed = next(n for n in amended["nodes"] if n["address"].endswith("/my-gate"))
    amended["nodes"].remove(removed)
    before = next(e["from_address"] for e in amended["edges"] if e["to_address"] == removed["address"])
    after = next(e["to_address"] for e in amended["edges"] if e["from_address"] == removed["address"])
    amended["edges"] = [e for e in amended["edges"] if removed["address"] not in e.values()]
    amended["edges"].append({"from_address": before, "to_address": after})
    for n in amended["nodes"]:
        if n["kind"] == "eval":
            n.update(issue_ref="5071", title="Review amended evaluation")
    response = await api.post(f"/orchestration/flows/{flow_id}/amendments", json=amended)
    assert response.status_code == 200, response.text
    detail = (await api.get(f"/orchestration/flows/{flow_id}")).json()
    assert len(detail["edges"]) == len(amended["edges"])
    nodes = await graph(api, flow_id)
    assert nodes["my-gate"]["state"] == "superseded"
    assert nodes["eval-w1"]["issue_ref"] == "5071"
    assert nodes["eval-w1"]["title"] == "Review amended evaluation"
    versions = (await api.get(f"/orchestration/flows/{flow_id}/plans")).json()
    assert versions[0]["plan_document"] == original
    table, evidence = MemoryTable(), Evidence()
    evidence.merged = True
    store = EngineRunStore(table)
    dispatched = []
    for _ in range(12):
        nodes = await graph(api, flow_id)
        if all(n["state"] in {"passed", "superseded"} for n in nodes.values()):
            break
        for node in nodes.values():
            if node["state"] == "awaiting_gate":
                await approve(api, node)
        envelopes = await tick_dispatch(session, store)
        dispatched.extend(envelopes)
        for envelope in envelopes:
            finish(table, envelope, transcript_key="fixture/amended.txt")
        await observe_results(session, run_store=store, evidence=evidence)
    assert all(n["state"] in {"passed", "superseded"} for n in (await graph(api, flow_id)).values())
    assert [e["source_ref"]["issue"] for e in dispatched if e["persona"] == "operations"] == [5071, 5071]


@pytest.mark.parametrize(
    "state,attempts,change",
    [
        ("running", 1, "issue_ref"),
        ("ready", 1, "issue_ref"),
        ("passed", 1, "issue_ref"),
        ("pending", 0, "kind"),
        ("superseded", 0, "title"),
    ],
)
async def test_amendment_cannot_reinterpret_execution_history(session, registrar, api, state, attempts, change):
    flow_id = await setup_plan(session, registrar)
    original = await accepted_document(api, flow_id)
    amended = deepcopy(original)
    target = next(n for n in amended["nodes"] if n["address"].endswith("/my-gate"))
    row = (await session.execute(select(OrchestrationNode).where(OrchestrationNode.node_ref == "my-gate"))).scalar_one()
    row.state, row.attempts = state, attempts
    await session.commit()
    target[change] = {"issue_ref": "5071", "kind": "story", "title": "Reused node"}[change]
    response = await api.post(f"/orchestration/flows/{flow_id}/amendments", json=amended)
    assert response.status_code == 422, response.text
    assert "new node address" in response.text
    assert await accepted_document(api, flow_id) == original
    assert len((await api.get(f"/orchestration/flows/{flow_id}/plans")).json()) == 1
    assert (await graph(api, flow_id))["my-gate"]["state"] == state


async def test_amendment_added_prerequisite_blocks_already_ready_story(session, registrar, api):
    flow_id = await setup_plan(session, registrar)
    table = MemoryTable()
    store = EngineRunStore(table)
    await approve(api, next(n for n in (await graph(api, flow_id)).values() if n["state"] == "awaiting_gate"))
    await run_tick(session)
    await session.commit()
    assert (await graph(api, flow_id))["story-a"]["state"] == "ready"
    amended = await accepted_document(api, flow_id)
    story = next(n for n in amended["nodes"] if n["address"].endswith("/story-a"))
    new_gate = story["address"].rsplit("/", 1)[0] + "/additional-review"
    amended["nodes"].append({"address": new_gate, "kind": "gate", "title": "Additional review"})
    amended["edges"].append({"from_address": new_gate, "to_address": story["address"]})
    response = await api.post(f"/orchestration/flows/{flow_id}/amendments", json=amended)
    assert response.status_code == 200, response.text
    assert await tick_dispatch(session, store) == []
    await approve(api, (await graph(api, flow_id))["additional-review"])
    (envelope,) = await tick_dispatch(session, store)
    assert envelope["source_ref"]["issue"] == 4527


async def test_amendment_rejects_new_prerequisite_for_started_work_atomically(session, registrar, api):
    flow_id = await setup_plan(session, registrar)
    store = EngineRunStore(MemoryTable())
    await approve(api, next(n for n in (await graph(api, flow_id)).values() if n["state"] == "awaiting_gate"))
    await tick_dispatch(session, store)
    original = await accepted_document(api, flow_id)
    amended = deepcopy(original)
    story = next(n for n in amended["nodes"] if n["address"].endswith("/story-a"))
    gate = story["address"].rsplit("/", 1)[0] + "/late-review"
    story["title"] = "Must be rolled back"
    amended["nodes"].append({"address": gate, "kind": "gate", "title": "Late prerequisite"})
    amended["edges"].append({"from_address": gate, "to_address": story["address"]})
    response = await api.post(f"/orchestration/flows/{flow_id}/amendments", json=amended)
    assert response.status_code == 422, response.text
    nodes = await graph(api, flow_id)
    assert nodes["story-a"]["title"] == "Story A"
    assert "late-review" not in nodes
    assert await accepted_document(api, flow_id) == original


@pytest.fixture(autouse=True)
def provider_repository_identity(monkeypatch):
    """Dispatch resolves immutable GitHub identity even with work claims off."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=12345))
