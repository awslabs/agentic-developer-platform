"""Recover original unstarted tasks and completed immutable plan proposals.

The exact task bootstrap and current recovery principal select the original paid
operation. A lifecycle workspace may not have a cluster yet. Its approved phase
and immutable proposal are validated without granting execution credentials.
Interrupted provider phases require phase-specific observation before settlement;
they cannot be replayed merely because a worker acquired a replacement claim.
"""

from harness_jobs.identity import OperationRefused
from superplane_executor.recovery import ScopedRecovery
from dataclasses import replace

from .recovery_proposals import PROPOSAL_PHASES, ProposalRecovery
from .recovery_applied import APPLIED_PHASES, AppliedRecovery
from .recovery_account_creation import AccountCreationRecovery


class LifecycleRecovery(ScopedRecovery):
    def __init__(self, provider, *, principal, operation_id, context, ledger=None):
        if not operation_id:
            raise OperationRefused("lifecycle recovery requires the original paid task")
        super().__init__(
            provider, principal=principal, operation_id=operation_id, ledger=ledger
        )
        self.context = context
        self.proposals = ProposalRecovery(context)
        self.observe_claim = self.proposals.observe

    async def _paid_candidate(self):
        from .runtime import validate_phase

        operation = await self._paid_operation()
        if operation is None:
            return frozenset()
        if "runtime_config_sha256" not in operation.request.parameters:
            raise OperationRefused("controller operation is not lifecycle recovery")
        async with self.provider.domain_pool.acquire() as connection:
            registered = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
                "AND provisioning_operation_id=$3)",
                self.principal.workspace_id,
                self.principal.org_id,
                self.operation_id,
            )
        if not registered or operation.request.action != "provision":
            raise OperationRefused("original lifecycle registration is unavailable")
        _, _, _, _, step = await validate_phase(
            operation, self.context, require_fresh=False
        )
        if step.step_id in APPLIED_PHASES:
            self.proposals = AppliedRecovery(self.context)
            self.observe_claim = self.proposals.observe
        elif step.step_id == "create-account":
            self.proposals = AccountCreationRecovery(self.context)
            self.observe_claim = self.proposals.observe
        async with self.provider.execution_pool.acquire() as connection:
            started = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM harness_provider_call_intent WHERE operation_id=$1)",
                self.operation_id,
            )
        if started and step.step_id != "create-account":
            async with self.provider.domain_pool.acquire() as connection:
                proposed = (
                    await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE org_id=$1 "
                        "AND workspace_id=$2 AND source_operation_id=$3)",
                        self.principal.org_id,
                        self.principal.workspace_id,
                        self.operation_id,
                    )
                    if step.step_id in PROPOSAL_PHASES | APPLIED_PHASES
                    else False
                )
            if not proposed:
                raise OperationRefused(
                    "lifecycle provider phase requires fresh recovery observations; reservation retained"
                )
        return frozenset({self.operation_id})

    async def _prepare(self, lease, calls):
        self.prepared[
            lease.operation_id, lease.fence_token
        ] = await self.proposals.accounting(lease, calls)
        # Proposal completion is not evidence of absence or allocation release.
        return False

    async def _settled(self, connection, lease, result):
        from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant

        if not await lock_recovery_grant(
            connection, RecoveryGrant(self.principal, lease)
        ):
            raise OperationRefused("lifecycle settlement recovery authority expired")
        subject = await connection.fetchval(
            "SELECT subject FROM harness_recovery_claim_bindings WHERE operation_id=$1 "
            "AND fence_token=$2 AND org_id=$3 AND workspace_id=$4 AND holder=$5 "
            "AND attempt_id=$6 FOR SHARE",
            lease.operation_id,
            lease.fence_token,
            lease.org_id,
            lease.workspace_id,
            lease.holder,
            lease.attempt_id,
        )
        if subject != self.principal.subject:
            raise OperationRefused("lifecycle settlement recovery subject was revoked")
        await super()._settled(
            connection, lease, replace(result, budget_disposition="retain")
        )


async def recover_lifecycle(authority, provider, *, operation_id, context):
    principal = await authority.recovery_scope()
    return await LifecycleRecovery(
        provider,
        principal=principal,
        operation_id=operation_id,
        context=context,
        ledger=authority,
    ).run(limit=1)
