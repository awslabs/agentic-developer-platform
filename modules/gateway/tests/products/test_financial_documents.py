"""Financial document import uses provider reads and selective profile writes."""

import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.product_onboarding.financial_documents import FinancialDocument, document_location, read_document
from src.products.models import ProductChange, ProductSourceBinding
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i07_api import mutation_headers

pytest_plugins = ("tests.products.test_i07_api",)

DOCUMENT = {
    "release_target_date": "2027-01-18",
    "allocation": {
        "name": "Suggested plan",
        "amount": {"amount": "100.00", "currency": "USD"},
        "category": "cloud",
        "period_start": "2026-10-01",
        "period_end": "2027-03-31",
    },
}


def source():
    binding = SimpleNamespace(
        kind="document",
        provider="github",
        installation_id="connection-1",
        tenant_id="tenant-1",
        native_id="123:plans/financials.json",
        namespace="456",
        desired={},
    )
    connection = SimpleNamespace(
        id="connection-1", org_id="tenant-1", provider="github", ownership_disputed=False, installation_id="789", provider_scope_id="example"
    )
    content = json.dumps(DOCUMENT).encode()
    digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    github = SimpleNamespace(
        get_installation=AsyncMock(return_value={"id": 789, "account": {"id": 456, "login": "example"}}),
        get_installation_repository=AsyncMock(return_value={"id": 123, "owner": {"id": 456, "login": "example"}, "name": "product"}),
        installation_request=AsyncMock(
            return_value={
                "type": "file",
                "path": "plans/financials.json",
                "encoding": "base64",
                "size": len(content),
                "content": base64.b64encode(content).decode(),
                "sha": digest,
            }
        ),
    )
    return binding, connection, github, digest


@pytest.mark.asyncio
async def test_document_read_proves_provider_identity_and_blob():
    binding, connection, github, digest = source()
    document, revision = await read_document(binding, connection, client=github)
    assert document.model_dump(mode="json") == DOCUMENT and revision == digest
    github.installation_request.assert_awaited_once_with(789, "GET", "/repos/example/product/contents/plans/financials.json")


@pytest.mark.asyncio
async def test_document_verification_retains_the_observed_revision():
    from src.products.verification import observe_binding

    binding, connection, github, digest = source()
    result = await observe_binding(binding, connection, github_client=github)
    assert result["status"] == "healthy" and result["coverage"] == "complete"
    assert result["observed"]["source_revision"] == digest
    assert result["observed"]["native_id"] == binding.native_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["installation", "repository", "owner", "blob", "symlink", "oversize"])
async def test_document_refuses_wrong_provider_or_content(failure):
    binding, connection, github, _ = source()
    if failure == "installation":
        github.get_installation.return_value["id"] = 790
    elif failure == "repository":
        github.get_installation_repository.return_value["id"] = 124
    elif failure == "owner":
        github.get_installation_repository.return_value["owner"]["id"] = 457
    elif failure == "blob":
        github.installation_request.return_value["sha"] = "0" * 40
    elif failure == "symlink":
        github.installation_request.return_value["type"] = "symlink"
    else:
        github.installation_request.return_value["size"] = 65537
    with pytest.raises((ValueError, HTTPException)):
        await read_document(binding, connection, client=github)


@pytest.mark.parametrize(
    "path", ["123:../private.json", "123:/private.json", "123:plans//file.json", "https://evil.test/file", "123:plans\\file.json"]
)
def test_document_locator_is_not_an_arbitrary_url(path):
    binding, *_ = source()
    binding.native_id = path
    with pytest.raises(ValueError):
        document_location(binding)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"release_target_date": "2027-02-30"},
        {"release_target_date": "2027-01-18", "approval_status": "approved"},
        {"allocation": {**DOCUMENT["allocation"], "period_end": "2020-01-01"}},
    ],
)
def test_document_requires_valid_proposed_financial_values(document):
    with pytest.raises(ValueError):
        FinancialDocument.model_validate(document)


