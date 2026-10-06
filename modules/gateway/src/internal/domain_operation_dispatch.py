"""Publish only a durable paid admission into a protected deterministic worker task."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import BootstrapRefusedError, _iso, _key
from src.agentauth.grants import AUTHORITY_PAID_DOMAIN_OPERATION, AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.internal.domain_operation_store import aws_client, harness, operation_connect


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


async def paid_operation(binding, operation_id, *, connection=None, require_current=False):
    if connection is None:
        async with operation_connect(binding) as current:
            return await paid_operation(binding, operation_id, connection=current, require_current=require_current)
    row = await connection.fetchrow(
        "SELECT o.*,a.approval_id,a.requester,a.approved_by,a.reservation_state,a.max_resource_units,"
        "a.max_runtime_seconds,a.max_cost_micros,r.state AS budget_state FROM harness_operations o "
        "JOIN harness_approval_consumption a USING(operation_id) "
        "JOIN operation_budget_reservations r ON r.reservation_id=a.reservation_id "
        "AND r.job_id=o.job_id AND r.attempt_id=o.attempt_id AND r.org_id=o.org_id AND r.workspace_id=o.workspace_id "
        "AND r.max_resource_units=a.max_resource_units AND r.max_runtime_seconds=a.max_runtime_seconds AND r.max_cost_micros=a.max_cost_micros "
        "JOIN organizations d ON d.id::text=o.org_id "
        "WHERE o.operation_id=$1 AND o.org_id=$2 AND d.adp_org_id=$3 "
        "AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id "
        "AND a.plan_digest=o.plan_digest AND a.reservation_state IN ('confirmed','retained','released') "
        "AND r.state IN ('confirmed','retained','released')",
        operation_id,
        binding.org_id,
        binding.adp_org_id,
    )
    if row is None:
        raise HTTPException(403, "paid operation authority refused")
    identity = harness("identity")
    request = identity.decode_payload(row["request_payload"])
    if identity.payload_digest(request) != row["plan_digest"]:
        raise HTTPException(403, "paid operation authority refused")
    operation = dict(row)
    if require_current:
        from src.internal.domain_operation_approval import current_approval

        if operation["budget_state"] != "confirmed" or operation["reservation_state"] != "confirmed":
            raise HTTPException(403, "current domain budget refused")
        operation["approval_expires_at"] = await current_approval(connection, operation, request)
        if binding.current_identity_enforced:
            from src.internal.domain_current_identity import revalidate_original_humans

            await revalidate_original_humans(operation, adp_org_id=binding.adp_org_id)
    return operation


async def dispatch(binding, body, store, *, authorize=None):
    async with operation_connect(binding) as connection:
        async with connection.transaction():
            # Serialize admission withdrawal and two producer retries. Nothing is
            # enqueued until the exact paid record and protected pending map exist.
            await connection.fetchrow(
                "SELECT operation_id FROM harness_operations WHERE operation_id=$1 AND org_id=$2 FOR UPDATE", body.operation_id, binding.org_id
            )
            operation = await paid_operation(binding, body.operation_id, connection=connection, require_current=body.mode == "execution")
            if body.mode == "recovery" and binding.current_identity_enforced:
                from src.internal.domain_current_identity import revalidate_original_humans

                await revalidate_original_humans(operation, adp_org_id=binding.adp_org_id)
            for key in ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id"):
                if operation[key] != getattr(body, key):
                    raise HTTPException(403, "paid operation dispatch refused")
            lease = await connection.fetchrow("SELECT * FROM harness_operation_leases WHERE operation_id=$1", body.operation_id)
            if body.mode == "execution":
                if (
                    operation["state"] not in {"pending", "running"}
                    or operation["cancel_requested_at"]
                    or operation["cleanup_required"]
                    or operation["reservation_state"] != "confirmed"
                ):
                    raise HTTPException(403, "paid operation dispatch refused")
                generation = 0 if lease is None else lease["fence_token"] - int(lease["holder"] is not None)
                if (
                    lease is not None
                    and lease["holder"] is not None
                    and await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM harness_recovery_claim_bindings WHERE operation_id=$1 AND fence_token=$2)",
                        body.operation_id,
                        lease["fence_token"],
                    )
                ):
                    raise HTTPException(403, "recovery owns domain operation")
            else:
                if (
                    lease is None
                    or lease["closed_at"] is not None
                    or lease["holder"] is None
                    or min(lease["expires_at"], lease["runtime_deadline"]) > datetime.now(UTC)
                ):
                    raise HTTPException(403, "eligible recovery operation unavailable")
                generation = lease["fence_token"]
            identity = {key: operation[key] for key in ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id")}
            identity.update(domain=binding.domain, domain_org_id=binding.org_id, adp_org_id=binding.adp_org_id, mode=body.mode)
            pending = await run_in_threadpool(
                provision,
                store,
                binding,
                identity,
                operation,
                generation,
                allow_retry=body.mode == "recovery" or lease is None or lease["holder"] is None,
                must_exist=body.mode == "execution" and lease is not None and lease["holder"] is not None,
            )
            if body.mode == "execution" and lease is not None and lease["holder"] is not None:
                if pending["envelope"]["message_id"] + "#1" != lease["holder"]:
                    raise HTTPException(403, "paid execution mapping differs from current lease")
        # The commit can wait; re-read authoritative admission before publication.
        current = await paid_operation(binding, body.operation_id, connection=connection, require_current=body.mode == "execution")
        if body.mode == "execution" and (
            current["cancel_requested_at"] or current["cleanup_required"] or current["state"] not in {"pending", "running"}
        ):
            raise HTTPException(403, "paid operation dispatch withdrawn")
    envelope = pending["envelope"]
    if authorize is not None:
        authorize()
    try:
        # An ambiguous send is retried with the identical protected invocation.
        # TaskDelivery + BootstrapStore permit exactly one bound pod for it.
        await run_in_threadpool(aws_client("sqs").send_message, QueueUrl=binding.queue_url, MessageBody=canonical(envelope))
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "paid operation task publication unavailable") from None
    return {
        "version": 1,
        **identity,
        "invocation_id": envelope["message_id"],
        "principal": envelope["message_id"] + "#1",
        "status": "pending",
        "not_after": pending["not_after"],
    }


def provision(store, binding, identity, operation, generation, *, allow_retry=False, retry=0, must_exist=False):
    """Persist original-ID mapping before creating pending grant/queue envelope."""
    digest = hashlib.sha256(canonical({**identity, "generation": generation, "bootstrap_retry": retry}).encode()).hexdigest()
    key = _key("TENANT#" + binding.adp_org_id, "DOMAIN_OPERATION#" + digest)
    if must_exist and store._read(key["pk"]["S"], key["sk"]["S"]) is None:
        raise BootstrapRefusedError("paid execution mapping unavailable")
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=min(operation["max_runtime_seconds"], 3600))
    if operation.get("approval_expires_at"):
        expires = min(expires, operation["approval_expires_at"])
    invocation = "domain-" + uuid4().hex
    envelope = {
        "message_id": invocation,
        "tenant_id": binding.adp_org_id,
        "arrived_at": _iso(now),
        "persona": "superplane-operation",
        "source_ref": {"repo": binding.repo},
        "domain_operation": identity,
    }
    value = {
        "identity": identity,
        "envelope": envelope,
        "not_after": _iso(expires),
        "plan_digest": operation["plan_digest"],
        "approval_id": operation["approval_id"],
    }
    item = {**key, "mapping": {"S": canonical(value)}}
    try:
        store.client.put_item(TableName=store.table, Item=item, ConditionExpression="attribute_not_exists(pk)")
    except (ClientError, BotoCoreError):
        stored = store._read(key["pk"]["S"], key["sk"]["S"])
        if not stored:
            raise BootstrapRefusedError("domain dispatch mapping unavailable") from None
        value = json.loads(stored["mapping"]["S"])
        if value["identity"] != identity or value["plan_digest"] != operation["plan_digest"] or value["approval_id"] != operation["approval_id"]:
            raise BootstrapRefusedError("domain dispatch mapping changed") from None
        envelope = value["envelope"]
        invocation = envelope["message_id"]
        expires = datetime.fromisoformat(value["not_after"].replace("Z", "+00:00"))
    if expires <= now:
        if retry < 4:
            # Retain each expired bootstrap mapping and select at most four
            # successors. Recovery can do this only while the shared claim is
            # expired; an active recovery holder was refused before this function.
            # Execution with a held lease cannot create a replacement mapping.
            return provision(store, binding, identity, operation, generation, allow_retry=allow_retry, retry=retry + 1, must_exist=not allow_retry)
        raise BootstrapRefusedError("domain dispatch expired")
    reference = "paid-domain:" + digest
    authority = {
        **_key("TENANT#" + binding.adp_org_id, "AUTHORITY#" + reference),
        "authority_kind": {"S": AUTHORITY_PAID_DOMAIN_OPERATION},
        "human_id": {"S": operation["approved_by"]},
        "actor_kind": {"S": "human"},
        "status": {"S": "active"},
        "expires_at": {"S": _iso(expires)},
    }
    try:
        store.client.put_item(TableName=store.table, Item=authority, ConditionExpression="attribute_not_exists(pk)")
    except (ClientError, BotoCoreError):
        if store._read("TENANT#" + binding.adp_org_id, "AUTHORITY#" + reference) != authority:
            raise BootstrapRefusedError("paid domain authority changed") from None
    grant = DelegatedGrant(
        grant_id="paid-domain-" + invocation,
        tenant_id=binding.adp_org_id,
        principal=invocation + "#1",
        authority=AuthorityReference(AUTHORITY_PAID_DOMAIN_OPERATION, reference, operation["approved_by"], binding.adp_org_id),
        allowed_actions=frozenset({AgentAction.MONITOR}),
        target_relationships=frozenset({TargetRelationship.SELF}),
        repo_scope=frozenset({binding.repo}),
        expires_at=expires,
    )
    store.provision_pending(envelope=envelope, grant=grant, now=now, execution_metadata={"domain_operation": {"S": canonical(identity)}})
    return value
