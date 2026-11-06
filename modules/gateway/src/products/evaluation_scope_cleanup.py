"""Check original public-route receipts before retiring a two-scope evaluation fixture."""

import hashlib
import re
from datetime import UTC

from sqlalchemy import or_, select

from src.product_onboarding.routes import fingerprint
from src.products.models import ProductChange, ProductOperation, ProductProjectBinding, ProductSourceBinding, Project
from src.shared.models.base import new_uuid, utcnow


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def retire_scope_product(db, manifest, create, product, default, actor_id, source_revision, require):
    scope = manifest["scope"]
    require(
        isinstance(scope, dict) and set(scope) == {"project_id", "intents", "inputs", "sources", "team_share", "editor_id", "retire_key"},
        "Scope retirement requires an exact original fixture journal",
    )
    second_id, intents, inputs, sources = (scope[field] for field in ("project_id", "intents", "inputs", "sources"))
    scope_name = "eval-scope-" + manifest["run_id"]
    require(
        isinstance(second_id, str)
        and second_id != default.id
        and second_id != scope_name
        and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", second_id) is not None
        and isinstance(intents, dict)
        and set(intents) == {"second_project", "source_0", "source_1", "configuration_0", "configuration_1"}
        and isinstance(inputs, list)
        and len(inputs) == 2
        and isinstance(sources, list)
        and len(sources) == 2
        and all(isinstance(source_id, str) and source_id for source_id in sources)
        and len(set(sources)) == 2
        and isinstance(scope["retire_key"], str)
        and re.fullmatch(r"[a-f0-9]{32}", scope["retire_key"]),
        "Scope retirement identities differ from the original run",
    )
    tenant_id = product.tenant_id
    second = await db.scalar(select(Project).where(Project.tenant_id == tenant_id, Project.id == second_id).with_for_update())
    require(
        second is not None and second.name == scope_name and second.owner_user_id == actor_id, "Second Project is not an owned evaluation fixture"
    )
    projects = (default, second)
    bindings = (
        await db.scalars(
            select(ProductProjectBinding)
            .where(
                ProductProjectBinding.tenant_id == tenant_id,
                or_(ProductProjectBinding.product_id == product.id, ProductProjectBinding.project_id.in_([default.id, second.id])),
            )
            .with_for_update()
        )
    ).all()
    current_bindings = [binding for binding in bindings if binding.valid_to is None]
    previous = [binding for binding in bindings if binding.valid_to is not None]
    require(
        len(bindings) in (2, 3)
        and {binding.project_id for binding in bindings} == {default.id, second.id}
        and all(binding.product_id == product.id and binding.provenance == {"actor_id": actor_id, "source": "direct_input"} for binding in bindings),
        "Scope binding is shared, rebound or unowned",
    )
    operations = (
        await db.scalars(
            select(ProductOperation).where(
                ProductOperation.tenant_id == tenant_id,
                ProductOperation.product_id == product.id,
            )
        )
    ).all()
    by_id = {operation.id: operation for operation in operations}
    expected = {create.id: ("createProduct", actor_id, default.id, None)}

    def accept(label, name, who, project_id, check):
        intent = intents[label] if label in intents else scope[label]
        require(
            isinstance(intent, dict) and isinstance(intent.get("key"), str) and isinstance(intent.get("revision"), int) and intent["revision"] > 0,
            "Scope operation lacks an original durable intent",
        )
        key_hash = fingerprint({"key": intent["key"]}) if name == "createBinding" else hashlib.sha256(intent["key"].encode()).hexdigest()
        matches = [
            operation for operation in operations if operation.key_hash == key_hash and operation.operation == name and operation.actor_id == who
        ]
        require(len(matches) == 1, "Scope operation does not match its original public-route receipt")
        operation = matches[0]
        require(
            operation.project_id == project_id
            and operation.expected_revision in (None, intent["revision"])
            and operation.state == "succeeded"
            and isinstance(operation.receipt, dict)
            and isinstance(operation.request, dict)
            and check(operation.request, operation.receipt, intent),
            "Scope operation differs from its owned public-route intent",
        )
        expected[operation.id] = (name, who, project_id, label)
        return operation

    accept(
        "second_project",
        "bindProject",
        actor_id,
        second.id,
        lambda request, receipt, intent: request
        == {"product_id": product.id, "project_id": None, "expected_revision": intent["revision"], "name": scope_name}
        and receipt.get("profile", {}).get("project_id") == second.id,
    )
    source_rows = (
        await db.scalars(
            select(ProductSourceBinding)
            .where(ProductSourceBinding.tenant_id == tenant_id, ProductSourceBinding.project_id.in_([default.id, second.id]))
            .with_for_update()
        )
    ).all()
    require(len(source_rows) == 2 and {row.id for row in source_rows} == set(sources), "Scope has an unexpected source association")
    receipt = create.receipt or {}
    if receipt.get("fixture_removed") is True:
        require(
            receipt.get("run_id") == manifest["run_id"]
            and receipt.get("retire_key") == scope["retire_key"]
            and product.archived_at is not None
            and all(project.archived_at is not None for project in projects)
            and all(binding.valid_to is not None for binding in bindings)
            and all(source.unlinked_at is not None and source.status == "unlinked" for source in source_rows),
            "Scope tombstone changed",
        )
        return dict(receipt)
    require(
        len(bindings) in (2, 3)
        and {binding.project_id for binding in bindings} == {default.id, second.id}
        and all(binding.product_id == product.id and binding.provenance == {"actor_id": actor_id, "source": "direct_input"} for binding in bindings)
        and len(current_bindings) == 2
        and {binding.project_id for binding in current_bindings} == {default.id, second.id}
        and len(previous) == len(bindings) - 2
        and (
            not previous
            or (
                previous[0].project_id == second.id
                and previous[0].relationship_revision == 2
                and previous[0].supersedes_revision == 1
                and aware(previous[0].valid_from) < aware(previous[0].valid_to)
                and aware(previous[0].valid_to) <= aware(next(binding.valid_from for binding in current_bindings if binding.project_id == second.id))
            )
        ),
        "Scope binding is shared, rebound or unowned",
    )
    for index, project in enumerate(projects):
        input_value, source = inputs[index], next(row for row in source_rows if row.id == sources[index])
        selection = input_value.get("selection") if isinstance(input_value, dict) else None
        require(
            isinstance(selection, dict)
            and source.project_id == project.id
            and source.kind == selection.get("kind") == "repository"
            and source.provider == selection.get("provider") == "github"
            and source.namespace == selection.get("namespace")
            and source.native_id == selection.get("native_id")
            and selection.get("connection_ref") == {"kind": "github_installation", "id": source.installation_id}
            and source.desired == selection
            and source.credential_id is None
            and source.unlinked_at is None
            and isinstance(project.configuration, dict)
            and isinstance(project.configuration.get("financials"), dict)
            and project.configuration.get("financials", {}).get("release_target_date") == input_value.get("release_target_date")
            and project.execution_policy_ref is None
            and project.budget_policy_ref is None,
            "Scope source or configuration changed outside the owned fixture",
        )
        accept(
            f"source_{index}",
            "createBinding",
            actor_id,
            project.id,
            lambda request, receipt, intent: request == {"selection": selection, "expected_revision": intent["revision"]}
            and receipt.get("binding_id") == source.id,
        )
        configuration_intent = intents[f"configuration_{index}"]
        require(
            isinstance(configuration_intent, dict)
            and isinstance(configuration_intent.get("body"), dict)
            and isinstance(configuration_intent["body"].get("configuration"), dict),
            "Scope configuration compensation lacks its original request",
        )
        configured = accept(
            f"configuration_{index}",
            "updateProject",
            actor_id,
            project.id,
            lambda request, receipt, intent: request.get("product_id") == product.id
            and request.get("project_id") == project.id
            and request.get("expected_revision") == intent["revision"]
            and request.get("patch") == {"configuration": intent["body"]["configuration"]}
            and receipt.get("profile", {}).get("project_id") == project.id
            and project.configuration == intent["body"]["configuration"],
        )
        recorded = configured.receipt.get("profile", {}).get("provenance")
        require(
            isinstance(recorded, list) and (project.provenance or []) == recorded,
            "Scope provenance differs from its original configuration receipt",
        )
    team = scope["team_share"]
    require(product.domain is None and not product.goals and product.starting_point == "idea", "Scope Product has unowned configuration")
    if team is None:
        require(product.team_id is None, "Shared-team assignment was not journaled")
    else:
        require(
            isinstance(team, dict)
            and team.get("product_id") == product.id
            and team.get("project_id") == default.id
            and team.get("previous_team_id") is None
            and isinstance(scope["editor_id"], str)
            and product.team_id == team.get("team_id"),
            "Shared-team assignment differs from its original run",
        )
        accept(
            "team_share",
            "updateProduct",
            actor_id,
            default.id,
            lambda request, receipt, intent: request.get("patch") == {"team_id": team["team_id"]}
            and request.get("expected_revision") == team["revision"],
        )
    editor_id = scope["editor_id"]
    editor = [operation for operation in operations if operation.actor_id == editor_id]
    if editor:
        require(
            len(editor) == 1 and editor[0].key_hash == hashlib.sha256(("scope-editor-" + manifest["run_id"]).encode()).hexdigest(),
            "Unexpected editor operation",
        )
        operation = editor[0]
        require(
            operation.operation == "updateProduct"
            and operation.project_id == default.id
            and operation.request.get("patch") == {"purpose": "Verify ordinary editor scope access"}
            and operation.state == "succeeded"
            and product.purpose == "Verify ordinary editor scope access",
            "Editor changed the scope outside the reviewed ordinary edit",
        )
        expected[operation.id] = ("updateProduct", editor_id, default.id, "editor")
    else:
        require(product.purpose == create.request["purpose"], "Scope Product purpose changed outside the run")
    if previous:
        history = {}
        for label, name in (("unbind", "unbindProject"), ("rebind", "bindProject")):
            key = hashlib.sha256(("scope_" + label + "_" + manifest["run_id"]).encode()).hexdigest()
            matches = [operation for operation in operations if operation.key_hash == key and operation.operation == name]
            require(len(matches) == 1, "Scope history lacks its original installed-CLI receipt")
            operation = matches[0]
            require(
                operation.actor_id == actor_id
                and operation.project_id == second.id
                and operation.state == "succeeded"
                and isinstance(operation.receipt, dict)
                and isinstance(operation.request, dict)
                and operation.request
                == {"product_id": product.id, "project_id": second.id, "expected_revision": operation.request.get("expected_revision")}
                and type(operation.request["expected_revision"]) is int
                and operation.receipt.get("profile", {}).get("project_id") == (default.id if label == "unbind" else second.id),
                "Scope history differs from the original owned Project",
            )
            history[label] = operation
            expected[operation.id] = (name, actor_id, second.id, label)
        require(
            history["rebind"].request["expected_revision"] == history["unbind"].request["expected_revision"] + 1,
            "Scope history revisions are not consecutive",
        )
    require(len(expected) == len(operations) and set(expected) == set(by_id), "Scope Product has unknown or external operations")
    changes = (
        await db.scalars(
            select(ProductChange).where(
                ProductChange.tenant_id == tenant_id,
                ProductChange.product_id == product.id,
            )
        )
    ).all()
    paths = {
        "createProduct": ["/product", "/project"],
        "bindProject": ["/project/binding"],
        "createBinding": ["/bindings"],
        "updateProject": ["/project/configuration"],
        "unbindProject": ["/project/binding"],
    }
    require(
        len(changes) == len(expected)
        and all(
            change.operation_id in expected
            and change.actor_id == expected[change.operation_id][1]
            and change.project_id == expected[change.operation_id][2]
            and change.changed_paths
            == (
                ["/product/team_id"]
                if expected[change.operation_id][3] == "team_share"
                else ["/product/purpose"]
                if expected[change.operation_id][3] == "editor"
                else paths[expected[change.operation_id][0]]
            )
            for change in changes
        ),
        "Scope Product has unknown or external change history",
    )
    if previous:
        historic = {
            expected[change.operation_id][3]: change
            for change in changes
            if change.operation_id in expected and expected[change.operation_id][3] in ("unbind", "rebind")
        }
        require(
            set(historic) == {"unbind", "rebind"}
            and all(
                isinstance(change.relationship_versions, list)
                and len(change.relationship_versions) == 1
                and isinstance(change.relationship_versions[0], dict)
                for change in historic.values()
            )
            and historic["unbind"].relationship_versions[0].get("relationship_id") == previous[0].id
            and historic["rebind"].relationship_versions[0].get("relationship_id")
            == next(binding.id for binding in current_bindings if binding.project_id == second.id),
            "Scope Project historical links differ from their original binding receipts",
        )
    require(
        product.archived_at is None and all(project.archived_at is None for project in projects) and len(current_bindings) == 2,
        "Scope retirement changed outside this run",
    )
    now = utcnow()
    for binding in current_bindings:
        binding.valid_to = now
    for source in source_rows:
        source.status = "unlinked"
        source.unlinked_at = now
        source.generation += 1
        source.revision += 1
    for project in projects:
        project.archived_at = now
        project.state = "archived"
    product.archived_at = now
    product.lifecycle_status = "retired"
    product.team_id = None
    product.revision += 1
    receipt = {
        **receipt,
        "fixture_removed": True,
        "run_id": manifest["run_id"],
        "retire_key": scope["retire_key"],
        "cleanup_revision": product.revision,
        "scope_project_id": second.id,
    }
    create.receipt = receipt
    db.add(
        ProductChange(
            tenant_id=tenant_id,
            id=new_uuid(),
            product_id=product.id,
            project_id=default.id,
            operation_id=create.id,
            actor_id=actor_id,
            revision=product.revision,
            changed_paths=["/fixture_cleanup"],
            snapshot={},
            source_revisions=[source_revision],
            relationship_versions=[{"binding_id": binding.id, "valid_to": now.isoformat()} for binding in current_bindings],
        )
    )
    await db.flush()
    return receipt
