"""Financial planning is a Project-scoped proposal, not a funding authorization."""

import asyncio
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.products.models import Product, ProductChange, ProductOperation, Project
from tests.products import test_i07_api
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i04 import GatewayClient
from tests.products.test_i07_api import mutation_headers

member = test_i07_api.member

SCRIPT = Path(__file__).parents[2] / "cli/adp-onboard.py"
spec = importlib.util.spec_from_file_location("adp_onboard_i14", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.mark.asyncio
async def financial_roundtrip(test_client, db_session, test_engine, member, tmp_path, monkeypatch, record_property):
    clock = datetime(2026, 10, 20, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Financial planning must not call a provider"))
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 1))
    target = {
        "tenant_id": member.org_id,
        "project_id": project_id,
        "source_connection_id": "adp.profile",
        "record_type": "product",
        "record_id": product_id,
        "record_revision": change.id,
    }
    allocation = {
        "id": "allocation-1",
        "name": "Compute plan",
        "target_ref": target,
        "amount": {"amount": "75.00", "currency": "USD"},
        "category": "cloud",
        "period_start": "2026-10-01",
        "period_end": "2026-12-31",
        "approval_status": "proposed",
        "owner_ref": {"kind": "user", "id": member.user_id},
    }
    root = f"/v1/products/{product_id}/projects/{project_id}"
    started = await test_client.post(f"{root}/onboarding-sessions", json={}, headers=mutation_headers(1, "financial_start_12345"))
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    payload = {"action": "save", "configuration": {"financials": {"allocations": [allocation], "release_target_date": "2026-12-01"}}}
    source = tmp_path / "financials.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    result = await asyncio.to_thread(
        cli.execute,
        cli.parser().parse_args(
            [
                "step",
                "--product",
                product_id,
                "--project",
                project_id,
                "--session",
                session_id,
                "--section",
                "financials",
                "--input",
                str(source),
                "--revision",
                "1",
                "--idempotency-key",
                "financial_save_12345",
                "--json",
            ]
        ),
        gateway,
    )
    assert result["detail"]["revision"] == 2
    readback = await test_client.get(root)
    assert readback.status_code == 200, readback.text
    assert readback.json()["configuration"]["financials"] == payload["configuration"]["financials"]
    assert readback.json()["configuration"].get("budget_policy_ref") is None
    assert (await test_client.get(f"{root}/onboarding-sessions/{session_id}")).json() == result["detail"]
    db_session.expire_all()
    project = await db_session.get(Project, (member.org_id, project_id))
    assert project.configuration["financials"]["allocations"][0]["amount"] == {"amount": "75.00", "currency": "USD"}
    assert project.budget_policy_ref is None
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as independent:
        stored = await independent.get(Project, (member.org_id, project_id))
        change = await independent.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 2))
        operations = (await independent.scalars(select(ProductOperation).where(ProductOperation.product_id == product_id))).all()
        assert stored.configuration["financials"] == payload["configuration"]["financials"]
        assert stored.budget_policy_ref is None
        assert change.snapshot["configuration"]["financials"]["allocations"][0]["amount"] == {"amount": "75.00", "currency": "USD"}
        assert change.relationship_versions == []
        assert len(operations) == 3
    record_property("scenario_id", "SCN-01")
    record_property("criterion", "AC-01")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"], text=True).strip(),
    )
    record_property("observation", "CLI step, independent scoped Profile read, fresh SQLAlchemy session")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")


