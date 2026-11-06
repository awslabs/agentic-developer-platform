"""Scoped Initiative commitments that refer to native work without owning it."""

import hashlib
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.catalog.models import (
    CatalogChange,
    CatalogReceipt,
    ContributionHead,
    ContributionVersion,
    InitiativeHead,
    InitiativeVersion,
)
from src.catalog.routes import (
    CATALOG_SOURCE,
    PRODUCT_SOURCE,
    CatalogRoute,
    Principal,
    RecordRef,
    admitted,
    can_edit,
    editor,
    fail,
    instant,
    member_for,
    page_cursor,
    preconditions,
    read_cursor,
    replay,
    scope_digest,
    visible_product,
    wire_time,
)
from src.products.models import Product, ProductProjectBinding, Project
from src.shared.database import get_db
from src.shared.identity.product_scope import product_visibility_scope
from src.shared.models.base import new_uuid, utcnow
from src.shared.schemas.auth import TokenContext

initiative_router = APIRouter(prefix="/v1/catalog/initiatives", tags=["catalog"], route_class=CatalogRoute)
contribution_router = APIRouter(prefix="/v1/catalog/projects/{project_id}/contributions", tags=["catalog"], route_class=CatalogRoute)


class InitiativeData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    status: Literal["backlog", "planned", "in_progress", "done", "canceled", "unknown"]
    objective: str | None = Field(default=None, min_length=1, max_length=10000)
    owner_ref: Principal | None = None


class NativeWorkRef(RecordRef):
    record_revision: str = Field(min_length=1, max_length=512, pattern=r"^\S(?:.*\S)?$")

    @field_validator("record_revision")
    @classmethod
    def exact_revision(cls, value: str) -> str:
        if value in {"latest", "null"}:
            raise ValueError("native work revision must be pinned")
        return value


class WorkLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_ref: NativeWorkRef
    valid_from: datetime
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def validate_interval(self):
        if self.valid_from.tzinfo is None or (
            self.valid_to is not None and (self.valid_to.tzinfo is None or instant(self.valid_to) <= instant(self.valid_from))
        ):
            raise ValueError("invalid work interval")
        return self


class ContributionData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    initiative_ref: RecordRef
    product_ref: RecordRef
    objective: str = Field(min_length=1, max_length=10000)
    owner_ref: Principal
    valid_from: datetime
    valid_to: datetime | None = None
    planned_start_date: str | None = None
    target_date: str | None = None
    work_links: list[WorkLink] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.valid_from.tzinfo is None or (
            self.valid_to is not None and (self.valid_to.tzinfo is None or instant(self.valid_to) <= instant(self.valid_from))
        ):
            raise ValueError("invalid contribution interval")
        seen = {}
        for link in self.work_links:
            if instant(link.valid_from) < instant(self.valid_from) or (
                self.valid_to is not None and (link.valid_to is None or instant(link.valid_to) > instant(self.valid_to))
            ):
                raise ValueError("work link must fall within the contribution interval")
            reference = link.work_ref
            identity = (
                reference.tenant_id,
                reference.project_id,
                reference.source_connection_id,
                reference.record_type,
                reference.record_id,
            )
            for earlier in seen.get(identity, []):
                if (link.valid_to is None or instant(earlier.valid_from) < instant(link.valid_to)) and (
                    earlier.valid_to is None or instant(link.valid_from) < instant(earlier.valid_to)
                ):
                    raise ValueError("overlapping work links for the same native record")
            seen.setdefault(identity, []).append(link)
        return self


def initiative_view(row: InitiativeVersion):
    return {
        "tenant_id": row.tenant_id,
        "source_connection_id": row.source_connection_id,
        "id": row.id,
        "revision": str(row.revision),
        "recorded_at": wire_time(row.recorded_at),
        "data": row.data,
    }


def allowed_work_sources(project: Project, tenant_id: str) -> set[str]:
    basic = (project.configuration or {}).get("basic") or {}
    return {
        record["record_id"]
        for record in basic.get("trackers") or []
        if record.get("tenant_id") == tenant_id
        and record.get("project_id") == project.id
        and record.get("source_connection_id") == PRODUCT_SOURCE
        and record.get("record_type") == "tracker"
    }


