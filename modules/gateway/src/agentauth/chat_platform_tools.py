"""First-party platform tools on the chat sandbox data plane (#8113).

What the sandbox may call is decided by the tools layer the adp-tools-platform flow
(#7245) shipped, not by this module: a tool is OFFERED to a turn only when it is in the
tenant catalogue (``tool_catalogue``), the tenant entitles it for the owner's team
(``tool_entitlements``) and the owner has consented to it (``TOOLS_POLICY`` consent,
``PUT /auth/tools/consent``). First-party read-only tools need no admin per-user
``allowed_tools`` grant (``tools_platform.implicitly_allowed``); everything else about
them is the same decision every other tool gets.

What this module adds is the EXECUTION of those first-party tools: inside the gateway,
under the turn capability, scoped to the launch's tenant principal — the identity chat
admission bound (#8104) — and read-only. Every result is bounded and carries
``source``, ``observed_at`` and ``coverage`` so the answer footer can report what was
consulted. Consented MCP-backed tools (AgentCore gateway) are *not* offered to the
default turn: the façade relay requires a persistent Crew binding
(``chat_tools.verify_crew``), which the default sandbox turn does not hold. That gap is
reported in ``coverage`` rather than bypassed.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.agentauth.chat_authority import tenant_member_user
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunch, Identifier
from src.internal.tool_entitlements import entitled_tools
from src.internal.tools_effective_policy import effective_tools_policy
from src.internal.tools_platform import PLATFORM_TOOL_NAMES, PLATFORM_TOOLS, mcp_name, permission_name
from src.internal.tools_policy import ToolsPolicyStore
from src.shared.identity.providers import IdentityProvider
from src.shared.identity.verification import PROVEN_METHODS
from src.shared.identity.workspaces import primary_team_for_workspace
from src.shared.models.organization import Organization, User

logger = logging.getLogger(__name__)

#: Model-facing names must satisfy the sandbox model contract (`^[A-Za-z0-9_-]{1,128}$`).
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}___[a-z][a-z0-9_]{0,63}$")
GITHUB_READ_PERMISSIONS = {"issues": "read", "pull_requests": "read", "metadata": "read"}
_TEXT = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _instant(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_instant(value: str | None, default: datetime) -> datetime:
    if value is None:
        return default
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, detail={"error": "tool_arguments_invalid"}) from None
    if parsed.tzinfo is None:
        raise HTTPException(422, detail={"error": "tool_arguments_invalid"})
    return parsed.astimezone(UTC)


def _clip(value: Any, limit: int = _TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) else None


# --------------------------------------------------------------------------------------
# Which tools this turn may use
# --------------------------------------------------------------------------------------


class OwnerTools(BaseModel):
    """The owner's effective first-party tool set for one turn, with the reasons for gaps."""

    model_config = ConfigDict(extra="forbid")

    owner_id: str
    team_id: str
    offered: list[str]
    withheld: list[dict]
    coverage: list[dict]
    policy_revision: int


async def owner_tools(db, store, launch: ChatLaunch) -> OwnerTools:
    """catalogue ∩ tenant entitlement ∩ owner consent, for the launch's tenant principal."""
    owner = await tenant_member_user(db, launch.tenant_id, launch.user_id)
    if owner is None:
        raise ChatAuthorizationRefusedError("chat tools owner is not a tenant member")
    primary = await primary_team_for_workspace(db, owner, launch.tenant_id)
    team_id = primary.id if primary else (launch.team_id or owner.team_id or "")
    entitled = await entitled_tools(db, tenant_id=launch.tenant_id, team_id=team_id, names=PLATFORM_TOOL_NAMES)
    policy = await run_in_threadpool(ToolsPolicyStore(table_name=store.table, client=store.client).get, tenant_id=launch.tenant_id, owner_id=owner.id)
    if policy is not None and (policy.get("status") != "active" or not isinstance(policy.get("consented_tools", []), list)):
        raise ChatAuthorizationUnavailableError("tool policy unavailable")
    try:
        # #8387: the tenant posture may grant consent to every entitled tool (minus opt-outs).
        effective = await effective_tools_policy(db, policy=policy, tenant_id=launch.tenant_id, owner_id=owner.id, team_id=team_id)
    except HTTPException as error:
        raise ChatAuthorizationUnavailableError("tool policy unavailable") from error
    consented = list(effective.consented_tools)
    allowed = list(effective.allowed_tools)
    revision = effective.revision
    offered = [mcp_name(name) for name in entitled if name in consented]
    withheld = [{"name": mcp_name(name), "reason": "not_consented"} for name in entitled if name not in consented]
    coverage = [
        {"source": "tools.platform", "status": "available" if offered else "empty", "reason": None if offered else "no_consented_platform_tools"}
    ]
    # Consented MCP-backed tools exist in the owner's policy but cannot run from this turn:
    # the façade relay requires a persistent Crew binding (chat_tools.verify_crew).
    mcp_consented = [tool for tool in consented if tool in allowed and tool not in PLATFORM_TOOL_NAMES]
    if mcp_consented:
        coverage.append({"source": "tools.mcp", "status": "unavailable", "reason": "crew_binding_required"})
    return OwnerTools(owner_id=owner.id, team_id=team_id, offered=offered, withheld=withheld, coverage=coverage, policy_revision=revision)


