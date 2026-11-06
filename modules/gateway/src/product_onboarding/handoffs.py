"""Durable requests for an existing administrator or connection workflow."""

from datetime import UTC, timedelta
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.connections.github_client import github_account_id
from src.admin.installations.resolver import InstallationOwnershipError, assert_installation_owned_by
from src.auth.dependencies import get_current_user
from src.auth.vault_service import _visible_credential_filter
from src.product_onboarding.callback_auth import PRODUCER
from src.product_onboarding.routes import fingerprint, headers, preconditions, scoped_session
from src.products.models import ProductOperation
from src.products.routes import require_profile_admission
from src.shared.database import get_db
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import ChannelTenantMap, UserCredential
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}", tags=["products"])


class RepositoryReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=255)
    project_id: str = Field(min_length=1, max_length=255)
    source_connection_id: str = Field(min_length=1, max_length=512)
    record_type: str = Field(min_length=1, max_length=200)
    record_id: str = Field(min_length=1, max_length=512)
    record_revision: str = Field(min_length=1, max_length=512)


class RepositoryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_ref: dict[str, str]
    owner_native_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    visibility: Literal["private", "internal", "public"]
    template_ref: RepositoryReference
    protections_ref: RepositoryReference
    confirmed: bool = False

    @model_validator(mode="after")
    def valid_connection(self):
        if set(self.connection_ref) != {"kind", "id"} or self.connection_ref["kind"] != "github_installation":
            raise ValueError("Repository creation requires a GitHub installation")
        if not self.connection_ref["id"] or len(self.connection_ref["id"]) > 36:
            raise ValueError("Invalid installation reference")
        if self.name in (".", ".."):
            raise ValueError("Invalid repository name")
        return self


class RepositoryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_plan: RepositoryPlan

    @model_validator(mode="after")
    def unconfirmed(self):
        if self.repository_plan.confirmed:
            raise ValueError("Preview cannot confirm a repository")
        return self


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["administrator", "github_connection", "aws_connection", "oauth_connection", "repository_create"]
    action: Literal["assign_team", "connect", "create_repository"]
    binding_id: str | None = Field(default=None, min_length=1, max_length=36)
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    preview_id: str | None = Field(default=None, min_length=1, max_length=36)
    repository_plan: RepositoryPlan | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.target == "repository_create":
            if (
                self.action != "create_repository"
                or self.binding_id is not None
                or self.provider is not None
                or self.preview_id is None
                or self.repository_plan is None
                or not self.repository_plan.confirmed
            ):
                raise ValueError("Repository creation requires an explicit confirmed preview")
        elif self.preview_id is not None or self.repository_plan is not None:
            raise ValueError("Only repository creation accepts a repository plan")
        elif self.target == "administrator":
            if self.action != "assign_team" or self.binding_id is not None or self.provider is not None:
                raise ValueError("Administrator handoffs assign a team without a source binding")
        elif self.action != "connect" or self.binding_id is not None:
            raise ValueError("Connection handoffs connect before a source binding exists")
        elif self.target == "oauth_connection" and self.provider is None:
            raise ValueError("OAuth connection handoffs require a provider")
        elif self.target != "oauth_connection" and self.provider is not None:
            raise ValueError("Only OAuth handoffs select a provider")
        return self


async def verify_repository_plan(db: AsyncSession, actor: TokenContext, plan: RepositoryPlan, project_id: str):
    connection_id = plan.connection_ref["id"]
    mapping = await db.get(ChannelTenantMap, connection_id)
    if (
        mapping is None
        or mapping.org_id != actor.org_id
        or mapping.provider != "github"
        or mapping.ownership_disputed
        or not github_account_id(mapping.installation_id)
        or mapping.provider_scope_id != plan.owner_native_id
        or (mapping.provider_scope_id.startswith("personal:") and mapping.installed_by_user_id != actor.user_id)
    ):
        raise HTTPException(403, detail={"code": "permission_denied"})
    for reference in (plan.template_ref, plan.protections_ref):
        if (
            reference.tenant_id != actor.org_id
            or reference.project_id != project_id
            or reference.source_connection_id != connection_id
            or reference.record_revision in ("latest", "null")
        ):
            raise HTTPException(409, detail={"code": "stale_revision"})
    try:
        await assert_installation_owned_by(actor.org_id, int(mapping.installation_id), db=db)
    except InstallationOwnershipError:
        raise HTTPException(403, detail={"code": "permission_denied"}) from None
    from src.product_onboarding.repository_creation import validate_repository_source

    try:
        await validate_repository_source(int(mapping.installation_id), plan.model_dump())
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, detail={"code": "source_unavailable"}) from None
    return mapping


