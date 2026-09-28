"""Request-ID-specific account observations under a real current RecoveryGrant.

The protected API mounts these reads and performs no settlement or handoff write.
The API composer must supply an observation provider scoped to the approved
management account and the two exact SDK reads below. Workers receive facts only.
"""

from copy import deepcopy
import re
from types import SimpleNamespace

from .runtime_config import LifecycleRefused


ACCOUNT_CREATION_READS = frozenset(
    {
        ("sts", "get_caller_identity"),
        ("organizations", "describe_create_account_status"),
    }
)


async def original_creation_call(operation, context, key, *, allow_settled=False):
    from harness_jobs.execution import CallOutcome, CallStage, read_call
    from harness_jobs.execution_plan import step_key
    from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
    from harness_jobs.store import OperationStore
    from .runtime import validate_phase

    if not isinstance(operation.grant, RecoveryGrant):
        raise LifecycleRefused("account observation requires a real recovery grant")
    _, request, _, source, step = await validate_phase(
        operation, context, require_fresh=False
    )
    if (
        request.mode.value != "new-account-managed"
        or source is not None
        or (step.step_id, step.provider, step.operation_kind)
        != ("create-account", "aws-organizations", "create-account")
    ):
        raise LifecycleRefused(
            "account recovery is outside the original creation phase"
        )
    lease = operation.grant.lease
    async with context.connect() as connection, connection.transaction():
        if not await lock_recovery_grant(connection, operation.grant):
            raise LifecycleRefused("account recovery principal or claim expired")
        record = await OperationStore().get(
            connection, operation.grant.principal, lease.operation_id
        )
        call = await read_call(connection, idempotency_key=key)
        if (
            record is None
            or (record.job_id, record.plan_digest, record.request_payload)
            != (operation.job_id, operation.plan_digest, operation.request_payload)
            or key != step_key(record, step)
            or call is None
            or (
                call.operation_id,
                call.org_id,
                call.workspace_id,
                call.job_id,
                call.provider,
                call.operation_kind,
                call.target,
            )
            != (
                lease.operation_id,
                lease.org_id,
                lease.workspace_id,
                operation.job_id,
                step.provider,
                step.operation_kind,
                step.target,
            )
            or not (
                call.stage is CallStage.INTENDED
                and call.outcome is None
                or call.outcome is CallOutcome.SUCCEEDED
                or allow_settled
                and (
                    call.stage in {CallStage.OBSERVED, CallStage.RECONCILED}
                    and call.outcome is CallOutcome.FAILED
                    or call.stage is CallStage.UNRESOLVED
                    and call.outcome is CallOutcome.UNKNOWN
                )
            )
            or call.fence_token >= lease.fence_token
        ):
            raise LifecycleRefused(
                "account recovery does not name the original provider intent"
            )
        keys = await connection.fetch(
            "SELECT idempotency_key FROM harness_provider_call_intent WHERE operation_id=$1",
            lease.operation_id,
        )
        if [item["idempotency_key"] for item in keys] != [key]:
            raise LifecycleRefused("account recovery found an extra provider call")
        audited = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM harness_execution_audit WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3 AND attempt_id=$4 AND fence_token=$5 "
            "AND event='record_intent' AND allowed)",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            call.attempt_id,
            call.fence_token,
        )
        if not audited:
            raise LifecycleRefused("account recovery has no original execution audit")
    return request, call


async def observe_account_creation(operation, context, *, idempotency_key, provider):
    """Use maintained interpretation of one fresh read, with no reconciliation store.

    An accepted request can be observed without an immutable result artifact. An
    absent request ID, changed source, or stale recovery claim never licenses a
    CreateAccount replay. These facts cannot release budget or assert bootstrap.
    """
    from account_provisioning.creation_runner import (
        _decode_reference,
        reconcile_creation,
    )

    request, original = await original_creation_call(
        operation, context, idempotency_key
    )
    stored_account, _, request_id = _decode_reference(original.provider_ref)
    if not isinstance(request_id, str) or not re.fullmatch(
        r"car-[A-Za-z0-9-]{1,100}", request_id
    ):
        raise LifecycleRefused(
            "original account creation has no recoverable request ID"
        )

    async def current():
        latest_request, latest_call = await original_creation_call(
            operation, context, idempotency_key
        )
        if latest_request != request or latest_call != original:
            raise LifecycleRefused(
                "original account creation changed during observation"
            )

    identity = await provider.aws_read("sts", "get_caller_identity")
    await current()
    if identity.get("Account") != request.management_account_id:
        raise LifecycleRefused(
            "account recovery provider is not the approved management account"
        )
    observed = await provider.aws_read(
        "organizations",
        "describe_create_account_status",
        CreateAccountRequestId=request_id,
    )
    await current()
    status = observed.get("CreateAccountStatus", {})
    if (status.get("Id"), status.get("AccountName")) != (
        request_id,
        "adp-" + request.workspace_id,
    ):
        raise LifecycleRefused(
            "account recovery observation names another request or account"
        )

    class StatusRead:
        def describe_create_account_status(self, *, CreateAccountRequestId):
            if CreateAccountRequestId != request_id:
                raise LifecycleRefused(
                    "maintained observer requested another creation ID"
                )
            return deepcopy(observed)

    class Credentials:
        async def management(self, *, operation_id):
            if operation_id != original.operation_id:
                raise LifecycleRefused("maintained observer named another operation")
            await current()
            return SimpleNamespace(organizations=StatusRead())

    # No DurableExecutor mutation methods or ReconciliationStore are supplied.
    # The maintained observer interprets the provider answer but cannot persist it.
    outcome = await reconcile_creation(
        SimpleNamespace(operation_id=original.operation_id),
        Credentials(),
        recorded=original,
        store=None,
    )
    if outcome.account_id is not None and (
        not re.fullmatch(r"[0-9]{12}", outcome.account_id)
        or outcome.account_id == request.management_account_id
        or stored_account is not None
        and outcome.account_id != stored_account
    ):
        raise LifecycleRefused(
            "recovered child account differs from original creation evidence"
        )
    await current()
    return {
        "observation_only": True,
        "original_call_key": idempotency_key,
        "plan_digest": operation.plan_digest,
        "creation_request_id": request_id,
        "creation_status": outcome.status.value,
        "account_id": outcome.account_id,
        "bootstrap_verified": False,
        "release_permitted": False,
    }


async def observe_account_handoff(operation, context, *, idempotency_key, provider):
    """Add the exact child's current root parent only after confirmed creation."""
    request, original = await original_creation_call(
        operation, context, idempotency_key
    )
    facts = await observe_account_creation(
        operation,
        context,
        idempotency_key=idempotency_key,
        provider=provider,
    )
    if facts["creation_status"] != "succeeded":
        return facts
    response = await provider.aws_read(
        "organizations",
        "list_parents",
        ChildId=facts["account_id"],
    )
    latest_request, latest_call = await original_creation_call(
        operation,
        context,
        idempotency_key,
    )
    parents = response.get("Parents", [])
    if (
        latest_request != request
        or latest_call != original
        or response.get("NextToken")
        or len(parents) != 1
        or parents[0].get("Type") != "ROOT"
        or not re.fullmatch(r"r-[a-z0-9]{4,32}", parents[0].get("Id", ""))
    ):
        raise LifecycleRefused("recovered account has no verified original root parent")
    return {**facts, "creation_source_parent_id": parents[0]["Id"]}
