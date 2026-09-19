"""Accepted coordinator authority enforced through the real dispatch route (#5224).

Everything here goes through `POST /internal/v1/agent/dispatch` against a coordinator
holding a real bootstrapped run credential, on a wave the engine actually materialized.
That is deliberate and it is the point of the file: the coordination checks live in
`graph_dispatch._authorize_dispatch_policy`, which no exported helper reaches. A suite
that called `authorize_child_request` directly would pass while the route refused
every request, or — much worse — while the route admitted one the policy denies.

The invariant these tests exist to defend is a *negative* one. A permit from the
coordination check means only "this coordinator was allowed to ask". So the assertions
come in pairs: the coordinator's request is admitted, **and** the child still went
through its own admission (its own action, its own claim, its own grant). Where the
two disagree, the child's refusal must win.

`ChildPersona` has no `operations` member, so "a coordinator requesting another
coordinator" is not expressible as a child request at all; the cross-wave and
self-launch refusals that bound that case live in `test_wave_dispatch.py` and are not
duplicated here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from src.agentauth.bootstrap import envelope_digest
from src.orchestration.dispatch import graph_address
from src.orchestration.execution_policy import (
    COORDINATION_SCHEMA_VERSION,
    Action,
    ChildPersona,
    CoordinationScope,
    ExecutionPolicy,
    PolicyLimits,
    stamp_policy,
)
from src.orchestration.models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationNode
from src.shared.models.onboarding import TenantMembership
from tests.agentauth.test_graph_dispatch import (  # noqa: F401
    child_dispatch,
    engine,
    enroll,
    messages,
    session,
    session_factory,
    store,
)
from tests.agentauth.test_graph_dispatch import graph_context as graph_context_fixture
from tests.agentauth.test_wave_dispatch import bind, bootstrap_child, dispatch, start_wave
from tests.agentauth.test_wave_dispatch import wave_context as wave_context_fixture
from tests.orchestration.test_policy_admission import (  # noqa: F401
    healthy_policy_reservations,
    policy_budget_initializers,
)

graph_context = graph_context_fixture
# Re-exported under its own name so the fixture resolves here without shadowing the
# import, the same aliasing `test_wave_dispatch` does for `graph_context`.
wave_context = wave_context_fixture

CHILD_ACTIONS = [Action.DEVELOP, Action.REVIEW, Action.REPAIR]
# `MERGE` is in the default `allowed_actions` and never in a coordination scope, and
# both halves are load-bearing. A developer child's token is only scopable when the
# policy permits merge (`policy_github_permissions` returns no write set otherwise, so
# the child is refused `credential_scope_unavailable` before coordination is reached).
# That makes the default document the interesting adversarial case for the guard in
# `authorize_child_request`: the policy DOES autonomously authorize merging, so the
# `allowed_actions`/`human_gates` re-check would pass a delegated merge, and only the
# non-delegable set stops it.
DEFAULT_ACTIONS = [*CHILD_ACTIONS, Action.MERGE, Action.COORDINATE]


def _policy(*, actions=None, child_actions=None, human_gates=None, coordination="default", limits=None, schema_version=COORDINATION_SCHEMA_VERSION):
    """A v3 policy that accepts a bounded coordinator on the wave fixture's flow.

    The assigned address is a placeholder rewritten by `_accept` once the evaluation
    node exists — the coordinator anchors to that node, and its address is composed
    from rows the fixture creates, so the test must not guess the string.

    `child_actions` defaults to the child-capable actions the policy itself permits
    and does not gate, because acceptance requires that containment: a scope naming an
    action the policy does not autonomously authorize is refused at acceptance, so a
    test narrowing `allowed_actions` has to narrow the scope with it or it is asserting
    against a document an owner could never have accepted.
    """
    actions = list(actions) if actions is not None else list(DEFAULT_ACTIONS)
    gates = list(human_gates or [])
    if child_actions is None:
        child_actions = [action for action in CHILD_ACTIONS if action in actions and action not in gates]
    scope = (
        CoordinationScope(
            assigned_node_addresses=["placeholder/epic-4191/wave-1/eval"],
            allowed_child_personas=[ChildPersona.DEVELOPER, ChildPersona.REVIEWER],
            allowed_child_actions=list(child_actions),
        )
        if coordination == "default"
        else coordination
    )
    return ExecutionPolicy(
        org_id="tenant",
        schema_version=schema_version,
        repository_ids=["org/repo"],
        allowed_actions=actions,
        human_gates=gates,
        coordination=scope,
        expires_at=datetime.now(UTC) + timedelta(hours=3),
        limits=limits or PolicyLimits(max_wall_clock_seconds=7200, max_spend_usd=Decimal("100"), max_attempts_per_node=3, max_concurrent_actions=4),
    )


async def _accept(ctx, policy=None, *, addresses=None, version=1):
    """Accept `policy` on the fixture's flow, anchored to the real evaluation node.

    The assigned node set is written through the persisted `plan_document`, the way
    `test_runtime_policy` does it, so the tests prove the scope is read back from
    acceptance rather than from a Python object the test still holds.
    """
    policy = policy if policy is not None else _policy()
    stamped = stamp_policy(policy, principal_id="human", org_id="tenant")
    document = stamped.model_dump(mode="json")
    async with ctx.session_factory() as db:
        if await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "human")) is None:
            db.add(TenantMembership(user_id="human", tenant_id="tenant", role="org_admin"))
        # Exactly one plan is ever in force. An amendment supersedes its predecessor,
        # which is what `load_in_force_policy` reads, so a test that added a version
        # without superseding would be asserting against a state acceptance cannot
        # produce (and would surface as an ambiguous-row error, not a policy result).
        await db.execute(
            update(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.flow_id == ctx.flow.id,
                OrchestrationAcceptedPlan.org_id == "tenant",
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
            .values(superseded_at=datetime.now(UTC))
        )
        if document.get("coordination"):
            evaluation = await db.get(OrchestrationNode, ctx.evaluation_id)
            document["coordination"] = {
                **document["coordination"],
                "assigned_node_addresses": addresses if addresses is not None else [graph_address(evaluation, flow_slug=ctx.flow.slug)],
            }
        db.add(
            OrchestrationAcceptedPlan(
                org_id="tenant",
                flow_id=ctx.flow.id,
                version=version,
                accepted_by_decision_id=ctx.approval.id,
                plan_hash=f"coordinator-policy-v{version}",
                plan_document={"flow_slug": ctx.flow.slug, "execution_policy": document},
            )
        )
        await db.commit()
    return policy


def drain(ctx):
    """Consume and delete everything queued so far, returning what was there.

    Necessary because launching the coordinator legitimately publishes one message.
    A refusal test asserting `messages(ctx) == []` would fail on the *coordinator's*
    envelope and read as "the child was published" — so the queue is drained after
    launch and the later assertion is genuinely about a new publication.
    """
    queued = messages(ctx)
    for message in queued:
        ctx.child.sqs.delete_message(QueueUrl=ctx.child.queue, ReceiptHandle=message["ReceiptHandle"])
    return queued


async def _launch(ctx):
    """Start the wave, then drain the coordinator's own envelope off the queue."""
    launch, headers = await start_wave(ctx)
    assert len(drain(ctx)) == 1
    return launch, headers