@pytest.mark.asyncio
async def financial_validation(test_client, db_session, test_engine, member, monkeypatch, record_property):
    clock = datetime(2026, 10, 20, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Validation cannot create an Initiative or Release"))
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    creation = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 1))
    target = {
        "tenant_id": member.org_id,
        "project_id": project_id,
        "source_connection_id": "adp.profile",
        "record_type": "product",
        "record_id": product_id,
        "record_revision": creation.id,
    }
    allocation = {
        "id": "planned-spend",
        "name": "Future infrastructure",
        "target_ref": target,
        "amount": {"amount": "75.00", "currency": "USD"},
        "category": "cloud",
        "period_start": "2026-10-01",
        "period_end": "2026-12-31",
        "approval_status": "proposed",
        "owner_ref": {"kind": "user", "id": member.user_id},
    }
    initiative = {**target, "source_connection_id": "external.catalog", "record_type": "initiative", "record_id": "existing-initiative"}
    root = f"/v1/products/{product_id}/projects/{project_id}"
    started = await test_client.post(f"{root}/onboarding-sessions", json={}, headers=mutation_headers(1, "financial_validation_start"))
    assert started.status_code == 201, started.text
    session = f"{root}/onboarding-sessions/{started.json()['id']}"
    valid = await test_client.put(
        f"{session}/steps/financials",
        json={"action": "save", "configuration": {"financials": {"allocations": [allocation]}}},
        headers=mutation_headers(1, "financial_validation_valid"),
    )
    assert valid.status_code == 200, valid.text
    initial = (await test_client.get(root)).json()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as independent:
        initial_operations = await independent.scalar(
            select(func.count()).select_from(ProductOperation).where(ProductOperation.product_id == product_id)
        )
        initial_changes = await independent.scalar(select(func.count()).select_from(ProductChange).where(ProductChange.product_id == product_id))
    invalid = [
        ({"allocations": [{**allocation, "amount": {"amount": "7.1234567", "currency": "USD"}}]}, 422),
        ({"allocations": [{**allocation, "amount": {"amount": 75, "currency": "USD"}}]}, 422),
        ({"allocations": [{**allocation, "period_start": "2026-02-30"}]}, 422),
        ({"allocations": [{**allocation, "period_start": "2027-01-01"}]}, 422),
        ({"allocations": [{**allocation, "approval_status": "approved"}]}, 422),
        ({"allocations": [{**allocation, "target_ref": {**target, "project_id": "foreign-project"}}]}, 404),
        ({"allocations": [{**allocation, "target_ref": {**target, "record_revision": "not-a-version"}}]}, 422),
        ({"allocations": [allocation], "initiatives": [{**initiative, "project_id": "foreign-project"}]}, 404),
        ({"allocations": [allocation], "initiatives": [initiative]}, 422),
        ({"allocations": [allocation], "release_target_date": "2026-02-30"}, 422),
    ]
    for index, (financials, direct_status) in enumerate(invalid):
        payload = {"action": "save", "configuration": {"financials": financials}}
        headers = mutation_headers(2, f"financial_validation_error_{index:02d}")
        step = await test_client.put(f"{session}/steps/financials", json=payload, headers=headers)
        assert step.status_code == 422, (index, step.text)
        direct = await test_client.patch(f"{root}/configuration", json={"configuration": {"financials": financials}}, headers=headers)
        assert direct.status_code == direct_status, (index, direct.text)
        if direct_status == 404:
            assert direct.json() == {"detail": {"code": "not_found"}}
            assert "foreign-project" not in step.text
        assert (await test_client.get(root)).json() == initial
    async with factory() as independent:
        stored = await independent.get(Project, (member.org_id, project_id))
        product = await independent.get(Product, (member.org_id, product_id))
        assert stored.configuration == initial["configuration"]
        assert stored.provenance == initial["provenance"]
        assert product.revision == initial["revision"] == 2
        assert (
            await independent.scalar(select(func.count()).select_from(ProductOperation).where(ProductOperation.product_id == product_id))
            == initial_operations
        )
        assert (
            await independent.scalar(select(func.count()).select_from(ProductChange).where(ProductChange.product_id == product_id)) == initial_changes
        )
    db_session.expire_all()
    project = await db_session.get(Project, (member.org_id, project_id))
    project.provenance = [
        {
            "state": "accepted",
            "method": "connector",
            "paths": ["/configuration/financials/initiatives"],
            "source_refs": [initiative],
            "observed_at": "2026-10-19T00:00:00Z",
            "authority_mapping_ref": {**initiative, "record_type": "authority_mapping", "record_id": "catalog-editor", "record_revision": "1"},
            "stale_after": "2099-01-01T00:00:00Z",
        }
    ]
    await db_session.commit()
    project.provenance = [{**project.provenance[0], "observed_at": "2099-01-01T00:00:00Z"}]
    await db_session.commit()
    premature = await test_client.put(
        f"{session}/steps/financials",
        json={"action": "save", "configuration": {"financials": {"allocations": [allocation], "initiatives": [initiative]}}},
        headers=mutation_headers(2, "financial_future_proof_denied"),
    )
    assert premature.status_code == 422
    assert (await test_client.get(root)).json()["configuration"] == initial["configuration"]
    project.provenance = [{**project.provenance[0], "observed_at": "2026-10-19T00:00:00Z"}]
    await db_session.commit()
    allowed = await test_client.put(
        f"{session}/steps/financials",
        json={
            "action": "save",
            "configuration": {"financials": {"allocations": [allocation], "initiatives": [initiative], "release_target_date": "2026-12-01"}},
        },
        headers=mutation_headers(2, "financial_validation_allowed"),
    )
    assert allowed.status_code == 200, allowed.text
    observed = (await test_client.get(root)).json()
    assert observed["configuration"]["financials"]["initiatives"] == [initiative]
    assert observed["configuration"]["financials"]["release_target_date"] == "2026-12-01"
    assert observed["configuration"]["financials"]["allocations"][0]["approval_status"] == "proposed"
    edited = await test_client.put(
        f"{session}/steps/financials",
        json={
            "action": "save",
            "configuration": {"financials": {"allocations": [allocation], "initiatives": [initiative], "release_target_date": "2027-01-01"}},
        },
        headers=mutation_headers(3, "financial_validation_reedit"),
    )
    assert edited.status_code == 200, edited.text
    assert (await test_client.get(root)).json()["configuration"]["financials"]["initiatives"] == [initiative]
    async with factory() as independent:
        stored = await independent.get(Project, (member.org_id, project_id))
        assert stored.configuration["financials"]["allocations"][0]["approval_status"] == "proposed"
        assert stored.configuration["financials"]["release_target_date"] == "2027-01-01"
        assert stored.budget_policy_ref is None
        changes = (await independent.scalars(select(ProductChange).where(ProductChange.product_id == product_id))).all()
        assert len(changes) == initial_changes + 2
        assert all(change.relationship_versions == [] for change in changes[1:])
    record_property("scenario_id", "SCN-02")
    record_property("criterion", "AC-02")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"], text=True).strip(),
    )
    record_property("observation", "rejected CLI/API-compatible payloads, unchanged scoped Profile and fresh SQLAlchemy session")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")


