"""Issue #5365: a human-summoned root coordinator fans out across its repository.

The live failure these cover: a coordinator summoned by a human on a tracking
issue was refused on every story it was summoned to hand out, because its grant
pins it to the one issue it was launched on. The capability that lifts that pin is
written only by the HMAC-verified webhook writer, so these tests drive the real
writer, the real dispatch service and the real HTTP route rather than stubs.

Everything else must stay exactly as narrow as it was: children inherit nothing,
an orchestration-rooted (``gate_decision``) run cannot use the escape to step
around the graph and its approval gate, and no budget or depth ceiling moves.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.coordinator import resolve_repository_fan_out
from src.agentauth.dispatch import FAN_OUT_CAPABILITY, FAN_OUT_CAPABILITY_FIELD, FAN_OUT_REPOSITORY_FIELD
from src.agentauth.routes import AgentRuntime, get_agent_runtime, router
from src.agentauth.workload import WORKLOAD_HEADER, VerifiedPod
from src.orchestration.models import OrchestrationFlow
from tests.agentauth.test_graph_dispatch import engine as engine_fixture
from tests.agentauth.test_graph_dispatch import session as session_fixture
from tests.agentauth.test_graph_dispatch import session_factory as session_factory_fixture
from tests.agentauth.test_human_dispatch import child_dispatch as child_dispatch_fixture
from tests.agentauth.test_human_dispatch import store as store_fixture
from tests.agentauth.test_human_dispatch import webhook
from tests.orchestration.test_dispatch_pass import _make_approval, _make_flow, _make_node, _make_org

store = store_fixture
child_dispatch = child_dispatch_fixture
engine = engine_fixture
session = session_fixture
session_factory = session_factory_fixture

# The stories the live coordinator was refused on (2026-09-17), used verbatim so
# the positive case is the reported failure rather than a convenient analogue.
STORIES = (5333, 5337, 5338)


@pytest.fixture
async def fan_out_context(store, child_dispatch, session, session_factory, monkeypatch):
    """A root Operations coordinator with no orchestration flow of its own.

    Deliberately *not* enrolled into a flow: with no flow naming issue 42 as its
    intent, ``resolve_coordinator_assignment`` leaves the run on its launch
    ``github_event`` authority. That is exactly the shape of the coordinator in
    the live incident, and the shape this capability exists to serve.
    """
    await _make_org(session, org_id="tenant", installations=[123])
    await session.commit()
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    pods = {
        "root-pod-proof": VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2"),
        "child-pod-proof": VerifiedPod("pod-b", "worker-b", "adp-agents", "agent-scaledjob-sa", "10.0.1.3"),
    }
    from src.agentauth.run_credential import CREDENTIAL_KEY_ENV

    env = {CREDENTIAL_KEY_ENV: "gateway-test-key-not-shared-with-workers", "BG_ORCH_DISPATCH_REPO": "org/repo"}
    runtime = AgentRuntime(store=store, workloads=SimpleNamespace(verify=lambda token: pods[token]), env=env, dispatcher=child_dispatch.service)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    original = store._read("TENANT#tenant", f"EXEC#{child_dispatch.invocation}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield SimpleNamespace(
            client=client,
            runtime=runtime,
            store=store,
            child=child_dispatch,
            session_factory=session_factory,
            headers={"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: "root-pod-proof"},
            bootstrap={"invocation_id": child_dispatch.invocation, "envelope_digest": original["envelope_digest"]["S"]},
        )


async def enroll(ctx, headers=None, bootstrap=None, pod="root-pod-proof"):
    target = headers if headers is not None else ctx.headers
    response = await ctx.client.post("/internal/v1/agent/bootstrap", json=bootstrap or ctx.bootstrap, headers=target)
    assert response.status_code == 200, response.text
    target["X-Adp-Run-Credential"] = response.json()["credential"]
    return response.json()


def story_body(issue: int, *, persona: str = "developer"):
    return {
        "persona": persona,
        "target": {"repo": "org/repo", "issue": issue},
        "request_id": f"story-{issue}",
        "reason": "Implement the approved story",
    }


async def send(ctx, body, headers=None):
    return await ctx.client.post("/internal/v1/agent/dispatch", json=body, headers=headers if headers is not None else ctx.headers)


def messages(ctx):
    return ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=10).get("Messages", [])


def release(ctx, invocation: str) -> None:
    """Finish a child so the next dispatch is sequential, not concurrent."""
    row = ctx.store._read("TENANT#tenant", f"EXEC#{invocation}")
    ctx.store.authority.release_dispatch(grant_id=row["parent_grant_id"]["S"], tenant_id="tenant", reservation_id=row["dispatch_reservation_id"]["S"])


def test_writer_and_reader_agree_on_the_capability_marker():
    """The gateway reader and the Lambda writer are separate deployables.

    A silent divergence in these strings fails open on the writer and closed on
    the reader, which is a security check that stops matching without failing.
    """
    assert (webhook.FAN_OUT_CAPABILITY_FIELD, webhook.FAN_OUT_CAPABILITY, webhook.FAN_OUT_REPOSITORY_FIELD) == (
        FAN_OUT_CAPABILITY_FIELD,
        FAN_OUT_CAPABILITY,
        FAN_OUT_REPOSITORY_FIELD,
    )


def test_human_writer_stamps_coordinators_only_and_never_from_the_envelope(store):
    """Only coordinator personas get the marker, and only with the verified repo."""
    from tests.agentauth.test_human_dispatch import envelope, event

    developer = webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client)
    raw = store._read("TENANT#tenant", f"GRANT#{developer['message_id']}#1")
    assert FAN_OUT_CAPABILITY_FIELD not in raw and FAN_OUT_REPOSITORY_FIELD not in raw

    coordinator = {**envelope(), "persona": "aidlc"}
    final = webhook.provision_human_dispatch(envelope=coordinator, event=event(), client=store.client)
    raw = store._read("TENANT#tenant", f"GRANT#{final['message_id']}#1")
    assert raw[FAN_OUT_CAPABILITY_FIELD] == {"S": FAN_OUT_CAPABILITY}
    # The repository is the HMAC-verified event's, so a widened envelope cannot
    # widen the scope: a mismatch is refused outright rather than stamped.
    assert raw[FAN_OUT_REPOSITORY_FIELD] == {"S": "org/repo"}
    widened = {**envelope(), "persona": "aidlc", "source_ref": {"repo": "attacker/repo", "issue": 42}}
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.provision_human_dispatch(envelope=widened, event=event(), client=store.client)


async def test_root_coordinator_dispatches_three_stories_within_unchanged_ceilings(fan_out_context):
    """Issue #5365 CD-7: the reported failure, now accepted.

    A coordinator launched on tracking issue 42 hands work to #5333, #5337 and
    #5338 in its own repository, sequentially. Each child is a sibling at the
    coordinator's depth + 1, and the coordinator's own depth never moves — so
    fanning out does not consume the generation budget that the original defect
    charged it for.
    """
    ctx = fan_out_context
    await enroll(ctx)
    grant = ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant")
    assert grant.authority.kind == "github_event"
    before = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")

    invocations = []
    for issue in STORIES:
        response = await send(ctx, story_body(issue))
        assert response.status_code == 202, response.text
        invocation = response.json()["invocation_id"]
        invocations.append(invocation)
        row = ctx.store._read("TENANT#tenant", f"EXEC#{invocation}")
        assert row["issue_number"] == {"N": str(issue)}
        assert row["chain_depth"] == {"N": "1"}
        assert row["parent_principal"] == {"S": f"{ctx.child.invocation}#1"}
        release(ctx, invocation)

    assert len(set(invocations)) == 3
    assert len(messages(ctx)) == 3
    # Fan-out widened which issue, not how many: the stored ceilings are byte-for
    # byte what the human launch wrote, and the coordinator stays at generation 0.
    after = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")
    assert after == before
    assert ctx.store._read("TENANT#tenant", f"EXEC#{ctx.child.invocation}").get("chain_depth", {"N": "0"}) == {"N": "0"}


async def test_fan_out_is_still_bounded_by_budget_and_concurrency(fan_out_context):
    """The pin is lifted; the ceilings are not. A human launch buys four dispatches.

    Both ceilings are exercised because fan-out is the first path that can reach
    them with *distinct* targets: the concurrency ceiling while children are still
    in flight (429, retry later), and the total budget once four have been spent
    (409, this launch is done). The status classes are the pre-existing ones.
    """
    ctx = fan_out_context
    await enroll(ctx)
    in_flight = []
    for issue in (5333, 5337):
        response = await send(ctx, story_body(issue))
        assert response.status_code == 202, response.text
        in_flight.append(response.json()["invocation_id"])
    # Two children in flight against max_dispatch_concurrency = 2.
    assert (await send(ctx, story_body(5338))).status_code == 429
    for invocation in in_flight:
        release(ctx, invocation)

    for issue in (5338, 5340):
        response = await send(ctx, story_body(issue))
        assert response.status_code == 202, response.text
        release(ctx, response.json()["invocation_id"])
    # Four dispatches spent; the fifth distinct story is refused, not queued.
    assert (await send(ctx, story_body(5341))).status_code == 409
    assert len(messages(ctx)) == 4


async def test_fan_out_does_not_reach_another_repository(fan_out_context):
    """The marker names one repository; the caller cannot name a different one."""
    ctx = fan_out_context
    await enroll(ctx)
    assert (await send(ctx, {**story_body(5333), "target": {"repo": "another/repo", "issue": 5333}})).status_code == 404
    assert messages(ctx) == []


async def test_child_inherits_neither_capability_nor_repository_scope(fan_out_context):
    """Issue #5365 CD-8: fan-out stops at the coordinator.

    Two independent guarantees, because either one alone could be undone by a
    later change to the other: the child's grant is written without the marker,
    *and* a child holding the marker is still refused because it is not a root.
    """
    ctx = fan_out_context
    await enroll(ctx)
    accepted = await send(ctx, story_body(5333))
    assert accepted.status_code == 202, accepted.text
    envelope = json.loads(messages(ctx)[0]["Body"])

    child_grant = ctx.store._read("TENANT#tenant", f"GRANT#{envelope['message_id']}#1")
    assert FAN_OUT_CAPABILITY_FIELD not in child_grant
    assert FAN_OUT_REPOSITORY_FIELD not in child_grant
    assert child_grant["work_item_issue"] == {"N": "5333"}

    child_headers = {"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: "child-pod-proof"}
    await enroll(
        ctx,
        headers=child_headers,
        bootstrap={"invocation_id": envelope["message_id"], "envelope_digest": envelope_digest(envelope)},
    )
    # Forge the marker directly onto the child's stored grant — the strongest
    # form of the inheritance question. A non-root caller gets nothing from it.
    ctx.store.client.update_item(
        TableName=ctx.store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{envelope['message_id']}#1"}},
        UpdateExpression="SET #c = :capability, #r = :repo",
        ExpressionAttributeNames={"#c": FAN_OUT_CAPABILITY_FIELD, "#r": FAN_OUT_REPOSITORY_FIELD},
        ExpressionAttributeValues={":capability": {"S": FAN_OUT_CAPABILITY}, ":repo": {"S": "org/repo"}},
    )
    refused = await send(ctx, story_body(5337, persona="reviewer"), headers=child_headers)
    assert refused.status_code == 404
    assert messages(ctx) == []
    # Its own story is still reviewable, so this refusal is about scope alone.
    assert (await send(ctx, story_body(5333, persona="reviewer"), headers=child_headers)).status_code == 202


async def test_gate_decision_root_cannot_bypass_the_graph_with_this_scope(store, child_dispatch, session, session_factory, monkeypatch):
    """Issue #5365 CD-9: an orchestration-rooted coordinator keeps the graph path.

    Its launch was a verified human webhook, so the marker *is* on its grant. But
    enrolment upgraded the authority to ``gate_decision``, which routes dispatch
    through ``dispatch_graph`` — and the escape requires ``github_event``. The
    approval gate therefore still governs which work starts, with no marker-shaped
    hole around it.
    """
    await _make_org(session, org_id="tenant", installations=[123])
    flow = await _make_flow(session, org_id="tenant")
    flow.intent_ref = "42"
    await _make_approval(session, flow, actor_id="human")
    await _make_node(session, flow, issue_ref="43")
    await session.commit()
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    from src.agentauth.run_credential import CREDENTIAL_KEY_ENV

    pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    env = {CREDENTIAL_KEY_ENV: "gateway-test-key-not-shared-with-workers", "BG_ORCH_DISPATCH_REPO": "org/repo"}
    runtime = AgentRuntime(store=store, workloads=SimpleNamespace(verify=lambda token: pod), env=env, dispatcher=child_dispatch.service)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    original = store._read("TENANT#tenant", f"EXEC#{child_dispatch.invocation}")
    headers = {"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: "root-pod-proof"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        response = await client.post(
            "/internal/v1/agent/bootstrap",
            json={"invocation_id": child_dispatch.invocation, "envelope_digest": original["envelope_digest"]["S"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        headers["X-Adp-Run-Credential"] = response.json()["credential"]
        raw = store._read("TENANT#tenant", f"GRANT#{child_dispatch.invocation}#1")
        assert raw[FAN_OUT_CAPABILITY_FIELD] == {"S": FAN_OUT_CAPABILITY}
        assert raw["authority_kind"] == {"S": "gate_decision"}

        # Not a node of the approved flow, so the graph refuses it; the marker
        # buys nothing because this authority never reaches the fan-out path.
        refused = await client.post("/internal/v1/agent/dispatch", json=story_body(5333), headers=headers)
        assert refused.status_code == 404
        assert child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue).get("Messages") is None
        # The graph's own node still dispatches, so the path was not broken.
        approved = await client.post("/internal/v1/agent/dispatch", json=story_body(43), headers=headers)
        assert approved.status_code == 202, approved.text
        envelope = json.loads(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"][0]["Body"])
        assert envelope["orchestration"]["flow_id"] == flow.id


async def test_fan_out_refuses_a_graph_owned_target(fan_out_context, session_factory):
    """Graph-sequenced work is dispatched by the graph, not out of band.

    Starting a second agent on a node's issue is the double dispatch the graph
    exists to prevent, so the target being any flow's node is a refusal even
    though this coordinator is otherwise cleared for its repository.
    """
    ctx = fan_out_context
    await enroll(ctx)
    async with session_factory() as db:
        flow = await _make_flow(db, org_id="tenant", slug="owns-5337")
        flow.intent_ref = "900"
        await _make_node(db, flow, issue_ref="5337")
        await db.commit()
    assert (await send(ctx, story_body(5337))).status_code == 404
    assert messages(ctx) == []
    # An issue the graph does not own is unaffected.
    assert (await send(ctx, story_body(5333))).status_code == 202


async def test_graph_node_ownership_is_scoped_to_the_engines_repository(fan_out_context, session_factory):
    """A node's issue *number* does not make the same number elsewhere graph-owned.

    Flow rows carry bare issue numbers because the engine governs one configured
    repository, so the node lookup must be scoped to that repository. Otherwise
    `org/repo#5337` being a node would block `another/repo#5337` purely on a
    numeric collision.

    Asserted against the resolver directly because the dispatch gate refuses
    cross-repository targets on its own signed-scope terms
    (``test_fan_out_does_not_reach_another_repository``), which would mask which
    check produced the refusal. The launch issue here is deliberately NOT this
    flow's intent, so this isolates node ownership from the launch rule below.
    """
    ctx = fan_out_context
    async with session_factory() as db:
        flow = await _make_flow(db, org_id="tenant", slug="same-number-other-repo")
        flow.intent_ref = "900"
        await _make_node(db, flow, issue_ref="5337")
        await db.commit()
        execution = ctx.store._read("TENANT#tenant", f"EXEC#{ctx.child.invocation}")
        grant = ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant")
        common = {"session": db, "execution": execution, "grant": grant, "target_issue": 5337, "orchestration_repo": "org/repo"}
        # In the engine's own repository the node owns the issue: refused.
        assert not await resolve_repository_fan_out(target_repo="org/repo", **common)
        # The same number in another repository is not this engine's node.
        assert await resolve_repository_fan_out(target_repo="another/repo", **common)


async def test_flow_launch_never_uses_generic_fan_out(fan_out_context, session_factory):
    """A flow launch reaches repository work only through graph dispatch.

    ``resolve_coordinator_assignment`` refuses to widen a coordinator whose flow
    has no human approval ("No approval means no widening beyond the launch
    issue"). Approval does not rewrite a credential that was already issued, so
    that stale ``github_event`` credential must remain pinned too. Re-enrolment
    binds the coordinator to ``gate_decision``; only then may a graph-owned node
    dispatch through ``dispatch_graph``.
    """
    ctx = fan_out_context
    async with session_factory() as db:
        flow = await _make_flow(db, org_id="tenant", slug="awaiting-approval")
        flow.intent_ref = "42"
        await _make_node(db, flow, issue_ref="5337")
        await db.commit()
        flow_id = flow.id
    await enroll(ctx)
    assert ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant").authority.kind == "github_event"
    assert (await send(ctx, story_body(5333))).status_code == 404
    assert messages(ctx) == []

    async with session_factory() as db:
        approved = await db.get(OrchestrationFlow, flow_id)
        await _make_approval(db, approved, actor_id="approver-5365")
        await db.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == flow_id).values(state="running"))
        await db.commit()

    # Approval alone cannot turn the stale launch credential into a generic
    # repository capability. It remains pinned until the coordinator re-enrols.
    assert (await send(ctx, story_body(5333))).status_code == 404
    assert ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant").authority.kind == "github_event"

    await enroll(ctx)
    assert ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant").authority.kind == "gate_decision"
    approved_dispatch = await send(ctx, story_body(5337))
    assert approved_dispatch.status_code == 202, approved_dispatch.text
    envelope = json.loads(messages(ctx)[0]["Body"])
    assert envelope["orchestration"]["flow_id"] == flow_id
    # Even after re-enrolment, a target outside the accepted graph is refused.
    assert (await send(ctx, story_body(5333))).status_code == 404


async def test_fan_out_clearance_fails_closed_when_orchestration_is_unavailable(fan_out_context, monkeypatch):
    """An unresolvable clearance must pin the caller, not widen it.

    If the orchestration database cannot answer whether a target is graph-owned,
    the safe answer is the pre-existing one: the coordinator keeps its launch
    issue. Widening on an unproven fact is the failure mode worth a test.
    """
    ctx = fan_out_context
    await enroll(ctx)
    monkeypatch.setattr(
        "src.agentauth.coordinator.resolve_repository_fan_out", AsyncMock(side_effect=RuntimeError("orchestration database unavailable"))
    )
    assert (await send(ctx, story_body(5333))).status_code == 404
    assert messages(ctx) == []
    assert (await send(ctx, story_body(42))).status_code == 202


async def test_a_coordinator_without_the_marker_stays_pinned(fan_out_context):
    """Persona alone confers nothing; the server-written marker is the authority."""
    ctx = fan_out_context
    ctx.store.client.update_item(
        TableName=ctx.store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{ctx.child.invocation}#1"}},
        UpdateExpression="REMOVE #c",
        ExpressionAttributeNames={"#c": FAN_OUT_CAPABILITY_FIELD},
    )
    await enroll(ctx)
    assert ctx.store._read("TENANT#tenant", f"EXEC#{ctx.child.invocation}")["persona"] == {"S": "operations"}
    assert (await send(ctx, story_body(5333))).status_code == 404
    assert messages(ctx) == []
