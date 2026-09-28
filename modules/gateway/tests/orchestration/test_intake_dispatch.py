"""Dispatching an intake turn through the real ingest contract (#5331).

What matters here is not that *something* was sent, but that the turn enters where a
browser turn enters. The previous version of this module wrote onto the input SQS FIFO
directly, and its tests asserted on the queue message — which is how it passed CI
while planning nothing: a turn on the queue for a conversation with no session row, no
thread, no transcript and no registered run. So these tests assert on the invocation
of `handler.lambda_handler` and on the handler's own return value, which is where the
session, the thread and the registration outcome actually come from.

The Lambda client is a stub. A live invocation would make exactly these assertions
impossible to check.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.orchestration.intake_dispatch import (
    GATEWAY_API_SOURCE,
    IntakeBusyError,
    IntakeDispatcher,
    IntakeDispatchError,
    new_session_id,
)
from src.orchestration.intake_session import INTAKE_PERSONA

FUNCTION = "adp-dev-agent-gateway-ingest"
SESSION = "sess-abc123"
USER = "user-alpha"
ORG = "org-alpha"


def ingest_ok(*, task_id: str = "task-1", thread_id: str = "th-ab12", status: str = "processing", session_id: str = SESSION) -> dict[str, Any]:
    """What `handle_long_running` returns on the happy path."""
    return {
        "StatusCode": 200,
        "Payload": json.dumps(
            {
                "statusCode": 200,
                "body": json.dumps({"task_id": task_id, "session_id": session_id, "thread_id": thread_id, "status": status}),
            }
        ),
    }


def ingest_raw(envelope: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """An arbitrary handler envelope, for the refusal paths."""
    return {"StatusCode": 200, "Payload": json.dumps(envelope), **extra}


class StubLambda:
    """Records invocations, or raises to simulate a transport failure."""

    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self._response = response if response is not None else ingest_ok()
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        if self._error:
            raise self._error
        self.calls.append(kwargs)
        return self._response

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(call["Payload"]) for call in self.calls]


def dispatcher(client: StubLambda | None = None, function_name: str = FUNCTION) -> IntakeDispatcher:
    return IntakeDispatcher(client if client is not None else StubLambda(), function_name)


class TestTheTurnEntersThroughTheRealIngestContract:
    """The defect this module exists to fix: the queue is the END of the contract."""

    def test_the_turn_invokes_the_ingest_function_rather_than_a_queue(self):
        """A queue write skips the session row, the thread and the run registration.

        All three are what the readback and the worker depend on, so a turn that
        bypasses them is a 202 for a conversation that does not exist.
        """
        client = StubLambda()
        dispatcher(client).send(session_id=SESSION, text="Add rate limiting", user_id=USER, org_id=ORG)
        assert len(client.calls) == 1
        assert client.calls[0]["FunctionName"] == FUNCTION

    def test_the_dispatcher_holds_no_queue_client_at_all(self):
        """Structural, so the shortcut cannot quietly come back.

        A `send_message` path surviving anywhere in this module would mean a second
        way to reach the worker, and the two would drift.
        """
        import inspect

        from src.orchestration import intake_dispatch

        source = inspect.getsource(intake_dispatch)
        assert "send_message" not in source
        assert "MessageGroupId" not in source, "ordering is the ingest Lambda's business now, not ours"

    def test_the_invocation_is_synchronous(self):
        """The handler's return value is the only source of the minted task id.

        It is also the only place the registration outcome appears — its 503 path does
        not enqueue — so fire-and-forget would report success for a refused turn.
        """
        client = StubLambda()
        dispatcher(client).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert client.calls[0]["InvocationType"] == "RequestResponse"

    def test_the_envelope_is_marked_as_the_gateway_source(self):
        """`detect_channel` routes on this discriminator; without it the event falls
        through to the webchat adapter, which finds no claims and drops it — a silent
        200 for a turn that never happened."""
        client = StubLambda()
        dispatcher(client).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert client.payloads[0]["source"] == GATEWAY_API_SOURCE

    def test_the_persona_is_pinned_to_intake(self):
        """Otherwise the server classifier routes a plain sentence to a general agent.

        Pinning also skips the classifier's Bedrock call, which is what keeps a
        synchronous dispatch from paying for one.
        """
        client = StubLambda()
        dispatcher(client).send(session_id=SESSION, text="Add rate limiting", user_id=USER, org_id=ORG)
        assert client.payloads[0]["requested_persona"] == INTAKE_PERSONA

    def test_the_session_id_is_carried_so_the_conversation_is_reused(self):
        """The ingest Lambda keys the session row off this, so the same id continues a
        conversation rather than starting a second one."""
        client = StubLambda()
        dispatcher(client).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert client.payloads[0]["session_id"] == SESSION

    def test_the_users_words_are_passed_through_unmodified(self):
        """It is the agent's input, not a command. Nothing here interprets it."""
        text = "Rate-limit /v1/chat to 100 rpm per tenant; do NOT touch /v1/health"
        client = StubLambda()
        dispatcher(client).send(session_id=SESSION, text=text, user_id=USER, org_id=ORG)
        assert client.payloads[0]["message"] == text


