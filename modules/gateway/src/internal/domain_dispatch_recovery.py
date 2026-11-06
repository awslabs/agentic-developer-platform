"""Protected recovery of an abandoned admission that never reached a worker."""

import hashlib

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from src.internal.domain_operation_approval import current_approval
from src.internal.domain_operation_dispatch import canonical, paid_operation
from src.internal.domain_operation_store import domain_connect, harness, operation_connect


async def recover(binding, body, store, *, authorize_producer):
    if not binding.current_identity_enforced:
        raise HTTPException(403, "current human identity enforcement required")
    principal = harness("identity").ResolvedPrincipal(
        body.org_id,
        body.workspace_id,
        body.requester,
        frozenset({"workspace:provision"}),
    )

    async def authorize(operation, paid):
        authorize_producer()
        current = await paid_operation(binding, body.operation_id)
        if current["budget_state"] != "confirmed" or paid["reservation_state"] != "confirmed":
            raise HTTPException(403, "original reservation is not confirmed")
        # Identity and budget remain the original admission's. The new decision is
        # a separately recorded authority to resume that same untouched plan.
        current.update(reauthorization_id=body.reauthorization_id, recovery_expected_version=body.expected_version)
        async with domain_connect(binding) as domain:
            deadline = await current_approval(domain, current, harness("identity").decode_payload(operation["request_payload"]))
        from src.internal.domain_current_identity import revalidate_original_humans

        await revalidate_original_humans(current, adp_org_id=binding.adp_org_id)
        return deadline

    async def prove_absence(operation):
        identity = {key: operation[key] for key in ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id")}
        identity.update(domain=binding.domain, domain_org_id=binding.org_id, adp_org_id=binding.adp_org_id, mode="execution")
        keys = [hashlib.sha256(canonical({**identity, "generation": 0, "bootstrap_retry": retry}).encode()).hexdigest() for retry in range(5)]
        # dispatch() allocates these maps while holding the operation lock we now
        # hold. Every possible first-lease mapping must be absent, not expired.
        for key in keys:
            item = await run_in_threadpool(store._read, "TENANT#" + binding.adp_org_id, "DOMAIN_OPERATION#" + key)
            if item is not None:
                raise HTTPException(409, "durable dispatch evidence prevents recovery")
        authorize_producer()
        return {"undelivered": True, "generation": 0, "consistent_read": True, "mapping_digests": keys}

    async with operation_connect(binding) as connection:
        receipt = await harness("dispatch_recovery").recover_undispatched(
            connection,
            principal=principal,
            operation_id=body.operation_id,
            expected_version=body.expected_version,
            reauthorization_id=body.reauthorization_id,
            authorize=authorize,
            prove_absence=prove_absence,
        )
    return {
        "operation_id": receipt["operation_id"],
        "reauthorization_id": receipt["reauthorization_id"],
        "restored_version": receipt["restored_version"],
        "status": "recovery-recorded",
    }
