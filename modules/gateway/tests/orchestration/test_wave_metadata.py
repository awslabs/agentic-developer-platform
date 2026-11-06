"""Display metadata cannot change execution or leak between plans/tenants."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.orchestration.compile import plan_hash
from src.orchestration.draft_revision import draft_hash
from src.orchestration.planning import plan_from_draft
from src.orchestration.preview import derive_waves
from src.orchestration.proposal import EpicDisplay, EpicMetadata, LoopProposal, WaveDisplay, WaveMetadata, validate_proposal
from src.orchestration.registration import transform_for_registration
from src.orchestration.repository import OrchestrationRepository
from src.products.models import Product
from tests.orchestration import test_list_flows as listing
from tests.orchestration.product_fixture import seed_product_membership
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


def test_declared_wave_order_sorts_rollups_and_keeps_undeclared_last():
    from src.orchestration.repository import FlowDisplayCounts, WaveAggregate, _in_declared_wave_order

    def rollup(wave: str) -> WaveAggregate:
        return WaveAggregate(flow_id="f", epic_ref="epic-1", wave_ref=wave, display_counts=FlowDisplayCounts())

    # Creation order (what the GROUP BY returns): dashboard first, then runtime, then a wave added by revision.
    rollups = [rollup("dashboard"), rollup("runtime"), rollup("targets"), rollup("kirocrew")]
    declared = [
        {"epic_ref": "epic-1", "wave_ref": "runtime"},
        {"epic_ref": "epic-1", "wave_ref": "kirocrew"},
        {"epic_ref": "epic-1", "wave_ref": "dashboard"},
    ]
    assert [w.wave_ref for w in _in_declared_wave_order(rollups, declared)] == ["runtime", "kirocrew", "dashboard", "targets"]
    # No metadata (legacy plan): creation order is preserved exactly.
    assert [w.wave_ref for w in _in_declared_wave_order(rollups, [])] == ["dashboard", "runtime", "targets", "kirocrew"]


async def test_flows_list_orders_waves_by_declared_plan_order_not_creation_or_name(session, app_with_router):
    """#7226: `dashboard` was created first and sorts first by name; the plan declares it third."""
    flow = await listing.seed_flow(session, slug="cos-runtime")
    await listing.seed_node(session, flow, node_ref="crew-proxy", wave="dashboard", created_offset=0)
    await listing.seed_node(session, flow, node_ref="crew-pod-class", wave="runtime", created_offset=1)
    await listing.seed_node(session, flow, node_ref="kirocrew-image", wave="kirocrew", created_offset=2)
    repo = OrchestrationRepository(session)
    order = ["runtime", "kirocrew", "dashboard"]
    await repo.record_accepted_plan(
        org_id=listing.ORG_A,
        flow_id=flow.id,
        plan_document={"wave_metadata": [display().model_copy(update={"wave_ref": ref, "title": f"Wave {ref}"}).model_dump() for ref in order]},
        plan_hash="declared",
    )
    await session.commit()
    page = await repo.list_flows_page_with_aggregates(org_id=listing.ORG_A)
    assert [w.wave_ref for w in page.flows[0].waves] == order
    assert page.flows[0].current_wave_ref == "runtime"
    with listing.client_for(app_with_router) as client:
        body = client.get("/orchestration/flows").json()["flows"][0]
        assert [w["wave_ref"] for w in body["waves"]] == order
        assert body["current_wave_ref"] == "runtime"
        assert [w["title"] for w in body["waves"]] == [f"Wave {ref}" for ref in order]


