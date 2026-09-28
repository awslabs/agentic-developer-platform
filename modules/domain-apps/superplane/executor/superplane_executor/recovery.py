"""Scoped recovery with claim-authorized inventory and atomic settlement receipts.

The shared engine owns claims, retries and call dispositions. Complete inventory is
required before terminal settlement. Ledger delivery is at least once; its immutable
receipt commits with the operation and the owning receiver deduplicates budget effects.
"""

import hashlib
import json

from harness_jobs.execution import BudgetDisposition
from harness_jobs.identity import OperationRefused, ResolvedPrincipal, decode_payload
from harness_jobs.recovery import sweep_scoped_expired_leases

MAX_RECOVERED_OPERATIONS = 25


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


async def journalled_operations(domain_pool, principal, *, candidates):
    """Narrow a bounded page of eligible leases to this domain's scoped journal."""
    if not candidates:
        return frozenset()
    async with domain_pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT DISTINCT operation_id FROM controller_provider_requests "
            "WHERE org_id=$1 AND workspace_id=$2 AND operation_id=ANY($3::text[])",
            principal.org_id,
            principal.workspace_id,
            list(candidates),
        )
    return frozenset(row["operation_id"] for row in rows)


async def authorized_operation(domain_pool, principal, operation_id):
    if operation_id not in await journalled_operations(
        domain_pool, principal, candidates=[operation_id]
    ):
        raise OperationRefused("operation is outside this recovery scope")


