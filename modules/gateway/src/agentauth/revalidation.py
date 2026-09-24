"""Online authorization of a queued command by its authenticated target worker."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from src.activity.control_schemas import MAX_REQUEST_BYTES
from src.agentauth.bootstrap import BootstrapRefusedError, issue_bound_credential
from src.agentauth.envelope import (
    ABORT_RECEIPT_ACTION,
    ABORT_RECEIPT_AUDIENCE,
    SIGNING_KEY_ID_ENV,
    EnvelopeError,
    _signing_key,
    sign_envelope,
    verify_envelope,
)
from src.agentauth.grants import LIVE_CONTROL_ACTIONS, AgentAction
from src.agentauth.store import AbortIntentConflictError

# ``AuthorityStoreError`` is deliberately *not* handled here. It means the store
# was unreachable, which is not an answer about this command. Letting it escape
# reaches the caller's 503 ("retry"), whereas converting it into a refusal would
# hand the worker a decided-looking answer derived from an unknown state.


class RevalidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    envelope: str = Field(min_length=1, max_length=8192)
    action: AgentAction
    command_id: str = Field(min_length=1, max_length=128)
    body_base64: str = Field(max_length=4 * ((MAX_REQUEST_BYTES + 2) // 3))


async def _accept_abort(runtime, body, *, target, generation, raw: bytes) -> dict:
    """Persist durable abort intent, then mint the receipt that proves it (#3963).

    This is the answer to "how does a finalizing supervisor know an abort was
    actually accepted by a live run, rather than merely requested at some point?"
    The command envelope cannot answer it: it is issued before delivery and stays
    valid for its whole TTL whether or not the run ever took the command. And the
    worker cannot answer it about itself — a pod-written literal saying
    ``delivery: accepted`` is a self-assertion, which is exactly the gap this
    replaces.

    Revalidation is the one place in the system that *does* know. It runs
    immediately before the executor, against the registration for the current
    generation and a live grant/ownership re-check, on a request authenticated as
    the target pod. So the receipt is minted here and nowhere else.

    **The invariant is that a receipt never exists without the durable fact.**
    ``record_abort_intent`` either establishes the marker for this exact attempt,
    command and body, or it raises — and every way it raises leaves this function
    with no return value, so nothing can be told "allowed" for an abort that was
    not made durable. That is what the receipt ultimately stands behind: it is the
    evidence that later justifies deleting the queue message and reporting a
    deliberate stop, so a receipt minted while nothing recorded the abort would
    produce a run that reports a clean operator stop and is then redelivered and
    run again.

    The moment comes back from the store rather than from this request, so a
    retried delivery of one command re-attests when the abort was *first* accepted
    instead of quietly moving it forward and making one abort look like two.

    ``abort_requested_at`` is returned for reporting only. Nothing should decide
    anything from it — only the signed receipt is evidence, because a plain field
    is trusted only as far as whatever relayed it.
    """
    # `verify_envelope` has already required the signed `body_digest` to equal the
    # digest of these exact bytes, so hashing them here records the digest the
    # gateway *signed over* rather than an independent claim about them.
    marker = await run_in_threadpool(
        runtime.store.authority.record_abort_intent,
        invocation_id=target.invocation_id,
        tenant_id=target.tenant_id,
        attempt=target.attempt,
        command_id=body.command_id,
        body_digest=hashlib.sha256(raw).hexdigest(),
    )
    receipt = sign_envelope(
        tenant_id=target.tenant_id,
        principal=f"{target.invocation_id}#{target.attempt}",
        target_run_id=target.invocation_id,
        target_generation=generation,
        action=ABORT_RECEIPT_ACTION,
        # Read back from the marker rather than from the request. They are equal on
        # every path the store can return from — it either recorded this command or
        # raised — so this is not a check, it is which of the two is the source of
        # truth. The receipt should say what the store holds, so that remains true
        # if the store ever grows a path that reconciles a command id.
        command_id=marker["command_id"],
        # The operator's exact request bytes. The finalizer hashes what it holds and
        # requires equality, which is what lets it read the abort *reason* out of a
        # signed preimage instead of trusting a field beside the signature.
        request_body=raw,
        audience=ABORT_RECEIPT_AUDIENCE,
        authority_kind="human_session",
        env=runtime.env,
    )
    return {
        "allowed": True,
        "command_id": body.command_id,
        "generation": generation,
        "max_round_trip_ms": 1000,
        "abort_receipt": receipt,
        "abort_requested_at": marker["requested_at"],
    }


async def revalidate_command(runtime, body, *, context):
    """No identity claims from the body; the signed proof names the initiator.

    Expired forwarding proofs are rejected even when queued. Revalidation never
    extends a proof or enables a runtime verb. The worker must hand off within
    one second of starting this request, without caching an approval.
    """
    _, target, _, _ = context
    config = os.environ if runtime.env is None else runtime.env
    try:
        raw = base64.b64decode(body.body_base64, validate=True)
        if len(raw) > MAX_REQUEST_BYTES or body.action not in LIVE_CONTROL_ACTIONS:
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("command_id") != body.command_id:
            raise ValueError
        registration = await run_in_threadpool(runtime.store._read, f"TENANT#{target.tenant_id}", f"REG#{target.invocation_id}#{target.attempt}")
        generation = int(registration["generation"]["N"])
        key_id = config.get(SIGNING_KEY_ID_ENV)
        if not key_id:
            raise EnvelopeError("envelope key id is not configured")
        proof = verify_envelope(
            body.envelope,
            public_keys={key_id: _signing_key(config).public_key()},
            expected_run_id=target.invocation_id,
            expected_generation=generation,
            expected_action=body.action.value,
            expected_command_id=body.command_id,
            request_body=raw,
        )
        if proof.tenant_id != target.tenant_id:
            raise ValueError
        if proof.authority_kind == "human_session":
            from src.agentauth.human_control import require_live_human_membership, require_protected_human_owner
            from src.shared.database import get_session_factory

            # The signature attests a JWT session valid through proof.exp. Do
            # not cache that decision: ownership and membership may have changed
            # since acceptance, and the target worker cannot assert either.
            await run_in_threadpool(
                require_protected_human_owner,
                runtime.store,
                user_id=proof.principal,
                tenant_id=proof.tenant_id,
                run_id=target.invocation_id,
                generation=generation,
                now=datetime.now(UTC),
            )
            async with get_session_factory()() as session:
                await require_live_human_membership(session, user_id=proof.principal, tenant_id=proof.tenant_id)
            runtime.dispatcher.policy.require_supported(body.action)
            # Membership reads must not extend the signed session's lifetime.
            if datetime.now(UTC) >= proof.expires_at:
                raise ValueError
            if body.action is AgentAction.ABORT:
                return await _accept_abort(runtime, body, target=target, generation=generation, raw=raw)
            return {"allowed": True, "command_id": body.command_id, "generation": generation, "max_round_trip_ms": 1000}
        invocation, attempt = proof.principal.rsplit("#", 1)
        caller_record = await run_in_threadpool(runtime.store.authority.load_execution, invocation_id=invocation, tenant_id=target.tenant_id)
        if caller_record is None or caller_record.current_attempt != int(attempt):
            raise ValueError
        grant = await run_in_threadpool(
            runtime.store.live_grant, invocation_id=invocation, tenant_id=target.tenant_id, attempt=int(attempt), now=datetime.now(UTC)
        )
        if (
            grant.grant_id != proof.grant_id
            or grant.revocation_epoch != proof.revocation_epoch
            or grant.flow_id != proof.flow_id
            or grant.authority.reference_id != proof.authority_reference_id
        ):
            raise ValueError
        await runtime.validate_flow(caller_record, grant)
        # This credential is used only inside the gateway to run the existing
        # policy against protected current state; it is never returned to a pod.
        credential = issue_bound_credential(caller_record, now=datetime.now(UTC), env=runtime.env)["credential"]
        authorized = await run_in_threadpool(
            runtime.dispatcher.policy.authorize,
            credential_token=credential,
            action=body.action,
            target_run_id=target.invocation_id,
            presented_workload_binding=caller_record.workload_binding,
        )
        if authorized.grant != grant or authorized.target.generation != generation:
            raise ValueError
        runtime.dispatcher.policy.require_supported(body.action)
        if body.action is AgentAction.ABORT:
            return await _accept_abort(runtime, body, target=target, generation=generation, raw=raw)
        return {"allowed": True, "command_id": body.command_id, "generation": generation, "max_round_trip_ms": 1000}
    except AbortIntentConflictError:
        # Durable intent could not be established for this attempt. Refuse rather
        # than approve: an accepted abort that nothing records is the exact state
        # in which a run reports a deliberate stop and is then redelivered and run
        # again. The operator can reissue; a silently unenforceable abort cannot be
        # recovered from.
        raise BootstrapRefusedError("abort intent could not be persisted") from None
    except (EnvelopeError, ValueError, KeyError, TypeError):
        raise BootstrapRefusedError("queued command is no longer authorized") from None
