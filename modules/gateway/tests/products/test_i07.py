"""I07 acceptance selectors with independent scoped database observation."""

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app import create_app
from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductOnboardingSession, ProductOperation, ProductProjectBinding, Project
from src.shared.database import get_db
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i04 import GatewayClient
from tests.products.test_i07_cli import cli
from tests.products.test_i07_state import proof


@pytest.fixture
async def member(db_session, app, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Run-owned workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="member@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    return valid_token_context


@pytest.mark.asyncio
async def session_minimal_resume(test_client, test_engine, member, monkeypatch, record_property):
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Minimal onboarding cannot depend on a model or source"))
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    ui = await test_client.post(
        root,
        json={},
        headers={
            "If-Profile-Revision": "1",
            "Idempotency-Key": "run_owned_ui_session_key_01",
        },
    )
    assert ui.status_code == 201, ui.text
    session_id = ui.json()["id"]
    assert ui.headers["Profile-Revision"] == "1"
    ui_read = await test_client.get(f"{root}/{session_id}")
    assert ui_read.status_code == 200 and ui_read.json() == ui.json()
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    scope = ("--product", product_id, "--project", project_id)

    async def invoke(*arguments):
        return await asyncio.to_thread(cli.execute, cli.parser().parse_args(arguments), gateway)

    cli_resume = (
        await invoke(
            "resume",
            *scope,
            "--revision",
            "1",
            "--idempotency-key",
            "run_owned_cli_resume_01",
            "--json",
        )
    )["detail"]
    assert cli_resume == ui_read.json()
    cli_read = (await invoke("resume", *scope, "--session", session_id, "--json"))["detail"]
    assert cli_read == ui_read.json()
    assert (
        await invoke(
            "resume",
            *scope,
            "--revision",
            "1",
            "--idempotency-key",
            "run_owned_cli_resume_01",
            "--json",
        )
    )["detail"] == cli_read
    assert cli_read["revision"] == 1
    assert cli_read["progress"]["rule_version"] == "profile-1"
    assert (cli_read["progress"]["completed"], cli_read["progress"]["applicable"], cli_read["progress"]["percentage"]) == (2, 2, 100)
    assert cli_read["progress"]["maturity"] == "saved"
    assert cli_read["steps"]["information"]["status"] == "complete"
    assert all(cli_read["steps"][section]["status"] == "not_started" for section in ("basic", "advanced", "financials"))
    assert cli_read["proposals"] == [] and cli_read["operation_ids"] == []
    fresh_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with fresh_sessions() as independent:
        saved = (await independent.scalars(select(ProductOnboardingSession))).one()
        product = (await independent.scalars(select(Product))).one()
        project = (await independent.scalars(select(Project))).one()
        bindings = (await independent.scalars(select(ProductProjectBinding))).all()
        operations = (await independent.scalars(select(ProductOperation))).all()
        assert saved.id == session_id and saved.last_product_revision == product.revision == 1
        assert saved.steps == cli_read["steps"]
        assert (saved.tenant_id, saved.product_id, saved.project_id) == (member.org_id, product.id, project.id)
        assert len(bindings) == 1 and bindings[0].project_id == project.id
        assert len(operations) == 3 and all(operation.state == "succeeded" for operation in operations)
        assert project.configuration == {} and project.provenance == []
        assert product.lifecycle_status == "draft"
    record_property("scenario_id", "SCN-01")
    record_property("criterion", "AC-01")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    requirements_revision = (
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"], text=True).strip()
    )
    record_property("requirements_revision", requirements_revision)
    record_property("observation", "UI API boundary, installed CLI, independent SQLAlchemy session")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")


@pytest.mark.asyncio
async def progress_applicability(test_client, test_engine, member, monkeypatch, tmp_path, record_property):
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Applicability must not call a provider"))
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    profile_path = f"/v1/products/{product_id}/projects/{project_id}"
    root = profile_path + "/onboarding-sessions"
    started = await test_client.post(root, json={}, headers={"If-Profile-Revision": "1", "Idempotency-Key": "run_owned_applicability_start_01"})
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    step_path = f"{root}/{session_id}/steps/financials"
    selected = await test_client.put(
        step_path,
        json={"action": "save", "configuration": {"financials": {}}},
        headers={"If-Profile-Revision": "1", "Idempotency-Key": "run_owned_applicability_select_01"},
    )
    assert selected.status_code == 200, selected.text
    assert (selected.json()["progress"]["completed"], selected.json()["progress"]["applicable"]) == (2, 3)

    later_input = tmp_path / "later.json"
    later_input.write_text(json.dumps({"action": "later"}), encoding="utf-8")
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    later_args = cli.parser().parse_args(
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
            str(later_input),
            "--revision",
            "2",
            "--idempotency-key",
            "run_owned_applicability_later_01",
            "--json",
        ]
    )
    deferred = (await asyncio.to_thread(cli.execute, later_args, gateway))["detail"]
    assert deferred["revision"] == 3
    assert (deferred["progress"]["completed"], deferred["progress"]["applicable"], deferred["progress"]["percentage"]) == (2, 3, 66)
    assert deferred["progress"]["checks"][-1]["status"] == "later"
    assert (await test_client.get(f"{root}/{session_id}")).json() == deferred
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as independent:
        assert (await independent.get(ProductOnboardingSession, (member.org_id, session_id))).steps["financials"]["status"] == "later"
        assert (await independent.get(Project, (member.org_id, project_id))).configuration["applicability"][0]["state"] == "later"

    excluded = await test_client.put(
        step_path,
        json={"action": "not_applicable", "reason": "No financial plan for this Idea"},
        headers={"If-Profile-Revision": "3", "Idempotency-Key": "run_owned_applicability_exclude_01"},
    )
    assert excluded.status_code == 200, excluded.text
    assert excluded.json()["revision"] == 4
    assert (excluded.json()["progress"]["completed"], excluded.json()["progress"]["applicable"], excluded.json()["progress"]["percentage"]) == (
        2,
        2,
        100,
    )
    assert excluded.json()["progress"]["checks"][-1] == {
        "id": "financials",
        "status": "not_applicable",
        "required": False,
        "reason": "No financial plan for this Idea",
    }
    denied = await test_client.put(
        f"{root}/{session_id}/steps/information",
        json={"action": "not_applicable", "reason": "Not needed"},
        headers={"If-Profile-Revision": "4", "Idempotency-Key": "run_owned_applicability_required_01"},
    )
    assert denied.status_code == 422 and denied.json()["detail"]["code"] == "required_check"
    read = await test_client.get(f"{root}/{session_id}")
    assert read.status_code == 200 and read.json() == excluded.json()
    assert (await test_client.get(profile_path)).json()["progress"] == read.json()["progress"]
    async with factory() as independent:
        saved = await independent.get(ProductOnboardingSession, (member.org_id, session_id))
        product = await independent.get(Product, (member.org_id, product_id))
        project = await independent.get(Project, (member.org_id, project_id))
        assert saved.steps["information"]["status"] == "complete"
        assert saved.steps["financials"]["reason"] == "No financial plan for this Idea"
        assert project.configuration["applicability"] == [
            {"check_id": "financials", "state": "not_applicable", "reason": "No financial plan for this Idea"}
        ]
        assert product.revision == saved.last_product_revision == 4
    record_property("scenario_id", "SCN-02")
    record_property("criterion", "AC-02")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"], text=True).strip(),
    )
    record_property("observation", "gateway API and installed CLI, independently read from database and Product profile")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")


