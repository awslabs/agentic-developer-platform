"""Resolve Task hierarchy from live identity records, never worker claims.

Canonical service principals may have several aliases. Every active alias must
resolve to the same tenant/team/department; choosing an arbitrary first alias
would let a Task charge or route through the wrong hierarchy.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from src.shared.models.organization import Department, ServiceAccount, Team
from src.shared.models.persona_models import ServicePrincipalAlias


def _refuse():
    from src.agentauth.model_policy import ModelPolicyError

    return ModelPolicyError("task_identity_hierarchy_unavailable")


async def _cognito_assignment(alias, tenant):
    from src.admin.agent_service import AgentService

    client_id = alias.alias_id.removeprefix("cognito_m2m:")
    if not client_id or ":" in client_id:
        raise _refuse()
    service = AgentService()
    response = await asyncio.to_thread(
        service.dynamodb.Table(service.table_name).get_item,
        Key={"client_id": client_id},
        ConsistentRead=True,
    )
    row = response.get("Item", {})
    if row.get("client_id") != client_id or row.get("org_id") != tenant or row.get("status") != "active":
        raise _refuse()
    if not row.get("team_id") or not row.get("department_id"):
        raise _refuse()
    return row["team_id"], row["department_id"]


async def _registry_assignments(aliases, tenant):
    from src.admin.agent_registry_service import AgentRegistryService

    service = AgentRegistryService()
    names = {alias.alias_id for alias in aliases}
    matched = {name: [] for name in names}
    key = None
    while True:
        page = await service.list_agents(org_id=tenant, page_size=100, last_key=key)
        for row in page.items:
            if row.agent_name in matched:
                matched[row.agent_name].append(row)
        key = page.last_key
        if not key:
            break
    assignments = []
    for name in names:
        rows = matched[name]
        if len(rows) != 1 or rows[0].org_id != tenant or rows[0].status != "active":
            raise _refuse()
        # The index is only discovery. Re-read the base row consistently so a
        # moved/disabled registry entry cannot retain its old hierarchy.
        response = await asyncio.to_thread(
            service.dynamodb.get_item,
            TableName=service.table_name,
            Key={"agent_id": {"S": rows[0].agent_id}},
            ConsistentRead=True,
        )
        item = response.get("Item", {})
        if any(item.get(field, {}).get("S") != expected for field, expected in (("agent_name", name), ("org_id", tenant), ("status", "active"))):
            raise _refuse()
        # IAM registry stores team only; its department comes from SQL below.
        assignments.append((item.get("team_id", {}).get("S"), None))
    return assignments


async def resolve_task_identity_context(db, context):
    """Preserve principal identity while restoring its validated hierarchy.

    Read on every admission and paid operation. A previous successful lookup is
    not a fallback for a missing or changed identity record.
    """
    from src.agentauth.model_policy import ModelPolicyError

    try:
        assignments = []
        if context.account_type == "human":
            assignments.append((context.team_id, None))
        else:
            aliases = list(
                await db.scalars(
                    select(ServicePrincipalAlias)
                    .where(
                        ServicePrincipalAlias.org_id == context.org_id,
                        ServicePrincipalAlias.canonical_service_principal_id == context.canonical_service_principal_id,
                        ServicePrincipalAlias.is_active.is_(True),
                    )
                    .execution_options(populate_existing=True)
                )
            )
            if not aliases:
                raise _refuse()
            registry = []
            for alias in aliases:
                if alias.alias_source == "cognito_m2m":
                    assignments.append(await _cognito_assignment(alias, context.org_id))
                elif alias.alias_source == "agent_registry":
                    registry.append(alias)
                elif alias.alias_source == "sa_registration":
                    row = (
                        await db.execute(
                            select(ServiceAccount.team_id, ServiceAccount.department_id).where(
                                ServiceAccount.org_id == context.org_id,
                                ServiceAccount.id == alias.alias_id.removeprefix("sa_registration:"),
                            )
                        )
                    ).first()
                    if row is None:
                        raise _refuse()
                    assignments.append((row.team_id, row.department_id))
                else:
                    # No authoritative hierarchy adapter exists for these roots.
                    raise _refuse()
            if registry:
                assignments.extend(await _registry_assignments(registry, context.org_id))
        resolved = set()
        for team_id, department_id in assignments:
            if not team_id:
                raise _refuse()
            row = (
                await db.execute(
                    select(Team.id, Department.id)
                    .select_from(Team)
                    .join(Department, (Department.id == Team.department_id) & (Department.org_id == Team.org_id))
                    .where(Team.org_id == context.org_id, Team.id == team_id)
                )
            ).first()
            if row is None:
                raise _refuse()
            current_team, current_department = row
            if department_id is not None and department_id != current_department:
                raise _refuse()
            resolved.add((current_team, current_department))
        if len(resolved) != 1:
            raise _refuse()
        team_id, department_id = resolved.pop()
        return context.model_copy(update={"team_id": team_id, "department_id": department_id})
    except ModelPolicyError:
        raise
    except Exception:
        # Provider/DB failure must not become a tenant-only identity.
        raise _refuse() from None