async def _child(ctx, headers, *, persona="developer", issue=43, request_id="child"):
    return await dispatch(ctx, persona=persona, issue=issue, request_id=request_id, headers=headers)


async def _story_state(ctx):
    async with ctx.session_factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        return node.state, node.attempts


class TestAllowedChildren:
    """The lane works end to end under an accepted scope, and stays idempotent."""

    async def test_coordinator_launch_and_developer_then_reviewer_child(self, wave_context, monkeypatch):
        """The whole point of the story: a coordinator can be dispatched and can ask.

        Before this change `graph_dispatch` refused every `coordinates` request
        outright, so a flow that accepted a coordinator still could not run one. Both
        the launch and each child request now pass, and every child leaves its own
        committed receipt and queue message.
        """
        ctx = wave_context
        await _accept(ctx)
        launch, headers = await start_wave(ctx)
        assert launch.status_code == 202, launch.text

        developer = await _child(ctx, headers, request_id="story")
        assert developer.status_code == 202, developer.text
        boot, developer_headers = await bootstrap_child(ctx, developer.json()["invocation_id"])
        assert boot.status_code == 200, boot.text
        assert await _story_state(ctx) == ("running", 1)

        from tests.agentauth.test_review_dispatch import seed_review_context

        await seed_review_context(ctx, monkeypatch)
        review = await dispatch(ctx, persona="reviewer", issue=43, request_id="review", headers=developer_headers)
        assert review.status_code == 202, review.text
        assert (await bootstrap_child(ctx, review.json()["invocation_id"]))[0].status_code == 200

    async def test_duplicate_child_request_replays_one_dispatch(self, wave_context):
        """Idempotent per request identity, so a lost reply cannot double-charge.

        The second call must return the *same* invocation rather than a second one:
        a coordinator retrying a request it never saw the answer to is the common
        case, and admitting it twice would consume two attempts against one shared
        allowance.
        """
        ctx = wave_context
        await _accept(ctx)
        _, headers = await _launch(ctx)
        first = await _child(ctx, headers, request_id="story")
        assert first.status_code == 202, first.text
        replay = await _child(ctx, headers, request_id="story")
        assert replay.status_code == 202, replay.text
        assert replay.json() == first.json()
        assert await _story_state(ctx) == ("running", 1)
        async with ctx.session_factory() as db:
            receipts = (
                await db.execute(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.kind == DecisionKind.AGENT_DISPATCHED.value,
                        OrchestrationDecision.node_id == ctx.node.id,
                    )
                )
            ).scalars()
            assert len(list(receipts)) == 1

    async def test_status_and_cancellation_survive_a_policy_refusal(self, wave_context):
        """Observability and cancellation are not gated on the action gate.

        A coordinator whose child request was refused must still be readable and still
        be cancellable. If a refusal took either path down with it, an operator would
        lose both the surface that explains the refusal and the lever that stops the
        run — exactly when they need them.

        Cancellation here is grant revocation, the primitive the engine and the operator
        surfaces both act through; the agent-facing `/control` verbs are a different
        (unimplemented) plane and a coordinator's grant carries no `pause` authority to
        begin with, so asserting on those would test delegation rather than this.
        """
        ctx = wave_context
        await _accept(ctx, _policy(actions=[Action.REVIEW, Action.MERGE, Action.COORDINATE]))
        launch, headers = await start_wave(ctx)
        invocation = launch.json()["invocation_id"]

        refused = await _child(ctx, headers, request_id="story")
        assert refused.status_code == 409, refused.text
        assert "child_action_not_permitted" in refused.text

        # Readable after the refusal, and reporting the coordinator's own live run.
        status = await ctx.client.get("/internal/v1/agent/status", params={"run": invocation}, headers=headers)
        assert status.status_code == 200, status.text

        grant = ctx.store._read("TENANT#tenant", f"GRANT#{invocation}#1")
        grant["revoked"] = {"BOOL": True}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
        # Cancellation takes effect immediately and is not something the refused
        # request left in an unrevokable state.
        assert (await ctx.client.get("/internal/v1/agent/status", params={"run": invocation}, headers=headers)).status_code == 404
        assert (await _child(ctx, headers, request_id="after-cancel")).status_code == 404


