"""Recover one original accepted account request; never redispatch creation.

Provider facts come only from the protected API. The shared recovery engine owns
call reconciliation and operation settlement. Immutable handoffs name the original
producer; the separate audit names the actual current recovery publisher.
"""

from account_provisioning.creation_runner import _decode_reference, encode_reference
import re
from harness_jobs.execution import CallOutcome, audit, read_call
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery import PendingObservation
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
from superplane_executor.recovery_authority import (
    same_recovery_claim,
    same_recovery_operation,
)

from .account_recovery_observer import original_creation_call
from .artifacts import canonical, digest, proposal


class AccountCreationRecovery:
    def __init__(self, context):
        self.context = context

    async def verified(self, lease, key, *, allow_settled=False):
        operation = await self.context.authority.resolve_recovery(lease)
        if not isinstance(operation.grant, RecoveryGrant) or not same_recovery_claim(
            operation.grant.lease, lease
        ):
            raise OperationRefused("account recovery claim changed")
        request, call = await original_creation_call(
            operation,
            self.context,
            key,
            allow_settled=allow_settled,
        )
        return operation, request, call

    async def facts(self, lease, key):
        operation, request, call = await self.verified(lease, key)
        account, _, request_id = _decode_reference(call.provider_ref)
        if not request_id:
            raise OperationRefused("account creation has no accepted request handle")
        facts = await self.context.authority.account_creation(
            lease,
            key,
            plan_digest=operation.plan_digest,
            request_id=request_id,
        )
        current, approved, original = await self.verified(lease, key)
        if (
            not same_recovery_operation(current, operation)
            or approved != request
            or original != call
            or facts["creation_status"] == "succeeded"
            and (
                facts["account_id"] == request.management_account_id
                or account is not None
                and account != facts["account_id"]
            )
        ):
            raise OperationRefused("original account creation changed during recovery")
        return current, request, call, facts

    async def observe(self, lease, key, provider, kind, target):
        operation, request, call, facts = await self.facts(lease, key)
        if (provider, kind, target) != (
            call.provider,
            call.operation_kind,
            call.target,
        ):
            raise OperationRefused("account recovery descriptor changed")
        status = facts["creation_status"]
        if status == "in-progress":
            return PendingObservation(key, provider, kind, target, call.provider_ref)
        if status == "failed":
            # The original request failed, but account-factory accounting is
            # retained until the allocation's separately governed finalization.
            return (
                CallOutcome.FAILED,
                "original account request failed",
                call.provider_ref,
            )
        await publish_handoff(operation, self.context, request, call, facts)
        return (
            CallOutcome.SUCCEEDED,
            "original account recovered; separately approved bootstrap required",
            encode_reference(
                request_id=facts["creation_request_id"], account_id=facts["account_id"]
            ),
        )

    async def accounting(self, lease, calls):
        if len(calls) != 1:
            raise OperationRefused(
                "account recovery requires exactly its original call"
            )
        operation, request, original = await self.verified(
            lease,
            calls[0].idempotency_key,
            allow_settled=True,
        )
        if original != calls[0]:
            raise OperationRefused("account call changed before accounting")
        result = {
            "allocation_id": operation.request.parameters["allocation_id"],
            "inventory_complete": False,
            "release_permitted": False,
            "may_mark_released": False,
            "resource_dispositions": {},
            "unresolved_resources": [],
            "exposure": "unresolved",
            "reason": "account creation observed; allocation remains reserved",
        }
        if original.outcome is CallOutcome.SUCCEEDED:
            current, approved, call, facts = await self.facts(
                lease, original.idempotency_key
            )
            if facts["creation_status"] != "succeeded" or approved != request:
                raise OperationRefused("account success is not freshly confirmed")
            row = await publish_handoff(current, self.context, approved, call, facts)
            result["lifecycle_proposal"] = proposal(row)
        return result


