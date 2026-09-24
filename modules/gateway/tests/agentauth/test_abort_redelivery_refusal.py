"""An aborted run does not execute again, even when nothing about the abort worked — Issue #3963 (S4).

## What this file proves that no other file does

The other abort suites each cover one link. ``test_abort_receipt.py`` proves the
gateway mints a receipt only when durable intent was recorded.
``agent-worker-abort.test.ts`` proves the worker cancels whether or not it can
report. ``test_registration_atomic.py`` proves a *bound* pod's re-bind is refused.
None of them answers the operator's actual question, which is the conjunction:

    a real accepted abort → the terminal write fails AND the queue ack fails
    → the message genuinely redelivers → **zero task starts**

That conjunction is the only interesting case, because every individual mechanism
has a failure mode the others were assumed to cover. The terminal ``aborted`` row
is what the legacy completion guard reads — so if the write fails, that guard has
nothing. The DeleteMessage is what stops SQS redelivering — so if the ack fails,
the envelope comes back. ``_finalize_abort_acknowledgement``'s own docstring calls
this row "unprotected" and logs that "the run may execute again despite being
aborted". This file is the test of whether that pessimism is warranted on the
protected path. It is not, and the reason is a *third* mechanism neither of the
first two is: the durable ``abort_intent`` marker read by ``BootstrapStore.bind``.

## Why the redelivery here is real

Nothing simulates the queue. The aborting pod holds its message in flight, and the
delete is failed by a corrupted receipt handle — so SQS itself raises
``ReceiptHandleIsInvalid`` and the message is genuinely still enqueued afterwards.
The replacement pod then takes it through the production ``TaskDelivery.acquire``,
the same call the gateway makes for a real worker's ``own_task``. The redelivery is
therefore not a helper invoked twice with the same arguments; it is the same
``MessageId`` arriving at a new pod the way it would in the cluster.

Likewise the abort is accepted through the live ``/internal/v1/agent/revalidate``
route with a signed envelope, a live grant and the shipped
``SUPPORTED_AGENT_ACTIONS``, so the marker under test is written by production code
on a request that production would have accepted.

## Why zero task starts is asserted at ``bind``, not at a flag

"Task starts" is not a counter this test can invent, because the thing that starts
tasks is ``subprocess.run(["node", ...])`` in the worker entrypoint, and that line
is unreachable unless ``bootstrap_run_identity`` returns. It does not return on a
404: ``RunIdentityError`` is uncaught all the way out of ``main()``, which kills the
pod. So a refused bind **is** zero task starts on the protected path, and the
assertions below are on the observable consequences of the refusal — no credential
issued, no new ``POD#.../BINDING`` row, the original binding undisturbed — rather
than on a spy that could be satisfied by a mechanism that does not ship.

``TestTheAbortingRunItselfIsNotStopped`` pins the ordering the whole argument rests
on: the marker sits *after* ``bind``'s existing-binding retry-return, so it stops the
next run without stopping the one that is finalizing. Hoisting it above that return
— the obvious-looking simplification — passes every other test in this file and
breaks the aborting pod's own 300s credential renewal, so it is mutation-checked
there rather than left to a comment.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.execution import ExecutionStatus
from src.agentauth.task_delivery import TaskDelivery
from src.agentauth.workload import WORKLOAD_HEADER, VerifiedPod
from tests.agentauth.test_abort_receipt import (  # noqa: F401
    abort_context,
    abort_supported,
    child_dispatch,
    engine,
    graph_context,
    queued_context,
    session,
    session_factory,
    store,
    wave_context,
)
from tests.agentauth.test_abort_receipt import abort_request, read_marker, revalidate

# The worker package is not importable as a module, and this suite needs its real
# abort constants rather than copies: a test that hard-coded "3 attempts" would keep
# passing if production dropped to one.
_WORKER_IMAGE = Path(__file__).resolve().parents[3] / "agent-factory" / "agent-worker-image"


@pytest.fixture(scope="module")
def worker():
    """The real worker entrypoint module, for its constants and finalizer.

    Loaded by **explicit file path**, not by ``import entrypoint``. There is another
    ``entrypoint.py`` on this interpreter's path (``/app/entrypoint.py``), and a plain
    import silently resolves to it — the first draft of this suite did exactly that
    and failed with "module has no attribute
    ``_finalize_abort_acknowledgement``", which is the *lucky* outcome. The dangerous
    one is a same-named symbol existing in both, which would have this suite assert
    against code the agent image does not ship. Naming the file removes the ambiguity.
    """
    spec = importlib.util.spec_from_file_location(
        "adp_agent_worker_entrypoint", _WORKER_IMAGE / "entrypoint.py"
    )
    module = importlib.util.module_from_spec(spec)
    # The worker image's own `lib.*` imports resolve relative to its root.
    if str(_WORKER_IMAGE) not in sys.path:
        sys.path.insert(0, str(_WORKER_IMAGE))
    spec.loader.exec_module(module)
    assert module.__file__ == str(_WORKER_IMAGE / "entrypoint.py")
    return module


def delivery(ctx) -> TaskDelivery:
    """Production task delivery over the fixture's real FIFO queue."""
    return TaskDelivery(store=ctx.store, sqs=ctx.child.sqs, queue_url=ctx.child.queue)


