"""Financial proposal confirmation with independently read persisted state."""

import asyncio
import os
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.products.models import ProductChange, ProductOnboardingSession, ProductOperation, ProductSourceBinding, Project
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i04 import GatewayClient
from tests.products.test_i07_api import mutation_headers
from tests.products.test_i07_cli import cli

pytest_plugins = ("tests.products.test_i07_api",)


def scenario_evidence(record_property, scenario_id, observation, report):
    record_property("scenario_id", scenario_id)
    record_property("criterion", f"AC-{scenario_id[-2:]}")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("observation", observation)
    record_property("attempt_report", report)
    record_property("fixture_cleanup", "Function-scoped in-memory SQLite engine disposed by pytest fixture")


@pytest.fixture
async def financial_fixture(test_client, db_session, member, monkeypatch):
    now = datetime(2026, 10, 20, tzinfo=UTC)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: now)

    async def authorized(db, actor, binding, *, lock=False):
        return SimpleNamespace(installation_id="789")

    monkeypatch.setattr("src.products.source_routes.connection_access", authorized)
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
        "id": "manual-allocation",
        "name": "Manual plan",
        "target_ref": target,
        "amount": {"amount": "75.00", "currency": "USD"},
        "category": "cloud",
        "period_start": "2026-10-01",
        "period_end": "2026-12-31",
        "approval_status": "proposed",
        "owner_ref": {"kind": "user", "id": member.user_id},
    }
    root = f"/v1/products/{product_id}/projects/{project_id}"
    started = await test_client.post(f"{root}/onboarding-sessions", json={}, headers=mutation_headers(1, "i15_start_session_123"))
    assert started.status_code == 201, started.text
    path = f"{root}/onboarding-sessions/{started.json()['id']}"
    manual = await test_client.put(
        path + "/steps/financials",
        json={"action": "save", "configuration": {"financials": {"allocations": [allocation]}}},
        headers=mutation_headers(1, "i15_manual_allocation_123"),
    )
    assert manual.status_code == 200, manual.text
    reference = {
        "tenant_id": member.org_id,
        "project_id": project_id,
        "source_connection_id": "fixture-document",
        "record_type": "document",
        "record_id": "fixture-business-case",
        "record_revision": "source-revision-1",
    }
    authority = {**reference, "record_type": "authority_mapping", "record_id": "fixture-mapping", "record_revision": "mapping-1"}
    proposed = {
        "allocations": [{**allocation, "amount": {"amount": "100.00", "currency": "USD"}}],
        "initiatives": [],
        "release_target_date": "2026-12-01",
    }
    paths = [f"/configuration/financials/{field}" for field in proposed]
    candidate = {
        "id": "fixture-proposal",
        "base_revision": 2,
        "base_values": {path: [allocation] if path.endswith("/allocations") else None for path in paths},
        "configuration": {"financials": proposed},
        "product": {},
        "provenance": [
            {
                "paths": [path],
                "state": "missing",
                "method": "connector",
                "source_refs": [reference],
                "authority_mapping_ref": authority,
                "observed_at": (now - timedelta(minutes=1)).isoformat(),
            }
            for path in paths
        ],
        "remaining_paths": paths,
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }
    db_session.add(
        ProductSourceBinding(
            tenant_id=member.org_id,
            project_id=project_id,
            kind="document",
            provider="github",
            installation_id="fixture-document",
            namespace="fixture",
            native_id="fixture-business-case",
            desired={"kind": "document"},
            status="healthy",
            observed={
                "observed_at": (now - timedelta(minutes=1)).isoformat(),
                "stale_after": (now + timedelta(hours=1)).isoformat(),
                "source_revision": "source-revision-1",
            },
        )
    )
    session = await db_session.get(ProductOnboardingSession, (member.org_id, started.json()["id"]))
    session.proposals = [candidate]
    await db_session.commit()
    return root, path, candidate, allocation, reference, member