def tool_listing(tools: OwnerTools) -> dict:
    """The data-plane ``tools/list`` document: only tools this turn can execute, with their schemas."""
    return {
        "tools": [
            {
                "name": name,
                "description": PLATFORM_TOOLS[permission_name(name)]["description"],
                "inputSchema": PLATFORM_TOOLS[permission_name(name)]["inputSchema"],
                "kind": "first_party",
                "read_only": True,
            }
            for name in tools.offered
        ],
        "withheld": tools.withheld,
        "coverage": tools.coverage,
        "policy_revision": tools.policy_revision,
        "observed_at": _instant(_now()),
    }


# --------------------------------------------------------------------------------------
# Argument models (strict, bounded) — one per tool
# --------------------------------------------------------------------------------------


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FlowsArguments(_Arguments):
    limit: int = Field(default=10, ge=1, le=20)
    status: str | None = Field(default=None, pattern=r"^(attention_needed|awaiting_you|running|queued|complete)$")
    include_archived: bool = False


class RunsArguments(_Arguments):
    since: str | None = Field(default=None, min_length=1, max_length=40)
    until: str | None = Field(default=None, min_length=1, max_length=40)
    limit: int = Field(default=10, ge=1, le=20)


class WorkItemsArguments(_Arguments):
    state: str = Field(default="open", pattern=r"^(open|closed|all)$")
    kind: str = Field(default="all", pattern=r"^(issue|pull_request|all)$")
    since: str | None = Field(default=None, min_length=1, max_length=40)
    limit: int = Field(default=20, ge=1, le=30)


class BudgetArguments(_Arguments):
    period_type: str = Field(default="monthly", pattern=r"^(daily|weekly|monthly)$")


ARGUMENTS = {
    "platform.my_flows": FlowsArguments,
    "platform.my_agent_runs": RunsArguments,
    "platform.my_work_items": WorkItemsArguments,
    "platform.my_budget": BudgetArguments,
}


def parse_arguments(permission: str, arguments: dict):
    try:
        return ARGUMENTS[permission].model_validate(arguments)
    except ValidationError:
        raise HTTPException(422, detail={"error": "tool_arguments_invalid"}) from None


# --------------------------------------------------------------------------------------
# Handlers — read-only, scoped by the launch, bounded, with source/freshness/coverage
# --------------------------------------------------------------------------------------


