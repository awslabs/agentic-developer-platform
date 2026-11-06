"""IAM-only service entry points reusing Product's current scoped read handlers."""

from copy import deepcopy

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.products import routes as product_routes
from src.products import source_routes
from src.shared.database import get_db
from src.shared.identity.product_scope import service_product_scope
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/internal/v1/products", tags=["internal-products"], dependencies=[Depends(verify_internal_or_irsa)])


def service_actor(request: Request) -> TokenContext:
    actor = getattr(request.state, "token_context", None)
    if (
        actor is None
        or actor.account_type != "service"
        or actor.auth_source != "iam"
        or actor.scope not in {"internal", "platform"}
        or not actor.agent_registry_id
    ):
        raise HTTPException(403, detail={"code": "access_denied"})
    return actor


async def checked_read(db, actor, delegation_id, product_id, project_id, read, *, source_items=False):
    grant = await service_product_scope(db, actor, delegation_id, product_id, project_id, "product:read")
    if grant is None:
        raise HTTPException(404, detail={"code": "not_found"})
    revision, operations, sources = grant.revision, deepcopy(grant.operations), deepcopy(grant.sources)

    async def source_state(current_grant):
        bindings = await source_routes.service_visible_bindings(db, current_grant)
        return [
            (binding.id, binding.revision, binding.generation, binding.status, deepcopy(binding.desired), deepcopy(binding.observed))
            for binding in bindings
        ]

    before = await source_state(grant) if source_items else None
    result = await read()
    grant = await service_product_scope(db, actor, delegation_id, product_id, project_id, "product:read")
    if grant is None or (grant.revision, grant.operations, grant.sources) != (revision, operations, sources):
        raise HTTPException(404, detail={"code": "not_found"})
    if source_items and await source_state(grant) != before:
        raise HTTPException(404, detail={"code": "not_found"})
    return result


@router.get("/{product_id}/projects/{project_id}", operation_id="getServiceProfile")
async def get_profile(
    product_id: str,
    project_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    delegation_id: str | None = Header(default=None, alias="X-L5-Delegation-ID"),
    actor: TokenContext = Depends(service_actor),
):
    return await checked_read(
        db,
        actor,
        delegation_id,
        product_id,
        project_id,
        lambda: product_routes.get_profile(product_id, project_id, response, db, actor, delegation_id),
    )


@router.get("/{product_id}/projects/{project_id}/revisions/{revision}", operation_id="getServiceRevision")
async def get_revision(
    product_id: str,
    project_id: str,
    revision: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    delegation_id: str | None = Header(default=None, alias="X-L5-Delegation-ID"),
    actor: TokenContext = Depends(service_actor),
):
    return await checked_read(
        db,
        actor,
        delegation_id,
        product_id,
        project_id,
        lambda: product_routes.get_revision(product_id, project_id, revision, response, db, actor, delegation_id),
    )


@router.get("/{product_id}/projects/{project_id}/changes", operation_id="listServiceChanges")
async def list_changes(
    product_id: str,
    project_id: str,
    response: Response,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    delegation_id: str | None = Header(default=None, alias="X-L5-Delegation-ID"),
    actor: TokenContext = Depends(service_actor),
):
    return await checked_read(
        db,
        actor,
        delegation_id,
        product_id,
        project_id,
        lambda: product_routes.list_changes(product_id, project_id, response, cursor, limit, db, actor, delegation_id),
    )


@router.get("/{product_id}/projects/{project_id}/project-bindings", operation_id="listServiceProjectBindings")
async def list_project_bindings(
    product_id: str,
    project_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    delegation_id: str | None = Header(default=None, alias="X-L5-Delegation-ID"),
    actor: TokenContext = Depends(service_actor),
):
    return await checked_read(
        db,
        actor,
        delegation_id,
        product_id,
        project_id,
        lambda: product_routes.list_project_bindings(product_id, project_id, response, db, actor, delegation_id),
    )


@router.get("/{product_id}/projects/{project_id}/bindings", operation_id="listServiceBindings")
async def list_bindings(
    product_id: str,
    project_id: str,
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    delegation_id: str | None = Header(default=None, alias="X-L5-Delegation-ID"),
    actor: TokenContext = Depends(service_actor),
):
    return await checked_read(
        db,
        actor,
        delegation_id,
        product_id,
        project_id,
        lambda: source_routes.list_bindings(product_id, project_id, response, limit, cursor, db, actor, delegation_id),
        source_items=True,
    )


@router.get("/{product_id}/projects/{project_id}/sources", operation_id="listServiceSources")
async def list_sources(
    product_id: str,
    project_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    delegation_id: str | None = Header(default=None, alias="X-L5-Delegation-ID"),
    actor: TokenContext = Depends(service_actor),
):
    return await checked_read(
        db,
        actor,
        delegation_id,
        product_id,
        project_id,
        lambda: source_routes.service_sources(db, actor, product_id, project_id, delegation_id, response),
        source_items=True,
    )