async def publish_handoff(operation, context, request, call, facts):
    """Write idempotent immutable original-producer output under recovery authority.

    No historical ExecutionGrant is created. The original call stays authoritative
    and a continuation cannot consume this row until shared recovery has confirmed
    that call and settled its original operation. A crash between databases may
    leave an unconsumable row, whose identical values a later claim can verify.
    """
    lease = operation.grant.lease
    if (
        not isinstance(operation.grant, RecoveryGrant)
        or facts["creation_status"] != "succeeded"
    ):
        raise OperationRefused(
            "account handoff needs current recovery and confirmed success"
        )
    approved, original = await original_creation_call(
        operation, context, call.idempotency_key
    )
    account, _, request_id = _decode_reference(original.provider_ref)
    if (
        approved != request
        or original != call
        or facts.get("observation_only") is not True
        or facts.get("original_call_key") != call.idempotency_key
        or facts.get("plan_digest") != operation.plan_digest
        or not request_id
        or facts.get("creation_request_id") != request_id
        or not isinstance(facts.get("account_id"), str)
        or not re.fullmatch(r"[0-9]{12}", facts["account_id"])
        or facts["account_id"] == request.management_account_id
        or account is not None
        and account != facts["account_id"]
        or not isinstance(facts.get("creation_source_parent_id"), str)
        or not re.fullmatch(r"r-[a-z0-9]{4,32}", facts["creation_source_parent_id"])
        or facts.get("release_permitted") is not False
        or facts.get("bootstrap_verified") is not False
    ):
        raise OperationRefused("recovered handoff differs from original account facts")
    reference = encode_reference(
        request_id=facts["creation_request_id"],
        account_id=facts["account_id"],
    )
    parameters = dict(operation.request.parameters)
    async with context.connect() as execution, execution.transaction():
        if not await lock_recovery_grant(execution, operation.grant):
            raise OperationRefused("account handoff recovery authority expired")
        current = await read_call(execution, idempotency_key=call.idempotency_key)
        source = await execution.fetchrow(
            "SELECT job_id,attempt_id,plan_digest,request_payload FROM harness_operations "
            "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
        if (
            current != call
            or source is None
            or (source["job_id"], source["plan_digest"], source["request_payload"])
            != (operation.job_id, operation.plan_digest, operation.request_payload)
        ):
            raise OperationRefused("account original admission or call changed")
        producers = await execution.fetch(
            "SELECT DISTINCT actor FROM harness_execution_audit WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3 AND attempt_id=$4 AND fence_token=$5 "
            "AND event='record_intent' AND allowed",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            call.attempt_id,
            call.fence_token,
        )
        if len(producers) != 1:
            raise OperationRefused("account handoff has no unique original producer")
        values = {
            "org_id": lease.org_id,
            "workspace_id": lease.workspace_id,
            "source_operation_id": lease.operation_id,
            "source_job_id": source["job_id"],
            "source_attempt_id": source["attempt_id"],
            "source_payload_digest": source["plan_digest"],
            "source_request_payload": source["request_payload"],
            "producer_holder": producers[0]["actor"],
            "producer_attempt_id": call.attempt_id,
            "producer_fence_token": call.fence_token,
            "request_revision": parameters["plan_revision"],
            "account_id": facts["account_id"],
            "parameters_json": canonical(parameters),
            "target_json": canonical(
                {
                    "account_id": facts["account_id"],
                    "aws_region": request.region,
                    "organizational_unit_id": request.organizational_unit_id,
                }
            ),
            "artifact_metadata_json": canonical(
                {
                    "next_phase": "bootstrap-account",
                    "creation_request_id": facts["creation_request_id"],
                    "creation_source_parent_id": facts["creation_source_parent_id"],
                    "creation_call": {
                        "idempotency_key": call.idempotency_key,
                        "provider": call.provider,
                        "operation_kind": call.operation_kind,
                        "target": call.target,
                        "provider_ref": reference,
                    },
                }
            ),
        }
        artifact_id = digest(values)
        columns = tuple(values)
        async with context.domain_connect() as domain, domain.transaction():
            rows = await domain.fetch(
                "SELECT artifact_id FROM workspace_lifecycle_artifacts "
                "WHERE org_id=$1 AND workspace_id=$2 AND source_operation_id=$3",
                lease.org_id,
                lease.workspace_id,
                lease.operation_id,
            )
            if rows and [row["artifact_id"] for row in rows] != [artifact_id]:
                raise OperationRefused(
                    "account handoff differs from existing immutable result"
                )
            await domain.execute(
                "INSERT INTO workspace_lifecycle_artifacts (artifact_id,"
                + ",".join(columns)
                + ") VALUES ($1,"
                + ",".join("$" + str(i) for i in range(2, len(columns) + 2))
                + ") ON CONFLICT (artifact_id) DO NOTHING",
                artifact_id,
                *values.values(),
            )
            row = await domain.fetchrow(
                "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1",
                artifact_id,
            )
            if row is None or any(row[key] != value for key, value in values.items()):
                raise OperationRefused("immutable recovered account handoff changed")
            if not await lock_recovery_grant(execution, operation.grant):
                raise OperationRefused("account recovery expired before publication")
        await audit(
            execution,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            actor=operation.grant.principal.subject,
            event="account.creation_handoff",
            allowed=True,
            attempt_id=lease.attempt_id,
            fence_token=lease.fence_token,
            detail=artifact_id,
        )
    return dict(row)
