"""Effective tools policy for a human owner (#8387).

Three layers decide whether a human may use a tool: the tenant/team entitlement an
administrator writes (``tool_entitlements``), the owner's protected policy row
(``TOOLS_POLICY#<owner>``: admin-written ``allowed_tools``, owner-written
``consented_tools``) and the façade's per-call authorization. Before #8387 every
new user needed an administrator to write their per-user allow list AND had to tick
each tool in Settings before anything worked, which also left a fresh Dev Box Crew
(#8380) and crew pods with no tools.

This module is the one place that turns the stored row plus the tenant's choice into
the lists every consumer uses, so the façade decision, the consent and access routes,
the admin access report, the chat first-party tools and the credential/knowledge
authorities agree:

* ``consent_mode`` on the row: ``default`` means the tenant's entitlement is the
  owner's consent for every tool entitled to their team, minus ``opted_out_tools``;
  ``explicit`` keeps the pre-#8387 opt-in list. A row without the attribute is
  ``explicit`` when it carries a non-empty consent list (the owner chose) and
  ``default`` otherwise (never edited).
* The tenant chooses the posture with ``Organization.settings.tools.consent_default``
  (``granted`` | ``explicit``); absent, the platform flag ``ADP_TOOLS_CONSENT_DEFAULT``
  applies (``granted`` unless rendered otherwise). ``explicit`` forces every owner of
  that tenant into opt-in semantics regardless of their row. Invalid values fail
  closed to ``explicit``.

Entitlement (admin), per-user and entitlement cost ceilings, connection scopes,
rate limits, budgets and the per-call decision are unchanged. In default mode a tool
with a per-user ceiling keeps it; otherwise the entitlement ceiling applies; a tool
with no ceiling anywhere is still refused ``tool_cost_policy_missing``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.internal.tools_platform import PLATFORM_TOOL_NAMES, implicitly_allowed
from src.shared.models.organization import Organization
from src.shared.models.tool_entitlement import ToolEntitlement

CONSENT_DEFAULT_ENV = "ADP_TOOLS_CONSENT_DEFAULT"
GRANTED = "granted"
EXPLICIT = "explicit"
TENANT_CONSENT_DEFAULTS = (GRANTED, EXPLICIT)

MODE_DEFAULT = "default"
MODE_EXPLICIT = "explicit"
CONSENT_MODES = (MODE_DEFAULT, MODE_EXPLICIT)


def platform_consent_default() -> str:
    """The platform-wide posture rendered into ``ADP_TOOLS_CONSENT_DEFAULT`` (``granted`` by default)."""
    value = os.environ.get(CONSENT_DEFAULT_ENV, GRANTED).strip().lower()
    return value if value in TENANT_CONSENT_DEFAULTS else EXPLICIT


def tenant_consent_override(settings) -> str | None:
    """The tenant's own ``tools.consent_default`` when present; ``explicit`` for an invalid value."""
    tools = settings.get("tools") if isinstance(settings, dict) else None
    value = tools.get("consent_default") if isinstance(tools, dict) else None
    if value is None:
        return None
    return value if value in TENANT_CONSENT_DEFAULTS else EXPLICIT


def tenant_consent_default(settings) -> str:
    """Tenant override first, platform flag otherwise."""
    override = tenant_consent_override(settings)
    return override if override is not None else platform_consent_default()


async def resolve_tenant_consent_default(db, tenant_id: str) -> str:
    try:
        settings = await db.scalar(select(Organization.settings).where(Organization.id == tenant_id))
    except SQLAlchemyError:
        raise HTTPException(503, "tool policy unavailable") from None
    return tenant_consent_default(settings)


def stored_consent_mode(policy) -> str:
    """``consent_mode`` on the row; legacy rows: a chosen (non-empty) consent list is explicit."""
    if policy is None:
        return MODE_DEFAULT
    mode = policy.get("consent_mode")
    if mode in CONSENT_MODES:
        return mode
    return MODE_EXPLICIT if policy.get("consented_tools") else MODE_DEFAULT


def _string_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_policy_row(policy) -> None:
    """The shape every consumer required before #8387; 503 keeps the fail-closed behaviour."""
    if policy is None:
        return
    if (
        policy.get("status") != "active"
        or not _string_list(policy.get("allowed_tools"))
        or not _string_list(policy.get("consented_tools", []))
        or not _string_list(policy.get("opted_out_tools", []))
        or not isinstance(policy.get("tool_max_cost_usd", {}), dict)
        or (policy.get("consent_mode") is not None and policy.get("consent_mode") not in CONSENT_MODES)
    ):
        raise HTTPException(503, "tool policy unavailable")


async def entitled_tool_names(db, *, tenant_id: str, team_id: str | None) -> dict[str, Decimal | None]:
    """Every tool the tenant entitles for ``team_id`` -> the tightest entitlement ceiling (or None).

    Same rule as ``tool_entitlements.effective_entitlement``: an enabled tenant row that no
    team row disables; the ceiling is the minimum of the tenant and team ceilings present.
    """
    try:
        result = await db.scalars(
            select(ToolEntitlement).where(
                ToolEntitlement.org_id == tenant_id,
                ToolEntitlement.team_id.in_(["", team_id] if team_id else [""]),
            )
        )
        grants = result.all() if hasattr(result, "all") else list(result)
    except SQLAlchemyError:
        raise HTTPException(503, "tool entitlement unavailable") from None
    tenant = {grant.tool_name: grant for grant in grants if grant.team_id == ""}
    team = {grant.tool_name: grant for grant in grants if team_id and grant.team_id == team_id}
    entitled: dict[str, Decimal | None] = {}
    for name in sorted(tenant):
        tenant_grant = tenant[name]
        team_grant = team.get(name)
        if not tenant_grant.enabled or (team_grant is not None and not team_grant.enabled):
            continue
        ceilings = [grant.max_cost_usd for grant in (tenant_grant, team_grant) if grant is not None and grant.max_cost_usd is not None]
        entitled[name] = min(ceilings) if ceilings else None
    return entitled


