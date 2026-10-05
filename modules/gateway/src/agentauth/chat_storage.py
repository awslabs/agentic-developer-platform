"""Transaction fences shared by delegated non-history storage operations."""

from datetime import UTC, datetime

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.orchestration.chat_data_migration import _owner_fields


def snapshot_condition(row: dict) -> dict:
    fields = sorted(set(row) | set(_owner_fields(("", "", ""))))
    names, values, conditions = {}, {}, []
    for index, field in enumerate(fields):
        name, value = f"#field{index}", f":value{index}"
        names[name] = field
        if field in row:
            conditions.append(f"{name} = {value}")
            values[value] = row[field]
        else:
            conditions.append(f"attribute_not_exists({name})")
    return {
        "ConditionExpression": " AND ".join(conditions),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": _encoded(values),
    }


def authority_checks(authority, launch, now: int) -> list[dict]:
    instant = datetime.fromtimestamp(now, UTC)
    store = authority.store
    grant = store.live_grant(invocation_id=launch.run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
    if grant.grant_id != launch.grant_id or grant.revocation_epoch != launch.grant_epoch:
        raise ChatAuthorizationRefusedError("chat grant changed")
    owner = _owner_fields((launch.tenant_id, launch.team_id, launch.user_id))
    names = {f"#{field}": field for field in owner}
    values = {f":{field}": value for field, value in owner.items()}
    required = {"orgId", "tenantId", "teamId", "ownerUserId"}
    conditions = [
        f"#{field} = :{field}" if field in required else f"(attribute_not_exists(#{field}) OR #{field} = :null OR #{field} = :{field})"
        for field in owner
    ]
    conditions.extend(
        [
            "#status = :active AND #ttl > :now",
            "#lease.run_id = :run AND #lease.sandbox_uid = :pod AND #lease.generation = :generation AND #lease.expires_at > :now",
        ]
    )
    names.update({"#status": "status", "#ttl": "ttl", "#lease": "chatLease"})
    values.update(
        {":null": None, ":active": "active", ":now": now, ":run": launch.run_id, ":pod": launch.sandbox_uid, ":generation": launch.lease_generation}
    )
    return [
        {
            "ConditionCheck": {
                "TableName": authority.context_table.name,
                "Key": _encoded({"PK": f"session#{launch.session_id}", "SK": "header"}),
                "ConditionExpression": " AND ".join(conditions),
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": _encoded(values),
            }
        },
        store._authority_check(grant),
        store._grant_check(grant, instant),
        {
            "ConditionCheck": {
                "TableName": store.table,
                "Key": _encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{launch.run_id}"}),
                "ConditionExpression": (
                    "#status = :active AND workload_binding = :pod AND current_attempt = :attempt "
                    "AND current_credential_epoch = :epoch AND attribute_not_exists(abort_command_id)"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": _encoded(
                    {":active": "active", ":pod": launch.sandbox_uid, ":attempt": launch.attempt, ":epoch": launch.credential_epoch}
                ),
            }
        },
    ]
