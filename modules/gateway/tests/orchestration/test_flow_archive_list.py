"""Public archive list filters and chips use the same persisted population."""

from datetime import UTC, datetime

from src.orchestration.models import OrchestrationNode
from src.orchestration.repository import OrchestrationRepository
from tests.orchestration import test_flow_archive as archive_fixtures
from tests.orchestration import test_list_flows as list_fixtures
from tests.orchestration.test_controls import ORG_A, seed_flow

session = archive_fixtures.session
app_with_router = list_fixtures.app_with_router


async def test_list_hides_archived_by_default(session, app_with_router):
    archived = await seed_flow(session, slug="rejected-plan")
    archived.archived_at = datetime.now(UTC)
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=archived.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="gate",
            kind="gate",
            title="Declined",
            state="rejected_at_gate",
        )
    )
    running = await seed_flow(session, slug="running-plan")
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=running.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="story",
            kind="story",
            title="Running",
            state="running",
        )
    )
    await session.commit()
    client = list_fixtures.client_for(app_with_router)
    route = "/orchestration/flows"

    default = client.get(route)
    assert default.status_code == 200, default.text
    assert [flow["id"] for flow in default.json()["flows"]] == [running.id]
    assert default.json()["total"] == 1
    assert default.json()["status_counts"]["archived"] == 0
    assert default.json()["status_counts"]["attention_needed"] == 0

    included = client.get(route, params={"include_archived": "true"})
    assert included.status_code == 200, included.text
    assert {flow["id"] for flow in included.json()["flows"]} == {running.id, archived.id}
    assert included.json()["total"] == 2
    assert included.json()["status_counts"]["archived"] == 1
    assert included.json()["status_counts"]["attention_needed"] == 0
    archived_row = next(flow for flow in included.json()["flows"] if flow["id"] == archived.id)
    assert archived_row["status"] == "archived"
    assert archived_row["stalled_count"] == 0
    assert archived_row["display_counts"]["stalled"] == 1
    pages = [client.get(route, params={"include_archived": "true", "limit": 1, "offset": offset}).json() for offset in range(2)]
    assert {page["flows"][0]["id"] for page in pages} == {running.id, archived.id}
    assert all(page["total"] == 2 for page in pages)
    assert client.get(route, params={"limit": 1, "offset": 1}).json()["total"] == 0
    assert client.get(route, params={"limit": 1, "offset": 1}).json()["flows"] == []

    only_archived = client.get(route, params={"status": "archived"})
    assert only_archived.status_code == 200, only_archived.text
    assert [flow["id"] for flow in only_archived.json()["flows"]] == [archived.id]
    assert only_archived.json()["status_counts"]["archived"] == 1
    assert client.get(route, params={"include_archived": "true", "status": "attention_needed"}).json()["total"] == 0
    assert client.get(route, params={"include_archived": "true", "needs_me": "true"}).json()["total"] == 0
    assert client.get(route, params={"status": "archived", "needs_me": "true"}).json()["total"] == 0


async def test_mixed_flows_status_chips_follow_archive_population(session, app_with_router):
    archived = await seed_flow(session, slug="superseded-plan")
    archived.archived_at = datetime(2026, 10, 13, 12, tzinfo=UTC)
    running = await seed_flow(session, slug="live-plan")
    gated = await seed_flow(session, slug="approval-plan")
    for flow, node_ref, state in (
        (archived, "rejected", "rejected_at_gate"),
        (running, "running", "running"),
        (gated, "gate", "awaiting_gate"),
    ):
        session.add(
            OrchestrationNode(
                org_id=ORG_A,
                flow_id=flow.id,
                epic_ref="epic",
                wave_ref="wave",
                node_ref=node_ref,
                kind="gate" if state in {"rejected_at_gate", "awaiting_gate"} else "story",
                title=node_ref,
                state=state,
            )
        )
    await session.commit()
    client = list_fixtures.client_for(app_with_router)
    path = "/orchestration/flows"

    default = client.get(path)
    assert default.status_code == 200, default.text
    assert {row["id"]: row["status"] for row in default.json()["flows"]} == {
        running.id: "running",
        gated.id: "awaiting_you",
    }
    assert default.json()["total"] == 2
    assert {status: default.json()["status_counts"][status] for status in ("archived", "running", "awaiting_you", "attention_needed")} == {
        "archived": 0,
        "running": 1,
        "awaiting_you": 1,
        "attention_needed": 0,
    }
    assert sum(default.json()["status_counts"].values()) == 2

    included = client.get(path, params={"include_archived": "true"})
    assert included.status_code == 200, included.text
    assert {row["id"]: row["status"] for row in included.json()["flows"]} == {
        archived.id: "archived",
        running.id: "running",
        gated.id: "awaiting_you",
    }
    assert included.json()["total"] == 3
    assert included.json()["status_counts"]["archived"] == 1
    assert included.json()["status_counts"]["attention_needed"] == 0
    assert sum(included.json()["status_counts"].values()) == 3

    archived_only = client.get(path, params={"status": "archived"})
    assert archived_only.status_code == 200, archived_only.text
    assert [(row["id"], row["status"]) for row in archived_only.json()["flows"]] == [(archived.id, "archived")]
    assert archived_only.json()["total"] == 1
    assert archived_only.json()["status_counts"] == included.json()["status_counts"]

    needed = client.get(path, params={"include_archived": "true", "needs_me": "true"})
    assert needed.status_code == 200, needed.text
    assert [row["id"] for row in needed.json()["flows"]] == [gated.id]
    assert needed.json()["status_counts"] == included.json()["status_counts"]
    live_status = client.get(path, params={"include_archived": "true", "status": "running"})
    assert [row["id"] for row in live_status.json()["flows"]] == [running.id]
    assert live_status.json()["status_counts"] == included.json()["status_counts"]


async def test_archived_slug_remains_reserved(session):
    archived = await seed_flow(session, slug="reserved-archive")
    archived.archived_at = datetime.now(UTC)
    await session.commit()
    repo = OrchestrationRepository(session)
    assert (await repo.get_flow_by_slug(org_id=ORG_A, slug="reserved-archive")).id == archived.id
    assert [flow.id for flow in await repo.list_flows(org_id=ORG_A)] == [archived.id]
    winner = await repo.create_flow(org_id=ORG_A, slug="reserved-archive", title="Another flow")
    assert winner.id == archived.id
    await session.commit()
