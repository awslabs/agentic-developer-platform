"""Credential-owner proof for one initial planning retry on the original admission."""

import hashlib
import json
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.agentauth.grants import AUTHORITY_PAID_DOMAIN_OPERATION
from src.internal.domain_current_identity import revalidate_original_humans
from src.internal.domain_operation_approval import current_approval
from src.internal.domain_operation_dispatch import canonical, paid_operation
from src.internal.domain_operation_runtime import metadata
from src.internal.domain_operation_store import domain_connect, harness, operation_connect
from src.internal.domain_provider_session import sealed_target
from src.shared.domain_provider_contract import reserved
from src.shared.models.audit import AuditLog

REFUSED = "original planning credential recovery refused"


def require(value):
    if not value:
        raise HTTPException(409, REFUSED)


async def expired_execution_proof(store, binding, operation, lease, *, source_principal=None):
    """Consistent reads of every possible original execution mapping, not a scan.

    Dispatch allocates these keys under the same operation row lock held by the
    caller. Grants remain expired after fresh human authorization; they cannot be
    repurposed to act in the new attempt. A missing/inconsistent source refuses.
    """
    identity = {key: operation[key] for key in ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id")}
    identity.update(domain=binding.domain, domain_org_id=binding.org_id, adp_org_id=binding.adp_org_id, mode="execution")
    require(1 <= lease["fence_token"] <= 32)
    checked, source = [], False
    source_principal = source_principal or lease["holder"]
    now = datetime.now(UTC)
    for generation in range(lease["fence_token"] + 1):
        for retry in range(5):
            digest = hashlib.sha256(canonical({**identity, "generation": generation, "bootstrap_retry": retry}).encode()).hexdigest()
            raw = await run_in_threadpool(store._read, "TENANT#" + binding.adp_org_id, "DOMAIN_OPERATION#" + digest)
            if raw is None:
                continue
            value = json.loads(raw["mapping"]["S"])
            expiry = datetime.fromisoformat(value["not_after"].replace("Z", "+00:00"))
            require(
                value["identity"] == identity
                and value["plan_digest"] == operation["plan_digest"]
                and value["approval_id"] == operation["approval_id"]
                and expiry.tzinfo is not None
                and expiry <= now
            )
            invocation = value["envelope"]["message_id"]
            record = await run_in_threadpool(store.authority.load_execution, invocation_id=invocation, tenant_id=binding.adp_org_id)
            grant = await run_in_threadpool(store.authority.load_grant, principal=invocation + "#1", tenant_id=binding.adp_org_id)
            if record is None:
                require(grant is None and invocation + "#1" != source_principal)
            else:
                require(
                    record.current_attempt == 1
                    and record.principal == invocation + "#1"
                    and await run_in_threadpool(metadata, store, record) == identity
                )
                require(
                    grant is not None
                    and grant.principal == record.principal
                    and grant.tenant_id == binding.adp_org_id
                    and grant.authority.kind == AUTHORITY_PAID_DOMAIN_OPERATION
                    and grant.expires_at is not None
                    and grant.expires_at <= now
                    and grant.expires_at <= expiry
                )
                source = source or record.principal == source_principal
            checked.append({"mapping_digest": digest, "principal": invocation + "#1", "expired_at": expiry.isoformat()})
    require(source)
    return checked


async def prove_no_credentials(db, store, binding, operation, lease, call, *, unstarted=False, completion=False, interrupted=False):
    request = harness("identity").decode_payload(operation["request_payload"])
    steps = harness("execution_descriptors").parse_execution_steps(request.parameters.get("execution_steps", ""))
    lifecycle = json.loads(request.parameters.get("lifecycle_request", "{}"))
    require(
        request.action == "provision"
        and "runtime_config_sha256" in request.parameters
        and not request.parameters.get("lifecycle_artifact_id")
        and not request.parameters.get("shared_membership")
        and lifecycle.get("mode") == "existing-account-managed"
        and len(steps) == 1
        and steps[0].step_id == steps[0].operation_kind == "prepare-infrastructure"
        and steps[0].provider == "superplane-lifecycle"
    )
    credential, account = sealed_target(operation, lifecycle.get("region"))
    # Raw vault/role delivery is intentionally ineligible: only the reserved
    # provider broker guarantees a durable issuance audit before returning keys.
    require(reserved(credential[0]))
    async with domain_connect(binding) as domain:
        require(
            await domain.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
                "AND provisioning_operation_id=$3 AND status='Provisioning' AND cluster_id IS NULL)",
                operation["workspace_id"],
                binding.org_id,
                operation["operation_id"],
            )
        )
        require(
            not await domain.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)", operation["operation_id"]
            )
        )
    mappings = await expired_execution_proof(store, binding, operation, lease)
    if unstarted or completion or interrupted:
        from src.internal.domain_planning_recovery import prove_unstarted

        return await prove_unstarted(
            db,
            store,
            binding,
            operation,
            lease,
            call,
            credential_id=credential[0],
            account=account,
            region=lifecycle["region"],
            mappings=mappings,
            completion=completion,
            interrupted=interrupted,
        )
    # All prior grants are now proven expired. The issuing handler commits this
    # audit before returning credentials, and rechecks the grant after commit.
    # An STS call still in flight cannot return credentials after grant expiry.
    issued = await db.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.org_id == binding.adp_org_id,
            AuditLog.event_type.in_(("paid_domain_provider_session_issued", "paid_provider_session_issued")),
            AuditLog.details["operation_id"].as_string() == operation["operation_id"],
        )
        .limit(1)
    )
    require(issued is None)
    return {
        "evidence_kind": "credential-delivery-barrier-v1",
        "credentials_delivered": False,
        "original_authority_expired": True,
        "mappings": mappings,
        "credential_id": credential[0],
        "account_id": account,
        "region": lifecycle["region"],
        "source_key": call["idempotency_key"],
        "checked_at": datetime.now(UTC).isoformat(),
    }