class TestCoordinationScopeBounds:
    """Requests outside the accepted scope are refused with the reason that names why."""

    async def test_coordinator_without_accepted_coordination_cannot_launch(self, wave_context):
        """A v1/v2 policy grants no coordination, and says so as a typed refusal.

        Absence of an accepted scope must not read as permission. It also must not
        read as a crash: the reason is `action_not_permitted`, which tells an owner
        the fix is to accept a coordinator, not to debug the platform.
        """
        ctx = wave_context
        await _accept(ctx, _policy(actions=[*CHILD_ACTIONS, Action.MERGE], coordination=None, schema_version=1))
        assert (await bind(ctx)).status_code == 200
        launch = await dispatch(ctx)
        assert launch.status_code == 409, launch.text
        assert "action_not_permitted" in launch.text

    async def test_coordinator_outside_the_assigned_node_set_cannot_launch(self, wave_context):
        """The scope names exact addresses, so a coordinator elsewhere is refused."""
        ctx = wave_context
        await _accept(ctx, addresses=["other-flow/epic-9999/wave-9/eval"])
        assert (await bind(ctx)).status_code == 200
        launch = await dispatch(ctx)
        assert launch.status_code == 409, launch.text
        assert "coordination_node_not_assigned" in launch.text

    async def test_child_persona_outside_the_accepted_set_is_refused(self, wave_context):
        """A persona the owner never accepted cannot ride the coordinator's authority."""
        ctx = wave_context
        await _accept(
            ctx,
            _policy(
                coordination=CoordinationScope(
                    assigned_node_addresses=["placeholder/epic-4191/wave-1/eval"],
                    allowed_child_personas=[ChildPersona.REVIEWER],
                    allowed_child_actions=list(CHILD_ACTIONS),
                )
            ),
        )
        _, headers = await _launch(ctx)
        refused = await _child(ctx, headers, request_id="story")
        assert refused.status_code == 409, refused.text
        assert "child_persona_not_permitted" in refused.text
        assert await _story_state(ctx) == ("ready", 0)
        assert messages(ctx) == []

    async def test_child_action_outside_the_accepted_set_is_refused(self, wave_context):
        """The scope bounds actions independently of personas.

        A developer child is accepted here but `develop` is not, so the refusal must
        name the action. Bounding only personas would let an accepted persona perform
        an action the owner never delegated.
        """
        ctx = wave_context
        await _accept(
            ctx,
            _policy(
                coordination=CoordinationScope(
                    assigned_node_addresses=["placeholder/epic-4191/wave-1/eval"],
                    allowed_child_personas=[ChildPersona.DEVELOPER, ChildPersona.REVIEWER],
                    allowed_child_actions=[Action.REVIEW],
                )
            ),
        )
        _, headers = await _launch(ctx)
        refused = await _child(ctx, headers, request_id="story")
        assert refused.status_code == 409, refused.text
        assert "child_action_not_permitted" in refused.text
        assert await _story_state(ctx) == ("ready", 0)

    async def test_cross_scope_request_into_another_wave_is_refused(self, wave_context):
        """A coordinator's reach stops at its own wave, before policy is consulted.

        The wave-membership check refuses first (404), which is the correct order:
        this request is not a policy question about a node the coordinator might
        coordinate, it is a request for work outside its assignment entirely.
        """
        ctx = wave_context
        await _accept(ctx)
        _, headers = await _launch(ctx)
        async with ctx.session_factory() as db:
            from tests.orchestration.test_dispatch_pass import _make_node

            other = await _make_node(db, ctx.flow, issue_ref="90", node_ref="other-wave-story")
            other.epic_ref, other.wave_ref = "epic-4191", "wave-2"
            await db.commit()
        refused = await _child(ctx, headers, issue=90, request_id="cross-wave")
        assert refused.status_code == 404, refused.text
        assert messages(ctx) == []


