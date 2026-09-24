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
aborted".

This file is the test of whether that pessimism is warranted on the protected path.
It is not — and the answer is the interesting part, because it is not a third abort
mechanism. It is the ordinary binding lifecycle: the execution record is ACTIVE and
already bound, so ``bind`` refuses the redelivered envelope for the same reason it
refuses any second pod, and nothing can restore the PENDING status that would admit
one. An abort-specific guard here would never execute; ``test_the_existing_binding_
check_is_what_refuses_it_no_abort_guard`` pins that, and a guard added in an earlier
revision of #3963 was removed on the strength of it.

## Why both failures and the redelivery here are real

Nothing simulates the queue, and nothing stands in for the worker. The aborted run's
finalization is the shipped ``_handle_abort`` → ``_persist_abort_terminal_status`` →
``_finalize_abort_acknowledgement`` chain, driven by
``run_worker_abort_finalization``, and both failures are injected at the transport
underneath it:

* the terminal write calls the shipped ``lib.invocation_status.update_status``
  against real (moto) DynamoDB with the events table pointed at a name that does not
  exist, so boto3 raises and the module's own fail-soft handler returns ``False``;
* the delete calls the shipped ``_delete_message`` against real SQS with a corrupted
  receipt handle, so SQS answers ``ReceiptHandleIsInvalid`` and the message stays
  enqueued.

The replacement pod then takes that message through the production
``TaskDelivery.acquire``, the same call the gateway makes for a real worker's
``own_task``. The redelivery is therefore not a helper invoked twice with the same
arguments; it is the same ``MessageId`` arriving at a new pod the way it would in the
cluster.

``test_the_same_harness_writes_a_real_aborted_row_when_dynamodb_answers`` is the
control that makes the above falsifiable: the identical harness against the table
that *does* exist must produce a genuine terminal ``aborted`` row. Without it,
"the write failed" would be indistinguishable from "the write was never attempted" —
which is exactly the defect an earlier revision of this file had.

Likewise the abort is accepted through the live ``/internal/v1/agent/revalidate``
route with a signed envelope, a live grant and the shipped
``SUPPORTED_AGENT_ACTIONS``, so the marker under test is written by production code
on a request that production would have accepted.

## Worker startup evidence

The combined test feeds the actual redelivered envelope into ``_main`` and
bridges the signed identity client's HTTP transport into the real ASGI bootstrap
route. A real admission refusal propagates out of startup as ``RunIdentityError``.
Spies at ``subprocess.run`` and ``Popen`` verify that no process launches. External
logging and transport credentials are fixtures; this is local integration evidence,
not a deployed-pod acceptance run. The combined failure case is parameterized across legacy direct-DynamoDB and
protected gateway-mediated reporting. The latter includes an authenticated
successful write before storage failure injection.

``TestTheAbortingRunItselfIsNotStopped`` covers the other half, and it is the half an
abort guard would have broken: the aborting pod re-presents its own binding every
300s through the run-identity renewal thread, and it still has work to do — cancel
the SDK, write the terminal row, delete the message. Anything that refuses it there
revokes the credential the abort itself needs, which is why "refuse an aborted run at
startup" is a more delicate instruction than it sounds.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.execution import ExecutionStatus
from src.agentauth.store import ExecutionTransitionConflictError
from src.agentauth.task_delivery import TaskDelivery
from src.agentauth.workload import WORKLOAD_HEADER, VerifiedPod
from tests.agentauth.test_abort_receipt import (
    abort_context as abort_context_fixture,
)
from tests.agentauth.test_abort_receipt import (  # noqa: F401
    abort_request,
    abort_supported,
    child_dispatch,
    engine,
    graph_context,
    queued_context,
    read_marker,
    revalidate,
    session,
    session_factory,
    store,
    wave_context,
)

