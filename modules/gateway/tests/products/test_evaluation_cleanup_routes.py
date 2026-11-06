"""Guard the protected evaluator caller and the run-owned retirement transaction."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.internal.auth_deps import verify_internal_or_irsa
from src.products.evaluation_cleanup_routes import BROWSER_REGISTRY_ID, CAPABILITY, EVALUATOR_REGISTRY_ID, RECOVERY_REGISTRY_ID, router
from src.products.models import Product, ProductChange, ProductProjectBinding
from src.shared.database import get_db
from tests.products.test_evaluation_cleanup import PRODUCT, fixture

ROLES = {
    EVALUATOR_REGISTRY_ID: ("ADP_PRODUCT_EVALUATOR_ROLE_ARN", "evaluator-instance"),
    RECOVERY_REGISTRY_ID: ("ADP_PRODUCT_RECOVERY_ROLE_ARN", "evaluator-recovery"),
    BROWSER_REGISTRY_ID: ("ADP_PRODUCT_BROWSER_RETIREMENT_ROLE_ARN", "browser-retirement"),
}


@pytest.fixture
async def context(db_session, monkeypatch):
    from src.products import evaluation_cleanup_routes

    for setting, name in ROLES.values():
        monkeypatch.setenv(setting, "arn:aws:iam::000000000000:role/" + name)
    app = FastAPI()
    app.include_router(router)
    principal = SimpleNamespace(
        auth_source="iam",
        user_id="evaluator-instance",
        scope="internal",
        credential_scopes=[CAPABILITY],
        agent_registry_id=EVALUATOR_REGISTRY_ID,
    )
    monkeypatch.setattr(
        evaluation_cleanup_routes,
        "verified_caller_identity",
        lambda _: "arn:aws:sts::000000000000:assumed-role/" + ROLES.get(principal.agent_registry_id, ("", "unregistered"))[1] + "/test",
    )

    async def authenticated(request: Request):
        request.state.token_context = principal

    app.dependency_overrides[verify_internal_or_irsa] = authenticated
    app.dependency_overrides[get_db] = lambda: db_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, principal, db_session


@pytest.mark.asyncio
@pytest.mark.parametrize("registry_id", list(ROLES))
async def test_registered_evaluator_retires_owned_product_and_preserves_history(context, registry_id):
    client, principal, db = context
    principal.agent_registry_id = registry_id
    manifest = await fixture(db)
    url = "/internal/v1/product-evaluation/retire"
    response = await client.post(url, json=manifest)
    assert response.status_code == 200, response.text
    assert response.json()["fixture_removed"] is True
    assert (await client.post(url, json=manifest)).json() == response.json()
    product = await db.scalar(select(Product).where(Product.id == PRODUCT))
    assert product.archived_at is not None
    assert len((await db.scalars(select(ProductChange).where(ProductChange.product_id == PRODUCT))).all()) == 2
    assert (await db.scalars(select(ProductProjectBinding).where(ProductProjectBinding.product_id == PRODUCT))).one().valid_to is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"agent_registry_id": "another-evaluator"},
        {"credential_scopes": []},
        {"scope": "shared"},
        {"auth_source": "jwt"},
    ],
)
async def test_non_evaluator_cannot_retire_fixture(context, override):
    client, principal, db = context
    manifest = await fixture(db)
    for field, value in override.items():
        setattr(principal, field, value)
    response = await client.post("/internal/v1/product-evaluation/retire", json=manifest)
    assert response.status_code == 403
    assert (await db.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("registry_id", list(ROLES))
@pytest.mark.parametrize(
    "change",
    [
        {"actor_id": "different-actor"},
        {"idempotency_key": "different-create-key"},
        {"product_id": "different-product"},
        {"source_revision": "not-pinned"},
    ],
)
async def test_unowned_manifest_does_not_mutate(context, change, registry_id):
    client, principal, db = context
    principal.agent_registry_id = registry_id
    manifest = await fixture(db)
    manifest.update(change)
    response = await client.post("/internal/v1/product-evaluation/retire", json=manifest)
    assert response.status_code == 409
    assert response.json() == {"detail": "Product fixture ownership could not be verified"}
    assert (await db.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.asyncio
async def test_extra_manifest_fields_are_rejected(context):
    client, _, db = context
    manifest = await fixture(db)
    response = await client.post("/internal/v1/product-evaluation/retire", json={**manifest, "owner_token": "never-forward"})
    assert response.status_code == 422
    assert (await db.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.parametrize("registry_id", list(ROLES))
@pytest.mark.parametrize("caller", [None, "arn:aws:sts::000000000000:assumed-role/wrong-role/test"])
async def test_retirement_requires_verified_matching_role(context, monkeypatch, registry_id, caller):
    from src.products import evaluation_cleanup_routes

    client, principal, db = context
    principal.agent_registry_id = registry_id
    manifest = await fixture(db)
    monkeypatch.setattr(evaluation_cleanup_routes, "verified_caller_identity", lambda _: caller)
    assert (await client.post("/internal/v1/product-evaluation/retire", json=manifest)).status_code == 403
    assert (await db.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.parametrize("registry_id", list(ROLES))
@pytest.mark.parametrize("account,role_name", [("123", "evaluator"), ("000000000000", "invalid role")])
async def test_matching_malformed_role_configuration_cannot_retire(context, monkeypatch, registry_id, account, role_name):
    from src.products import evaluation_cleanup_routes

    client, principal, db = context
    principal.agent_registry_id = registry_id
    manifest = await fixture(db)
    setting = ROLES[registry_id][0]
    monkeypatch.setenv(setting, f"arn:aws:iam::{account}:role/{role_name}")
    caller = f"arn:aws:sts::{account}:assumed-role/{role_name}/test"
    monkeypatch.setattr(evaluation_cleanup_routes, "verified_caller_identity", lambda _: caller)
    response = await client.post("/internal/v1/product-evaluation/retire", json=manifest)
    assert response.status_code == 403
    assert response.json() == {"detail": "configured evaluator role required"}
    assert (await db.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.parametrize("registry_id", list(ROLES))
async def test_missing_role_configuration_refuses_retirement(context, monkeypatch, registry_id):
    client, principal, db = context
    principal.agent_registry_id = registry_id
    manifest = await fixture(db)
    monkeypatch.delenv(ROLES[registry_id][0])
    assert (await client.post("/internal/v1/product-evaluation/retire", json=manifest)).status_code == 403
    assert (await db.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None


@pytest.mark.parametrize(
    "registry_id,caller_role",
    [
        (registry_id, role)
        for registry_id, (_, selected) in ROLES.items()
        for role in ["browser-checks", *[name for _, name in ROLES.values()]]
        if role != selected
    ],
)
async def test_other_evaluator_or_browser_checks_role_cannot_borrow_identity(context, monkeypatch, registry_id, caller_role):
    from src.products import evaluation_cleanup_routes

    client, principal, db = context
    principal.agent_registry_id = registry_id
    manifest = await fixture(db)
    monkeypatch.setattr(
        evaluation_cleanup_routes, "verified_caller_identity", lambda _: "arn:aws:sts::000000000000:assumed-role/" + caller_role + "/test"
    )
    assert (await client.post("/internal/v1/product-evaluation/retire", json=manifest)).status_code == 403
    assert (await db.scalar(select(Product).where(Product.id == PRODUCT))).archived_at is None