async def scoped_project(db: AsyncSession, actor: TokenContext, member, project_id: str):
    project = await db.scalar(
        select(Project)
        .join(
            ProductProjectBinding,
            (ProductProjectBinding.tenant_id == Project.tenant_id)
            & (ProductProjectBinding.project_id == Project.id)
            & ProductProjectBinding.valid_to.is_(None),
        )
        .join(Product, (Product.tenant_id == ProductProjectBinding.tenant_id) & (Product.id == ProductProjectBinding.product_id))
        .where(
            Project.tenant_id == actor.org_id,
            Project.id == project_id,
            Project.archived_at.is_(None),
            Product.archived_at.is_(None),
            product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin),
        )
    )
    if project is None:
        fail(404, "not_found")
    return project


async def native_work_verified(actor: TokenContext, reference: NativeWorkRef) -> bool | None:
    return None


async def contribution_view(row: ContributionVersion, actor: TokenContext, effective_at: datetime | None = None, sources: set[str] | None = None):
    effective_time = instant(effective_at or utcnow())
    links = []
    for link in row.work_links:
        reference = NativeWorkRef.model_validate(link["work_ref"])
        if (
            (sources is None or reference.source_connection_id in sources)
            and instant(datetime.fromisoformat(link["valid_from"].replace("Z", "+00:00"))) <= effective_time
            and ("valid_to" not in link or effective_time < instant(datetime.fromisoformat(link["valid_to"].replace("Z", "+00:00"))))
        ):
            if await native_work_verified(actor, reference) is True:
                links.append(link)
    return {
        "tenant_id": row.tenant_id,
        "source_connection_id": row.source_connection_id,
        "id": row.id,
        "revision": str(row.revision),
        "project_id": row.project_id,
        "recorded_at": wire_time(row.recorded_at),
        **({"supersedes_revision": str(row.supersedes_revision)} if row.supersedes_revision else {}),
        "data": {**row.data, "work_links": links},
    }


def record_change(db: AsyncSession, actor: TokenContext, operation: str, key: str, digest: str, row: InitiativeVersion | ContributionVersion):
    contribution = isinstance(row, ContributionVersion)
    change_id = new_uuid()
    db.add(
        CatalogChange(
            tenant_id=actor.org_id,
            id=change_id,
            actor_id=actor.user_id,
            source_connection_id=CATALOG_SOURCE,
            record_type="contribution" if contribution else "initiative",
            record_id=row.id,
            native_record_revision=str(row.revision),
            relationship_revision=None,
            mapping_version="catalog-1",
            valid_from=row.valid_from if contribution else None,
            valid_to=row.valid_to if contribution else None,
            source_refs=[row.data["initiative_ref"], row.data["product_ref"], *(link["work_ref"] for link in row.work_links)] if contribution else [],
            recorded_at=row.recorded_at,
        )
    )
    outcome = {"id": row.id, "revision": str(row.revision), "change_id": change_id}
    db.add(
        CatalogReceipt(
            tenant_id=actor.org_id, actor_id=actor.user_id, operation=operation, idempotency_key=key, payload_digest=digest, outcome=outcome
        )
    )
    return outcome


async def commit(db: AsyncSession):
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        fail(409, "conflict")


async def initiative(db: AsyncSession, actor: TokenContext, initiative_id: str, member=None, lock=False):
    member = member or await member_for(db, actor)
    query = (
        select(InitiativeVersion)
        .join(
            InitiativeHead,
            (InitiativeHead.tenant_id == InitiativeVersion.tenant_id)
            & (InitiativeHead.source_connection_id == InitiativeVersion.source_connection_id)
            & (InitiativeHead.id == InitiativeVersion.id)
            & (InitiativeHead.revision == InitiativeVersion.revision),
        )
        .where(
            InitiativeVersion.tenant_id == actor.org_id,
            InitiativeVersion.source_connection_id == CATALOG_SOURCE,
            InitiativeVersion.id == initiative_id,
        )
    )
    row = await db.scalar(query.with_for_update() if lock else query)
    if row is None:
        fail(404, "not_found")
    if not await can_edit(db, actor):
        heads = (
            await db.scalars(
                select(ContributionHead).where(ContributionHead.tenant_id == actor.org_id, ContributionHead.initiative_id == initiative_id)
            )
        ).all()
        if not any(
            [await visible_product(db, member, actor.org_id, head.product_id, head.project_id, is_platform_admin=actor.is_admin) for head in heads]
        ):
            fail(404, "not_found")
    return row


