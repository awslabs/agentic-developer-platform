"""Pending operations and expiring evidence are projected on read, never granted authority."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.dependencies import get_current_user
from src.product_onboarding.progress import profile_progress
from src.products.models import Product, ProductOnboardingSession, ProductOperation, Project
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member
from tests.products.test_i01 import HEADERS, REQUEST


@pytest.fixture
async def member(db_session, app, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Test workspace"))
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


def headers(revision, key):
    return {"If-Profile-Revision": str(revision), "Idempotency-Key": key}


def proof(project_id, tenant_id, record_type, field, clock, *, method="connector", stale_after=None):
    ref = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "source_connection_id": "test-source",
        "record_type": record_type,
        "record_id": "fixture-record",
        "record_revision": "revision-1",
    }
    evidence = {
        "paths": [f"/configuration/{field}"],
        "state": "accepted",
        "method": method,
        "source_refs": [ref],
        "observed_at": clock.isoformat(),
        "stale_after": (stale_after or clock + timedelta(hours=1)).isoformat(),
    }
    return ref, evidence


@pytest.mark.asyncio
async def test_pending_then_failed_operation_after_restart(test_client, db_session, test_engine, member, monkeypatch):
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    started = await test_client.post(root, json={}, headers=headers(1, "start_state_key_123"))
    session_id = started.json()["id"]
    selected = await test_client.put(
        f"{root}/{session_id}/steps/financials",
        json={"action": "later"},
        headers=headers(1, "later_state_key_123"),
    )
    assert selected.status_code == 200, selected.text
    operation = ProductOperation(
        tenant_id=member.org_id,
        product_id=product_id,
        project_id=project_id,
        session_id=session_id,
        actor_id=member.user_id,
        operation="startHandoff",
        key_hash="a" * 64,
        request_hash="b" * 64,
        request={"step_id": "financials"},
        receipt={"state": "pending"},
        state="pending",
        expires_at=clock + timedelta(days=1),
        created_at=clock,
    )
    db_session.add(operation)
    await db_session.commit()
    operation_id = operation.id
    fresh_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with fresh_sessions() as new_process:
        assert (await new_process.scalars(select(ProductOperation).where(ProductOperation.session_id == session_id))).all()
    waiting = await test_client.get(f"{root}/{session_id}")
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["state"] == "waiting_external"
    assert waiting.json()["steps"]["financials"]["status"] == "waiting_external"
    assert waiting.json()["steps"]["financials"]["operation_id"] == operation_id
    assert waiting.json()["operation_ids"] == [operation_id]
    assert waiting.json()["steps"]["financials"]["evidence_refs"] == []
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (member.org_id, operation_id))
    operation.state = "failed"
    await db_session.commit()
    blocked = (await test_client.get(f"{root}/{session_id}")).json()
    assert blocked["state"] == "open" and blocked["steps"]["financials"]["status"] == "blocked"
    assert blocked["steps"]["financials"]["operation_id"] == operation_id
    assert blocked["operation_ids"] == [operation_id]
    assert blocked["progress"]["maturity"] == "saved"
    db_session.expire_all()
    assert (await db_session.get(Product, (member.org_id, product_id))).lifecycle_status == "draft"
    assert (await db_session.get(Product, (member.org_id, product_id))).revision == 2
    assert (await db_session.get(Project, (member.org_id, project_id))).execution_policy_ref is None
    assert (await db_session.get(ProductOnboardingSession, (member.org_id, session_id))).state == "open"


@pytest.mark.asyncio
async def test_evidence_freshness_changes_without_revision_or_permission(test_client, db_session, member, monkeypatch):
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock + timedelta(minutes=30))
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    started = await test_client.post(root, json={}, headers=headers(1, "start_evidence_key_123"))
    session_id = started.json()["id"]
    ref, evidence = proof(project_id, member.org_id, "repository", "basic/repositories", clock)
    project = await db_session.get(Project, (member.org_id, project_id))
    project.configuration = {"basic": {"repositories": [ref]}}
    project.provenance = [evidence]
    await db_session.commit()
    active = (await test_client.get(f"{root}/{session_id}")).json()
    assert active["progress"]["completed"] == active["progress"]["applicable"] == 3
    assert active["progress"]["maturity"] == "getting_started"
    assert active["steps"]["basic"]["status"] == "complete"
    assert active["steps"]["basic"]["evidence_refs"] == [ref]
    assert active["state"] == "open"
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock + timedelta(hours=2))
    expired = (await test_client.get(f"{root}/{session_id}")).json()
    assert expired["revision"] == active["revision"] == 1
    assert expired["progress"]["completed"] == 2 and expired["progress"]["applicable"] == 3
    assert expired["progress"]["checks"][2]["status"] == "stale"
    assert expired["progress"]["maturity"] == "saved"
    assert expired["steps"]["basic"]["status"] == "blocked"
    assert expired["steps"]["basic"]["evidence_refs"] == []
    assert "expired" in expired["steps"]["basic"]["reason"]
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")).json()["progress"]["checks"][2]["status"] == "stale"
    db_session.expire_all()
    assert (await db_session.get(Project, (member.org_id, project_id))).provenance[0]["observed_at"] == evidence["observed_at"]
    assert (await db_session.get(Product, (member.org_id, product_id))).revision == 1
    assert (await db_session.get(Product, (member.org_id, product_id))).lifecycle_status == "draft"
    assert (await db_session.get(Project, (member.org_id, project_id))).execution_policy_ref is None


def test_only_scoped_current_connector_evidence_can_complete_selected_checks():
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    tenant_id, project_id = "fixture-tenant", "fixture-project"
    product = Product(
        tenant_id=tenant_id,
        name="Planner",
        purpose="Test",
        intended_users=["Maintainers"],
        owner_user_id="owner",
        starting_point="running_product",
        created_by="owner",
        lifecycle_status="draft",
    )
    paths = {
        "repository": ("basic", "repositories"),
        "environment": ("advanced", "environments"),
        "pipeline": ("advanced", "pipelines"),
        "operations": ("advanced", "runbooks"),
    }
    config, provenance = {"basic": {}, "advanced": {}}, []
    for check_id, (section, field) in paths.items():
        ref, evidence = proof(project_id, tenant_id, check_id, f"{section}/{field}", clock)
        config[section][field] = [ref]
        provenance.append(evidence)
    project = Project(
        tenant_id=tenant_id,
        id=project_id,
        name="Scope",
        owner_user_id="owner",
        configuration=config,
        provenance=provenance,
    )
    verified = profile_progress(product, project, now=clock + timedelta(minutes=30))
    assert verified["completed"] == verified["applicable"] == 6
    assert verified["maturity"] == "pro"
    assert product.lifecycle_status == "draft" and project.execution_policy_ref is None
    project.provenance = [{**entry, "method": "manual"} for entry in provenance]
    assert profile_progress(product, project, now=clock + timedelta(minutes=30))["completed"] == 2
    project.provenance = [{**entry, "source_refs": [{**entry["source_refs"][0], "project_id": "another-project"}]} for entry in provenance]
    assert profile_progress(product, project, now=clock + timedelta(minutes=30))["completed"] == 2
    project.provenance = provenance
    deferred = profile_progress(product, project, steps={"basic": {"status": "later"}}, now=clock + timedelta(hours=2))
    assert next(check for check in deferred["checks"] if check["id"] == "repository")["status"] == "stale"
    config["basic"]["repositories"] = [{**config["basic"]["repositories"][0], "record_revision": "new-revision"}]
    assert profile_progress(product, project, now=clock + timedelta(minutes=30))["completed"] == 5
