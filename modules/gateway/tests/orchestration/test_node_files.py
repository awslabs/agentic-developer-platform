"""Story file plans are bounded display metadata, versioned with the proposal."""

import pytest
from pydantic import ValidationError

from src.orchestration.compile import plan_hash
from src.orchestration.node_files import project_file_groups
from src.orchestration.proposal import LoopProposal, ProposedNode
from tests.orchestration.test_proposal import make_proposal


def story(**overrides):
    return ProposedNode(address="demo-flow/epic-1/wave-1/story", kind="story", title="Story", **overrides)


def test_file_plan_round_trip_and_legacy_hash_compatibility():
    proposal = make_proposal()
    baseline = proposal.model_dump(mode="json")
    assert all("file_groups" not in node for node in baseline["nodes"])
    explicit = {**baseline, "nodes": [{**node, "file_groups": []} for node in baseline["nodes"]]}
    assert plan_hash(LoopProposal.model_validate(explicit)) == plan_hash(proposal)

    baseline["nodes"][0]["file_groups"] = [
        {
            "group": "API and storage",
            "purpose": "Persist configuration",
            "create": ["modules/service/api.py"],
            "modify": ["modules/gateway/src/app.py"],
            "modify_after": [{"path": "modules/service/models.py", "created_by": "SA02"}],
        }
    ]
    annotated = LoopProposal.model_validate(baseline)
    assert annotated.model_dump(mode="json") == baseline
    assert plan_hash(annotated) != plan_hash(proposal)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../file.py",
        "a/../file.py",
        "a//b",
        "./a",
        "a/",
        "a\\b",
        "a/*.py",
        "https://host/a",
        "a\nb",
        " a",
        "a" * 513,
    ],
)
def test_nonliteral_or_unbounded_paths_are_rejected(path):
    with pytest.raises(ValidationError):
        story(file_groups=[{"group": "API", "create": [path]}])


@pytest.mark.parametrize("path", ["app/[slug]/page.tsx", "pages/[...slug].tsx", "{{cookiecutter.project}}/main.py", "src/routes/(auth)/+page.svelte"])
def test_literal_framework_and_template_paths_are_supported(path):
    node = story(file_groups=[{"group": "Routes", "create": [path]}])
    assert node.model_dump(mode="json")["file_groups"][0]["create"] == [path]


@pytest.mark.parametrize(
    "groups",
    [
        [{"group": "Empty"}],
        [{"group": " ", "create": ["a.py"]}],
        [{"group": "API", "create": ["a.py"], "modify": ["a.py"]}],
        [{"group": "API", "create": ["a.py"]}, {"group": "API", "create": ["b.py"]}],
        [{"group": "API", "create": ["a.py"]}, {"group": "Tests", "modify_after": [{"path": "a.py", "created_by": "SA01"}]}],
        [{"group": str(i), "create": [f"file-{i}.py"]} for i in range(17)],
        [{"group": "API", "create": [f"a-{i}.py" for i in range(129)]}],
        [{"group": str(i), "create": [f"a-{i}-{j}.py" for j in range(100)]} for i in range(3)],
    ],
)
def test_invalid_or_unbounded_file_groups_are_rejected(groups):
    with pytest.raises(ValidationError):
        story(file_groups=groups)


@pytest.mark.parametrize("kind", ["eval", "gate", "fix"])
def test_file_groups_belong_to_stories(kind):
    with pytest.raises(ValidationError):
        ProposedNode(address="demo-flow/epic-1/wave-1/node", kind=kind, title="Node", file_groups=[{"group": "API", "create": ["a.py"]}])


def test_projection_omits_malformed_ambiguous_and_nonstory_entries():
    node = story(file_groups=[{"group": "API", "create": ["a.py"]}]).model_dump(mode="json")
    assert project_file_groups({"nodes": [node]})[node["address"]][0].create == ["a.py"]
    assert project_file_groups({"nodes": [node, node, node]}) == {}
    assert project_file_groups({"nodes": [None, {**node, "kind": "eval"}, {**node, "file_groups": "invalid"}]}) == {}
    assert project_file_groups({"nodes": None}) == {}


@pytest.mark.asyncio
async def test_current_plan_file_groups_reach_graph_and_removal_does_not_leave_stale_metadata(session, app_with_router):
    from src.orchestration.compile import upsert_nodes
    from src.orchestration.repository import OrchestrationRepository
    from tests.orchestration import test_read_api as read

    flow = await read.seed_flow(session)
    flow_id = flow.id
    proposal = make_proposal(flow_slug=flow.slug)
    payload = proposal.model_dump(mode="json")
    for node in payload["nodes"]:
        node["address"] = node["address"].replace("demo-flow/", f"{flow.slug}/")
    payload["nodes"][0]["file_groups"] = [{"group": "API", "create": ["src/api.py"]}]
    proposal = LoopProposal.model_validate(payload)
    repo = OrchestrationRepository(session)
    await upsert_nodes(repo, proposal=proposal, org_id=read.ORG_A, flow_id=flow_id)
    await repo.record_accepted_plan(org_id=read.ORG_A, flow_id=flow_id, plan_document=proposal.model_dump(mode="json"), plan_hash=plan_hash(proposal))
    await session.commit()
    session.expire_all()

    graph = read.client_for(app_with_router).get(read.route(flow_id)).json()
    assert read.node_by_ref(graph, "story-a")["file_groups"][0]["create"] == ["src/api.py"]
    assert read.node_by_ref(graph, "eval")["file_groups"] == []
    assert read.client_for(app_with_router, org_id=read.ORG_B).get(read.route(flow_id)).status_code == 404

    payload["nodes"][0].pop("file_groups")
    replacement = LoopProposal.model_validate(payload)
    await repo.record_accepted_plan(
        org_id=read.ORG_A, flow_id=flow_id, plan_document=replacement.model_dump(mode="json"), plan_hash=plan_hash(replacement)
    )
    await session.commit()
    session.expire_all()
    graph = read.client_for(app_with_router).get(read.route(flow_id)).json()
    assert read.node_by_ref(graph, "story-a")["file_groups"] == []
    versions = await repo.list_plan_versions(org_id=read.ORG_A, flow_id=flow_id)
    assert versions[0].plan_document["nodes"][0]["file_groups"][0]["create"] == ["src/api.py"]


# Share the real graph read harness, including tenant and table setup.
from tests.orchestration import test_read_api as _read  # noqa: E402

session = _read.session
app_with_router = _read.app_with_router