async def contribution(db: AsyncSession, actor: TokenContext, project_id: str, contribution_id: str, member=None, lock=False):
    member = member or await member_for(db, actor)
    head = await db.scalar(
        select(ContributionHead)
        .where(
            ContributionHead.tenant_id == actor.org_id,
            ContributionHead.source_connection_id == CATALOG_SOURCE,
            ContributionHead.id == contribution_id,
            ContributionHead.project_id == project_id,
        )
        .with_for_update()
        if lock
        else select(ContributionHead).where(
            ContributionHead.tenant_id == actor.org_id,
            ContributionHead.source_connection_id == CATALOG_SOURCE,
            ContributionHead.id == contribution_id,
            ContributionHead.project_id == project_id,
        )
    )
    if head is None or not await visible_product(db, member, actor.org_id, head.product_id, project_id, lock=lock, is_platform_admin=actor.is_admin):
        fail(404, "not_found")
    return head, await db.get(ContributionVersion, (actor.org_id, CATALOG_SOURCE, contribution_id, head.revision))


async def validate_endpoints(db: AsyncSession, actor: TokenContext, member, project_id: str, data: ContributionData, *, lock=False):
    initiative_ref, product_ref = data.initiative_ref, data.product_ref
    if any(ref.tenant_id != actor.org_id or ref.project_id != project_id for ref in (initiative_ref, product_ref)):
        fail(404, "not_found")
    if (
        initiative_ref.source_connection_id != CATALOG_SOURCE
        or initiative_ref.record_type != "initiative"
        or product_ref.source_connection_id != PRODUCT_SOURCE
        or product_ref.record_type != "product"
    ):
        fail(422, "invalid_request")
    product = await visible_product(db, member, actor.org_id, product_ref.record_id, project_id, lock=lock, is_platform_admin=actor.is_admin)
    if product is None:
        fail(404, "not_found")
    initiative_row = await initiative(db, actor, initiative_ref.record_id, member, lock=lock)
    if product.revision != int(product_ref.record_revision) or initiative_row.revision != int(initiative_ref.record_revision):
        fail(412, "stale_revision")
    project = await scoped_project(db, actor, member, project_id)
    sources = allowed_work_sources(project, actor.org_id)
    for link in data.work_links:
        ref = link.work_ref
        if ref.tenant_id != actor.org_id or ref.project_id != project_id:
            fail(404, "not_found")
        if ref.record_type not in {"epic", "story", "release"} or ref.source_connection_id in {CATALOG_SOURCE, PRODUCT_SOURCE}:
            fail(422, "invalid_request")
        if ref.source_connection_id not in sources:
            fail(404, "not_found")
        verified = await native_work_verified(actor, ref)
        if verified is None:
            fail(503, "dependency_unavailable")
        if not verified:
            fail(404, "not_found")
    return initiative_row, product


