"""An exact current release can request, but cannot author, local human facts."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, Field
from sqlalchemy import func, select

from src.products.security_evidence_contract import FixtureCase, Hash, Reference, Selection, Strict, timestamp
from src.products.security_evidence_model import SecurityEvidenceEvent
from src.products.security_evidence_store import FLAGS, append, event, reference
from src.shared.identity import resolve_canonical_user_id
from src.shared.identity.workflow_access import is_workflow_member
from src.shared.services.credential_resolver import CredentialNotFoundError

from .accepted_node import accepted_node_contract
from .l5_observation_export import authorized_read
from .l5_observation_scope import ObservationError, aware, digest, require
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .operations_release_job import AUDIENCE, NativeJobRequest, github_claims, service
from .policy_admission import load_in_force_policy
from .review_cycle import CycleBlockedError
from .shared_window import accepted_document_hash

router = APIRouter(prefix="/orchestration/operations-release-job/security-evidence", tags=["orchestration"])

# A release may use the minimum 30-second invitation throughout the original
# maximum 900-second fixture window, plus its initial invitation. Replays do
# not consume slots; changed snapshots and renewals do. The dispatch action is
# locked by authorized_read, so concurrent requests share this durable bound.
MAX_REQUESTS_PER_CASE_PHASE = 32


NEXT_ACTION = {
    "security_evidence_connection_unavailable": (
        "The approved release connection is unavailable to its owner. Check that the registered AWS credential still exists, "
        "has not expired, and remains accessible to the policy owner. Restore that same approved connection before continuing the original release."
    ),
    "security_evidence_release_job_refused": (
        "The workflow job could not prove current release authority. Check its original run and attempt, accepted source and workflow revisions, "
        "current work claim and target lease. Reconcile the original dispatch and preserve its receipts before continuing."
    ),
    "security_evidence_request_limit": (
        "The original release exhausted this tenant's bounded phase invitations. Preserve its requests and receipts for review; "
        "do not replace the fixture or repeat completed effects."
    ),
    "security_evidence_release_changed": (
        "The release authority changed during collection. Preserve the original receipts; do not move them to another run or repeat effects."
    ),
    "security_evidence_policy_revoked": (
        "The accepted policy or its owner's membership is no longer active. Restore legitimate release "
        "policy authority before requesting new evidence."
    ),
    "security_evidence_original_lease_expired": (
        "The original release lease ended. Retain the saved work and receipts for review; extending a lease cannot revive this request."
    ),
    "security_evidence_request_expired": (
        "Request an exact renewal from the same still-running release within the original lease, with fresh owner consent for disclosure."
    ),
    "security_evidence_fixture_changed": (
        "Use the original frozen two-tenant fixture and expiry. A replacement Product, tenant or fixture needs a separately reviewed release."
    ),
    "security_evidence_reconcile_original_required": (
        "Reconcile the original save receipt locally before renewing disclosure. Do not perform the save again."
    ),
    "security_evidence_grant_revoked": (
        "The Product owner revoked this evidence. Retained history remains inspectable but cannot authorize another transfer."
    ),
}


class Issue(Strict):
    job: NativeJobRequest
    fixture_sha256: Hash
    fixture_cases: list[FixtureCase] = Field(min_length=2, max_length=2)
    fixture_expires_at: AwareDatetime = Field(strict=False)
    selections: list[Selection] = Field(min_length=1, max_length=2)
    lifetime_seconds: int = Field(ge=30, le=900)
    renew_of: Reference | None = None


class Exchange(Reference):
    job: NativeJobRequest
    ticket_id: str | None = None


async def release_scope(db, action):
    now = datetime.now(UTC)
    execution = await db.get(OrchestrationExecution, action.execution_id)
    node = await db.get(OrchestrationNode, execution.node_id)
    flow = await db.get(OrchestrationFlow, execution.flow_id)
    claim = await db.get(OrchestrationWorkClaim, execution.claim_id)
    contract = await accepted_node_contract(db, node)
    release = contract.node.operational_release_binding if contract else None
    require(
        release is not None
        and release.component == "l5-security"
        and release.delivery_contract is not None
        and "ui-cli-mcp-parity" in release.delivery_contract.required_checks,
        "security_evidence_check_not_accepted",
    )
    admitted = await load_in_force_policy(db, org_id=action.org_id, flow_id=flow.id)
    policy = admitted.policy
    require(admitted.refusal is None and policy is not None and policy.expires_at > now, "security_evidence_policy_revoked")
    owner = await resolve_canonical_user_id(db, policy.principal_id, org_id=action.org_id)
    require(await is_workflow_member(db, user_id=owner, org_id=action.org_id), "security_evidence_policy_revoked")
    controls = list(
        await db.scalars(
            select(OrchestrationDecision.id)
            .where(
                OrchestrationDecision.org_id == action.org_id,
                OrchestrationDecision.flow_id == flow.id,
                OrchestrationDecision.kind.in_(["flow_paused", "flow_resumed", "flow_archived"]),
            )
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        )
    )
    control = controls[0] if controls else None
    transitions = list(
        await db.scalars(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == action.org_id,
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == "execution_phase_changed",
            )
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        )
    )
    # A legacy running execution has no anchor until its first real transition.
    # Its claim/control/source pins remain mandatory; the first transition then
    # invalidates its prior None anchor permanently.
    relevant = [(json.loads(row.reason)["revision"], row.id) for row in transitions if json.loads(row.reason)["execution_id"] == execution.id]
    transition = max(relevant)[1] if relevant else None
    detail = action.detail
    identity = {
        key: detail[key]
        for key in (
            "correlation",
            "contract_digest",
            "repo",
            "provider_repository_id",
            "workflow",
            "source_revision",
            "definition_revision",
            "dispatch_revision",
            "branch",
        )
    }
    plan = await db.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == action.org_id,
            OrchestrationAcceptedPlan.flow_id == flow.id,
            OrchestrationAcceptedPlan.version == execution.accepted_plan_version,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    require(plan is not None and plan.plan_hash == accepted_document_hash(plan), "security_evidence_release_changed")
    # revision is a CAS counter and normal next_check_at polling advances it.
    # Stable authority pins and immutable control history invalidate captures;
    # harmless scheduling updates must not discard a person's observation.
    identity.update(
        release_org_id=action.org_id,
        flow_id=flow.id,
        node_id=node.id,
        execution_id=execution.id,
        execution_phase=execution.phase,
        execution_status=execution.status,
        execution_transition_id=transition,
        cycle=execution.cycle,
        accepted_plan_hash=plan.plan_hash,
        accepted_plan_version=execution.accepted_plan_version,
        policy_hash=policy.policy_hash,
        policy_owner_id=owner,
        dispatch_action_id=action.id,
        claim_id=claim.id,
        claim_generation=claim.generation,
        flow_control_id=control,
        flow_control_history_sha256=digest(sorted(controls)),
        run_id=int(detail["release_job"]["run_id"]),
        run_attempt=int(detail["release_job"]["run_attempt"]),
    )
    return identity, min(policy.expires_at, aware(claim.lease_expires_at))


async def issue(guard, body, claims):
    async def write(db, action):
        identity, bound = await release_scope(db, action)
        now = datetime.now(UTC)
        require(len({item.tenant_id for item in body.selections}) == len(body.selections), "security_evidence_distinct_tenants_required")
        require(
            len({item.tenant_id for item in body.fixture_cases}) == 2 and len({item.product_id for item in body.fixture_cases}) == 2,
            "security_evidence_distinct_tenants_required",
        )
        from types import SimpleNamespace

        from src.products.security_evidence_store import event_id

        from .l5_observations import append as append_decision

        roster = {
            "fixture_sha256": body.fixture_sha256,
            "fixture_expires_at": body.fixture_expires_at.isoformat(),
            "cases": sorted([item.model_dump() for item in body.fixture_cases], key=lambda item: item["tenant_id"]),
        }
        roster_id = event_id(action.id, "security_fixture_roster")
        retained_roster = await db.get(OrchestrationDecision, roster_id)
        if retained_roster is None:
            append_decision(
                db,
                identity=SimpleNamespace(org_id=action.org_id, flow_id=identity["flow_id"], node_id=identity["node_id"]),
                kind="security_evidence_roster",
                actor_id="system:security-release",
                actor_kind="service",
                data=roster,
                row_id=roster_id,
            )
        else:
            require(json.loads(retained_roster.reason) == roster, "security_evidence_fixture_changed")
        bound = min(bound, body.fixture_expires_at)
        results = []
        for selected in body.selections:
            # This is a request addressed to the tenant, not an authority to read
            # its Product. Only the local owner can turn it into a consent.
            require({key: selected.model_dump()[key] for key in FixtureCase.model_fields} in roster["cases"], "security_evidence_fixture_changed")
            key = digest({"release": identity, "fixture": body.fixture_sha256, "selection": selected.model_dump()})
            from src.products.security_evidence_store import event_id

            original_bound = bound
            predecessor = None
            origin_request_id = None
            export_only = False
            if body.renew_of is not None:
                require(len(body.selections) == 1 and body.renew_of.tenant_id == selected.tenant_id, "security_evidence_renewal_invalid")
                prior = await event(db, selected.tenant_id, body.renew_of.request_id, "request")
                require(
                    prior.document["release"] == identity
                    and prior.document["selection"] == selected.model_dump()
                    and prior.document["fixture_sha256"] == body.fixture_sha256
                    and timestamp(prior.document["expires_at"]) <= now,
                    "security_evidence_renewal_invalid",
                )
                origin_request_id = prior.document.get("origin_request_id") or prior.request_id
                begun = await event(db, selected.tenant_id, origin_request_id, "effect_before", required=False)
                observed = await event(db, selected.tenant_id, origin_request_id, "observed", required=False)
                if begun is not None or observed is not None:
                    reconciled = await event(db, selected.tenant_id, origin_request_id, "effect_reconciled", required=False)
                    returned = await event(db, selected.tenant_id, origin_request_id, "effect_after", required=False)
                    require(
                        observed is not None or reconciled is not None or (returned is not None and returned.document["outcome"] == "returned"),
                        "security_evidence_reconcile_original_required",
                    )
                    export_only = True
                require(await event(db, selected.tenant_id, origin_request_id, "revoked", required=False) is None, "security_evidence_grant_revoked")
                predecessor = prior.request_id
                original_bound = min(bound, timestamp(prior.document["initial_claim_lease_expires_at"]))
                key = digest({"selection_key": key, "predecessor": predecessor})
            require(original_bound > now, "security_evidence_original_lease_expired")
            request_id = event_id(key, "request")
            existing = await event(db, selected.tenant_id, request_id, "request", required=False)
            if existing is None:
                count = await db.scalar(
                    select(func.count())
                    .select_from(SecurityEvidenceEvent)
                    .where(
                        SecurityEvidenceEvent.tenant_id == selected.tenant_id,
                        SecurityEvidenceEvent.kind == "request",
                        SecurityEvidenceEvent.document["release"]["dispatch_action_id"].as_string() == action.id,
                        SecurityEvidenceEvent.document["selection"]["phase"].as_string() == selected.phase,
                    )
                )
                require(count < MAX_REQUESTS_PER_CASE_PHASE, "security_evidence_request_limit")
                row = await append(
                    db,
                    selected.tenant_id,
                    request_id,
                    "request",
                    "system:security-release",
                    "service",
                    {
                        "release": identity,
                        "fixture_sha256": body.fixture_sha256,
                        "selection": selected.model_dump(),
                        "initial_claim_lease_expires_at": original_bound.isoformat(),
                        "predecessor_request_id": predecessor,
                        "origin_request_id": origin_request_id if export_only else None,
                        "export_only": export_only,
                        "created_at": now.isoformat(),
                        "expires_at": min(original_bound, now + timedelta(seconds=body.lifetime_seconds)).isoformat(),
                    },
                )
            else:
                row = existing
            require(timestamp(row.document["expires_at"]) > now, "security_evidence_request_expired")
            results.append({"request_id": request_id, "tenant_id": selected.tenant_id, "request": row.document, "receipt": reference(row)})
        return {"requests": results, **FLAGS}

    return await authorized_read(guard, body.job, claims, write)


async def exchange(guard, body, claims, *, consume=False):
    async def transfer(db, action):
        identity, bound = await release_scope(db, action)
        request = await event(db, body.tenant_id, body.request_id, "request")
        require(request.document["release"] == identity, "security_evidence_release_changed")
        require(timestamp(request.document["expires_at"]) > datetime.now(UTC), "security_evidence_request_expired")
        require(timestamp(request.document["initial_claim_lease_expires_at"]) > datetime.now(UTC), "security_evidence_original_lease_expired")
        if not consume:
            ticket = str(uuid4())
            row = await append(
                db,
                body.tenant_id,
                ticket,
                "ticket",
                "system:security-release",
                "service",
                {
                    "request_id": body.request_id,
                    "release": identity,
                    "expires_at": min(bound, timestamp(request.document["expires_at"]), datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                },
            )
            return {"ticket_id": ticket, "receipt": reference(row), **FLAGS}
        require(body.ticket_id is not None, "security_evidence_ticket_required")
        ticket = await event(db, body.tenant_id, body.ticket_id, "ticket")
        require(
            ticket.document["request_id"] == body.request_id
            and ticket.document["release"] == identity
            and timestamp(ticket.document["expires_at"]) > datetime.now(UTC),
            "security_evidence_ticket_expired",
        )
        exported = await event(db, body.tenant_id, body.ticket_id, "export")
        from src.products.security_evidence_local import validate_export_authority

        await validate_export_authority(db, request, exported.document["reader"])
        facts = exported.document["facts"]
        require(facts["request"] == request.document, "security_evidence_export_changed")
        # The source never uploads authoritative views. Import only the exact
        # locally retained export authorized by this ticket and owner grant.
        from src.products.security_evidence_store import event_id

        from .l5_observations import append as append_decision

        decision_id = event_id(body.ticket_id, "import")
        prior = await db.get(OrchestrationDecision, decision_id)
        document = {"contract": "security-manual-setup-export/v1", "facts": facts, "receipt": reference(exported), **FLAGS}
        if prior is None:
            execution = await db.get(OrchestrationExecution, action.execution_id)
            from types import SimpleNamespace

            append_decision(
                db,
                identity=SimpleNamespace(org_id=action.org_id, flow_id=execution.flow_id, node_id=execution.node_id),
                kind="security_evidence_import",
                actor_id="system:security-release",
                actor_kind="service",
                row_id=decision_id,
                data=document,
            )
        else:
            require(json.loads(prior.reason) == document, "security_evidence_export_changed")
        return document

    return await authorized_read(guard, body.job, claims, transfer)


async def call(request, response, guard, body, operation):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192, "security_evidence_oidc_required", 401)
    claims = await github_claims(token, audience=AUDIENCE)
    try:
        return await operation(guard, body, claims)
    except (CredentialNotFoundError, HTTPException) as error:
        if isinstance(error, CredentialNotFoundError):
            code = "security_evidence_connection_unavailable"
        elif error.status_code == 403 and error.detail == "operations_release_job_refused":
            code = "security_evidence_release_job_refused"
        else:
            raise
        raise HTTPException(403, detail={"code": code, "next_action": NEXT_ACTION[code]}) from None
    except (ObservationError, CycleBlockedError) as error:
        raise HTTPException(
            getattr(error, "status", 409),
            detail={
                "code": getattr(error, "code", "security_evidence_release_changed"),
                "next_action": NEXT_ACTION.get(
                    getattr(error, "code", ""),
                    "Read the exact Security phase and retained receipts. Restore current owner/release authority; "
                    "never repeat an uncertain save or submission.",
                ),
            },
        ) from None


@router.post("/requests")
async def request_evidence(body: Issue, request: Request, response: Response, guard=Depends(service)):
    return await call(request, response, guard, body, issue)


@router.post("/tickets")
async def export_ticket(body: Exchange, request: Request, response: Response, guard=Depends(service)):
    return await call(request, response, guard, body, exchange)


@router.post("/imports")
async def import_evidence(body: Exchange, request: Request, response: Response, guard=Depends(service)):
    async def consume(guard, body, claims):
        return await exchange(guard, body, claims, consume=True)

    return await call(request, response, guard, body, consume)
