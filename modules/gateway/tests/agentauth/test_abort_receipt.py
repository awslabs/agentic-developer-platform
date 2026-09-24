"""The gateway's signed proof that a live run accepted an abort — Issue #3963 (S4).

Finding 1 of root's review is the reason this file exists. The worker used to tell
the finalizer ``delivery: 'accepted'`` in a plain sentinel field, and a pod can
write that field whether or not any abort ever happened. The command envelope does
not close the gap either: it is signed *before* delivery and stays valid for its
whole TTL regardless of whether the run took the command. So "an operator asked at
some point" and "this run accepted and is stopping" were indistinguishable, and the
second one is what justifies deleting a queue message and reporting a deliberate
stop.

The receipt minted in ``_accept_abort`` is the distinction. It exists only as the
result of a durable conditional write against the run's own authority row, on a
request authenticated as the target pod, checked against the current generation and
a live grant. The tests below are organized around the two claims that makes:

- **it cannot be forged** — a genuine, unexpired, correctly signed *command*
  envelope does not satisfy the receipt's audience or action, so replaying real
  issuance bytes buys an attacker nothing (``TestReceiptCannotBeForgedFromIssuance``);
- **it cannot exist without the durable fact** — if the marker write fails, the
  request is refused and no receipt is produced, so the worker never learns it may
  report an accepted abort (``TestOrderingIntentBeforeReceipt``).

These run through the real HTTP route against moto, not against a stubbed store, so
"the second acceptance is refused" is demonstrated by the conditional write itself.

They also run against the **shipped** ``SUPPORTED_AGENT_ACTIONS``: ABORT is in the
real constant as of #3963, and the ``abort_supported`` fixture asserts that rather
than patching it in. That distinction is the difference between proving the route
works and proving it would work if the deployment offered the verb.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import select

from src.agentauth import policy as policy_module
from src.agentauth.envelope import (
    ABORT_RECEIPT_ACTION,
    ABORT_RECEIPT_AUDIENCE,
    ENVELOPE_AUDIENCE,
    SIGNING_KEY_ID_ENV,
    EnvelopeError,
    _signing_key,
    sign_envelope,
    verify_envelope,
)
from src.agentauth.grants import AgentAction
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from tests.agentauth.test_revalidation import (  # noqa: F401
    child_dispatch,
    engine,
    graph_context,
    session,
    session_factory,
    store,
    wave_context,
)
from tests.agentauth.test_revalidation import (
    queued_context as queued_context_fixture,
)

# Re-exported under its own name so the fixtures below can take it as an
# argument without shadowing the import (the pattern in test_human_control.py).
queued_context = queued_context_fixture

COMMAND = "queued-abort"
BODY = json.dumps({"command_id": COMMAND, "reason": "wrong issue"}).encode()


@pytest.fixture
def abort_supported():
    """Assert — do not arrange — that this deployment performs ABORT.

    This fixture used to ``monkeypatch`` ``SUPPORTED_AGENT_ACTIONS`` to add ABORT,
    which was honest while the verb was unimplemented but made every test in this
    file prove something about a set no deployment had. ``require_supported`` runs
    *before* the receipt is minted, so a patched set meant the whole receipt path
    was exercised only under a configuration that did not ship: remove ABORT from
    the real constant and each of these tests would keep passing while the live
    route answered 501.

    #3963 enables the verb for real, so the fixture inverts: it reads the shipped
    constant and fails loudly if ABORT is absent. Every test below now runs against
    the deployed policy, and ``require_supported`` is executing the real check on
    the real value.
    """
    assert AgentAction.ABORT in policy_module.SUPPORTED_AGENT_ACTIONS, (
        "SUPPORTED_AGENT_ACTIONS no longer contains ABORT, so revalidate_command would refuse every "
        "abort with 501 before minting a receipt. These tests describe the shipped deployment and must "
        "not be made to pass by patching the set back in."
    )


@pytest.fixture
async def abort_context(queued_context, abort_supported):
    """A queued abort from a delegated initiator, ready to revalidate."""
    ctx = queued_context
    grant = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")
    grant["allowed_actions"]["SS"].append("abort")
    ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
    ctx.control_grant = ctx.store.live_grant(invocation_id=ctx.child.invocation, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    return ctx


@pytest.fixture
async def human_abort_context(abort_context):
    """The same queued abort, from a live human session instead of a grant."""
    ctx = abort_context
    async with ctx.session_factory() as db:
        user = await db.get(User, "human")
        assert user is not None
        user.is_shadow = False
        membership = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "human", TenantMembership.tenant_id == "tenant"))
        if membership is None:
            db.add(TenantMembership(user_id="human", tenant_id="tenant", is_active=True))
        else:
            membership.is_active = True
        await db.commit()
    return ctx


def abort_request(ctx, *, action="abort", command_id=COMMAND, body=None, human=False, **changes):
    # The route requires the body's own `command_id` to match the request's, so
    # the default body follows `command_id` rather than being a fixed constant.
    if body is None:
        body = BODY if command_id == COMMAND else json.dumps({"command_id": command_id}).encode()
    grant = ctx.control_grant
    params = dict(
        tenant_id="tenant",
        target_run_id=ctx.target_invocation,
        target_generation=1,
        action=action,
        command_id=command_id,
        request_body=body,
        env=ctx.runtime.env,
    )
    if human:
        params.update(principal="human", authority_kind="human_session")
    else:
        params.update(
            principal=f"{ctx.child.invocation}#1",
            grant_id=grant.grant_id,
            revocation_epoch=grant.revocation_epoch,
            flow_id=grant.flow_id,
            authority_reference_id=grant.authority.reference_id,
        )
    params.update(changes)
    return {
        "action": action,
        "command_id": command_id,
        "body_base64": base64.b64encode(body).decode(),
        "envelope": sign_envelope(**params),
    }


async def revalidate(ctx, body):
    return await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)


def read_marker(ctx):
    return ctx.store.authority.abort_intent(invocation_id=ctx.target_invocation, tenant_id="tenant")


def verify_receipt(ctx, receipt, *, audience=ABORT_RECEIPT_AUDIENCE, action=ABORT_RECEIPT_ACTION, body=BODY, command_id=COMMAND, generation=1):
    """Verify exactly the way the worker's finalizer will, with its own facts."""
    return verify_envelope(
        receipt,
        public_keys={ctx.runtime.env[SIGNING_KEY_ID_ENV]: _signing_key(ctx.runtime.env).public_key()},
        expected_run_id=ctx.target_invocation,
        expected_generation=generation,
        expected_action=action,
        expected_command_id=command_id,
        request_body=body,
        expected_audience=audience,
    )


