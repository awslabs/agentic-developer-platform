"""Authorized Portfolio catalog and optional Product membership."""

import base64
import binascii
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.catalog.models import CatalogChange, CatalogReceipt, MembershipHead, MembershipVersion, PortfolioHead, PortfolioVersion
from src.products.models import Product, ProductProjectBinding
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.identity.product_scope import product_member, product_visibility_scope
from src.shared.models.base import new_uuid, utcnow
from src.shared.schemas.auth import TokenContext


class CatalogRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except HTTPException as error:
                if isinstance(error.detail, dict) and "code" in error.detail:
                    return JSONResponse(status_code=error.status_code, content=error.detail)
                raise
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"code": "invalid_request", "message": "Invalid request"})

        return handle


router = APIRouter(prefix="/v1/catalog/portfolios", tags=["catalog"], route_class=CatalogRoute)
CATALOG_SOURCE = "adp.catalog"
PRODUCT_SOURCE = "adp.profile"


def fail(status: int, code: str):
    raise HTTPException(status, detail={"code": code, "message": code.replace("_", " ")})


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["user", "team", "service"]
    id: str = Field(min_length=1, max_length=512)


class PortfolioData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    owner_ref: Principal
    lifecycle_status: Literal["draft", "active", "retired"]
    objective: str | None = Field(default=None, min_length=1, max_length=10000)


class RecordRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    source_connection_id: str = Field(min_length=1)
    record_type: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    record_revision: str = Field(pattern=r"^[1-9][0-9]*$")


class CreateMembership(BaseModel):
    model_config = ConfigDict(extra="forbid")
    portfolio_ref: RecordRef
    product_ref: RecordRef
    valid_from: datetime
    valid_to: datetime | None = None
    source_refs: list[RecordRef] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def check_interval(self):
        if self.valid_from.tzinfo is None or (self.valid_to is not None and (self.valid_to.tzinfo is None or self.valid_to <= self.valid_from)):
            raise ValueError("valid_to must be later than timezone-aware valid_from")
        return self


class CloseMembership(BaseModel):
    model_config = ConfigDict(extra="forbid")
    valid_to: datetime


def instant(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def wire_time(value: datetime) -> str:
    return instant(value).isoformat().replace("+00:00", "Z")


async def scope_digest(db: AsyncSession, actor: TokenContext, member, editable: bool) -> str:
    bindings = (
        await db.execute(
            select(ProductProjectBinding.project_id, Product.id, Product.revision)
            .join(Product, (Product.tenant_id == ProductProjectBinding.tenant_id) & (Product.id == ProductProjectBinding.product_id))
            .where(
                product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin),
                Product.archived_at.is_(None),
                ProductProjectBinding.valid_to.is_(None),
            )
        )
    ).all()
    visible_pairs = {(product_id, project_id) for project_id, product_id, _ in bindings}
    memberships = (
        await db.execute(
            select(MembershipHead.relationship_id, MembershipHead.relationship_revision, MembershipHead.product_id, MembershipHead.project_id).where(
                MembershipHead.tenant_id == actor.org_id
            )
        )
    ).all()
    visible_edges = sorted(
        (relationship_id, revision) for relationship_id, revision, product_id, project_id in memberships if (product_id, project_id) in visible_pairs
    )
    scope = {"bindings": sorted(tuple(row) for row in bindings), "edges": visible_edges, "editor": editable, "source": CATALOG_SOURCE}
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def page_cursor(actor: TokenContext, route: str, limit: int, digest: str, watermark: datetime, last: str) -> str:
    secret = get_settings().token_secret_key
    if not secret:
        fail(503, "dependency_unavailable")
    payload = [
        actor.org_id,
        actor.user_id,
        route,
        limit,
        digest,
        instant(watermark).isoformat(),
        last,
        (utcnow() + timedelta(minutes=15)).isoformat(),
    ]
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def read_cursor(cursor: str, actor: TokenContext, route: str, limit: int, digest: str) -> tuple[datetime, str]:
    secret = get_settings().token_secret_key
    try:
        if not secret or len(cursor) > 2048:
            raise ValueError("invalid cursor")
        encoded, signature = cursor.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=="))
        if (
            not isinstance(payload, list)
            or len(payload) != 8
            or payload[:5] != [actor.org_id, actor.user_id, route, limit, digest]
            or instant(datetime.fromisoformat(payload[7])) <= utcnow()
        ):
            raise ValueError("invalid scope")
        watermark = datetime.fromisoformat(payload[5])
        if watermark.tzinfo is None or not isinstance(payload[6], str):
            raise ValueError("invalid time")
        return watermark, payload[6]
    except (ValueError, TypeError, IndexError, UnicodeDecodeError, binascii.Error):
        fail(400, "invalid_cursor")


