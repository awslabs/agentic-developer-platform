"""Read frozen Security fixture identities without entering tenant Products.

This projects retained release-request metadata, not local owner consent, native
authority or fixture acceptance. Paused and superseded dispatches remain visible.
"""

import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session
from src.auth.dependencies import get_current_user
from src.products.security_evidence_contract import FixtureCase, Hash, Strict
from src.products.security_evidence_store import event_id
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .compile import address_of
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)
from .policy_identity import same_human_principal
from .proposal import ProposedNode
from .shared_window import accepted_document_hash

router = APIRouter()
KIND = "security_evidence_roster"
MAX_DISPATCHES = 2000


class FrozenRoster(Strict):
    fixture_sha256: Hash
    fixture_expires_at: AwareDatetime = Field(strict=False)
    cases: list[FixtureCase] = Field(min_length=2, max_length=2)


def unavailable():
    return HTTPException(
        503,
        detail={
            "code": "security_evidence_roster_unavailable",
            "next_action": (
                "The retained roster or its original dispatch cannot be verified. Preserve the original release history "
                "and repair this read; do not infer that no fixture was frozen."
            ),
        },
    )


def not_found():
    return HTTPException(404, detail={"code": "not_found"})


async def project_roster(db, flow, row, dispatches):
    matches = [(action, execution) for action, execution in dispatches if event_id(action.id, "security_fixture_roster") == row.id]
    if (
        len(matches) != 1
        or row.actor_kind != "service"
        or row.actor_id != "system:security-release"
        or not isinstance(row.reason, str)
        or len(row.reason) > 16 * 1024
    ):
        raise unavailable()
    action, execution = matches[0]
    if (
        row.node_id != execution.node_id
        or row.org_id != execution.org_id
        or row.flow_id != execution.flow_id
        or action.org_id != row.org_id
        or action.execution_id != execution.id
    ):
        raise unavailable()
    node = await db.scalar(
        select(OrchestrationNode).where(
            OrchestrationNode.id == row.node_id, OrchestrationNode.org_id == flow.org_id, OrchestrationNode.flow_id == flow.id
        )
    )
    plan = await db.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == flow.org_id,
            OrchestrationAcceptedPlan.flow_id == flow.id,
            OrchestrationAcceptedPlan.version == execution.accepted_plan_version,
        )
    )
    if node is None or plan is None or plan.plan_hash != accepted_document_hash(plan):
        raise unavailable()
    # contract_from_plan deliberately rejects superseded plans. Historical reads
    # validate the immutable original document without reviving its authority.
    entries = [item for item in plan.plan_document.get("nodes", []) if isinstance(item, dict) and item.get("address") == address_of(flow.slug, node)]
    if len(entries) != 1:
        raise unavailable()
    release = ProposedNode.model_validate(entries[0]).operational_release_binding
    detail = action.detail or {}
    if (
        not isinstance(detail, dict)
        or release is None
        or release.component != "l5-security"
        or release.delivery_contract is None
        or "ui-cli-mcp-parity" not in release.delivery_contract.required_checks
        or detail.get("contract_digest") != release.delivery_contract.digest
        or any(
            not isinstance(detail.get(key), str) or re.fullmatch(r"[a-f0-9]{40}", detail[key]) is None
            for key in ("source_revision", "definition_revision", "dispatch_revision")
        )
    ):
        raise unavailable()
    roster = FrozenRoster.model_validate(json.loads(row.reason))
    if len({item.tenant_id for item in roster.cases}) != 2 or len({item.product_id for item in roster.cases}) != 2:
        raise unavailable()
    job = detail.get("release_job") or {}
    if not isinstance(job, dict) or any(not str(job.get(key, "")).isdecimal() or int(job[key]) < 1 for key in ("run_id", "run_attempt")):
        raise unavailable()
    # Explicit fields only. The dispatch detail and local evidence-event payloads
    # may contain private data that this lookup has no authority to disclose.
    return {
        "roster_id": row.id,
        "recorded_at": row.created_at.isoformat(),
        "node_id": row.node_id,
        "execution_id": execution.id,
        "dispatch_action_id": action.id,
        "accepted_plan_version": plan.version,
        "accepted_plan_hash": plan.plan_hash,
        "source_revision": detail["source_revision"],
        "definition_revision": detail["definition_revision"],
        "dispatch_revision": detail["dispatch_revision"],
        "run_id": int(job["run_id"]),
        "run_attempt": int(job["run_attempt"]),
        **roster.model_dump(mode="json"),
    }


