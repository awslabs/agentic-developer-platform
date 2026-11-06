"""Join installer start-barrier evidence to expired credential-owner facts."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.internal.domain_operation_store import aws_client
from src.internal.domain_provider_authority import read as read_authority
from src.internal.domain_provider_authority import tables
from src.shared.domain_planning_recovery import IDENTITY, record_id, validate
from src.shared.domain_provider_contract import digest
from src.shared.models.audit import AuditLog


def read_evidence(identity, *, completion=False, interrupted=False):
    table, _ = tables()
    key = record_id(identity)
    item = aws_client("dynamodb").get_item(TableName=table, Key={"record_id": {"S": key}}, ConsistentRead=True).get("Item")
    if item is None or set(item) != {"record_id", "document", "revision"}:
        raise ValueError("protected planning evidence unavailable")
    value = validate(json.loads(item["document"]["S"]), completion=completion, interrupted=interrupted)
    if item["revision"]["S"] != digest(value) or value["record_id"] != key or any(value[k] != identity[k] for k in IDENTITY):
        raise ValueError("protected planning evidence differs")
    return value


async def prove_unstarted(
    db, store, binding, operation, lease, call, *, credential_id, account, region, mappings, completion=False, interrupted=False
):
    from src.internal.domain_provider_recovery import require

    identity = {key: operation[key] for key in ("org_id", "workspace_id", "operation_id", "plan_digest")}
    identity.update(source_key=call["idempotency_key"], source_fence=lease["fence_token"])
    evidence = await run_in_threadpool(read_evidence, identity, completion=completion, interrupted=interrupted)
    authority = await run_in_threadpool(read_authority, credential_id)
    now = datetime.now(UTC)
    checked = datetime.fromisoformat(evidence["checked_at"])
    require(
        evidence["credential_id"] == credential_id
        and evidence["adp_org_id"] == binding.adp_org_id
        and evidence["installation_id"] == authority["installation_id"]
        and evidence["account_id"] == authority["account_id"] == account
        and evidence["region"] == authority["region"] == region
        and authority["org_id"] == operation["org_id"]
        and authority["workspace_id"] == operation["workspace_id"]
        and authority["subject"] == operation["requester"]
        and evidence["source_principal"] == lease["holder"] == call["attempt_id"]
        and datetime.fromisoformat(evidence["source_acquired_at"]) == lease["acquired_at"]
        and checked >= lease["runtime_deadline"] + timedelta(seconds=900)
        and all(checked >= datetime.fromisoformat(m["expired_at"]) + timedelta(seconds=900) for m in mappings)
    )
    invocation, attempt = lease["holder"].rsplit("#", 1)
    record = await run_in_threadpool(store.authority.load_execution, invocation_id=invocation, tenant_id=binding.adp_org_id)
    require(record is not None and attempt == "1" and record.workload_binding == evidence["workload_binding"])
    raw = await run_in_threadpool(store._read, "TENANT#" + binding.adp_org_id, "EXEC#" + invocation)
    require(raw is not None and raw.get("pod_name", {}).get("S") == evidence["source_pod_name"])
    rows = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.org_id == binding.adp_org_id,
                    AuditLog.event_type.in_(("paid_domain_provider_session_issued", "paid_provider_session_issued")),
                    AuditLog.details["operation_id"].as_string() == operation["operation_id"],
                )
            )
        )
        .scalars()
        .all()
    )
    # Preserve issuance. An issuance is eligible only after its authentic expiry;
    # missing/malformed expiry or another credential owner refuses. The extra
    # 900-second barrier also covers STS calls that never reached audit commit.
    for row in rows:
        expiry = datetime.fromisoformat(row.details["expires_at"])
        require(
            row.event_type == "paid_domain_provider_session_issued"
            and row.details["credential_id"] == credential_id
            and expiry.tzinfo is not None
            and expiry <= checked <= now
        )
    require(await run_in_threadpool(read_evidence, identity, completion=completion, interrupted=interrupted) == evidence)
    require(await run_in_threadpool(read_authority, credential_id) == authority)
    return {
        "evidence_kind": "interrupted-read-only-plan-v1"
        if interrupted
        else "saved-plan-finalization-v1"
        if completion
        else "planning-start-barrier-v1",
        "planning_started": completion or interrupted,
        **({"apply_started": False} if completion or interrupted else {}),
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "owner_evidence_id": evidence["record_id"],
        "owner_evidence": evidence,
        "issuance_audit_ids": sorted(str(row.id) for row in rows),
        "mappings": mappings,
        "credential_id": credential_id,
        "account_id": account,
        "region": region,
        "source_key": call["idempotency_key"],
        **({"attempt_extension": evidence["attempt_extension"]} if "attempt_extension" in evidence else {}),
        **({"preparation_boundary": evidence["preparation_boundary"]} if "preparation_boundary" in evidence else {}),
        "checked_at": now.isoformat(),
    }
