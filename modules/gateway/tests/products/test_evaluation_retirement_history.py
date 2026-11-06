# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Exercise the registered owning-service route and real IAM authorization chain."""

import hashlib
import json
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from src.products.models import Product, ProductChange, ProductOnboardingSession, ProductOperation, ProductProjectBinding, Project
from tests.products.test_evaluation_retirement import HEADERS, PATH
from tests.products.test_evaluation_retirement import evaluator as evaluator
from tests.products.test_evaluation_retirement import owned as owned


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", ("-resume_ui", "-resume_cli", "-progress", "-recovery"), indirect=True)
@pytest.mark.parametrize("stage", ("initial", "selected", "later", "not_applicable"))
async def test_v05_retirement_preserves_onboarding_history(test_client, db_session, owned, stage):
    manifest, _, other = owned
    product_path = f"/v1/products/{manifest['product_id']}/projects/{manifest['project_id']}"
    started = await test_client.post(
        product_path + "/onboarding-sessions",
        json={},
        headers={"Idempotency-Key": "v05_cleanup_start_123", "If-Profile-Revision": "1"},
    )
    assert started.status_code == 201, started.text
    session_path = product_path + "/onboarding-sessions/" + started.json()["id"]
    actions = []
    if stage != "initial":
        actions.append({"action": "save", "configuration": {"financials": {}}})
    if stage in ("later", "not_applicable"):
        actions.append({"action": "later", "reason": "Deferred for later."})
    if stage == "not_applicable":
        actions.append({"action": "not_applicable", "reason": "No financial plan yet"})
    for revision, body in enumerate(actions, start=1):
        saved = await test_client.put(
            session_path + "/steps/financials",
            json=body,
            headers={"Idempotency-Key": f"v05_cleanup_step_{revision}_123", "If-Profile-Revision": str(revision)},
        )
        assert saved.status_code == 200, saved.text
    changes = {
        change.id: deepcopy(change.snapshot)
        for change in await db_session.scalars(select(ProductChange).where(ProductChange.product_id == manifest["product_id"]))
    }
    operation_count = await db_session.scalar(select(func.count()).select_from(ProductOperation))
    await db_session.rollback()
    retired = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert retired.status_code == 200, retired.text
    assert retired.json()["fixture_removed"] is True
    for target in (product_path, product_path + "/changes", product_path + "/revisions/1", session_path):
        assert (await test_client.get(target)).status_code == 404
    assert (await test_client.get(f"/v1/products/{other['product_id']}/projects/{other['project_id']}")).json() == other
    repeated = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert repeated.status_code == 200 and repeated.json() == retired.json()
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == operation_count
    for change_id, snapshot in changes.items():
        assert (await db_session.get(ProductChange, (manifest["tenant_id"], change_id))).snapshot == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", ("-recovery",), indirect=True)