async def resumable_repository(db: AsyncSession, actor: TokenContext, member_id: str, project_id: str, plan: RepositoryPlan, installation_id: int):
    from src.product_onboarding.repository_creation import reconcile_repository

    pending = (
        await db.scalars(
            select(ProductOperation)
            .where(
                ProductOperation.tenant_id == actor.org_id,
                ProductOperation.project_id == project_id,
                ProductOperation.actor_id == member_id,
                ProductOperation.operation == "startHandoff",
                ProductOperation.state == "outcome_unknown",
            )
            .order_by(ProductOperation.created_at.desc())
            .limit(100)
            .with_for_update()
        )
    ).all()
    for previous in pending:
        old_plan = previous.request.get("request", {}).get("repository_plan")
        if not isinstance(old_plan, dict) or previous.request.get("request", {}).get("target") != "repository_create":
            continue
        if (
            old_plan.get("connection_ref") != plan.connection_ref
            or old_plan.get("owner_native_id") != plan.owner_native_id
            or old_plan.get("name") != plan.name
            or old_plan.get("template_ref", {}).get("record_id") != plan.template_ref.record_id
            or old_plan.get("protections_ref", {}).get("record_id") != plan.protections_ref.record_id
        ):
            continue
        origin_id = previous.request.get("resume_operation_id") or previous.id
        try:
            observed = await reconcile_repository(installation_id, old_plan, origin_id)
        except (httpx.HTTPError, OSError, ValueError):
            raise HTTPException(503, detail={"code": "source_unavailable"}) from None
        if observed is None:
            raise HTTPException(409, detail={"code": "outcome_unknown"})
        if previous.provider_operation_id not in (None, observed["native_id"]):
            raise HTTPException(409, detail={"code": "effect_identity_conflict"})
        if observed["coverage"] == "complete":
            raise HTTPException(409, detail={"code": "repository_exists"})
        previous.provider_operation_id = observed["native_id"]
        previous.receipt = observed
        previous.state = "cancelled"
        previous.updated_at = utcnow()
        return origin_id, observed
    return None


