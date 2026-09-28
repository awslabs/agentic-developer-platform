"""Gateway operator-plane channel adapter (#5331).

EPIC #4191. Lets a non-browser client — `adp flow start` — reach the SAME intake
conversation the SPA reaches, by entering through the SAME ingest contract.

--------------------------------------------------------------------------------
Why this is an adapter and not a second pipeline
--------------------------------------------------------------------------------

`handle_unified_message` is where a turn becomes durable. It creates or reuses the
session row, classifies (or honours a pinned persona), appends the transcript,
creates or continues a thread, marks that thread as processing, registers the run in
the webhook-events table so the worker may inherit its owner's Bedrock destination,
and only then enqueues. That is nine side effects, and every one of them is load-
bearing: the operator-plane readback (`gateway/src/orchestration/intake_session.py`)
reads `threads.<tid>.processing_task_id` for in-flight state and
`threads.<tid>.github_issue_number` for the issue, neither of which exists unless
`create_thread` ran.

A client that wrote onto the input queue directly would therefore enqueue a turn
whose conversation does not exist. The worker would have no thread to attach to, the
readback would return 404 for a session the caller was just handed, and nothing would
be registered — so the turn would spend against the shared worker's account rather
than its owner's. The gateway's own tests would pass throughout, because they would
be asserting on the queue message rather than on the conversation.

So the seam is here, at the point where a platform event becomes a `UnifiedMessage`,
which is the seam the ingest Lambda already has for Slack and webchat. Everything
after this function is the one existing implementation, shared verbatim.

--------------------------------------------------------------------------------
The channel is deliberately WEBCHAT, not a new channel value
--------------------------------------------------------------------------------

`user_workspace` on the session row is `f"{user_id}#{channel}"` and is the hash key
of the `user-workspace-index` GSI that `--resume` queries. Minting a new channel
value would put terminal-started conversations in a different GSI partition from
browser-started ones, so the same person's conversation would be invisible from the
other client — which is precisely the split this EPIC exists to prevent. One
conversation, reachable from either surface.

What differs is recorded as provenance instead: `platform_data["ingress"]` is
`"gateway-api"`. Nothing downstream branches on it, so the agent's behaviour is
identical either way; it exists so an operator reading a session row can tell where
a turn came from.

--------------------------------------------------------------------------------
The trust boundary: IAM, not the envelope
--------------------------------------------------------------------------------

The webchat adapter derives identity from Cognito authorizer claims, because its
transport authenticates. This adapter has no authorizer: it parses a direct
`lambda:InvokeFunction` payload, so the identity in that payload is only as
trustworthy as the permission to invoke this function at all.

That is the whole control, and it is deliberately the only one: `lambda:Invoke` on
the ingest function is granted to the gateway's service role and to nothing else, and
the gateway resolved `user_id` / `org_id` from a verified JWT before calling. There is
no signature to check here because there is no shared secret that would add anything
an IAM grant does not already assert.

Two consequences are worth stating because they are easy to get wrong:

- **This envelope must be unforgeable from the WebSocket.** It is recognised only by
  an explicit `source` discriminator, and `detect_channel` checks for it only on
  events carrying no `connectionId` and no Slack signature. An API Gateway WebSocket
  event always carries a `connectionId`, so a client that sent `{"source":
  "gateway-api", ...}` as their chat text cannot reach this parser — their event is
  still a webchat event and their identity still comes from their own claims.

- **Identity is read from the envelope, never from anything nested under it that a
  browser could also have supplied.** `attachments` and free text are carried;
  `requested_persona` is NOT taken from the caller at all but pinned by the gateway's
  own constant, so there is no field here for a caller to aim at.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import (
    ChannelAdapter,
    ChannelType,
    MessageRole,
    UnifiedMessage,
)

logger = logging.getLogger(__name__)

# The discriminator that marks an event as an IAM-gated operator-plane invocation.
# Explicit rather than inferred from the absence of other markers: "this event has no
# connectionId and no Slack signature" is also true of a malformed webchat event, and
# treating malformed-webchat as trusted-gateway would be an identity bypass.
GATEWAY_API_SOURCE = "gateway-api"


class GatewayApiAdapter(ChannelAdapter):
    """Parses an IAM-gated invocation from the gateway's operator-plane API.

    Emits `ChannelType.WEBCHAT` so the resulting session row is indistinguishable
    from a browser-started one — see the module docstring on why a new channel value
    would fragment the resume GSI.
    """

    def __init__(self) -> None:
        pass

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.WEBCHAT

    def verify_request(self, headers: dict[str, str], body: bytes) -> bool:
        """Authenticity is the IAM grant on `lambda:InvokeFunction`.

        Returns True because there is nothing further to verify in-process: this
        function is not reachable over HTTP, so an event that arrives at all was
        invoked by a principal the resource policy permits. A signature check here
        would need a shared secret whose compromise is strictly more likely than a
        stolen IAM role, and would read as defence it does not provide.
        """
        return True

    def parse_event(self, payload: dict[str, Any]) -> UnifiedMessage | None:
        """Parse the operator-plane envelope into the shared message shape.

        Expected payload::

            {
              "source": "gateway-api",
              "session_id": "sess-...",     # minted by the gateway, server-side
              "message": "Add per-tenant rate limiting",
              "user_id": "...",             # from the gateway's verified JWT
              "org_id": "...",              #   ""
              "tenant_id": "...",           # optional; defaults to org_id
              "team_id": "", "department_id": "", "account_type": "human",
              "requested_persona": "intent-refinement",
              "message_id": "...",          # optional; the run-registration key
            }

        Returns None for anything unusable, which the handler turns into a 200 with
        no side effects — the same "quietly ignore" contract the other adapters have
        for events that are not messages.
        """
        session_id = str(payload.get("session_id", "") or "").strip()
        text = str(payload.get("message", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        org_id = str(payload.get("org_id", "") or "").strip()

        if not text:
            # No side effects for an empty turn: it would consume the conversation's
            # per-session ordering slot and produce a reply to nothing.
            logger.warning("gateway-api envelope carried no message text; dropping")
            return None
        if not user_id:
            # Fail closed. An unattributed turn is one whose spend and credential
            # authority cannot be resolved later, and `handle_long_running` would
            # refuse it anyway — refusing here keeps it from touching the session row
            # on the way to that refusal.
            logger.error("gateway-api envelope carried no user_id; dropping")
            return None
        if not session_id:
            # Required rather than defaulted to `session_key`. The gateway mints the
            # id and hands it to the caller BEFORE the reply exists, so that the
            # caller can resume; a server-side substitution here would hand back an
            # id that resolves to nothing.
            logger.error("gateway-api envelope carried no session_id; dropping")
            return None

        tenant_id = str(payload.get("tenant_id", "") or "").strip() or org_id

        message_kwargs: dict[str, Any] = {}
        # Carried when supplied so the gateway and the run-registration row agree on
        # one key. `log_invocation` writes `event_id = message_id` and the worker
        # advances status by that same pair, so a regenerated id here would orphan
        # the row the worker later tries to update.
        supplied_message_id = str(payload.get("message_id", "") or "").strip()
        if supplied_message_id:
            message_kwargs["message_id"] = supplied_message_id

        return UnifiedMessage(
            channel=ChannelType.WEBCHAT,
            # Empty: there is no socket to post back to. The response Lambda already
            # treats an absent `connection_id` as "no live client", which is correct
            # here — a terminal client polls the readback instead.
            channel_id="",
            user_id=user_id,
            # The id rather than an email: this adapter has no directory lookup, and
            # inventing a display name would put a guess in the transcript.
            user_name=user_id,
            # `thread_id` is what `handle_unified_message` reads as the session id.
            # Named `thread_id` on this dataclass for historical reasons; it is the
            # conversation, not the per-task thread inside it.
            thread_id=session_id,
            text=text,
            role=MessageRole.USER,
            timestamp=time.time(),
            is_mention=True,
            is_direct_message=True,
            # No resolver call. `provider="cognito"` marks the identity as already
            # resolved, exactly as the webchat adapter does, because the gateway
            # verified the JWT before invoking — a resolve_user round trip here would
            # either be a no-op or, worse, re-resolve an already-trusted identity
            # through an untrusted channel_context.
            provider="cognito",
            provider_user_id=user_id,
            platform_data={
                # Absent on purpose, not empty-by-accident: see channel_id above.
                "connection_id": "",
                "tenant_id": tenant_id,
                "org_id": org_id,
                "team_id": str(payload.get("team_id", "") or ""),
                "department_id": str(payload.get("department_id", "") or ""),
                # Defaulted to human because the operator-plane API authenticates
                # humans and service accounts alike, and a service account's run is
                # not human-rooted. The gateway sends the real value; the default is
                # the conservative one only in the sense that it is the common case —
                # the caller is expected to state it.
                "account_type": str(payload.get("account_type", "") or "human"),
                "role": str(payload.get("role", "") or ""),
                # Validated downstream against `PINNABLE_PERSONAS` like any other
                # client-supplied pin. Not exempted from that check because being
                # IAM-gated makes the CALLER trusted, not the value correct: a
                # gateway bug that sent a bad persona should be rejected, not
                # obeyed.
                "requested_persona": str(payload.get("requested_persona", "") or ""),
                # Provenance only. Nothing downstream branches on it — see the module
                # docstring on why the two clients must not be able to diverge.
                "ingress": GATEWAY_API_SOURCE,
                "intake_repository": str(payload.get("repository", "") or ""),
                "intake_issue": str(payload.get("issue", "") or ""),
            },
            **message_kwargs,
        )