class TestAcceptedAbortProducesBoundEvidence:
    @pytest.mark.parametrize("human", [False, True])
    async def test_receipt_is_bound_to_run_generation_command_and_body(self, abort_context, human_abort_context, human):
        """The four bindings that make the receipt about *this* stop and no other.

        Root's requirement is evidence "bound to command/run/generation/body
        digest". ``verify_receipt`` passes the verifier's own independently known
        values for each, so these assertions fail if any binding is dropped —
        there is nothing tautological left to satisfy.
        """
        ctx = human_abort_context if human else abort_context
        response = await revalidate(ctx, abort_request(ctx, human=human))
        assert response.status_code == 200, response.text
        payload = response.json()

        proof = verify_receipt(ctx, payload["abort_receipt"])
        assert proof.tenant_id == "tenant"
        assert payload["command_id"] == COMMAND
        assert payload["generation"] == 1
        # Same moment in the signed marker and in the plain response field, so a
        # finalizer reading either cannot report a time the store disagrees with.
        assert payload["abort_requested_at"] == read_marker(ctx)["requested_at"]

    async def test_the_durable_marker_lands_before_the_receipt_is_usable(self, abort_context):
        """Accepting the abort is what writes the fact the redelivery guard reads."""
        ctx = abort_context
        assert read_marker(ctx) is None
        response = await revalidate(ctx, abort_request(ctx))
        assert response.status_code == 200, response.text

        marker = read_marker(ctx)
        assert marker["command_id"] == COMMAND
        assert marker["attempt"] == "1"
        # The digest binds the marker to the operator's exact request bytes, which
        # is what lets the finalizer read the reason out of a signed preimage
        # instead of trusting a field sitting next to the signature.
        assert marker["body_digest"] == verify_receipt(ctx, response.json()["abort_receipt"]).body_digest

    async def test_a_non_abort_command_gets_no_receipt(self, abort_context):
        """Only an abort mints one. A pause is not a terminal outcome.

        Minting receipts for every verb would hand the worker a signed artifact it
        has no rule for, and the finalizer's job is to distinguish "stopped on
        purpose" from every other way a run can end.
        """
        ctx = abort_context
        response = await revalidate(ctx, abort_request(ctx, action="pause", command_id="queued-pause"))
        assert response.status_code == 200, response.text
        assert response.json() == {"allowed": True, "command_id": "queued-pause", "generation": 1, "max_round_trip_ms": 1000}
        assert read_marker(ctx) is None

    async def test_retrying_the_same_acceptance_re_attests_the_first_moment(self, abort_context):
        """A retried revalidation reports when the abort was *first* accepted.

        The stored moment is rewritten to an unmistakably earlier one before the
        retry, because comparing two live calls proves nothing here: they would
        agree within the same second even if the gateway were inventing a fresh
        timestamp each time. Planting a distinguishable value is what makes this
        fail if the reported moment stops coming from the store — and the reason
        that matters is that a retried delivery of one command must not read as two
        aborts at two different times.
        """
        ctx = abort_context
        first = await revalidate(ctx, abort_request(ctx))
        assert first.status_code == 200, first.text

        ctx.store.client.update_item(
            TableName=ctx.store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"EXEC#{ctx.target_invocation}"}},
            UpdateExpression="SET abort_requested_at = :then",
            ExpressionAttributeValues={":then": {"S": "2020-01-01T00:00:00Z"}},
        )
        second = await revalidate(ctx, abort_request(ctx))
        assert second.status_code == 200, second.text
        assert second.json()["abort_requested_at"] == "2020-01-01T00:00:00Z"
        assert verify_receipt(ctx, second.json()["abort_receipt"]).command_id == COMMAND

    async def test_a_second_different_abort_is_refused_not_re_signed(self, abort_context):
        """The first accepted abort is the one that stopped the run.

        Refused by the conditional write in the store, not by a read-then-write in
        the route, which is why this test runs against real DynamoDB semantics.
        """
        ctx = abort_context
        assert (await revalidate(ctx, abort_request(ctx))).status_code == 200

        response = await revalidate(ctx, abort_request(ctx, command_id="queued-abort-2"))
        assert response.status_code == 404, response.text
        assert read_marker(ctx)["command_id"] == COMMAND