@router.get(
    "/onboarding-sessions/{session_id}/repository-options",
    operation_id="repositoryOptions",
    dependencies=[Depends(require_profile_admission)],
)
async def get_repository_options(
    product_id: str,
    project_id: str,
    session_id: str,
    connection_id: str = Query(min_length=1, max_length=36),
    template_id: str = Query(pattern=r"^[1-9][0-9]{0,19}$"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, _, session = await scoped_session(db, actor, product_id, project_id, session_id)
    if member.id != session.actor_user_id or product.archived_at is not None:
        raise HTTPException(403, detail={"code": "permission_denied"})
    mapping = await db.get(ChannelTenantMap, connection_id)
    if (
        mapping is None
        or mapping.org_id != actor.org_id
        or mapping.provider != "github"
        or mapping.ownership_disputed
        or not github_account_id(mapping.installation_id)
        or not github_account_id(mapping.provider_scope_id)
    ):
        raise HTTPException(403, detail={"code": "permission_denied"})
    try:
        await assert_installation_owned_by(actor.org_id, int(mapping.installation_id), db=db)
    except InstallationOwnershipError:
        raise HTTPException(403, detail={"code": "permission_denied"}) from None
    from src.product_onboarding.repository_creation import repository_options

    try:
        versions = await repository_options(int(mapping.installation_id), mapping.provider_scope_id, template_id)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, detail={"code": "source_unavailable"}) from None
    reference = {"tenant_id": actor.org_id, "project_id": project_id, "source_connection_id": connection_id, "record_id": template_id}
    return {
        "connection_ref": {"kind": "github_installation", "id": connection_id},
        "owner_native_id": mapping.provider_scope_id,
        "template_ref": {**reference, "record_type": "repository", "record_revision": versions["template_revision"]},
        "protections_ref": {**reference, "record_type": "repository_protections", "record_revision": versions["protections_revision"]},
        "protections": versions["protections"],
    }


@router.post(
    "/onboarding-sessions/{session_id}/repository-plans",
    status_code=201,
    operation_id="previewRepository",
    dependencies=[Depends(require_profile_admission)],
)
async def preview_repository(
    product_id: str,
    project_id: str,
    session_id: str,
    request: RepositoryPreviewRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, _, session = await scoped_session(db, actor, product_id, project_id, session_id)
    if member.id != session.actor_user_id or session.state not in ("open", "waiting_external"):
        raise HTTPException(403, detail={"code": "permission_denied"})
    if product.archived_at is not None:
        raise HTTPException(409, detail={"code": "product_archived"})
    preconditions(idempotency_key, expected_revision)
    plan = request.repository_plan
    body = {"product_id": product_id, "project_id": project_id, "session_id": session_id, "plan": plan.model_dump()}
    key_hash, digest = fingerprint({"key": idempotency_key}), fingerprint(body)
    existing = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "repositoryPreview",
            ProductOperation.key_hash == key_hash,
        )
    )
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
        response.status_code = 200
        headers(response, product.revision)
        return {"id": existing.id, "revision": existing.expected_revision, "repository_plan": existing.request["plan"]}
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    await verify_repository_plan(db, actor, plan, project_id)
    preview = ProductOperation(
        tenant_id=actor.org_id,
        product_id=product_id,
        project_id=project_id,
        session_id=session_id,
        actor_id=member.id,
        operation="repositoryPreview",
        expected_revision=expected_revision,
        key_hash=key_hash,
        request_hash=digest,
        state="pending",
        request=body,
        expires_at=utcnow() + timedelta(days=1),
    )
    db.add(preview)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    headers(response, product.revision)
    return {"id": preview.id, "revision": expected_revision, "repository_plan": plan.model_dump()}


def handoff_view(operation: ProductOperation, revision: int) -> dict:
    view = {
        "id": operation.id,
        "tenant_id": operation.tenant_id,
        "product_id": operation.product_id,
        "project_id": operation.project_id,
        "revision": revision,
        "state": operation.state,
        "kind": operation.operation,
        "next_action": (
            "Repository created; verify the recorded native ID."
            if operation.request.get("request", {}).get("target") == "repository_create" and operation.state == "succeeded"
            else "Repository outcome is uncertain; reconcile before attempting another creation."
            if operation.request.get("request", {}).get("target") == "repository_create" and operation.state == "outcome_unknown"
            else "Repository creation has not started."
            if operation.request.get("request", {}).get("target") == "repository_create"
            else "Complete the request using the existing administrator or connection workflow."
        ),
        "resume_path": f"/products/{operation.product_id}/projects/{operation.project_id}/onboarding-sessions/{operation.session_id}",
    }
    if operation.request.get("request", {}).get("target") == "repository_create" and operation.receipt is not None:
        view["repository"] = operation.receipt
    return view