async def test_reviewed_amendment_updates_wave_order_only_after_exact_acceptance(session, app_with_router):
    from src.orchestration.proposal import ProposedEdge, ProposedNode
    from tests.orchestration import test_amendment_review_routes as review

    flow_id = await review.accepted_flow(session)
    draft = await review.pending_draft(session, flow_id, extra_gate=False)
    proposal = review.amended_proposal(extra_gate=False)
    input_address = review.address("inputs", wave="wave-0")
    proposal.nodes.append(ProposedNode(address=input_address, kind="gate", title="Authorize inputs"))
    proposal.edges.append(ProposedEdge(from_address=input_address, to_address=review.address("story-b")))
    order = ["wave-0", "wave-1", "wave-2"]
    proposal.wave_metadata = [display(title=f"Reviewed {wave_ref}").model_copy(update={"wave_ref": wave_ref}) for wave_ref in order]

    with listing.client_for(app_with_router) as reader, review.client_for(app_with_router) as reviewer:
        before = reader.get("/orchestration/flows").json()["flows"][0]
        reviewed_before_edit = review.exact_accept_body(reviewer, flow_id, draft.draft_id)
        edited = reviewer.post(review.draft_route(flow_id, draft.draft_id, "edit"), json={"proposal": proposal.model_dump(mode="json")})
        assert edited.status_code == 200, edited.text
        revision_id = edited.json()["draft_id"]
        preview = reviewer.post(review.draft_route(flow_id, revision_id, "preview"))
        assert preview.status_code == 200, preview.text
        assert preview.json()["acceptable"] is True
        pending = reader.get("/orchestration/flows").json()["flows"][0]
        assert pending["waves"] == before["waves"]
        assert pending["current_wave_ref"] == before["current_wave_ref"]

        stale = reviewer.post(review.draft_route(flow_id, draft.draft_id, "accept"), json=reviewed_before_edit)
        assert stale.status_code == 409, stale.text
        assert reader.get("/orchestration/flows").json()["flows"][0]["waves"] == before["waves"]

        accepted = reviewer.post(review.draft_route(flow_id, revision_id, "accept"), json=review.exact_accept_body(reviewer, flow_id, revision_id))
        assert accepted.status_code == 200, accepted.text
        after = reader.get("/orchestration/flows").json()["flows"][0]
        assert [wave["wave_ref"] for wave in after["waves"]] == order
        assert [wave["title"] for wave in after["waves"]] == [f"Reviewed {wave_ref}" for wave_ref in order]
        assert after["current_wave_ref"] == "wave-0"


async def test_graph_projects_product_epic_and_distinct_accepted_story_targets(session, app_with_router, monkeypatch):
    from src.shared.identity import product_scope

    flow = await listing.seed_flow(session, slug="product-delivery")
    flow.product_id = "product-a"
    flow.tracker_refs = [
        {
            "scope": "epic",
            "address": "product-delivery/epic-1",
            "issue": {"provider": "github", "repository_id": 30, "repository": "planning/epics", "installation_id": 3, "issue_number": 21},
        }
    ]
    await seed_product_membership(session, org_id=listing.ORG_A, user_id=listing.USER_ID)
    session.add(
        Product(
            team_id=f"product-team-{listing.ORG_A}",
            tenant_id=listing.ORG_A,
            id=flow.product_id,
            name="Checkout",
            purpose="Improve checkout",
            intended_users=[],
            owner_user_id=listing.USER_ID,
            starting_point="blank",
            created_by=listing.USER_ID,
        )
    )
    first = await listing.seed_node(session, flow, node_ref="api")
    second = await listing.seed_node(session, flow, node_ref="web")
    for node, repository, repository_id, issue in ((first, "delivery/api", 11, "#42"), (second, "delivery/web", 12, "43")):
        node.repository_identity = repository
        node.provider_repository_id = repository_id
        node.repository_connection = {"provider": "github", "installation_id": 3}
        node.issue_ref = issue
    await OrchestrationRepository(session).record_accepted_plan(
        org_id=listing.ORG_A,
        flow_id=flow.id,
        plan_hash="accepted-product-targets",
        plan_document={
            "product_id": flow.product_id,
            "nodes": [
                {
                    "address": f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}",
                    "kind": "story",
                    "issue_ref": node.issue_ref,
                    "repository_identity": node.repository_identity,
                    "provider_repository_id": node.provider_repository_id,
                    "repository_connection": node.repository_connection,
                }
                for node in (first, second)
            ],
        },
    )
    await session.commit()

    async def member(*_args, **_kwargs):
        return SimpleNamespace(id=listing.USER_ID, team_id=None)

    monkeypatch.setattr(product_scope, "product_member", member)

    with listing.client_for(app_with_router) as client:
        response = client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
        assert response.status_code == 200, response.text
        graph = response.json()
        assert (graph["product_id"], graph["product_name"], graph["product_status"]) == ("product-a", "Checkout", "accessible")
        assert graph["planning_epics"] == flow.tracker_refs
        by_ref = {node["node_ref"]: node for node in graph["nodes"]}
        target_fields = ("repository_identity", "provider_repository_id", "repository_status", "issue_url")
        targets = [tuple(by_ref[ref][field] for field in target_fields) for ref in ("api", "web")]
        assert targets == [
            ("delivery/api", 11, "verified", "https://github.com/delivery/api/issues/42"),
            ("delivery/web", 12, "verified", "https://github.com/delivery/web/issues/43"),
        ]
        assert all(node["planning_epic"] == flow.tracker_refs[0]["issue"] for node in by_ref.values())
    with listing.client_for(app_with_router, org_id=listing.ORG_B) as client:
        assert client.get(f"/orchestration/flows/{flow.id}?include_activity=false").status_code == 404