class TestReceiptCannotBeForgedFromIssuance:
    """Root: "A forged delivery literal with valid issuance bytes but no actual
    live handoff must refuse in a real-signature regression."

    Every envelope in this class is genuinely signed by the real gateway key and
    is unexpired. What an attacker cannot manufacture is the gateway's *statement
    that delivery happened*, and these tests pin the separation that makes it so.
    """

    async def test_a_real_command_envelope_is_not_a_receipt(self, abort_context):
        """The attack that finding 1 describes, executed with authentic bytes.

        The command envelope here is exactly what a legitimate operator's abort
        request contains. It verifies perfectly as a command. Presented as proof
        that the run accepted the abort, it must fail — and it fails on audience
        and action, not on signature, because the signature is real.
        """
        ctx = abort_context
        issuance = abort_request(ctx)["envelope"]

        # It really is valid issuance: the listener's own check accepts it.
        assert verify_receipt(ctx, issuance, audience=ENVELOPE_AUDIENCE, action="abort").command_id == COMMAND

        with pytest.raises(EnvelopeError):
            verify_receipt(ctx, issuance)
        # And nothing was recorded, because issuance is not acceptance.
        assert read_marker(ctx) is None

    async def test_a_receipt_cannot_be_replayed_as_a_command(self, abort_context):
        """The separation holds in both directions.

        If the receipt shared the listener's audience, a captured receipt could be
        submitted as a fresh abort command against the run.
        """
        ctx = abort_context
        response = await revalidate(ctx, abort_request(ctx))
        receipt = response.json()["abort_receipt"]

        with pytest.raises(EnvelopeError):
            verify_receipt(ctx, receipt, audience=ENVELOPE_AUDIENCE, action="abort")

    @pytest.mark.parametrize("collapse", ["audience", "action"])
    async def test_each_half_of_the_separation_stands_on_its_own(self, abort_context, collapse):
        """Audience and action must *each* keep the two artifacts apart.

        Checking only that a command envelope fails as a receipt is not enough: it
        would keep passing if one of the two constants were quietly collapsed onto
        the listener's, because the other would still be carrying the refusal. So
        each is verified with everything else correct, which is the only way the
        test fails when that specific separation is lost.
        """
        ctx = abort_context
        response = await revalidate(ctx, abort_request(ctx))
        receipt = response.json()["abort_receipt"]
        collapsed = {"audience": ENVELOPE_AUDIENCE} if collapse == "audience" else {"action": "abort"}

        with pytest.raises(EnvelopeError):
            verify_receipt(ctx, receipt, **collapsed)

    async def test_a_receipt_for_one_generation_does_not_prove_another(self, abort_context):
        """A redelivered attempt is a different execution, not the aborted one."""
        ctx = abort_context
        response = await revalidate(ctx, abort_request(ctx))
        receipt = response.json()["abort_receipt"]

        with pytest.raises(EnvelopeError):
            verify_receipt(ctx, receipt, generation=2)

    async def test_a_receipt_does_not_carry_delegated_claims(self, abort_context):
        """The receipt speaks for the gateway, not for whoever asked.

        It is minted as ``human_session`` authority deliberately: a receipt
        carrying a grant id and revocation epoch would invite a verifier to treat
        the initiator's delegated authority as still live at finalization time,
        which the gateway is not asserting here. It asserts one thing — this run
        accepted this abort.
        """
        ctx = abort_context
        response = await revalidate(ctx, abort_request(ctx))
        proof = verify_receipt(ctx, response.json()["abort_receipt"])
        assert proof.authority_kind == "human_session"
        assert proof.grant_id is None
        assert proof.principal == f"{ctx.target_invocation}#1"