@pytest.mark.asyncio
async def session_state_freshness(test_client, test_engine, member, monkeypatch, record_property):
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    observed_clock = clock + timedelta(minutes=30)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: observed_clock)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Session reads cannot depend on a provider"))
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    profile_path = f"/v1/products/{product_id}/projects/{project_id}"
    root = profile_path + "/onboarding-sessions"
    started = await test_client.post(root, json={}, headers={"If-Profile-Revision": "1", "Idempotency-Key": "run_owned_freshness_start_01"})
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    later = await test_client.put(
        f"{root}/{session_id}/steps/financials",
        json={"action": "later"},
        headers={"If-Profile-Revision": "1", "Idempotency-Key": "run_owned_freshness_later_01"},
    )
    assert later.status_code == 200 and later.json()["revision"] == 2
    reference, evidence = proof(project_id, member.org_id, "repository", "basic/repositories", clock)
    operation = ProductOperation(
        tenant_id=member.org_id,
        product_id=product_id,
        project_id=project_id,
        session_id=session_id,
        actor_id=member.user_id,
        operation="startHandoff",
        key_hash="c" * 64,
        request_hash="d" * 64,
        request={"step_id": "financials"},
        receipt={"state": "pending"},
        state="pending",
        expires_at=clock + timedelta(days=1),
        created_at=clock,
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as writer:
        project = await writer.get(Project, (member.org_id, project_id))
        project.configuration = {**project.configuration, "basic": {"repositories": [reference]}}
        project.provenance = [evidence]
        writer.add(operation)
        await writer.commit()
        operation_id = operation.id

    restarted_app = create_app()
    restarted_app.dependency_overrides[get_current_user] = lambda: member

    async def restored_db():
        async with factory() as session:
            yield session

    restarted_app.dependency_overrides[get_db] = restored_db
    async with AsyncClient(transport=ASGITransport(app=restarted_app), base_url="http://test") as restarted:
        waiting_response = await restarted.get(f"{root}/{session_id}")
        assert waiting_response.status_code == 200, waiting_response.text
        waiting = waiting_response.json()
        assert waiting["state"] == "waiting_external" and waiting["revision"] == 2
        assert waiting["steps"]["financials"]["status"] == "waiting_external"
        assert waiting["steps"]["financials"]["operation_id"] == operation_id
        assert waiting["operation_ids"] == [operation_id]
        assert waiting["steps"]["basic"]["status"] == "complete"
        assert waiting["steps"]["basic"]["evidence_refs"] == [reference]
        assert (waiting["progress"]["completed"], waiting["progress"]["applicable"], waiting["progress"]["maturity"]) == (
            3,
            4,
            "getting_started",
        )
        async with factory() as writer:
            saved_operation = await writer.get(ProductOperation, (member.org_id, operation_id))
            assert saved_operation.session_id == session_id and saved_operation.state == "pending"
            saved_operation.state = "failed"
            await writer.commit()
        failed_response = await restarted.get(f"{root}/{session_id}")
        assert failed_response.status_code == 200, failed_response.text
        failed = failed_response.json()
        assert failed["steps"]["financials"]["status"] == "blocked"
        assert failed["steps"]["financials"]["operation_id"] == operation_id
        assert failed["operation_ids"] == [operation_id] and failed["revision"] == 2
        assert failed["state"] == "open" and failed["progress"]["maturity"] == "getting_started"

        monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock + timedelta(hours=2))
        monkeypatch.setattr("src.products.routes.utcnow", lambda: clock + timedelta(hours=2))
        expired_response = await restarted.get(f"{root}/{session_id}")
        assert expired_response.status_code == 200, expired_response.text
        expired = expired_response.json()
        assert expired["revision"] == 2 and expired["state"] == "open"
        assert expired["steps"]["basic"]["status"] == "blocked"
        assert expired["steps"]["basic"]["evidence_refs"] == []
        assert expired["steps"]["financials"]["status"] == "blocked"
        assert expired["steps"]["financials"]["operation_id"] == operation_id
        assert expired["progress"]["maturity"] == "saved"
        assert (expired["progress"]["completed"], expired["progress"]["applicable"]) == (2, 4)
        assert next(check for check in expired["progress"]["checks"] if check["id"] == "repository")["status"] == "stale"
        profile_response = await restarted.get(profile_path)
        assert profile_response.status_code == 200, profile_response.text
        assert next(check for check in profile_response.json()["progress"]["checks"] if check["id"] == "repository")["status"] == "stale"
    async with factory() as independent:
        saved = await independent.get(ProductOnboardingSession, (member.org_id, session_id))
        product = await independent.get(Product, (member.org_id, product_id))
        project = await independent.get(Project, (member.org_id, project_id))
        operation = await independent.get(ProductOperation, (member.org_id, operation_id))
        assert saved.state == "open" and saved.last_product_revision == product.revision == 2
        assert saved.steps["basic"]["status"] == "not_started"
        assert operation.state == "failed" and operation.session_id == session_id
        assert project.provenance == [evidence] and project.configuration["basic"]["repositories"] == [reference]
        assert product.lifecycle_status == "draft" and project.execution_policy_ref is None
    record_property("scenario_id", "SCN-03")
    record_property("criterion", "AC-03")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"], text=True).strip(),
    )
    record_property("observation", "fresh ASGI app, separate database sessions, synthetic connector evidence, pinned clock")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")