class ScopedRecovery:
    def __init__(
        self,
        provider,
        *,
        principal,
        observe=None,
        observe_claim=None,
        finalize=None,
        ledger=None,
        operation_id=None,
    ):
        if (
            not isinstance(principal, ResolvedPrincipal)
            or "workspace:recover" not in principal.permissions
        ):
            raise OperationRefused("authenticated workspace:recover principal required")
        self.provider, self.principal = provider, principal
        self.observe, self.observe_claim = observe, observe_claim
        self.finalize, self.ledger = finalize, ledger
        self.prepared = {}
        if operation_id is not None and (
            not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 255
        ):
            raise OperationRefused("invalid paid recovery operation selector")
        self.operation_id = operation_id

    async def _paid_operation(self):
        """Read only the exact historical paid operation selected by this task."""
        from harness_jobs.leases import read_lease
        from harness_jobs.recovery_grant import RecoveryGrant

        from .authority import VerifiedOperation

        async with self.provider.execution_pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT o.job_id,o.plan_digest,o.request_payload,a.reservation_state,"
                "a.max_resource_units,a.max_runtime_seconds,a.max_cost_micros "
                "FROM harness_operations o JOIN harness_approval_consumption a USING(operation_id) "
                "JOIN harness_operation_leases l USING(operation_id) "
                "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3 "
                "AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id AND a.plan_digest=o.plan_digest "
                "AND a.reservation_state IN ('confirmed','retained','released') "
                "AND l.org_id=o.org_id AND l.workspace_id=o.workspace_id "
                "AND l.holder IS NOT NULL AND l.closed_at IS NULL "
                "AND (l.expires_at<=clock_timestamp() OR l.runtime_deadline<=clock_timestamp())",
                self.operation_id,
                self.principal.org_id,
                self.principal.workspace_id,
            )
            if row is None:
                return None
            lease = await read_lease(connection, operation_id=self.operation_id)
        if lease is None:
            return None
        return VerifiedOperation(RecoveryGrant(self.principal, lease), **dict(row))

    async def _paid_candidate(self):
        """A protected paid task may recover a crash before the first journal write.

        Its original operation ID comes from authenticated task bootstrap, never
        from the generic recovery scanner. Validate the complete controller plan
        and registered target before allowing even an empty-call retry.
        """
        from .plan import Plan

        operation = await self._paid_operation()
        if operation is None:
            return frozenset()
        async with self.provider.domain_pool.acquire() as connection:
            from .deployment_registry import require_deployment_registration

            await require_deployment_registration(connection, operation)
            target = await connection.fetchrow(
                "SELECT w.id::text AS workspace_id,w.org_id::text AS domain_org_id,"
                "w.namespace_name AS namespace,c.id::text AS cluster_id,"
                "c.eks_cluster_arn AS cluster_arn,c.endpoint "
                "FROM workspaces w JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id "
                "WHERE w.id::text=$1 AND w.org_id::text=$2",
                self.principal.workspace_id,
                self.principal.org_id,
            )
        if target is None:
            return frozenset()
        if "runtime_config_sha256" in operation.request.parameters:
            raise OperationRefused("lifecycle operation is not controller recovery")
        Plan.read(operation, dict(target))
        return frozenset({self.operation_id})

    async def _candidates(self, limit):
        if self.operation_id is not None:
            return await self._paid_candidate()
        # Limit eligible leases, not permanent journal history. A durable keyset
        # cursor advances past other domains and repeatedly deferred operations.
        async with self.provider.execution_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO harness_recovery_scan_cursors(org_id,workspace_id,consumer) "
                    "VALUES($1,$2,'superplane-controller') ON CONFLICT DO NOTHING",
                    self.principal.org_id,
                    self.principal.workspace_id,
                )
                after = await connection.fetchval(
                    "SELECT after_operation_id FROM harness_recovery_scan_cursors "
                    "WHERE org_id=$1 AND workspace_id=$2 AND consumer='superplane-controller' FOR UPDATE",
                    self.principal.org_id,
                    self.principal.workspace_id,
                )
                rows = await connection.fetch(
                    "SELECT operation_id FROM harness_operation_leases "
                    "WHERE org_id=$1 AND workspace_id=$2 AND holder IS NOT NULL "
                    "AND closed_at IS NULL AND (expires_at<=clock_timestamp() "
                    "OR runtime_deadline<=clock_timestamp()) AND operation_id>$3 "
                    "ORDER BY operation_id LIMIT $4",
                    self.principal.org_id,
                    self.principal.workspace_id,
                    after,
                    limit,
                )
                await connection.execute(
                    "UPDATE harness_recovery_scan_cursors SET after_operation_id=$3 "
                    "WHERE org_id=$1 AND workspace_id=$2 AND consumer='superplane-controller'",
                    self.principal.org_id,
                    self.principal.workspace_id,
                    rows[-1]["operation_id"] if rows else "",
                )
        return await journalled_operations(
            self.provider.domain_pool,
            self.principal,
            candidates=[row["operation_id"] for row in rows],
        )

    async def _prepare(self, lease, calls):
        if self.finalize is None:
            raise OperationRefused("claim-authorized allocation finalizer unavailable")
        accounting = await self.finalize(lease)
        if (
            not isinstance(accounting, dict)
            or accounting.get("inventory_complete") is not True
        ):
            raise OperationRefused("complete allocation inventory unavailable")
        async with self.provider.execution_pool.acquire() as connection:
            request = await connection.fetchval(
                "SELECT request_payload FROM harness_operations WHERE operation_id=$1",
                lease.operation_id,
            )
        action = decode_payload(request).action
        if action == "teardown" and accounting.get("may_mark_released") is not True:
            raise OperationRefused("teardown still has unresolved resource exposure")
        if (
            accounting.get("release_permitted") is True
            and accounting.get("may_mark_released") is not True
        ):
            raise OperationRefused("release requires complete established absence")
        self.prepared[lease.operation_id, lease.fence_token] = accounting
        return accounting.get("may_mark_released") is True

    async def _settled(self, connection, lease, result):
        if (lease.org_id, lease.workspace_id) != (
            self.principal.org_id,
            self.principal.workspace_id,
        ):
            raise OperationRefused("settlement is outside this recovery scope")
        accounting = self.prepared.get((lease.operation_id, lease.fence_token))
        if accounting is None:
            raise OperationRefused("fresh allocation finalization missing")
        original = await connection.fetchrow(
            "SELECT job_id,attempt_id FROM harness_operations WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
        if original is None:
            raise OperationRefused("original admission unavailable")
        payload = {
            **accounting,
            "version": 1,
            "operation_id": lease.operation_id,
            "job_id": original["job_id"],
            "attempt_id": original["attempt_id"],
            "org_id": lease.org_id,
            "workspace_id": lease.workspace_id,
            "claim": {
                "holder": lease.holder,
                "attempt_id": lease.attempt_id,
                "fence_token": lease.fence_token,
            },
            "action": result.action,
            "call_dispositions": {
                key: value.value for key, value in result.call_dispositions
            },
            "budget": result.budget_disposition,
        }
        # Call absence alone cannot return a reservation with unverified inventory.
        if (
            payload["budget"] == BudgetDisposition.RELEASE.value
            and not payload["release_permitted"]
        ):
            payload["budget"] = BudgetDisposition.RETAIN.value
        digest = payload_digest(payload)
        receipt_id = "recovery:" + digest
        await connection.execute(
            "INSERT INTO harness_recovery_settlements "
            "(receipt_id,operation_id,org_id,workspace_id,job_id,attempt_id,claim_holder,"
            "claim_attempt_id,claim_fence_token,payload_digest,accounting) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)",
            receipt_id,
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            original["job_id"],
            original["attempt_id"],
            lease.holder,
            lease.attempt_id,
            lease.fence_token,
            digest,
            canonical(payload),
        )

    async def deliver_pending(self):
        if self.ledger is None:
            return ()
        async with self.provider.execution_pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT r.* FROM harness_recovery_settlements r "
                "JOIN harness_operations o USING(operation_id) "
                "JOIN harness_operation_leases l USING(operation_id) "
                "WHERE r.org_id=$1 AND r.workspace_id=$2 AND r.delivered_at IS NULL "
                "AND ($4::text IS NULL OR r.operation_id=$4) "
                "AND o.org_id=r.org_id AND o.workspace_id=r.workspace_id "
                "AND o.job_id=r.job_id AND o.attempt_id=r.attempt_id "
                "AND o.state=r.accounting->>'action' AND l.closed_at IS NOT NULL "
                "AND l.closed_holder=r.claim_holder AND l.closed_attempt_id=r.claim_attempt_id "
                "AND l.fence_token=r.claim_fence_token "
                "ORDER BY r.created_at LIMIT $3",
                self.principal.org_id,
                self.principal.workspace_id,
                MAX_RECOVERED_OPERATIONS,
                self.operation_id,
            )
        delivered = []
        for row in rows:
            payload = (
                json.loads(row["accounting"])
                if isinstance(row["accounting"], str)
                else row["accounting"]
            )
            if payload_digest(payload) != row["payload_digest"]:
                raise OperationRefused("settlement receipt digest mismatch")
            await self._project(row, payload)
            receipt = await self.ledger.deliver_settlement(
                **{
                    key: row[key]
                    for key in (
                        "receipt_id",
                        "payload_digest",
                        "operation_id",
                        "job_id",
                        "attempt_id",
                        "org_id",
                        "workspace_id",
                    )
                },
                accounting=payload,
            )
            if receipt != row["receipt_id"]:
                raise OperationRefused(
                    "ledger did not acknowledge exact settlement receipt"
                )
            async with self.provider.execution_pool.acquire() as connection:
                await connection.execute(
                    "UPDATE harness_recovery_settlements SET delivered_at=clock_timestamp() "
                    "WHERE receipt_id=$1 AND payload_digest=$2 AND delivered_at IS NULL",
                    row["receipt_id"],
                    row["payload_digest"],
                )
            delivered.append(row["operation_id"])
        return tuple(delivered)

    async def _project(self, row, payload):
        # The original shared admission names the durable domain organization UUID.
        async with self.provider.domain_pool.acquire() as connection:
            organization = await connection.fetchval(
                "SELECT w.org_id::text FROM workspaces w JOIN organizations o ON o.id=w.org_id "
                "WHERE w.id::text=$1 AND o.id::text=$2",
                row["workspace_id"],
                row["org_id"],
            )
            if organization is None:
                raise OperationRefused("workspace accounting registration unavailable")
            await connection.execute(
                "INSERT INTO controller_execution_accounting(operation_id,org_id,workspace_id,observation) "
                "VALUES($1,$2::text::uuid,$3::text::uuid,$4::json) "
                "ON CONFLICT(operation_id) DO UPDATE SET observation=EXCLUDED.observation",
                row["operation_id"],
                organization,
                row["workspace_id"],
                canonical(
                    {
                        **payload,
                        "source": "scoped_recovery",
                        "receipt_id": row["receipt_id"],
                    }
                ),
            )

    async def run(self, *, limit=MAX_RECOVERED_OPERATIONS):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise OperationRefused("recovery limit must be between 1 and 200")
        await self.deliver_pending()
        candidates = await self._candidates(limit)
        if not candidates:
            return ()
        async with self.provider.execution_pool.acquire() as connection:
            report = await sweep_scoped_expired_leases(
                connection,
                principal=self.principal,
                candidates=candidates,
                observe_call=self.observe,
                observe_claim=self.observe_claim,
                prepare_settlement=self._prepare,
                on_settled=self._settled,
                max_operations=limit,
            )
        await self.deliver_pending()
        return report.results
