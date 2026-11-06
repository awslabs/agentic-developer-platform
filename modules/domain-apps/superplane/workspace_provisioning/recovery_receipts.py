"""Publish accepted accounting only while its original closed claim is current."""

import json

from harness_jobs.identity import OperationRefused


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


async def project_and_acknowledge(operation_connect, domain_connect, row, payload):
    """A delayed sender may acknowledge history, but cannot overwrite a new run.

    Ledger acceptance commits first. Hold the shared operation/claim locks until
    the domain projection commits and acknowledgement follows. Reauthorization
    takes the same operation lock, so it cannot race a stale projection.
    """
    async with operation_connect() as operation, operation.transaction():
        current = await operation.fetchrow(
            "SELECT o.state,l.closed_at,l.closed_holder,l.closed_attempt_id,l.fence_token,"
            "r.claim_holder,r.claim_attempt_id,r.claim_fence_token,r.accounting "
            "FROM harness_operations o JOIN harness_operation_leases l USING(operation_id) "
            "JOIN harness_recovery_settlements r USING(operation_id) "
            "WHERE r.receipt_id=$1 AND r.payload_digest=$2 AND o.operation_id=$3 "
            "AND o.org_id=$4 AND o.workspace_id=$5 AND o.job_id=r.job_id "
            "AND o.attempt_id=r.attempt_id AND r.org_id=o.org_id "
            "AND r.workspace_id=o.workspace_id AND l.org_id=o.org_id "
            "AND l.workspace_id=o.workspace_id FOR UPDATE OF o,l,r",
            row["receipt_id"],
            row["payload_digest"],
            row["operation_id"],
            row["org_id"],
            row["workspace_id"],
        )
        if current is None:
            raise OperationRefused("accepted recovery receipt is unavailable")
        original = current["accounting"]
        if isinstance(original, str):
            original = json.loads(original)
        if canonical(original) != canonical(payload):
            raise OperationRefused("accepted recovery receipt payload changed")
        claim_current = (
            current["closed_at"] is not None
            and current["state"] == payload["action"]
            and current["closed_holder"] == current["claim_holder"]
            and current["closed_attempt_id"] == current["claim_attempt_id"]
            and current["fence_token"] == current["claim_fence_token"]
        )
        if claim_current:
            async with domain_connect() as domain, domain.transaction():
                owned = await domain.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM workspaces w "
                    "JOIN organizations o ON o.id=w.org_id "
                    "WHERE w.id::text=$1 AND o.id::text=$2)",
                    row["workspace_id"],
                    row["org_id"],
                )
                if not owned:
                    raise OperationRefused(
                        "workspace accounting registration unavailable"
                    )
                await domain.execute(
                    "INSERT INTO controller_execution_accounting"
                    "(operation_id,org_id,workspace_id,observation) "
                    "VALUES($1,$2::text::uuid,$3::text::uuid,$4::json) "
                    "ON CONFLICT(operation_id) DO UPDATE SET observation=EXCLUDED.observation",
                    row["operation_id"],
                    row["org_id"],
                    row["workspace_id"],
                    canonical(
                        {
                            **payload,
                            "source": "scoped_recovery",
                            "receipt_id": row["receipt_id"],
                        }
                    ),
                )
        await operation.execute(
            "UPDATE harness_recovery_settlements SET delivered_at=clock_timestamp() "
            "WHERE receipt_id=$1 AND payload_digest=$2 AND delivered_at IS NULL",
            row["receipt_id"],
            row["payload_digest"],
        )