class TestCoordinationGrantsNoForbiddenAuthority:
    """`coordinate` is not gate, merge, deploy or evaluation-success authority."""

    async def test_coordinate_does_not_release_a_human_gated_child_action(self, wave_context):
        """A gate stays a gate. Coordination must not route around it.

        `develop` is gated to a human here while remaining in `allowed_actions`, so
        the only thing standing between the coordinator and the child is the gate.
        The refusal proves coordination authority does not clear one.

        Note the scope cannot name `develop` at all in this document — acceptance
        refuses a scope naming a gated action, which is itself the first half of the
        invariant. The route refusal below is the second half.
        """
        ctx = wave_context
        await _accept(ctx, _policy(actions=DEFAULT_ACTIONS, human_gates=[Action.DEVELOP]))
        _, headers = await _launch(ctx)
        refused = await _child(ctx, headers, request_id="story")
        assert refused.status_code == 409, refused.text
        assert "human_gate_required" in refused.text or "child_action_not_permitted" in refused.text
        assert await _story_state(ctx) == ("ready", 0)
        assert messages(ctx) == []

    async def test_a_coordinator_cannot_conclude_the_evaluation_it_coordinates(self, wave_context):
        """The conflation the story exists to prevent.

        A wave coordinator is assigned *to* its evaluation node. If `coordinate` and
        `evaluate` were read as the same authority at that node, holding the lane
        would mean holding the power to declare the wave a success. The policy here
        does not permit `evaluate` at all, so the launch that succeeds as
        `coordinate` proves the two resolve separately — and the evaluation dispatch
        that follows is refused on its own action.
        """
        ctx = wave_context
        await _accept(ctx, _policy(actions=DEFAULT_ACTIONS))
        _, headers = await _launch(ctx)
        async with ctx.session_factory() as db:
            await db.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(state="passed"))
            await db.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.evaluation_id).values(state="ready"))
            await db.commit()
        refused = await dispatch(ctx, issue=45, request_id="conclude", headers=headers)
        assert refused.status_code == 409, refused.text
        assert "action_not_permitted" in refused.text
        async with ctx.session_factory() as db:
            evaluation = await db.get(OrchestrationNode, ctx.evaluation_id)
            assert (evaluation.state, evaluation.attempts) == ("ready", 0)

    @pytest.mark.parametrize("forbidden", [Action.MERGE, Action.DEPLOY, Action.EVALUATE, Action.COORDINATE])
    def test_non_delegable_actions_cannot_be_accepted_into_a_scope(self, forbidden):
        """Acceptance itself refuses the non-delegable set.

        Asserted at the model rather than the route because an owner must not be able
        to *record* this authority — a document that named it would read as a granted
        control even though admission denies it.
        """
        with pytest.raises(ValueError, match="delegab|coordinate"):
            CoordinationScope(
                assigned_node_addresses=["flow/epic-1/wave-1/eval"],
                allowed_child_personas=[ChildPersona.DEVELOPER],
                allowed_child_actions=[Action.DEVELOP, forbidden],
            )

    def test_operations_is_not_a_requestable_child_persona(self):
        """No coordinator tree: a coordinator cannot request a coordinator."""
        with pytest.raises(ValueError):
            CoordinationScope(
                assigned_node_addresses=["flow/epic-1/wave-1/eval"],
                allowed_child_personas=[ChildPersona.DEVELOPER, "operations"],
                allowed_child_actions=[Action.DEVELOP],
            )