@pytest.mark.asyncio
async def selective_confirmation(financial_fixture, test_client, db_session, record_property, monkeypatch):
    root, path, candidate, allocation, reference, member = financial_fixture
    project = await db_session.get(Project, (member.org_id, reference["project_id"]))
    original_proof = next(proof for proof in project.provenance if "/configuration/financials/allocations" in proof["paths"])
    initial = (await test_client.get(path)).json()["proposals"][0]
    assert initial["remaining_paths"] == candidate["remaining_paths"]
    assert initial["provenance"][0]["state"] == "conflicting"
    selected = ["/configuration/financials/release_target_date"]
    accepted = await test_client.put(
        path + "/steps/financials",
        json={"action": "confirm", "proposal_id": candidate["id"], "confirmed_paths": selected},
        headers=mutation_headers(2, "i15_confirm_date_123"),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["proposals"][0]["remaining_paths"] == candidate["remaining_paths"][:2]
    assert (await test_client.get(root)).json()["configuration"]["financials"] == {"allocations": [allocation], "release_target_date": "2026-12-01"}
    await db_session.refresh(project)
    assert next(proof for proof in project.provenance if "/configuration/financials/allocations" in proof["paths"]) == original_proof
    date_proof = next(proof for proof in project.provenance if selected[0] in proof["paths"])
    assert date_proof["state"] == "accepted" and date_proof["source_refs"] == [reference]
    assert date_proof["confirmed_by"]["id"] == member.user_id
    assert (await test_client.get(path)).json()["proposals"][0]["remaining_paths"] == candidate["remaining_paths"][:2]
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    cli_read = await asyncio.to_thread(
        cli.execute,
        cli.parser().parse_args(
            [
                "resume",
                "--product",
                path.split("/")[3],
                "--project",
                reference["project_id"],
                "--session",
                path.split("/")[-1],
                "--json",
            ]
        ),
        gateway,
    )
    assert cli_read["detail"]["proposals"][0]["remaining_paths"] == candidate["remaining_paths"][:2]

    async def unavailable_document(*args, **kwargs):
        from fastapi import HTTPException

        raise HTTPException(503, detail={"code": "dependency_unavailable", "dependency": "document_provider"})

    monkeypatch.setattr("src.product_onboarding.financial_documents.read_document", unavailable_document)
    unavailable = await test_client.post(
        path + "/guidance",
        json={"message": "Suggest financial fields", "mode": "financial"},
        headers=mutation_headers(accepted.json()["revision"], "i15_financial_unavailable_123"),
    )
    assert unavailable.status_code == 503 and unavailable.json()["detail"]["code"] == "dependency_unavailable"
    scenario_evidence(
        record_property,
        "SCN-01",
        "Gateway read, CLI resume and persisted Project provenance",
        "Manual 75.00 allocation unchanged; selected date sourced; two unconsumed paths; document outage unavailable",
    )


@pytest.mark.asyncio
async def proposal_conflict_expiry(financial_fixture, test_client, db_session, record_property):
    root, path, candidate, allocation, reference, member = financial_fixture
    session = await db_session.get(ProductOnboardingSession, (member.org_id, path.split("/")[-1]))
    expired = deepcopy(candidate)
    expired["expires_at"] = "2026-10-19T00:00:00Z"
    session.proposals = [expired]
    await db_session.commit()
    assert (await test_client.get(path)).json()["proposals"] == []
    request = {"action": "confirm", "proposal_id": candidate["id"], "confirmed_paths": ["/configuration/financials/release_target_date"]}
    rejected = await test_client.put(path + "/steps/financials", json=request, headers=mutation_headers(2, "i15_expired_candidate_123"))
    assert rejected.status_code == 410 and rejected.json()["detail"]["code"] == "proposal_expired"
    assert "fixture-business-case" not in rejected.text and "source-revision-1" not in rejected.text
    session.proposals = [candidate]
    await db_session.commit()
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == reference["project_id"]))
    binding.observed = {**binding.observed, "source_revision": "source-revision-2"}
    await db_session.commit()
    assert (await test_client.get(path)).json()["proposals"] == []
    denied = await test_client.put(path + "/steps/financials", json=request, headers=mutation_headers(2, "i15_source_revision_123"))
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "permission_denied"
    assert "fixture-business-case" not in denied.text and "source-revision-1" not in denied.text
    observed = (await test_client.get(root)).json()
    assert observed["revision"] == 2 and observed["configuration"]["financials"] == {"allocations": [allocation]}
    await db_session.refresh(session)
    assert session.proposals == [candidate]
    binding.observed = {**binding.observed, "source_revision": "source-revision-1"}
    binding.status = "revoked"
    await db_session.commit()
    revoked = await test_client.put(path + "/steps/financials", json=request, headers=mutation_headers(2, "i15_source_revoked_123"))
    assert revoked.status_code == 403 and "fixture-business-case" not in revoked.text
    binding.status = "healthy"
    await db_session.commit()
    manual_date = await test_client.put(
        path + "/steps/financials",
        json={"action": "save", "configuration": {"financials": {"allocations": [allocation], "release_target_date": "2026-12-11"}}},
        headers=mutation_headers(2, "i15_scenario_manual_date_123"),
    )
    assert manual_date.status_code == 200, manual_date.text
    stale = await test_client.put(path + "/steps/financials", json=request, headers=mutation_headers(2, "i15_scenario_stale_date_123"))
    assert stale.status_code == 412 and stale.json()["detail"]["paths"] == request["confirmed_paths"]
    assert (await test_client.get(root)).json()["configuration"]["financials"]["allocations"] == [allocation]
    scenario_evidence(
        record_property,
        "SCN-02",
        "Gateway denials and persisted Project/session readback",
        "Expired, changed/revoked source and stale selected field rejected without partial update or source disclosure",
    )