# Re-exported under its own name so the test methods below can take it as a
# parameter without shadowing the import (the pattern `test_revalidation.py` uses
# for `wave_context`).
abort_context = abort_context_fixture

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
    spec = importlib.util.spec_from_file_location("adp_agent_worker_entrypoint", _WORKER_IMAGE / "entrypoint.py")
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


def arrived_at(ctx) -> str:
    """The aborted run's row sort key, read from the row the ingress actually wrote.

    Scanned rather than passed in, because the worker's status writer addresses its
    row by ``(event_id, arrived_at)`` and a value invented here would make every
    write miss a row that exists — which looks exactly like the failure this suite
    injects deliberately, and would make the injection unfalsifiable.
    """
    rows = ctx.store.client.scan(TableName="events")["Items"]
    matching = [row for row in rows if row["event_id"]["S"] == ctx.target_invocation]
    assert len(matching) == 1, f"expected exactly one row for the aborted run, got {len(matching)}"
    return matching[0]["arrived_at"]["S"]


def run_worker_abort_finalization(worker, ctx, monkeypatch, receipt_handle, *, events_table, protected=False):
    """Drive the worker's REAL abort finalization, and report what it observed.

    This is the part root's review of e9b630dd found missing: the earlier version of
    the combined test asserted that no ``aborted`` row existed after never having
    asked for one, so the absence followed from the test rather than from a failure.
    Here the write is genuinely attempted and genuinely fails.

    Both failures are injected at the **transport**, not by replacing worker
    functions:

    * The terminal write goes through the shipped ``lib.invocation_status.update_status``
      against the fixture's real (moto) DynamoDB, with ``WEBHOOK_EVENTS_TABLE`` pointed
      at a table that does not exist — so boto3 raises ``ResourceNotFoundException``
      and the module's own fail-soft handler returns ``False``, exactly as a real
      DynamoDB outage reaches it. ``_persist_abort_terminal_status`` therefore runs
      its real retry loop.
    * The acknowledgement goes through the shipped ``_delete_message`` to real SQS
      with a corrupted receipt handle, so SQS raises ``ReceiptHandleIsInvalid`` and
      the message stays enqueued.

    Returns ``(exit_code, terminal_persisted, write_attempts)``.
    """
    import lib.invocation_status as invocation_status

    row_arrived_at = arrived_at(ctx)
    # The worker's own module-level client cache and table name, pointed at the
    # fixture's DynamoDB. `_ddb` is reset so the cached client cannot leak between
    # tests, and the table name is the injection point for the failure.
    monkeypatch.setattr(invocation_status, "_ddb", ctx.store.client, raising=False)
    monkeypatch.setattr(invocation_status, "_table_name", events_table, raising=False)
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", events_table)
    # The legacy (direct-DynamoDB) status path, so the write under test is the one
    # whose failure mode this suite injects. The gateway-mediated path is covered by
    # `test_handoff_routes.py`; here the requirement is a *failed* durable write.
    if not protected:
        monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)

    attempts: list[str] = []
    real_update = invocation_status.update_status

    def counted(*args, **kwargs):
        attempts.append(kwargs.get("status") or (args[2] if len(args) > 2 else ""))
        return real_update(*args, **kwargs)

    monkeypatch.setattr(worker, "update_invocation_status", counted)
    # Backoff only; the retry COUNT stays production's. Sleeping 1s+2s per retry loop
    # would add ~6s of pure idling to this test for no assertion.
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)
    # The one stub, and it is not the subject: `_handle_abort` also posts a GitHub
    # comment through the `gh` CLI. That is a separate side effect requiring network
    # and a repo, while everything this test asserts is about the durable row and the
    # queue. Stubbed rather than skipped so the real `_handle_abort` still runs —
    # including its summary text and its decision to post before writing.
    posted: list[tuple] = []
    monkeypatch.setattr(worker, "_post_comment", lambda *a, **k: posted.append(a))

    # The real entry point the supervisor calls, not the private writer beneath it.
    exit_code, terminal_persisted = worker._handle_abort(
        "org/repo",
        42,
        "developer",
        ctx.target_invocation,
        row_arrived_at,
        {"reason": "wrong issue"},
    )
    assert posted, "the operator's comment is still posted by the real code path"
    exit_code = worker._finalize_abort_acknowledgement(
        queue_url=ctx.child.queue,
        region="us-east-1",
        receipt_handle=receipt_handle,
        exit_code=exit_code,
        terminal_persisted=terminal_persisted,
        message_id=ctx.target_invocation,
        arrived_at=row_arrived_at,
        summary="stopped by operator",
    )
    return exit_code, terminal_persisted, attempts


