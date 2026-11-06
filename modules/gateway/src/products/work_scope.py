"""One canonical Product/Project scope for delivery, agents and metered usage."""

from dataclasses import dataclass, replace

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.products.models import Product, ProductProjectBinding, Project, ProjectRun


class ProjectScopeError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectAttribution:
    tenant_id: str
    product_id: str
    project_id: str
    relationship_revision: int
    run_id: str | None = None
    flow_id: str | None = None


async def resolve_project(db, *, tenant_id, product_id=None, project_id=None):
    """Resolve an explicit project or the Product's base; never guess by name."""
    if not product_id and not project_id:
        return None
    query = (
        select(ProductProjectBinding)
        .join(Product, (Product.tenant_id == ProductProjectBinding.tenant_id) & (Product.id == ProductProjectBinding.product_id))
        .join(Project, (Project.tenant_id == ProductProjectBinding.tenant_id) & (Project.id == ProductProjectBinding.project_id))
        .where(
            ProductProjectBinding.tenant_id == tenant_id,
            ProductProjectBinding.valid_to.is_(None),
            Product.archived_at.is_(None),
            Project.archived_at.is_(None),
        )
    )
    if product_id:
        query = query.where(ProductProjectBinding.product_id == product_id)
    query = query.where(ProductProjectBinding.project_id == project_id) if project_id else query.where(ProductProjectBinding.is_base.is_(True))
    row = (await db.scalars(query)).one_or_none()
    if row is None and product_id and not project_id:
        row = await ensure_base_project(db, tenant_id=tenant_id, product_id=product_id)
    if row is None:
        raise ProjectScopeError("Select an active Project belonging to this Product.")
    return ProjectAttribution(tenant_id, row.product_id, row.project_id, row.relationship_revision)


async def ensure_base_project(db, *, tenant_id, product_id):
    """Repair imported/legacy Products under the same tenant-scoped row lock."""
    product = await db.scalar(
        select(Product).where(Product.tenant_id == tenant_id, Product.id == product_id, Product.archived_at.is_(None)).with_for_update()
    )
    if product is None:
        return None
    binding = await db.scalar(
        select(ProductProjectBinding)
        .join(Project, (Project.tenant_id == ProductProjectBinding.tenant_id) & (Project.id == ProductProjectBinding.project_id))
        .where(
            ProductProjectBinding.tenant_id == tenant_id,
            ProductProjectBinding.product_id == product_id,
            ProductProjectBinding.valid_to.is_(None),
            Project.archived_at.is_(None),
        )
        .order_by(ProductProjectBinding.is_base.desc(), ProductProjectBinding.valid_from, ProductProjectBinding.id)
        .limit(1)
    )
    if binding is None:
        from src.shared.models.base import new_uuid

        project = Project(id=new_uuid(), tenant_id=tenant_id, name="Base", owner_user_id=product.owner_user_id)
        db.add(project)
        await db.flush()
        binding = ProductProjectBinding(
            tenant_id=tenant_id, product_id=product_id, project_id=project.id, provenance={"source": "base-project-default"}
        )
        db.add(binding)
    binding.is_base = True
    await db.flush()
    return binding


def attribution(row):
    return ProjectAttribution(row.tenant_id, row.product_id, row.project_id, row.relationship_revision, row.run_id, row.flow_id)


async def authorize_project_selection(db, *, user_id, tenant_id, product_id=None, project_id=None):
    from src.shared.identity.product_authority import stored_product_actor
    from src.shared.identity.product_scope import product_member, product_visibility_scope

    scope = await resolve_project(db, tenant_id=tenant_id, product_id=product_id, project_id=project_id)
    if scope is None:
        return None
    member = await product_member(db, user_id=user_id, org_id=tenant_id)
    if await db.scalar(select(Product.id).where(Product.id == scope.product_id, product_visibility_scope(member, tenant_id))):
        return scope
    from fastapi import HTTPException

    try:
        actor = await stored_product_actor(db, user_id=user_id, tenant_id=tenant_id)
    except HTTPException:
        raise ProjectScopeError("Project access is unavailable.") from None
    if not actor.is_admin:
        raise ProjectScopeError("Project access is unavailable.")
    return scope