async def test_graph_keeps_unavailable_product_unbound_target_and_existing_hold_separate(session, app_with_router):
    flow = await listing.seed_flow(session, slug="missing-binding")
    flow.product_id = "missing-product"
    node = await listing.seed_node(session, flow, node_ref="unbound")
    node.issue_ref = "42"
    await OrchestrationRepository(session).record_accepted_plan(
        org_id=listing.ORG_A,
        flow_id=flow.id,
        plan_hash="unbound-plan",
        plan_document={
            "product_id": flow.product_id,
            "nodes": [{"address": f"{flow.slug}/epic-1/wave-1/unbound", "kind": "story", "issue_ref": "42"}],
        },
    )
    await session.commit()
    with listing.client_for(app_with_router) as client:
        response = client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
        assert response.status_code == 200, response.text
        graph = response.json()
        assert (graph["product_id"], graph["product_name"], graph["product_status"]) == ("missing-product", None, "unavailable")
        story = graph["nodes"][0]
        assert story["repository_status"] == "unbound"
        assert story["readiness_hold"] == "repository_verification_pending"
        assert story["issue_url"] is None


async def test_graph_reports_created_direct_story_without_claiming_native_epic_link(session, app_with_router):
    from src.orchestration.models import OrchestrationAmendmentIssueBinding

    flow = await listing.seed_flow(session, slug="direct-story")
    flow.product_id = "product-a"
    node = await listing.seed_node(session, flow, node_ref="direct")
    node.repository_identity = "delivery/api"
    node.provider_repository_id = 11
    node.repository_connection = {"provider": "github", "installation_id": 3}
    node.issue_ref = "42"
    await OrchestrationRepository(session).record_accepted_plan(
        org_id=listing.ORG_A,
        flow_id=flow.id,
        plan_hash="direct-plan",
        plan_document={
            "product_id": flow.product_id,
            "nodes": [
                {
                    "address": "direct-story/epic-1/wave-1/direct",
                    "kind": "story",
                    "issue_ref": "42",
                    "repository_identity": "delivery/api",
                    "provider_repository_id": 11,
                    "repository_connection": node.repository_connection,
                    "proposal_identity": "p:direct",
                }
            ],
        },
    )
    session.add(
        OrchestrationAmendmentIssueBinding(
            org_id=listing.ORG_A,
            repository="delivery/api",
            proposal_identity="p:direct",
            provider_repository_id=11,
            parent_repository_id=None,
            parent_issue_id=None,
            link_confirmed_at=None,
            issue_number=42,
            state="created",
            claim_token="claim-direct",
            content_hash="hash",
        )
    )
    await session.commit()

    with listing.client_for(app_with_router) as client:
        response = client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
        assert response.status_code == 200, response.text
        graph = response.json()
        assert graph["planning_epics"] == []
        direct = graph["nodes"][0]
        assert direct["planning_epic"] is None
        assert direct["repository_status"] == "verified"
        assert direct["issue_publication_status"] == "issue_created"
        assert direct["issue_url"] == "https://github.com/delivery/api/issues/42"
        assert direct["readiness_hold"] != "amendment_issue_link_unconfirmed"