@router.post(
    "/onboarding-sessions/{session_id}/handoffs",
    status_code=202,
    operation_id="startHandoff",
    dependencies=[Depends(require_profile_admission)],
)
async def start_handoff(
    product_id: str,
    project_id: str,
    session_id: str,
    request: HandoffRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, _, session = await scoped_session(db, actor, product_id, project_id, session_id)
    if member.id != session.actor_user_id or session.state not in ("open", "waiting_external"):
        raise HTTPException(403, detail={"code": "permission_denied"})
    if product.archived_at is not None:
        raise HTTPException(409, detail={"code": "product_archived"})
    preconditions(idempotency_key, expected_revision)
    body = {"product_id": product_id, "project_id": project_id, "session_id": session_id, "request": request.model_dump()}
    key_hash, digest = fingerprint({"key": idempotency_key}), fingerprint(body)
    existing = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "startHandoff",
            ProductOperation.key_hash == key_hash,
        )
    )
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
        response.status_code = 200
        headers(response, product.revision)
        return handoff_view(existing, product.revision)
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    resumed = None
    if request.target == "repository_create":
        preview = await db.scalar(
            select(ProductOperation)
            .where(
                ProductOperation.tenant_id == actor.org_id,
                ProductOperation.id == request.preview_id,
            )
            .with_for_update()
        )
        if (
            preview is None
            or preview.operation != "repositoryPreview"
            or preview.product_id != product_id
            or preview.project_id != project_id
            or preview.session_id != session_id
            or preview.actor_id != member.id
            or preview.expires_at.replace(tzinfo=UTC) <= utcnow()
            or preview.expected_revision != expected_revision
            or preview.provider_operation_id is not None
            or preview.request["plan"] != {**request.repository_plan.model_dump(), "confirmed": False}
        ):
            raise HTTPException(409, detail={"code": "stale_revision"})
        mapping = await verify_repository_plan(db, actor, request.repository_plan, project_id)
        resumed = await resumable_repository(db, actor, member.id, project_id, request.repository_plan, int(mapping.installation_id))
    if request.target == "administrator":
        if product.team_id is None:
            raise HTTPException(409, detail={"code": "team_not_selected"})
        if (
            await db.scalar(
                select(TeamMembership.id).where(
                    TeamMembership.org_id == actor.org_id,
                    TeamMembership.user_id == member.id,
                    TeamMembership.team_id == product.team_id,
                )
            )
            is not None
        ):
            raise HTTPException(409, detail={"code": "already_assigned"})
    operation_id = new_uuid()
    operation = ProductOperation(
        tenant_id=actor.org_id,
        id=operation_id,
        product_id=product_id,
        project_id=project_id,
        session_id=session_id,
        actor_id=member.id,
        operation="startHandoff",
        expected_revision=expected_revision,
        key_hash=key_hash,
        request_hash=digest,
        state="pending" if request.target == "repository_create" else "waiting_external",
        request={
            **body,
            **({"resume_operation_id": resumed[0]} if resumed else {}),
            "step_id": "information" if request.target == "administrator" else "basic",
            "generation": 1,
            "sequence": 0,
            "callback_scope": f"product-callback/{operation_id}",
            "callback_producer": PRODUCER,
            **({"team_id": product.team_id, "member_id": member.id} if request.target == "administrator" else {}),
        },
        provider_operation_id=resumed[1]["native_id"] if resumed else None,
        receipt=resumed[1] if resumed else None,
        expires_at=utcnow() + timedelta(days=1),
    )
    db.add(operation)
    if request.target == "repository_create":
        preview.provider_operation_id = operation_id
        preview.state = "succeeded"
    if request.target != "repository_create":
        session.state = "waiting_external"
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    if request.target == "repository_create":
        from src.product_onboarding.repository_creation import RepositoryPreconditionError, create_confirmed_repository, finish_repository

        await db.refresh(product, with_for_update=True)
        await db.refresh(operation, with_for_update=True)
        if product.archived_at is not None or operation.state == "cancelled":
            operation.state = "cancelled"
            operation.updated_at = utcnow()
            await db.commit()
            raise HTTPException(409, detail={"code": "product_archived"})
        precondition_error = None
        try:
            if resumed:
                receipt = await finish_repository(
                    int(mapping.installation_id), request.repository_plan.model_dump(), resumed[0], resumed[1]["native_id"]
                )
            else:
                receipt = await create_confirmed_repository(int(mapping.installation_id), request.repository_plan.model_dump(), operation.id)
        except RepositoryPreconditionError as error:
            operation.state = "failed"
            operation.receipt = {"error": error.detail}
            operation.updated_at = utcnow()
            await db.commit()
            raise HTTPException(error.status_code, detail=error.detail) from None
        except HTTPException as error:
            precondition_error = error if resumed else None
            operation.state = "outcome_unknown"
        except (httpx.HTTPError, OSError, ValueError):
            operation.state = "outcome_unknown"
        else:
            operation.provider_operation_id = receipt["native_id"]
            operation.receipt = receipt
            operation.state = "succeeded"
        operation.updated_at = utcnow()
        await db.commit()
        if precondition_error is not None:
            raise precondition_error
    headers(response, product.revision)
    response.headers["Location"] = f"/products/{product_id}/projects/{project_id}/operations/{operation.id}"
    if operation.state in ("waiting_external", "outcome_unknown"):
        response.headers["Retry-After"] = "2"
    return handoff_view(operation, product.revision)