async def member_for(db: AsyncSession, actor: TokenContext):
    member = await product_member(
        db,
        user_id=actor.user_id,
        org_id=actor.org_id,
        username=actor.cognito_username,
        account_type=actor.account_type,
        is_platform_admin=actor.is_admin,
    )
    if member is None:
        fail(403, "permission_denied")
    return member


async def can_edit(db: AsyncSession, actor: TokenContext) -> bool:
    try:
        await AccessControl(db).check_permission(actor, Permission.ORG_UPDATE, target_org_id=actor.org_id)
    except AccessDeniedError:
        return False
    return True


async def editor(db: AsyncSession, actor: TokenContext):
    await member_for(db, actor)
    if not await can_edit(db, actor):
        fail(403, "permission_denied")


def admitted():
    if os.getenv("BG_CATALOG_ENABLED", "false").lower() != "true":
        fail(503, "catalog_disabled")


def preconditions(key: str | None, revision: str | None = None, *, patch: bool = False):
    if not key or (patch and not revision):
        fail(428, "precondition_required")
    if len(key) > 128 or (patch and (not revision.isdecimal() or int(revision) < 1)):
        fail(400, "invalid_request")


async def replay(db: AsyncSession, actor: TokenContext, operation: str, key: str, payload: dict):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = await db.get(CatalogReceipt, (actor.org_id, actor.user_id, operation, key))
    if existing:
        if existing.payload_digest != digest:
            fail(409, "conflict")
        return existing.outcome, digest
    return None, digest


def receipt(db: AsyncSession, actor: TokenContext, operation: str, key: str, digest: str, changed: PortfolioVersion | MembershipVersion):
    membership = isinstance(changed, MembershipVersion)
    record_id = changed.relationship_id if membership else changed.id
    revision = changed.relationship_revision if membership else changed.revision
    change_id = new_uuid()
    db.add(
        CatalogChange(
            tenant_id=actor.org_id,
            id=change_id,
            actor_id=actor.user_id,
            source_connection_id=changed.source_connection_id,
            record_type="portfolio_product" if membership else "portfolio",
            record_id=record_id,
            native_record_revision=str(changed.product_revision if membership else changed.revision),
            relationship_revision=changed.relationship_revision if membership else None,
            mapping_version=changed.mapping_version if membership else "catalog-1",
            valid_from=changed.valid_from if membership else None,
            valid_to=changed.valid_to if membership else None,
            source_refs=changed.source_refs if membership else [],
            recorded_at=changed.recorded_at,
        )
    )
    outcome = {"id": record_id, "revision": str(revision), "change_id": change_id}
    db.add(
        CatalogReceipt(
            tenant_id=actor.org_id, actor_id=actor.user_id, operation=operation, idempotency_key=key, payload_digest=digest, outcome=outcome
        )
    )
    return outcome


def portfolio_view(row: PortfolioVersion):
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "revision": str(row.revision),
        "source_connection_id": row.source_connection_id,
        "recorded_at": wire_time(row.recorded_at),
        "data": row.data,
    }


async def visible_product(
    db: AsyncSession, member, tenant: str, product_id: str, project_id: str, lock: bool = False, *, is_platform_admin: bool = False
):
    query = (
        select(Product)
        .join(
            ProductProjectBinding,
            (ProductProjectBinding.tenant_id == Product.tenant_id)
            & (ProductProjectBinding.product_id == Product.id)
            & (ProductProjectBinding.project_id == project_id)
            & ProductProjectBinding.valid_to.is_(None),
        )
        .where(product_visibility_scope(member, tenant, is_platform_admin=is_platform_admin), Product.id == product_id, Product.archived_at.is_(None))
    )
    return await db.scalar(query.with_for_update() if lock else query)


async def visible_memberships(
    db: AsyncSession, actor: TokenContext, member, portfolio_id: str, effective_at: datetime | None = None, decision_at: datetime | None = None
):
    effective_time = instant(effective_at or utcnow())
    rows = (
        await db.scalars(
            select(MembershipVersion)
            .where(MembershipVersion.tenant_id == actor.org_id, MembershipVersion.portfolio_id == portfolio_id)
            .order_by(MembershipVersion.relationship_id, MembershipVersion.relationship_revision)
        )
    ).all()
    selected = {}
    for row in rows:
        if decision_at is not None and instant(row.recorded_at) > instant(decision_at):
            continue
        selected[row.relationship_id] = row
    return [
        row
        for row in selected.values()
        if instant(row.valid_from) <= effective_time
        and (row.valid_to is None or effective_time < instant(row.valid_to))
        and await visible_product(db, member, actor.org_id, row.product_id, row.project_id, is_platform_admin=actor.is_admin)
    ]