async def my_flows(db, launch: ChatLaunch, arguments: FlowsArguments) -> dict:
    from src.orchestration.display_state import FlowStatus
    from src.orchestration.repository import OrchestrationRepository

    page = await OrchestrationRepository(db).list_flows_page_with_aggregates(
        org_id=launch.tenant_id,
        limit=arguments.limit,
        status=FlowStatus(arguments.status) if arguments.status else None,
        include_archived=arguments.include_archived,
        sort="updated",
    )
    flows = []
    for aggregate in page.flows:
        flow = aggregate.flow
        counts = aggregate.display_counts
        flows.append(
            {
                "id": flow.id,
                "slug": flow.slug,
                "title": _clip(flow.title),
                "status": aggregate.status.value if hasattr(aggregate.status, "value") else str(aggregate.status),
                "execution_paused": bool(flow.execution_paused),
                "archived": flow.archived_at is not None,
                "stories": aggregate.story_count,
                "stories_complete": aggregate.completed_story_count,
                "gates": aggregate.gate_count,
                "attention_needed": aggregate.stalled_count,
                "nodes": {
                    "queued": counts.queued,
                    "in_progress": counts.in_progress,
                    "gate": counts.gate,
                    "stalled": counts.stalled,
                    "complete": counts.complete,
                },
                "created_at": _instant(flow.created_at),
                "updated_at": _instant(flow.updated_at),
                "url": f"/orchestration/flows/{flow.id}",
            }
        )
    return {
        "source": "orchestration.flows",
        "scope": "tenant",
        "observed_at": _instant(_now()),
        "coverage": [{"source": "orchestration.flows", "status": "available" if flows else "empty"}],
        "has_more": page.total > len(flows),
        "total": page.total,
        "flows": flows,
    }


async def my_agent_runs(service, launch: ChatLaunch, arguments: RunsArguments) -> dict:
    now = _now()
    until = _parse_instant(arguments.until, now)
    since = _parse_instant(arguments.since, until - timedelta(days=7))
    if not since < until <= since + timedelta(days=90):
        raise HTTPException(422, detail={"error": "tool_arguments_invalid"})
    window = {"from": _instant(since), "to": _instant(until)}
    result = await run_in_threadpool(
        service.query_by_user,
        launch.user_id,
        tenant_id=launch.tenant_id,
        page_size=arguments.limit,
        since=window["from"],
        until=window["to"],
    )
    runs = [
        {
            "invocation_id": item.invocation_id,
            "invoked_at": item.invoked_at,
            "status": item.status,
            "persona": item.persona,
            "channel": item.channel,
            "topic": _clip(item.topic),
            "summary": _clip(item.summary, 400),
            "repo": item.repo,
            "issue_number": item.issue_number,
            "trigger_kind": item.trigger_kind,
            "source_url": item.source_url,
            "url": f"/me/agent-invocations/{item.invocation_id}",
        }
        for item in result.items[: arguments.limit]
    ]
    return {
        "source": "activity.agent_runs",
        "scope": "owner",
        "window": window,
        "observed_at": _instant(now),
        "coverage": [{"source": "activity.agent_runs", "status": "available" if runs else "empty"}],
        "has_more": result.last_key is not None,
        "runs": runs,
    }


def github_client() -> httpx.AsyncClient:
    """One short-lived client per call; replaced in tests."""
    from src.knowledge.github_app_service import GITHUB_API_BASE

    return httpx.AsyncClient(
        base_url=GITHUB_API_BASE,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        timeout=10.0,
    )


async def _owner_github_login(db, launch: ChatLaunch, owner_id: str) -> str | None:
    """The owner's proven GitHub login, from the identity index — never a caller-supplied name."""
    from src.shared.models.vault import UserIdentity

    rows = (
        await db.scalars(
            select(UserIdentity).where(
                UserIdentity.user_id == owner_id,
                UserIdentity.provider == IdentityProvider.github,
                UserIdentity.verification_method.in_(PROVEN_METHODS),
            )
        )
    ).all()
    rows = sorted(rows, key=lambda row: (row.org_id != launch.tenant_id, row.provider_username or ""))
    for row in rows:
        login = row.provider_username
        if isinstance(login, str) and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
            return login
    return None


async def _tenant_installation(db, tenant_id: str) -> int | None:
    raw = await db.scalar(select(Organization.github_installation_ids).where(Organization.id == tenant_id))
    ids = [str(value).strip() for value in (raw or []) if str(value).strip()]
    if len(ids) != 1 or not ids[0].isdigit():
        return None
    return int(ids[0])