class TestChildIdentitiesArePersistedAndReportable:
    """Pending, refused and committed child identities, on the coordinator's lane.

    Three distinct facts, and an operator asking "what did this coordinator actually
    start?" needs all three separable. A refused request that left no trace is
    indistinguishable from one never made, and a pending one that read as committed
    would have an operator waiting on a child that will never arrive.

    Read through the records the existing dispatch machinery already keeps —
    `publish_state` on the reserved command, `status` on the execution, and the
    `AGENT_DISPATCHED` receipt — rather than through anything this story added. That
    is the point of the checklist item: the coordinator lane must report through the
    same surfaces, not acquire its own scheduler or its own bookkeeping.
    """

    async def test_a_committed_child_is_reported_with_its_receipt_and_identity(self, wave_context):
        """The committed case: identity on the response, on the receipt, on the queue.

        The invocation the coordinator is told about must be the same one the receipt
        records and the same one the child bootstraps with. If those three could
        disagree, a coordinator's readback would follow an id that no worker will ever
        claim.
        """
        ctx = wave_context
        await _accept(ctx)
        _, headers = await _launch(ctx)

        child = await _child(ctx, headers, request_id="story")
        assert child.status_code == 202, child.text
        invocation = child.json()["invocation_id"]

        published = drain(ctx)
        assert len(published) == 1
        assert invocation in published[0]["Body"]

        execution = ctx.store._read("TENANT#tenant", f"EXEC#{invocation}")
        assert execution["status"] == {"S": "pending"}, "not yet bootstrapped, so pending — not absent"

        async with ctx.session_factory() as db:
            receipt = await db.scalar(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.kind == DecisionKind.AGENT_DISPATCHED.value,
                    OrchestrationDecision.node_id == ctx.node.id,
                )
            )
        assert receipt is not None
        assert invocation in receipt.reason

    async def test_a_pending_child_becomes_committed_only_on_bootstrap(self, wave_context):
        """Accepted transport is not a started invocation (issue AC-4).

        `202` means the request was admitted and an envelope published. The child is
        still `pending` until it bootstraps, and reporting it as started before then
        would tell an operator work is underway that nothing has picked up.
        """
        ctx = wave_context
        await _accept(ctx)
        _, headers = await _launch(ctx)

        child = await _child(ctx, headers, request_id="story")
        invocation = child.json()["invocation_id"]
        assert ctx.store._read("TENANT#tenant", f"EXEC#{invocation}")["status"] == {"S": "pending"}

        boot, _ = await bootstrap_child(ctx, invocation)
        assert boot.status_code == 200, boot.text
        started = ctx.store._read("TENANT#tenant", f"EXEC#{invocation}")
        assert started["status"] != {"S": "pending"}
        assert started.get("workload_binding"), "the bound worker is what distinguishes started from published"

    async def test_a_refused_child_is_recorded_as_refused_and_not_as_absent(self, wave_context):
        """The refused case, and the one most easily lost.

        A policy refusal after the reservation was taken must leave the reservation
        fenced `refused` and the execution `cancelled`, so the slot is freed without
        the attempt being refundable — and so an operator can see that a request was
        made and denied. Reporting nothing would be indistinguishable from a
        coordinator that never asked.
        """
        ctx = wave_context
        # A scope permitting only `review` refuses the developer child at admission,
        # after the reservation exists — which is precisely the state this asserts on.
        await _accept(ctx, _policy(actions=[Action.REVIEW, Action.MERGE, Action.COORDINATE]))
        launch, headers = await _launch(ctx)

        refused = await _child(ctx, headers, request_id="story")
        assert refused.status_code == 409, refused.text
        assert "child_action_not_permitted" in refused.text
        assert drain(ctx) == [], "a refused child must never reach the queue"
        assert await _story_state(ctx) == ("ready", 0)

        # The requester is the *coordinator's* run, not the engine that launched it:
        # `dispatch_graph` keys the reservation on the calling credential's principal,
        # so a refused coordinator request is recorded under the coordinator's identity.
        key = envelope_digest({"principal": f"{launch.json()['invocation_id']}#1", "request_id": "story"})
        command = ctx.store._read("TENANT#tenant", f"DISPATCH#{key}")
        assert command is not None, "the refusal is recorded against the request identity, not discarded"
        assert command["publish_state"] == {"S": "refused"}
        assert ctx.store._read("TENANT#tenant", f"EXEC#{command['invocation_id']['S']}")["status"] == {"S": "cancelled"}

        async with ctx.session_factory() as db:
            committed = await db.scalar(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.kind == DecisionKind.AGENT_DISPATCHED.value,
                    OrchestrationDecision.node_id == ctx.node.id,
                )
            )
        assert committed is None, "a refused request must leave no committed receipt"