@pytest.mark.parametrize(
    "unsafe",
    (
        "pending_operation",
        "provider_operation",
        "foreign_actor",
        "guided_start",
        "other_step",
        "configured_step",
        "session_proposal",
        "waiting_session",
        "configured_financials",
        "other_applicability",
        "governance",
        "shared_project",
    ),
)
async def test_v05_retirement_refuses_external_or_configured_work(test_client, db_session, owned, unsafe):
    manifest, _, other = owned
    product_path = f"/v1/products/{manifest['product_id']}/projects/{manifest['project_id']}"
    started = await test_client.post(
        product_path + "/onboarding-sessions",
        json={},
        headers={"Idempotency-Key": "v05_refusal_start_123", "If-Profile-Revision": "1"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    saved = await test_client.put(
        product_path + f"/onboarding-sessions/{session_id}/steps/financials",
        json={"action": "save", "configuration": {"financials": {}}},
        headers={"Idempotency-Key": "v05_refusal_step_123", "If-Profile-Revision": "1"},
    )
    assert saved.status_code == 200, saved.text
    operation = await db_session.scalar(
        select(ProductOperation).where(
            ProductOperation.product_id == manifest["product_id"],
            ProductOperation.operation == ("startOnboarding" if unsafe == "guided_start" else "updateStep"),
        )
    )
    session = await db_session.get(ProductOnboardingSession, (manifest["tenant_id"], session_id))
    project = await db_session.get(Project, (manifest["tenant_id"], manifest["project_id"]))
    if unsafe == "pending_operation":
        operation.state = "pending"
    elif unsafe == "provider_operation":
        operation.provider_operation_id = "external-operation"
    elif unsafe == "foreign_actor":
        operation.actor_id = "another-actor"
    elif unsafe in ("guided_start", "other_step", "configured_step"):
        changed = deepcopy(operation.request)
        if unsafe == "guided_start":
            changed["request"]["mode"] = "guided"
        elif unsafe == "other_step":
            changed["step_id"] = "advanced"
        else:
            changed["request"]["configuration"]["financials"] = {"release_target_date": "2030-01-01"}
        operation.request = changed
        operation.request_hash = hashlib.sha256(json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    elif unsafe == "session_proposal":
        session.proposals = [{"id": "unresolved-proposal"}]
    elif unsafe == "waiting_session":
        session.state = "waiting_external"
    elif unsafe == "configured_financials":
        project.configuration = {"financials": {"budget_policy_ref": {"record_id": "shared-budget"}}}
    elif unsafe == "other_applicability":
        project.configuration = {"applicability": [{"check_id": "advanced", "state": "later", "reason": "Deferred"}]}
    elif unsafe == "governance":
        project.configuration = {"governance": {"execution_policy_ref": {"record_id": "shared-execution"}}}
    else:
        binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.product_id == manifest["product_id"]))
        db_session.add(
            ProductProjectBinding(
                tenant_id=manifest["tenant_id"],
                product_id=other["product_id"],
                project_id=manifest["project_id"],
                valid_from=binding.valid_from - timedelta(days=2),
                valid_to=binding.valid_from - timedelta(days=1),
                provenance={"actor_id": manifest["actor_id"], "source": "direct_input"},
            )
        )
    await db_session.commit()
    response = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert response.status_code == 409, response.text
    assert (await test_client.get(product_path)).status_code == 200
    assert (await test_client.get(f"/v1/products/{other['product_id']}/projects/{other['project_id']}")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", ("-unreviewed",), indirect=True)
async def test_v05_retirement_refuses_unreviewed_fixture_name(test_client, owned):
    manifest, _, _ = owned
    response = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert response.status_code == 409, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("actions", [("edit",), ("archive",), ("archive", "restore")])
async def test_owned_retirement_accepts_product_wide_history(test_client, db_session, owned, actions):
    manifest, _, other = owned
    product_path = f"/v1/products/{manifest['product_id']}/projects/{manifest['project_id']}"
    for revision, action in enumerate(actions, start=1):
        headers = {"Idempotency-Key": f"owned_fixture_{action}_{revision}", "If-Profile-Revision": str(revision)}
        if action == "edit":
            response = await test_client.patch(product_path, json={"purpose": "Edited in browser"}, headers=headers)
        else:
            response = await test_client.post(product_path + "/lifecycle", json={"action": action, "reason": "Owned evaluation"}, headers=headers)
        assert response.status_code == 200, response.text
    changes = (
        await db_session.scalars(
            select(ProductChange).where(
                ProductChange.product_id == manifest["product_id"],
                ProductChange.revision > 1,
            )
        )
    ).all()
    assert len(changes) == len(actions) and all(change.project_id is None for change in changes)
    snapshots = {change.id: deepcopy(change.snapshot) for change in changes}
    await db_session.rollback()
    response = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["cleanup_revision"] == len(actions) + 2
    repeat = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert repeat.status_code == 200 and repeat.json() == response.json()
    for suffix in ("", "/changes", "/revisions/1", "/revisions/2"):
        assert (await test_client.get(product_path + suffix)).status_code == 404
    for change_id, snapshot in snapshots.items():
        assert (await db_session.get(ProductChange, (manifest["tenant_id"], change_id))).snapshot == snapshot
    assert (await test_client.get(f"/v1/products/{other['product_id']}/projects/{other['project_id']}")).json() == other


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["actor_id", "project_id", "operation_id"])
async def test_product_wide_history_still_requires_owned_operation(test_client, db_session, owned, field):
    manifest, _, other = owned
    product_path = f"/v1/products/{manifest['product_id']}/projects/{manifest['project_id']}"
    edited = await test_client.patch(
        product_path,
        json={"purpose": "Edited in browser"},
        headers={
            "Idempotency-Key": "owned_fixture_edit_1",
            "If-Profile-Revision": "1",
        },
    )
    assert edited.status_code == 200, edited.text
    change = await db_session.scalar(
        select(ProductChange).where(
            ProductChange.product_id == manifest["product_id"],
            ProductChange.revision == 2,
        )
    )
    assert change.project_id is None
    foreign_operation = await db_session.scalar(select(ProductOperation).where(ProductOperation.product_id == other["product_id"]))
    values = {"actor_id": "another-person", "project_id": other["project_id"], "operation_id": foreign_operation.id}
    setattr(change, field, values[field])
    await db_session.commit()
    response = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert response.status_code == 409, response.text
    assert (await db_session.scalar(select(Product).where(Product.id == manifest["product_id"]))).archived_at is None