async def original_owner(db, flow, reader_id, operator):
    if operator:
        return
    plans = list(
        await db.scalars(
            select(OrchestrationAcceptedPlan)
            .where(OrchestrationAcceptedPlan.org_id == flow.org_id, OrchestrationAcceptedPlan.flow_id == flow.id)
            .order_by(OrchestrationAcceptedPlan.version)
            .limit(MAX_DISPATCHES + 1)
        )
    )
    if len(plans) > MAX_DISPATCHES:
        raise unavailable()
    for plan in plans:
        if not isinstance(plan.plan_document, dict):
            raise unavailable()
        policy = (plan.plan_document or {}).get("execution_policy") or {}
        if not isinstance(policy, dict):
            raise unavailable()
        if policy.get("principal_id"):
            if plan.plan_hash != accepted_document_hash(plan) or policy.get("org_id") != flow.org_id:
                raise unavailable()
            if await same_human_principal(db, org_id=flow.org_id, recorded_id=policy["principal_id"], current_id=reader_id):
                return
            break
    # A newer policy, billing identity or team membership cannot impersonate the
    # original owner. Verified platform operators retain the existing read role.
    raise not_found()


async def read_rosters(db, *, org_id, flow_id, reader_id, operator=False, node_id=None, limit=25, offset=0):
    flow = await db.scalar(select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id))
    if flow is None:
        raise not_found()
    await original_owner(db, flow, reader_id, operator)
    if node_id is not None and not await db.scalar(
        select(OrchestrationNode.id).where(OrchestrationNode.id == node_id, OrchestrationNode.org_id == org_id, OrchestrationNode.flow_id == flow_id)
    ):
        raise not_found()
    query = select(OrchestrationDecision).where(
        OrchestrationDecision.org_id == org_id, OrchestrationDecision.flow_id == flow_id, OrchestrationDecision.kind == KIND
    )
    if node_id is not None:
        query = query.where(OrchestrationDecision.node_id == node_id)
    rows = list(await db.scalars(query.order_by(OrchestrationDecision.created_at, OrchestrationDecision.id).offset(offset).limit(limit + 1)))
    # Empty later pages are not evidence of an empty entire history.
    any_recorded = bool(rows) or bool(await db.scalar(query.with_only_columns(OrchestrationDecision.id).limit(1)))
    output = []
    if rows:
        dispatch_query = (
            select(OrchestrationAction, OrchestrationExecution)
            .join(
                OrchestrationExecution,
                (OrchestrationAction.execution_id == OrchestrationExecution.id) & (OrchestrationAction.org_id == OrchestrationExecution.org_id),
            )
            .where(
                OrchestrationAction.org_id == org_id,
                OrchestrationExecution.flow_id == flow_id,
                OrchestrationAction.kind == "delivery_workflow_dispatch",
                OrchestrationExecution.node_id.in_({row.node_id for row in rows[:limit]}),
            )
        )
        dispatches = list((await db.execute(dispatch_query.limit(MAX_DISPATCHES + 1))).all())
        if len(dispatches) > MAX_DISPATCHES:
            raise unavailable()
        output = [await project_roster(db, flow, row, dispatches) for row in rows[:limit]]
    return {
        "flow_id": flow_id,
        "node_id": node_id,
        "state": "recorded" if any_recorded else "no_roster_recorded",
        "items": output,
        "next_offset": offset + limit if len(rows) > limit else None,
        "evidence_only": True,
        "execution_authorized": False,
        "limitation": (
            "A frozen release request does not prove Product owner consent, native execution authority or acceptance. "
            "No roster record does not prove that no historical grant existed."
        ),
    }


@router.get("/flows/{flow_id}/security-evidence-rosters")
async def get_security_rosters(
    response: Response,
    flow_id: str = Path(min_length=1, max_length=36),
    node_id: str | None = Query(default=None, min_length=1, max_length=36),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    if current_user.token_use != "access" or not isinstance(current_user.expires_at, datetime):
        raise HTTPException(401, detail={"code": "session_required"})
    try:
        human = await authorize_human_session(current_user, db)
        await AccessControl(db).check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=human.tenant_id)
        return await read_rosters(
            db,
            org_id=human.tenant_id,
            flow_id=flow_id,
            reader_id=human.user_id,
            operator=current_user.is_admin,
            node_id=node_id,
            limit=limit,
            offset=offset,
        )
    except BootstrapRefusedError:
        raise HTTPException(403, detail={"code": "security_evidence_roster_reader_denied"}) from None
    except (ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError):
        raise unavailable() from None