class TestTheTurnIdentityIsTheOneDownstreamUses:
    """A gateway-minted id would name a run nothing downstream had heard of."""

    def test_the_task_id_comes_from_the_ingest_lambda(self):
        """It is the id the thread lock, the registration row and the worker's status
        updates all key off. Minting our own would return a handle that matches
        nothing."""
        turn = dispatcher(StubLambda(ingest_ok(task_id="task-from-ingest"))).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert turn.task_id == "task-from-ingest"

    def test_the_thread_is_surfaced_so_a_client_can_correlate(self):
        """In-flight state and the issue reference both live on the thread."""
        turn = dispatcher(StubLambda(ingest_ok(thread_id="th-99"))).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert turn.thread_id == "th-99"

    def test_a_retry_carries_the_same_registration_key(self):
        """`log_invocation` writes `event_id = message_id`, so a retry that changed it
        would register a SECOND run against the caller's budget for one turn."""
        client = StubLambda()
        subject = dispatcher(client)
        subject.send(session_id=SESSION, text="Narrow it", user_id=USER, org_id=ORG, retry_token="token-7")
        subject.send(session_id=SESSION, text="Narrow it", user_id=USER, org_id=ORG, retry_token="token-7")
        assert client.payloads[0]["message_id"] == client.payloads[1]["message_id"] == "token-7"

    def test_the_retry_token_is_returned_so_a_caller_can_reproduce_it(self):
        """A retry key a caller cannot see is a retry key they cannot use.

        This is the value `message_id` was sent as, and therefore the key the
        registration row exists under. Without it on the turn, a caller has only the
        ingest-minted `task_id` to send back — a different namespace — and every retry
        registers a second run.
        """
        turn = dispatcher(StubLambda()).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG, retry_token="token-7")
        assert turn.retry_token == "token-7"

    def test_a_minted_retry_token_is_surfaced_too(self):
        """The common case: the caller supplied nothing, so the token they need to
        retry with is one they have never seen unless it is returned."""
        client = StubLambda()
        turn = dispatcher(client).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert turn.retry_token == client.payloads[0]["message_id"]

    def test_the_retry_token_is_not_the_ingest_minted_task_id(self):
        """The defect this pair of fields exists to prevent.

        `task_id` comes from the handler and keys the thread lock; the registration row
        is keyed by our `message_id`. Returning one value for both would mean a retry
        either names no registration or names no task.
        """
        turn = dispatcher(StubLambda(ingest_ok(task_id="task-from-ingest"))).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert turn.task_id == "task-from-ingest"
        assert turn.retry_token != turn.task_id

    def test_a_blank_retry_token_is_treated_as_absent(self):
        """An empty string from a JSON body must not become the registration key for
        every turn in the deployment, which would make the second turn look like a
        re-delivery of the first."""
        client = StubLambda()
        subject = dispatcher(client)
        subject.send(session_id=SESSION, text="One", user_id=USER, org_id=ORG, retry_token="  ")
        subject.send(session_id=SESSION, text="Two", user_id=USER, org_id=ORG, retry_token="")
        assert client.payloads[0]["message_id"] not in ("", "  ")
        assert client.payloads[0]["message_id"] != client.payloads[1]["message_id"]

    def test_distinct_turns_get_distinct_registration_keys(self):
        """The mirror of the above: without a retry token two turns must not collapse
        into one run, or the second would be dropped as a re-delivery."""
        client = StubLambda()
        subject = dispatcher(client)
        subject.send(session_id=SESSION, text="One", user_id=USER, org_id=ORG)
        subject.send(session_id=SESSION, text="Two", user_id=USER, org_id=ORG)
        assert client.payloads[0]["message_id"] != client.payloads[1]["message_id"]