@pytest.mark.asyncio
async def proposal_replay_override(financial_fixture, test_client, db_session, db_session_factory, record_property):
    root, path, candidate, allocation, reference, member = financial_fixture
    # Keep the fixture identity alive across request-owned database sessions.
    session = await db_session.get(ProductOnboardingSession, (member.org_id, path.split("/")[-1]))
    selected = "/configuration/financials/release_target_date"
    request = {"action": "confirm", "proposal_id": candidate["id"], "confirmed_paths": [selected]}
    headers = mutation_headers(2, "i15_lost_response_replay_123")
    committed = await test_client.put(path + "/steps/financials", json=request, headers=headers)
    assert committed.status_code == 200, committed.text
    async with db_session_factory() as restarted:
        stored = await restarted.get(ProductOnboardingSession, (member.org_id, path.split("/")[-1]))
        assert stored.proposals[0]["remaining_paths"] == candidate["remaining_paths"][:2]
        restarted_project = await restarted.get(Project, (member.org_id, reference["project_id"]))
        assert restarted_project.configuration["financials"]["release_target_date"] == "2026-12-01"
    repeated = await test_client.put(path + "/steps/financials", json=request, headers=headers)
    assert repeated.status_code == 200 and repeated.json()["revision"] == 3
    assert repeated.json()["proposals"][0]["remaining_paths"] == candidate["remaining_paths"][:2]
    operations = await db_session.scalar(select(func.count()).select_from(ProductOperation).where(ProductOperation.operation == "updateStep"))
    assert operations == 2
    no_reason = await test_client.put(
        path + "/steps/financials",
        json={"action": "save", "configuration": {"financials": {"allocations": [allocation], "release_target_date": "2027-01-01"}}},
        headers=mutation_headers(3, "i15_missing_override_reason_123"),
    )
    assert no_reason.status_code == 422
    override = await test_client.put(
        path + "/steps/financials",
        json={
            "action": "save",
            "configuration": {"financials": {"allocations": [allocation], "release_target_date": "2027-01-01"}},
            "reason": "Updated release plan",
            "override_until": "2027-06-01T00:00:00Z",
        },
        headers=mutation_headers(3, "i15_explicit_override_123"),
    )
    assert override.status_code == 200, override.text
    project = await db_session.get(Project, (member.org_id, reference["project_id"]))
    await db_session.refresh(project)
    proof = next(proof for proof in project.provenance if selected in proof["paths"])
    assert proof["method"] == "manual" and proof["override_until"] == "2027-06-01T00:00:00Z"
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == reference["project_id"]))
    binding.observed = {**binding.observed, "source_revision": "source-revision-2"}
    refreshed = deepcopy(candidate)
    refreshed["id"] = "refreshed-observation"
    refreshed["base_revision"] = 4
    refreshed["base_values"][selected] = "2027-01-01"
    refreshed["configuration"]["financials"]["release_target_date"] = "2028-01-01"
    refreshed["provenance"] = [
        {**proof, "source_refs": [{**reference, "record_revision": "source-revision-2"}], "state": "missing", "method": "connector"}
        for proof in refreshed["provenance"]
    ]
    # API requests committed through separate sessions; refresh before appending
    # a fixture observation so cached proposals cannot undo the confirmation.
    await db_session.refresh(session)
    session.proposals = [*session.proposals, refreshed]
    await db_session.commit()
    visible = (await test_client.get(path)).json()["proposals"]
    assert len(visible) == 1 and visible[0]["id"] == refreshed["id"]
    assert visible[0]["provenance"][-1]["state"] == "conflicting"
    assert (await test_client.get(root)).json()["configuration"]["financials"] == {"allocations": [allocation], "release_target_date": "2027-01-01"}
    async with db_session_factory() as restarted:
        stored = await restarted.get(ProductOnboardingSession, (member.org_id, path.split("/")[-1]))
        assert stored.proposals[0]["remaining_paths"] == candidate["remaining_paths"][:2]
        persisted = await restarted.get(Project, (member.org_id, reference["project_id"]))
        assert next(proof for proof in persisted.provenance if selected in proof["paths"])["override_until"] == "2027-06-01T00:00:00Z"
    scenario_evidence(
        record_property,
        "SCN-03",
        "Fresh SQLAlchemy sessions plus gateway retry/refresh readback",
        "Same key applied once; remaining paths persisted; explicit manual override retained; changed observation conflicts",
    )