async def bind_run(db, *, scope, run_id, owner_user_id, flow_id=None, parent_run_id=None):
    """Capture once, atomically. Replays cannot move an execution's charges."""
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    await db.execute(
        insert(ProjectRun)
        .values(
            tenant_id=scope.tenant_id,
            run_id=run_id,
            product_id=scope.product_id,
            project_id=scope.project_id,
            relationship_revision=scope.relationship_revision,
            flow_id=flow_id,
            parent_run_id=parent_run_id,
            owner_user_id=owner_user_id,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "run_id"])
    )
    row = await db.get(ProjectRun, (scope.tenant_id, run_id), populate_existing=True)
    if (row.product_id, row.project_id, row.flow_id, row.owner_user_id) != (scope.product_id, scope.project_id, flow_id, owner_user_id):
        raise ProjectScopeError("This run already belongs to another Project or owner.")
    return attribution(row)


async def resolve_run_project(db, *, binding):
    """Only accepts a server-verified run binding; never a raw client run ID."""
    row = await db.get(ProjectRun, (binding.tenant_id, binding.run_id))
    if row is not None:
        if row.flow_id != binding.flow_id or row.owner_user_id != binding.root_human_id:
            raise ProjectScopeError("Run Project attribution does not match its authority.")
        if binding.is_human_rooted:
            await authorize_project_selection(
                db,
                user_id=binding.root_human_id,
                tenant_id=binding.tenant_id,
                product_id=row.product_id,
                project_id=row.project_id,
            )
        return attribution(row)
    scope = None
    if binding.flow_id:
        from src.orchestration.models import OrchestrationFlow

        flow = await db.scalar(
            select(OrchestrationFlow).where(
                OrchestrationFlow.org_id == binding.tenant_id,
                OrchestrationFlow.id == binding.flow_id,
            )
        )
        if flow is None:
            raise ProjectScopeError("The run's delivery flow is unavailable.")
        if flow.product_id:
            scope = await resolve_project(db, tenant_id=binding.tenant_id, product_id=flow.product_id, project_id=flow.project_id)
            if flow.project_id is None:
                flow.project_id = scope.project_id
    else:
        parent_id = getattr(binding, "session_id", None) or binding.correlation_id
        parent = await db.get(ProjectRun, (binding.tenant_id, parent_id)) if parent_id and parent_id != binding.run_id else None
        if parent is not None:
            if parent.owner_user_id != binding.root_human_id:
                raise ProjectScopeError("Parent Project attribution does not match the run owner.")
            scope = replace(attribution(parent), run_id=None)
    if scope is None:
        return None
    if binding.is_human_rooted:
        await authorize_project_selection(
            db,
            user_id=binding.root_human_id,
            tenant_id=binding.tenant_id,
            product_id=scope.product_id,
            project_id=scope.project_id,
        )
    return await bind_run(
        db,
        scope=scope,
        run_id=binding.run_id,
        owner_user_id=binding.root_human_id,
        flow_id=binding.flow_id,
        parent_run_id=(getattr(binding, "session_id", None) or binding.correlation_id) if not binding.flow_id else None,
    )


async def assign_flow_project(db, *, flow, product_id, project_id=None):
    """Default once; a started flow cannot move to a fresh budget."""
    from src.orchestration.compile import ProposalRejectedError
    from src.orchestration.models import OrchestrationExecution, OrchestrationNode

    selected = project_id or (flow.project_id if flow.product_id == product_id else None)
    try:
        scope = await resolve_project(db, tenant_id=flow.org_id, product_id=product_id, project_id=selected)
    except ProjectScopeError as exc:
        raise ProposalRejectedError(str(exc)) from exc
    new_id = scope.project_id if scope else None
    if flow.project_id is not None and flow.project_id != new_id:
        started = await db.scalar(
            select(
                exists().where(
                    OrchestrationNode.org_id == flow.org_id,
                    OrchestrationNode.flow_id == flow.id,
                    OrchestrationNode.attempts > 0,
                )
            )
        ) or await db.scalar(
            select(
                exists().where(
                    OrchestrationExecution.org_id == flow.org_id,
                    OrchestrationExecution.flow_id == flow.id,
                )
            )
        )
        if started:
            raise ProposalRejectedError("Project cannot change after execution starts; create a new flow.")
    flow.project_id = new_id
    return scope


async def attach_session_run(db, *, tenant_id, user_id, session_id, run_id):
    """Resolve only a session and run already authenticated by the chat host."""
    parent = await db.get(ProjectRun, (tenant_id, session_id))
    if parent is None:
        return None
    if parent.owner_user_id != user_id:
        raise ProjectScopeError("Project session owner changed.")
    await authorize_project_selection(db, user_id=user_id, tenant_id=tenant_id, product_id=parent.product_id, project_id=parent.project_id)
    scope = await bind_run(db, scope=attribution(parent), run_id=run_id, owner_user_id=user_id, parent_run_id=session_id)
    await db.commit()
    return scope
