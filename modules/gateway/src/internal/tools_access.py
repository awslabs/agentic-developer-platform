"""Read-only effective tool access for a verified tenant user."""

import os
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.agentauth.task_tool_policy import valid_tools
from src.internal.tools_connection import connection_scopes
from src.internal.tools_platform import implicitly_allowed, is_platform_tool
from src.shared.config import get_settings
from src.shared.identity.workspaces import primary_team_for_workspace
from src.shared.models.tool_entitlement import ToolEntitlement


async def tool_access_report(db, *, tenant_id, owner, policy, catalogue):
    if policy is not None and (
        policy.get("status") != "active"
        or not valid_tools(policy.get("allowed_tools"))
        or not valid_tools(policy.get("consented_tools", []))
        or not isinstance(policy.get("tool_max_cost_usd", {}), dict)
    ):
        raise HTTPException(503, "tool policy unavailable")
    try:
        primary_team = await primary_team_for_workspace(db, owner, tenant_id)
        team_id = primary_team.id if primary_team else owner.team_id
        grants = (
            await db.scalars(
                select(ToolEntitlement).where(
                    ToolEntitlement.org_id == tenant_id,
                    ToolEntitlement.team_id.in_(["", team_id]),
                )
            )
        ).all()
    except (SQLAlchemyError, ValueError):
        raise HTTPException(503, "tool entitlement unavailable") from None
    policy_catalog = policy.get("catalog", []) if policy else []
    if (
        not isinstance(policy_catalog, list)
        or len(policy_catalog) > 64
        or any(not isinstance(tool, dict) or not isinstance(tool.get("name"), str) for tool in policy_catalog)
    ):
        raise HTTPException(503, "tool catalog unavailable")
    discoverable = {tool["name"] for tool in policy_catalog}
    deployed = {tool["name"]: tool for tool in catalogue["tools"]}
    consent_mode = policy.get("consent_mode") if policy else None
    if consent_mode == "default":
        # #8387: tools the tenant entitles without an admin per-user snapshot are described
        # to the façade from the deployed catalogue, so they are never "catalogue_stale".
        stored_allowed = set(policy.get("stored_allowed_tools", policy.get("allowed_tools", [])))
        discoverable |= {name.replace(".", "___", 1) for name in policy.get("allowed_tools", []) if name in deployed and name not in stored_allowed}
    unavailable = {
        connector["name"].replace("-", "_")
        for connector in catalogue.get("connectors", [])
        if connector.get("dynamic") and connector.get("discovery_status") != "ready"
    }
    names = sorted(set(deployed) | {grant.tool_name for grant in grants} | set(policy.get("allowed_tools", []) if policy else []))
    if len(names) > 320:
        raise HTTPException(503, "tool catalogue unavailable")
    tenant = {grant.tool_name: grant for grant in grants if grant.team_id == ""}
    team = {grant.tool_name: grant for grant in grants if grant.team_id == team_id}
    allowed = set(policy.get("allowed_tools", []) if policy else [])
    consented = set(policy.get("consented_tools", []) if policy else [])
    costs = policy.get("tool_max_cost_usd", {}) if policy else {}
    if not isinstance(costs, dict):
        raise HTTPException(503, "tool policy unavailable")
    tools = []
    for name in names:
        chat_only = is_platform_tool(name)
        reason = None
        ceiling = None
        tenant_grant = tenant.get(name)
        team_grant = team.get(name)
        if name not in deployed:
            reason = "discovery_unavailable" if name.split(".", 1)[0] in unavailable else "undeployed"
        elif tenant_grant is None or not tenant_grant.enabled:
            reason = "tenant_disabled"
        elif team_grant is not None and not team_grant.enabled:
            reason = "team_disabled"
        elif name not in allowed and not implicitly_allowed(name):
            reason = "not_allowed"
        elif name not in consented:
            reason = "not_consented"
        elif implicitly_allowed(name):
            # First-party read-only tools have no provider cost and no credential: tenant
            # entitlement plus the owner's consent is the whole decision (#8113).
            ceiling = Decimal(0)
        else:
            try:
                ceiling = Decimal(str(costs.get(name)))
            except (TypeError, InvalidOperation):
                reason = "cost_policy_missing"
            if reason is None and (not ceiling.is_finite() or ceiling <= 0):
                reason = "cost_policy_missing"
            if reason is None:
                limits = [grant.max_cost_usd for grant in (tenant_grant, team_grant) if grant is not None and grant.max_cost_usd is not None]
                ceiling = min([ceiling, *limits])
                tool = name.replace(".", "___", 1)
                if tool.split("___", 1)[0] in {"github", "jira", "slack"}:
                    reason = await connection_reason(db, tenant_id=tenant_id, owner_id=owner.id, tool=tool)
        # Platform reads are deliberately absent from the facade/MCP catalogue:
        # they execute through the owner's authenticated ADP chat session.
        discovery_reason = "catalogue_stale" if reason is None and not chat_only and name.replace(".", "___", 1) not in discoverable else None
        tools.append(
            {
                "name": name,
                "transport": "chat" if chat_only else "tools_gateway",
                "ready": reason is None,
                "reason": reason,
                "discovery_reason": discovery_reason,
                "max_cost_usd": str(ceiling) if reason is None else None,
                "tenant_entitlement_revision": tenant_grant.revision if tenant_grant else 0,
                "team_entitlement_revision": team_grant.revision if team_grant else 0,
            }
        )
    return {
        "schema_version": "1.0",
        "tenant_id": tenant_id,
        "user_id": owner.id,
        "team_id": team_id,
        "policy_revision": int(policy.get("revision", 0)) if policy else 0,
        **({"consent_mode": consent_mode} if consent_mode in ("default", "explicit") else {}),
        "tools": tools,
    }


async def connection_reason(db, *, tenant_id, owner_id, tool):
    if tool == "jira___get_issue":
        from src.internal.jira_oauth import selected_jira_connection

        if os.environ.get("ADP_TOOLS_JIRA_MEDIATOR_ENABLED") != "true":
            return "jira_connection_unqualified"
        try:
            await selected_jira_connection(db, user_id=owner_id, tenant_id=tenant_id)
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            return "jira_connection_unqualified"
    _, reason = await connection_scopes(
        db,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tool=tool,
        workload_name=get_settings().oauth_workload_name,
    )
    return reason