def _work_item(item: dict, login: str) -> dict:
    author = (item.get("user") or {}).get("login")
    assignees = [entry.get("login") for entry in item.get("assignees") or [] if isinstance(entry, dict)]
    relation = "authored" if author == login else "assigned" if login in assignees else "involved"
    repository = item.get("repository_url", "")
    repo = "/".join(repository.rsplit("/", 2)[-2:]) if isinstance(repository, str) and repository.count("/") >= 2 else None
    is_pull = "pull_request" in item
    return {
        "kind": "pull_request" if is_pull else "issue",
        "repo": repo,
        "number": item.get("number"),
        "title": _clip(item.get("title")),
        "state": item.get("state"),
        "draft": bool(item.get("draft")) if is_pull else None,
        "relation": relation,
        "author": author,
        "assignees": assignees[:10],
        "labels": [label.get("name") for label in (item.get("labels") or [])[:10] if isinstance(label, dict)],
        "updated_at": item.get("updated_at"),
        "url": item.get("html_url") if isinstance(item.get("html_url"), str) and item["html_url"].startswith("https://") else None,
    }


async def my_work_items(db, launch: ChatLaunch, owner_id: str, arguments: WorkItemsArguments) -> dict:
    """The owner's GitHub issues/PRs via the tenant's GitHub App installation, filtered by their proven login.

    Reads use an installation token minted for ONE call with read-only ``issues``,
    ``pull_requests`` and ``metadata`` permissions; the token never leaves this process and
    nothing here accepts a personal token. GitHub's search API answers ``involves:<login>``
    across the repositories the installation can see; results outside that set are dropped.
    """
    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    now = _now()
    since = _parse_instant(arguments.since, now - timedelta(days=30))
    window = {"from": _instant(since), "to": _instant(now)}
    base = {"source": "github.work_items", "scope": "owner", "window": window, "observed_at": _instant(now), "has_more": False, "items": []}

    def unavailable(status: str, reason: str) -> dict:
        return {**base, "coverage": [{"source": "github.work_items", "status": status, "reason": reason}]}

    login = await _owner_github_login(db, launch, owner_id)
    if login is None:
        return unavailable("unavailable", "github_identity_unlinked")
    installation_id = await _tenant_installation(db, launch.tenant_id)
    if installation_id is None:
        return unavailable("unavailable", "installation_unresolved")
    try:
        app_id, private_key = await resolve_tenant_app_credentials(launch.tenant_id)
    except Exception:
        return unavailable("unavailable", "app_credentials_unavailable")
    coverage: list[dict] = []
    try:
        token, _ = await mint_installation_token_with_expiry(app_id, private_key, installation_id, permissions=GITHUB_READ_PERMISSIONS)
    except httpx.HTTPStatusError as error:
        return unavailable("denied" if error.response.status_code in (401, 403) else "unavailable", "installation_token_refused")
    except Exception:
        return unavailable("unavailable", "installation_token_unavailable")
    headers = {"Authorization": f"Bearer {token}"}
    async with github_client() as client:
        try:
            repositories = await client.get("/installation/repositories", headers=headers, params={"per_page": 100})
            repositories.raise_for_status()
            listed = repositories.json().get("repositories") or []
        except httpx.HTTPStatusError as error:
            return unavailable("denied" if error.response.status_code in (401, 403) else "unavailable", "repositories_unreadable")
        except (httpx.HTTPError, ValueError):
            return unavailable("unavailable", "provider_failure")
        repos = {entry.get("full_name") for entry in listed if isinstance(entry, dict) and isinstance(entry.get("full_name"), str)}
        if not repos:
            return unavailable("empty", "no_repositories")
        if len(listed) >= 100:
            coverage.append({"source": "github.work_items", "status": "partial", "reason": "repository_page_limit"})
        account = str(listed[0].get("owner", {}).get("login", ""))
        qualifiers = [f"involves:{login}", f"updated:>={since.date().isoformat()}"]
        if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", account):
            qualifiers.append(f"user:{account}")
        if arguments.state != "all":
            qualifiers.append(f"is:{arguments.state}")
        if arguments.kind != "all":
            qualifiers.append("is:pr" if arguments.kind == "pull_request" else "is:issue")
        try:
            search = await client.get(
                "/search/issues",
                headers=headers,
                params={"q": " ".join(qualifiers), "sort": "updated", "order": "desc", "per_page": arguments.limit},
            )
            search.raise_for_status()
            body = search.json()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            return {
                **base,
                "coverage": [
                    *coverage,
                    {
                        "source": "github.work_items",
                        "status": "denied" if status in (401, 403) else "unavailable",
                        "reason": "search_refused" if status in (401, 403, 422) else "provider_failure",
                    },
                ],
            }
        except (httpx.HTTPError, ValueError):
            return {**base, "coverage": [*coverage, {"source": "github.work_items", "status": "unavailable", "reason": "provider_failure"}]}
    items = [_work_item(item, login) for item in body.get("items") or [] if isinstance(item, dict)]
    items = [item for item in items if item["repo"] in repos][: arguments.limit]
    total = body.get("total_count") if isinstance(body.get("total_count"), int) else len(items)
    coverage.append({"source": "github.work_items", "status": "available" if items else "empty"})
    return {**base, "coverage": coverage, "has_more": total > len(items), "total": total, "login": login, "items": items}


