# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Exercise the registered owning-service route and real IAM authorization chain."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from src.products.evaluation_cleanup_routes import BROWSER_REGISTRY_ID, EVALUATOR_REGISTRY_ID, RECOVERY_REGISTRY_ID
from src.products.models import Product, ProductChange, ProductProjectBinding, Project
from src.shared.models.organization import Team
from tests.products.test_evaluation_retirement import CALLER, HEADERS, PATH
from tests.products.test_evaluation_retirement import evaluator as evaluator
from tests.products.test_evaluation_retirement import owned as owned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registry_id,setting,role_name",
    [
        (RECOVERY_REGISTRY_ID, "ADP_PRODUCT_RECOVERY_ROLE_ARN", "product-recovery-retirement"),
        (BROWSER_REGISTRY_ID, "ADP_PRODUCT_BROWSER_RETIREMENT_ROLE_ARN", "product-browser-retirement"),
    ],
)
@pytest.mark.parametrize("failure", [None, "evaluator-role", "evaluator-registry", "wrong-account", "forged-edge", "unconfigured"])
async def test_separate_retirement_requires_its_exact_protected_identity(
    test_client,
    db_session,
    owned,
    evaluator,
    monkeypatch,
    failure,
    registry_id,
    setting,
    role_name,
):
    manifest, _, _ = owned
    selected_role = "arn:aws:iam::123456789012:role/" + role_name
    monkeypatch.setenv(setting, selected_role)
    evaluator.update(agent_id=registry_id, role_arn=selected_role)
    headers = {**HEADERS, "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/" + role_name + "/retirement"}
    if failure == "evaluator-role":
        headers["X-Caller-Identity"] = CALLER
    elif failure == "evaluator-registry":
        evaluator["agent_id"] = EVALUATOR_REGISTRY_ID
    elif failure == "wrong-account":
        headers["X-Caller-Identity"] = headers["X-Caller-Identity"].replace("123456789012", "000000000000")
    elif failure == "forged-edge":
        headers["X-Adp-Edge-Provenance"] = "forged-proof"
    elif failure == "unconfigured":
        monkeypatch.delenv(setting)
    intent = {key: value for key, value in manifest.items() if key not in {"product_id", "project_id"}}
    response = await test_client.post(PATH, json=intent, headers=headers)
    if failure is not None:
        assert response.status_code == 403, response.text
        assert (await db_session.scalar(select(Product).where(Product.id == manifest["product_id"]))).archived_at is None
    else:
        assert response.status_code == 200, response.text
        assert response.json()["product_id"] == manifest["product_id"] and response.json()["fixture_removed"] is True
        repeat = await test_client.post(PATH, json=intent, headers=headers)
        assert repeat.status_code == 200 and repeat.json() == response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"actor_id": "other-person"},
        {"tenant_id": "other-tenant"},
        {"run_id": "product-43-1"},
        {"idempotency_key": "other_product_key_123"},
        {"product_id": "other-product"},
        {"project_id": "other-project"},
    ],
)
async def test_wrong_owner_or_original_intent_cannot_retire(test_client, db_session, owned, changed):
    manifest, _, _ = owned
    response = await test_client.post(PATH, json={**manifest, **changed}, headers=HEADERS)
    assert response.status_code == 409, response.text
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2
    assert (await db_session.scalar(select(Product).where(Product.id == manifest["product_id"]))).archived_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["configured", "reassigned-team", "cleared-team", "rebound", "foreign-history"])
async def test_shared_or_configured_fixture_is_not_retired(test_client, db_session, owned, kind):
    manifest, _, other = owned
    if kind == "configured":
        project = await db_session.scalar(select(Project).where(Project.id == manifest["project_id"]))
        project.configuration = {"provider": "shared"}
    elif kind in {"reassigned-team", "cleared-team"}:
        product = await db_session.get(Product, (manifest["tenant_id"], manifest["product_id"]))
        original = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product.id, ProductChange.revision == 1))
        assert product.team_id == original.snapshot["product"]["team_id"]
        if kind == "reassigned-team":
            db_session.add(Team(id="different-team", org_id=manifest["tenant_id"], department_id="fixture-department", name="Another team"))
        product.team_id = "different-team" if kind == "reassigned-team" else None
    elif kind == "rebound":
        binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.product_id == manifest["product_id"]))
        binding.valid_to = binding.valid_from + timedelta(seconds=1)
        await db_session.flush()
        db_session.add(
            ProductProjectBinding(
                tenant_id=manifest["tenant_id"],
                product_id=other["product_id"],
                project_id=manifest["project_id"],
                valid_from=binding.valid_to + timedelta(seconds=1),
                provenance={"actor_id": manifest["actor_id"], "source": "direct_input"},
            )
        )
    else:
        change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == manifest["product_id"]))
        change.actor_id = "another-person"
    await db_session.commit()
    response = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert response.status_code == 409, response.text
    assert (await db_session.scalar(select(Product).where(Product.id == manifest["product_id"]))).archived_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "no-iam",
        "forged-edge",
        "wrong-scope",
        "missing-capability",
        "wrong-registry-id",
        "wrong-role",
        "wrong-account",
        "unconfigured",
    ],
)
async def test_real_authorization_chain_refuses_unentitled_cleanup(test_client, db_session, owned, evaluator, monkeypatch, failure):
    manifest, _, _ = owned
    headers = dict(HEADERS)
    if failure == "no-iam":
        headers = {"Authorization": "Bearer ordinary-token"}
    elif failure == "forged-edge":
        headers["X-Adp-Edge-Provenance"] = "forged-proof"
    elif failure == "wrong-scope":
        evaluator["scope"] = "tenant"
    elif failure == "missing-capability":
        evaluator["credential_scopes"] = []
    elif failure == "wrong-registry-id":
        evaluator["agent_id"] = "another-evaluator"
    elif failure == "wrong-role":
        headers["X-Caller-Identity"] = CALLER.replace("product-evaluator-retirement", "other-role")
    elif failure == "wrong-account":
        headers["X-Caller-Identity"] = CALLER.replace("123456789012", "000000000000")
    else:
        monkeypatch.delenv("ADP_PRODUCT_EVALUATOR_ROLE_ARN")
    response = await test_client.post(PATH, json=manifest, headers=headers)
    assert response.status_code == 403, response.text
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2
    assert (await db_session.scalar(select(Product).where(Product.id == manifest["product_id"]))).archived_at is None


@pytest.mark.asyncio
async def test_protected_route_refuses_connected_mode_without_owned_scope(test_client, owned):
    manifest, _, _ = owned
    response = await test_client.post(PATH, json={**manifest, "scope_fixture_mode": "connected_scope"}, headers=HEADERS)
    assert response.status_code == 409
    assert "ownership" in response.json()["detail"].lower()
    invalid = await test_client.post(PATH, json={**manifest, "scope_fixture_mode": "unsupported"}, headers=HEADERS)
    assert invalid.status_code == 422
