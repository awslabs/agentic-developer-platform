"""Minimal Product create and scoped profile reads."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from jwt.exceptions import PyJWTError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.features.routes import _is_enabled_strict
from src.product_onboarding.progress import profile_progress, required_checks
from src.products.configuration import Financials, ProjectConfiguration, manually_confirmed_paths, normalize_environments
from src.products.models import Product, ProductChange, ProductOperation, ProductProjectBinding, ProductSourceBinding, Project
from src.products.shared_profile import SharedProfilePatch
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.identity.product_scope import product_member, product_visibility_scope, service_product_scope
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.organization import Team, TeamMembership
from src.shared.schemas.auth import TokenContext


class ProductRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validate(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                fields = [{"field": ".".join(map(str, issue["loc"])), "reason": issue["type"]} for issue in error.errors()]
                return JSONResponse(status_code=422, content={"detail": {"code": "validation_error", "fields": fields}})

        return validate


router = APIRouter(prefix="/v1/products", tags=["products"], route_class=ProductRoute)
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
DEFERRED_PEERS = {
    "context": "Product Context integration is unavailable in this release. Your saved Product remains available.",
    "l5": "L5 services integration is unavailable in this release. Your saved Product remains available.",
}


def peer_availability() -> dict:
    return {peer: {"status": "unavailable", "reason": "integration_deferred", "message": message} for peer, message in DEFERRED_PEERS.items()}


def registry_relationship(binding: ProductProjectBinding, event_id: str, recorded_at: datetime) -> dict:
    reference = {
        "tenant_id": binding.tenant_id,
        "project_id": binding.project_id,
        "source_connection_id": binding.source_connection_id,
        "record_revision": event_id,
    }
    product_ref = {**reference, "record_type": "product", "record_id": binding.product_id}
    project_ref = {**reference, "record_type": "project", "record_id": binding.project_id}
    version = {
        "relationship_id": binding.id,
        "relationship_type": "product_project",
        "from_ref": product_ref,
        "to_ref": project_ref,
        "valid_from": binding.valid_from.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "recorded_at": recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "mapping_version": binding.mapping_version,
        "source_refs": [product_ref, project_ref],
        "source_connection_id": binding.source_connection_id,
        "relationship_revision": str(binding.relationship_revision),
    }
    if binding.valid_to is not None:
        version["valid_to"] = binding.valid_to.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if binding.supersedes_revision is not None:
        version["supersedes_revision"] = str(binding.supersedes_revision)
    return version


def require_profile_admission():
    if not _is_enabled_strict("BG_PRODUCT_PROFILE_ENABLED"):
        raise HTTPException(503, detail={"code": "product_profile_disabled"})


class ProjectSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["create"]


class CreateProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=10000)
    intended_users: list[str] = Field(min_length=1, max_length=100)
    starting_point: Literal["idea"]
    project: ProjectSelection

    @field_validator("intended_users")
    @classmethod
    def check_users(cls, users: list[str]) -> list[str]:
        stripped = [user.strip() for user in users]
        if any(not user or len(user) > 200 for user in stripped) or len(stripped) != len(set(stripped)):
            raise ValueError("intended_users must contain distinct nonblank names of at most 200 characters")
        return stripped


class LifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["archive", "restore"]
    reason: str = Field(min_length=1, max_length=10000)


class DeliveryHomeRepository(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["github"]
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=250)
    repository_id: int = Field(gt=0)


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default=None, min_length=1, max_length=200)
    purpose: str = Field(default=None, min_length=1, max_length=10000)
    intended_users: list[str] = Field(default=None, min_length=1, max_length=100)
    starting_point: Literal["idea", "existing_assets", "running_product"] | None = None
    lifecycle_status: Literal["draft", "active", "retired"] | None = None
    owner_user_id: str | None = Field(default=None, min_length=1, max_length=255)
    team_id: str | None = Field(default=None, min_length=1, max_length=255)
    domain: str | None = Field(default=None, min_length=1, max_length=200)
    goals: list[str] | None = Field(default=None, max_length=100)
    delivery_home_repository: DeliveryHomeRepository | None = None
    profile_configuration: SharedProfilePatch | None = None

    @field_validator("intended_users")
    @classmethod
    def check_users(cls, users: list[str] | None) -> list[str] | None:
        return CreateProduct.check_users(users) if users is not None else None

    @field_validator("goals")
    @classmethod
    def check_goals(cls, goals: list[str] | None) -> list[str] | None:
        if goals is not None and any(not goal.strip() or len(goal.strip()) > 10000 for goal in goals):
            raise ValueError("goals must contain nonblank text of at most 10000 characters")
        return [goal.strip() for goal in goals] if goals is not None else None


async def current_owner(db: AsyncSession, actor: TokenContext):
    member = await product_member(
        db,
        user_id=actor.user_id,
        org_id=actor.org_id,
        username=actor.cognito_username,
        account_type=actor.account_type,
        is_platform_admin=actor.is_admin,
    )
    if member is None:
        raise HTTPException(403, detail={"code": "access_denied"})
    return member


def profile(product: Product, project: Project) -> dict:
    return {
        "schema_version": "1.0",
        "tenant_id": product.tenant_id,
        "product_id": product.id,
        "project_id": project.id,
        "revision": product.revision,
        "delivery_home_repository": product.delivery_home_repository,
        "delivery_home_implied": False,
        "product_provenance": product.provenance,
        "profile_configuration": product.profile_configuration or {},
        "product": {
            "name": product.name,
            "purpose": product.purpose,
            "intended_users": product.intended_users,
            "owner_ref": {"kind": "user", "id": product.owner_user_id},
            "starting_point": product.starting_point,
            "lifecycle_status": product.lifecycle_status,
            **({"team_id": product.team_id} if product.team_id is not None else {}),
            **({"domain": product.domain} if product.domain is not None else {}),
            "goals": product.goals,
        },
        "project": {
            "id": project.id,
            "tenant_id": project.tenant_id,
            "name": project.name,
            "owner_user_id": project.owner_user_id,
            "state": project.state,
        },
        "configuration": normalize_environments(project.configuration or {}),
        "provenance": project.provenance,
        "progress": profile_progress(product, project),
        "context_status": "unavailable",
        "peer_availability": peer_availability(),
        **({"archived_at": product.archived_at.isoformat()} if product.archived_at is not None else {}),
    }


async def implied_delivery_home(db: AsyncSession, actor: TokenContext, product: Product, service_grant=None) -> dict | None:
    bindings = await product_github_bindings(db, product)
    if any(not binding.native_id.isascii() or not binding.native_id.isdecimal() for binding in bindings):
        return None
    repositories = {int(binding.native_id): binding for binding in bindings}
    if len(repositories) != 1:
        return None
    for binding in bindings:
        home = await verified_delivery_home(db, actor, binding, service_grant)
        if home is not None:
            return home
    return None


async def delivery_home_connection(db: AsyncSession, actor: TokenContext, binding: ProductSourceBinding, service_grant=None):
    from src.products.source_routes import connection_access, service_visible_bindings
    from src.shared.models.vault import ChannelTenantMap

    if actor.account_type == "service":
        if service_grant is None or binding.id not in {item.id for item in await service_visible_bindings(db, service_grant)}:
            return None
        return await db.get(ChannelTenantMap, binding.installation_id)
    return await connection_access(db, actor, binding)


async def verified_delivery_home(db: AsyncSession, actor: TokenContext, binding: ProductSourceBinding, service_grant=None) -> dict | None:
    from src.admin.connections.github_client import GitHubAppClient
    from src.admin.connections.service import _get_github_app_credentials

    if not binding.installation_id or not binding.native_id.isascii() or not binding.native_id.isdecimal():
        return None
    connection = await delivery_home_connection(db, actor, binding, service_grant)
    if connection is None or not binding.installation_id or not str(connection.installation_id).isdecimal():
        return None
    app_id, private_key = await asyncio.to_thread(_get_github_app_credentials)
    if not app_id or not private_key:
        return None
    client = GitHubAppClient(app_id, private_key)
    try:
        async with asyncio.timeout(10):
            repository = await client.get_installation_repository(int(connection.installation_id), int(binding.native_id))
        owner = repository.get("owner") or {}
        name = repository.get("full_name")
        if (
            str(repository.get("id")) != str(int(binding.native_id))
            or str(owner.get("id")) != binding.namespace
            or connection.provider_scope_id not in (binding.namespace, owner.get("login"))
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
            or name != f"{owner.get('login')}/{repository.get('name')}"
        ):
            return None
        return {"provider": "github", "repository": name, "repository_id": int(binding.native_id)}
    except (TimeoutError, httpx.HTTPError, PyJWTError, ValueError, TypeError):
        return None
    finally:
        await client.aclose()


async def product_github_bindings(db: AsyncSession, product: Product, exclude_project_id: str | None = None) -> list[ProductSourceBinding]:
    query = (
        select(ProductSourceBinding)
        .join(
            ProductProjectBinding,
            (ProductProjectBinding.tenant_id == ProductSourceBinding.tenant_id)
            & (ProductProjectBinding.project_id == ProductSourceBinding.project_id),
        )
        .where(
            ProductProjectBinding.tenant_id == product.tenant_id,
            ProductProjectBinding.product_id == product.id,
            ProductProjectBinding.valid_to.is_(None),
            ProductSourceBinding.kind == "repository",
            ProductSourceBinding.provider == "github",
            ProductSourceBinding.unlinked_at.is_(None),
            ProductSourceBinding.status != "revoked",
        )
    )
    if exclude_project_id is not None:
        query = query.where(ProductProjectBinding.project_id != exclude_project_id)
    return (await db.scalars(query)).all()


async def projected_profile(
    db: AsyncSession,
    actor: TokenContext,
    product: Product,
    project: Project,
    snapshot: dict | None = None,
    now: datetime | None = None,
    service_grant=None,
) -> dict:
    from src.products.source_routes import source_overlay

    observed_now = now or utcnow()
    result = json.loads(json.dumps(snapshot if snapshot is not None else profile(product, project)))
    from src.products.shared_profile import shared_profile

    saved_settings = result.pop("profile_configuration", {})
    result["shared_profile"] = await shared_profile(db, actor, product, service_grant=service_grant, settings=saved_settings)
    result["shared_profile"]["revision"] = result["revision"]
    result["peer_availability"] = peer_availability()
    projected, evidence = await source_overlay(
        db, actor, project, snapshot=result, now=observed_now, service_grant=service_grant, product_id=product.id
    )
    result["configuration"] = projected.configuration
    result["provenance"] = projected.provenance
    result["progress"] = profile_progress(product, projected, now=observed_now, source_evidence=evidence)
    if result.get("delivery_home_repository") is not None:
        home_id = result["delivery_home_repository"]["repository_id"]
        for binding in await product_github_bindings(db, product):
            if (
                binding.native_id.isascii()
                and binding.native_id.isdecimal()
                and int(binding.native_id) == home_id
                and await delivery_home_connection(db, actor, binding, service_grant) is not None
            ):
                break
        else:
            result["delivery_home_repository"] = None
            result["delivery_home_implied"] = False
    elif result["revision"] == product.revision and product.delivery_home_repository is None:
        home = await implied_delivery_home(db, actor, product, service_grant)
        if home is not None:
            result["delivery_home_repository"] = home
            result["delivery_home_implied"] = True
    return result


async def scoped_snapshots(db: AsyncSession, product: Product) -> dict:
    projects = (
        await db.scalars(
            select(Project)
            .join(
                ProductProjectBinding,
                (ProductProjectBinding.tenant_id == Project.tenant_id) & (ProductProjectBinding.project_id == Project.id),
            )
            .where(
                ProductProjectBinding.tenant_id == product.tenant_id,
                ProductProjectBinding.product_id == product.id,
                ProductProjectBinding.valid_to.is_(None),
            )
        )
    ).all()
    return {"scope_snapshots": {project.id: profile(product, project) for project in projects}}


@router.post("", status_code=201, operation_id="createProduct", dependencies=[Depends(require_profile_admission)])
async def create_product(
    request: CreateProduct,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member = await current_owner(db, actor)
    if idempotency_key is None or not KEY_PATTERN.fullmatch(idempotency_key):
        raise HTTPException(422, detail={"code": "validation_error", "field": "Idempotency-Key"})
    body = request.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    operation_query = select(ProductOperation).where(
        ProductOperation.tenant_id == actor.org_id,
        ProductOperation.actor_id == member.id,
        ProductOperation.operation == "createProduct",
        ProductOperation.key_hash == key_hash,
    )

    async def replay(existing: ProductOperation):
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if (existing.receipt or {}).get("fixture_removed") is True:
            raise HTTPException(410, detail={"code": "fixture_removed"})
        pair = await db.execute(
            select(Product, Project).where(
                product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin),
                Product.id == existing.product_id,
                Project.tenant_id == actor.org_id,
                Project.id == existing.project_id,
            )
        )
        found = pair.first()
        if found is None:
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Location"] = f"/api/v1/products/{found[0].id}/projects/{found[1].id}"
        response.headers["Profile-Revision"] = str(found[0].revision)
        response.headers["Cache-Control"] = "no-store"
        return await projected_profile(db, actor, found[0], found[1])

    existing = await db.scalar(operation_query)
    if existing is not None:
        return await replay(existing)

    # Membership rows are the authority. Prefer the user's declared primary;
    # a sole membership is unambiguous even when older data has no primary bit.
    memberships = (
        await db.execute(
            select(TeamMembership.team_id, TeamMembership.is_primary)
            .join(Team, and_(Team.id == TeamMembership.team_id, Team.org_id == TeamMembership.org_id))
            .where(TeamMembership.user_id == member.id, TeamMembership.org_id == actor.org_id)
        )
    ).all()
    primary = [team_id for team_id, is_primary in memberships if is_primary]
    team_id = primary[0] if len(primary) == 1 else memberships[0][0] if len(memberships) == 1 else None
    if team_id is None:
        raise HTTPException(409, detail={"code": "team_assignment_required", "message": "Choose a primary ADP team before creating a Product."})

    now = utcnow()
    product = Product(
        id=new_uuid(),
        tenant_id=actor.org_id,
        name=request.name,
        purpose=request.purpose,
        intended_users=request.intended_users,
        owner_user_id=member.id,
        team_id=team_id,
        created_by=member.id,
        starting_point=request.starting_point,
    )
    project = Project(id=new_uuid(), tenant_id=actor.org_id, name=request.name, owner_user_id=member.id)
    binding = ProductProjectBinding(
        id=new_uuid(),
        tenant_id=actor.org_id,
        product_id=product.id,
        project_id=project.id,
        is_base=True,
        relationship_revision=1,
        mapping_version="profile-registry-1",
        source_connection_id="adp.profile",
        valid_from=now,
        recorded_at=now,
        provenance={"actor_id": member.id, "source": "direct_input"},
    )
    operation = ProductOperation(
        id=new_uuid(),
        tenant_id=actor.org_id,
        actor_id=member.id,
        operation="createProduct",
        key_hash=key_hash,
        request_hash=digest,
        request=body,
        product_id=product.id,
        project_id=project.id,
        expires_at=now + timedelta(days=1),
    )
    change = ProductChange(
        id=new_uuid(),
        tenant_id=actor.org_id,
        product_id=product.id,
        project_id=project.id,
        revision=1,
        actor_id=member.id,
        operation_id=operation.id,
        changed_paths=["/product", "/project"],
        snapshot={},
        recorded_at=now,
        relationship_versions=[],
    )
    change.relationship_versions = [registry_relationship(binding, change.id, now)]
    operation.receipt = {"product_id": product.id, "project_id": project.id, "revision": 1}
    try:
        # Scalar foreign keys do not order these ORM objects through the
        # onboarding/source-operation cycles. Flush parents before dependants,
        # retaining a single transaction for the profile and its history.
        db.add_all([product, project])
        await db.flush()
        db.add_all([binding, operation])
        await db.flush()
        change.snapshot = profile(product, project)
        db.add(change)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(operation_query)
        if existing is None:
            raise
        return await replay(existing)
    return await replay(operation)


@router.patch("/{product_id}/projects/{project_id}", operation_id="updateProduct", dependencies=[Depends(require_profile_admission)])
async def update_product(
    product_id: str,
    project_id: str,
    patch: ProductPatch,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    try:
        member = await current_owner(db, actor)
    except HTTPException:
        raise HTTPException(404, detail={"code": "not_found"}) from None
    pair = await db.execute(
        select(Product, Project)
        .join(
            ProductProjectBinding,
            (ProductProjectBinding.tenant_id == Product.tenant_id)
            & (ProductProjectBinding.product_id == Product.id)
            & (ProductProjectBinding.valid_to.is_(None)),
        )
        .join(Project, (Project.tenant_id == ProductProjectBinding.tenant_id) & (Project.id == ProductProjectBinding.project_id))
        .where(product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin), Product.id == product_id, Project.id == project_id)
    )
    found = pair.first()
    if found is None:
        raise HTTPException(404, detail={"code": "not_found"})
    product, project = found
    if idempotency_key is None or expected_revision is None:
        raise HTTPException(428, detail={"code": "precondition_required"})
    if not KEY_PATTERN.fullmatch(idempotency_key) or not 1 <= expected_revision <= 9007199254740991:
        raise HTTPException(422, detail={"code": "validation_error"})
    fields = patch.model_dump(exclude_unset=True)
    if not fields or any(value is None for field, value in fields.items() if field not in {"domain", "team_id", "delivery_home_repository"}):
        raise HTTPException(422, detail={"code": "validation_error"})
    body = {"product_id": product_id, "project_id": project_id, "expected_revision": expected_revision, "patch": fields}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    operation_query = select(ProductOperation).where(
        ProductOperation.tenant_id == actor.org_id,
        ProductOperation.actor_id == member.id,
        ProductOperation.operation == "updateProduct",
        ProductOperation.key_hash == key_hash,
    )
    existing = await db.scalar(operation_query)

    async def replay(existing: ProductOperation):
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.expires_at.replace(tzinfo=UTC) <= utcnow() or existing.receipt is None:
            raise HTTPException(410, detail={"code": "operation_expired"})
        response.headers["Profile-Revision"] = str(existing.receipt["revision"])
        response.headers["Cache-Control"] = "no-store"
        return await projected_profile(db, actor, product, project, existing.receipt["profile"])

    if existing is not None:
        return await replay(existing)
    if product.archived_at is not None:
        raise HTTPException(409, detail={"code": "product_archived"})

    async def conflict_paths():
        changes = (
            await db.scalars(
                select(ProductChange).where(
                    ProductChange.tenant_id == actor.org_id,
                    ProductChange.product_id == product_id,
                    ProductChange.revision > expected_revision,
                )
            )
        ).all()
        return sorted({path for change in changes for path in change.changed_paths if path.startswith("/product/")})

    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision", "conflict_paths": await conflict_paths()})
    home = fields.get("delivery_home_repository")
    if home is not None:
        bindings = await product_github_bindings(db, product)
        binding = next(
            (item for item in bindings if item.native_id.isascii() and item.native_id.isdecimal() and int(item.native_id) == home["repository_id"]),
            None,
        )
        if binding is None or await verified_delivery_home(db, actor, binding) != home:
            raise HTTPException(422, detail={"code": "delivery_home_not_bound"})
    if "profile_configuration" in fields:
        from src.products.shared_profile import apply_patch

        # The receipt hashes the requested patch; audit snapshots retain the merged configuration.
        fields = {**fields, "profile_configuration": await apply_patch(db, actor, product, fields["profile_configuration"])}
    changed = {field: value for field, value in fields.items() if getattr(product, field) != value}
    if "owner_user_id" in changed or "team_id" in changed:
        access = AccessControl(db)
        try:
            if "owner_user_id" in changed:
                await access.check_permission(actor, Permission.USER_MANAGE, target_org_id=actor.org_id)
            if "team_id" in changed:
                await access.check_permission(actor, Permission.ORG_UPDATE, target_org_id=actor.org_id)
        except AccessDeniedError:
            raise HTTPException(403, detail={"code": "permission_denied"}) from None
        if "owner_user_id" in changed and await product_member(db, user_id=changed["owner_user_id"], org_id=actor.org_id) is None:
            raise HTTPException(404, detail={"code": "not_found"})
        if "team_id" in changed and changed["team_id"] is not None:
            team = await db.scalar(select(Team).where(Team.id == changed["team_id"], Team.org_id == actor.org_id))
            if team is None:
                raise HTTPException(404, detail={"code": "not_found"})
    if not changed:
        now = utcnow()
        result = profile(product, project)
        db.add(
            ProductOperation(
                tenant_id=actor.org_id,
                actor_id=member.id,
                operation="updateProduct",
                key_hash=key_hash,
                request_hash=digest,
                request=body,
                product_id=product_id,
                project_id=project_id,
                receipt={"revision": product.revision, "profile": result},
                expires_at=now + timedelta(days=1),
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(operation_query)
            if existing is None:
                raise
            return await replay(existing)
        response.headers["Profile-Revision"] = str(product.revision)
        response.headers["Cache-Control"] = "no-store"
        return await projected_profile(db, actor, product, project, result)
    now = utcnow()
    if "delivery_home_repository" in changed:
        changed["provenance"] = [
            *(product.provenance or []),
            {
                "path": "/product/delivery_home_repository",
                "actor_id": member.id,
                "recorded_at": now.isoformat().replace("+00:00", "Z"),
                "revision": expected_revision + 1,
            },
        ]
    applied = await db.execute(
        update(Product)
        .where(Product.tenant_id == actor.org_id, Product.id == product_id, Product.revision == expected_revision, Product.archived_at.is_(None))
        .values(**changed, revision=expected_revision + 1, updated_at=now)
    )
    if applied.rowcount != 1:
        await db.rollback()
        existing = await db.scalar(operation_query)
        if existing is not None:
            return await replay(existing)
        raise HTTPException(412, detail={"code": "stale_revision", "conflict_paths": await conflict_paths()})
    await db.refresh(product)
    result = profile(product, project)
    operation = ProductOperation(
        id=new_uuid(),
        tenant_id=actor.org_id,
        actor_id=member.id,
        operation="updateProduct",
        key_hash=key_hash,
        request_hash=digest,
        request=body,
        product_id=product_id,
        project_id=project_id,
        receipt={"revision": product.revision, "profile": result},
        expires_at=now + timedelta(days=1),
    )
    change = ProductChange(
        tenant_id=actor.org_id,
        product_id=product_id,
        project_id=None,
        revision=product.revision,
        actor_id=member.id,
        operation_id=operation.id,
        changed_paths=[f"/product/{field}" for field in changed if field != "provenance"],
        snapshot={**result, **await scoped_snapshots(db, product)},
    )
    db.add(operation)
    try:
        # The audit row references this receipt through an immediate foreign key.
        await db.flush()
        db.add(change)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(operation_query)
        if existing is None:
            raise
        return await replay(existing)
    response.headers["Profile-Revision"] = str(product.revision)
    response.headers["Cache-Control"] = "no-store"
    return await projected_profile(db, actor, product, project, result)


async def scoped_product(db: AsyncSession, actor: TokenContext, product_id: str, project_id: str, delegation_id: str | None = None):
    if actor.account_type == "service":
        grant = await service_product_scope(db, actor, delegation_id, product_id, project_id, "product:read")
        if grant is None:
            raise HTTPException(404, detail={"code": "not_found"})
        member = grant
        scope = Product.tenant_id == grant.tenant_id
    else:
        try:
            member = await current_owner(db, actor)
        except HTTPException:
            raise HTTPException(404, detail={"code": "not_found"}) from None
        scope = product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin)
    result = await db.execute(
        select(Product, Project)
        .join(
            ProductProjectBinding,
            (ProductProjectBinding.tenant_id == Product.tenant_id)
            & (ProductProjectBinding.product_id == Product.id)
            & (ProductProjectBinding.valid_to.is_(None)),
        )
        .join(Project, (Project.tenant_id == ProductProjectBinding.tenant_id) & (Project.id == ProductProjectBinding.project_id))
        .where(scope, Product.id == product_id, Project.id == project_id)
    )
    pair = result.first()
    if pair is None:
        raise HTTPException(404, detail={"code": "not_found"})
    return member, pair[0], pair[1]


@router.post("/{product_id}/projects/{project_id}/lifecycle", operation_id="changeLifecycle", dependencies=[Depends(require_profile_admission)])
async def change_lifecycle(
    product_id: str,
    project_id: str,
    request: LifecycleRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, project = await scoped_product(db, actor, product_id, project_id)
    if product.owner_user_id != member.id:
        raise HTTPException(403, detail={"code": "permission_denied"})
    if idempotency_key is None or expected_revision is None:
        raise HTTPException(428, detail={"code": "precondition_required"})
    if not KEY_PATTERN.fullmatch(idempotency_key) or not 1 <= expected_revision <= 9007199254740991:
        raise HTTPException(422, detail={"code": "validation_error"})
    body = {"product_id": product_id, "project_id": project_id, "expected_revision": expected_revision, **request.model_dump()}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    operation_query = select(ProductOperation).where(
        ProductOperation.tenant_id == actor.org_id,
        ProductOperation.actor_id == member.id,
        ProductOperation.operation == "changeLifecycle",
        ProductOperation.key_hash == key_hash,
    )

    async def replay(existing: ProductOperation):
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.expires_at.replace(tzinfo=UTC) <= utcnow() or existing.receipt is None:
            raise HTTPException(410, detail={"code": "operation_expired"})
        response.headers["Profile-Revision"] = str(existing.receipt["revision"])
        response.headers["Cache-Control"] = "no-store"
        return await projected_profile(db, actor, product, project, existing.receipt["profile"])

    existing = await db.scalar(operation_query)
    if existing is not None:
        return await replay(existing)
    if product.revision != expected_revision:
        changes = (
            await db.scalars(
                select(ProductChange).where(
                    ProductChange.tenant_id == actor.org_id,
                    ProductChange.product_id == product_id,
                    ProductChange.revision > expected_revision,
                )
            )
        ).all()
        paths = sorted({path for change in changes for path in change.changed_paths if path.startswith("/product/")})
        raise HTTPException(412, detail={"code": "stale_revision", "conflict_paths": paths})
    if (request.action == "archive") == (product.archived_at is not None):
        raise HTTPException(409, detail={"code": "lifecycle_conflict"})
    now = utcnow()
    archived_at = now if request.action == "archive" else None
    result = await db.execute(
        update(Product)
        .where(
            Product.tenant_id == actor.org_id,
            Product.id == product_id,
            Product.revision == expected_revision,
            Product.archived_at.is_not(None) if archived_at is None else Product.archived_at.is_(None),
        )
        .values(archived_at=archived_at, revision=expected_revision + 1, updated_at=now)
    )
    if result.rowcount != 1:
        await db.rollback()
        existing = await db.scalar(operation_query)
        if existing is not None:
            return await replay(existing)
        raise HTTPException(412, detail={"code": "stale_revision", "conflict_paths": []})
    if request.action == "archive":
        await db.execute(
            update(ProductOperation)
            .where(
                ProductOperation.tenant_id == actor.org_id,
                ProductOperation.product_id == product_id,
                ProductOperation.state.in_(["pending", "running", "waiting_external", "outcome_unknown"]),
            )
            .values(state="cancelled", updated_at=now)
        )
    await db.refresh(product)
    snapshot = profile(product, project)
    operation = ProductOperation(
        id=new_uuid(),
        tenant_id=actor.org_id,
        actor_id=member.id,
        operation="changeLifecycle",
        key_hash=key_hash,
        request_hash=digest,
        request=body,
        product_id=product_id,
        project_id=project_id,
        receipt={"revision": product.revision, "profile": snapshot},
        expires_at=now + timedelta(days=1),
    )
    change = ProductChange(
        tenant_id=actor.org_id,
        product_id=product_id,
        project_id=None,
        revision=product.revision,
        actor_id=member.id,
        operation_id=operation.id,
        changed_paths=["/product/archived_at"],
        snapshot={**snapshot, **await scoped_snapshots(db, product)},
    )
    db.add(operation)
    try:
        # Keep receipt and audit insertion ordered within the same transaction.
        await db.flush()
        db.add(change)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(operation_query)
        if existing is None:
            raise
        return await replay(existing)
    response.headers["Profile-Revision"] = str(product.revision)
    response.headers["Cache-Control"] = "no-store"
    return await projected_profile(db, actor, product, project, snapshot)


def change_cursor(payload: list, secret: str) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def change_in_project(project_id: str):
    return or_(
        ProductChange.project_id == project_id,
        and_(
            ProductChange.project_id.is_(None),
            ProductChange.snapshot["scope_snapshots"][project_id].as_string().is_not(None),
        ),
    )


def authorized_profile_ref(reference: dict, tenant_id: str, project_id: str) -> bool:
    return (
        reference.get("tenant_id") == tenant_id
        and reference.get("project_id") == project_id
        and reference.get("source_connection_id") == "adp.profile"
    )


async def validate_financial_targets(db: AsyncSession, financials: Financials | None, product: Product, project: Project, actor: TokenContext):
    if financials is None:
        return
    for initiative in financials.initiatives or []:
        reference = initiative.model_dump()
        if (
            initiative.tenant_id != product.tenant_id
            or initiative.project_id != project.id
            or initiative.record_type != "initiative"
            or initiative.source_connection_id == "adp.profile"
        ):
            raise HTTPException(422, detail={"code": "validation_error", "field": "financials.initiatives"})
        if initiative.source_connection_id == "adp.catalog":
            from src.catalog.initiative_routes import initiative as visible_initiative

            record = await visible_initiative(db, actor, initiative.record_id)
            if initiative.record_revision != str(record.revision):
                raise HTTPException(412, detail={"code": "stale_revision"})
            continue
        authorized = False
        for proof in project.provenance or []:
            if (
                proof.get("state") != "accepted"
                or proof.get("method") not in ("connector", "manual")
                or "/configuration/financials/initiatives" not in proof.get("paths", [])
                or (
                    reference not in proof.get("source_refs", [])
                    and not (proof.get("method") == "manual" and reference in (project.configuration.get("financials") or {}).get("initiatives", []))
                )
            ):
                continue
            try:
                observed = datetime.fromisoformat(proof["observed_at"].replace("Z", "+00:00"))
                expires = datetime.fromisoformat(proof["stale_after"].replace("Z", "+00:00"))
                now = utcnow()
                authorized = observed.tzinfo is not None and expires.tzinfo is not None and observed <= now < expires
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if authorized:
                break
        if not authorized:
            raise HTTPException(422, detail={"code": "validation_error", "field": "financials.initiatives"})
    for allocation in financials.allocations or []:
        target = allocation.target_ref
        if not (
            target.tenant_id == product.tenant_id
            and target.project_id == project.id
            and target.source_connection_id == "adp.profile"
            and target.record_type == "product"
            and target.record_id == product.id
        ):
            raise HTTPException(422, detail={"code": "validation_error", "field": "financials.allocations.target_ref"})
        version = await db.scalar(
            select(ProductChange.id).where(
                ProductChange.tenant_id == product.tenant_id,
                ProductChange.product_id == product.id,
                ProductChange.project_id == project.id,
                ProductChange.id == target.record_revision,
            )
        )
        if version is None:
            raise HTTPException(422, detail={"code": "validation_error", "field": "financials.allocations.target_ref"})


def record_basic_source_edits(project: Project, product: Product, previous: dict, updated: dict, actor_id: str, revision: int) -> bool:
    old_basic = previous.get("basic") or {}
    new_basic = updated.get("basic") or {}
    protected_paths = manually_confirmed_paths(project.provenance or [])
    changed = {
        f"/configuration/basic/{field}"
        for field in ("repositories", "trackers")
        if old_basic.get(field) != new_basic.get(field) or (field in new_basic and f"/configuration/basic/{field}" not in protected_paths)
    }
    if not changed:
        return False
    retained = []
    for proof in project.provenance or []:
        paths = [path for path in proof.get("paths", []) if path not in changed]
        if paths:
            retained.append({**proof, "paths": paths})
    timestamp = utcnow().isoformat().replace("+00:00", "Z")
    reference = {
        "tenant_id": project.tenant_id,
        "project_id": project.id,
        "source_connection_id": "adp.profile",
        "record_type": "product",
        "record_id": product.id,
        "record_revision": str(revision),
    }
    project.provenance = retained + [
        {
            "paths": [path],
            "state": "accepted",
            "method": "manual",
            "source_refs": [reference],
            "observed_at": timestamp,
            "confirmed_by": {"kind": "user", "id": actor_id},
            "confirmed_at": timestamp,
        }
        for path in sorted(changed)
    ]
    return True


def record_financial_edits(project: Project, product: Product, previous: dict, updated: dict, actor_id: str, revision: int):
    old_financials = previous.get("financials") or {}
    new_financials = updated.get("financials") or {}
    changed = {
        f"/configuration/financials/{field}"
        for field in old_financials.keys() | new_financials.keys()
        if old_financials.get(field) != new_financials.get(field)
    }
    if not changed:
        return
    retained = []
    for proof in project.provenance or []:
        paths = [path for path in proof.get("paths", []) if path not in changed]
        if paths:
            retained.append({**proof, "paths": paths})
    timestamp = utcnow().isoformat().replace("+00:00", "Z")
    reference = {
        "tenant_id": project.tenant_id,
        "project_id": project.id,
        "source_connection_id": "adp.profile",
        "record_type": "product",
        "record_id": product.id,
        "record_revision": str(revision),
    }
    for path in sorted(changed):
        selected_refs = (new_financials.get("initiatives") or []) if path.endswith("/initiatives") else []
        expiry = next(
            (
                proof.get("stale_after")
                for proof in project.provenance or []
                if proof.get("state") == "accepted"
                and proof.get("method") == "connector"
                and path in proof.get("paths", [])
                and any(ref in proof.get("source_refs", []) for ref in selected_refs)
            ),
            None,
        )
        retained.append(
            {
                "paths": [path],
                "state": "accepted",
                "method": "manual",
                "source_refs": [reference],
                "authority_mapping_ref": {**reference, "record_type": "authority_mapping", "record_id": "manual-editor", "record_revision": "1"},
                "observed_at": timestamp,
                "confirmed_by": {"kind": "user", "id": actor_id},
                "confirmed_at": timestamp,
                **({"stale_after": expiry} if expiry else {}),
            }
        )
    project.provenance = retained


def has_foreign_profile_ref(value, tenant_id: str, project_id: str, product_id: str) -> bool:
    if isinstance(value, list):
        return any(has_foreign_profile_ref(item, tenant_id, project_id, product_id) for item in value)
    if isinstance(value, dict):
        if {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"} <= value.keys():
            return (
                (value["tenant_id"], value["project_id"]) != (tenant_id, project_id)
                or (value["source_connection_id"] == "adp.profile" and value["record_type"] == "product" and value["record_id"] != product_id)
                or (value["source_connection_id"] == "adp.profile" and value["record_type"] == "project" and value["record_id"] != project_id)
            )
        return any(has_foreign_profile_ref(item, tenant_id, project_id, product_id) for item in value.values())
    return False


def has_unverified_source(value, tenant_id: str, project_id: str, permitted: set[tuple[str, str]] | None = None) -> bool:
    if isinstance(value, list):
        return any(has_unverified_source(item, tenant_id, project_id, permitted) for item in value)
    if isinstance(value, dict):
        if {"tenant_id", "project_id", "source_connection_id", "record_id", "record_revision"} <= value.keys():
            return not (
                authorized_profile_ref(value, tenant_id, project_id)
                or (
                    value["tenant_id"] == tenant_id
                    and value["project_id"] == project_id
                    and (value["source_connection_id"], value["record_id"]) in (permitted or set())
                )
            )
        return any(has_unverified_source(item, tenant_id, project_id, permitted) for item in value.values())
    return False


@router.get("/{product_id}/projects/{project_id}/changes", operation_id="listChanges")
async def list_changes(
    product_id: str,
    project_id: str,
    response: Response,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
    x_l5_delegation_id: str | None = Header(default=None),
):
    try:
        member, product, _ = await scoped_product(db, actor, product_id, project_id, x_l5_delegation_id)
    except HTTPException as error:
        if actor.account_type == "service" or error.status_code != 404:
            raise
        member, product, _ = await binding_owner(db, actor, product_id, project_id)
    if actor.account_type == "service":
        actor = actor.model_copy(update={"org_id": product.tenant_id})
    secret = get_settings().token_secret_key
    if not secret:
        raise HTTPException(503, detail={"code": "dependency_unavailable"})
    lower, upper = 0, product.revision
    expires_at = utcnow() + timedelta(minutes=15)
    if cursor is not None:
        try:
            if len(cursor) > 2048:
                raise ValueError("cursor too long")
            encoded, signature = cursor.split(".", 1)
            expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("invalid signature")
            tenant, principal, product_key, project_key, page_limit, lower, upper, expires = json.loads(base64.urlsafe_b64decode(encoded + "=="))
            if (tenant, principal, product_key, project_key, page_limit) != (actor.org_id, member.id, product_id, project_id, limit):
                raise ValueError("invalid scope")
            if type(lower) is not int or type(upper) is not int or not 0 <= lower < upper <= product.revision:
                raise ValueError("invalid bounds")
            expires_at = datetime.fromisoformat(expires)
        except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
            raise HTTPException(400, detail={"code": "invalid_cursor"}) from None
        if expires_at.tzinfo is None or expires_at <= utcnow():
            raise HTTPException(410, detail={"code": "revision_expired"})
    changes = (
        await db.scalars(
            select(ProductChange)
            .where(
                ProductChange.tenant_id == actor.org_id,
                ProductChange.product_id == product_id,
                change_in_project(project_id),
                ProductChange.revision > lower,
                ProductChange.revision <= upper,
            )
            .order_by(ProductChange.revision)
            .limit(limit + 1)
        )
    ).all()
    visible = changes[:limit]
    next_cursor = None
    if len(changes) > limit:
        next_cursor = change_cursor(
            [
                actor.org_id,
                member.id,
                product_id,
                project_id,
                limit,
                visible[-1].revision,
                upper,
                expires_at.isoformat(),
            ],
            secret,
        )
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [
            {
                "schema_version": change.schema_version,
                "event_id": change.id,
                "tenant_id": actor.org_id,
                "product_id": product_id,
                "project_id": project_id,
                "revision": change.revision,
                "actor_ref": {"kind": "user", "id": change.actor_id},
                "changed_paths": [path for path in change.changed_paths if path.startswith(("/product", "/project"))],
                "recorded_at": change.recorded_at.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z"),
                "source_revisions": [
                    reference
                    for reference in change.source_revisions
                    if isinstance(reference, dict)
                    and authorized_profile_ref(reference, actor.org_id, project_id)
                    and (actor.account_type != "service" or reference in member.sources)
                ],
                "relationship_versions": [
                    version
                    for version in change.relationship_versions
                    if isinstance(version, dict)
                    and authorized_profile_ref(version.get("from_ref", {}), actor.org_id, project_id)
                    and authorized_profile_ref(version.get("to_ref", {}), actor.org_id, project_id)
                    and (
                        actor.account_type != "service"
                        or all(version[ref].get("record_type") in {"product", "project"} for ref in ("from_ref", "to_ref"))
                    )
                    and all(
                        isinstance(reference, dict)
                        and authorized_profile_ref(reference, actor.org_id, project_id)
                        and (actor.account_type != "service" or reference.get("record_type") in {"product", "project"})
                        for reference in version.get("source_refs", [])
                    )
                ],
            }
            for change in visible
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{product_id}/projects/{project_id}/revisions/{revision}", operation_id="getRevision")
async def get_revision(
    product_id: str,
    project_id: str,
    revision: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
    x_l5_delegation_id: str | None = Header(default=None),
):
    try:
        member, product, project = await scoped_product(db, actor, product_id, project_id, x_l5_delegation_id)
    except HTTPException as error:
        if actor.account_type == "service" or error.status_code != 404:
            raise
        _, product, project = await binding_owner(db, actor, product_id, project_id)
    if actor.account_type == "service":
        actor = actor.model_copy(update={"org_id": product.tenant_id})
    change = await db.scalar(
        select(ProductChange).where(
            ProductChange.tenant_id == actor.org_id,
            ProductChange.product_id == product_id,
            ProductChange.revision == revision,
            change_in_project(project_id),
        )
    )
    if change is None or revision < 1 or revision > product.revision:
        raise HTTPException(404, detail={"code": "not_found"})
    if not change.snapshot or change.recorded_at.replace(tzinfo=UTC) <= utcnow() - timedelta(days=90):
        raise HTTPException(410, detail={"code": "revision_expired"})
    snapshot = json.loads(json.dumps(change.snapshot))
    if "scope_snapshots" in snapshot:
        snapshot = snapshot["scope_snapshots"].get(project_id, {})
    if snapshot.get("project_id") != project_id:
        legacy_fields = ("name", "purpose", "intended_users", "starting_point")
        if not (
            change.revision == 1
            and change.project_id == project_id
            and snapshot.get("project") == {"mode": "create"}
            and all(field in snapshot for field in legacy_fields)
        ):
            raise HTTPException(410, detail={"code": "revision_expired"})
        snapshot = {
            "schema_version": "1.0",
            "tenant_id": change.tenant_id,
            "product_id": change.product_id,
            "project_id": change.project_id,
            "revision": 1,
            "product": {
                "name": snapshot["name"],
                "purpose": snapshot["purpose"],
                "intended_users": snapshot["intended_users"],
                "owner_ref": {"kind": "user", "id": change.actor_id},
                "starting_point": snapshot["starting_point"],
                "lifecycle_status": "draft",
                "goals": [],
            },
            "project": {
                "id": change.project_id,
                "tenant_id": change.tenant_id,
                "name": snapshot["name"],
                "owner_user_id": change.actor_id,
                "state": "registered",
            },
            "configuration": {},
            "provenance": [],
        }
    from src.products.source_routes import permitted_source_refs, service_visible_bindings

    if has_foreign_profile_ref(snapshot, actor.org_id, project_id, product_id):
        raise HTTPException(404, detail={"code": "not_found"})

    snapshot = await projected_profile(
        db,
        actor,
        product,
        project,
        snapshot=snapshot,
        service_grant=member if actor.account_type == "service" else None,
    )
    if actor.account_type == "service":
        permitted = {(binding.installation_id or binding.credential_id, binding.native_id) for binding in await service_visible_bindings(db, member)}
    else:
        permitted = await permitted_source_refs(db, actor, project_id)
    if has_unverified_source(snapshot, actor.org_id, project_id, permitted):
        raise HTTPException(404, detail={"code": "not_found"})
    snapshot["context_status"] = "unavailable"
    response.headers["Profile-Revision"] = str(revision)
    response.headers["Cache-Control"] = "no-store"
    return snapshot


@router.post("/{product_id}/projects/{project_id}/peer-requests/{peer}", operation_id="requestProductPeer")
async def request_product_peer(
    product_id: str,
    project_id: str,
    peer: Literal["context", "l5"],
    if_profile_revision: int = Header(alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    _, product, _ = await scoped_product(db, actor, product_id, project_id)
    if product.revision != if_profile_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    raise HTTPException(503, detail={"code": "peer_integration_deferred", "peer": peer, **peer_availability()[peer]})


@router.get("/{product_id}/projects/{project_id}", operation_id="getProfile")
async def get_profile(
    product_id: str,
    project_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
    x_l5_delegation_id: str | None = Header(default=None),
):
    member, product, project = await scoped_product(db, actor, product_id, project_id, x_l5_delegation_id)
    effective_actor = actor.model_copy(update={"org_id": product.tenant_id}) if actor.account_type == "service" else actor
    result = await projected_profile(
        db,
        effective_actor,
        product,
        project,
        service_grant=member if actor.account_type == "service" else None,
    )
    response.headers["Profile-Revision"] = str(product.revision)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("", operation_id="listProducts")
async def list_products(
    response: Response,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member = await current_owner(db, actor)
    binding = exists().where(
        ProductProjectBinding.tenant_id == Product.tenant_id,
        ProductProjectBinding.product_id == Product.id,
        ProductProjectBinding.valid_to.is_(None),
    )
    query = select(Product).where(product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin), binding)
    if cursor is not None:
        try:
            timestamp, product_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            created_at = datetime.fromisoformat(timestamp)
            if not created_at.tzinfo or not re.fullmatch(r"[0-9a-f-]{36}", product_id):
                raise ValueError("invalid cursor")
        except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
            raise HTTPException(400, detail={"code": "invalid_cursor"}) from None
        query = query.where(or_(Product.created_at > created_at, and_(Product.created_at == created_at, Product.id > product_id)))
    products = (await db.scalars(query.order_by(Product.created_at, Product.id).limit(limit + 1))).all()
    visible = products[:limit]
    scope_rows = (
        (
            await db.execute(
                select(ProductProjectBinding.product_id, Project.id, Project.name, ProductProjectBinding.is_base)
                .join(Project, (Project.tenant_id == ProductProjectBinding.tenant_id) & (Project.id == ProductProjectBinding.project_id))
                .where(
                    ProductProjectBinding.tenant_id == actor.org_id,
                    ProductProjectBinding.product_id.in_([product.id for product in visible]),
                    ProductProjectBinding.valid_to.is_(None),
                )
                .order_by(ProductProjectBinding.is_base.desc(), Project.name, Project.id)
            )
        ).all()
        if visible
        else []
    )
    by_product: dict[str, list[dict]] = {product.id: [] for product in visible}
    for product_id, project_id, name, is_base in scope_rows:
        by_product[product_id].append({"id": project_id, "name": name, "is_base": is_base})
    teams = (
        (
            await db.scalars(
                select(Team).where(Team.org_id == actor.org_id, Team.id.in_([product.team_id for product in visible if product.team_id]))
            )
        ).all()
        if visible
        else []
    )
    team_names = {team.id: {"id": team.id, "name": team.name} for team in teams}
    next_cursor = None
    if len(products) > limit:
        last = visible[-1]
        created_at = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=UTC)
        next_cursor = base64.urlsafe_b64encode(json.dumps([created_at.isoformat(), last.id]).encode()).decode()
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [
            {
                "id": product.id,
                "name": product.name,
                "revision": product.revision,
                "lifecycle_status": product.lifecycle_status,
                "project_ids": [scope["id"] for scope in by_product[product.id]],
                "projects": by_product[product.id],
                "base_project_id": next((scope["id"] for scope in by_product[product.id] if scope["is_base"]), None),
                "team": team_names.get(product.team_id),
            }
            for product in visible
        ],
        "next_cursor": next_cursor,
    }


class BindProject(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: Literal["create", "select"] | None = None
    project_id: str | None = Field(default=None, min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def check_selection(self):
        if self.mode == "create":
            if self.project_id is not None or not self.name:
                raise ValueError("Creating a Project requires its name and no existing ID")
        elif self.project_id is None or self.name is not None:
            raise ValueError("Selecting a Project requires its ID and no new name")
        return self


@router.get("/{product_id}/projects/{project_id}/people", operation_id="getProductPeople", dependencies=[Depends(require_profile_admission)])
async def get_product_people(
    product_id: str,
    project_id: str,
    response: Response,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.products.people import product_people

    member, product, _ = await scoped_product(db, actor, product_id, project_id)
    response.headers["Cache-Control"] = "no-store"
    return await product_people(db, actor, member, product, offset=offset, limit=limit)


async def binding_owner(db: AsyncSession, actor: TokenContext, product_id: str, project_id: str):
    member = await current_owner(db, actor)
    retired_fixture = exists().where(
        ProductOperation.tenant_id == Product.tenant_id,
        ProductOperation.product_id == Product.id,
        ProductOperation.operation == "createProduct",
        ProductOperation.receipt["fixture_removed"].as_boolean().is_(True),
    )
    product = await db.scalar(
        select(Product).where(
            product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin), Product.id == product_id, ~retired_fixture
        )
    )
    project = await db.scalar(select(Project).where(Project.tenant_id == actor.org_id, Project.id == project_id))
    if product is None or project is None or product.owner_user_id != member.id or project.owner_user_id != member.id:
        raise HTTPException(404, detail={"code": "not_found"})
    retired_fixture = await db.scalar(
        select(ProductOperation.id)
        .where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.product_id == product_id,
            ProductOperation.operation == "createProduct",
            ProductOperation.receipt["fixture_removed"].as_boolean().is_(True),
        )
        .limit(1)
    )
    if retired_fixture is not None:
        raise HTTPException(404, detail={"code": "not_found"})
    return member, product, project


async def binding_mutation(
    operation_name: str,
    product_id: str,
    project_id: str,
    response: Response,
    idempotency_key: str | None,
    expected_revision: int | None,
    db: AsyncSession,
    actor: TokenContext,
    new_project_name: str | None = None,
):
    if new_project_name is None:
        member, product, project = await binding_owner(db, actor, product_id, project_id)
    else:
        member = await current_owner(db, actor)
        product = await db.scalar(
            select(Product).where(product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin), Product.id == product_id)
        )
        if product is None or product.owner_user_id != member.id:
            raise HTTPException(404, detail={"code": "not_found"})
        project = None
    if idempotency_key is None or expected_revision is None:
        raise HTTPException(428, detail={"code": "precondition_required"})
    if not KEY_PATTERN.fullmatch(idempotency_key) or not 1 <= expected_revision <= 9007199254740991:
        raise HTTPException(422, detail={"code": "validation_error"})
    body = {"product_id": product_id, "project_id": project_id, "expected_revision": expected_revision}
    if new_project_name is not None:
        body["name"] = new_project_name
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    operation_query = select(ProductOperation).where(
        ProductOperation.tenant_id == actor.org_id,
        ProductOperation.actor_id == member.id,
        ProductOperation.operation == operation_name,
        ProductOperation.key_hash == key_hash,
    )

    async def replay(existing: ProductOperation):
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.receipt is None or existing.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
        response.headers["Profile-Revision"] = str(existing.receipt["revision"])
        response.headers["Cache-Control"] = "no-store"
        scope_id = existing.receipt["profile"]["project_id"]
        scoped_project = await db.get(Project, (actor.org_id, scope_id))
        if scoped_project is None or scoped_project.owner_user_id != member.id:
            raise HTTPException(404, detail={"code": "not_found"})
        return await projected_profile(db, actor, product, scoped_project, existing.receipt["profile"])

    existing = await db.scalar(operation_query)
    if existing is not None:
        return await replay(existing)
    if product.archived_at is not None:
        raise HTTPException(409, detail={"code": "product_archived"})
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    if new_project_name is None:
        current = await db.scalar(
            select(ProductProjectBinding).where(
                ProductProjectBinding.tenant_id == actor.org_id,
                ProductProjectBinding.project_id == project_id,
                ProductProjectBinding.valid_to.is_(None),
            )
        )
    else:
        current = None
        project = Project(id=new_uuid(), tenant_id=actor.org_id, name=new_project_name, owner_user_id=member.id)
        project_id = project.id
    if operation_name == "bindProject":
        if current is not None and current.product_id != product_id:
            raise HTTPException(409, detail={"code": "binding_conflict"})
        changed = current is None
    else:
        if current is None or current.product_id != product_id:
            raise HTTPException(404, detail={"code": "not_found"})
        changed = True
    now = utcnow()
    if operation_name == "unbindProject":
        remaining = await db.scalar(
            select(Project)
            .join(
                ProductProjectBinding,
                (ProductProjectBinding.tenant_id == Project.tenant_id) & (ProductProjectBinding.project_id == Project.id),
            )
            .where(
                ProductProjectBinding.tenant_id == actor.org_id,
                ProductProjectBinding.product_id == product_id,
                ProductProjectBinding.project_id != project_id,
                ProductProjectBinding.valid_to.is_(None),
            )
            .order_by(Project.id)
        )
        if remaining is None:
            raise HTTPException(409, detail={"code": "last_project"})
        if current.is_base:
            raise HTTPException(409, detail={"code": "base_project_required", "message": "The base Project stays with its Product."})
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
    clear_home = False
    if operation_name == "unbindProject" and product.delivery_home_repository is not None:
        remaining_sources = await product_github_bindings(db, product, exclude_project_id=project_id)
        home_id = product.delivery_home_repository["repository_id"]
        clear_home = not any(
            source.native_id.isascii() and source.native_id.isdecimal() and int(source.native_id) == home_id for source in remaining_sources
        )
        if clear_home:
            product.delivery_home_repository = None
            product.provenance = [
                *(product.provenance or []),
                {
                    "path": "/product/delivery_home_repository",
                    "actor_id": member.id,
                    "recorded_at": now.isoformat().replace("+00:00", "Z"),
                    "revision": product.revision,
                },
            ]
    if new_project_name is not None:
        db.add(project)
        await db.flush()
    if changed and operation_name == "bindProject":
        current = ProductProjectBinding(
            id=new_uuid(),
            tenant_id=actor.org_id,
            product_id=product_id,
            project_id=project_id,
            relationship_revision=1,
            mapping_version="profile-registry-1",
            source_connection_id="adp.profile",
            valid_from=now,
            recorded_at=now,
            provenance={"actor_id": member.id, "source": "direct_input"},
        )
        db.add(current)
        await db.flush()
    elif changed:
        closed_at = max(now, current.valid_from.replace(tzinfo=UTC) + timedelta(microseconds=1))
        current.valid_to = closed_at
        current.supersedes_revision = current.relationship_revision
        current.relationship_revision += 1
    result = profile(product, project if operation_name == "bindProject" else remaining)
    operation = ProductOperation(
        id=new_uuid(),
        tenant_id=actor.org_id,
        actor_id=member.id,
        operation=operation_name,
        key_hash=key_hash,
        request_hash=digest,
        request=body,
        product_id=product_id,
        project_id=project_id,
        receipt={"revision": product.revision, "profile": result},
        expires_at=now + timedelta(days=1),
    )
    try:
        db.add(operation)
        await db.flush()
        if changed:
            change = ProductChange(
                id=new_uuid(),
                tenant_id=actor.org_id,
                product_id=product_id,
                project_id=project_id,
                revision=product.revision,
                actor_id=member.id,
                operation_id=operation.id,
                changed_paths=["/project/binding", "/product/delivery_home_repository"] if clear_home else ["/project/binding"],
                snapshot=profile(product, project) if operation_name == "unbindProject" else result,
                relationship_versions=[],
                recorded_at=now,
            )
            change.relationship_versions = [registry_relationship(current, change.id, now)]
            db.add(change)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(operation_query)
        if existing is not None:
            return await replay(existing)
        raise HTTPException(409, detail={"code": "binding_conflict"}) from None
    response.headers["Profile-Revision"] = str(product.revision)
    response.headers["Cache-Control"] = "no-store"
    return await projected_profile(db, actor, product, project if operation_name == "bindProject" else remaining, result)


@router.post("/{product_id}/projects", operation_id="bindProject", dependencies=[Depends(require_profile_admission)])
async def bind_project(
    product_id: str,
    request: BindProject,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    if request.mode == "create":
        response.status_code = 201
    return await binding_mutation(
        "bindProject", product_id, request.project_id, response, idempotency_key, expected_revision, db, actor, request.name
    )


@router.delete("/{product_id}/projects/{project_id}", operation_id="unbindProject", dependencies=[Depends(require_profile_admission)])
async def unbind_project(
    product_id: str,
    project_id: str,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    return await binding_mutation("unbindProject", product_id, project_id, response, idempotency_key, expected_revision, db, actor)


@router.get("/{product_id}/projects/{project_id}/project-bindings", operation_id="listProjectBindings")
async def list_project_bindings(
    product_id: str,
    project_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
    x_l5_delegation_id: str | None = Header(default=None),
):
    try:
        _, product, _ = await scoped_product(db, actor, product_id, project_id, x_l5_delegation_id)
    except HTTPException:
        if actor.account_type == "service":
            raise
        await binding_owner(db, actor, product_id, project_id)
    else:
        if actor.account_type == "service":
            actor = actor.model_copy(update={"org_id": product.tenant_id})
    history = (
        await db.scalars(
            select(ProductChange)
            .where(
                ProductChange.tenant_id == actor.org_id,
                ProductChange.product_id == product_id,
                ProductChange.project_id == project_id,
            )
            .order_by(ProductChange.revision)
        )
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [
            {"revision": change.revision, "relationship_versions": change.relationship_versions} for change in history if change.relationship_versions
        ]
    }


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    configuration: ProjectConfiguration | None = None


@router.patch("/{product_id}/projects/{project_id}/configuration", operation_id="updateProject", dependencies=[Depends(require_profile_admission)])
async def update_project(
    product_id: str,
    project_id: str,
    patch: ProjectPatch,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, project = await scoped_product(db, actor, product_id, project_id)
    if idempotency_key is None or expected_revision is None:
        raise HTTPException(428, detail={"code": "precondition_required"})
    if not KEY_PATTERN.fullmatch(idempotency_key) or not 1 <= expected_revision <= 9007199254740991:
        raise HTTPException(422, detail={"code": "validation_error"})
    fields = patch.model_dump(exclude_unset=True)
    if has_foreign_profile_ref(fields.get("configuration", {}), actor.org_id, project_id, product_id):
        raise HTTPException(404, detail={"code": "not_found"})
    if not fields or any(value is None for value in fields.values()):
        raise HTTPException(422, detail={"code": "validation_error"})
    if patch.configuration and any(
        choice.state == "not_applicable" and choice.check_id in required_checks(product) for choice in patch.configuration.applicability or []
    ):
        raise HTTPException(422, detail={"code": "required_check"})
    if len(json.dumps(fields.get("configuration", {})).encode()) > 256 * 1024:
        raise HTTPException(422, detail={"code": "validation_error"})
    body = {"product_id": product_id, "project_id": project_id, "expected_revision": expected_revision, "patch": fields}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    operation_query = select(ProductOperation).where(
        ProductOperation.tenant_id == actor.org_id,
        ProductOperation.actor_id == member.id,
        ProductOperation.operation == "updateProject",
        ProductOperation.key_hash == key_hash,
    )
    existing = await db.scalar(operation_query)
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.receipt is None or existing.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
        response.headers["Profile-Revision"] = str(existing.receipt["revision"])
        response.headers["Cache-Control"] = "no-store"
        return await projected_profile(db, actor, product, project, existing.receipt["profile"])
    if product.archived_at is not None:
        raise HTTPException(409, detail={"code": "product_archived"})
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    if patch.configuration is not None:
        await validate_financial_targets(db, patch.configuration.financials, product, project, actor)
    if "configuration" in fields:
        bindings = (
            await db.scalars(
                select(ProductSourceBinding).where(
                    ProductSourceBinding.tenant_id == actor.org_id,
                    ProductSourceBinding.project_id == project_id,
                    ProductSourceBinding.kind == "environment",
                    ProductSourceBinding.status.in_(("healthy", "degraded")),
                    ProductSourceBinding.unlinked_at.is_(None),
                )
            )
        ).all()
        references = [
            {
                "tenant_id": actor.org_id,
                "project_id": project_id,
                "source_connection_id": binding.credential_id,
                "record_type": "environment",
                "record_id": binding.native_id,
                "record_revision": binding.observed["source_revision"],
            }
            for binding in bindings
            if binding.credential_id and binding.observed and binding.observed.get("source_revision")
        ]
        if references:
            configuration = fields["configuration"]
            basic = dict(configuration.get("basic") or {})
            basic["environments"] = [*(basic.get("environments") or []), *references]
            fields["configuration"] = normalize_environments({**configuration, "basic": basic})
    changed = {field: value for field, value in fields.items() if getattr(project, field) != value}
    if "configuration" in changed:
        record_financial_edits(project, product, project.configuration, changed["configuration"], member.id, expected_revision + 1)
    if "configuration" in fields and record_basic_source_edits(
        project, product, project.configuration, fields["configuration"], member.id, expected_revision + 1
    ):
        changed["provenance"] = project.provenance
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
        for field, value in changed.items():
            setattr(project, field, value)
        project.updated_at = now
        await db.flush()
    result = profile(product, project)
    operation = ProductOperation(
        id=new_uuid(),
        tenant_id=actor.org_id,
        actor_id=member.id,
        operation="updateProject",
        key_hash=key_hash,
        request_hash=digest,
        request=body,
        product_id=product_id,
        project_id=project_id,
        receipt={"revision": product.revision, "profile": result},
        expires_at=now + timedelta(days=1),
    )
    try:
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
                    changed_paths=[f"/project/{field}" for field in changed],
                    snapshot=result,
                )
            )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    response.headers["Profile-Revision"] = str(product.revision)
    response.headers["Cache-Control"] = "no-store"
    return await projected_profile(db, actor, product, project, result)