async def observed_handoff_effect(
    db: AsyncSession,
    actor: TokenContext,
    operation: ProductOperation,
    *,
    provider_operation_id: str | None = None,
) -> str | None:
    effect_id = operation.provider_operation_id or provider_operation_id
    target = operation.request.get("request", {}).get("target")
    if target == "repository_create":
        return None
    if target == "administrator":
        effect = await db.scalar(
            select(TeamMembership.id).where(
                TeamMembership.org_id == actor.org_id,
                TeamMembership.user_id == operation.request["member_id"],
                TeamMembership.team_id == operation.request["team_id"],
                TeamMembership.source == "admin",
                TeamMembership.id == effect_id if effect_id is not None else True,
            )
        )
    elif target == "github_connection":
        mapping = await db.scalar(
            select(ChannelTenantMap).where(
                ChannelTenantMap.org_id == actor.org_id,
                ChannelTenantMap.provider == "github",
                ChannelTenantMap.created_at >= operation.created_at,
                ChannelTenantMap.installation_id.is_not(None),
                ChannelTenantMap.ownership_disputed.is_(False),
                ChannelTenantMap.id == effect_id if effect_id is not None else True,
                or_(
                    ~ChannelTenantMap.provider_scope_id.startswith("personal:"),
                    ChannelTenantMap.installed_by_user_id == actor.user_id,
                ),
            )
        )
        effect = None
        if mapping is not None:
            try:
                await assert_installation_owned_by(actor.org_id, int(mapping.installation_id), db=db)
            except (InstallationOwnershipError, ValueError):
                pass
            else:
                effect = mapping.id
    else:
        visible = _visible_credential_filter(actor)
        credential = None
        if visible:
            criteria = [
                UserCredential.org_id == actor.org_id,
                UserCredential.created_at >= operation.created_at,
                UserCredential.service == ("aws" if target == "aws_connection" else operation.request["request"].get("provider")),
                or_(UserCredential.expires_at.is_(None), UserCredential.expires_at > utcnow()),
                or_(*visible),
            ]
            if effect_id is not None:
                criteria.append(UserCredential.id == effect_id)
            if target == "aws_connection":
                criteria.append(UserCredential.aws_verified_at.is_not(None))
            credential = await db.scalar(select(UserCredential).where(*criteria))
        effect = credential.id if credential is not None else None
    return effect


async def handoff_readback(db: AsyncSession, actor: TokenContext, operation: ProductOperation, revision: int) -> dict:
    view = handoff_view(operation, revision)
    if await observed_handoff_effect(db, actor, operation) is not None:
        view["coverage"] = "complete"
        view["next_action"] = "Connection or administrator change observed; awaiting scoped completion callback."
    return view


