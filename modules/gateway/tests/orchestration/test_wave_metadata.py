"""Display metadata cannot change execution or leak between plans/tenants."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from src.orchestration.compile import plan_hash
from src.orchestration.draft_revision import draft_hash
from src.orchestration.planning import plan_from_draft
from src.orchestration.preview import derive_waves
from src.orchestration.proposal import EpicDisplay, EpicMetadata, LoopProposal, WaveDisplay, WaveMetadata, validate_proposal
from src.orchestration.registration import transform_for_registration
from src.orchestration.repository import OrchestrationRepository
from tests.orchestration import test_list_flows as listing
from tests.orchestration.test_planning import _inputs
from tests.orchestration.test_registration import gateless_proposal

session = listing.session
app_with_router = listing.app_with_router


def display(**overrides):
    return WaveMetadata(
        epic_ref="epic-1", wave_ref="wave-1", **{"title": "API and persistence", "description": "Build and qualify durable APIs.", **overrides}
    )


def test_legacy_hashes_and_metadata_round_trip():
    legacy = gateless_proposal()
    document = legacy.model_dump(mode="json", exclude={"wave_metadata", "epic_metadata"})
    expected_draft = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert draft_hash(LoopProposal.model_validate(document)) == expected_draft
    _, epic, wave, _ = legacy.nodes[0].address.split("/")
    metadata = WaveMetadata(epic_ref=epic, wave_ref=wave, title="Contracts", description="Freeze contracts.")
    described = legacy.model_copy(update={"wave_metadata": [metadata]})
    assert validate_proposal(described) == []
    assert plan_hash(described) == plan_hash(legacy)
    assert draft_hash(described) != expected_draft
    effective, _ = transform_for_registration(described)
    assert effective.wave_metadata == [metadata]
    assert LoopProposal.model_validate(effective.model_dump(mode="json")).wave_metadata == [metadata]
    preview = next(w for w in derive_waves(effective) if (w.epic_ref, w.wave_ref) == (epic, wave))
    assert (preview.title, preview.description) == (metadata.title, metadata.description)
    assert all(w.title is None for w in derive_waves(legacy))


@pytest.mark.parametrize("updates", [{"title": "  "}, {"title": "x" * 121}, {"description": " "}, {"description": "x" * 501}])
def test_blank_and_oversize_metadata_rejected(updates):
    with pytest.raises(ValidationError):
        display(**updates)


def test_unknown_and_duplicate_refs_are_rejected_by_shared_validator():
    proposal = gateless_proposal().model_copy(update={"wave_metadata": [display(), display()]})
    rules = [v.rule for v in validate_proposal(proposal)]
    assert "duplicate_wave_metadata" in rules
    unknown = proposal.model_copy(update={"wave_metadata": [display().model_copy(update={"epic_ref": "not-in-plan"})]})
    assert "unknown_wave_metadata" in [v.rule for v in validate_proposal(unknown)]


def test_hosted_planner_supplies_metadata_without_changing_authority():
    wave_text = WaveDisplay(title="Checkout performance", description="Reduce latency and evaluate conversion impact.")
    epic_text = EpicDisplay(title="Faster checkout", description="Customers abandon slow checkouts. Reduce latency while preserving conversion.")
    proposal = plan_from_draft(_inputs(wave_display=wave_text, epic_display=epic_text))
    legacy = plan_from_draft(_inputs())
    assert plan_hash(proposal) == plan_hash(legacy)
    assert legacy.wave_metadata[0].title == "Cut checkout latency"
    assert proposal.epic_metadata[0].description == epic_text.description
    effective, _ = transform_for_registration(proposal)
    assert validate_proposal(effective) == []
    assert len(effective.wave_metadata) == 1
    wave = derive_waves(effective)[0]
    assert wave.title == wave_text.title
    assert "evaluate" in wave.description
    assert effective.execution_policy is None


async def test_graph_and_list_read_current_metadata_with_epic_and_tenant_scope(session, app_with_router):
    flow = await listing.seed_flow(session, slug="readable-waves")
    await listing.seed_node(session, flow, node_ref="first")
    await listing.seed_node(session, flow, node_ref="second", epic="epic-2")
    repo = OrchestrationRepository(session)
    first = display()
    epic = EpicMetadata(epic_ref="epic-1", title="Durable APIs", description="Clients need dependable task submission.")
    second = first.model_copy(update={"epic_ref": "epic-2", "title": "Release acceptance"})
    await repo.record_accepted_plan(
        org_id=listing.ORG_A, flow_id=flow.id, plan_document={"wave_metadata": [display(title="Old name").model_dump()]}, plan_hash="old"
    )
    await repo.record_accepted_plan(
        org_id=listing.ORG_A,
        flow_id=flow.id,
        plan_document={"wave_metadata": [first.model_dump(), second.model_dump()], "epic_metadata": [epic.model_dump()]},
        plan_hash="new",
    )
    await session.commit()
    with listing.client_for(app_with_router) as client:
        graph = client.get(f"/orchestration/flows/{flow.id}")
        assert graph.status_code == 200, graph.text
        assert graph.json()["epic_metadata"] == [epic.model_dump()]
        assert graph.json()["wave_metadata"] == [first.model_dump(), second.model_dump()]
        response = client.get("/orchestration/flows")
        assert response.status_code == 200, response.text
        waves = response.json()["flows"][0]["waves"]
        assert {w["epic_ref"]: w["title"] for w in waves} == {"epic-1": first.title, "epic-2": second.title}
        assert all(w["description"] == first.description for w in waves)
    assert await repo.display_metadata_for_flows(org_id=listing.ORG_B, flow_ids=[flow.id]) == {}
    with listing.client_for(app_with_router, org_id=listing.ORG_B) as client:
        assert client.get(f"/orchestration/flows/{flow.id}").status_code == 404
        assert client.get("/orchestration/flows").json()["flows"] == []


@pytest.mark.parametrize("field,value", [("title", " "), ("title", "x" * 201), ("description", " "), ("description", "x" * 3001)])
def test_epic_display_rejects_blank_and_oversize_text(field, value):
    with pytest.raises(ValidationError):
        EpicDisplay.model_validate({"title": "Contracts", "description": "Freeze the shared contract.", field: value})


def test_epic_metadata_is_validated_and_excluded_from_execution_hash():
    legacy = gateless_proposal()
    epic_ref = legacy.nodes[0].address.split("/")[1]
    metadata = EpicMetadata(epic_ref=epic_ref, title="External tasks", description="Services need a durable task lifecycle without GitHub.")
    proposal = legacy.model_copy(update={"epic_metadata": [metadata]})
    assert validate_proposal(proposal) == []
    assert plan_hash(proposal) == plan_hash(legacy)
    assert draft_hash(proposal) != draft_hash(legacy)
    assert transform_for_registration(proposal)[0].epic_metadata == [metadata]
    duplicate = proposal.model_copy(update={"epic_metadata": [metadata, metadata]})
    assert "duplicate_epic_metadata" in [v.rule for v in validate_proposal(duplicate)]
    unknown = proposal.model_copy(update={"epic_metadata": [metadata.model_copy(update={"epic_ref": "missing"})]})
    assert "unknown_epic_metadata" in [v.rule for v in validate_proposal(unknown)]
