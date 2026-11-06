"""Product Jira source mappings and on-demand MCP reads; no background importer."""

import hashlib
import time
from datetime import UTC, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.atlassian_system_routes import adapter_read, audit, current_system, enabled
from src.auth.dependencies import get_current_user
from src.internal.tool_adapter_observation import canonical
from src.knowledge.product_access import require_product_access
from src.products.jira_context_contract import JiraContextQuery, jira_board_in_project, jira_jql
from src.products.models import ProductProjectBinding, ProductSourceBinding
from src.products.routes import scoped_product
from src.shared.database import get_db
from src.shared.models.atlassian_system import AtlassianSystemConnection
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.vault import OAuthToolReceipt
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/jira-context", tags=["products"])


class JiraSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connection_id: str = Field(min_length=1, max_length=36)
    jira_project_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=32)
    jira_project_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)
    board_id: int | None = Field(default=None, ge=1)
    share_with_product: bool


def human_actor(user):
    return TokenContext(
        user_id=user.id,
        org_id=user.org_id,
        team_id=user.team_id,
        department_id="",
        account_type="human",
        expires_at=utcnow() + timedelta(seconds=30),
    )


async def scope(db, actor, product_id, project_id, *, write=False):
    enabled()
    if actor.account_type != "human":
        raise HTTPException(403, detail={"error": "jira_context_authority_required"})
    await require_product_access(db, actor, product_id, lock=True, write=write)
    await db.execute(
        select(ProductProjectBinding.id)
        .where(
            ProductProjectBinding.tenant_id == actor.org_id,
            ProductProjectBinding.product_id == product_id,
            ProductProjectBinding.project_id == project_id,
            ProductProjectBinding.valid_to.is_(None),
        )
        .with_for_update(read=not write)
    )
    return await scoped_product(db, actor, product_id, project_id)


def source_view(binding, connection):
    return {
        "id": binding.id,
        "connection_id": connection.credential_id,
        "site_id": connection.site_id,
        "jira_project_id": binding.desired["jira_project_id"],
        "jira_project_key": binding.desired["jira_project_key"],
        "board_id": binding.desired.get("board_id"),
        "board_type": binding.desired.get("board_type"),
        "label": binding.desired.get("label", binding.native_id),
        "revision": binding.revision,
    }


