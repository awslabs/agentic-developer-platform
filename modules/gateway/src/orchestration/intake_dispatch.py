"""Send an intake turn through the real ingest contract (#5331).

EPIC #4191. Paired with `intake_session.py`, which reads the conversation back.

--------------------------------------------------------------------------------
Why this invokes the ingest Lambda instead of writing to the queue
--------------------------------------------------------------------------------

The planning agent already exists: the `intent-refinement` persona, driven by the chat
worker. What did not exist was a way for a non-browser client to reach it. The obvious
shortcut — put a message on the same SQS FIFO the ingest Lambda publishes to — is the
one this module deliberately does not take, because **the queue is the end of the
intake contract, not the contract itself.**

Before `handle_long_running` enqueues anything it does nine other things, and every
one of them is load-bearing:

1. creates or reuses the session row (`get_or_create_session`) — and stamps `org_id`,
   which is what the readback's tenant check compares against;
2. validates any pinned persona against `PINNABLE_PERSONAS`, rejecting with no side
   effects;
3. classifies the message, or honours the pin and skips the classifier;
4. decides `new` vs `follow_up` against the session's existing threads;
5. appends the user's turn to the session transcript (`append_message`);
6. creates the thread (`create_thread`) or reuses an idle one;
7. marks that thread as processing (`set_thread_processing`) — the in-flight lock;
8. registers the run in the webhook-events table (`log_invocation`), which is what
   authorizes the worker to inherit its owner's Bedrock destination;
9. sends an acknowledgement onto the response queue.

A client writing to the queue directly performs **none** of them. The turn would
arrive for a conversation that does not exist: no session row, so
`GET /intake/sessions/{id}` 404s on an id the caller was just handed; no thread, so
`working` and `issue_ref` are structurally empty; no registered run, so the turn spends
against the shared worker's account rather than the caller's. Worse, all of that is
invisible to a test that asserts on the queue message — which is exactly how the
earlier version of this module passed CI while planning nothing.

So the turn enters where the browser's turn enters, through
`handler.lambda_handler`, via a channel adapter (`channels/gateway_api.py`) whose only
job is to turn this envelope into the same `UnifiedMessage` the webchat adapter
produces. Everything after that point is one implementation, shared verbatim. Two
planners that agreed today and drifted tomorrow would hand an operator different
plans depending on which client they opened; there is now only one.

--------------------------------------------------------------------------------
Synchronous invocation, on purpose
--------------------------------------------------------------------------------

`RequestResponse`, not `Event`. The ingest Lambda's return value is the only place
several answers a caller needs exist at all: the `task_id` it minted (the handle a
retry and a status lookup both need), the `thread_id`, and critically whether
registration succeeded — its 503 path deliberately un-marks the thread and does NOT
enqueue. Fire-and-forget would report success for a turn that was refused, and a
caller would poll for a reply that can never arrive.

The cost is that a start waits on the ingest Lambda's cold start plus, on the
unpinned path, the classifier's Bedrock call. The intake path pins its persona
precisely so the classifier is skipped, and the route returns before the *agent*
replies, so what the caller waits on is dispatch rather than planning.

--------------------------------------------------------------------------------
What this module refuses to do
--------------------------------------------------------------------------------

It does not accept a caller-supplied persona. The envelope's `requested_persona` is a
module constant, so there is no field for a caller to aim at — and it is still
validated on the far side against the allowlist, because being IAM-gated makes the
caller trusted, not the value correct.

It does not accept a caller-supplied `user_id` or `org_id` either. Both come from the
authenticated context the route resolved, because the ingest Lambda treats them as
authority for whose credentials and whose budget a run may use.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from src.orchestration.intake_session import INTAKE_CHANNEL, INTAKE_PERSONA

logger = logging.getLogger(__name__)

# The discriminator the ingest Lambda's `detect_channel` matches on. Mirrors
# `GATEWAY_API_SOURCE` in `channels/gateway_api.py`. Named once here rather than
# inlined, because a mismatch fails in the worst possible way: the event falls
# through to the webchat adapter, which finds no Cognito claims, drops the message and
# returns 200 — a silent success for a turn that never happened.
GATEWAY_API_SOURCE = "gateway-api"


class LambdaClient(Protocol):
    """The `invoke` subset of boto3's Lambda client.

    A Protocol so the envelope, the persona pin and the response mapping are testable
    without AWS — those are behavioural requirements, not incidental plumbing.
    """

    def invoke(self, **kwargs: Any) -> dict[str, Any]: ...


class IntakeDispatchError(RuntimeError):
    """The turn could not be dispatched.

    Deliberately not swallowed into a success response. A user told their message was
    sent, when it was not, waits for a reply that can never arrive — strictly worse
    than an error they can retry.
    """


class IntakeBusyError(IntakeDispatchError):
    """The conversation is already answering a turn, so this one was buffered.

    Distinct from a failure because nothing was lost: the ingest Lambda appends the
    message to the thread and the agent picks it up when the current turn completes.
    A caller must be told it is queued rather than told it failed, or they will retry
    and stack up duplicate turns.
    """


@dataclass(frozen=True)
class IntakeTurn:
    """A dispatched turn, as the caller may report it.

    Two identities, deliberately named apart, because conflating them is a real
    double-billing defect rather than a naming preference:

    - `task_id` is minted by the INGEST LAMBDA. It is what the thread's processing
      lock (`threads.<tid>.processing_task_id`) and the worker's own progress
      reporting key off, so a gateway-minted value here would name a task nothing
      downstream had heard of. It is useful for correlation and useless for retry.
    - `retry_token` is minted HERE, and is the value sent as the envelope's
      `message_id`. `log_invocation` writes `event_id = message_id` and the worker
      advances that row by the same pair, so it — and only it — is the key a retry
      must reproduce.

    Handing a caller the first and asking them to retry with it is how one turn
    becomes two registered runs against their budget, with the worker's status
    updates split across two rows. So both are returned, and the one a caller sends
    back is the one that keys the registration.
    """

    session_id: str
    task_id: str
    enqueued_at: int
    # The per-topic thread inside the conversation. Surfaced because the readback's
    # in-flight state and issue reference both live on it, so a client that wants to
    # correlate what it sent with what it reads back needs it.
    thread_id: str = ""
    # What to send back to retry this exact turn. See the class docstring.
    retry_token: str = ""


def new_session_id() -> str:
    """Mint a session id for a conversation the CLI is starting.

    Server-side rather than client-side, which is the deliberate difference from the
    browser path. The SPA mints `sess-<epoch>-<rand>` locally and keeps it in
    `localStorage`, which is why a cleared browser orphans its server rows
    irrecoverably. An id minted here is returned to the caller AND resolvable from
    the caller's identity afterwards (`IntakeSessionReader.latest_for_user`), so
    losing it is recoverable.

    `uuid4` rather than a timestamp-plus-random string: it needs no clock to be
    unique, and an id that embeds its creation time invites a client to sort or
    reason about it, which the GSI's non-chronological range key makes unsound.
    """
    return f"sess-{uuid.uuid4().hex}"


class IntakeDispatcher:
    """Sends turns to the planning agent through the ingest Lambda.

    Holds no AWS client of its own; both the client and the function name are
    injected, so a deployment missing the configuration is a constructed-but-
    unavailable dispatcher rather than an import-time failure that would take the
    whole app down.
    """

    def __init__(self, lambda_client: LambdaClient | None, function_name: str = "") -> None:
        self._lambda = lambda_client
        self._function_name = function_name

    @property
    def is_configured(self) -> bool:
        """Whether this deployment can start a planning conversation at all.

        Read by the route so it can answer "unavailable" rather than "failed": an
        unconfigured deployment is an operator's problem and a failed dispatch is a
        retryable one, and a caller needs to tell them apart.
        """
        return self._lambda is not None and bool(self._function_name)

    def send(
        self,
        *,
        session_id: str,
        text: str,
        user_id: str,
        org_id: str,
        tenant_id: str = "",
        retry_token: str | None = None,
        account_type: str = "human",
        team_id: str = "",
        department_id: str = "",
        repository: str = "",
        issue: str = "",
    ) -> IntakeTurn:
        """Dispatch one turn of an intake conversation.

        Args:
            session_id: The conversation this turn belongs to. The ingest Lambda
                creates the row on first use and reuses it afterwards, so the same
                id continues a conversation rather than starting a second.
            text: What the user said. Passed through unmodified — it is the agent's
                input, not a command, and nothing here interprets it.
            user_id: The authenticated caller. Never taken from a request body; the
                ingest Lambda treats this as the run's owner for credential and
                budget attribution.
            org_id: The caller's authenticated tenant, for the same reason.
            tenant_id: Optional explicit tenant, defaulting to `org_id`.
            retry_token: The value a caller was handed as `IntakeTurn.retry_token` on
                a turn that may already have been sent. Becomes the envelope's
                `message_id`, which is the run-registration key — so a retry names
                the same run rather than registering a second one against the
                caller's budget. Deliberately NOT the ingest-minted `task_id`: that
                names the task, not the registration, and sending it here would
                register a second run under a fresh key. A new token is minted when
                this is absent.
            account_type: Whether the caller is a human or a service account. Carried
                because `log_invocation` records `is_human_rooted` from it, and a
                service account's run is not human-rooted.

        Raises:
            IntakeBusyError: The conversation is mid-turn; this one was buffered.
            IntakeDispatchError: Unconfigured, refused, or the invocation failed.
        """
        if not self.is_configured:
            raise IntakeDispatchError("conversational planning is not configured on this deployment; no intake ingest function is available")
        if not text.strip():
            # Refused rather than sent: an empty turn would consume the agent's
            # per-session ordering slot and produce a reply to nothing.
            raise IntakeDispatchError("an intake turn needs a message")
        if not user_id:
            # Fail closed. An unattributed turn is one whose spend and credential
            # authority cannot be resolved later.
            raise IntakeDispatchError("an intake turn requires an authenticated caller")
        if not org_id:
            # The ingest Lambda refuses to register a run without a tenant, so
            # dispatching would 503 after touching the session row. Refused here
            # instead, with a message that names the cause.
            raise IntakeDispatchError("an intake turn requires an authenticated tenant")

        # The retry identity, carried end-to-end and returned to the caller as
        # `IntakeTurn.retry_token`. `log_invocation` writes `event_id = message_id`
        # and the worker advances status by that same key, so a caller that retries
        # with the same token names the same run instead of registering a second one
        # against their budget.
        message_id = (retry_token or "").strip() or uuid.uuid4().hex[:16]

        payload: dict[str, Any] = {
            "source": GATEWAY_API_SOURCE,
            "session_id": session_id,
            "message": text,
            "user_id": user_id,
            "org_id": org_id,
            "tenant_id": tenant_id or org_id,
            "team_id": team_id,
            "department_id": department_id,
            "account_type": account_type,
            # The persona pin. A module constant, not a parameter — see the module
            # docstring on why there is no field here for a caller to aim at. Pinning
            # it also skips the classifier, which is what keeps a synchronous
            # dispatch from paying a Bedrock call.
            "requested_persona": INTAKE_PERSONA,
            "channel": INTAKE_CHANNEL,
            "message_id": message_id,
            "repository": repository,
            "issue": issue,
        }

        assert self._lambda is not None  # guarded by is_configured above
        try:
            response = self._lambda.invoke(
                FunctionName=self._function_name,
                # Synchronous: the ingest Lambda's return value is the only place the
                # minted task id and the registration outcome exist. See the module
                # docstring.
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode("utf-8"),
            )
        except Exception as exc:
            # Logged without the message text: a planning turn is user content and
            # does not belong in operational logs.
            logger.exception("intake turn dispatch failed session=%s message=%s", session_id, message_id)
            raise IntakeDispatchError("could not dispatch this intake turn; please retry") from exc

        return self._turn_from_response(response, session_id=session_id, message_id=message_id, user_id=user_id, text=text)

    def _turn_from_response(self, response: dict[str, Any], *, session_id: str, message_id: str, user_id: str, text: str) -> IntakeTurn:
        """Map the ingest Lambda's return value onto a turn, or onto the right error.

        Three distinct failure shapes have to stay distinct, because a caller acts
        differently on each:

        - `FunctionError` — the Lambda itself raised. The turn's side effects are
          indeterminate, so this is a failure, not a refusal.
        - a 503 body — registration was unavailable, and the handler deliberately
          un-marked the thread and did not enqueue. Nothing happened; retrying is
          correct.
        - a `queued` status — the conversation is mid-turn and the message was
          buffered. NOT a failure: it will be answered, and a caller told otherwise
          would retry and stack up duplicates.
        """
        if response.get("FunctionError"):
            logger.error("intake ingest raised session=%s message=%s kind=%s", session_id, message_id, response.get("FunctionError"))
            raise IntakeDispatchError("the intake service failed to handle this turn; please retry")

        body = self._decode(response)
        status_code = int(body.get("statusCode", 0) or 0)
        detail = self._decode_body(body)

        if status_code == 503:
            # The ingest Lambda's own registration-unavailable path. Surfaced as
            # unavailable-shaped rather than failed, because an operator has to fix
            # something (the run-registration table) and the caller's retry is
            # pointless until they do.
            logger.error("intake run registration unavailable session=%s message=%s", session_id, message_id)
            raise IntakeDispatchError(
                "this deployment could not register the planning run, so nothing was dispatched; "
                "the intake run-registration table may not be configured"
            )
        if status_code and status_code >= 400:
            logger.error("intake ingest refused session=%s message=%s status=%s", session_id, message_id, status_code)
            raise IntakeDispatchError(f"the intake service refused this turn ({detail.get('error') or status_code})")

        turn_status = str(detail.get("status", "") or "")
        if turn_status not in {"queued", "processing", "dispatched_github"} or not detail.get("session_id"):
            raise IntakeDispatchError(
                "the intake service did not acknowledge the turn; delivery is uncertain. Check the session before sending again"
            )
        resolved_task_id = str(detail.get("task_id") or "") or message_id
        thread_id = str(detail.get("thread_id") or "")

        if turn_status == "queued":
            raise IntakeBusyError("the planning agent is still answering the previous turn; this message was queued and will be answered next")

        logger.info(
            "intake turn dispatched session=%s task=%s thread=%s user=%s chars=%s",
            session_id,
            resolved_task_id,
            thread_id,
            user_id,
            len(text),
        )
        return IntakeTurn(
            session_id=str(detail.get("session_id") or session_id),
            task_id=resolved_task_id,
            enqueued_at=int(time.time()),
            thread_id=thread_id,
            # Ours, not the handler's. This is the key the registration row was
            # written under, so it is the only value a retry can reproduce.
            retry_token=message_id,
        )

    @staticmethod
    def _decode(response: dict[str, Any]) -> dict[str, Any]:
        """The Lambda invoke envelope's `Payload`, as a dict.

        Tolerant of an unreadable payload because a successful invoke with an
        unparseable body is still a turn that probably happened — raising here would
        tell the caller nothing was dispatched, and they would retry into a
        conversation that already has their message.
        """
        payload = response.get("Payload")
        if payload is None:
            return {}
        try:
            raw = payload.read() if hasattr(payload, "read") else payload
            decoded = json.loads(raw)
        except Exception:
            logger.warning("could not decode intake ingest payload", exc_info=True)
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _decode_body(envelope: dict[str, Any]) -> dict[str, Any]:
        """The handler's JSON `body`, which arrives as a string inside the envelope."""
        body = envelope.get("body")
        if isinstance(body, dict):
            return body
        if not isinstance(body, str) or not body.strip():
            return {}
        try:
            decoded = json.loads(body)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}