@pytest.mark.asyncio
async def financial_scope_provenance(test_client, db_session, test_engine, member, monkeypatch, record_property):
    clock = datetime(2026, 10, 20, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Scope changes must not call a provider"))
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, first_id = created.json()["product_id"], created.json()["project_id"]
    second_id = "financial-second-project"
    db_session.add(Project(id=second_id, tenant_id=member.org_id, name="Second plan", owner_user_id=member.user_id))
    await db_session.commit()
    bound = await test_client.post(
        f"/v1/products/{product_id}/projects", json={"project_id": second_id}, headers=mutation_headers(1, "financial_bind_second_123")
    )
    assert bound.status_code == 200, bound.text
    root = f"/v1/products/{product_id}/projects"
    first, second = f"{root}/{first_id}", f"{root}/{second_id}"
    creation = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 1))

    def plan(project_id, amount):
        return {
            "allocations": [
                {
                    "id": "allocation",
                    "name": "Project plan",
                    "target_ref": {
                        "tenant_id": member.org_id,
                        "project_id": project_id,
                        "source_connection_id": "adp.profile",
                        "record_type": "product",
                        "record_id": product_id,
                        "record_revision": creation.id,
                    },
                    "amount": {"amount": amount, "currency": "USD"},
                    "category": "ai",
                    "period_start": "2026-10-01",
                    "period_end": "2026-12-31",
                    "approval_status": "proposed",
                    "owner_ref": {"kind": "user", "id": member.user_id},
                }
            ],
            "release_target_date": "2026-12-01",
        }

    started = await test_client.post(f"{first}/onboarding-sessions", json={}, headers=mutation_headers(2, "financial_scope_start_123"))
    assert started.status_code == 201, started.text
    session = f"{first}/onboarding-sessions/{started.json()['id']}"
    first_plan, second_plan = plan(first_id, "75.00"), plan(second_id, "120.00")
    second_binding = await db_session.scalar(
        select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.project_id == second_id)
    )
    second_plan["allocations"][0]["target_ref"]["record_revision"] = second_binding.id
    saved = await test_client.put(
        f"{session}/steps/financials",
        json={"action": "save", "configuration": {"financials": first_plan}},
        headers=mutation_headers(2, "financial_scope_first_123"),
    )
    assert saved.status_code == 200, saved.text
    second_saved = await test_client.patch(
        f"{second}/configuration", json={"configuration": {"financials": second_plan}}, headers=mutation_headers(3, "financial_scope_second_123")
    )
    assert second_saved.status_code == 200, second_saved.text
    original = (await test_client.get(first)).json()
    assert original["configuration"]["financials"] == first_plan
    assert (await test_client.get(second)).json()["configuration"]["financials"] == second_plan
    db_session.expire_all()
    project = await db_session.get(Project, (member.org_id, first_id))
    project.provenance = [
        {
            "paths": ["/configuration/financials/allocations", "/configuration/financials/release_target_date"],
            "state": "accepted",
            "method": "connector",
            "source_refs": [first_plan["allocations"][0]["target_ref"]],
            "observed_at": "2026-10-19T00:00:00Z",
            "stale_after": "2099-01-01T00:00:00Z",
            "authority_mapping_ref": {
                **first_plan["allocations"][0]["target_ref"],
                "record_type": "authority_mapping",
                "record_id": "fixture-editor",
                "record_revision": "1",
            },
        }
    ]
    await db_session.commit()
    edited_date = await test_client.patch(
        f"{first}/configuration",
        json={"configuration": {"financials": {**first_plan, "release_target_date": "2027-01-01"}}},
        headers=mutation_headers(4, "financial_scope_date_12345"),
    )
    assert edited_date.status_code == 200, edited_date.text
    assert edited_date.json()["configuration"]["financials"]["allocations"] == first_plan["allocations"]
    assert edited_date.json()["configuration"]["financials"]["release_target_date"] == "2027-01-01"
    provenance = edited_date.json()["provenance"]
    assert next(proof for proof in provenance if "/configuration/financials/allocations" in proof["paths"])["method"] == "connector"
    assert next(proof for proof in provenance if "/configuration/financials/release_target_date" in proof["paths"])["method"] == "manual"
    edited_plan = {
        **first_plan,
        "release_target_date": "2027-01-01",
        "allocations": [{**first_plan["allocations"][0], "amount": {"amount": "80.00", "currency": "USD"}}],
    }
    edited = await test_client.put(
        f"{session}/steps/financials",
        json={"action": "save", "configuration": {"financials": edited_plan}},
        headers=mutation_headers(5, "financial_scope_amount_123"),
    )
    assert edited.status_code == 200, edited.text
    observed = (await test_client.get(first)).json()
    assert observed["configuration"]["financials"] == edited_plan
    assert {path: proof["method"] for proof in observed["provenance"] for path in proof["paths"]} == {
        "/configuration/financials/allocations": "manual",
        "/configuration/financials/release_target_date": "manual",
    }
    financial_provenance = observed["provenance"]
    assert (await test_client.get(f"{first}/changes")).json()["items"][-1]["project_id"] == first_id
    for action in ("later", "not_applicable"):
        revision = observed["revision"]
        change = await test_client.put(
            f"{session}/steps/financials",
            json={"action": action, "reason": "Planning deferred"},
            headers=mutation_headers(revision, f"financial_scope_{action}_123"),
        )
        assert change.status_code == 200, change.text
        observed = (await test_client.get(first)).json()
        assert observed["configuration"]["financials"] == edited_plan
        assert observed["configuration"]["applicability"] == [{"check_id": "financials", "state": action, "reason": "Planning deferred"}]
        assert observed["provenance"] == financial_provenance
        assert next(check for check in observed["progress"]["checks"] if check["id"] == "financials")["status"] == action
    mandatory = await test_client.put(
        f"{session}/steps/information",
        json={"action": "not_applicable", "reason": "Not needed"},
        headers=mutation_headers(observed["revision"], "financial_scope_required_123"),
    )
    assert mandatory.status_code == 422
    assert (await test_client.get(first)).json() == observed
    other = (await test_client.get(second)).json()
    assert other["configuration"]["financials"] == second_plan
    assert "80.00" not in json.dumps(other)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as independent:
        first_stored = await independent.get(Project, (member.org_id, first_id))
        second_stored = await independent.get(Project, (member.org_id, second_id))
        assert first_stored.configuration["financials"] == edited_plan
        assert second_stored.configuration["financials"] == second_plan
        assert first_stored.provenance == financial_provenance
        assert second_stored.provenance != first_stored.provenance
        changes = (await independent.scalars(select(ProductChange).where(ProductChange.product_id == product_id))).all()
        assert all(change.project_id in (first_id, second_id) for change in changes)
        assert any(change.project_id == first_id and change.snapshot.get("configuration", {}).get("financials") == edited_plan for change in changes)
        assert any(change.project_id == second_id and change.snapshot.get("configuration", {}).get("financials") == second_plan for change in changes)
    record_property("scenario_id", "SCN-03")
    record_property("criterion", "AC-03")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"], text=True).strip(),
    )
    record_property("observation", "two scoped Profile reads, immutable change snapshots and fresh SQLite session")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")