async def sources(db, actor, product_id, project_id):
    await scope(db, actor, product_id, project_id)
    bindings = (
        await db.scalars(
            select(ProductSourceBinding)
            .where(
                ProductSourceBinding.tenant_id == actor.org_id,
                ProductSourceBinding.project_id == project_id,
                ProductSourceBinding.provider == "atlassian-mcp",
                ProductSourceBinding.kind == "tracker",
                ProductSourceBinding.unlinked_at.is_(None),
                ProductSourceBinding.status == "healthy",
            )
            .order_by(ProductSourceBinding.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).all()
    result = []
    for binding in bindings:
        try:
            _, connection = await current_system(db, binding.credential_id, actor.org_id, product_id, lock=True)
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            raise
        if (
            binding.namespace != connection.site_id
            or binding.desired.get("share_with_product") is not True
            or binding.desired.get("product_id") != product_id
        ):
            continue
        result.append((binding, connection))
    return result


async def context_connection(db, actor, query):
    available = await sources(db, actor, query.product_id, query.project_id)
    selected = next(((binding, connection) for binding, connection in available if binding.id == query.source_id), None)
    if query.view == "sources":
        selected = available[0] if available else None
    if selected is None:
        raise HTTPException(404, detail={"error": "jira_context_source_unavailable"})
    binding, connection = selected
    return binding, connection, available


async def first_context_connection(db, actor):
    enabled()
    bindings = (
        await db.execute(
            select(ProductProjectBinding.product_id, ProductSourceBinding.project_id)
            .join(
                ProductSourceBinding,
                (ProductSourceBinding.tenant_id == ProductProjectBinding.tenant_id)
                & (ProductSourceBinding.project_id == ProductProjectBinding.project_id),
            )
            .where(
                ProductSourceBinding.tenant_id == actor.org_id,
                ProductSourceBinding.provider == "atlassian-mcp",
                ProductSourceBinding.unlinked_at.is_(None),
                ProductProjectBinding.valid_to.is_(None),
            )
        )
    ).all()
    for product_id, project_id in bindings:
        try:
            available = await sources(db, actor, product_id, project_id)
            if available:
                return available[0][1]
        except HTTPException as exc:
            if exc.status_code not in {403, 404}:
                raise
    raise HTTPException(403, detail={"error": "jira_context_source_unavailable"})


async def tool_context_connection(db, user, query=None):
    """Rebuild current authority only after the tool plane authenticates its root."""
    from src.shared.identity.product_authority import stored_product_actor

    actor = human_actor(user)

    async def selected(current_actor):
        if query is None:
            return await first_context_connection(db, current_actor)
        return (await context_connection(db, current_actor, query))[1]

    try:
        return actor, await selected(actor)
    except HTTPException as error:
        if error.status_code not in {403, 404}:
            raise
    actor = await stored_product_actor(db, user_id=user.id, tenant_id=user.org_id)
    return actor, await selected(actor)


def query_scope(query, binding, connection):
    return hashlib.sha256(
        canonical(
            {
                "query": query.model_dump(exclude={"cursor"}),
                "binding_revision": binding.revision,
                "connection_revision": connection.revision,
                "tenant_id": binding.tenant_id,
            }
        )
    ).hexdigest()


async def read_context(db, actor, query: JiraContextQuery, *, deadline_at_ms=None):
    binding, connection, available = await context_connection(db, actor, query)
    if query.view == "sources":
        return {"sources": [source_view(b, c) for b, c in available], "retrieved_at": utcnow().isoformat()}
    board = binding.desired.get("board_id")
    if query.view in {"backlog", "board", "sprints"} and board is None:
        raise HTTPException(422, detail={"error": "select_a_board_source"})
    if query.view == "sprints" and binding.desired.get("board_type") == "kanban":
        raise HTTPException(422, detail={"error": "scrum_board_required"})
    args = {}
    if query.view in {"issues", "backlog"}:
        args = {
            "jql": jira_jql([binding.desired["jira_project_id"]], query),
            "maxResults": query.limit,
            "searchResultMode": "issues",
            "view": "evidence",
            "fields": ["summary", "description", "status", "issuetype", "priority", "parent", "updated", "project"],
        }
        if board is not None:
            args.update(boardId=board, dataset="backlog" if query.view == "backlog" else "all")
    elif query.view == "board":
        args = {"boardId": board}
    elif query.view == "sprints":
        args = {"boardId": board, "maxResults": query.limit, "startAt": 0}
    digest = query_scope(query, binding, connection)
    if query.cursor:
        cursor = await db.get(OAuthToolReceipt, "jira-page:" + query.cursor)
        expiry = cursor.expires_at.replace(tzinfo=UTC) if cursor and cursor.expires_at.tzinfo is None else cursor.expires_at if cursor else None
        if cursor is None or expiry <= utcnow() or cursor.authorization.get("scope") != digest:
            raise HTTPException(409, detail={"error": "jira_cursor_expired_or_changed"})
        args.update(cursor.authorization["arguments"])
    result = await adapter_read(connection, query.view, args, deadline_at_ms=deadline_at_ms)
    data = result.get("data")
    cursor_args = None
    if isinstance(data, dict):
        next_token = data.get("nextPageToken")
        if isinstance(next_token, str) and next_token:
            if len(next_token) > 2048:
                raise HTTPException(503, detail={"error": "jira_pagination_unavailable"})
            cursor_args = {"nextPageToken": next_token}
        elif query.view == "sprints" and data.get("isLast") is False:
            values = data.get("values")
            if not isinstance(values, list) or not values:
                raise HTTPException(503, detail={"error": "jira_pagination_unavailable"})
            cursor_args = {"startAt": args["startAt"] + len(values)}
        elif query.view in {"issues", "backlog"} and data.get("isLast") is False:
            raise HTTPException(503, detail={"error": "jira_pagination_unavailable"})
    page_cursor = None
    if cursor_args:
        page_cursor = new_uuid()
        db.add(
            OAuthToolReceipt(
                id="jira-page:" + page_cursor, expires_at=utcnow() + timedelta(minutes=15), authorization={"scope": digest, "arguments": cursor_args}
            )
        )
        await db.flush()
    # The opaque cursor is the only pagination authority exposed to clients.
    if isinstance(data, dict):
        data = {name: value for name, value in data.items() if name != "nextPageToken"}
    return {
        "source": source_view(binding, connection),
        "view": query.view,
        "data": data,
        "upstream_tool": result.get("upstream_tool"),
        "retrieved_at_ms": result.get("retrieved_at_ms"),
        "next_cursor": page_cursor,
        "coverage": "page" if page_cursor else "provider_response",
    }


@router.get("/connections")
async def product_connections(product_id: str, project_id: str, actor=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await scope(db, actor, product_id, project_id)
    rows = (await db.scalars(select(AtlassianSystemConnection).where(AtlassianSystemConnection.org_id == actor.org_id))).all()
    result = []
    for row in rows:
        try:
            credential, connection = await current_system(db, row.credential_id, actor.org_id, product_id)
        except HTTPException:
            continue
        result.append({"id": credential.id, "label": credential.label, "site_id": connection.site_id})
    return {"connections": result}


@router.get("/connections/{connection_id}/resources")
async def discover_resources(
    product_id: str,
    project_id: str,
    connection_id: str,
    kind: str = "projects",
    query: str = "",
    start_at: int = 0,
    actor=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await scope(db, actor, product_id, project_id)
    _, connection = await current_system(db, connection_id, actor.org_id, product_id, lock=True)
    if kind not in {"projects", "boards"} or not 0 <= start_at <= 100000 or len(query) > 100:
        raise HTTPException(422, detail={"error": "invalid_discovery"})
    args = {"maxResults": 50, "startAt": start_at}
    if query:
        args["query" if kind == "projects" else "projectKeyOrId"] = query
    return await adapter_read(connection, kind, args)


@router.post("/sources")
async def select_source(product_id: str, project_id: str, body: JiraSourceInput, actor=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await scope(db, actor, product_id, project_id, write=True)
    if body.share_with_product is not True:
        raise HTTPException(422, detail={"error": "product_sharing_required"})
    _, connection = await current_system(db, body.connection_id, actor.org_id, product_id, lock=True)
    connection_revision, site_id = connection.revision, connection.site_id
    observed = await adapter_read(connection, "projects", {"query": body.jira_project_key, "maxResults": 100, "expand": "description"})
    data = observed.get("data")
    projects = data.get("values", data.get("projects", [])) if isinstance(data, dict) else data
    project = (
        next((p for p in projects if str(p.get("id")) == body.jira_project_id and p.get("key") == body.jira_project_key), None)
        if isinstance(projects, list)
        else None
    )
    if project is None:
        raise HTTPException(404, detail={"error": "jira_project_not_found"})
    board_type = None
    board_data = None
    if body.board_id is not None:
        board = await adapter_read(connection, "board", {"boardId": body.board_id})
        board_data = board.get("data")
        board_type = board_data.get("type") if isinstance(board_data, dict) else None
        if (
            not isinstance(board_type, str)
            or str(board_data.get("id")) != str(body.board_id)
            or not jira_board_in_project(board_data, body.jira_project_id, body.jira_project_key)
        ):
            raise HTTPException(503, detail={"error": "board_configuration_unavailable"})
    _, current = await current_system(db, body.connection_id, actor.org_id, product_id, lock=True)
    if current.revision != connection_revision or current.site_id != site_id:
        raise HTTPException(409, detail={"error": "jira_connection_changed"})
    observed_at = utcnow()
    observed = {
        "connection_revision": connection_revision,
        "source_revision": hashlib.sha256(canonical({"project": project, "board": board_data})).hexdigest(),
        "observed_at": observed_at.isoformat(),
        "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
    }
    native_id = body.jira_project_id + ":" + (str(body.board_id) if body.board_id is not None else "all")
    binding = await db.scalar(
        select(ProductSourceBinding)
        .where(
            ProductSourceBinding.tenant_id == actor.org_id,
            ProductSourceBinding.project_id == project_id,
            ProductSourceBinding.provider == "atlassian-mcp",
            ProductSourceBinding.namespace == connection.site_id,
            ProductSourceBinding.native_id == native_id,
            ProductSourceBinding.unlinked_at.is_(None),
        )
        .with_for_update()
    )
    if binding and binding.credential_id != connection.credential_id:
        raise HTTPException(409, detail={"error": "source_uses_another_connection"})
    if binding is None:
        binding = ProductSourceBinding(
            tenant_id=actor.org_id,
            id=new_uuid(),
            project_id=project_id,
            kind="tracker",
            provider="atlassian-mcp",
            credential_id=connection.credential_id,
            namespace=connection.site_id,
            native_id=native_id,
            desired={**body.model_dump(exclude={"connection_id"}), "label": project.get("name", body.jira_project_key), "product_id": product_id},
            observed=observed,
            status="healthy",
        )
        db.add(binding)
        await db.flush()
    else:
        if (
            binding.desired.get("product_id") != product_id
            or (binding.observed or {}).get("source_revision") != observed["source_revision"]
            or (binding.observed or {}).get("connection_revision") != connection.revision
        ):
            binding.revision += 1
            binding.generation += 1
        binding.observed = observed
    binding.desired = {**binding.desired, "product_id": product_id, "share_with_product": True, "board_type": board_type}
    audit(db, actor, "jira_source_selected", connection.credential_id, product_id=product_id, project_id=project_id, source_id=binding.id)
    await db.commit()
    return source_view(binding, connection)


@router.get("/sources")
async def list_sources(product_id: str, project_id: str, actor=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Keep unavailable mappings visible for removal. Tool authorization uses
    # sources(), which returns only currently usable connections.
    await scope(db, actor, product_id, project_id)
    rows = (
        await db.execute(
            select(ProductSourceBinding, AtlassianSystemConnection)
            .join(
                AtlassianSystemConnection,
                AtlassianSystemConnection.credential_id == ProductSourceBinding.credential_id,
            )
            .where(
                ProductSourceBinding.tenant_id == actor.org_id,
                ProductSourceBinding.project_id == project_id,
                ProductSourceBinding.provider == "atlassian-mcp",
                ProductSourceBinding.unlinked_at.is_(None),
                AtlassianSystemConnection.org_id == actor.org_id,
            )
        )
    ).all()
    result = []
    for binding, connection in rows:
        if binding.desired.get("product_id") != product_id:
            continue
        try:
            await current_system(db, connection.credential_id, actor.org_id, product_id)
            available = binding.status == "healthy" and binding.namespace == connection.site_id
        except HTTPException:
            available = False
        result.append({**source_view(binding, connection), "available": available})
    return {"sources": result}


@router.post("/read")
async def read_source(product_id: str, project_id: str, body: JiraContextQuery, actor=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if body.product_id != product_id or body.project_id != project_id:
        raise HTTPException(404, detail={"error": "source_not_found"})
    result = await read_context(db, actor, body, deadline_at_ms=int(time.time() * 1000) + 20000)
    await db.commit()
    return result


@router.delete("/sources/{source_id}")
async def unlink_source(
    product_id: str, project_id: str, source_id: str, revision: int, actor=Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await scope(db, actor, product_id, project_id, write=True)
    binding = await db.scalar(
        select(ProductSourceBinding)
        .where(
            ProductSourceBinding.tenant_id == actor.org_id,
            ProductSourceBinding.project_id == project_id,
            ProductSourceBinding.id == source_id,
            ProductSourceBinding.provider == "atlassian-mcp",
            ProductSourceBinding.unlinked_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if binding is None or binding.desired.get("product_id") != product_id:
        raise HTTPException(404, detail={"error": "jira_context_source_unavailable"})
    if binding.revision != revision:
        raise HTTPException(409, detail={"error": "source_changed"})
    binding.status, binding.unlinked_at = "unlinked", utcnow()
    binding.revision += 1
    audit(db, actor, "jira_source_unlinked", binding.credential_id, product_id=product_id, project_id=project_id, source_id=source_id)
    await db.commit()
    return {"status": "unlinked"}