class TestAttributionComesFromTheAuthenticatedCaller:
    """This envelope authorizes a worker to inherit its owner's Bedrock destination."""

    def test_identity_fields_are_populated_from_the_resolved_context(self):
        client = StubLambda()
        dispatcher(client).send(
            session_id=SESSION, text="Hi", user_id=USER, org_id=ORG, team_id="team-7", department_id="dept-3", account_type="service"
        )
        payload = client.payloads[0]
        assert payload["user_id"] == USER
        assert payload["org_id"] == ORG
        assert payload["team_id"] == "team-7"
        assert payload["department_id"] == "dept-3"
        assert payload["account_type"] == "service"

    def test_tenant_defaults_to_the_authenticated_org(self):
        client = StubLambda()
        dispatcher(client).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert client.payloads[0]["tenant_id"] == ORG

    def test_an_unattributed_turn_is_refused(self):
        """Fail closed: spend and credential authority could not be resolved later."""
        with pytest.raises(IntakeDispatchError):
            dispatcher().send(session_id=SESSION, text="Hi", user_id="", org_id=ORG)

    def test_a_tenantless_turn_is_refused_before_it_touches_the_session(self):
        """The ingest Lambda refuses to register a run without a tenant, so dispatching
        would 503 *after* creating the session row. Refused here instead, naming the
        cause rather than surfacing a generic service failure."""
        client = StubLambda()
        with pytest.raises(IntakeDispatchError, match="tenant"):
            dispatcher(client).send(session_id=SESSION, text="Hi", user_id=USER, org_id="")
        assert client.calls == [], "nothing may be invoked for a turn that cannot be registered"

    def test_there_is_no_persona_parameter_for_a_caller_to_aim_at(self):
        """The pin is a module constant. A parameter would be a field a request could
        reach, and the far side's allowlist is a second line of defence, not the
        first."""
        import inspect

        parameters = inspect.signature(IntakeDispatcher.send).parameters
        assert "persona" not in parameters
        assert "agent_type" not in parameters
        assert "requested_persona" not in parameters


class TestABufferedTurnIsNotAFailure:
    """The agent will answer it. A caller told otherwise retries and duplicates it."""

    def test_a_queued_turn_raises_busy_rather_than_failed(self):
        """`handle_long_running` returns `status: queued` with `task_id: null` when the
        thread is mid-turn: the message was appended to the thread and will be picked
        up on completion. Nothing was lost."""
        queued = {"task_id": None, "session_id": SESSION, "thread_id": "th-1", "status": "queued"}
        response = ingest_raw({"statusCode": 200, "body": json.dumps(queued)})
        with pytest.raises(IntakeBusyError):
            dispatcher(StubLambda(response)).send(session_id=SESSION, text="Also X", user_id=USER, org_id=ORG)

    def test_busy_is_a_kind_of_dispatch_error_so_no_caller_misses_it(self):
        """Subclassing means an `except IntakeDispatchError` that predates this
        distinction still catches it, rather than letting it escape as a 500."""
        assert issubclass(IntakeBusyError, IntakeDispatchError)


