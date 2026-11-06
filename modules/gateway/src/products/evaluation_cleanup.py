"""Retire only a verified evaluation Product while retaining its immutable history."""

from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.products.models import Product, ProductChange, ProductOnboardingSession, ProductOperation, ProductProjectBinding, Project
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.organization import Organization, Team, User


class FixtureOwnershipError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise FixtureOwnershipError(reason)


def resume_configuration(configuration):
    if not isinstance(configuration, dict) or set(configuration) - {"financials", "applicability"}:
        return False
    choices = configuration.get("applicability", [])
    return (
        configuration.get("financials", {}) == {}
        and isinstance(choices, list)
        and len(choices) <= 1
        and all(
            isinstance(choice, dict)
            and set(choice) == {"check_id", "state", "reason"}
            and choice["check_id"] == "financials"
            and choice["state"] in ("later", "not_applicable")
            and isinstance(choice["reason"], str)
            and choice["reason"].strip()
            for choice in choices
        )
    )


async def retire_run_product(db: AsyncSession, manifest: dict) -> dict:
    """Atomically hide one run-owned default scope; never purge history or shared assets."""
    require(isinstance(manifest, dict), "Protected Product fixture manifest missing")
    tenant_id, actor_id, run_id = (manifest.get(key) for key in ("tenant_id", "actor_id", "run_id"))
    source_revision, key = manifest.get("source_revision"), manifest.get("idempotency_key")
    require(
        isinstance(run_id, str)
        and re.fullmatch(r"[a-z0-9-]{8,64}", run_id)
        and isinstance(source_revision, str)
        and re.fullmatch(r"[a-f0-9]{40}", source_revision)
        and isinstance(key, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key)
        and isinstance(tenant_id, str)
        and tenant_id
        and isinstance(actor_id, str)
        and actor_id,
        "Product fixture requires a pinned run, actor and original create key",
    )
    tenant = await db.scalar(select(Organization).where(Organization.id == tenant_id))
    connected_scope = manifest.get("scope_fixture_mode") == "connected_scope"
    require(manifest.get("scope_fixture_mode") in (None, "connected_scope"), "Unsupported Product fixture mode")
    require(tenant is not None and isinstance(tenant.name, str) and tenant.name, "Product fixture tenant is not an evaluation workspace")
    if connected_scope:
        scope = manifest.get("scope")
        # A connected workspace can host a single owned Product as well as
        # the two-Project scope scenario. Both use the same dedicated people;
        # only the latter supplies authority to retire a second Project.
        editor_id = scope.get("editor_id") if isinstance(scope, dict) else manifest.get("fixture_editor_id")
        require(
            re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id)
            and isinstance(editor_id, str)
            and editor_id != actor_id
            and (scope is None or manifest.get("fixture_editor_id") is None),
            "Connected scope requires the original B03 run and editor",
        )
        team_id = None
        for user_id in (actor_id, editor_id):
            user = await db.scalar(select(User).where(User.id == user_id, User.org_id == tenant_id))
            require(
                user is not None
                and user.role == "member"
                and user.user_kind == "human"
                and not user.is_shadow
                and isinstance(user.cognito_sub, str)
                and user.cognito_sub
                and isinstance(user.cognito_username, str)
                and user.cognito_username.startswith("eval-regression-"),
                "Connected scope requires dedicated ordinary tenant members",
            )
            if team_id is None:
                team_id = user.team_id
            require(user.team_id == team_id and team_id, "Connected scope members must share the dedicated team")
        team = await db.scalar(select(Team).where(Team.id == team_id, Team.org_id == tenant_id))
        require(team is not None, "Connected scope team must belong to its fixture tenant")
    else:
        require(manifest.get("fixture_editor_id") is None, "Connected editor requires the protected fixture mode")
        require(tenant.name.startswith("eval-regression-"), "Product fixture tenant is not an evaluation workspace")
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    create = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == tenant_id,
            ProductOperation.actor_id == actor_id,
            ProductOperation.operation == "createProduct",
            ProductOperation.key_hash == key_hash,
        )
    )
    require(create is not None and create.product_id and create.project_id, "Original scoped Product create receipt is missing")
    body = create.request
    resume_fixture = (
        re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id) is not None
        and isinstance(body, dict)
        and body.get("name") in tuple(f"eval-product-{run_id}-{case}" for case in ("resume_ui", "resume_cli", "progress", "recovery", "v11_resume"))
    )
    guidance_starting_point = next(
        (
            starting_point
            for starting_point in ("idea", "existing_assets", "running_product")
            if re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id)
            and isinstance(body, dict)
            and body.get("name") == f"eval-product-{run_id}-guidance_{starting_point}"
        ),
        None,
    )
    workspace_starting_point = next(
        (
            starting_point
            for starting_point in ("idea", "existing_assets", "running_product")
            if re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id)
            and isinstance(body, dict)
            and body.get("name") == f"eval-product-{run_id}-v11_{starting_point}"
        ),
        None,
    )
    workspace_fixture = workspace_starting_point or (
        re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id)
        and isinstance(body, dict)
        and body.get("name") == f"eval-product-{run_id}-v11_release"
    )
    starting_point = guidance_starting_point or workspace_starting_point
    require(
        isinstance(body, dict)
        and (
            body.get("name") in (f"eval-product-{run_id}", f"Owned Product {run_id}")
            or resume_fixture
            or guidance_starting_point
            or workspace_fixture
            or (manifest.get("scope") is not None and body.get("name") == f"eval-product-{run_id}-scope")
        )
        and (
            not connected_scope
            or manifest.get("scope") is None
            # Issue #8250: the B03 connected-scope browser case owns the plain
            # run-named Product because the reviewed V01 create guard accepts a
            # fixed set of case suffixes and "-scope" is not one of them. Accept
            # the plain name and the legacy "-scope" name, and nothing else, so
            # the exact-scope ownership check stays as narrow as before.
            or body.get("name") in (f"eval-product-{run_id}", f"eval-product-{run_id}-scope")
        )
        and body.get("starting_point") == "idea"
        and body.get("project") == {"mode": "create"}
        and create.request_hash == hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "Product fixture create request does not match the run",
    )
    require(
        manifest.get("product_id") in (None, create.product_id) and manifest.get("project_id") in (None, create.project_id),
        "Product fixture receipt differs from the original create",
    )
    product = await db.scalar(
        select(Product)
        .where(
            Product.tenant_id == tenant_id,
            Product.id == create.product_id,
        )
        .with_for_update()
    )
    project = await db.scalar(
        select(Project)
        .where(
            Project.tenant_id == tenant_id,
            Project.id == create.project_id,
        )
        .with_for_update()
    )
    require(
        product is not None
        and project is not None
        and product.owner_user_id == actor_id
        and product.created_by == actor_id
        and project.owner_user_id == actor_id
        and project.name == body["name"],
        "Product fixture owner or default Project differs",
    )
    if manifest.get("scope") is not None:
        from src.products.evaluation_scope_cleanup import retire_scope_product

        return await retire_scope_product(db, manifest, create, product, project, actor_id, source_revision, require)
    bindings = (
        await db.scalars(
            select(ProductProjectBinding)
            .where(
                ProductProjectBinding.tenant_id == tenant_id,
                or_(ProductProjectBinding.project_id == project.id, ProductProjectBinding.product_id == product.id),
            )
            .with_for_update()
        )
    ).all()
    require(len(bindings) == 1 and bindings[0].product_id == product.id, "Product fixture Project is shared or rebound")
    require(
        bindings[0].provenance == {"actor_id": actor_id, "source": "direct_input"},
        "Product fixture binding provenance differs from the original create",
    )
    operations = (
        await db.scalars(
            select(ProductOperation).where(
                ProductOperation.tenant_id == tenant_id,
                ProductOperation.product_id == product.id,
            )
        )
    ).all()
    onboarding_operations = {
        op.id: op
        for op in operations
        if (resume_fixture or guidance_starting_point or workspace_fixture) and op.operation in ("startOnboarding", "updateStep")
    }
    guidance_updates = {
        op.id: op
        for op in operations
        if starting_point
        and op.operation == "updateProduct"
        and (
            guidance_starting_point
            or (isinstance(op.request, dict) and isinstance(op.request.get("patch"), dict) and "starting_point" in op.request["patch"])
        )
    }
    require(
        all(
            op.actor_id == actor_id
            and op.project_id == project.id
            and (op.operation in ("createProduct", "updateProduct", "changeLifecycle") or op.id in onboarding_operations)
            for op in operations
        ),
        "Product fixture has an unknown or external operation",
    )
    for operation in guidance_updates.values():
        request = operation.request
        require(
            starting_point in ("existing_assets", "running_product")
            and operation.state == "succeeded"
            and operation.binding_id is None
            and operation.provider_operation_id is None
            and isinstance(operation.receipt, dict)
            and isinstance(request, dict)
            and request.get("product_id") == product.id
            and request.get("project_id") == project.id
            and request.get("patch") == {"starting_point": starting_point}
            and operation.request_hash == hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "Product fixture has an unsafe guidance starting-point update",
        )
    for operation in onboarding_operations.values():
        request = operation.request
        require(
            operation.state == "succeeded"
            and operation.binding_id is None
            and operation.provider_operation_id is None
            and isinstance(operation.receipt, dict)
            and isinstance(request, dict)
            and request.get("product_id") == product.id
            and request.get("project_id") == project.id
            and operation.request_hash == hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "Product fixture has an unsafe onboarding operation",
        )
        session = await db.get(ProductOnboardingSession, (tenant_id, operation.session_id)) if operation.session_id else None
        require(
            session is not None
            and session.product_id == product.id
            and session.project_id == project.id
            and session.actor_user_id == actor_id
            and session.state == "open"
            and not session.proposals,
            "Product fixture has an external onboarding session",
        )
        intent = request.get("request")
        if operation.operation == "startOnboarding":
            safe_intent = intent == {"mode": "direct", "selected_binding_ids": []}
        elif guidance_starting_point:
            safe_intent = (
                request.get("session_id") == session.id
                and request.get("step_id") == "information"
                and intent == {"action": "save", "product": {"purpose": "Confirmed by direct edit"}}
            )
        else:
            safe_intent = (
                request.get("session_id") == session.id
                and request.get("step_id") == "financials"
                and isinstance(intent, dict)
                and (
                    intent == {"action": "save", "configuration": {"financials": {}}}
                    or (
                        set(intent) <= {"action", "reason"}
                        and intent.get("action") in ("later", "not_applicable")
                        and isinstance(intent.get("reason"), str)
                        and bool(intent["reason"].strip())
                    )
                )
            )
        require(safe_intent, "Product fixture has configured onboarding history")
    changes = (
        await db.scalars(
            select(ProductChange).where(
                ProductChange.tenant_id == tenant_id,
                ProductChange.product_id == product.id,
            )
        )
    ).all()
    allowed_paths = {
        "/product",
        "/project",
        "/product/name",
        "/product/purpose",
        "/product/intended_users",
        "/product/archived_at",
        "/fixture_cleanup",
    }
    operation_paths = {
        operation_id: (
            {"/steps/information", "/product/purpose"} if guidance_starting_point else allowed_paths | {"/steps/financials", "/project/configuration"}
        )
        for operation_id in onboarding_operations
    }
    operation_paths.update({operation_id: {"/product/starting_point"} for operation_id in guidance_updates})
    require(
        all(
            change.actor_id == actor_id
            and (
                change.project_id == project.id
                or (
                    change.project_id is None
                    and isinstance(change.snapshot, dict)
                    and isinstance(scopes := change.snapshot.get("scope_snapshots"), dict)
                    and set(scopes) == {project.id}
                    and isinstance(scopes[project.id], dict)
                    and scopes[project.id].get("tenant_id") == tenant_id
                    and scopes[project.id].get("product_id") == product.id
                    and scopes[project.id].get("project_id") == project.id
                )
            )
            and change.operation_id in {op.id for op in operations}
            and set(change.changed_paths) <= operation_paths.get(change.operation_id, allowed_paths)
            for change in changes
        ),
        "Product fixture has external or configured history",
    )

    # Creation now assigns the member's primary team. Retirement may retain
    # exactly that original assignment, never a later configured/reassigned team.
    original_changes = [change for change in changes if change.operation_id == create.id and change.revision == 1]
    original_product = original_changes[0].snapshot.get("product", {}) if len(original_changes) == 1 else {}
    original_team_id = original_product.get("team_id") if isinstance(original_product, dict) else None
    require(
        product.team_id == original_team_id
        and product.domain is None
        and not product.goals
        and (not project.configuration or (resume_fixture and resume_configuration(project.configuration)))
        and not project.provenance
        and project.execution_policy_ref is None
        and project.budget_policy_ref is None,
        "Product fixture has configured or provider resources",
    )
    require(
        not starting_point or product.starting_point == (starting_point if guidance_updates else "idea"),
        "Product fixture starting point differs from its recorded journey",
    )
    require(
        not guidance_starting_point
        or (
            product.starting_point == (guidance_starting_point if guidance_updates else "idea")
            and product.name == body["name"]
            and product.purpose in (body.get("purpose"), "Confirmed by direct edit")
            and product.intended_users == body.get("intended_users")
        ),
        "Product fixture differs from the guidance journey",
    )
    receipt = create.receipt or {}
    if receipt.get("fixture_removed") is True:
        require(
            receipt.get("run_id") == run_id
            and product.archived_at is not None
            and project.archived_at is not None
            and bindings[0].valid_to is not None,
            "Product fixture tombstone does not match the recorded removal",
        )
        return dict(receipt)
    require(
        product.archived_at is None
        or any(
            operation.operation == "changeLifecycle"
            and isinstance(operation.request, dict)
            and operation.request.get("action") == "archive"
            and isinstance(operation.receipt, dict)
            and operation.receipt.get("revision") == product.revision
            and change.operation_id == operation.id
            and change.revision == product.revision
            and change.changed_paths == ["/product/archived_at"]
            and isinstance(change.snapshot, dict)
            and change.snapshot.get("archived_at") == product.archived_at.isoformat()
            for operation in operations
            for change in changes
        ),
        "Product fixture archive has no matching owned lifecycle receipt",
    )
    require(
        bindings[0].valid_to is None and project.archived_at is None and product.name in (body["name"], body["name"] + "-edited"),
        "Product fixture is already removed or has an unexpected identity",
    )
    now = utcnow()
    bindings[0].valid_to = now
    product.archived_at = project.archived_at = now
    product.lifecycle_status = "retired"
    project.state = "archived"
    product.revision += 1
    receipt = {**receipt, "fixture_removed": True, "run_id": run_id, "cleanup_revision": product.revision}
    create.receipt = receipt
    db.add(
        ProductChange(
            tenant_id=tenant_id,
            id=new_uuid(),
            product_id=product.id,
            project_id=project.id,
            operation_id=create.id,
            actor_id=actor_id,
            revision=product.revision,
            changed_paths=["/fixture_cleanup"],
            snapshot={},
            source_revisions=[source_revision],
            relationship_versions=[{"binding_id": bindings[0].id, "valid_to": now.isoformat()}],
        )
    )
    await db.flush()
    return receipt