async def link_l5_configuration(
    db: AsyncSession,
    actor: TokenContext,
    product_id: str,
    project_id: str,
    configuration_ref: dict,
    capabilities: list[str],
    expected_revision: int,
    idempotency_key: str,
):
    """Conditionally link only selected capabilities; preserve all other Profile fields."""
    import hashlib
    import json
    from copy import deepcopy

    from pydantic import ValidationError
    from sqlalchemy import update

    from src.products.configuration import RecordRef
    from src.products.models import Product, ProductChange, ProductProjectBinding
    from src.products.routes import KEY_PATTERN, profile, scoped_product

    member, product, project = await scoped_product(db, actor, product_id, project_id)
    if actor.account_type != "human" or not KEY_PATTERN.fullmatch(idempotency_key):
        raise HTTPException(403, detail={"code": "access_denied"})
    binding = await db.scalar(
        select(ProductProjectBinding).where(
            ProductProjectBinding.tenant_id == actor.org_id,
            ProductProjectBinding.product_id == product_id,
            ProductProjectBinding.project_id == project_id,
            ProductProjectBinding.valid_to.is_(None),
        )
    )
    try:
        RecordRef.model_validate(configuration_ref)
    except ValidationError:
        raise HTTPException(422, detail={"code": "invalid_configuration_ref"}) from None

    if (
        binding is None
        or configuration_ref.get("source_connection_id") != binding.source_connection_id
        or (configuration_ref.get("tenant_id"), configuration_ref.get("project_id"), configuration_ref.get("record_type"))
        != (actor.org_id, project_id, "l5_configuration")
    ):
        raise HTTPException(422, detail={"code": "invalid_configuration_ref"})
    body = {
        "product_id": product_id,
        "project_id": project_id,
        "expected_revision": expected_revision,
        "configuration_ref": configuration_ref,
        "capabilities": capabilities,
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    existing = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "linkL5Configuration",
            ProductOperation.key_hash == key_hash,
        )
    )
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        return existing.receipt
    if product.archived_at is not None or product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    updated = deepcopy(project.configuration or {})
    selections = updated.get("selected_capabilities") or []
    if (
        not capabilities
        or len(capabilities) != len(set(capabilities))
        or not set(capabilities) <= {choice.get("capability") for choice in selections}
    ):
        raise HTTPException(422, detail={"code": "capability_not_selected"})
    for choice in selections:
        if choice.get("capability") != "devops" or "devops" not in capabilities:
            continue
        if choice.get("ownership_mode") == "not_applicable":
            raise HTTPException(422, detail={"code": "capability_not_selected"})
        linked = choice.get("configuration_ref")
        if linked is not None and linked.get("record_id") != configuration_ref["record_id"]:
            raise HTTPException(409, detail={"code": "configuration_ref_conflict"})
    for choice in selections:
        if choice.get("capability") in capabilities:
            choice["configuration_ref"] = deepcopy(configuration_ref)
    changed = updated != (project.configuration or {})
    now = utcnow()
    if changed:
        applied = await db.execute(
            update(Product)
            .where(
                Product.tenant_id == actor.org_id,
                Product.id == product_id,
                Product.revision == expected_revision,
                Product.archived_at.is_(None),
            )
            .values(revision=expected_revision + 1, updated_at=now)
        )
        if applied.rowcount != 1:
            raise HTTPException(412, detail={"code": "stale_revision"})
        await db.refresh(product)
        project.configuration = updated
        project.updated_at = now
        await db.flush()
    snapshot = profile(product, project)
    operation = ProductOperation(
        tenant_id=actor.org_id,
        actor_id=member.id,
        operation="linkL5Configuration",
        key_hash=key_hash,
        request_hash=digest,
        request=body,
        product_id=product_id,
        project_id=project_id,
        receipt={"revision": product.revision},
        expires_at=now + timedelta(days=1),
    )
    db.add(operation)
    await db.flush()
    if changed:
        db.add(
            ProductChange(
                tenant_id=actor.org_id,
                product_id=product_id,
                project_id=project_id,
                revision=product.revision,
                actor_id=member.id,
                operation_id=operation.id,
                changed_paths=["/project/configuration/selected_capabilities"],
                snapshot=snapshot,
            )
        )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    return operation.receipt
