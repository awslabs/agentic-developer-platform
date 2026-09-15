"""Online authorization of a queued command by its authenticated target worker."""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import BootstrapRefusedError, issue_bound_credential
from src.agentauth.envelope import SIGNING_KEY_ID_ENV, EnvelopeError, _signing_key, verify_envelope
from src.agentauth.grants import LIVE_CONTROL_ACTIONS, AgentAction


class RevalidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    envelope: str = Field(min_length=1, max_length=8192)
    action: AgentAction
    command_id: str = Field(min_length=1, max_length=128)
    body_base64: str = Field(max_length=10924)


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
        if len(raw) > 8192 or body.action not in LIVE_CONTROL_ACTIONS:
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("command_id") != body.command_id:
            raise ValueError
        registration = await run_in_threadpool(runtime.store._read, f"TENANT#{target.tenant_id}", f"REG#{target.invocation_id}#{target.attempt}")
        generation = int(registration["generation"]["N"])
        proof = verify_envelope(
            body.envelope,
            public_keys={config.get(SIGNING_KEY_ID_ENV, "primary"): _signing_key(config).public_key()},
            expected_run_id=target.invocation_id,
            expected_generation=generation,
            expected_action=body.action.value,
            expected_command_id=body.command_id,
            request_body=raw,
        )
        if proof.tenant_id != target.tenant_id:
            raise ValueError
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
        return {"allowed": True, "command_id": body.command_id, "generation": generation, "max_round_trip_ms": 1000}
    except (EnvelopeError, ValueError, KeyError, TypeError):
        raise BootstrapRefusedError("queued command is no longer authorized") from None