class TestFailuresAreReportedNotSwallowed:
    """A caller told their message was sent waits forever for a reply."""

    def test_a_transport_failure_raises(self):
        with pytest.raises(IntakeDispatchError):
            dispatcher(StubLambda(error=RuntimeError("connection reset"))).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)

    def test_a_lambda_that_raised_is_a_failure_not_a_success(self):
        """`FunctionError` means the handler blew up mid-way, so the turn's side
        effects are indeterminate. A 200 StatusCode on the invoke envelope does NOT
        mean the handler succeeded — reading only that is the classic mistake."""
        response = {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": json.dumps({"errorMessage": "KeyError"})}
        with pytest.raises(IntakeDispatchError):
            dispatcher(StubLambda(response)).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)

    def test_a_registration_failure_names_the_operators_problem(self):
        """The ingest Lambda's 503: it un-marked the thread and did not enqueue, so
        nothing happened. The message must point at the run-registration table rather
        than telling the caller to retry into a deployment that cannot register."""
        response = ingest_raw({"statusCode": 503, "body": json.dumps({"error": "Could not register and dispatch this run. Please retry."})})
        with pytest.raises(IntakeDispatchError, match="run-registration table"):
            dispatcher(StubLambda(response)).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)

    def test_registration_unavailable_is_not_reported_as_a_generic_refusal(self):
        """Distinguished from the 4xx path deliberately, because the two need
        different actions.

        A refusal is the caller's problem and a retry may help. This is an operator's:
        the run-registration table is missing or unreachable, and every retry will fail
        the same way until someone configures it. Echoing the handler's own terse
        "please retry" as a generic refusal would send the caller round a loop that
        cannot succeed.
        """
        response = ingest_raw({"statusCode": 503, "body": json.dumps({"error": "Could not register and dispatch this run. Please retry."})})
        with pytest.raises(IntakeDispatchError) as caught:
            dispatcher(StubLambda(response)).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)
        assert "refused this turn" not in str(caught.value)
        assert "nothing was dispatched" in str(caught.value)

    def test_a_rejected_persona_pin_is_a_failure_not_a_silent_success(self):
        """The handler's 400 path. It returns before touching the session, so the
        conversation does not exist — reporting 202 would hand back an id that 404s."""
        response = ingest_raw({"statusCode": 400, "body": json.dumps({"error": "invalid persona", "session_id": SESSION})})
        with pytest.raises(IntakeDispatchError):
            dispatcher(StubLambda(response)).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)

    def test_an_unconfigured_deployment_reports_unavailable_rather_than_invoking(self):
        subject = IntakeDispatcher(None, "")
        assert subject.is_configured is False
        with pytest.raises(IntakeDispatchError):
            subject.send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG)

    def test_a_missing_function_name_is_unconfigured_even_with_a_client(self):
        """Both halves must be present, or `is_configured` would answer yes to a
        dispatcher with nowhere to send."""
        assert IntakeDispatcher(StubLambda(), "").is_configured is False

    def test_an_empty_turn_is_refused_before_it_reaches_the_agent(self):
        client = StubLambda()
        with pytest.raises(IntakeDispatchError):
            dispatcher(client).send(session_id=SESSION, text="   ", user_id=USER, org_id=ORG)
        assert client.calls == []

    def test_the_failure_message_does_not_carry_the_users_text(self):
        """A planning turn is user content and does not belong in an error a log or a
        ticket might capture."""
        secret = "migrate the acme-payroll database"
        try:
            dispatcher(StubLambda(error=RuntimeError("boom"))).send(session_id=SESSION, text=secret, user_id=USER, org_id=ORG)
        except IntakeDispatchError as exc:
            assert secret not in str(exc)

    @pytest.mark.parametrize("payload", ["not json at all", "{}", '{"statusCode": 200, "body": "{}"}'])
    def test_an_unacknowledged_payload_reports_uncertain_delivery(self, payload):
        response = {"StatusCode": 200, "Payload": payload}
        with pytest.raises(IntakeDispatchError, match="delivery is uncertain"):
            dispatcher(StubLambda(response)).send(session_id=SESSION, text="Hi", user_id=USER, org_id=ORG, retry_token="mine")


class TestSessionIdsAreServerMinted:
    """A client-chosen id could collide with, or address, somebody else's row."""

    def test_ids_are_unique(self):
        assert new_session_id() != new_session_id()

    def test_an_id_does_not_embed_a_sortable_timestamp(self):
        """The SPA's `sess-<epoch>-<rand>` invites a client to sort on it, which the
        GSI's non-chronological range key makes unsound."""
        session_id = new_session_id()
        assert session_id.startswith("sess-")
        assert session_id[5:].isalnum()
        assert "-" not in session_id[5:]