async def my_budget(db, launch: ChatLaunch, owner: User, arguments: BudgetArguments) -> dict:
    """The owner's own ``/me/budget`` figures, produced by that route's handler for a principal built from the launch."""
    from src.budget.me_routes import get_my_budget
    from src.shared.schemas.auth import TokenContext

    now = _now()
    principal = TokenContext(
        user_id=owner.cognito_sub or owner.id,
        org_id=launch.tenant_id,
        team_id=launch.team_id or owner.team_id or "",
        department_id="",
        account_type="human",
        expires_at=now + timedelta(minutes=1),
        attributed_org_id=launch.tenant_id,
    )
    base = {"source": "budget.me", "scope": "owner", "observed_at": _instant(now)}
    try:
        figures = await get_my_budget(current_user=principal, db=db, period_type=arguments.period_type)
    except HTTPException as error:
        return {**base, "coverage": [{"source": "budget.me", "status": "unavailable", "reason": f"http_{error.status_code}"}], "budget": None}
    document = figures.model_dump(mode="json")
    budget = {
        key: document.get(key)
        for key in (
            "period",
            "entity_type",
            "cap_usd",
            "spend_usd",
            "remaining_usd",
            "utilization_pct",
            "band",
            "cap_status",
            "enforcement_mode",
            "identity_status",
            "freshness",
        )
    }
    budget["lines"] = [
        {key: line.get(key) for key in ("entity_type", "spend_usd", "cap_usd", "remaining_usd", "cap_status")}
        for line in (document.get("lines") or [])[:4]
    ]
    return {**base, "coverage": [{"source": "budget.me", "status": "available"}], "budget": budget}


async def execute_platform_tool(tool: str, arguments: dict, *, db, launch: ChatLaunch, owner_id: str, activity_service) -> dict:
    """Run one offered first-party tool for this turn and record the authorized call."""
    permission = permission_name(tool)
    parsed = parse_arguments(permission, arguments)
    owner = await tenant_member_user(db, launch.tenant_id, launch.user_id)
    if owner is None or owner.id != owner_id:
        raise ChatAuthorizationRefusedError("chat tools owner changed")
    if permission == "platform.my_flows":
        result = await my_flows(db, launch, parsed)
    elif permission == "platform.my_agent_runs":
        result = await my_agent_runs(activity_service, launch, parsed)
    elif permission == "platform.my_work_items":
        result = await my_work_items(db, launch, owner.id, parsed)
    elif permission == "platform.my_budget":
        result = await my_budget(db, launch, owner, parsed)
    else:  # pragma: no cover - the listing only offers registered tools
        raise HTTPException(404, detail={"error": "tool_unknown"})
    receipt_id = str(uuid4())
    # Same durable "authorized call" row the façade path writes (docs/tools/architecture.md §4),
    # with the honest zero estimate a first-party read carries. Its absence fails the call.
    await run_in_threadpool(
        activity_service.record_tool_call,
        receipt_id=receipt_id,
        user_id=owner.id,
        tenant_id=launch.tenant_id,
        tool=tool,
        estimated_cost_usd=Decimal(0),
        run_id=launch.run_id,
        team_id=launch.team_id or None,
    )
    return {"tool": tool, "receipt_id": receipt_id, "status": "confirmed", "result": result}


class ToolsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier


class ToolsCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    tool: str = Field(pattern=TOOL_NAME.pattern)
    arguments: dict = Field(default_factory=dict)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


__all__ = [
    "GITHUB_READ_PERMISSIONS",
    "OwnerTools",
    "ToolsCallRequest",
    "ToolsListRequest",
    "execute_platform_tool",
    "github_client",
    "owner_tools",
    "tool_listing",
]