@pytest.fixture
async def imported_document(test_client, db_session, member, monkeypatch):
    connection = SimpleNamespace(
        id="connection-1", installation_id="789", org_id=member.org_id, provider="github", ownership_disputed=False, provider_scope_id="example"
    )

    async def access(*args, **kwargs):
        return connection

    monkeypatch.setattr("src.products.source_routes.connection_access", access)
    reader = AsyncMock(return_value=(FinancialDocument.model_validate(DOCUMENT), "a" * 40))
    monkeypatch.setattr("src.product_onboarding.financial_documents.read_document", reader)
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}"
    selected = await test_client.post(
        root + "/bindings",
        json={
            "kind": "document",
            "provider": "github",
            "connection_ref": {"kind": "github_installation", "id": "connection-1"},
            "namespace": "456",
            "native_id": "123:plans/financials.json",
        },
        headers=mutation_headers(1, "document_selection_123"),
    )
    assert selected.status_code == 201, selected.text
    started = await test_client.post(root + "/onboarding-sessions", json={}, headers=mutation_headers(2, "document_session_123"))
    assert started.status_code == 201, started.text
    target = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 2))
    allocation = {
        **DOCUMENT["allocation"],
        "id": "manual-plan",
        "approval_status": "proposed",
        "amount": {"amount": "75.00", "currency": "USD"},
        "owner_ref": {"kind": "user", "id": member.user_id},
        "target_ref": {
            "tenant_id": member.org_id,
            "project_id": project_id,
            "source_connection_id": "adp.profile",
            "record_type": "product",
            "record_id": product_id,
            "record_revision": target.id,
        },
    }
    session = root + "/onboarding-sessions/" + started.json()["id"]
    saved = await test_client.put(
        session + "/steps/financials",
        json={"action": "save", "configuration": {"financials": {"allocations": [allocation]}}},
        headers=mutation_headers(2, "document_manual_plan_123"),
    )
    assert saved.status_code == 200, saved.text
    return root, session, allocation, reader, selected.json()["id"]


@pytest.mark.asyncio
async def test_import_selective_confirmation_and_replay(imported_document, test_client):
    root, session, allocation, reader, _ = imported_document
    body = {"mode": "financial", "message": "Read connected financial documents"}
    headers = mutation_headers(3, "document_import_123")
    proposed = await test_client.post(session + "/guidance", json=body, headers=headers)
    assert proposed.status_code == 200, proposed.text
    (candidate,) = proposed.json()["proposals"]
    assert proposed.json()["mode"] == "financial"
    assert candidate["configuration"]["financials"]["allocations"][0]["amount"]["amount"] == "100.00"
    assert (await test_client.get(root)).json()["configuration"]["financials"] == {"allocations": [allocation]}
    assert (await test_client.post(session + "/guidance", json=body, headers=headers)).json() == proposed.json()
    reader.assert_awaited_once()

    accepted = await test_client.put(
        session + "/steps/financials",
        json={"action": "confirm", "proposal_id": candidate["id"], "confirmed_paths": ["/configuration/financials/release_target_date"]},
        headers=mutation_headers(3, "document_confirm_date_123"),
    )
    assert accepted.status_code == 200, accepted.text
    current = (await test_client.get(root)).json()
    assert current["configuration"]["financials"] == {"allocations": [allocation], "release_target_date": "2027-01-18"}
    replay = await test_client.post(session + "/guidance", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["revision"] == 4
    assert replay.json()["proposals"][0]["remaining_paths"] == ["/configuration/financials/allocations"]
    reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_document_import_rejects_stale_request_without_provider_read(imported_document, test_client):
    root, session, allocation, reader, _ = imported_document
    result = await test_client.post(
        session + "/guidance", json={"mode": "financial", "message": "Read document"}, headers=mutation_headers(2, "document_stale_import_123")
    )
    assert result.status_code == 412
    reader.assert_not_awaited()
    assert (await test_client.get(root)).json()["configuration"]["financials"] == {"allocations": [allocation]}


@pytest.mark.asyncio
async def test_revocation_during_document_read_publishes_nothing(imported_document, test_client, db_session, monkeypatch):
    root, session, allocation, reader, binding_id = imported_document

    async def revoke(binding, connection):
        row = await db_session.get(ProductSourceBinding, (binding.tenant_id, binding_id))
        row.status = "revoked"
        await db_session.commit()
        return FinancialDocument.model_validate(DOCUMENT), "a" * 40

    reader.side_effect = revoke
    result = await test_client.post(
        session + "/guidance", json={"mode": "financial", "message": "Read document"}, headers=mutation_headers(3, "document_revocation_123")
    )
    assert result.status_code == 409, result.text
    assert (await test_client.get(session)).json()["proposals"] == []
    assert (await test_client.get(root)).json()["configuration"]["financials"] == {"allocations": [allocation]}


@pytest.mark.asyncio
async def test_document_timeout_does_not_publish_or_change_saved_values(imported_document, test_client):
    root, session, allocation, reader, _ = imported_document
    reader.side_effect = TimeoutError()
    response = await test_client.post(
        session + "/guidance",
        json={"mode": "financial", "message": "Read document"},
        headers=mutation_headers(3, "document_timeout_123"),
    )
    assert response.status_code == 503 and response.json()["detail"]["code"] == "source_unavailable"
    assert (await test_client.get(session)).json()["proposals"] == []
    assert (await test_client.get(root)).json()["configuration"]["financials"] == {"allocations": [allocation]}
