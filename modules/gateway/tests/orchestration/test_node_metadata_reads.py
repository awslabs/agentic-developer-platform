"""Accepted node metadata survives storage and reaches the flow graph read."""

import pytest

from src.orchestration.compile import upsert_nodes
from src.orchestration.proposal import LoopProposal, ProposedNode
from src.orchestration.repository import OrchestrationRepository
from tests.orchestration import test_read_api as read

session = read.session
app_with_router = read.app_with_router


@pytest.mark.asyncio
async def test_metadata_is_stored_and_read_from_the_flow_graph(session, app_with_router):
    flow = await read.seed_flow(session)
    flow_id = flow.id
    proposal = LoopProposal(
        flow_slug=flow.slug,
        title="Metadata flow",
        org_id=read.ORG_A,
        spec_revision="revision-1",
        nodes=[
            ProposedNode(
                address=f"{flow.slug}/epic-1/wave-1/story",
                kind="story",
                title="Story",
                lineage={"story_id": "EI-S02"},
                labels={"source": "issue"},
            ),
            ProposedNode(address=f"{flow.slug}/epic-1/wave-1/eval", kind="eval", title="Eval"),
        ],
    )
    repo = OrchestrationRepository(session)
    ids, created = await upsert_nodes(repo, proposal=proposal, org_id=read.ORG_A, flow_id=flow.id)
    assert created == 2
    await session.commit()
    session.expire_all()
    story_id = ids[proposal.nodes[0].address]
    stored = await repo.get_node(org_id=read.ORG_A, node_id=story_id)
    assert stored.lineage == proposal.nodes[0].lineage.model_dump(mode="json")
    assert stored.labels == {"source": "issue"}
    assert await repo.get_node(org_id=read.ORG_B, node_id=stored.id) is None

    response = read.client_for(app_with_router).get(read.route(flow_id))
    assert response.status_code == 200
    graph = response.json()
    story = read.node_by_ref(graph, "story")
    assert story["lineage"] == proposal.nodes[0].model_dump(mode="json")["lineage"]
    assert story["labels"] == proposal.nodes[0].labels
    evaluation = read.node_by_ref(graph, "eval")
    assert evaluation["lineage"] is None and evaluation["labels"] == {}

    replacement = proposal.model_copy(update={"nodes": [proposal.nodes[0].model_copy(update={"labels": {"source": "revision"}}), proposal.nodes[1]]})
    _, created = await upsert_nodes(repo, proposal=replacement, org_id=read.ORG_A, flow_id=flow_id)
    assert created == 0
    await session.commit()
    session.expire_all()
    assert (await repo.get_node(org_id=read.ORG_A, node_id=story_id)).labels == {"source": "revision"}
    assert read.node_by_ref(read.client_for(app_with_router).get(read.route(flow_id)).json(), "story")["labels"] == {"source": "revision"}