@pytest.mark.parametrize("refusal_code", ["repository_not_permitted", "connection_revoked"])
async def test_graph_distinguishes_created_issue_pending_link_from_confirmed_link_and_stale_projection(session, app_with_router, refusal_code):
    from src.orchestration.models import OrchestrationAmendmentIssueBinding

    flow = await listing.seed_flow(session, slug="native-links")
    flow.product_id = "product-a"
    flow.tracker_refs = [
        {
            "scope": "epic",
            "address": "native-links/epic-1",
            "issue": {
                "provider": "github",
                "repository": "planning/epics",
                "repository_id": 30,
                "installation_id": 3,
                "issue_number": 21,
            },
        }
    ]
    node = await listing.seed_node(session, flow, node_ref="new")
    node.repository_identity = "delivery/api"
    node.provider_repository_id = 11
    node.repository_connection = {"provider": "github", "installation_id": 3}
    node.issue_ref = "42"
    await OrchestrationRepository(session).record_accepted_plan(
        org_id=listing.ORG_A,
        flow_id=flow.id,
        plan_hash="published-plan",
        plan_document={
            "product_id": flow.product_id,
            "tracker_refs": flow.tracker_refs,
            "nodes": [
                {
                    "address": "native-links/epic-1/wave-1/new",
                    "kind": "story",
                    "issue_ref": "42",
                    "repository_identity": "delivery/api",
                    "provider_repository_id": 11,
                    "repository_connection": node.repository_connection,
                    "proposal_identity": "p:new",
                }
            ],
        },
    )
    binding = OrchestrationAmendmentIssueBinding(
        org_id=listing.ORG_A,
        repository="delivery/api",
        proposal_identity="p:new",
        provider_repository_id=11,
        parent_repository_id=30,
        parent_issue_id=210,
        issue_number=42,
        state="created",
        claim_token="claim-1",
        content_hash="hash",
    )
    session.add(binding)
    await session.commit()
    with listing.client_for(app_with_router) as client:
        path = f"/orchestration/flows/{flow.id}?include_activity=false"
        pending = client.get(path).json()["nodes"][0]
        assert pending["repository_status"] == "verified"
        assert pending["issue_publication_status"] == "issue_created_link_pending"
        assert pending["readiness_hold"] == "amendment_issue_link_unconfirmed"
        binding.link_confirmed_at = listing.BASE_TIME
        await session.commit()
        linked = client.get(path).json()["nodes"][0]
        assert linked["issue_publication_status"] == "linked"
        assert linked["readiness_hold"] == "repository_execution_withheld"
        binding.issue_number = 44
        await session.commit()
        unconfirmed = client.get(path).json()["nodes"][0]
        assert unconfirmed["issue_publication_status"] == "unconfirmed"
        assert unconfirmed["issue_url"] is None
        binding.issue_number = 42
        await session.commit()
        node.repository_identity = "delivery/other"
        await session.commit()
        stale = client.get(path).json()["nodes"][0]
        assert stale["repository_status"] == "stale"
        assert stale["issue_url"] is None

        from src.orchestration.dispatch_pass import _admission_refused, _record_admission_refusal

        node.repository_identity = "delivery/api"
        node.state = "ready"
        await _record_admission_refusal(
            session,
            node_id=node.id,
            org_id=listing.ORG_A,
            flow_id=flow.id,
            admission=_admission_refused(refusal_code, owner="flow-owner", required_input="Review policy", detail="not permitted"),
            scope={"attempt": 0, "accepted_plan_version": 1, "policy_hash": None},
        )
        await session.commit()
        denied = client.get(path).json()["nodes"][0]
        assert denied["delivery_progress"]["blocker"] == refusal_code
        assert denied["repository_status"] == "denied"
        assert denied["issue_url"] is None
