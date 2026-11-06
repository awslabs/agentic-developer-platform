"""Owner UI discovers original operations and canonical source selectors."""

from sqlalchemy import select

from src.products.knowledge_evidence_model import KnowledgeUseEvidence
from tests.products.test_knowledge_evidence import AUTH
from tests.products.test_knowledge_evidence import claimed_knowledge as claimed_knowledge
from tests.products.test_knowledge_evidence import completed_publication as completed_publication
from tests.products.test_knowledge_evidence import consent_fixture as consent_fixture
from tests.products.test_knowledge_evidence import knowledge_fixture as knowledge_fixture
from tests.products.test_knowledge_evidence import knowledge_worker as knowledge_worker
from tests.products.test_knowledge_evidence import l5_owner as l5_owner

__all__ = ["claimed_knowledge", "completed_publication", "consent_fixture", "knowledge_fixture", "knowledge_worker", "l5_owner"]


async def test_original_operation_discovery_and_prepare_metadata_without_existing_evidence(test_client, db_session, completed_publication):
    fixture = completed_publication
    row = fixture.row
    listing = await test_client.get(row.fixture["path"] + "/knowledge-operations", headers=AUTH, params={"binding_id": row.operation.binding_id})
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"] == [
        {
            "operation_id": row.intent.operation_id,
            "binding_id": row.operation.binding_id,
            "action": "sync_knowledge",
            "state": "waiting",
            "revision": row.operation.revision,
            "gaps": ["knowledge_native_use_unqualified"],
            "configuration_revision": row.intent.request["configuration_revision"],
            "binding_generation": row.intent.binding_generation,
        }
    ]
    assert listing.json()["next_offset"] is None
    inspected = await test_client.get(fixture.path, headers=AUTH)
    assert inspected.status_code == 200, inspected.text
    data = inspected.json()
    assert inspected.headers["etag"] == f'"{row.operation.revision}"'
    assert data["items"] == []
    assert data["selection"]["binding_generation"] == row.intent.binding_generation
    assert data["publications"][0]["asset_id"] == "1" and data["publications"][0]["version"] == 1
    assert len(data["publications"][0]["source_key"]) == 64
    assert data["publications"][0]["metadata"] == {"agent_type": "CHAT"}
    assert data["eligibility"] == {"can_prepare": True, "can_review": False, "blocked_code": None}
    assert await db_session.scalar(select(KnowledgeUseEvidence)) is None
    assert not fixture.objects.values
    assert "content" not in data["publications"][0]


async def test_history_metadata_survives_source_revocation_but_collection_is_disabled(test_client, db_session, completed_publication):
    fixture = completed_publication
    fixture.row.fixture["source_binding"].status = "revoked"
    await db_session.commit()
    result = await test_client.get(fixture.path, headers=AUTH)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["eligibility"]["can_prepare"] is False and data["eligibility"]["can_review"] is False
    assert data["eligibility"]["blocked_code"] is not None
    assert data["publications"][0]["asset_id"] is None
    assert "source_excerpt" not in result.text and "transcript" not in result.text
    assert not fixture.objects.values


async def test_operation_discovery_is_owner_scoped_bounded_and_binding_filtered(test_client, db_session, completed_publication):
    fixture = completed_publication
    path = fixture.row.fixture["path"] + "/knowledge-operations"
    filtered = await test_client.get(path, headers=AUTH, params={"binding_id": "another-binding"})
    assert filtered.status_code == 200 and filtered.json()["items"] == []
    assert (await test_client.get(path, headers=AUTH, params={"offset": 1})).json()["items"] == []
    assert (await test_client.get(path, headers=AUTH, params={"limit": 101})).status_code == 422
    fixture.row.fixture["configurations"]["devops"][0].owner_id = "another-owner"
    await db_session.commit()
    assert (await test_client.get(path, headers=AUTH)).status_code == 404
