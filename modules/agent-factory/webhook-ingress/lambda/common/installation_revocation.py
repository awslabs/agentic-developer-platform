"""Admission for installation routing after durable local revocation.

The gateway owns denial. A permanent DDB marker also blocks delayed writes, but
its absence is not proof: publishing it can fail after SQL has committed. Thus
canonical unavailability denies installation authority, including cached hits.
"""

import logging

logger = logging.getLogger(__name__)


def admit_installation(
    table, installation_id: int, *, org_id: str | None = None
) -> tuple[dict | None, str]:
    from common.gateway_client import resolve_installation_by_id

    try:
        marker = table.get_item(
            Key={
                "identity_type": "github_installation_revoked",
                "identity_value": str(installation_id),
            },
            ConsistentRead=True,
        ).get("Item")
        if marker:
            return None, "installation_revoked"
        canonical = resolve_installation_by_id(str(installation_id)) or {}
        if canonical.get("state") == "revoked":
            return None, "installation_revoked"
        if canonical.get("state") != "resolved" or not canonical.get("tenant_id"):
            return None, "installation_unavailable" if canonical.get(
                "state"
            ) == "error" else "unknown_installation"
        if canonical.get("revocation_checked") is not True:
            return None, "installation_unavailable"
        if org_id is not None and canonical["tenant_id"] != org_id:
            return None, "installation_owner_mismatch"
        return canonical, "ok"
    except Exception:
        logger.exception(
            "Cannot establish installation revocation state: installation=%s",
            installation_id,
        )
        return None, "installation_unavailable"


def put_active_installation(
    table, installation_id: int, item: dict, *, condition: str | None = None
) -> None:
    """Keep a marker published after admission from racing a routing write."""
    operation = {"TableName": table.name, "Item": item}
    if condition:
        operation["ConditionExpression"] = condition
    # DynamoDB resource clients carry attribute conversion handlers. Use the
    # resource's native representation for the transaction, just as table.put_item.
    table.meta.client.transact_write_items(
        TransactItems=[
            {
                "ConditionCheck": {
                    "TableName": table.name,
                    "Key": {
                        "identity_type": "github_installation_revoked",
                        "identity_value": str(installation_id),
                    },
                    "ConditionExpression": "attribute_not_exists(identity_type)",
                }
            },
            {"Put": operation},
        ]
    )