@dataclass
class EffectiveToolsPolicy:
    tenant_id: str
    owner_id: str
    consent_mode: str
    tenant_consent_default: str
    allowed_tools: list[str]
    consented_tools: list[str]
    opted_out_tools: list[str]
    tool_max_cost_usd: dict[str, str]
    catalog: list
    revision: int
    stored: dict | None
    stored_allowed: list[str]
    stored_consented: list[str]
    entitled_tools: list[str] = field(default_factory=list)

    def consent_source(self, permission: str) -> str:
        """``explicit`` when the owner ticked the tool, ``default`` when the tenant posture granted it."""
        return MODE_EXPLICIT if permission in self.stored_consented else MODE_DEFAULT

    def as_policy(self) -> dict:
        """A row-shaped dict for consumers that read ``policy.get(...)``."""
        return {
            **(self.stored or {}),
            "status": "active",
            "allowed_tools": list(self.allowed_tools),
            "consented_tools": list(self.consented_tools),
            "opted_out_tools": list(self.opted_out_tools),
            "tool_max_cost_usd": dict(self.tool_max_cost_usd),
            "catalog": list(self.catalog),
            "revision": self.revision,
            "consent_mode": self.consent_mode,
            "tenant_consent_default": self.tenant_consent_default,
            "stored_allowed_tools": list(self.stored_allowed),
            "stored_consented_tools": list(self.stored_consented),
            "entitled_tools": list(self.entitled_tools),
        }


async def effective_tools_policy(
    db,
    *,
    policy,
    tenant_id: str,
    owner_id: str,
    team_id: str | None,
    include_platform: bool = True,
    tenant_default: str | None = None,
    entitled: dict[str, Decimal | None] | None = None,
) -> EffectiveToolsPolicy:
    """Resolve the owner's effective lists from the stored row (``None`` when absent).

    ``include_platform`` adds the tenant-entitled first-party tools to the allowed list in
    explicit mode too (the #8113 implicit allow); the façade passes ``False`` because those
    tools never run through it. Callers may pass ``tenant_default`` / ``entitled`` they
    already resolved; otherwise they are read here, and only when the stored mode is
    ``default`` (an explicit row never needs them).
    """
    validate_policy_row(policy)
    stored_allowed = list(policy.get("allowed_tools", [])) if policy else []
    stored_consented = list(policy.get("consented_tools", [])) if policy else []
    stored_opted_out = list(policy.get("opted_out_tools", [])) if policy else []
    ceilings = {name: str(cost) for name, cost in (policy.get("tool_max_cost_usd", {}) if policy else {}).items()}
    catalog = list(policy.get("catalog", [])) if policy else []
    revision = int(policy.get("revision", 0)) if policy else 0

    mode = stored_consent_mode(policy)
    if mode == MODE_DEFAULT:
        if tenant_default is None:
            tenant_default = await resolve_tenant_consent_default(db, tenant_id)
        if tenant_default != GRANTED:
            mode = MODE_EXPLICIT
    # An explicit row is explicit under either posture, so the tenant default is not read for
    # it unless the caller supplied one (the consent UI shows it to offer the default mode).

    if mode == MODE_EXPLICIT:
        allowed = list(stored_allowed)
        if include_platform:
            platform = await entitled_tool_names(db, tenant_id=tenant_id, team_id=team_id) if entitled is None else entitled
            allowed.extend(name for name in sorted(platform) if name in PLATFORM_TOOL_NAMES and name not in allowed)
        return EffectiveToolsPolicy(
            tenant_id=tenant_id,
            owner_id=owner_id,
            consent_mode=MODE_EXPLICIT,
            tenant_consent_default=tenant_default or "",
            allowed_tools=allowed,
            consented_tools=list(stored_consented),
            opted_out_tools=[],
            tool_max_cost_usd=ceilings,
            catalog=catalog,
            revision=revision,
            stored=policy,
            stored_allowed=stored_allowed,
            stored_consented=stored_consented,
        )

    if entitled is None:
        entitled = await entitled_tool_names(db, tenant_id=tenant_id, team_id=team_id)
    entitled_names = [name for name in sorted(entitled) if include_platform or not implicitly_allowed(name)]
    allowed = list(stored_allowed)
    allowed.extend(name for name in entitled_names if name not in allowed)
    opted_out = [name for name in stored_opted_out if name in allowed]
    # Default consent covers what the tenant entitles; an admin-allowed tool the tenant has
    # not entitled is refused ``tool_not_entitled`` before consent is ever consulted, and is
    # never reported as consented.
    consented = [name for name in allowed if name in entitled and name not in opted_out]
    for name in allowed:
        if name not in ceilings and entitled.get(name) is not None:
            ceilings[name] = str(entitled[name])
    return EffectiveToolsPolicy(
        tenant_id=tenant_id,
        owner_id=owner_id,
        consent_mode=MODE_DEFAULT,
        tenant_consent_default=tenant_default or GRANTED,
        allowed_tools=allowed,
        consented_tools=consented,
        opted_out_tools=opted_out,
        tool_max_cost_usd=ceilings,
        catalog=catalog,
        revision=revision,
        stored=policy,
        stored_allowed=stored_allowed,
        stored_consented=stored_consented,
        entitled_tools=entitled_names,
    )
