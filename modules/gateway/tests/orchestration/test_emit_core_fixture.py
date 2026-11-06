"""Shared emit-core fixture exercised through the gateway preview route."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.orchestration import test_registration as registration

app_with_router = registration.app_with_router
autonomy_default_unset = registration.autonomy_default_unset
session = registration.session
registrar = registration.registrar

FIXTURE = Path(__file__).parent / "fixtures" / "proposal.bounded.json"
TEMPLATE = Path(__file__).parents[4] / "modules/agent-factory/skills/_shared/policy.default.json"


def preview_document(filename: str) -> dict:
    document = json.loads((FIXTURE.parent / filename).read_text(encoding="utf-8"))
    policy = document.get("proposed_execution_policy")
    document["org_id"] = policy["org_id"] if policy else "example-org"
    if policy:
        policy["expires_at"] = (datetime.now(UTC) + timedelta(days=14)).isoformat()
    return document


def test_bounded_fixture_previews_with_visible_limits(app_with_router, autonomy_default_unset):
    document = preview_document("proposal.bounded.json")
    with registration.client_for(app_with_router, permitted=True) as client:
        response = client.post("/orchestration/flows/drafts/preview", json=document)
    assert response.status_code == 200
    body = response.json()
    assert body["would_register"] is True, body["violations"]
    assert body["execution_is_unbounded"] is False
    summary = body["proposed_execution_policy"]
    assert summary["repository_ids"] == ["example/repository"]
    assert summary["limits"]["max_spend_usd"] == "600"
    assert summary["limits"]["max_wall_clock_seconds"] == 1209600


@pytest.mark.parametrize(
    ("filename", "unbounded"),
    [("proposal.bounded.json", False), ("proposal.unbounded.json", True)],
)
def test_paired_emit_core_fixtures_preview_distinct_authority(app_with_router, autonomy_default_unset, filename, unbounded):
    document = preview_document(filename)
    with registration.client_for(app_with_router, permitted=True) as client:
        response = client.post("/orchestration/flows/drafts/preview", json=document)
    assert response.status_code == 200
    body = response.json()
    assert body["would_register"] is True, body["violations"]
    assert body["execution_is_unbounded"] is unbounded
    assert (body["proposed_execution_policy"] is None) is unbounded


def test_authored_check_refuses_omitted_eval_address():
    import importlib.util

    checker_path = Path(__file__).parents[4] / "modules/agent-factory/skills/_shared/check_proposal.py"
    spec = importlib.util.spec_from_file_location("check_proposal", checker_path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    policy = document["proposed_execution_policy"]
    for field in ("schema_version", "team_ids", "environment_connection_ids", "allowed_actions", "human_gates", "limits"):
        assert policy[field] == template[field]
    checker.check(document)
    document["proposed_execution_policy"]["evaluation_acceptance"] = {}
    with pytest.raises(ValueError, match="every eval address"):
        checker.check(document)


def test_fixture_binds_product_and_every_story():
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert document["product_id"] == "example-product"
    stories = [node for node in document["nodes"] if node["kind"] == "story"]
    assert stories
    for story in stories:
        assert story["repository_identity"] == "example/repository"
        assert story["provider_repository_id"] == 12345
        assert story["repository_connection"] == {"provider": "github", "installation_id": 67890}
        assert story["repository_identity"] in document["proposed_execution_policy"]["repository_ids"]


def test_authoring_check_refuses_missing_product_or_story_binding():
    import importlib.util

    checker_path = Path(__file__).parents[4] / "modules/agent-factory/skills/_shared/check_proposal.py"
    spec = importlib.util.spec_from_file_location("check_proposal", checker_path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document.pop("product_id")
    with pytest.raises(ValueError, match="product_id"):
        checker.check(document)
    document["product_id"] = "example-product"
    document["nodes"][0].pop("repository_connection")
    with pytest.raises(ValueError, match="verified repository binding"):
        checker.check(document)


async def test_product_story_has_no_repository_verification_hold_after_registration(session, registrar, monkeypatch):
    from sqlalchemy import select

    from src.orchestration.dispatch_pass import product_binding_hold
    from src.orchestration.models import OrchestrationAcceptedPlan
    from src.orchestration.proposal import LoopProposal
    from src.orchestration.registration import register_draft_proposal
    from tests.orchestration.product_fixture import seed_product

    monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "true")
    await registration.seed_org(session)
    await seed_product(session, org_id=registration.ORG_A, actor_id=registration.AGENT_USER_ID, product_id="example-product")
    document = preview_document("proposal.bounded.json")
    document["org_id"] = document["proposed_execution_policy"]["org_id"] = registration.ORG_A
    result, _ = await register_draft_proposal(session, LoopProposal.model_validate(document), registrar)
    assert await product_binding_hold(session, org_id=registration.ORG_A, flow_id=result.flow_id) is None

    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == result.flow_id))
    unbound = json.loads(json.dumps(plan.plan_document))
    next(node for node in unbound["nodes"] if node["kind"] == "story").pop("repository_connection")
    plan.plan_document = unbound
    await session.flush()
    assert await product_binding_hold(session, org_id=registration.ORG_A, flow_id=result.flow_id) == "repository_verification_pending"


def test_policyless_fixture_is_unbounded_and_authoring_refuses_it(app_with_router, autonomy_default_unset):
    import importlib.util

    checker_path = Path(__file__).parents[4] / "modules/agent-factory/skills/_shared/check_proposal.py"
    spec = importlib.util.spec_from_file_location("check_proposal", checker_path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    document = json.loads((FIXTURE.parent / "proposal.unbounded.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="unbounded previews"):
        checker.check(document)

    document["org_id"] = "example-org"
    with registration.client_for(app_with_router, permitted=True) as client:
        response = client.post("/orchestration/flows/drafts/preview", json=document)
    assert response.status_code == 200
    body = response.json()
    assert body["execution_is_unbounded"] is True
    assert body["proposed_execution_policy"] is None

    document["proposed_execution_policy"] = json.loads(FIXTURE.read_text(encoding="utf-8"))["proposed_execution_policy"]
    with pytest.raises(ValueError, match="preview is unbounded"):
        checker.check(document, body)
