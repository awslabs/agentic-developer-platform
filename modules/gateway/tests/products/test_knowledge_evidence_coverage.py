"""One conversation can cover distinct pinned sources, but no source may be omitted."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from l5_services.devops.knowledge_native import KnowledgeNativeDriver
from l5_services.devops.knowledge_sync import source_key
from l5_services.devops.knowledge_worker import KnowledgeSetupWorker
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.products.models import ProductSourceBinding
from src.products.source_snapshots import ArtifactRef, CapturedSource
from tests.products.test_knowledge_evidence import AUTH, observation, scenario
from tests.products.test_knowledge_evidence import completed_publication as completed_publication
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as initial_knowledge_fixture
from tests.products.test_l5_knowledge_effects import Native
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["completed_publication", "initial_knowledge_fixture", "claimed_knowledge", "consent_fixture", "l5_owner"]


@pytest.fixture
async def knowledge_fixture(initial_knowledge_fixture, db_session):
    fixture = initial_knowledge_fixture
    second = {**fixture["source_ref"], "record_id": "document-b"}
    policy = deepcopy(fixture["policy"])
    policy["source_refs"].append(second)
    # Skills may coexist in one native space; agent instructions intentionally
    # refuse overwriting a separate instruction target.
    policy["publications"] = [
        {
            "source_ref": reference,
            "kind": "skill",
            "scope": "agent",
            "active": True,
            "native_metadata": {"name": reference["record_id"], "description": "Follow the team's operational policy", "agent_types": ["CHAT"]},
        }
        for reference in policy["source_refs"]
    ]
    version = fixture["version"]
    version.settings = {**version.settings, "devops": {**version.settings["devops"], "knowledge_policy": policy}}
    db_session.add(
        ProductSourceBinding(
            tenant_id=second["tenant_id"],
            project_id=second["project_id"],
            credential_id=second["source_connection_id"],
            kind="document",
            provider="document",
            namespace="examples",
            native_id="document-b",
            desired={"source_revision": "r1"},
            observed={"source_revision": "r1"},
            status="healthy",
        )
    )

    async def capture(_binding, _mapping, request, _objects):
        return CapturedSource(
            source_ref=request.source_ref,
            exclusions=[],
            file_count=1,
            expires_at=request.retain_until,
            content=ArtifactRef(
                artifact_id="captured-" + request.source_ref.record_id,
                version="v1",
                sha256=sha256(b"original").hexdigest(),
                media_type="text/plain",
                bytes=8,
            ),
            file_manifest=ArtifactRef(
                artifact_id="manifest-" + request.source_ref.record_id, version="v1", sha256="b" * 64, media_type="application/json", bytes=20
            ),
        )

    fixture["capture"].side_effect = capture
    await db_session.commit()
    return {**fixture, "second_source": second}


@pytest.fixture
async def knowledge_worker(test_engine, claimed_knowledge):
    row = claimed_knowledge

    async def stream(*args):
        yield b"original"

    async def read(*args):
        return next(item for item in row.intent.authorization["knowledge_admission"]["source_snapshots"] if item["snapshot_id"] == args[2])

    native = Native()
    worker = KnowledgeSetupWorker(
        async_sessionmaker(test_engine, expire_on_commit=False),
        SimpleNamespace(read_source=read, stream_source=stream),
        KnowledgeNativeDriver(native),
        AsyncMock(),
    )
    return row, worker, native


async def test_all_original_sources_need_evidence_and_same_real_conversation_can_cover_both(test_client, completed_publication):
    fixture = completed_publication
    prepared = []
    for index, source in enumerate((fixture.row.fixture["source_ref"], fixture.row.fixture["second_source"])):
        result = await test_client.post(
            fixture.path + "/prepare",
            json={**scenario(fixture), "source_key": source_key(source)},
            headers={**AUTH, "Idempotency-Key": f"source_knowledge_{index:02}", "If-Match": f'"{fixture.row.operation.revision}"'},
        )
        assert result.status_code == 200, result.text
        prepared.append(result.json())
    first = observation(fixture, prepared[0])
    first["transcript"][-1]["text"] = "Invoked document-a and document-b; follow their original policy."
    first["source_citation_quote"] = "document-a"
    first["skill_invocation_quote"] = "Invoked document-a and document-b"
    second = deepcopy(first)
    second["preparation_hash"] = prepared[1]["event_hash"]
    second["source_citation_quote"] = "document-b"
    for key in ("before", "after"):
        second[key].update(asset_id="2", metadata=fixture.native.assets["2"]["metadata"])
    observed = []
    for preparation, body in zip(prepared, (first, second), strict=True):
        result = await test_client.post(fixture.path + "/" + preparation["evidence_id"] + "/observe", json=body, headers=AUTH)
        assert result.status_code == 200, result.text
        observed.append(result.json())
    reviews = [
        {
            "evidence_id": item["evidence_id"],
            "observation_hash": item["event_hash"],
            "source_effect_explanation": "I verified this selected skill's actual invocation and source-dependent operational policy.",
        }
        for item in observed
    ]
    headers = {**AUTH, "Idempotency-Key": "review_both_sources_01", "If-Match": f'"{fixture.row.operation.revision}"'}
    incomplete = await test_client.post(fixture.path + "/complete", json={"reviews": reviews[:1]}, headers=headers)
    assert incomplete.status_code == 409 and incomplete.json()["detail"]["code"] == "knowledge_evidence_incomplete"
    result = await test_client.post(fixture.path + "/complete", json={"reviews": reviews}, headers=headers)
    assert result.status_code == 200 and result.json()["state"] == "succeeded", result.text
    assert len(result.json()["readback_ref"]["knowledge_qualification"]["evidence"]) == 2
    assert fixture.native.calls.count("create") == 2