async def recover(
    binding, body, store, db, *, authorize_producer, unstarted=False, completion=False, bootstrap=False, settled=False, interrupted=False
):
    require(binding.current_identity_enforced and sum((unstarted, completion, bootstrap, interrupted)) <= 1)
    require(not settled or bootstrap)
    scope = (
        "unstarted-bootstrap"
        if bootstrap
        else "interrupted-planning"
        if interrupted
        else "saved-plan-completion"
        if completion
        else "unstarted-planning"
        if unstarted
        else "uncredentialed-planning"
    )
    principal = harness("identity").ResolvedPrincipal(body.org_id, body.workspace_id, body.requester, frozenset({"workspace:provision"}))
    runtime_review = []

    async def authorize(operation, paid):
        authorize_producer()
        current = await paid_operation(binding, body.operation_id)
        require(current["budget_state"] == paid["reservation_state"] == "confirmed")
        current.update(
            reauthorization_id=body.reauthorization_id,
            recovery_expected_version=body.expected_version,
            recovery_scope=scope,
        )
        async with domain_connect(binding) as domain:
            deadline = await current_approval(
                domain, current, harness("identity").decode_payload(operation["request_payload"]), pending_recovery=True
            )
        await revalidate_original_humans(current, adp_org_id=binding.adp_org_id)
        if bootstrap:
            settled_review = current.get("bootstrap_settled_review")
            require(bool(settled_review) == settled)
            reviewed = (current.get("bootstrap_runtime_amendment"), deadline, settled_review)
            require(not runtime_review or runtime_review[0] == reviewed)
            runtime_review[:] = [reviewed]
        return deadline

    async def prove(operation, lease, call):
        current = await paid_operation(binding, body.operation_id)
        if bootstrap:
            from src.internal.domain_bootstrap_recovery import prove as prove_bootstrap

            proof = await prove_bootstrap(db, store, binding, current, lease, call, **({"settled": True} if settled else {}))
            require(bool(runtime_review))
            terms, deadline, settled_review = runtime_review[0]
            if settled:
                require(harness("settled_bootstrap").digest(proof["settled_bootstrap"]) == settled_review["closure_sha256"])
                require(proof.get("provenance_assumption") == settled_review.get("provenance_assumption"))
            if terms is not None:
                proof.update(runtime_amendment=terms, runtime_authority_expires_at=deadline.isoformat())
            return proof
        return await prove_no_credentials(
            db, store, binding, current, lease, call, unstarted=unstarted, completion=completion, interrupted=interrupted
        )

    async with operation_connect(binding) as connection:
        receipt = await harness("provider_retry").recover_uncredentialed(
            connection,
            principal=principal,
            operation_id=body.operation_id,
            expected_version=body.expected_version,
            reauthorization_id=body.reauthorization_id,
            authorize=authorize,
            prove_not_started=prove,
            recovery_scope=scope,
            **({"settled_bootstrap": True} if settled else {}),
        )
    return {
        "operation_id": receipt["operation_id"],
        "reauthorization_id": receipt["reauthorization_id"],
        "restored_version": receipt["restored_version"],
        "status": "recovery-recorded",
    }