@initiative_router.post("", status_code=201)
async def create_initiative(
    data: InitiativeData,
    key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    admitted()
    await editor(db, actor)
    preconditions(key)
    payload = data.model_dump(exclude_none=True)
    previous, digest = await replay(db, actor, "create_initiative", key, payload)
    if previous:
        return previous
    row = InitiativeVersion(
        tenant_id=actor.org_id, source_connection_id=CATALOG_SOURCE, id=new_uuid(), revision=1, data=payload, recorded_at=utcnow()
    )
    db.add(row)
    await db.flush()
    db.add(InitiativeHead(tenant_id=actor.org_id, source_connection_id=CATALOG_SOURCE, id=row.id, revision=1))
    result = record_change(db, actor, "create_initiative", key, digest, row)
    await commit(db)
    return result


@initiative_router.patch("/{initiative_id}")
async def revise_initiative(
    initiative_id: str,
    data: InitiativeData,
    key: str | None = Header(default=None, alias="Idempotency-Key"),
    revision: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    admitted()
    await editor(db, actor)
    row = await initiative(db, actor, initiative_id, lock=True)
    preconditions(key, revision, patch=True)
    payload = {"data": data.model_dump(exclude_none=True), "revision": revision}
    operation = f"revise_initiative:{initiative_id}"
    previous, digest = await replay(db, actor, operation, key, payload)
    if previous:
        return previous
    if row.revision != int(revision):
        fail(412, "stale_revision")
    next_row = InitiativeVersion(
        tenant_id=actor.org_id,
        source_connection_id=CATALOG_SOURCE,
        id=initiative_id,
        revision=row.revision + 1,
        data=payload["data"],
        recorded_at=utcnow(),
    )
    db.add(next_row)
    await db.flush()
    head = await db.get(InitiativeHead, (actor.org_id, CATALOG_SOURCE, initiative_id))
    head.revision = next_row.revision
    result = record_change(db, actor, operation, key, digest, next_row)
    await commit(db)
    return result


@initiative_router.get("")
async def list_initiatives(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member = await member_for(db, actor)
    editable = await can_edit(db, actor)
    digest = await scope_digest(db, actor, member, editable)
    watermark, last = read_cursor(cursor, actor, "initiatives", limit, digest) if cursor else (utcnow(), "")
    rows = (
        await db.scalars(
            select(InitiativeVersion)
            .where(InitiativeVersion.tenant_id == actor.org_id, InitiativeVersion.source_connection_id == CATALOG_SOURCE, InitiativeVersion.id > last)
            .order_by(InitiativeVersion.id, InitiativeVersion.revision)
        )
    ).all()
    snapshot = {row.id: row for row in rows if instant(row.recorded_at) <= watermark}
    visible = []
    for row in snapshot.values():
        if editable:
            visible.append(row)
        else:
            heads = (
                await db.scalars(select(ContributionHead).where(ContributionHead.tenant_id == actor.org_id, ContributionHead.initiative_id == row.id))
            ).all()
            if any(
                [
                    await visible_product(db, member, actor.org_id, head.product_id, head.project_id, is_platform_admin=actor.is_admin)
                    for head in heads
                ]
            ):
                visible.append(row)
    page = visible[:limit]
    result = {"items": [initiative_view(row) for row in page]}
    if len(visible) > limit:
        result["next_cursor"] = page_cursor(actor, "initiatives", limit, digest, watermark, page[-1].id)
    return result


@initiative_router.get("/{initiative_id}")
async def get_initiative(initiative_id: str, db: AsyncSession = Depends(get_db), actor: TokenContext = Depends(get_current_user)):
    return initiative_view(await initiative(db, actor, initiative_id))


@contribution_router.post("", status_code=201)
async def create_contribution(
    project_id: str,
    data: ContributionData,
    key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    admitted()
    await editor(db, actor)
    member = await member_for(db, actor)
    initiative_row, product = await validate_endpoints(db, actor, member, project_id, data, lock=True)
    preconditions(key)
    payload = data.model_dump(mode="json", exclude_none=True)
    operation = f"create_contribution:{project_id}"
    previous, digest = await replay(db, actor, operation, key, payload)
    if previous:
        return previous
    existing = await db.scalar(
        select(ContributionHead).where(
            ContributionHead.tenant_id == actor.org_id,
            ContributionHead.initiative_source == CATALOG_SOURCE,
            ContributionHead.initiative_id == initiative_row.id,
            ContributionHead.product_source == PRODUCT_SOURCE,
            ContributionHead.product_id == product.id,
            ContributionHead.project_id == project_id,
        )
    )
    if existing:
        previous = await db.get(ContributionVersion, (actor.org_id, CATALOG_SOURCE, existing.id, existing.revision))
        if previous.valid_to is None or instant(data.valid_from) < instant(previous.valid_to):
            fail(409, "conflict")
        await db.delete(existing)
        await db.flush()
    row = ContributionVersion(
        tenant_id=actor.org_id,
        source_connection_id=CATALOG_SOURCE,
        id=new_uuid(),
        revision=1,
        project_id=project_id,
        initiative_source=CATALOG_SOURCE,
        initiative_id=initiative_row.id,
        initiative_revision=initiative_row.revision,
        product_source=PRODUCT_SOURCE,
        product_id=product.id,
        data={key: value for key, value in payload.items() if key != "work_links"},
        work_links=payload["work_links"],
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        recorded_at=utcnow(),
    )
    db.add(row)
    await db.flush()
    db.add(
        ContributionHead(
            tenant_id=actor.org_id,
            source_connection_id=CATALOG_SOURCE,
            id=row.id,
            revision=1,
            project_id=project_id,
            initiative_source=CATALOG_SOURCE,
            initiative_id=initiative_row.id,
            product_source=PRODUCT_SOURCE,
            product_id=product.id,
        )
    )
    result = record_change(db, actor, operation, key, digest, row)
    await commit(db)
    return result


@contribution_router.patch("/{contribution_id}")
async def revise_contribution(
    project_id: str,
    contribution_id: str,
    data: ContributionData,
    key: str | None = Header(default=None, alias="Idempotency-Key"),
    revision: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    admitted()
    await editor(db, actor)
    member = await member_for(db, actor)
    head, row = await contribution(db, actor, project_id, contribution_id, member, lock=True)
    initiative_row, product = await validate_endpoints(db, actor, member, project_id, data, lock=True)
    if initiative_row.id != head.initiative_id or product.id != head.product_id:
        fail(422, "invalid_request")
    preconditions(key, revision, patch=True)
    payload = {"data": data.model_dump(mode="json", exclude_none=True), "revision": revision}
    operation = f"revise_contribution:{contribution_id}"
    previous, digest = await replay(db, actor, operation, key, payload)
    if previous:
        return previous
    if head.revision != int(revision):
        fail(412, "stale_revision")
    next_row = ContributionVersion(
        tenant_id=actor.org_id,
        source_connection_id=CATALOG_SOURCE,
        id=row.id,
        revision=row.revision + 1,
        project_id=row.project_id,
        initiative_source=row.initiative_source,
        initiative_id=row.initiative_id,
        initiative_revision=initiative_row.revision,
        product_source=row.product_source,
        product_id=row.product_id,
        data={key: value for key, value in payload["data"].items() if key != "work_links"},
        work_links=payload["data"]["work_links"],
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        supersedes_revision=row.revision,
        recorded_at=utcnow(),
    )
    db.add(next_row)
    await db.flush()
    head.revision = next_row.revision
    result = record_change(db, actor, operation, key, digest, next_row)
    await commit(db)
    return result


@contribution_router.get("")
async def list_contributions(
    project_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    effective_at: datetime | None = None,
    decision_at: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member = await member_for(db, actor)
    project = await scoped_project(db, actor, member, project_id)
    sources = allowed_work_sources(project, actor.org_id)
    editable = await can_edit(db, actor)
    scope = await scope_digest(db, actor, member, editable)
    digest = hashlib.sha256(json.dumps([scope, sorted(sources)]).encode()).hexdigest()
    route = f"contributions:{project_id}:{effective_at}:{decision_at}"
    watermark, last = read_cursor(cursor, actor, route, limit, digest) if cursor else (utcnow(), "")
    rows = (
        await db.scalars(
            select(ContributionVersion)
            .where(
                ContributionVersion.tenant_id == actor.org_id,
                ContributionVersion.source_connection_id == CATALOG_SOURCE,
                ContributionVersion.project_id == project_id,
                ContributionVersion.id > last,
            )
            .order_by(ContributionVersion.id, ContributionVersion.revision)
        )
    ).all()
    snapshot = {}
    for row in rows:
        if instant(row.recorded_at) <= instant(decision_at or watermark):
            snapshot[row.id] = row
    visible = []
    effective_time = instant(effective_at or utcnow())
    for row in snapshot.values():
        if await visible_product(db, member, actor.org_id, row.product_id, project_id, is_platform_admin=actor.is_admin):
            if instant(row.valid_from) <= effective_time and (row.valid_to is None or effective_time < instant(row.valid_to)):
                visible.append(row)
    response.headers["X-Catalog-View"] = "historical" if effective_at or decision_at else "current"
    page = visible[:limit]
    result = {"items": [await contribution_view(row, actor, effective_time, sources) for row in page]}
    if len(visible) > limit:
        result["next_cursor"] = page_cursor(actor, route, limit, digest, watermark, page[-1].id)
    return result


@contribution_router.get("/{contribution_id}")
async def get_contribution(
    project_id: str, contribution_id: str, db: AsyncSession = Depends(get_db), actor: TokenContext = Depends(get_current_user)
):
    _, row = await contribution(db, actor, project_id, contribution_id)
    member = await member_for(db, actor)
    project = await scoped_project(db, actor, member, project_id)
    return await contribution_view(row, actor, sources=allowed_work_sources(project, actor.org_id))


router = APIRouter()
router.include_router(initiative_router)
router.include_router(contribution_router)
