"""Observation authority tied to the actual authenticated recovery run.

Recovery holders are fencing claims, not run identities. This type deliberately
does not inherit ExecutionGrant and cannot authorize execution RPCs.
"""

from dataclasses import dataclass

from .identity import OperationRefused, ResolvedPrincipal
from .leases import ExecutionLease, lock_lease


@dataclass(frozen=True)
class RecoveryGrant:
    principal: ResolvedPrincipal
    lease: ExecutionLease

    def __post_init__(self):
        if (
            not isinstance(self.principal, ResolvedPrincipal)
            or "workspace:recover" not in self.principal.permissions
        ):
            raise OperationRefused("Recovery permission required")
        if not isinstance(self.lease, ExecutionLease) or (
            self.principal.org_id,
            self.principal.workspace_id,
        ) != (self.lease.org_id, self.lease.workspace_id):
            raise OperationRefused("Run identity does not match recovery scope")


async def lock_recovery_grant(connection, grant: RecoveryGrant) -> bool:
    """Verify the persisted subject and full claim while holding its live lease."""
    if not isinstance(grant, RecoveryGrant):
        return False
    lease = grant.lease
    if not await lock_lease(connection, lease):
        return False
    return bool(
        await connection.fetchval(
            "SELECT 1 FROM harness_recovery_claim_bindings "
            "WHERE operation_id=$1 AND fence_token=$2 AND org_id=$3 "
            "AND workspace_id=$4 AND holder=$5 AND attempt_id=$6 AND subject=$7",
            lease.operation_id,
            lease.fence_token,
            lease.org_id,
            lease.workspace_id,
            lease.holder,
            lease.attempt_id,
            grant.principal.subject,
        )
    )
