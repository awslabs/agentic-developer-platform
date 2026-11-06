"""An included archived stall is not active attention."""

from datetime import UTC, datetime

from src.orchestration.models import OrchestrationNode
from tests.orchestration import test_flow_archive as archive_fixtures
from tests.orchestration import test_list_flows as list_fixtures
from tests.orchestration.test_controls import ORG_A, seed_flow

session = archive_fixtures.session
app_with_router = list_fixtures.app_with_router


async def test_archived_stall_is_not_attention_or_needs_me(session, app_with_router):
    flow = await seed_flow(session, slug="archived-stall")
    flow.archived_at = datetime.now(UTC)
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="gate",
            kind="gate",
            title="Rejected",
            state="rejected_at_gate",
        )
    )
    await session.commit()
    client = list_fixtures.client_for(app_with_router)
    included = client.get("/orchestration/flows", params={"include_archived": "true"})
    assert included.status_code == 200, included.text
    assert included.json()["status_counts"]["attention_needed"] == 0
    assert included.json()["status_counts"]["archived"] == 1
    assert included.json()["flows"][0]["stalled_count"] == 0
    assert included.json()["flows"][0]["display_counts"]["stalled"] == 1
    needs_me = client.get("/orchestration/flows", params={"include_archived": "true", "needs_me": "true"})
    assert needs_me.status_code == 200, needs_me.text
    assert needs_me.json()["total"] == 0