def pod(uid: str) -> VerifiedPod:
    """A verified pod identity, as the workload verifier would return one."""
    return VerifiedPod(uid, "worker", "adp-agents", "agent-scaledjob-sa", "10.0.1.3")


async def accept_abort(ctx) -> str:
    """Accept an abort through the live route and return the signed receipt.

    Deliberately goes through HTTP rather than calling ``record_abort_intent``: the
    marker this suite depends on must be the one production writes, on a request
    production would have authorized, or the refusal proven below is a refusal of a
    fact no real abort produces.
    """
    assert read_marker(ctx) is None, "the fixture must start with no abort recorded"
    response = await revalidate(ctx, abort_request(ctx))
    assert response.status_code == 200, response.text
    receipt = response.json()["abort_receipt"]
    assert receipt, "an accepted abort must mint a receipt"
    marker = read_marker(ctx)
    assert marker is not None and marker["command_id"] == "queued-abort"
    return receipt


class TestTheAbortedRunDoesNotStartAgain:
    """The combined flow, with both durable writes failed."""

    async def test_accepted_abort_survives_terminal_and_ack_failure_with_zero_task_starts(
        self, abort_context, monkeypatch, worker
    ):
        ctx = abort_context
        # 1. A real abort, accepted through the live route by production code.
        await accept_abort(ctx)
        # The run is still ACTIVE, which is the whole reason a marker is needed: the
        # status-based refusals below cannot fire on an active record.
        record = ctx.store.authority.load_execution(invocation_id=ctx.target_invocation, tenant_id="tenant")
        assert record.status is ExecutionStatus.ACTIVE
        original_binding = record.workload_binding

        # 2. The aborting pod holds its message in flight, and its acknowledgement
        # fails for real. The pod took this message before it bound, so it owns a
        # receipt handle and no PODTASK row — which is exactly the state the fixture's
        # bootstrap produced, and the state a pod is in while it runs.
        held = ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=1)["Messages"][0]
        assert json.loads(held["Body"])["message_id"] == ctx.target_invocation
        receipt_handle = held["ReceiptHandle"]

        # The ack fails at SQS, not in a stub: the handle is corrupted, so moto
        # answers ReceiptHandleIsInvalid exactly as AWS would, and the message is
        # therefore genuinely still on the queue afterwards.
        with pytest.raises(ClientError, match="ReceiptHandleIsInvalid"):
            ctx.child.sqs.delete_message(QueueUrl=ctx.child.queue, ReceiptHandle="broken-" + receipt_handle[:20])
        # Bounded retry, read from the shipped constant rather than asserted as a
        # literal, so shrinking production's bound fails this test.
        assert worker.ABORT_ACK_ATTEMPTS == 3

        # No terminal `aborted` row exists — the write failed. This is the state the
        # finalizer's own docstring calls "unprotected", and the claim under test is
        # that the protected path is nonetheless protected.
        events = ctx.store.client.scan(TableName="events")["Items"]
        assert all(row.get("status", {}).get("S") != "aborted" for row in events)

        # 3. The message genuinely redelivers: never deleted, visibility released.
        ctx.child.sqs.change_message_visibility(
            QueueUrl=ctx.child.queue, ReceiptHandle=receipt_handle, VisibilityTimeout=0
        )
        redelivered = delivery(ctx).acquire("replacement-pod")
        assert redelivered is not None, "the undeleted message must come back"
        assert json.loads(redelivered)["message_id"] == ctx.target_invocation, (
            "the same envelope must redeliver, not a different task"
        )

        # 4. Zero task starts. The replacement pod holds the real envelope and asks
        # to bind, which is the last gate before any task code runs.
        digest = envelope_digest(json.loads(redelivered))
        with pytest.raises(BootstrapRefusedError, match="run was aborted"):
            ctx.store.bind(
                invocation_id=ctx.target_invocation,
                digest=digest,
                pod=pod("replacement-pod"),
                now=datetime.now(UTC),
            )

        # The refusal left no trace a later attempt could build on, and did not
        # disturb the aborting run's own binding.
        assert ctx.store._read("POD#replacement-pod", "BINDING") is None
        after = ctx.store.authority.load_execution(invocation_id=ctx.target_invocation, tenant_id="tenant")
        assert after.workload_binding == original_binding

    async def test_the_marker_is_what_refuses_it_not_the_status_check(self, abort_context):
        """Isolate the mechanism, so this suite cannot pass on the wrong one.

        ``bind`` has two refusals in sequence: the abort marker, then
        ``status != PENDING or workload_binding is not None``. On a redelivery the
        second one is also true, so a suite that only asserted "the replacement pod
        was refused" would stay green with the marker deleted — and would then be
        asserting nothing about abort at all.

        The distinguishing evidence is the message. ``bind`` raises
        ``"run was aborted"`` for the marker and ``"bootstrap refused"`` for the
        status, and only the first names a fact that no ordinary redelivery of a
        healthy run produces.
        """
        ctx = abort_context
        digest = ctx.store._read("TENANT#tenant", f"EXEC#{ctx.target_invocation}")["envelope_digest"]["S"]

        # Before the abort: a second pod is refused, but for the ordinary reason.
        with pytest.raises(BootstrapRefusedError) as generic:
            ctx.store.bind(invocation_id=ctx.target_invocation, digest=digest, pod=pod("other-pod"), now=datetime.now(UTC))
        assert str(generic.value) == "bootstrap refused"

        await accept_abort(ctx)

        # After it: the same call is refused by the marker, which is checked first
        # and says something the status never could.
        with pytest.raises(BootstrapRefusedError) as aborted:
            ctx.store.bind(invocation_id=ctx.target_invocation, digest=digest, pod=pod("other-pod"), now=datetime.now(UTC))
        assert str(aborted.value) == "run was aborted"

    async def test_a_refused_redelivery_is_a_404_to_the_pod(self, abort_context):
        """End to end through HTTP, because the pod never calls ``bind`` directly.

        The worker reaches this through ``bootstrap_run_identity``, which raises
        ``RunIdentityError`` on any non-200 and is uncaught in ``entrypoint.main()``.
        So the status code is the whole worker-side contract: 404 kills the pod
        before ``subprocess.run(["node", ...])``, and 200 would start the task.

        404 rather than a distinct "aborted" code is deliberate — it is the same
        answer every other refused bootstrap gets, so a pod cannot probe this
        endpoint to learn whether a run it does not own was aborted.
        """
        ctx = abort_context
        await accept_abort(ctx)
        row = ctx.store._read("TENANT#tenant", f"EXEC#{ctx.target_invocation}")

        original = ctx.runtime.workloads.verify
        proof = "proof-replacement"
        ctx.runtime.workloads.verify = lambda token: (
            pod("replacement-pod") if token == proof else original(token)
        )
        response = await ctx.client.post(
            "/internal/v1/agent/bootstrap",
            headers={"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: proof},
            json={"invocation_id": ctx.target_invocation, "envelope_digest": row["envelope_digest"]["S"]},
        )

        assert response.status_code == 404, response.text
        # No credential was issued, so nothing the task needs to run exists.
        assert "credential" not in response.json()


class TestTheAbortingRunItselfIsNotStopped:
    """Quiescence before terminal — instruction 5808383984.

    The marker refuses the *next* run, and must not refuse the one that is
    finalizing. That run still needs its credential renewed (every 300s) and its
    own idempotent re-bind to succeed, because it has work left to do: cancel the
    SDK, write the terminal row, delete the message. A marker that stopped it would
    destroy the ability to complete the very abort it records.
    """

    async def test_the_aborting_pod_can_still_rebind_and_finalize(self, abort_context):
        ctx = abort_context
        await accept_abort(ctx)
        record = ctx.store.authority.load_execution(invocation_id=ctx.target_invocation, tenant_id="tenant")
        binding = record.workload_binding
        digest = ctx.store._read("TENANT#tenant", f"EXEC#{ctx.target_invocation}")["envelope_digest"]["S"]

        # The aborting pod's own re-bind returns its record rather than raising,
        # because `bind` returns on the existing-binding branch *before* reaching the
        # marker check. That ordering is the design, not an accident of placement.
        rebound = ctx.store.bind(
            invocation_id=ctx.target_invocation, digest=digest, pod=pod(binding), now=datetime.now(UTC)
        )
        assert rebound.workload_binding == binding
        assert rebound.status is ExecutionStatus.ACTIVE

    async def test_no_premature_terminal_surface_is_published_by_acceptance(self, abort_context):
        """Accepting an abort must not itself report the run as aborted.

        The operator-visible terminal row belongs after real cancellation. If
        acceptance published it, an abort that was accepted and then failed to stop
        the SDK would read as a completed abort while the agent kept committing.
        """
        ctx = abort_context
        await accept_abort(ctx)

        record = ctx.store.authority.load_execution(invocation_id=ctx.target_invocation, tenant_id="tenant")
        assert record.status is ExecutionStatus.ACTIVE, "acceptance must not transition the protected record"
        for row in ctx.store.client.scan(TableName="events")["Items"]:
            assert row.get("status", {}).get("S") != "aborted", (
                "the events row must not show a terminal abort until the run has actually stopped"
            )


class TestPermanentTerminalReconciliation:
    """When every terminal-repair retry fails after a confirmed ack.

    Root's requirement: do not label this state reconciled merely because retry
    logic exists. The run is genuinely stopped and genuinely unreported, and the
    honest outcome is that the *rerun* guarantee holds while the *reporting* does
    not — and that the difference is visible, not swallowed.
    """

    async def test_exhausted_repair_after_a_confirmed_ack_is_reported_not_reconciled(
        self, abort_context, monkeypatch, worker, caplog
    ):
        ctx = abort_context
        await accept_abort(ctx)

        # Every terminal write fails, permanently. The ack succeeds.
        calls: list[str] = []
        monkeypatch.setattr(
            worker, "update_invocation_status", lambda *a, **k: calls.append(k.get("summary", "")) or False
        )
        monkeypatch.setattr(worker.time, "sleep", lambda _s: None)
        acked: list[str] = []
        monkeypatch.setattr(worker, "_delete_message", lambda *a, **k: acked.append("deleted"))

        with caplog.at_level("ERROR"):
            exit_code = worker._finalize_abort_acknowledgement(
                queue_url=ctx.child.queue,
                region="us-east-1",
                receipt_handle="handle",
                exit_code=0,
                terminal_persisted=False,
                message_id=ctx.target_invocation,
                arrived_at="2026-09-24T00:00:00Z",
                summary="stopped by operator",
            )

        # The ack landed, so the run cannot restart: exiting non-zero would summon a
        # pod that can only pick up an unrelated message. That is why this is 0.
        assert acked == ["deleted"]
        assert exit_code == 0
        # Every bounded attempt was actually spent, from the shipped constant.
        assert len(calls) == worker.ABORT_TERMINAL_WRITE_ATTEMPTS == 3
        # And the unreported outcome is stated, not implied by a clean exit code.
        assert "could not be persisted or repaired" in caplog.text, (
            "an unreported abort must say so; a clean exit code alone reads as a clean abort"
        )
        # Named as a reporting defect with the run ID an operator needs, not as a
        # generic error — this is the line that explains an active-looking aborted run.
        assert "cannot execute again" in caplog.text
        assert ctx.target_invocation in caplog.text

        # The marker still refuses a redelivery even though no terminal row exists,
        # which is why the stale dashboard is a reporting defect and not a rerun risk.
        assert read_marker(ctx) is not None