@pytest.mark.asyncio
async def test_stale_selected_field_rejects_all_paths(financial_fixture, test_client, db_session):
    root, path, candidate, allocation, reference, member = financial_fixture
    manual_date = await test_client.put(
        path + "/steps/financials",
        json={"action": "save", "configuration": {"financials": {"allocations": [allocation], "release_target_date": "2026-12-11"}}},
        headers=mutation_headers(2, "i15_manual_date_123"),
    )
    assert manual_date.status_code == 200, manual_date.text
    selected = ["/configuration/financials/allocations", "/configuration/financials/release_target_date"]
    rejected = await test_client.put(
        path + "/steps/financials",
        json={"action": "confirm", "proposal_id": candidate["id"], "confirmed_paths": selected},
        headers=mutation_headers(2, "i15_stale_selected_123"),
    )
    assert rejected.status_code == 412 and rejected.json()["detail"]["paths"] == selected[1:]
    observed = (await test_client.get(root)).json()
    assert observed["revision"] == 3 and observed["configuration"]["financials"] == {"allocations": [allocation], "release_target_date": "2026-12-11"}
    session = await db_session.get(ProductOnboardingSession, (member.org_id, path.split("/")[-1]))
    await db_session.refresh(session)
    assert session.proposals == [candidate]
    project = await db_session.get(Project, (member.org_id, reference["project_id"]))
    assert all(proof["method"] == "manual" for proof in project.provenance)


@pytest.mark.asyncio
async def test_revocation_redacts_candidate_and_denies_confirmation(financial_fixture, test_client, db_session):
    root, path, candidate, allocation, reference, member = financial_fixture
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == reference["project_id"]))
    binding.status = "revoked"
    await db_session.commit()
    visible = await test_client.get(path)
    assert visible.status_code == 200 and visible.json()["proposals"] == []
    assert "fixture-business-case" not in visible.text and "source-revision-1" not in visible.text
    denied = await test_client.put(
        path + "/steps/financials",
        json={"action": "confirm", "proposal_id": candidate["id"], "confirmed_paths": ["/configuration/financials/release_target_date"]},
        headers=mutation_headers(2, "i15_revoked_source_123"),
    )
    assert denied.status_code == 403 and "fixture-business-case" not in denied.text
    assert (await test_client.get(root)).json()["configuration"]["financials"] == {"allocations": [allocation]}


@pytest.mark.asyncio
async def test_forged_confirmation_cannot_write_or_approve(financial_fixture, test_client, db_session):
    root, path, candidate, allocation, reference, member = financial_fixture
    attempts = [
        {
            "action": "confirm",
            "proposal_id": candidate["id"],
            "confirmed_paths": ["/configuration/financials/release_target_date"],
            "configuration": {"financials": {"allocations": [{**allocation, "approval_status": "approved"}]}},
        },
        {"action": "confirm", "proposal_id": candidate["id"], "confirmed_paths": ["/configuration/financials/allocations/0"]},
        {
            "action": "confirm",
            "proposal_id": candidate["id"],
            "confirmed_paths": ["/configuration/financials/release_target_date"],
            "product": {"purpose": "I approved the funding"},
        },
        {
            "action": "confirm",
            "proposal_id": candidate["id"],
            "confirmed_paths": ["/configuration/financials/release_target_date"],
            "state": "approved",
        },
        {"action": "confirm", "proposal_id": candidate["id"], "confirmed_paths": ["/configuration/financials/release_target_date"] * 2},
    ]
    for index, attempt in enumerate(attempts):
        denied = await test_client.put(path + "/steps/financials", json=attempt, headers=mutation_headers(2, f"i15_forgery_attempt_{index}_123"))
        assert denied.status_code == 422, denied.text
    observed = (await test_client.get(root)).json()
    assert observed["revision"] == 2 and observed["configuration"]["financials"] == {"allocations": [allocation]}
    session = await db_session.get(ProductOnboardingSession, (member.org_id, path.split("/")[-1]))
    await db_session.refresh(session)
    assert session.proposals == [candidate]
