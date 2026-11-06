"""Evaluation-only Product retirement retains history and refuses other assets."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.product_onboarding.routes import fingerprint
from src.products.evaluation_cleanup import FixtureOwnershipError, retire_run_product
from src.products.evaluation_cleanup_routes import CAPABILITY, EVALUATOR_REGISTRY_ID
from src.products.models import Product, ProductChange, ProductOperation, ProductProjectBinding, ProductSourceBinding, Project
from src.products.routes import record_financial_edits
from src.shared.models.organization import Organization, Team, User

RUN = "test-run-0001"
TENANT = "evaluation-tenant"
ACTOR = "fixture-owner"
KEY = "product_eval_create_001"
SOURCE = "a" * 40
TEST_ROLE = "arn:aws:iam::123456789012:role/product-evaluator-retirement"
TEST_HEADERS = {
    "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/product-evaluator-retirement/evaluation",
    "X-Adp-Edge-Provenance": "controlled-edge-proof",
}
RETIRE_PATH = "/internal/v1/product-evaluation/retire"
PRODUCT = "owned-product-id"
PROJECT = "owned-project-id"
# Public bindProject create mode mints a UUID Project ID that never equals the display name.
SECOND_PROJECT = "6f1a2b3c-4d5e-4f60-8a71-9b2c3d4e5f60"


async def fixture(db, *, name=None):
    body = {
        "name": name or f"eval-product-{RUN}",
        "purpose": "Verify Product",
        "intended_users": ["Maintainers"],
        "starting_point": "idea",
        "project": {"mode": "create"},
    }
    now = datetime.now(UTC)
    db.add_all(
        [
            Organization(id=TENANT, name="eval-regression-owned-tenant"),
            Product(
                tenant_id=TENANT,
                id=PRODUCT,
                name=body["name"],
                purpose=body["purpose"],
                intended_users=body["intended_users"],
                owner_user_id=ACTOR,
                starting_point="idea",
                created_by=ACTOR,
            ),
            Project(tenant_id=TENANT, id=PROJECT, name=body["name"], owner_user_id=ACTOR, configuration={}, provenance=[]),
            ProductProjectBinding(
                tenant_id=TENANT, id="owned-binding", product_id=PRODUCT, project_id=PROJECT, provenance={"actor_id": ACTOR, "source": "direct_input"}
            ),
            ProductOperation(
                tenant_id=TENANT,
                id="create-operation",
                product_id=PRODUCT,
                project_id=PROJECT,
                actor_id=ACTOR,
                operation="createProduct",
                key_hash=hashlib.sha256(KEY.encode()).hexdigest(),
                request_hash=hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                request=body,
                expires_at=now + timedelta(days=1),
                receipt={"product_id": PRODUCT, "project_id": PROJECT, "revision": 1},
            ),
            ProductChange(
                tenant_id=TENANT,
                id="first-change",
                product_id=PRODUCT,
                project_id=PROJECT,
                revision=1,
                actor_id=ACTOR,
                operation_id="create-operation",
                changed_paths=["/product", "/project"],
                snapshot={"product": {"name": body["name"]}},
                relationship_versions=[],
            ),
        ]
    )
    await db.commit()
    return {
        "tenant_id": TENANT,
        "actor_id": ACTOR,
        "run_id": RUN,
        "source_revision": SOURCE,
        "idempotency_key": KEY,
        "product_id": PRODUCT,
        "project_id": PROJECT,
    }


@pytest.mark.asyncio
async def test_only_owned_fixture_becomes_invisible_with_immutable_audit(db_session):
    manifest = await fixture(db_session)
    receipt = await retire_run_product(db_session, manifest)
    await db_session.commit()
    assert receipt == {"product_id": PRODUCT, "project_id": PROJECT, "revision": 1, "fixture_removed": True, "run_id": RUN, "cleanup_revision": 2}
    assert await db_session.scalar(select(func.count()).select_from(Product)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2
    original = await db_session.scalar(select(ProductChange).where(ProductChange.id == "first-change"))
    assert original.snapshot == {"product": {"name": f"eval-product-{RUN}"}}
    active = await db_session.scalar(
        select(ProductProjectBinding).where(
            ProductProjectBinding.tenant_id == TENANT,
            ProductProjectBinding.product_id == PRODUCT,
            ProductProjectBinding.valid_to.is_(None),
        )
    )
    assert active is None
    change = await db_session.scalar(select(ProductChange).where(ProductChange.revision == 2))
    assert change.changed_paths == ["/fixture_cleanup"] and change.source_revisions == [SOURCE]
    assert (await retire_run_product(db_session, manifest)) == receipt
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2


@pytest.mark.asyncio
async def test_unknown_response_can_resolve_from_original_key(db_session):
    manifest = await fixture(db_session, name=f"Owned Product {RUN}")
    manifest.pop("product_id")
    manifest.pop("project_id")
    assert (await retire_run_product(db_session, manifest))["product_id"] == PRODUCT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"tenant_id": "another-tenant"}, "receipt is missing|evaluation workspace"),
        ({"actor_id": "foreign-actor"}, "receipt is missing"),
        ({"idempotency_key": "another_product_key_123"}, "receipt is missing"),
        ({"product_id": "another-product"}, "receipt differs"),
        ({"run_id": "foreign-run-0001"}, "does not match"),
        ({"source_revision": "not-a-sha"}, "pinned run"),
    ],
)
async def test_wrong_owner_scope_or_pinned_run_refuses_mutation(db_session, change, reason):
    manifest = await fixture(db_session)
    manifest.update(change)
    with pytest.raises(FixtureOwnershipError, match=reason):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.asyncio
async def test_reused_provider_project_refuses_cleanup(db_session):
    manifest = await fixture(db_session)
    project = await db_session.scalar(select(Project).where(Project.id == PROJECT))
    project.configuration = {"selected_provider": "shared"}
    await db_session.commit()
    with pytest.raises(FixtureOwnershipError, match="configured or provider"):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.scalar(select(ProductProjectBinding))).valid_to is None


@pytest.mark.asyncio
async def test_forged_binding_provenance_refuses_cleanup(db_session):
    manifest = await fixture(db_session)
    binding = await db_session.scalar(select(ProductProjectBinding))
    binding.provenance = {"actor_id": "someone-else", "source": "direct_input"}
    await db_session.commit()
    with pytest.raises(FixtureOwnershipError, match="provenance"):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.parametrize("failure", ["missing", "extra_scope", "tenant", "product", "project"])
async def test_product_wide_history_must_still_belong_to_only_the_owned_scope(db_session, failure):
    manifest = await fixture(db_session)
    change = await db_session.scalar(select(ProductChange).where(ProductChange.id == "first-change"))
    snapshot = {"tenant_id": TENANT, "product_id": PRODUCT, "project_id": PROJECT}
    scopes = {PROJECT: snapshot}
    if failure == "extra_scope":
        scopes["shared-project"] = dict(snapshot, project_id="shared-project")
    elif failure != "missing":
        snapshot[f"{failure}_id"] = "foreign"
    change.project_id = None
    change.snapshot = {} if failure == "missing" else {"scope_snapshots": scopes}
    await db_session.commit()
    with pytest.raises(FixtureOwnershipError, match="external or configured history"):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.asyncio
async def test_unknown_product_operation_refuses_cleanup(db_session):
    manifest = await fixture(db_session)
    db_session.add(
        ProductOperation(
            tenant_id=TENANT,
            id="external-operation",
            product_id=PRODUCT,
            project_id=PROJECT,
            actor_id=ACTOR,
            operation="provisionResources",
            key_hash="f" * 64,
            request_hash="f" * 64,
            request={},
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await db_session.commit()
    with pytest.raises(FixtureOwnershipError, match="external operation"):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.asyncio
async def test_external_history_refuses_cleanup(db_session):
    manifest = await fixture(db_session)
    db_session.add(
        ProductChange(
            tenant_id=TENANT,
            id="foreign-change",
            product_id=PRODUCT,
            project_id=PROJECT,
            revision=2,
            actor_id=ACTOR,
            operation_id="create-operation",
            changed_paths=["/project/provider"],
            snapshot={},
            relationship_versions=[],
        )
    )
    await db_session.commit()
    with pytest.raises(FixtureOwnershipError, match="configured history"):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.asyncio
async def test_retired_fixture_is_absent_by_id_list_and_original_create_replay(
    test_client,
    app,
    db_session,
    valid_token_context,
):
    from src.auth.dependencies import get_current_user

    manifest = await fixture(db_session)
    db_session.add(User(id=ACTOR, org_id=TENANT, team_id=valid_token_context.team_id, email="owner@example.test", user_kind="human"))
    await db_session.commit()
    # Legacy teamless fixtures are readable through the verified-admin dependency context.
    app.dependency_overrides[get_current_user] = lambda: valid_token_context.model_copy(update={"org_id": TENANT, "user_id": ACTOR, "is_admin": True})
    assert (await test_client.get(f"/v1/products/{PRODUCT}/projects/{PROJECT}")).status_code == 200
    await retire_run_product(db_session, manifest)
    await db_session.commit()
    assert (await test_client.get(f"/v1/products/{PRODUCT}/projects/{PROJECT}")).status_code == 404
    listing = await test_client.get("/v1/products")
    assert listing.status_code == 200 and all(item["id"] != PRODUCT for item in listing.json()["items"])
    operation = await db_session.scalar(select(ProductOperation).where(ProductOperation.id == "create-operation"))
    replay = await test_client.post("/v1/products", json=operation.request, headers={"Idempotency-Key": KEY})
    assert replay.status_code == 410 and replay.json()["detail"]["code"] == "fixture_removed"


@pytest.mark.asyncio
async def test_real_api_archive_restore_then_retirement_keeps_audit(test_client, app, db_session, valid_token_context):
    from src.auth.dependencies import get_current_user

    manifest = await fixture(db_session)
    db_session.add(User(id=ACTOR, org_id=TENANT, team_id=valid_token_context.team_id, email="owner@example.test", user_kind="human"))
    await db_session.commit()
    # Legacy teamless fixtures are readable through the verified-admin dependency context.
    app.dependency_overrides[get_current_user] = lambda: valid_token_context.model_copy(update={"org_id": TENANT, "user_id": ACTOR, "is_admin": True})
    path = f"/v1/products/{PRODUCT}/projects/{PROJECT}"
    edit = await test_client.patch(
        path,
        json={"name": f"eval-product-{RUN}-edited", "purpose": "Updated"},
        headers={"Idempotency-Key": "product-edit-archive-test", "If-Profile-Revision": "1"},
    )
    assert edit.status_code == 200, edit.text
    for action, revision in (("archive", 2), ("restore", 3)):
        result = await test_client.post(
            f"{path}/lifecycle",
            json={"action": action, "reason": "Evaluation"},
            headers={"Idempotency-Key": f"product-{action}-owned-test", "If-Profile-Revision": str(revision)},
        )
        assert result.status_code == 200, result.text
    assert (await test_client.get(f"{path}/changes")).status_code == 200
    receipt = await retire_run_product(db_session, manifest)
    await db_session.commit()
    assert receipt["revision"] == 1 and receipt["cleanup_revision"] == 5
    for target in (path, f"{path}/changes", f"{path}/revisions/1"):
        response = await test_client.get(target)
        assert response.status_code == 404, response.text
    listing = await test_client.get("/v1/products")
    assert listing.status_code == 200 and all(item["id"] != PRODUCT for item in listing.json()["items"])
    operation = await db_session.scalar(select(ProductOperation).where(ProductOperation.id == "create-operation"))
    replay = await test_client.post("/v1/products", json=operation.request, headers={"Idempotency-Key": KEY})
    assert replay.status_code == 410 and replay.json()["detail"]["code"] == "fixture_removed"
    changes = (await db_session.scalars(select(ProductChange).where(ProductChange.product_id == PRODUCT))).all()
    assert [change.revision for change in changes] == [1, 2, 3, 4, 5]
    assert changes[-1].changed_paths == ["/fixture_cleanup"]
    assert changes[0].snapshot == {"product": {"name": f"eval-product-{RUN}"}}


@pytest.mark.asyncio
async def test_interrupted_after_real_archive_retires_only_the_owned_fixture(test_client, app, db_session, valid_token_context):
    from src.auth.dependencies import get_current_user

    manifest = await fixture(db_session)
    db_session.add(User(id=ACTOR, org_id=TENANT, team_id=valid_token_context.team_id, email="owner@example.test", user_kind="human"))
    await db_session.commit()
    # Legacy teamless fixtures are readable through the verified-admin dependency context.
    app.dependency_overrides[get_current_user] = lambda: valid_token_context.model_copy(update={"org_id": TENANT, "user_id": ACTOR, "is_admin": True})
    path = f"/v1/products/{PRODUCT}/projects/{PROJECT}"
    archived = await test_client.post(
        f"{path}/lifecycle",
        json={"action": "archive", "reason": "Evaluation"},
        headers={"Idempotency-Key": "product-archive-interrupted", "If-Profile-Revision": "1"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"]
    receipt = await retire_run_product(db_session, manifest)
    await db_session.commit()
    assert receipt["cleanup_revision"] == 3
    for target in (path, f"{path}/changes", f"{path}/revisions/1"):
        assert (await test_client.get(target)).status_code == 404
    operation = await db_session.scalar(select(ProductOperation).where(ProductOperation.id == "create-operation"))
    replay = await test_client.post("/v1/products", json=operation.request, headers={"Idempotency-Key": KEY})
    assert replay.status_code == 410 and replay.json()["detail"]["code"] == "fixture_removed"
    changes = (await db_session.scalars(select(ProductChange).where(ProductChange.product_id == PRODUCT))).all()
    assert [change.revision for change in changes] == [1, 2, 3]
    assert changes[0].snapshot == {"product": {"name": f"eval-product-{RUN}"}}
    assert changes[-1].changed_paths == ["/fixture_cleanup"]
    assert await retire_run_product(db_session, manifest) == receipt


@pytest.mark.asyncio
async def test_unrecorded_archive_state_cannot_be_reclassified_as_owned_cleanup(db_session):
    manifest = await fixture(db_session)
    product = await db_session.scalar(select(Product).where(Product.id == PRODUCT))
    product.archived_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(FixtureOwnershipError, match="archive|unexpected"):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.scalar(select(ProductProjectBinding))).valid_to is None


@pytest.mark.asyncio
async def test_run_owned_second_scope_requires_explicit_retirement_proof(db_session):
    manifest = await fixture(db_session)
    second = "eval-scope-" + RUN
    db_session.add_all(
        [
            Project(tenant_id=TENANT, id=second, name=second, owner_user_id=ACTOR, configuration={}, provenance=[]),
            ProductProjectBinding(
                tenant_id=TENANT, id="second-binding", product_id=PRODUCT, project_id=second, provenance={"actor_id": ACTOR, "source": "direct_input"}
            ),
        ]
    )
    await db_session.commit()
    with pytest.raises(FixtureOwnershipError, match="shared or rebound"):
        await retire_run_product(db_session, manifest)


async def scope_fixture(db_session, *, name=None):
    manifest = await fixture(db_session, name=name)
    second, scope_name = SECOND_PROJECT, "eval-scope-" + RUN
    db_session.add_all(
        [
            Project(tenant_id=TENANT, id=second, name=scope_name, owner_user_id=ACTOR, configuration={}, provenance=[]),
            ProductProjectBinding(
                tenant_id=TENANT, id="second-binding", product_id=PRODUCT, project_id=second, provenance={"actor_id": ACTOR, "source": "direct_input"}
            ),
        ]
    )
    scope = {
        "project_id": second,
        "intents": {},
        "inputs": [],
        "sources": [],
        "team_share": None,
        "editor_id": None,
        "retire_key": "a" * 32,
    }

    def operation(label, name, project_id, revision, request, receipt, paths, *, source=False):
        key = "scope-" + label + "-12345678"
        scope["intents"][label] = {"key": key, "revision": revision}
        db_session.add(
            ProductOperation(
                tenant_id=TENANT,
                id="operation-" + label,
                product_id=PRODUCT,
                project_id=project_id,
                actor_id=ACTOR,
                operation=name,
                key_hash=fingerprint({"key": key}) if source else hashlib.sha256(key.encode()).hexdigest(),
                request_hash="a" * 64,
                request=request,
                receipt=receipt,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db_session.add(
            ProductChange(
                tenant_id=TENANT,
                id="change-" + label,
                product_id=PRODUCT,
                project_id=project_id,
                revision=revision + 1,
                actor_id=ACTOR,
                operation_id="operation-" + label,
                changed_paths=paths,
                snapshot={},
                relationship_versions=[],
            )
        )

    operation(
        "second_project",
        "bindProject",
        second,
        1,
        {"product_id": PRODUCT, "project_id": None, "expected_revision": 1, "name": scope_name},
        {"profile": {"project_id": second}},
        ["/project/binding"],
    )
    revision = 2
    for index, project_id in enumerate((PROJECT, second)):
        selection = {
            "kind": "repository",
            "provider": "github",
            "namespace": "example-org",
            "native_id": "repository-" + str(index),
            "connection_ref": {"kind": "github_installation", "id": "connection-" + str(index)},
        }
        release_date = f"202{index + 7}-12-01"
        scope["inputs"].append({"selection": selection, "record_revision": "b" * 40, "release_target_date": release_date})
        source_id = "source-" + str(index)
        scope["sources"].append(source_id)
        db_session.add(
            ProductSourceBinding(
                tenant_id=TENANT,
                id=source_id,
                project_id=project_id,
                kind="repository",
                provider="github",
                namespace="example-org",
                native_id=selection["native_id"],
                installation_id=selection["connection_ref"]["id"],
                desired=selection,
            )
        )
        configuration = {
            "basic": {
                "repositories": [
                    {
                        "tenant_id": TENANT,
                        "project_id": project_id,
                        "source_connection_id": selection["connection_ref"]["id"],
                        "record_type": "repository",
                        "record_id": selection["native_id"],
                        "record_revision": "b" * 40,
                    }
                ]
            },
            "financials": {"release_target_date": release_date},
        }
        project = await db_session.get(Project, (TENANT, project_id))
        owned = await db_session.get(Product, (TENANT, PRODUCT))
        project.configuration = configuration
        # The public configuration route writes an accepted manual financial proof.
        record_financial_edits(project, owned, {}, configuration, ACTOR, revision + 2)
        operation(
            f"source_{index}",
            "createBinding",
            project_id,
            revision,
            {"selection": selection, "expected_revision": revision},
            {"binding_id": source_id},
            ["/bindings"],
            source=True,
        )
        revision += 1
        operation(
            f"configuration_{index}",
            "updateProject",
            project_id,
            revision,
            {"product_id": PRODUCT, "project_id": project_id, "expected_revision": revision, "patch": {"configuration": configuration}},
            {"profile": {"project_id": project_id, "provenance": deepcopy(project.provenance)}},
            ["/project/configuration"],
        )
        scope["intents"][f"configuration_{index}"]["body"] = {"configuration": configuration}
        revision += 1
    product = await db_session.get(Product, (TENANT, PRODUCT))
    product.revision = revision
    await db_session.commit()
    manifest["scope"] = scope
    return manifest


@pytest.mark.asyncio
async def test_scope_retirement_preserves_history_and_reconciles_lost_response(db_session):
    manifest = await scope_fixture(db_session)
    first_change = await db_session.get(ProductChange, (TENANT, "first-change"))
    original = dict(first_change.snapshot)
    receipt = await retire_run_product(db_session, manifest)
    await db_session.commit()
    assert receipt["scope_project_id"] == SECOND_PROJECT
    assert (await db_session.get(Project, (TENANT, SECOND_PROJECT))).name == "eval-scope-" + RUN
    assert await retire_run_product(db_session, manifest) == receipt
    assert (await db_session.get(ProductChange, (TENANT, "first-change"))).snapshot == original
    assert (await db_session.get(Project, (TENANT, manifest["scope"]["project_id"]))).archived_at is not None
    assert all(source.unlinked_at is not None for source in (await db_session.scalars(select(ProductSourceBinding))).all())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["wrong_project", "wrong_source", "invalid_source", "missing_configuration", "external_history", "external_operation", "external_source"],
)
async def test_scope_retirement_refuses_unowned_mutations(db_session, corruption):
    manifest = await scope_fixture(db_session)
    if corruption == "wrong_project":
        manifest["scope"]["project_id"] = "foreign-project"
    elif corruption == "wrong_source":
        manifest["scope"]["sources"][1] = "foreign-source"
    elif corruption == "invalid_source":
        manifest["scope"]["sources"][1] = {"untrusted": "source"}
    elif corruption == "missing_configuration":
        manifest["scope"]["intents"]["configuration_0"].pop("body")
    elif corruption == "external_history":
        product = await db_session.get(Product, (TENANT, PRODUCT))
        product.purpose = "External change"
        await db_session.flush()
    elif corruption == "external_source":
        source = await db_session.get(ProductSourceBinding, (TENANT, "source-1"))
        source.desired = {"kind": "repository", "provider": "github", "native_id": "other-repository"}
        await db_session.flush()
    else:
        db_session.add(
            ProductOperation(
                tenant_id=TENANT,
                id="foreign-operation",
                product_id=PRODUCT,
                project_id=PROJECT,
                actor_id="other-editor",
                operation="updateProduct",
                key_hash="f" * 64,
                request_hash="a" * 64,
                request={"patch": {"purpose": "External"}},
                receipt={"revision": 7},
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await db_session.flush()
    with pytest.raises(FixtureOwnershipError):
        await retire_run_product(db_session, manifest)
    assert (await db_session.get(Product, (TENANT, PRODUCT))).archived_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption,reason",
    [
        ("name_as_id", "identities differ"),
        ("renamed_scope", "owned evaluation fixture"),
        ("bound_uuid_request", "owned public-route intent"),
        ("receipt_project", "owned public-route intent"),
        ("cleared_provenance", "original configuration receipt"),
        ("forged_provenance", "original configuration receipt"),
    ],
)
async def test_scope_retirement_requires_route_shaped_identity_and_provenance(db_session, corruption, reason):
    manifest = await scope_fixture(db_session)
    if corruption == "name_as_id":
        manifest["scope"]["project_id"] = "eval-scope-" + RUN
    elif corruption == "renamed_scope":
        project = await db_session.get(Project, (TENANT, SECOND_PROJECT))
        project.name = SECOND_PROJECT
    elif corruption == "bound_uuid_request":
        bound = await db_session.scalar(select(ProductOperation).where(ProductOperation.id == "operation-second_project"))
        bound.request = {**bound.request, "project_id": SECOND_PROJECT}
    elif corruption == "receipt_project":
        bound = await db_session.scalar(select(ProductOperation).where(ProductOperation.id == "operation-second_project"))
        bound.receipt = {"profile": {"project_id": "eval-scope-" + RUN}}
    elif corruption == "cleared_provenance":
        project = await db_session.get(Project, (TENANT, PROJECT))
        project.provenance = []
    else:
        project = await db_session.get(Project, (TENANT, SECOND_PROJECT))
        project.provenance = [
            *project.provenance,
            {"paths": ["/configuration/financials/release_target_date"], "state": "accepted", "method": "manual"},
        ]
    await db_session.flush()
    with pytest.raises(FixtureOwnershipError, match=reason):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.get(Product, (TENANT, PRODUCT))).archived_at is None
    assert (await db_session.get(Project, (TENANT, SECOND_PROJECT))).archived_at is None
    assert (await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.id == "second-binding"))).valid_to is None


@pytest.mark.asyncio
async def test_scope_retirement_restores_shared_team_after_editor_update(db_session):
    manifest = await scope_fixture(db_session)
    scope = manifest["scope"]
    product = await db_session.get(Product, (TENANT, PRODUCT))
    revision = product.revision
    team_id = "owned-evaluation-team"
    team_key = "team-share-12345678"
    scope["team_share"] = {
        "key": team_key,
        "revision": revision,
        "product_id": PRODUCT,
        "project_id": PROJECT,
        "team_id": team_id,
        "previous_team_id": None,
    }
    scope["editor_id"] = "ordinary-editor"
    editor_key = "scope-editor-" + RUN
    for offset, (identifier, actor, key, patch, changed_paths) in enumerate(
        [
            ("team", ACTOR, team_key, {"team_id": team_id}, ["/product/team_id"]),
            ("editor", "ordinary-editor", editor_key, {"purpose": "Verify ordinary editor scope access"}, ["/product/purpose"]),
        ]
    ):
        db_session.add(
            ProductOperation(
                tenant_id=TENANT,
                id=identifier + "-operation",
                product_id=PRODUCT,
                project_id=PROJECT,
                actor_id=actor,
                operation="updateProduct",
                key_hash=hashlib.sha256(key.encode()).hexdigest(),
                request_hash="a" * 64,
                request={"product_id": PRODUCT, "project_id": PROJECT, "expected_revision": revision + offset, "patch": patch},
                receipt={"revision": revision + offset + 1},
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db_session.add(
            ProductChange(
                tenant_id=TENANT,
                id=identifier + "-change",
                product_id=PRODUCT,
                project_id=PROJECT,
                revision=revision + offset + 1,
                actor_id=actor,
                operation_id=identifier + "-operation",
                changed_paths=changed_paths,
                snapshot={},
            )
        )
    product.team_id = team_id
    product.purpose = "Verify ordinary editor scope access"
    product.revision += 2
    await db_session.commit()
    receipt = await retire_run_product(db_session, manifest)
    await db_session.commit()
    assert receipt["fixture_removed"] is True
    assert product.team_id is None
    assert (await db_session.get(ProductChange, (TENANT, "editor-change"))).changed_paths == ["/product/purpose"]


@pytest.mark.asyncio
async def test_scope_retirement_preserves_dated_unbind_and_rebind_history(db_session):
    manifest = await scope_fixture(db_session)
    second = manifest["scope"]["project_id"]
    first_binding = await db_session.get(ProductProjectBinding, (TENANT, "second-binding"))
    closed_at = datetime.now(UTC) - timedelta(days=1)
    first_binding.valid_from = closed_at - timedelta(days=1)
    first_binding.valid_to = closed_at
    first_binding.relationship_revision = 2
    first_binding.supersedes_revision = 1
    db_session.add(
        ProductProjectBinding(
            tenant_id=TENANT,
            id="rebound-binding",
            product_id=PRODUCT,
            project_id=second,
            provenance={"actor_id": ACTOR, "source": "direct_input"},
            valid_from=datetime.now(UTC),
        )
    )
    product = await db_session.get(Product, (TENANT, PRODUCT))
    revision = product.revision
    for offset, (label, name, profile_id, binding_id) in enumerate(
        [
            ("unbind", "unbindProject", PROJECT, "second-binding"),
            ("rebind", "bindProject", second, "rebound-binding"),
        ]
    ):
        key = "scope_" + label + "_" + RUN
        operation_id = "history-" + label
        db_session.add(
            ProductOperation(
                tenant_id=TENANT,
                id=operation_id,
                product_id=PRODUCT,
                project_id=second,
                actor_id=ACTOR,
                operation=name,
                key_hash=hashlib.sha256(key.encode()).hexdigest(),
                request_hash="a" * 64,
                request={"product_id": PRODUCT, "project_id": second, "expected_revision": revision + offset},
                receipt={"revision": revision + offset + 1, "profile": {"project_id": profile_id}},
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db_session.add(
            ProductChange(
                tenant_id=TENANT,
                id=operation_id + "-change",
                product_id=PRODUCT,
                project_id=second,
                revision=revision + offset + 1,
                actor_id=ACTOR,
                operation_id=operation_id,
                changed_paths=["/project/binding"],
                snapshot={},
                relationship_versions=[{"relationship_id": binding_id}],
            )
        )
    product.revision += 2
    await db_session.commit()
    receipt = await retire_run_product(db_session, manifest)
    await db_session.commit()
    assert receipt["fixture_removed"] is True
    await db_session.refresh(first_binding)
    assert first_binding.valid_to == closed_at.replace(tzinfo=None) or first_binding.valid_to == closed_at
    assert first_binding.relationship_revision == 2
    assert (await db_session.get(ProductProjectBinding, (TENANT, "rebound-binding"))).valid_to is not None
    assert await retire_run_product(db_session, manifest) == receipt


@pytest.mark.asyncio
async def test_scope_retirement_refuses_interrupted_unbind(db_session):
    manifest = await scope_fixture(db_session)
    binding = await db_session.get(ProductProjectBinding, (TENANT, "second-binding"))
    binding.valid_to = datetime.now(UTC)
    await db_session.flush()
    with pytest.raises(FixtureOwnershipError, match="binding"):
        await retire_run_product(db_session, manifest)
    assert (await db_session.get(Product, (TENANT, PRODUCT))).archived_at is None


async def connected_scope_fixture(db_session, monkeypatch, *, name_suffix="-scope"):
    monkeypatch.setitem(globals(), "RUN", "product-42-1")
    manifest = await scope_fixture(db_session, name=f"eval-product-{RUN}{name_suffix}")
    tenant = await db_session.get(Organization, TENANT)
    tenant.name = "connected-fixture-tenant"
    db_session.add_all(
        [
            Team(id="fixture-team", org_id=TENANT, department_id="fixture-department", name="B03 fixture team"),
            User(
                id=ACTOR,
                org_id=TENANT,
                team_id="fixture-team",
                email="owner@example.test",
                role="member",
                cognito_sub="owner-sub",
                cognito_username="eval-regression-owner",
                user_kind="human",
            ),
            User(
                id="fixture-editor",
                org_id=TENANT,
                team_id="fixture-team",
                email="editor@example.test",
                role="member",
                cognito_sub="editor-sub",
                cognito_username="eval-regression-editor",
                user_kind="human",
            ),
            Project(tenant_id=TENANT, id="original-project", name="Untouched", owner_user_id="other", configuration={}, provenance=[]),
        ]
    )
    manifest["scope"]["editor_id"] = "fixture-editor"
    manifest["scope_fixture_mode"] = "connected_scope"
    await db_session.commit()
    return manifest


@pytest.mark.asyncio
@pytest.mark.parametrize("name_suffix", ["", "-scope"], ids=["plain_run_name", "legacy_scope_name"])
async def test_connected_scope_retires_only_dedicated_original_fixture(db_session, monkeypatch, name_suffix):
    # Issue #8250: the browser fixture provider creates the plain run-named
    # Product for the connected-scope case, so both names must retire.
    manifest = await connected_scope_fixture(db_session, monkeypatch, name_suffix=name_suffix)
    receipt = await retire_run_product(db_session, manifest)
    await db_session.commit()
    assert receipt["fixture_removed"] is True
    assert await retire_run_product(db_session, manifest) == receipt
    assert (await db_session.get(Project, (TENANT, "original-project"))).archived_at is None
    assert (await db_session.get(User, "fixture-editor")).role == "member"
    assert all(source.unlinked_at is not None for source in (await db_session.scalars(select(ProductSourceBinding))).all())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption,reason",
    [
        ("no_mode", "evaluation workspace"),
        ("missing_scope", "original B03 run"),
        ("wrong_run", "original B03 run"),
        ("wrong_owner", "dedicated ordinary"),
        ("wrong_editor", "dedicated ordinary"),
        ("admin_editor", "dedicated ordinary"),
        ("foreign_editor", "dedicated ordinary"),
        ("wrong_team", "share the dedicated team"),
        ("foreign_team", "must belong to its fixture tenant"),
        ("foreign_project", "receipt differs|Scope retirement"),
        ("unknown_mode", "Unsupported Product fixture mode"),
    ],
)
async def test_connected_scope_refuses_unowned_actors_and_assets(db_session, monkeypatch, corruption, reason):
    manifest = await connected_scope_fixture(db_session, monkeypatch)
    if corruption == "no_mode":
        manifest.pop("scope_fixture_mode")
    elif corruption == "missing_scope":
        manifest.pop("scope")
    elif corruption == "wrong_run":
        manifest["run_id"] = "another-run"
    elif corruption == "wrong_owner":
        owner = await db_session.get(User, ACTOR)
        owner.cognito_username = "ordinary-owner"
    elif corruption == "wrong_editor":
        editor = await db_session.get(User, "fixture-editor")
        editor.cognito_username = "shared-editor"
    elif corruption == "admin_editor":
        editor = await db_session.get(User, "fixture-editor")
        editor.role = "org_admin"
    elif corruption == "foreign_editor":
        editor = await db_session.get(User, "fixture-editor")
        editor.org_id = "foreign-tenant"
    elif corruption == "wrong_team":
        editor = await db_session.get(User, "fixture-editor")
        editor.team_id = "other-team"
    elif corruption == "foreign_team":
        team = await db_session.get(Team, "fixture-team")
        team.org_id = "foreign-tenant"
    elif corruption == "foreign_project":
        manifest["scope"]["project_id"] = "original-project"
    elif corruption == "unknown_mode":
        manifest["scope_fixture_mode"] = "untrusted"
    await db_session.flush()
    with pytest.raises(FixtureOwnershipError, match=reason):
        await retire_run_product(db_session, manifest)
    await db_session.rollback()
    assert (await db_session.get(Product, (TENANT, PRODUCT))).archived_at is None


@pytest.mark.asyncio
async def test_connected_scope_protected_route_retires_only_original_associations(test_client, db_session, monkeypatch):
    monkeypatch.setenv("ADP_PRODUCT_EVALUATOR_ROLE_ARN", TEST_ROLE)
    monkeypatch.setenv("BG_TRUST_APIGW_HEADERS", "true")
    monkeypatch.setenv("BG_APIGW_PROVENANCE_SECRET", "controlled-edge-proof")
    entry = {
        "agent_id": EVALUATOR_REGISTRY_ID,
        "agent_name": "Product fixture evaluator",
        "scope": "internal",
        "org_id": "__platform__",
        "team_id": "evaluators",
        "role_arn": TEST_ROLE,
        "credential_scopes": [CAPABILITY],
    }
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: SimpleNamespace(get_agent_by_role_arn=lambda role: entry))
    manifest = await connected_scope_fixture(db_session, monkeypatch)
    response = await test_client.post(RETIRE_PATH, json=manifest, headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["fixture_removed"] is True
    assert (await test_client.post(RETIRE_PATH, json=manifest, headers=TEST_HEADERS)).json() == receipt
    assert (await db_session.get(Project, (TENANT, "original-project"))).archived_at is None
    assert (await db_session.get(User, "fixture-editor")).role == "member"
    assert (await db_session.get(Organization, TENANT)).name == "connected-fixture-tenant"