class TestSharedLimitsAndRevocation:
    """Coordinator requests consume the flow's one allowance and honour revocation."""

    async def test_exhausted_attempt_allowance_stops_further_child_requests(self, wave_context):
        """A coordinator cannot outlive its flow's allowance by fanning out.

        `CoordinationScope` carries no limits of its own precisely so this holds: the
        attempt ceiling is per-policy, so once the node is at it the coordinator's
        request is refused rather than granted a fresh budget.
        """
        ctx = wave_context
        limits = PolicyLimits(max_wall_clock_seconds=7200, max_spend_usd=Decimal("100"), max_attempts_per_node=1, max_concurrent_actions=4)
        await _accept(ctx, _policy(limits=limits))
        _, headers = await _launch(ctx)
        async with ctx.session_factory() as db:
            await db.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(attempts=1, state="ready"))
            await db.commit()
        refused = await _child(ctx, headers, request_id="story")
        assert refused.status_code == 409, refused.text
        assert "attempt_limit_exceeded" in refused.text
        assert messages(ctx) == []

    async def test_membership_revocation_refuses_the_coordinator_request(self, wave_context):
        """Revoking the plan owner's role stops the lane at the next request.

        Coordination is delegated authority, so it cannot outlive the delegation. The
        check runs on every request rather than at launch, which is what makes a
        mid-wave revocation effective.
        """
        ctx = wave_context
        await _accept(ctx)
        _, headers = await _launch(ctx)
        async with ctx.session_factory() as db:
            membership = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "human"))
            membership.role = "member"
            await db.commit()
        refused = await _child(ctx, headers, request_id="story")
        assert refused.status_code == 409, refused.text
        assert "role_revoked" in refused.text
        assert await _story_state(ctx) == ("ready", 0)
        assert messages(ctx) == []

    async def test_stale_policy_version_refuses_the_coordinator_request(self, wave_context):
        """An amendment the coordinator's authority does not bind stops it.

        A newer accepted version must not silently authorize a coordinator issued
        under an earlier human decision, and a narrower one must take effect at the
        next request rather than the next acceptance.
        """
        ctx = wave_context
        await _accept(ctx)
        _, headers = await _launch(ctx)
        await _accept(ctx, _policy(actions=[Action.REVIEW, Action.MERGE, Action.COORDINATE]), version=2)
        refused = await _child(ctx, headers, request_id="story")
        assert refused.status_code == 409, refused.text
        assert await _story_state(ctx) == ("ready", 0)
        assert messages(ctx) == []


@pytest.mark.skipif(
    not __import__("os").environ.get("ADP_GRAPH_TEST_DATABASE_URL"),
    reason="requires PostgreSQL row locks; SQLite serializes these writes and cannot exhibit the race",
)
async def test_concurrent_coordinator_requests_admit_one_child(wave_context):
    """Two overlapping requests for the same child must not both be admitted.

    The row lock in `_lock_assignment` is what makes this safe, and the coordination
    check does not weaken it: distinct request identities mean idempotent replay
    cannot collapse them, so exactly one must win on the node's state.
    """
    ctx = wave_context
    await _accept(ctx)
    _, headers = await _launch(ctx)
    results = await asyncio.gather(
        _child(ctx, headers, request_id="race-a"),
        _child(ctx, headers, request_id="race-b"),
    )
    # The losing request sees an ineligible story, which the dispatch route
    # deliberately reports as 404 (the same contract as ordinary child dispatch).
    assert sorted(response.status_code for response in results) == [202, 404]
    assert await _story_state(ctx) == ("running", 1)
    assert len(messages(ctx)) == 1
