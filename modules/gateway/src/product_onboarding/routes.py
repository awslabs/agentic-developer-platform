"""Scoped, resumable Product onboarding without client-owned completion claims."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.product_onboarding.guidance import (
    deterministic_hints,
    metered_model_call,
    pending_proposal_refs,
    validate_model_proposals,
)
from src.product_onboarding.progress import (
    CHECK_SECTIONS,
    profile_progress,
    required_checks,
    section_states,
)
from src.products.configuration import ProjectConfiguration
from src.products.models import (
    Product,
    ProductChange,
    ProductOnboardingSession,
    ProductOperation,
    ProductSourceBinding,
    Project,
)
from src.products.routes import (
    KEY_PATTERN,
    ProductPatch,
    profile,
    record_basic_source_edits,
    record_financial_edits,
    require_profile_admission,
    scoped_product,
    scoped_snapshots,
    validate_financial_targets,
)
from src.shared.database import get_db
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/onboarding-sessions", tags=["products"])


class SessionStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["direct", "guided"] = "direct"
    selected_binding_ids: list[str] = Field(default_factory=list, max_length=10)


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_ids: list[str] = Field(min_length=1, max_length=10)
    cursor: str | None = Field(default=None, max_length=2048)
    adapter: Literal["native", "devlake"] = "native"

    @model_validator(mode="after")
    def unique_bindings(self):
        if len(set(self.binding_ids)) != len(self.binding_ids):
            raise ValueError("Each selected binding must occur once")
        return self


class GuidanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    mode: Literal["deterministic", "model", "financial"] = "deterministic"


class StepUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["save", "confirm", "not_applicable", "later"]
    product: ProductPatch | None = None
    configuration: ProjectConfiguration | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=10000)
    override_until: datetime | None = None
    proposal_id: str | None = None
    confirmed_paths: list[str] | None = None

    @model_validator(mode="after")
    def check_action(self):
        if self.action == "confirm":
            if (
                not self.proposal_id
                or not self.confirmed_paths
                or self.product is not None
                or self.configuration is not None
                or self.reason is not None
                or self.override_until is not None
            ):
                raise ValueError("Confirmation requires a proposal and paths, not replacement values")
            if len(self.confirmed_paths) > 20 or len(set(self.confirmed_paths)) != len(self.confirmed_paths):
                raise ValueError("Confirmation paths must be distinct and bounded")
            return self
        if self.proposal_id is not None or self.confirmed_paths is not None:
            raise ValueError("Proposal fields require a confirmation")
        if self.override_until is not None and (self.action != "save" or not self.reason or not self.reason.strip()):
            raise ValueError("A manual override expiry requires a save reason")
        if self.action != "save" and (self.product is not None or self.configuration is not None):
            raise ValueError("Only save accepts profile values")
        if self.action == "not_applicable" and not (self.reason and self.reason.strip()):
            raise ValueError("Not applicable requires a reason")
        return self


def preconditions(key: str | None, revision: int | None = None, *, needs_revision: bool = True):
    if key is None or (needs_revision and revision is None):
        raise HTTPException(428, detail={"code": "precondition_required"})
    if not KEY_PATTERN.fullmatch(key) or (revision is not None and not 1 <= revision <= 9007199254740991):
        raise HTTPException(422, detail={"code": "validation_error"})


def fingerprint(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def headers(response: Response, revision: int):
    response.headers["Profile-Revision"] = str(revision)
    response.headers["Cache-Control"] = "no-store"


async def authorized_pending_proposals(db, session, product, project, actor, now, *, lock_sources=False, project_conflicts=False):
    if session.state not in ("open", "waiting_external") or (session.expires_at and session.expires_at.replace(tzinfo=UTC) <= now):
        return []
    candidates = []
    for item in session.proposals or []:
        refs = pending_proposal_refs(item, product.revision, now)
        if refs is not None:
            candidates.append((item, refs))
    if not any(refs for _, refs in candidates):
        return [item for item, _ in candidates[-10:]]

    from src.products.source_routes import binding_view, connection_access

    bindings_query = select(ProductSourceBinding).where(
        ProductSourceBinding.tenant_id == actor.org_id,
        ProductSourceBinding.project_id == project.id,
        ProductSourceBinding.unlinked_at.is_(None),
    )
    if lock_sources:
        bindings_query = bindings_query.with_for_update().execution_options(populate_existing=True)
    bindings = (await db.scalars(bindings_query)).all()
    accessible = {}
    for binding in bindings:
        access = await connection_access(db, actor, binding, lock=True) if lock_sources else await connection_access(db, actor, binding)
        view = binding_view(binding, access is not None, now)
        if view["status"] == "healthy" and (binding.observed or {}).get("source_revision"):
            accessible[(binding.installation_id or binding.credential_id, binding.native_id, binding.kind)] = binding.observed["source_revision"]
    authorized = [
        item
        for item, refs in candidates
        if all(
            ref["tenant_id"] == actor.org_id
            and ref["project_id"] == project.id
            and accessible.get((ref["source_connection_id"], ref["record_id"], ref["record_type"])) == ref["record_revision"]
            for ref in refs
        )
    ][-10:]
    if not project_conflicts:
        return authorized
    accepted_paths = {path for proof in project.provenance or [] if proof.get("state") == "accepted" for path in proof.get("paths", [])}
    projected = []
    for item in authorized:
        if "base_values" not in item:
            projected.append(item)
            continue
        current = (project.configuration or {}).get("financials") or {}
        proofs = [
            {**proof, "state": "conflicting"}
            if proof["paths"][0] in accepted_paths
            and proof["paths"][0] in item["remaining_paths"]
            and item["configuration"]["financials"][proof["paths"][0].removeprefix("/configuration/financials/")]
            != current.get(proof["paths"][0].removeprefix("/configuration/financials/"))
            else proof
            for proof in item["provenance"]
        ]
        projected.append({**item, "provenance": proofs})
    return projected


async def session_view(db: AsyncSession, session: ProductOnboardingSession, product: Product, project: Project, actor: TokenContext) -> dict:
    now = utcnow()
    operations = (
        await db.scalars(
            select(ProductOperation)
            .where(
                ProductOperation.tenant_id == session.tenant_id,
                ProductOperation.product_id == product.id,
                ProductOperation.project_id == project.id,
                ProductOperation.session_id == session.id,
                ProductOperation.state.in_(("pending", "running", "waiting_external", "outcome_unknown", "failed", "expired")),
            )
            .order_by(ProductOperation.created_at.desc(), ProductOperation.id.desc())
            .limit(100)
        )
    ).all()
    from src.products.source_routes import source_overlay

    projected, evidence = await source_overlay(db, actor, project, now=now, product_id=product.id)
    progress = profile_progress(product, projected, session.steps, now, source_evidence=evidence)
    steps, operation_ids = section_states(session.steps, progress, operations, projected, now, source_evidence=evidence)
    from src.products.source_routes import discovery_readback

    discovery_operations = (
        await db.scalars(
            select(ProductOperation)
            .where(
                ProductOperation.tenant_id == session.tenant_id,
                ProductOperation.product_id == product.id,
                ProductOperation.project_id == project.id,
                ProductOperation.session_id == session.id,
                ProductOperation.actor_id == session.actor_user_id,
                ProductOperation.operation == "discoverSources",
                ProductOperation.state.in_(("pending", "running", "succeeded", "failed", "cancelled")),
            )
            .order_by(ProductOperation.created_at.desc(), ProductOperation.id.desc())
            .limit(20)
        )
    ).all()
    discoveries = []
    for operation in discovery_operations:
        readback = await discovery_readback(db, actor, operation, project.id)
        discoveries.append(
            {
                "id": operation.id,
                "state": readback["state"],
                "coverage": readback["coverage"],
                "complete": readback["state"] == "succeeded" and readback["coverage"] == "complete",
                "resource_count": len(readback.get("resources", [])),
                **({"cursor": readback["cursor"]} if "cursor" in readback else {}),
                **(
                    {"binding_ids": operation.request["binding_ids"], "adapter": operation.request.get("adapter", "native")}
                    if readback["coverage"] != "denied"
                    else {}
                ),
            }
        )
    if session.state == "cancelled":
        state = "cancelled"
    elif any(step["status"] == "waiting_external" for step in steps.values()):
        state = "waiting_external"
    elif session.state == "completed" and all(step["status"] in ("complete", "not_started", "not_applicable") for step in steps.values()):
        state = "completed"
    else:
        state = "open"
    return {
        "id": session.id,
        "tenant_id": session.tenant_id,
        "product_id": session.product_id,
        "project_id": session.project_id,
        "revision": product.revision,
        "state": state,
        "steps": steps,
        "progress": progress,
        "operation_ids": operation_ids,
        "discoveries": discoveries,
        "resume_path": f"/products/{product.id}/projects/{project.id}/onboarding-sessions/{session.id}",
        "proposals": await authorized_pending_proposals(db, session, product, project, actor, now, project_conflicts=True),
    }


async def scoped_session(db, actor, product_id, project_id, session_id):
    member, product, project = await scoped_product(db, actor, product_id, project_id)
    session = await db.get(ProductOnboardingSession, (actor.org_id, session_id))
    if session is None or session.product_id != product_id or session.project_id != project_id or session.actor_user_id != member.id:
        raise HTTPException(404, detail={"code": "not_found"})
    return member, product, project, session


@router.post("", status_code=201, operation_id="startOnboarding", dependencies=[Depends(require_profile_admission)])
async def start_onboarding(
    product_id: str,
    project_id: str,
    request: SessionStart,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, project = await scoped_product(db, actor, product_id, project_id)
    preconditions(idempotency_key, expected_revision)
    if request.selected_binding_ids:
        raise HTTPException(422, detail={"code": "validation_error", "field": "selected_binding_ids"})
    body = {"product_id": product_id, "project_id": project_id, "request": request.model_dump()}
    key_hash, digest = fingerprint({"key": idempotency_key}), fingerprint(body)
    operation_query = select(ProductOperation).where(
        ProductOperation.tenant_id == actor.org_id,
        ProductOperation.actor_id == member.id,
        ProductOperation.operation == "startOnboarding",
        ProductOperation.key_hash == key_hash,
    )
    existing = await db.scalar(operation_query)
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
        session = await db.get(ProductOnboardingSession, (actor.org_id, existing.session_id))
        if session is None or session.product_id != product_id or session.project_id != project_id:
            raise HTTPException(404, detail={"code": "not_found"})
        response.status_code = 200
        headers(response, product.revision)
        return await session_view(db, session, product, project, actor)
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    session = await db.scalar(
        select(ProductOnboardingSession).where(
            ProductOnboardingSession.tenant_id == actor.org_id,
            ProductOnboardingSession.product_id == product_id,
            ProductOnboardingSession.project_id == project_id,
            ProductOnboardingSession.actor_user_id == member.id,
            ProductOnboardingSession.state.in_(("open", "waiting_external")),
        )
    )
    if session is None:
        session = ProductOnboardingSession(
            tenant_id=actor.org_id,
            product_id=product_id,
            project_id=project_id,
            actor_user_id=member.id,
            last_product_revision=product.revision,
            expires_at=utcnow() + timedelta(days=90),
        )
        db.add(session)
        await db.flush()
        session.steps = {**session.steps, "information": {"status": "complete", "evidence_refs": []}}
    else:
        response.status_code = 200
    db.add(
        ProductOperation(
            tenant_id=actor.org_id,
            product_id=product_id,
            project_id=project_id,
            session_id=session.id,
            actor_id=member.id,
            operation="startOnboarding",
            key_hash=key_hash,
            request_hash=digest,
            request=body,
            receipt={"session_id": session.id, "revision": product.revision},
            expires_at=utcnow() + timedelta(days=1),
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    headers(response, product.revision)
    return await session_view(db, session, product, project, actor)


@router.get("/{session_id}", operation_id="getOnboarding")
async def get_onboarding(
    product_id: str,
    project_id: str,
    session_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    _, product, project, session = await scoped_session(db, actor, product_id, project_id, session_id)
    headers(response, product.revision)
    return await session_view(db, session, product, project, actor)


@router.post("/{session_id}/guidance", operation_id="guideOnboarding", dependencies=[Depends(require_profile_admission)])
async def guide_onboarding(
    product_id: str,
    project_id: str,
    session_id: str,
    request: GuidanceRequest,
    response: Response,
    http_request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, project, session = await scoped_session(db, actor, product_id, project_id, session_id)
    preconditions(idempotency_key, expected_revision)
    if session.state not in ("open", "waiting_external"):
        raise HTTPException(409, detail={"code": "session_closed"})
    if request.mode == "financial":
        from src.product_onboarding.financial_documents import import_suggestions

        result = await import_suggestions(db, actor, member, product, project, session, request, expected_revision, idempotency_key)
        headers(response, result["revision"])
        return result
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    snapshot = await session_view(db, session, product, project, actor)
    hints = deterministic_hints(product, snapshot["progress"])
    result = None
    proposals = []
    mode = "deterministic"
    if request.mode == "model":
        from src.shared.config import get_settings

        model_id = get_settings().product_onboarding_model_id
        if not model_id:
            raise HTTPException(403, detail={"code": "model_not_configured"})
        body = {
            "product_id": product_id,
            "project_id": project_id,
            "session_id": session_id,
            "expected_revision": expected_revision,
            "request": request.model_dump(),
        }
        key_hash, digest = fingerprint({"key": idempotency_key}), fingerprint(body)
        existing = await db.scalar(
            select(ProductOperation).where(
                ProductOperation.tenant_id == actor.org_id,
                ProductOperation.actor_id == member.id,
                ProductOperation.operation == "guideOnboarding",
                ProductOperation.key_hash == key_hash,
            )
        )
        if existing is not None:
            if existing.request_hash != digest:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            if existing.session_id != session.id:
                raise HTTPException(404, detail={"code": "not_found"})
            if existing.expires_at.replace(tzinfo=UTC) <= utcnow():
                raise HTTPException(410, detail={"code": "operation_expired"})
            mode = "model" if existing.state == "succeeded" else "deterministic"
            proposal_ids = set((existing.receipt or {}).get("proposal_ids", []))
            proposals = [item for item in snapshot["proposals"] if item["id"] in proposal_ids] if mode == "model" else []
        else:
            operation = ProductOperation(
                tenant_id=actor.org_id,
                product_id=product_id,
                project_id=project_id,
                session_id=session.id,
                actor_id=member.id,
                operation="guideOnboarding",
                key_hash=key_hash,
                request_hash=digest,
                request={"mode": "model", "revision": expected_revision},
                state="running",
                expires_at=utcnow() + timedelta(days=1),
            )
            db.add(operation)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise HTTPException(409, detail={"code": "outcome_unknown"}) from None
            try:
                result = await metered_model_call(http_request, model_id, request.message, hints, product)
                proposals = validate_model_proposals(result, model_id, expected_revision)
            except (TimeoutError, httpx.TimeoutException):
                operation.state = "outcome_unknown"
                operation.receipt = {"model": model_id, "actor_id": member.id, "status": "outcome_unknown"}
            except ValueError:
                operation.state = "failed"
                operation.receipt = {"model": model_id, "actor_id": member.id, "status": "invalid_output"}
            except (OSError, httpx.HTTPError):
                operation.state = "failed"
                operation.receipt = {"model": model_id, "actor_id": member.id, "status": "unavailable"}
            else:
                db.expire_all()
                member, product, project, session = await scoped_session(db, actor, product_id, project_id, session_id)
                if product.revision != expected_revision or session.state not in ("open", "waiting_external"):
                    proposals = []
                    operation.state = "failed"
                    operation.receipt = {"model": model_id, "actor_id": member.id, "status": "stale"}
                elif len(session.proposals or []) + len(proposals) > 100:
                    proposals = []
                    operation.state = "failed"
                    operation.receipt = {"model": model_id, "actor_id": member.id, "status": "capacity"}
                else:
                    session.proposals = (session.proposals or []) + proposals
                    operation.state = "succeeded"
                    operation.receipt = {
                        "model": model_id,
                        "actor_id": member.id,
                        "status": "succeeded",
                        "model_request_id": str(result.get("id", ""))[:255],
                        "proposal_ids": [item["id"] for item in proposals],
                    }
                    mode = "model"
            await db.commit()
        db.expire_all()
        _, product, project, session = await scoped_session(db, actor, product_id, project_id, session_id)
        snapshot = await session_view(db, session, product, project, actor)
        hints = deterministic_hints(product, snapshot["progress"])
        proposal_ids = {item["id"] for item in proposals}
        proposals = [item for item in snapshot["proposals"] if item["id"] in proposal_ids]
    headers(response, product.revision)
    return {
        "revision": product.revision,
        "message": "\n".join(hints) if hints else "No missing steps in the current scope.",
        "proposals": proposals,
        "mode": mode,
    }


@router.post("/{session_id}/discoveries", status_code=202, operation_id="discoverSources", dependencies=[Depends(require_profile_admission)])
async def discover_sources(
    product_id: str,
    project_id: str,
    session_id: str,
    request: DiscoveryRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.product_onboarding.discovery import (
        configured_devlake,
        connection_fence,
        connector_enabled,
        progress,
        run_attempt,
        valid_page,
    )
    from src.products.models import ProductSourceBinding
    from src.products.source_routes import (
        binding_view,
        collection_connection_access,
        connection_access,
    )

    member, product, _, session = await scoped_session(db, actor, product_id, project_id, session_id)
    preconditions(idempotency_key, expected_revision)
    if request.adapter == "devlake":
        for binding_id in request.binding_ids:
            binding = await db.get(ProductSourceBinding, (actor.org_id, binding_id))
            if (
                binding is None
                or binding.project_id != project_id
                or binding.unlinked_at is not None
                or await collection_connection_access(db, actor, binding, product_id) is None
            ):
                raise HTTPException(404, detail={"code": "not_found"})
        if not await connector_enabled(db, actor.org_id):
            raise HTTPException(503, detail={"code": "feature_disabled"})
        if configured_devlake() is None:
            raise HTTPException(503, detail={"code": "connector_unavailable"})
    digest = fingerprint(
        {
            "product_id": product_id,
            "project_id": project_id,
            "session_id": session_id,
            "revision": expected_revision,
            "request": request.model_dump(exclude={"adapter"} if request.adapter == "native" else None),
        }
    )
    key_hash = fingerprint({"key": idempotency_key})
    previous = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "discoverSources",
            ProductOperation.key_hash == key_hash,
        )
    )
    if previous is not None:
        if previous.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
        if previous.request_hash != digest or previous.session_id != session_id:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        from src.products.source_routes import get_operation

        response.headers["Location"] = f"/products/{product_id}/projects/{project_id}/operations/{previous.id}"
        return await get_operation(product_id, project_id, previous.id, response, db, actor)
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    if product.archived_at is not None or session.state not in ("open", "waiting_external"):
        raise HTTPException(409, detail={"code": "session_closed"})
    parent = None
    if request.cursor is not None:
        parent = await db.get(ProductOperation, (actor.org_id, request.cursor), with_for_update=True)
        if (
            parent is None
            or parent.actor_id != member.id
            or parent.operation != "discoverSources"
            or parent.product_id != product_id
            or parent.session_id != session_id
            or parent.project_id != project_id
            or parent.request.get("binding_ids") != request.binding_ids
            or parent.request.get("adapter", "native") != request.adapter
            or not (parent.receipt or {}).get("cursor")
            or (parent.receipt or {}).get("consumed")
        ):
            raise HTTPException(400, detail={"code": "invalid_cursor"})
        if parent.state == "running" and parent.lease_until is not None and parent.lease_until.replace(tzinfo=UTC) > utcnow():
            raise HTTPException(409, detail={"code": "attempt_in_progress"})
        if parent.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
    bindings = []
    fences = []
    source_revisions = []
    for binding_id in request.binding_ids:
        binding = await db.get(ProductSourceBinding, (actor.org_id, binding_id))
        if (
            binding is None
            or binding.project_id != project_id
            or binding.unlinked_at is not None
            or (
                await collection_connection_access(db, actor, binding, product_id)
                if request.adapter == "devlake"
                else await connection_access(db, actor, binding)
            )
            is None
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        bindings.append(binding)
        if request.adapter == "devlake":
            connection = await collection_connection_access(db, actor, binding, product_id)
            if connection is None or binding.provider not in (
                "github",
                "atlassian-mcp",
                "jenkins",
                "sonarqube",
                "argocd",
                "gitlab",
                "pagerduty",
                "linear",
                "slack",
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            source_revision = (binding.observed or {}).get("source_revision")
            if binding_view(binding, True)["status"] != "healthy" or not isinstance(source_revision, str) or not source_revision:
                raise HTTPException(404, detail={"code": "not_found"})
            source_revisions.append(source_revision)
            pinned_connection = json.dumps([str(value) for value in connection_fence(connection)], separators=(",", ":"))
            fences.append(hashlib.sha256(pinned_connection.encode()).hexdigest())
    if parent is not None and request.adapter == "devlake" and parent.request.get("connection_fences") != fences:
        raise HTTPException(400, detail={"code": "invalid_cursor"})
    if parent is not None and request.adapter == "devlake" and parent.request.get("source_revisions") != source_revisions:
        raise HTTPException(400, detail={"code": "invalid_cursor"})
    if parent is not None and request.adapter == "devlake":
        receipt = parent.receipt
        index = receipt.get("index")
        if (
            parent.expected_revision != expected_revision
            or parent.request.get("consumer") != configured_devlake()[1]
            or parent.state not in ("running", "succeeded")
            or (parent.state == "running" and parent.lease_until is None)
            or receipt.get("coverage") not in ("partial", "unavailable")
            or type(index) is not int
            or not 0 <= index < len(bindings)
            or not valid_page(receipt.get("page"), bindings[index])
        ):
            raise HTTPException(400, detail={"code": "invalid_cursor"})
    if parent is not None and parent.request.get("generations") != [binding.generation for binding in bindings]:
        raise HTTPException(400, detail={"code": "invalid_cursor"})
    operation = ProductOperation(
        tenant_id=actor.org_id,
        actor_id=member.id,
        product_id=product_id,
        project_id=project_id,
        session_id=session_id,
        operation="discoverSources",
        key_hash=key_hash,
        request_hash=digest,
        state="pending",
        expected_revision=expected_revision,
        request={
            "binding_ids": request.binding_ids,
            "generations": [binding.generation for binding in bindings],
            "adapter": request.adapter,
            **(
                {"connection_fences": fences, "source_revisions": source_revisions, "consumer": configured_devlake()[1]}
                if request.adapter == "devlake"
                else {}
            ),
        },
        receipt=progress(parent.receipt if parent is not None else {}),
        expires_at=utcnow() + timedelta(days=1),
    )
    operation.receipt["cursor"] = True
    db.add(operation)
    if parent is not None:
        parent.receipt = {**parent.receipt, "consumed": True}
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    await run_attempt(db, actor, operation)
    operation_id = operation.id
    db.expire_all()
    from src.products.source_routes import get_operation

    result = await get_operation(product_id, project_id, operation_id, response, db, actor)
    response.headers["Location"] = f"/products/{product_id}/projects/{project_id}/operations/{operation_id}"
    return result


@router.put("/{session_id}/steps/{step_id}", operation_id="updateStep", dependencies=[Depends(require_profile_admission)])
async def update_step(
    product_id: str,
    project_id: str,
    session_id: str,
    step_id: Literal["information", "basic", "advanced", "financials"],
    request: StepUpdate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, project, session = await scoped_session(db, actor, product_id, project_id, session_id)
    preconditions(idempotency_key, expected_revision)
    if session.state not in ("open", "waiting_external"):
        raise HTTPException(409, detail={"code": "session_closed"})
    if request.action == "not_applicable" and (
        step_id == "information" or any(section == step_id and check_id in required_checks(product) for check_id, section in CHECK_SECTIONS.items())
    ):
        raise HTTPException(422, detail={"code": "required_check"})
    if request.action == "save":
        if step_id == "information":
            if request.product is None or request.configuration is not None:
                raise HTTPException(422, detail={"code": "validation_error"})
            changed = request.product.model_dump(exclude_unset=True)
            allowed = {"name", "purpose", "intended_users", "domain", "goals"}
            if not changed or any(field not in allowed or value is None for field, value in changed.items()):
                raise HTTPException(422, detail={"code": "validation_error"})
        elif request.configuration is None or request.product is not None or set(request.configuration.model_fields_set) != {step_id}:
            raise HTTPException(422, detail={"code": "validation_error"})
        if request.override_until is not None and (
            step_id != "financials" or request.override_until.tzinfo is None or request.override_until <= utcnow()
        ):
            raise HTTPException(422, detail={"code": "validation_error", "field": "override_until"})
        if step_id == "financials":
            old_financials = project.configuration.get("financials") or {}
            new_financials = request.configuration.financials.model_dump(mode="json", exclude_none=True)
            imported_paths = {
                path
                for proof in project.provenance or []
                if proof.get("state") == "accepted" and proof.get("method") == "connector" and proof.get("confirmed_by")
                for path in proof.get("paths", [])
            }
            replacing_import = any(
                f"/configuration/financials/{field}" in imported_paths and old_financials.get(field) != new_financials.get(field)
                for field in old_financials.keys() | new_financials.keys()
            )
            if replacing_import and not (request.reason and request.reason.strip()):
                raise HTTPException(422, detail={"code": "override_reason_required"})
    if request.action == "confirm" and step_id != "financials":
        raise HTTPException(422, detail={"code": "validation_error"})
    body = {
        "product_id": product_id,
        "project_id": project_id,
        "session_id": session_id,
        "step_id": step_id,
        "expected_revision": expected_revision,
        "request": request.model_dump(mode="json", exclude_none=True),
    }
    key_hash, digest = fingerprint({"key": idempotency_key}), fingerprint(body)
    operation_query = select(ProductOperation).where(
        ProductOperation.tenant_id == actor.org_id,
        ProductOperation.actor_id == member.id,
        ProductOperation.operation == "updateStep",
        ProductOperation.key_hash == key_hash,
    )
    existing = await db.scalar(operation_query)
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.expires_at.replace(tzinfo=UTC) <= utcnow() or existing.receipt is None:
            raise HTTPException(410, detail={"code": "operation_expired"})
        headers(response, product.revision)
        return await session_view(db, session, product, project, actor)
    if product.revision != expected_revision and request.action != "confirm":
        raise HTTPException(412, detail={"code": "stale_revision"})
    selected = None
    candidate = None
    if request.action == "confirm":
        candidate = next((item for item in session.proposals or [] if isinstance(item, dict) and item.get("id") == request.proposal_id), None)
        if candidate is None:
            raise HTTPException(404, detail={"code": "not_found"})
        if "base_values" not in candidate or pending_proposal_refs(candidate, product.revision, utcnow()) is None:
            raise HTTPException(410, detail={"code": "proposal_expired"})
        if candidate not in await authorized_pending_proposals(db, session, product, project, actor, utcnow(), lock_sources=True):
            raise HTTPException(403, detail={"code": "permission_denied"})
        offered = set(candidate["remaining_paths"])
        if any(path not in offered for path in request.confirmed_paths):
            raise HTTPException(422, detail={"code": "validation_error", "field": "confirmed_paths"})
        selected = {path.removeprefix("/configuration/financials/"): path for path in request.confirmed_paths}
        current_financials = (project.configuration or {}).get("financials") or {}
        conflicts = [
            path
            for path in request.confirmed_paths
            if current_financials.get(path.removeprefix("/configuration/financials/")) != candidate["base_values"][path]
        ]
        if conflicts:
            raise HTTPException(412, detail={"code": "stale_revision", "paths": conflicts})
        merged = {**current_financials, **{field: candidate["configuration"]["financials"][field] for field in selected}}
        await validate_financial_targets(db, ProjectConfiguration.model_validate({"financials": merged}).financials, product, project, actor)
    if request.action == "save" and step_id == "financials":
        await validate_financial_targets(db, request.configuration.financials, product, project, actor)
    next_steps = dict(session.steps)
    previous_configuration = project.configuration
    values_changed = False
    if request.action == "confirm":
        project.configuration = {**project.configuration, "financials": merged}
        project.configuration["applicability"] = [
            choice for choice in project.configuration.get("applicability", []) if choice["check_id"] != "financials"
        ]
        next_steps[step_id] = {"status": "in_progress", "evidence_refs": []}
        updated = dict(candidate)
        updated["remaining_paths"] = [path for path in candidate["remaining_paths"] if path not in request.confirmed_paths]
        session.proposals = [updated if item["id"] == candidate["id"] else item for item in session.proposals]
    elif request.action == "save":
        if step_id == "information":
            changed = {field: value for field, value in changed.items() if getattr(product, field) != value}
            values_changed = bool(changed)
            for field, value in changed.items():
                setattr(product, field, value)
        else:
            section = getattr(request.configuration, step_id).model_dump(mode="json", exclude_none=True)
            values_changed = project.configuration.get(step_id) != section
            project.configuration = {**project.configuration, step_id: section}
            if step_id == "basic":
                values_changed = (
                    record_basic_source_edits(project, product, previous_configuration, project.configuration, member.id, product.revision + 1)
                    or values_changed
                )
            if step_id == "financials":
                project.configuration["applicability"] = [
                    choice for choice in project.configuration.get("applicability", []) if choice["check_id"] != "financials"
                ]
        next_steps[step_id] = {"status": "complete" if step_id == "information" else "in_progress", "evidence_refs": []}
    else:
        next_steps[step_id] = {"status": request.action, "evidence_refs": [], **({"reason": request.reason.strip()} if request.reason else {})}
        if step_id == "financials" and request.action in ("later", "not_applicable"):
            remaining = [choice for choice in project.configuration.get("applicability", []) if choice["check_id"] != "financials"]
            project.configuration = {
                **project.configuration,
                "applicability": [
                    *remaining,
                    {"check_id": "financials", "state": request.action, "reason": request.reason.strip() if request.reason else "Deferred."},
                ],
            }
    if previous_configuration != project.configuration:
        record_financial_edits(project, product, previous_configuration, project.configuration, member.id, product.revision + 1)
        if request.action == "save" and request.override_until is not None:
            changed_paths = {
                f"/configuration/financials/{field}"
                for field in (previous_configuration.get("financials") or {}).keys() | (project.configuration.get("financials") or {}).keys()
                if (previous_configuration.get("financials") or {}).get(field) != (project.configuration.get("financials") or {}).get(field)
            }
            project.provenance = [
                {**proof, "override_until": request.override_until.isoformat().replace("+00:00", "Z")}
                if proof["method"] == "manual" and any(path in changed_paths for path in proof["paths"])
                else proof
                for proof in project.provenance
            ]
    if request.action == "confirm":
        retained = [proof for proof in project.provenance if not any(path in request.confirmed_paths for path in proof["paths"])]
        timestamp = utcnow().isoformat().replace("+00:00", "Z")
        project.provenance = retained + [
            {**proof, "state": "accepted", "confirmed_by": {"kind": "user", "id": member.id}, "confirmed_at": timestamp}
            for proof in candidate["provenance"]
            if proof["paths"][0] in request.confirmed_paths
        ]
    unchanged = request.action != "confirm" and next_steps == session.steps and not values_changed and previous_configuration == project.configuration
    if unchanged:
        result = await session_view(db, session, product, project, actor)
        revision = product.revision
    else:
        current_revision = product.revision
        claimed = await db.execute(
            update(Product)
            .where(
                Product.tenant_id == actor.org_id,
                Product.id == product_id,
                Product.revision == current_revision,
            )
            .values(revision=current_revision + 1)
            .returning(Product.id)
        )
        if claimed.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(412, detail={"code": "stale_revision"})
        product.revision = current_revision + 1
        session.steps = next_steps
        session.last_product_revision = product.revision
        session.updated_at = utcnow()
        revision = product.revision
        result = await session_view(db, session, product, project, actor)
    operation = ProductOperation(
        tenant_id=actor.org_id,
        product_id=product_id,
        project_id=project_id,
        session_id=session_id,
        actor_id=member.id,
        operation="updateStep",
        key_hash=key_hash,
        request_hash=digest,
        request=body,
        receipt={"revision": revision, "session": result},
        expires_at=utcnow() + timedelta(days=1),
    )
    db.add(operation)
    await db.flush()
    if revision != expected_revision:
        shared_product_change = step_id == "information" and values_changed
        snapshot = profile(product, project)
        changed_paths = [f"/steps/{step_id}"]
        if shared_product_change:
            snapshot = {**snapshot, **await scoped_snapshots(db, product)}
            changed_paths.extend(f"/product/{field}" for field in changed)
        if previous_configuration != project.configuration:
            changed_paths.append("/project/configuration")
        if request.action == "confirm":
            changed_paths.extend(request.confirmed_paths)
        db.add(
            ProductChange(
                tenant_id=actor.org_id,
                product_id=product_id,
                project_id=None if shared_product_change else project_id,
                revision=revision,
                actor_id=member.id,
                operation_id=operation.id,
                changed_paths=changed_paths,
                snapshot=snapshot,
                source_revisions=[],
                relationship_versions=[],
            )
        )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    headers(response, revision)
    return result


class SourceEnrichmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=36)


@router.post("/{session_id}/source-enrichment", operation_id="applySourceEnrichment", dependencies=[Depends(require_profile_admission)])
async def apply_source_enrichment(
    product_id: str,
    project_id: str,
    session_id: str,
    request: SourceEnrichmentRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.product_onboarding.discovery import connection_fence, connector_enabled
    from src.product_onboarding.enrichment import selected_native_id, selected_source_fact, source_changes
    from src.products.source_routes import (
        binding_view,
        collection_connection_access,
        get_operation,
    )

    member, product, project, session = await scoped_session(db, actor, product_id, project_id, session_id)
    preconditions(idempotency_key, expected_revision)
    if not await connector_enabled(db, actor.org_id, lock=True):
        raise HTTPException(503, detail={"code": "feature_disabled"})
    body = {"operation_id": request.operation_id, "revision": expected_revision, "session_id": session_id}
    key_hash, digest = fingerprint({"key": idempotency_key}), fingerprint(body)
    prior = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "applySourceEnrichment",
            ProductOperation.key_hash == key_hash,
        )
    )
    if prior is not None:
        if prior.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if prior.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
        discovery = await get_operation(product_id, project_id, request.operation_id, response, db, actor)
        if discovery["coverage"] == "denied":
            raise HTTPException(403, detail={"code": "source_access_denied"})
        headers(response, product.revision)
        return prior.receipt
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    operation = await db.get(ProductOperation, (actor.org_id, request.operation_id))
    if (
        session.state not in ("open", "waiting_external")
        or product.archived_at is not None
        or operation is None
        or operation.actor_id != member.id
        or operation.product_id != product_id
        or operation.project_id != project_id
        or operation.session_id != session_id
        or operation.operation != "discoverSources"
        or operation.request.get("adapter") != "devlake"
        or operation.state != "succeeded"
        or operation.expires_at.replace(tzinfo=UTC) <= utcnow()
    ):
        raise HTTPException(404, detail={"code": "discovery_unavailable"})
    receipt = operation.receipt or {}
    if receipt.get("coverage") not in ("complete", "partial") or receipt.get("cursor"):
        raise HTTPException(409, detail={"code": "discovery_incomplete"})
    binding_ids = operation.request.get("binding_ids")
    generations = operation.request.get("generations")
    source_revisions = operation.request.get("source_revisions")
    connection_fences = operation.request.get("connection_fences")
    resources = receipt.get("resources")
    if (
        not isinstance(binding_ids, list)
        or not 1 <= len(binding_ids) <= 10
        or not isinstance(generations, list)
        or len(generations) != len(binding_ids)
        or not isinstance(source_revisions, list)
        or len(source_revisions) != len(binding_ids)
        or not isinstance(connection_fences, list)
        or len(connection_fences) != len(binding_ids)
        or not isinstance(resources, list)
        or len(resources) > 500
    ):
        raise HTTPException(409, detail={"code": "discovery_incomplete"})
    bindings = {}
    for binding_id, generation, source_revision, connection_pin in zip(
        binding_ids,
        generations,
        source_revisions,
        connection_fences,
        strict=True,
    ):
        binding = await db.get(ProductSourceBinding, (actor.org_id, binding_id), with_for_update=True, populate_existing=True)
        access = await collection_connection_access(db, actor, binding, product_id, lock=True) if binding is not None else None
        if (
            binding is None
            or binding.project_id != project_id
            or binding.generation != generation
            or binding_view(binding, access is not None)["status"] != "healthy"
        ):
            raise HTTPException(403, detail={"code": "source_access_denied"})
        current_connection_pin = hashlib.sha256(
            json.dumps([str(value) for value in connection_fence(access)], separators=(",", ":")).encode()
        ).hexdigest()
        if current_connection_pin != connection_pin:
            raise HTTPException(403, detail={"code": "source_access_denied"})
        if not isinstance(source_revision, str) or not source_revision or (binding.observed or {}).get("source_revision") != source_revision:
            raise HTTPException(409, detail={"code": "source_changed"})
        bindings[binding_id] = binding
    facts = []
    for item in resources:
        if not isinstance(item, dict) or item.get("binding_id") not in bindings:
            raise HTTPException(409, detail={"code": "discovery_incomplete"})
        binding = bindings[item["binding_id"]]
        if (item.get("kind"), item.get("namespace"), item.get("native_id")) != (
            binding.kind,
            binding.namespace,
            selected_native_id(binding),
        ) or selected_native_id(binding) is None:
            continue
        fact = await selected_source_fact(db, actor, binding, item, receipt["coverage"])
        if fact is None:
            raise HTTPException(409, detail={"code": "source_changed"})
        facts.append(fact)
    previous_provenance = project.provenance or []
    configuration, provenance, conflicts, suggestions = source_changes(project.configuration or {}, previous_provenance, facts)
    changed = configuration != project.configuration
    next_revision = product.revision + int(changed)
    if changed:
        updated = await db.execute(
            update(Product)
            .where(
                Product.tenant_id == actor.org_id,
                Product.id == product_id,
                Product.revision == expected_revision,
            )
            .values(revision=next_revision, updated_at=utcnow())
        )
        if updated.rowcount != 1:
            raise HTTPException(412, detail={"code": "stale_revision"})
        product.revision = next_revision
        project.configuration = configuration
        project.provenance = provenance
    result = {
        "revision": next_revision,
        "applied": [proof for proof in provenance if proof not in previous_provenance],
        "conflicts": conflicts,
        "suggestions": suggestions,
        "coverage": receipt["coverage"],
    }
    applied_operation = ProductOperation(
        tenant_id=actor.org_id,
        actor_id=member.id,
        product_id=product_id,
        project_id=project_id,
        session_id=session_id,
        operation="applySourceEnrichment",
        key_hash=key_hash,
        request_hash=digest,
        request=body,
        receipt=result,
        expires_at=utcnow() + timedelta(days=1),
    )
    db.add(applied_operation)
    await db.flush()
    if changed:
        db.add(
            ProductChange(
                tenant_id=actor.org_id,
                product_id=product_id,
                project_id=project_id,
                revision=next_revision,
                actor_id=member.id,
                operation_id=applied_operation.id,
                changed_paths=["/project/configuration"],
                snapshot=profile(product, project),
                source_revisions=[proof["source_refs"][0] for proof in result["applied"]],
                relationship_versions=[],
            )
        )
    await db.commit()
    headers(response, next_revision)
    return result