async def visible_portfolio(
    db: AsyncSession,
    actor: TokenContext,
    portfolio_id: str,
    member=None,
    effective_at: datetime | None = None,
    decision_at: datetime | None = None,
    lock: bool = False,
):
    if member is None:
        try:
            member = await member_for(db, actor)
        except HTTPException:
            fail(404, "not_found")
    query = (
        select(PortfolioVersion)
        .join(
            PortfolioHead,
            (PortfolioHead.tenant_id == PortfolioVersion.tenant_id)
            & (PortfolioHead.source_connection_id == PortfolioVersion.source_connection_id)
            & (PortfolioHead.id == PortfolioVersion.id)
            & (PortfolioHead.revision == PortfolioVersion.revision),
        )
        .where(
            PortfolioVersion.tenant_id == actor.org_id, PortfolioVersion.source_connection_id == CATALOG_SOURCE, PortfolioVersion.id == portfolio_id
        )
    )
    row = await db.scalar(query.with_for_update() if lock else query)
    if row is None or (not await can_edit(db, actor) and not await visible_memberships(db, actor, member, portfolio_id, effective_at, decision_at)):
        fail(404, "not_found")
    return row


@router.post("", status_code=201)
async def create_portfolio(
    data: PortfolioData,
    key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    admitted()
    await editor(db, actor)
    preconditions(key)
    payload = data.model_dump(exclude_none=True)
    previous, digest = await replay(db, actor, "create_portfolio", key, payload)
    if previous:
        return previous
    row = PortfolioVersion(tenant_id=actor.org_id, source_connection_id=CATALOG_SOURCE, id=new_uuid(), revision=1, data=payload, recorded_at=utcnow())
    db.add(row)
    await db.flush()
    db.add(PortfolioHead(tenant_id=actor.org_id, source_connection_id=CATALOG_SOURCE, id=row.id, revision=1))
    result = receipt(db, actor, "create_portfolio", key, digest, row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        fail(409, "conflict")
    return result


@router.patch("/{portfolio_id}")
async def edit_portfolio(
    portfolio_id: str,
    data: PortfolioData,
    key: str | None = Header(default=None, alias="Idempotency-Key"),
    revision: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    admitted()
    await editor(db, actor)
    row = await visible_portfolio(db, actor, portfolio_id, lock=True)
    preconditions(key, revision, patch=True)
    payload = {"data": data.model_dump(exclude_none=True), "revision": revision}
    operation = f"edit_portfolio:{portfolio_id}"
    previous, digest = await replay(db, actor, operation, key, payload)
    if previous:
        return previous
    if row.revision != int(revision):
        fail(412, "stale_revision")
    next_version = PortfolioVersion(
        tenant_id=row.tenant_id,
        source_connection_id=row.source_connection_id,
        id=row.id,
        revision=row.revision + 1,
        data=payload["data"],
        recorded_at=utcnow(),
    )
    db.add(next_version)
    await db.flush()
    head = await db.get(PortfolioHead, (row.tenant_id, row.source_connection_id, row.id))
    head.revision = next_version.revision
    result = receipt(db, actor, operation, key, digest, next_version)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        fail(409, "conflict")
    return result


@router.get("")
async def list_portfolios(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    try:
        member = await member_for(db, actor)
    except HTTPException:
        if cursor:
            fail(400, "invalid_cursor")
        raise
    editable = await can_edit(db, actor)
    digest = await scope_digest(db, actor, member, editable)
    watermark, last = read_cursor(cursor, actor, "portfolios", limit, digest) if cursor else (utcnow(), "")
    rows = (
        await db.scalars(
            select(PortfolioVersion)
            .where(PortfolioVersion.tenant_id == actor.org_id, PortfolioVersion.source_connection_id == CATALOG_SOURCE, PortfolioVersion.id > last)
            .order_by(PortfolioVersion.id, PortfolioVersion.revision)
        )
    ).all()
    snapshot = {}
    for row in rows:
        if instant(row.recorded_at) <= watermark:
            snapshot[row.id] = row
    visible = [row for row in snapshot.values() if editable or await visible_memberships(db, actor, member, row.id)]
    page = visible[:limit]
    result = {"items": [portfolio_view(row) for row in page], "can_edit": editable}
    if len(visible) > limit:
        result["next_cursor"] = page_cursor(actor, "portfolios", limit, digest, watermark, page[-1].id)
    return result


@router.get("/{portfolio_id}")
async def get_portfolio(portfolio_id: str, db: AsyncSession = Depends(get_db), actor: TokenContext = Depends(get_current_user)):
    return portfolio_view(await visible_portfolio(db, actor, portfolio_id))


def membership_view(row: MembershipVersion):
    return {
        "tenant_id": row.tenant_id,
        "relationship_id": row.relationship_id,
        "relationship_revision": str(row.relationship_revision),
        **({"supersedes_revision": str(row.supersedes_revision)} if row.supersedes_revision else {}),
        "portfolio_ref": {
            "tenant_id": row.tenant_id,
            "project_id": row.project_id,
            "source_connection_id": row.portfolio_source,
            "record_type": "portfolio",
            "record_id": row.portfolio_id,
            "record_revision": str(row.portfolio_revision),
        },
        "product_ref": {
            "tenant_id": row.tenant_id,
            "project_id": row.project_id,
            "source_connection_id": row.product_source,
            "record_type": "product",
            "record_id": row.product_id,
            "record_revision": str(row.product_revision),
        },
        "valid_from": wire_time(row.valid_from),
        **({"valid_to": wire_time(row.valid_to)} if row.valid_to else {}),
        "recorded_at": wire_time(row.recorded_at),
        "mapping_version": row.mapping_version,
        "source_connection_id": row.source_connection_id,
        "source_refs": row.source_refs,
    }


@router.get("/{portfolio_id}/memberships")
async def list_memberships(
    portfolio_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    effective_at: datetime | None = None,
    decision_at: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    try:
        member = await member_for(db, actor)
    except HTTPException:
        fail(404, "not_found")
    await visible_portfolio(db, actor, portfolio_id, member, effective_at, decision_at)
    if (
        effective_at is not None
        and effective_at.tzinfo is None
        or decision_at is not None
        and (decision_at.tzinfo is None or instant(decision_at) > utcnow())
    ):
        fail(400, "invalid_request")
    editable = await can_edit(db, actor)
    digest = await scope_digest(db, actor, member, editable)
    route = f"membership:{portfolio_id}:{effective_at}:{decision_at}"
    watermark, last = read_cursor(cursor, actor, route, limit, digest) if cursor else (utcnow(), "")
    rows = await visible_memberships(db, actor, member, portfolio_id, effective_at, decision_at or (watermark if cursor else None))
    rows = [row for row in rows if row.relationship_id > last]
    response.headers["X-Catalog-View"] = "historical" if effective_at or decision_at else "current"
    page = rows[:limit]
    result = {"items": [membership_view(row) for row in page]}
    if len(rows) > limit:
        result["next_cursor"] = page_cursor(actor, route, limit, digest, watermark, page[-1].relationship_id)
    return result


@router.post("/{portfolio_id}/memberships", status_code=201)
async def add_membership(
    portfolio_id: str,
    data: CreateMembership,
    key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    admitted()
    await editor(db, actor)
    portfolio = await visible_portfolio(db, actor, portfolio_id, lock=True)
    reference, product_reference = data.portfolio_ref, data.product_ref
    if (
        reference.tenant_id != actor.org_id
        or reference.record_type != "portfolio"
        or reference.source_connection_id != CATALOG_SOURCE
        or reference.record_id != portfolio_id
        or product_reference.tenant_id != actor.org_id
        or product_reference.record_type != "product"
        or product_reference.source_connection_id != PRODUCT_SOURCE
        or product_reference.project_id != reference.project_id
    ):
        fail(422, "invalid_request")
    member = await member_for(db, actor)
    product = await visible_product(
        db, member, actor.org_id, product_reference.record_id, product_reference.project_id, lock=True, is_platform_admin=actor.is_admin
    )
    if product is None:
        fail(404, "not_found")
    verified_refs = {json.dumps(reference.model_dump(), sort_keys=True), json.dumps(product_reference.model_dump(), sort_keys=True)}
    if any(json.dumps(source_ref.model_dump(), sort_keys=True) not in verified_refs for source_ref in data.source_refs):
        fail(422, "invalid_request")
    preconditions(key)
    payload = data.model_dump(mode="json", exclude_none=True)
    operation = f"add_membership:{portfolio_id}"
    previous, digest = await replay(db, actor, operation, key, payload)
    if previous:
        return previous
    if int(reference.record_revision) != portfolio.revision or product.revision != int(product_reference.record_revision):
        fail(412, "stale_revision")
    earlier_head = await db.scalar(
        select(MembershipHead)
        .where(
            MembershipHead.tenant_id == actor.org_id,
            MembershipHead.portfolio_source == CATALOG_SOURCE,
            MembershipHead.portfolio_id == portfolio_id,
            MembershipHead.product_source == PRODUCT_SOURCE,
            MembershipHead.product_id == product.id,
        )
        .with_for_update()
    )
    if earlier_head is not None:
        earlier = await db.get(MembershipVersion, (actor.org_id, earlier_head.relationship_id, earlier_head.relationship_revision))
        if earlier.valid_to is None or instant(data.valid_from) < instant(earlier.valid_to):
            fail(409, "conflict")
        await db.delete(earlier_head)
        await db.flush()
    row = MembershipVersion(
        tenant_id=actor.org_id,
        relationship_id=new_uuid(),
        relationship_revision=1,
        portfolio_source=CATALOG_SOURCE,
        portfolio_id=portfolio_id,
        portfolio_revision=portfolio.revision,
        product_source=PRODUCT_SOURCE,
        product_id=product.id,
        product_revision=product.revision,
        project_id=product_reference.project_id,
        source_connection_id=CATALOG_SOURCE,
        mapping_version="catalog-1",
        source_refs=payload["source_refs"],
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        recorded_at=utcnow(),
    )
    db.add(row)
    await db.flush()
    db.add(
        MembershipHead(
            tenant_id=actor.org_id,
            relationship_id=row.relationship_id,
            relationship_revision=1,
            portfolio_source=CATALOG_SOURCE,
            portfolio_id=portfolio_id,
            product_source=PRODUCT_SOURCE,
            product_id=product.id,
            project_id=product_reference.project_id,
        )
    )
    result = receipt(db, actor, operation, key, digest, row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        fail(409, "conflict")
    return result


@router.patch("/{portfolio_id}/memberships/{relationship_id}")
async def close_membership(
    portfolio_id: str,
    relationship_id: str,
    data: CloseMembership,
    key: str | None = Header(default=None, alias="Idempotency-Key"),
    revision: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    admitted()
    await editor(db, actor)
    await visible_portfolio(db, actor, portfolio_id)
    head = await db.scalar(
        select(MembershipHead)
        .where(
            MembershipHead.tenant_id == actor.org_id,
            MembershipHead.portfolio_source == CATALOG_SOURCE,
            MembershipHead.portfolio_id == portfolio_id,
            MembershipHead.relationship_id == relationship_id,
        )
        .with_for_update()
    )
    if head is None:
        fail(404, "not_found")
    row = await db.get(MembershipVersion, (actor.org_id, relationship_id, head.relationship_revision))
    member = await member_for(db, actor)
    if not await visible_product(db, member, actor.org_id, row.product_id, row.project_id, lock=True, is_platform_admin=actor.is_admin):
        fail(404, "not_found")
    preconditions(key, revision, patch=True)
    if data.valid_to.tzinfo is None or instant(data.valid_to) <= instant(row.valid_from):
        fail(422, "invalid_request")
    payload = {"valid_to": instant(data.valid_to).isoformat(), "revision": revision}
    operation = f"close_membership:{relationship_id}"
    previous, digest = await replay(db, actor, operation, key, payload)
    if previous:
        return previous
    if row.relationship_revision != int(revision):
        fail(412, "stale_revision")
    next_version = MembershipVersion(
        tenant_id=row.tenant_id,
        relationship_id=row.relationship_id,
        relationship_revision=row.relationship_revision + 1,
        portfolio_source=row.portfolio_source,
        portfolio_id=row.portfolio_id,
        portfolio_revision=row.portfolio_revision,
        product_source=row.product_source,
        product_id=row.product_id,
        product_revision=row.product_revision,
        project_id=row.project_id,
        source_connection_id=row.source_connection_id,
        mapping_version=row.mapping_version,
        source_refs=row.source_refs,
        valid_from=row.valid_from,
        valid_to=data.valid_to,
        supersedes_revision=row.relationship_revision,
        recorded_at=utcnow(),
    )
    db.add(next_version)
    await db.flush()
    head.relationship_revision = next_version.relationship_revision
    result = receipt(db, actor, operation, key, digest, next_version)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        fail(409, "conflict")
    return result