async def recovered_delivery(binding, envelope, store):
    """Recognize only an expired delivery covered by the immutable retry chain.

    Queue acknowledgement retires a duplicate delivery, not its execution,
    approval or provider intent. A fresh retry, unrelated task or changed grant
    is never covered by this proof.
    """
    if not isinstance(envelope, dict) or "kind" in envelope:
        return False
    identity = envelope.get("domain_operation")
    if not isinstance(identity, dict) or identity.get("mode") not in {"execution", "recovery"}:
        return False
    if (identity.get("domain"), identity.get("org_id"), envelope.get("tenant_id")) != (binding.domain, binding.org_id, binding.adp_org_id):
        return False
    operation_id, invocation = identity.get("operation_id"), envelope.get("message_id")
    if not isinstance(operation_id, str) or not isinstance(invocation, str):
        return False
    async with operation_connect(binding) as connection:
        receipts = await harness("provider_retry").receipts_for(connection, operation_id)
        if not receipts:
            return False
        operation = await paid_operation(binding, operation_id, connection=connection)
        expected = {key: operation[key] for key in ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id")}
        expected.update(domain=binding.domain, domain_org_id=binding.org_id, adp_org_id=binding.adp_org_id, mode=identity["mode"])
        if identity != expected:
            return False
        lease = await connection.fetchrow("SELECT * FROM harness_operation_leases WHERE operation_id=$1", operation_id)
        if lease is None or lease["holder"] == invocation + "#1":
            return False
        # A newer retry must not hide an older delivery's qualifying receipt.
        # Validate every original intent and link before selecting any ancestor;
        # never infer obsolescence merely from age or a smaller fence number.
        await harness("provider_retry").active_call_rows(connection, operation_id, [])
        if any(
            receipt["original_approval_id"] != operation["approval_id"]
            or any(receipt[key] != operation[key] for key in ("org_id", "workspace_id", "plan_digest"))
            or lease["fence_token"] <= receipt["source_fence"]
            for receipt in receipts
        ):
            return False
    for receipt in receipts:
        if await _recovered_receipt_delivery(binding, envelope, store, expected, receipt, lease["fence_token"]):
            return True
    return False


async def _recovered_receipt_delivery(binding, envelope, store, expected, receipt, current_fence):
    """Exact delivery checks after the caller verifies the complete receipt chain."""
    identity, invocation = envelope["domain_operation"], envelope["message_id"]
    proof = json.loads(receipt["proof"])
    if (
        not (
            proof.get("evidence_kind") == "credential-delivery-barrier-v1"
            and proof.get("credentials_delivered") is False
            or proof.get("evidence_kind") == "planning-start-barrier-v1"
            and proof.get("planning_started") is False
            and proof.get("issued_sessions_expired") is True
            and proof.get("owner_evidence_id")
            or receipt["recovery_scope"] == "interrupted-planning"
            and harness("interrupted_plan").valid_proof(proof)
            or proof.get("evidence_kind") == "saved-plan-finalization-v1"
            and receipt["recovery_scope"] == "saved-plan-completion"
            and proof.get("planning_started") is True
            and proof.get("apply_started") is False
            and proof.get("issued_sessions_expired") is True
            and proof.get("owner_evidence_id")
            or receipt["recovery_scope"] == "unstarted-bootstrap"
            and harness("provider_retry").bootstrap_recoverable(proof)
            and proof.get("issued_sessions_expired") is True
            and proof.get("owner_evidence_id")
            or proof.get("evidence_kind") == "partial-apply-state-v1"
            and receipt["recovery_scope"] == "partial-apply-repair"
            and proof.get("apply_started") is True
            and proof.get("issued_sessions_expired") is True
            and proof.get("owner_evidence_id")
        )
        or proof.get("original_authority_expired") is not True
    ):
        return False
    matches = [item for item in proof.get("mappings", []) if item.get("principal") == invocation + "#1"]
    if identity["mode"] == "recovery":
        # Recovery deliveries have their own protected mappings. They cannot
        # execute once the receipt has superseded their exact source fence and
        # their grant has expired. Retire only the old queue delivery; preserve
        # its execution, grant, pod binding and all operation/attempt history.
        # A settled bootstrap also records the intervening recovery claims. Its
        # validated closure proves the complete, stopped fence chain; include
        # only those exact generations, never an inferred range or a successor.
        generations = [receipt["source_fence"]]
        if "settled_bootstrap" in proof:
            closure = harness("settled_bootstrap").validate(proof["settled_bootstrap"])
            if (
                receipt["recovery_scope"] != "unstarted-bootstrap"
                or closure["call"]["fence_token"] != receipt["source_fence"]
                or closure["call"]["idempotency_key"] != receipt["original_key"]
                or any(closure["operation"][key] != expected[key] for key in ("operation_id", "org_id", "workspace_id"))
                or current_fence <= closure["lease"]["fence_token"]
            ):
                return False
            generations.extend(claim["fence_token"] for claim in closure["claims"])
        matches = []
        for generation in generations:
            for retry in range(5):
                digest = hashlib.sha256(canonical({**expected, "generation": generation, "bootstrap_retry": retry}).encode()).hexdigest()
                raw = await run_in_threadpool(store._read, "TENANT#" + binding.adp_org_id, "DOMAIN_OPERATION#" + digest)
                if raw is not None:
                    mapping = json.loads(raw["mapping"]["S"])
                    if mapping.get("envelope") == envelope:
                        matches.append({"mapping_digest": digest, "expired_at": mapping["not_after"]})
    if len(matches) != 1:
        return False
    evidence = matches[0]
    raw = await run_in_threadpool(store._read, "TENANT#" + binding.adp_org_id, "DOMAIN_OPERATION#" + evidence["mapping_digest"])
    if raw is None:
        return False
    mapping = json.loads(raw["mapping"]["S"])
    expiry = datetime.fromisoformat(mapping["not_after"].replace("Z", "+00:00"))
    now = datetime.now(UTC)
    if (
        mapping["envelope"] != envelope
        or mapping["identity"] != expected
        or mapping["plan_digest"] != receipt["plan_digest"]
        or mapping["approval_id"] != receipt["original_approval_id"]
        or expiry.tzinfo is None
        or expiry > now
        or expiry != datetime.fromisoformat(evidence["expired_at"])
    ):
        return False
    record = await run_in_threadpool(store.authority.load_execution, invocation_id=invocation, tenant_id=binding.adp_org_id)
    grant = await run_in_threadpool(store.authority.load_grant, principal=invocation + "#1", tenant_id=binding.adp_org_id)
    if record is None:
        return False
    return bool(
        record.current_attempt == 1
        and record.principal == invocation + "#1"
        and await run_in_threadpool(metadata, store, record) == expected
        and grant is not None
        and grant.principal == record.principal
        and grant.tenant_id == binding.adp_org_id
        and grant.authority.kind == AUTHORITY_PAID_DOMAIN_OPERATION
        and grant.expires_at is not None
        and grant.expires_at <= expiry <= now
    )