class TestOrderingIntentBeforeReceipt:
    """Root: "If acceptance cannot be made durable, do not report an
    accepted/applied abort."

    Each test here breaks the durable write and asserts the *absence* of a
    receipt. That order is the whole property: a receipt minted first, or minted
    despite a failed write, is a signed statement that an abort is enforceable
    when nothing in the system will enforce it.
    """

    async def test_a_failed_marker_write_yields_no_receipt(self, abort_context, monkeypatch):
        """Store unreachable at acceptance: the command is not approved.

        A 503 rather than a refusal, because an unreachable store is not an answer
        about this command. The operator can reissue; a worker told "allowed" here
        would proceed to report a stop nothing recorded.
        """
        ctx = abort_context

        def unreachable(**kwargs):
            raise EndpointConnectionError(endpoint_url="https://authority.test")

        monkeypatch.setattr(ctx.store.client, "update_item", unreachable)
        response = await revalidate(ctx, abort_request(ctx))
        assert response.status_code == 503, response.text
        assert "abort_receipt" not in response.text

    async def test_an_acceptance_for_a_superseded_attempt_is_refused(self, abort_context):
        """A stale acceptance must not be signed for an attempt it never reached.

        Recorded intent is attempt-scoped, so an abort accepted against attempt 1
        cannot silently become a receipt covering attempt 2's work.
        """
        ctx = abort_context
        ctx.store.client.update_item(
            TableName=ctx.store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"EXEC#{ctx.target_invocation}"}},
            UpdateExpression="SET current_attempt = :two",
            ExpressionAttributeValues={":two": {"N": "2"}},
        )
        response = await revalidate(ctx, abort_request(ctx))
        assert response.status_code == 404, response.text
        assert read_marker(ctx) is None

    async def test_recording_intent_leaves_the_abort_its_own_channel(self, abort_context):
        """The failure mode that rules out implementing intent as a cancellation.

        Only an ACTIVE execution authorizes a run credential. If accepting the
        abort cancelled the row, the accepted command could never reach the still
        running task: the worker would lose the channel it needs to apply the
        cancellation and write the terminal outcome, leaving an operator holding an
        accepted abort with no effect and no report. So after acceptance the run is
        still able to revalidate — which is exactly what finalization requires.
        """
        ctx = abort_context
        assert (await revalidate(ctx, abort_request(ctx))).status_code == 200

        row = ctx.store._read("TENANT#tenant", f"EXEC#{ctx.target_invocation}")
        assert row["status"]["S"] == "active"
        assert row["current_attempt"]["N"] == "1"
        # Not just the stored state: the live path still works end to end.
        still_live = await revalidate(ctx, abort_request(ctx, action="pause", command_id="queued-pause"))
        assert still_live.status_code == 200, still_live.text

    async def test_an_expired_session_is_refused_before_anything_is_recorded(self, human_abort_context):
        """Freshness is checked ahead of the write, so a dead session leaves no mark.

        Recording first would let an expired proof plant a marker that permanently
        blocks the run, turning a refused abort into an unrecoverable one.
        """
        ctx = human_abort_context
        stale = abort_request(ctx, human=True, now=datetime.now(UTC) - timedelta(seconds=31))
        response = await revalidate(ctx, stale)
        assert response.status_code == 404, response.text
        assert read_marker(ctx) is None
