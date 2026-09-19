"""The agent-facing adapter for delegated monitoring and controls (#5028).

## What this is

The transport-neutral half of the agent control path. It takes a request that
has already been authenticated at the transport layer (SigV4 over the shared
worker IAM role), authenticates the *individual run* behind it, asks
:class:`~src.agentauth.policy.AgentAuthorizationService` the one authorization
question, and — only for a verb this deployment can actually perform — mints the
short-lived signed statement the worker's listener independently verifies.

It is a class rather than a set of route handlers because both agent-facing
surfaces (`status` and `control`) must go through the same six steps in the same
order, and because the CLI's two subcommands are two calls into one object
rather than two paths that could drift.

## The three things a caller does not get to choose

1. **Who it is.** The credential arrives in a header and is HMAC-verified; the
   invocation ID inside it is the caller's identity. Nothing reads
   ``parent_invocation_id`` from the body. That field is what today's
   ``/agent/trigger`` trusts, and it is sourced from ``ADP_MESSAGE_ID``, which
   the worker can rewrite — so a body-supplied identity is a self-assertion.
2. **What is true about the target.** The caller supplies a run ID and nothing
   else. Tenant, flow, generation and relationship all come from the resolver.
   The generation in particular matters: taking it from the request would let a
   caller have an envelope minted for a generation the target is not running,
   and the whole point of binding it is that the *listener* can disagree.
3. **What the command says.** ``command_id`` is read out of the request body
   rather than accepted as a separate argument, because the listener journals
   the body's ``command_id``. Two sources for one value is how an envelope
   authorizing command A ends up attached to journaled command B.

## Why the envelope is minted here and not in the policy

The policy answers "may this caller do this?" and is a pure decision plus store
reads. Signing is an authority-bearing side effect that needs the gateway's
private key, and keeping it out of the policy means the policy stays callable
from anywhere (including tests, and including the spawn adapter, which needs no
envelope at all) without the signing key being in scope.

## What this deliberately does not do

The adapter prepares delegated commands; the shared control service performs
pause/resume and the worker owns their runtime behavior. Authorization precedes
the supported-verb check, so unauthorized callers cannot enumerate capabilities.
Steer and abort remain unavailable. Direct human commands use the distinct
session/ownership authorization path in ``human_control.py`` and the same signer,
transport and worker verifier; this adapter never synthesizes a delegated grant
for a human.

It also does not forward. Returning a :class:`PreparedForward` rather than
performing the request keeps the destination validation and the HTTP client in
``activity/control_service.py``, which already owns both and has the CIDR and
registration checks a second forwarder would have to reimplement.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from src.agentauth.envelope import MAX_ENVELOPE_TTL_SECONDS, EnvelopeError, sign_envelope
from src.agentauth.grants import AgentAction
from src.agentauth.policy import (
    REFUSED_STATUS,
    AgentAuthorizationService,
    AuthorizedRequest,
    PolicyError,
    is_live_control,
)

logger = logging.getLogger("bedrockgateway.agentauth.adapter")

# The credential travels in its own header, not in ``Authorization``. API Gateway
# owns ``Authorization`` for SigV4 on this route, and the two answer different
# questions anyway: SigV4 says "a worker sent this", the credential says "this
# run sent this".
CREDENTIAL_HEADER = "X-Adp-Run-Credential"

# Must match ``ENVELOPE_HEADER`` in
# ``modules/agent-factory/agent/src/control-listener.ts``. Asserted by a parity
# test rather than shared through a config file, because there is no build step
# that spans a Python service and a TypeScript worker image.
ENVELOPE_HEADER = "X-Adp-Control-Authorization"

# A control body is a handful of fields. Bounded before ``json.loads`` so an
# oversized payload is refused rather than parsed; the worker applies its own
# (smaller) cap, and this one only exists so the gateway is not the weak link.
MAX_COMMAND_BODY_BYTES = 8192

# Refusal status for a body the adapter cannot bind an envelope to. 400 rather
# than 404 because it is reached only *after* authorization succeeded — the
# caller is entitled to know its own request was malformed.
INVALID_BODY_STATUS = 400


@dataclass(frozen=True)
class AgentStatusView:
    """What a delegated caller is allowed to learn about another run.

    A projection, not a pass-through of the target's row or of the pod's own
    state payload. Two reasons, and the second is the load-bearing one:

    - a row carries ``control_token``, and a coordinator that could read one
      would be able to command the run directly, bypassing this policy entirely;
    - a projection cannot leak a field a future worker adds, whereas a
      pass-through leaks it the moment the worker starts sending it.
    """

    run_id: str
    generation: int
    state: str
    available: bool
    reason: str | None = None
    # Advertised verbs, already intersected with what the deployment supports by
    # whoever produced this view. Reported so a coordinator does not attempt a
    # verb that cannot work.
    capabilities: dict[str, bool] = field(default_factory=dict)
    updated_at: str | None = None
    # Audit breadcrumbs the caller is entitled to see about its own authority.
    # Deliberately not the grant's contents — an authority reference is a pointer
    # to a human decision row, which is exactly what a coordinator should be able
    # to cite in a comment, and nothing more.
    authority_reference_id: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        """Serialize for the CLI. Nothing here is credential material."""
        return {
            "run_id": self.run_id,
            "generation": self.generation,
            "state": self.state,
            "available": self.available,
            "reason": self.reason,
            "capabilities": dict(self.capabilities),
            "updated_at": self.updated_at,
            "authority_reference_id": self.authority_reference_id,
        }


@dataclass(frozen=True)
class PreparedForward:
    """An authorized command, ready for the transport layer to send.

    Carries the *resolved* target so the forwarder cannot act on the caller's
    version of it, and the headers the listener needs. ``envelope`` is None only
    for actions that are not live control, which today means it is never
    populated in production because no live-control verb is supported.
    """

    authorized: AuthorizedRequest
    command_id: str
    envelope: str | None
    headers: dict[str, str]
    expires_in_seconds: int


class RunStateReader(Protocol):
    """Reads a run's control state for the status path.

    Injected rather than imported so this adapter does not depend on
    ``ControlService.resolve_target``, whose authorization model is the *human*
    one (tenant AND human owner). A delegated agent caller satisfies neither
    check and must not: its authority comes from a grant, not from owning a run.
    """

    def read_state(self, *, run_id: str, generation: int) -> AgentStatusView | None:
        """Return the current state of ``run_id``, or None if unreadable.

        ``generation`` is passed in from resolved target facts so the reader can
        report a mismatch rather than silently answering about a different
        attempt of the same run.
        """
        ...


class AgentControlAdapter:
    """The single agent-facing entry point for status and control."""

    def __init__(
        self,
        *,
        policy: AgentAuthorizationService,
        state_reader: RunStateReader,
        now: Callable[[], datetime] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._policy = policy
        self._reader = state_reader
        self._now = now or (lambda: datetime.now(UTC))
        self._env = env

    # -- status -----------------------------------------------------------

    def status(self, *, credential_token: str, target_run_id: str, presented_workload_binding: str | None = None) -> AgentStatusView:
        """Serve a delegated status read (AC1).

        ``MONITOR`` is the one action a run holds over *itself* without a grant,
        so this is the path that must keep working for an ordinary run reading
        its own state. Reading *another* run's state needs the grant like
        everything else.

        Note the absence of an envelope: a read does not mutate the target, so
        there is nothing for the listener to authorize. Signing one anyway would
        put the gateway's signature on traffic that does not need it and would
        make the read path fail whenever key distribution was incomplete.

        ``authorize_read`` rather than ``authorize``: it is the method that can
        return a grantless authorization, and it cannot express any action other
        than ``MONITOR``, so this call site cannot become a grantless mutation.
        """
        authorized = self._policy.authorize_read(
            credential_token=credential_token,
            target_run_id=target_run_id,
            presented_workload_binding=presented_workload_binding,
        )
        self._policy.require_supported(AgentAction.MONITOR)

        view = self._reader.read_state(
            run_id=authorized.target.run_id,
            generation=authorized.target.generation,
        )
        if view is None:
            # The policy already confirmed the target exists, so an unreadable
            # state is a transport or registration gap, not an authorization
            # answer. Reported as unavailable rather than as not-found: telling
            # an authorized coordinator "no such run" about a run it may legally
            # see would be a lie it cannot act on.
            return AgentStatusView(
                run_id=authorized.target.run_id,
                generation=authorized.target.generation,
                state="unavailable",
                available=False,
                reason="control state could not be read",
                authority_reference_id=authorized.decision.authority_reference_id,
            )

        # The authority reference is stamped from the *decision*, never from the
        # reader, so a state source cannot attribute a read to an authority that
        # did not permit it.
        return AgentStatusView(
            run_id=view.run_id,
            generation=view.generation,
            state=view.state,
            available=view.available,
            reason=view.reason,
            capabilities=dict(view.capabilities),
            updated_at=view.updated_at,
            authority_reference_id=authorized.decision.authority_reference_id,
        )

    # -- control ----------------------------------------------------------

    def prepare_command(
        self,
        *,
        credential_token: str,
        target_run_id: str,
        action: AgentAction,
        request_body: bytes,
        presented_workload_binding: str | None = None,
    ) -> PreparedForward:
        """Authorize a control command and mint its authorization envelope.

        Order, and why each step is where it is:

        1. **Authorize.** Before the body is looked at beyond its size, so an
           unauthorized caller cannot use body validation as an oracle for
           whether the target exists.
        2. **Refuse unimplemented verbs (501).** After authorization, matching
           the human path in ``control_service.authorize_command``.
        3. **Read ``command_id`` from the body.** After 501, so a caller probing
           an unbuilt verb learns nothing about body requirements it will never
           need to satisfy.
        4. **Sign**, binding target, generation, action, command ID and the exact
           body bytes.

        ``request_body`` must be the bytes that will actually be forwarded. A
        re-serialized body digests differently, which the listener will reject —
        correctly, since a rewritten body is precisely what the digest exists to
        catch.
        """
        authorized = self._policy.authorize(
            credential_token=credential_token,
            action=action,
            target_run_id=target_run_id,
            presented_workload_binding=presented_workload_binding,
        )

        # 501 for a verb this deployment does not implement. Every live-control
        # verb lands here today.
        self._policy.require_supported(action)

        command_id = self._command_id(request_body)

        envelope: str | None = None
        if is_live_control(action):
            envelope = self._sign(authorized=authorized, action=action, command_id=command_id, request_body=request_body)

        headers = {
            "X-Adp-Control-Generation": str(authorized.target.generation),
        }
        if envelope is not None:
            headers[ENVELOPE_HEADER] = envelope

        return PreparedForward(
            authorized=authorized,
            command_id=command_id,
            envelope=envelope,
            headers=headers,
            expires_in_seconds=MAX_ENVELOPE_TTL_SECONDS,
        )

    def _sign(
        self,
        *,
        authorized: AuthorizedRequest,
        action: AgentAction,
        command_id: str,
        request_body: bytes,
    ) -> str:
        """Mint the envelope, or refuse the request.

        A signing failure is a 404, the same refusal as everything else on this
        path. It is *not* a 500 and it is *not* an unsigned forward: an
        unconfigured signing key must stop control traffic, because forwarding
        without an envelope is exactly the state the listener's fail-closed check
        exists to refuse. Refusing here means the operator sees one clear
        failure rather than every pod reporting a different one.
        """
        grant = authorized.grant
        if grant is None:
            # Unreachable through ``prepare_command``, which uses the granted
            # path. Kept as a refusal rather than an assertion because the cost
            # of being wrong is signing an envelope with no authority behind it,
            # and "not found" is the correct answer to a request we cannot
            # attribute.
            raise PolicyError(REFUSED_STATUS, "not found")
        try:
            return sign_envelope(
                tenant_id=authorized.credential.tenant_id,
                principal=authorized.credential.principal,
                target_run_id=authorized.target.run_id,
                target_generation=authorized.target.generation,
                action=action.value,
                command_id=command_id,
                request_body=request_body,
                grant_id=grant.grant_id,
                revocation_epoch=grant.revocation_epoch,
                flow_id=authorized.target.flow_id,
                authority_reference_id=grant.authority.reference_id,
                now=self._now(),
                env=self._env,
            )
        except EnvelopeError as exc:
            # The reason is logged, not returned: "envelope key is not
            # configured" tells a caller about the deployment's internals.
            logger.error(
                "Refusing an authorized control command: envelope could not be signed",
                extra={"reason": str(exc), "target_run_id": authorized.target.run_id},
            )
            raise PolicyError(REFUSED_STATUS, "not found") from exc

    @staticmethod
    def _command_id(request_body: bytes) -> str:
        """Extract the command ID the listener will journal.

        Read from the body rather than taken as an argument so the envelope and
        the journal can never name different commands. A body without a usable
        ``command_id`` is refused before signing, because an envelope bound to a
        command ID the body does not carry would be rejected by every listener
        that receives it — and a 400 here is a far better diagnostic than an
        opaque 403 from a pod.
        """
        if not request_body or len(request_body) > MAX_COMMAND_BODY_BYTES:
            raise PolicyError(INVALID_BODY_STATUS, "command body is missing or too large")
        try:
            parsed = json.loads(request_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PolicyError(INVALID_BODY_STATUS, "command body is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise PolicyError(INVALID_BODY_STATUS, "command body must be a JSON object")
        command_id = parsed.get("command_id")
        if not isinstance(command_id, str) or not command_id.strip():
            raise PolicyError(INVALID_BODY_STATUS, "command body must carry a command_id")
        return command_id