class TestTheAbortedRunDoesNotStartAgain:
    """The combined flow, with both durable writes failed."""

    @pytest.mark.parametrize("protected", [False, True])
    async def test_accepted_abort_survives_terminal_and_ack_failure_with_zero_task_starts(
        self, abort_context, monkeypatch, worker, tmp_path, protected
    ):
        ctx = abort_context
        # 1. A real abort, accepted through the live route by production code.
        await accept_abort(ctx)
        # The run is still ACTIVE, which is the whole reason a marker is needed: the
        # status-based refusals below cannot fire on an active record.
        record = ctx.store.authority.load_execution(invocation_id=ctx.target_invocation, tenant_id="tenant")
        assert record.status is ExecutionStatus.ACTIVE
        original_binding = record.workload_binding

        # 2. The aborting pod holds its message in flight. The pod took this message
        # before it bound, so it owns a receipt handle and no PODTASK row — which is
        # exactly the state the fixture's bootstrap produced, and the state a pod is
        # in while it runs.
        held = ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=1)["Messages"][0]
        assert json.loads(held["Body"])["message_id"] == ctx.target_invocation
        receipt_handle = held["ReceiptHandle"]
        row_before = arrived_at(ctx)

        # 3. The worker's REAL finalization runs, and both durable writes fail for
        # real: the terminal row against a DynamoDB table that does not answer, the
        # delete against SQS with a corrupted handle. Neither failure is simulated by
        # replacing a worker function with one that returns False.
        import asyncio
        from functools import partial

        responses = []
        if protected:
            from contextlib import nullcontext

            import lib.status_gateway_client as status_client
            from botocore.credentials import Credentials

            from src.agentauth import registration_routes
            from src.agentauth.registration import AgentRegistrationService

            # Real policy, run credential, workload binding and transactional
            # writer. Only the destination table fails; authorization stays live.
            service = AgentRegistrationService(
                policy=ctx.runtime.dispatcher.policy, authority_table=ctx.store.table,
                events_table="events-that-does-not-exist", dynamodb_client=ctx.store.client,
                env=ctx.runtime.env,
            )
            registration = registration_routes.RegistrationRuntime(service=service, runtime=ctx.runtime)
            app = ctx.client._transport.app
            app.include_router(registration_routes.router)
            app.dependency_overrides[registration_routes.get_registration_runtime] = lambda: registration
            credential_path = tmp_path / "run-credential"
            credential_path.write_text(ctx.target_headers[status_client.CREDENTIAL_HEADER])
            monkeypatch.setenv("ADP_RUN_CREDENTIAL_FILE", str(credential_path))
            monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
            monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.test/internal/v1/agent")
            monkeypatch.setattr(status_client, "read_workload_token", lambda: ctx.target_headers[WORKLOAD_HEADER])
            monkeypatch.setattr("adp_trigger.transport_identity.worker_credentials",
                                lambda _: Credentials("AKIAEXAMPLE", "secret", "token"))
            loop = asyncio.get_running_loop()

            class ProtectedTransport:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def post(self, url, *, data, headers, **_kwargs):
                    assert url == "https://gateway.test/internal/v1/agent/self/status"
                    assert "Authorization" in headers
                    assert headers[status_client.CREDENTIAL_HEADER] == ctx.target_headers[status_client.CREDENTIAL_HEADER]
                    response = asyncio.run_coroutine_threadsafe(ctx.client.post(
                        "/internal/v1/agent/self/status", content=data,
                        headers={**headers, "X-Caller-Identity": "shared-role"},
                    ), loop).result(timeout=10)
                    responses.append(response)
                    from types import SimpleNamespace

                    return nullcontext(SimpleNamespace(
                        status_code=response.status_code,
                        raw=SimpleNamespace(read=lambda size, **_: response.content[:size]),
                    ))

            monkeypatch.setattr(status_client.requests, "Session", ProtectedTransport)
            # Positive control: the identical authenticated path writes while
            # the configured table exists. The later 503 is a storage failure,
            # not a missing credential, route or policy configuration.
            service._events_table = "events"
            await asyncio.to_thread(status_client.record_status, "in_progress", {})
            assert responses[-1].status_code == 200
            responses.clear()
            service._events_table = "events-that-does-not-exist"

        exit_code, terminal_persisted, write_attempts = await asyncio.to_thread(partial(
            run_worker_abort_finalization, worker, ctx, monkeypatch,
            "broken-" + receipt_handle[:20], events_table="events-that-does-not-exist",
            protected=protected,
        ))
        if protected:
            assert len(responses) == worker.ABORT_TERMINAL_WRITE_ATTEMPTS
            assert all(response.status_code == 503 for response in responses)


        # The write was attempted, on production's own bound, and observed to fail.
        # This is the assertion the previous revision of this test could not make,
        # because it never called the writer at all.
        assert terminal_persisted is False, "the injected DynamoDB failure must be observed as a failure"
        assert len(write_attempts) == worker.ABORT_TERMINAL_WRITE_ATTEMPTS == 3
        assert set(write_attempts) == {"aborted"}, write_attempts
        # Retryable, not 0: neither the row nor the ack landed, so this pod must not
        # report a clean abort. The aborted run's row is still exactly as it was.
        assert exit_code == worker.AGENT_EXIT_RETRYABLE
        events = ctx.store.client.scan(TableName="events")["Items"]
        assert all(row.get("status", {}).get("S") != "aborted" for row in events)
        assert arrived_at(ctx) == row_before

        # 4. The message genuinely redelivers: never deleted, visibility released.
        ctx.child.sqs.change_message_visibility(QueueUrl=ctx.child.queue, ReceiptHandle=receipt_handle, VisibilityTimeout=0)
        redelivered = delivery(ctx).acquire("replacement-pod")
        assert redelivered is not None, "the undeleted message must come back"
        assert json.loads(redelivered)["message_id"] == ctx.target_invocation, "the same envelope must redeliver, not a different task"

        # 5. Zero task starts. The replacement pod holds the real envelope and asks
        # to bind, which is the last gate before any task code runs.
        digest = envelope_digest(json.loads(redelivered))
        with pytest.raises(BootstrapRefusedError):
            ctx.store.bind(
                invocation_id=ctx.target_invocation,
                digest=digest,
                pod=pod("replacement-pod"),
                now=datetime.now(UTC),
            )

        # Drive the actual startup on the same redelivery, bridging only HTTP
        # transport into the production gateway route and bootstrap store.
        import asyncio
        from contextlib import nullcontext
        from unittest.mock import Mock

        import lib.run_identity as run_identity
        from botocore.credentials import Credentials

        loop = asyncio.get_running_loop()
        responses = []
        original_verify = ctx.runtime.workloads.verify
        monkeypatch.setattr(ctx.runtime.workloads, "verify", lambda token:
            pod("replacement-pod") if token == "replacement-proof" else original_verify(token))

        class Transport:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def post(self, url, *, data, headers, **_kwargs):
                assert url == "https://gateway.test/internal/v1/agent/bootstrap"
                assert headers[WORKLOAD_HEADER] == "replacement-proof"
                assert "Authorization" in headers
                # API Gateway supplies this verified transport identity.
                response = asyncio.run_coroutine_threadsafe(ctx.client.post(
                    "/internal/v1/agent/bootstrap", content=data,
                    headers={**headers, "X-Caller-Identity": "shared-role"},
                ), loop).result(timeout=10)
                responses.append(response)
                return nullcontext(response)

        monkeypatch.setenv("QUEUE_URL", ctx.child.queue)
        monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.test/internal/v1/agent")
        monkeypatch.setattr(worker, "_receive_one_message", lambda *_: (redelivered, "owned-via-gateway"))
        monkeypatch.setattr(worker, "BootstrapLogger", Mock())
        monkeypatch.setattr(run_identity.requests, "Session", Transport)
        monkeypatch.setattr(run_identity, "read_workload_token", lambda: "replacement-proof")
        monkeypatch.setattr("adp_trigger.transport_identity.worker_credentials",
                            lambda _: Credentials("AKIAEXAMPLE", "secret", "token"))
        launch = Mock(side_effect=AssertionError("refused worker launched a subprocess"))
        monkeypatch.setattr(worker.subprocess, "run", launch)
        monkeypatch.setattr(worker.subprocess, "Popen", launch)
        with pytest.raises(run_identity.RunIdentityError, match="gateway refused run identity"):
            await asyncio.to_thread(worker._main)
        assert len(responses) == 1
        assert responses[0].status_code == 404
        launch.assert_not_called()

        # The refusal left no trace a later attempt could build on, and did not
        # disturb the aborting run's own binding.
        assert ctx.store._read("POD#replacement-pod", "BINDING") is None
        after = ctx.store.authority.load_execution(invocation_id=ctx.target_invocation, tenant_id="tenant")
        assert after.workload_binding == original_binding

    async def test_the_same_harness_writes_a_real_aborted_row_when_dynamodb_answers(self, abort_context, monkeypatch, worker):
        """The control for the test above: prove the injected failure is the only failure.

        Without this, the combined test is unfalsifiable in the direction that matters.
        ``terminal_persisted is False`` would be satisfied just as well by a harness
        that never reaches DynamoDB at all — a wrong table name, an unset env var, a
        client that was never wired up — and the conclusion "the write was attempted
        and failed" would be indistinguishable from "the write was never attempted".
        That is precisely the defect root found in the previous revision.

        So the same helper runs against the table that *does* exist, and the worker's
        real ``update_status`` is required to produce a genuine terminal ``aborted``
        row. Its success is what licenses reading the other test's ``False`` as an
        injected transport failure rather than as a broken fixture.
        """
        ctx = abort_context
        await accept_abort(ctx)
        held = ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=1)["Messages"][0]

        exit_code, terminal_persisted, write_attempts = run_worker_abort_finalization(
            worker,
            ctx,
            monkeypatch,
            held["ReceiptHandle"],
            events_table="events",
        )

        # One attempt, because the first one landed — the retry loop is not entered.
        assert terminal_persisted is True
        assert write_attempts == ["aborted"]
        # Row and ack both succeeded, so this is the clean abort: exit 0, and the
        # operator's dashboard genuinely shows `aborted`.
        assert exit_code == 0
        row = next(item for item in ctx.store.client.scan(TableName="events")["Items"] if item["event_id"]["S"] == ctx.target_invocation)
        assert row["status"]["S"] == "aborted"
        assert row["stop_reason"]["S"] == "operator_aborted"
        # And the message is really gone, which is what makes the abort terminal.
        assert not ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=1).get("Messages")

    async def test_the_existing_binding_check_is_what_refuses_it_no_abort_guard(self, abort_context):
        """Name the mechanism, so nobody adds an unreachable guard believing it is needed.

        An earlier revision of #3963 added an ``abort_intent`` check to ``bind`` above
        the PENDING check. It is dead code, and this test is the reason it was removed
        rather than kept as defence in depth: a guard that never executes is not
        defence, it is a misleading signal about where the invariant lives.

        The proof is that the refusal does not depend on the abort at all. A second pod
        is refused identically **before** any abort exists, for exactly the reason it is
        refused after one — the record is ACTIVE and already bound. And a marker can
        only ever exist on such a record, because ``_accept_abort`` runs behind
        ``evaluate_execution_state``, which authorizes only an ACTIVE record. Neither
        ``set_execution_status`` (which refuses PENDING as a destination) nor
        ``provision_pending`` (which writes under ``attribute_not_exists(pk)``) can put
        the record back, so "aborted" and "not PENDING" are not independent conditions.

        Zero task starts on the protected path is therefore enforced by the binding
        lifecycle, which is what root's instruction "do not invent a new guard if actual
        protected startup already enforces zero restarts" describes.
        """
        ctx = abort_context
        digest = ctx.store._read("TENANT#tenant", f"EXEC#{ctx.target_invocation}")["envelope_digest"]["S"]

        # Before the abort: already refused, and this is the mechanism.
        with pytest.raises(BootstrapRefusedError) as before:
            ctx.store.bind(invocation_id=ctx.target_invocation, digest=digest, pod=pod("other-pod"), now=datetime.now(UTC))
        assert str(before.value) == "bootstrap refused"

        await accept_abort(ctx)

        # After it: the same refusal, unchanged. The abort adds no new rejection here
        # because there was never an admission left to reject.
        with pytest.raises(BootstrapRefusedError) as after:
            ctx.store.bind(invocation_id=ctx.target_invocation, digest=digest, pod=pod("other-pod"), now=datetime.now(UTC))
        assert str(after.value) == "bootstrap refused"

        # And the precondition that makes the guard unreachable is the marker's own:
        # it exists only on a record that is ACTIVE and bound.
        record = ctx.store.authority.load_execution(invocation_id=ctx.target_invocation, tenant_id="tenant")
        assert read_marker(ctx) is not None
        assert record.status is ExecutionStatus.ACTIVE
        assert record.workload_binding, "a marker cannot exist on an unbound record"

    def test_no_transition_can_return_an_aborted_run_to_pending(self, abort_context):
        """The premise the guard removal rests on, asserted rather than argued.

        Removing the ``abort_intent`` check from ``bind`` is only safe because an
        aborted record can never again be ``PENDING`` and unbound — the state that
        admits a pod. That is a claim about ``set_execution_status``, not about abort
        code, so if a later change made ``PENDING`` a reachable destination the removal
        would silently become a real hole and every test above would still pass. This
        is the test that would fail instead.

        Driven through the store's public transition API, from each source status a
        real record can hold, rather than by reading the transition table — the table
        is the implementation, the refusal is the requirement.

        Mutation-checked, and the two results are worth recording because they are
        different. Adding ``PENDING`` to the reachable destinations of ``ACTIVE`` fails
        this test, which is the hole it exists to catch. Deleting only the
        ``status is ExecutionStatus.PENDING`` clause does *not* fail it — and that is
        correct rather than a gap: with the transition table unchanged, ``PENDING`` is
        still not a permitted destination from any source, and the conditional write
        would also refuse it. So the requirement is enforced in two places, and this
        test pins the one that would actually admit a pod.
        """
        ctx = abort_context
        authority = ctx.store.authority
        for source in (ExecutionStatus.PENDING, ExecutionStatus.ACTIVE, ExecutionStatus.CANCELLED):
            with pytest.raises(ExecutionTransitionConflictError):
                authority.set_execution_status(
                    invocation_id=ctx.target_invocation,
                    tenant_id="tenant",
                    status=ExecutionStatus.PENDING,
                    expected_attempt=1,
                    expected_status=source,
                )

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
        ctx.runtime.workloads.verify = lambda token: pod("replacement-pod") if token == proof else original(token)
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

    Accepting an abort must not stop the run that is finalizing it. That run still
    needs its credential renewed (every 300s, via the run-identity thread's re-bind)
    because it has work left to do: cancel the SDK, write the terminal row, delete the
    message. Anything that refused it here would revoke the credential the abort needs
    and leave the operator with an accepted command that never reaches the task.

    This is the constraint that makes a startup abort guard self-defeating if placed
    before ``bind``'s existing-binding return, and dead if placed after it.
    """

    async def test_the_aborting_pod_can_still_rebind_and_finalize(self, abort_context):
        ctx = abort_context
        await accept_abort(ctx)
        record = ctx.store.authority.load_execution(invocation_id=ctx.target_invocation, tenant_id="tenant")
        binding = record.workload_binding
        digest = ctx.store._read("TENANT#tenant", f"EXEC#{ctx.target_invocation}")["envelope_digest"]["S"]

        # The aborting pod's own re-bind returns its record rather than raising: it
        # presents the binding it already holds, so `bind` returns on the
        # existing-binding branch and the abort is irrelevant to the decision.
        rebound = ctx.store.bind(invocation_id=ctx.target_invocation, digest=digest, pod=pod(binding), now=datetime.now(UTC))
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
            assert row.get("status", {}).get("S") != "aborted", "the events row must not show a terminal abort until the run has actually stopped"


class TestPermanentTerminalReconciliation:
    """When every terminal-repair retry fails after a confirmed ack.

    Root's requirement: do not label this state reconciled merely because retry
    logic exists. The run is genuinely stopped and genuinely unreported, and the
    honest outcome is that the *rerun* guarantee holds while the *reporting* does
    not — and that the difference is visible, not swallowed.
    """

    async def test_exhausted_repair_after_a_confirmed_ack_is_reported_not_reconciled(self, abort_context, monkeypatch, worker, caplog):
        ctx = abort_context
        await accept_abort(ctx)
        # The ack must genuinely succeed here, so the message is really received and
        # really deleted through the shipped path — this is the post-quiescence
        # branch, and it only exists when the delete is confirmed.
        held = ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=1)["Messages"][0]
        row_before = arrived_at(ctx)

        with caplog.at_level("ERROR"):
            exit_code, terminal_persisted, calls = run_worker_abort_finalization(
                worker,
                ctx,
                monkeypatch,
                held["ReceiptHandle"],
                events_table="events-that-does-not-exist",
            )

        # The ack landed, so the run cannot restart: exiting non-zero would summon a
        # pod that can only pick up an unrelated message. That is why this is 0.
        assert exit_code == 0
        assert terminal_persisted is False
        assert not ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=1).get("Messages"), (
            "the delete must be confirmed for this branch to be the one under test"
        )
        # Every bounded attempt was actually spent, from the shipped constant — twice
        # over: three before the delete, three more in the post-ack repair.
        assert len(calls) == worker.ABORT_TERMINAL_WRITE_ATTEMPTS * 2 == 6
        # And the unreported outcome is stated, not implied by a clean exit code.
        assert "could not be persisted or repaired" in caplog.text, "an unreported abort must say so; a clean exit code alone reads as a clean abort"
        # Named as a reporting defect with the run ID an operator needs, not as a
        # generic error — this is the line that explains an active-looking aborted run.
        assert "cannot execute again" in caplog.text
        assert ctx.target_invocation in caplog.text

        # What is NOT reconciled, stated as an assertion so this test cannot be read
        # as proving repair. The row is still exactly as it was before the abort: no
        # `aborted` status, nothing repaired. The rerun guarantee holds here because
        # the message is deleted, and the marker remains as the durable record that an
        # operator stopped this run — but no process re-attempts this write later, so
        # the dashboard stays stale until something outside this pod reconciles it.
        # Root's instruction: do not label this reconciled merely because bounded
        # retries exist. It is not.
        assert arrived_at(ctx) == row_before
        stale = next(item for item in ctx.store.client.scan(TableName="events")["Items"] if item["event_id"]["S"] == ctx.target_invocation)
        assert stale["status"]["S"] != "aborted", "this branch leaves the row stale by construction"
        assert read_marker(ctx) is not None, "the durable marker is the only surviving record of the operator's stop"
