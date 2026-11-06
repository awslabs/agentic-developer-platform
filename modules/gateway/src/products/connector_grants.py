"""Issue explicit collection grants under current Product and source authority."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.product_onboarding.routes import fingerprint, preconditions
from src.products.models import ConnectorCell, ConnectorCollectionGrant, ConnectorPolicyRealm, ProductOperation
from src.products.source_routes import binding_view, connection_access, scoped_binding
from src.shared.database import get_db
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/collection-grants", tags=["products"])


class CollectionGrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1, max_length=36)
    binding_generation: int = Field(ge=1)
    binding_revision: int = Field(ge=1)
    cell_id: str = Field(min_length=1, max_length=36)
    consumer: str = Field(min_length=1, max_length=255)
    purpose: Literal["collection"]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@router.post("", operation_id="createCollectionGrant", status_code=201)
async def create_collection_grant(
    product_id: str,
    project_id: str,
    selection: CollectionGrantInput,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    if actor.account_type != "human":
        raise HTTPException(404, detail={"code": "not_found"})
    preconditions(idempotency_key, expected_revision)
    member, product, _, binding = await scoped_binding(db, actor, product_id, project_id, selection.binding_id)
    cell = await db.scalar(
        select(ConnectorCell)
        .where(ConnectorCell.tenant_id == actor.org_id, ConnectorCell.project_id == project_id, ConnectorCell.id == selection.cell_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if cell is None or cell.state != "active" or cell.workload_id != selection.consumer:
        raise HTTPException(404, detail={"code": "not_found"})
    realm = await db.get(ConnectorPolicyRealm, (actor.org_id, project_id, cell.realm_id, cell.realm_revision))
    if realm is None or realm.state != "active":
        raise HTTPException(409, detail={"code": "cell_retired"})
    if await connection_access(db, actor, binding, lock=True) is None:
        raise HTTPException(404, detail={"code": "not_found"})
    now = utcnow()
    if (
        binding.generation != selection.binding_generation
        or binding.revision != selection.binding_revision
        or binding_view(binding, True, now)["status"] != "healthy"
        or product.archived_at is not None
        or product.revision != expected_revision
    ):
        raise HTTPException(409, detail={"code": "stale_source_or_profile"})
    source_revision = (binding.observed or {}).get("source_revision")
    if not isinstance(source_revision, str) or not source_revision:
        raise HTTPException(409, detail={"code": "stale_source_or_profile"})
    source_ref = {
        "tenant_id": actor.org_id,
        "project_id": project_id,
        "source_connection_id": binding.installation_id or binding.credential_id,
        "record_type": binding.kind,
        "record_id": binding.native_id,
        "record_revision": source_revision,
    }
    key_hash = fingerprint({"key": idempotency_key})
    request_hash = fingerprint(
        {
            "product_id": product_id,
            "project_id": project_id,
            "selection": selection.model_dump(),
            "expected_revision": expected_revision,
            "source_ref": source_ref,
            "realm_revision": realm.revision,
        }
    )
    prior = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "createCollectionGrant",
            ProductOperation.key_hash == key_hash,
        )
    )
    if prior is not None:
        if prior.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        grant = await db.get(ConnectorCollectionGrant, (actor.org_id, prior.receipt["grant_id"]))
        if grant is None or grant.state != "active" or _aware(grant.expires_at) <= now:
            raise HTTPException(409, detail={"code": "grant_expired_or_revoked"})
        response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return {"id": grant.id, "expires_at": _aware(grant.expires_at).isoformat()}

    grant = ConnectorCollectionGrant(
        tenant_id=actor.org_id,
        project_id=project_id,
        binding_id=binding.id,
        binding_generation=binding.generation,
        cell_id=cell.id,
        consumer=selection.consumer,
        purpose=selection.purpose,
        source_selections=[source_ref],
        granting_actor_id=member.id,
        policy_revision=realm.revision,
        expires_at=now + timedelta(hours=1),
    )
    db.add(grant)
    await db.flush()
    db.add(
        ProductOperation(
            tenant_id=actor.org_id,
            actor_id=member.id,
            operation="createCollectionGrant",
            key_hash=key_hash,
            request_hash=request_hash,
            request={"selection": selection.model_dump(), "expected_revision": expected_revision},
            receipt={"grant_id": grant.id},
            product_id=product_id,
            project_id=project_id,
            binding_id=binding.id,
            binding_generation=binding.generation,
            expires_at=now + timedelta(days=14),
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "grant_conflict"}) from None
    response.headers["Cache-Control"] = "no-store"
    return {"id": grant.id, "expires_at": _aware(grant.expires_at).isoformat()}
