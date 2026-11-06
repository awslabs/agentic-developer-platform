"""Exercise the registered owning-service route and real IAM authorization chain."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.auth.dependencies import get_current_user
from src.products.evaluation_cleanup_routes import CAPABILITY, EVALUATOR_REGISTRY_ID
from src.products.models import Product, ProductChange, ProductOperation, ProductProjectBinding, Project
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member

PATH = "/internal/v1/product-evaluation/retire"
RUN = "product-42-1"
SOURCE = "a" * 40
ROLE = "arn:aws:iam::123456789012:role/product-evaluator-retirement"
CALLER = "arn:aws:sts::123456789012:assumed-role/product-evaluator-retirement/evaluation"
HEADERS = {"X-Caller-Identity": CALLER, "X-Adp-Edge-Provenance": "controlled-edge-proof"}


@pytest.fixture
def evaluator(monkeypatch):
    monkeypatch.setenv("ADP_PRODUCT_EVALUATOR_ROLE_ARN", ROLE)
    monkeypatch.setenv("BG_TRUST_APIGW_HEADERS", "true")
    monkeypatch.setenv("BG_APIGW_PROVENANCE_SECRET", "controlled-edge-proof")
    entry = {
        "agent_id": EVALUATOR_REGISTRY_ID,
        "agent_name": "Product fixture evaluator",
        "scope": "internal",
        "org_id": "__platform__",
        "team_id": "evaluators",
        "role_arn": ROLE,
        "credential_scopes": [CAPABILITY],
    }
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: SimpleNamespace(get_agent_by_role_arn=lambda role: entry))
    return entry


@pytest.fixture
async def owned(test_client, app, db_session, valid_token_context, evaluator, request):
    owner = valid_token_context
    db_session.add(Organization(id=owner.org_id, name="eval-regression-product-browser"))
    db_session.add(User(id=owner.user_id, org_id=owner.org_id, team_id=owner.team_id, email="owner@example.test", user_kind="human"))
    await add_team_member(db_session, owner)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: owner
    body = {
        "name": f"eval-product-{RUN}" + getattr(request, "param", ""),
        "purpose": "Verify Product workspace",
        "intended_users": ["Maintainers"],
        "starting_point": "idea",
        "project": {"mode": "create"},
    }
    created = await test_client.post("/v1/products", json=body, headers={"Idempotency-Key": "original_product_key_123"})
    assert created.status_code == 201, created.text
    receipt = created.json()
    other = await test_client.post(
        "/v1/products", json={**body, "name": "Unrelated Product"}, headers={"Idempotency-Key": "unrelated_product_key_123"}
    )
    assert other.status_code == 201, other.text
    return (
        {
            "tenant_id": owner.org_id,
            "actor_id": owner.user_id,
            "run_id": RUN,
            "source_revision": SOURCE,
            "idempotency_key": "original_product_key_123",
            "product_id": receipt["product_id"],
            "project_id": receipt["project_id"],
        },
        receipt,
        other.json(),
    )


@pytest.mark.asyncio
async def test_owned_retirement_hides_only_fixture_and_retains_receipts_and_history(test_client, db_session, owned):
    manifest, _, other = owned
    original = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == manifest["product_id"]))
    original_id, snapshot = original.id, deepcopy(original.snapshot)
    operation = await db_session.scalar(select(ProductOperation).where(ProductOperation.product_id == manifest["product_id"]))
    original_receipt = deepcopy(operation.receipt)
    await db_session.rollback()
    response = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt == {**original_receipt, "fixture_removed": True, "run_id": RUN, "cleanup_revision": 2}
    product_path = f"/v1/products/{manifest['product_id']}/projects/{manifest['project_id']}"
    for suffix in ("", "/changes", "/revisions/1"):
        assert (await test_client.get(product_path + suffix)).status_code == 404
    assert [item["id"] for item in (await test_client.get("/v1/products")).json()["items"]] == [other["product_id"]]
    assert (await test_client.get(f"/v1/products/{other['product_id']}/projects/{other['project_id']}")).json() == other
    repeat = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert repeat.status_code == 200 and repeat.json() == receipt
    assert await db_session.scalar(select(func.count()).select_from(Product)) == 2
    assert await db_session.scalar(select(func.count()).select_from(Project)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 3
    assert (await db_session.get(ProductChange, (manifest["tenant_id"], original_id))).snapshot == snapshot
    binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.product_id == manifest["product_id"]))
    assert binding.valid_to is not None
    cleanup = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == manifest["product_id"], ProductChange.revision == 2))
    assert cleanup.changed_paths == ["/fixture_cleanup"] and cleanup.source_revisions == [SOURCE]


@pytest.mark.asyncio
async def test_lost_create_response_recovers_from_original_key(test_client, owned):
    manifest, original, _ = owned
    intent = {key: value for key, value in manifest.items() if key not in {"product_id", "project_id"}}
    first = await test_client.post(PATH, json=intent, headers=HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["product_id"] == original["product_id"]
    repeat = await test_client.post(PATH, json=intent, headers=HEADERS)
    assert repeat.status_code == 200 and repeat.json() == first.json()
