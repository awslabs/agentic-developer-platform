"""Retry immutable accounting receipts after their execution lease has closed."""

import asyncio
import json
import logging

from workspace_provisioning.recovery_receipts import project_and_acknowledge

logger = logging.getLogger(__name__)


class RecoverySettlementDelivery:
    """Delivery cannot claim/reopen execution or manufacture a settlement receipt."""

    def __init__(self, operation_connect, domain_connect, ledger):
        self.operation_connect = operation_connect
        self.domain_connect = domain_connect
        self.ledger = ledger
        self.cursor = ""

    async def drain_once(self, *, limit=25):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("settlement batch must contain between 1 and 100 receipts")
        async with self.operation_connect() as connection:
            rows = await connection.fetch(
                "SELECT * FROM harness_recovery_settlements "
                "WHERE delivered_at IS NULL AND receipt_id>$1 "
                "ORDER BY receipt_id LIMIT $2",
                self.cursor,
                limit,
            )
        # Advance even past another domain's receipt or a refused receipt. Wrap
        # after a bounded page so a repaired receiver retries old failures too.
        self.cursor = rows[-1]["receipt_id"] if len(rows) == limit else ""
        delivered = []
        for row in rows:
            try:
                if await self._deliver(row):
                    delivered.append(row["receipt_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                # Database/transport exceptions can contain private connection data.
                logger.warning("Recovery settlement delivery deferred", exc_info=False)
        return tuple(delivered)

    async def _deliver(self, row):
        async with self.domain_connect() as connection:
            owned = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspaces w "
                "JOIN organizations o ON o.id=w.org_id "
                "JOIN operation_budget_reservations b "
                "ON b.workspace_id=w.id::text AND b.org_id=o.id::text "
                "WHERE w.id::text=$1 AND o.id::text=$2 "
                "AND b.job_id=$3 AND b.attempt_id=$4)",
                row["workspace_id"],
                row["org_id"],
                row["job_id"],
                row["attempt_id"],
            )
        if not owned:
            return False
        payload = row["accounting"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        receipt = {
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
        }
        # The existing receiver rechecks the complete immutable payload, original
        # closed claim and reservation under their owners' locks. Commit its
        # deduplicated budget effect before publishing a projection or acknowledging.
        accepted = await self.ledger.deliver_settlement(**receipt, accounting=payload)
        if accepted != row["receipt_id"]:
            raise ValueError("settlement receiver did not acknowledge this receipt")
        await project_and_acknowledge(
            self.operation_connect, self.domain_connect, row, payload
        )
        return True
