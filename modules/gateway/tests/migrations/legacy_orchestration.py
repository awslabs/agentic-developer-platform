"""Seed historical orchestration schemas without depending on current ORM columns."""

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text


async def seed_flow_node(session, *, slug, node_ref, kind="story"):
    flow = SimpleNamespace(id=str(uuid4()), org_id="org-alpha")
    node = SimpleNamespace(id=str(uuid4()))
    await session.execute(
        text(
            "INSERT INTO orchestration_flows (id, org_id, slug, title, state, created_at) "
            "VALUES (:id, :org_id, :slug, :title, 'pending', CURRENT_TIMESTAMP)"
        ),
        {"id": flow.id, "org_id": flow.org_id, "slug": slug, "title": f"Flow {slug}"},
    )
    await session.execute(
        text(
            "INSERT INTO orchestration_nodes "
            "(id, org_id, flow_id, epic_ref, wave_ref, node_ref, kind, title, state, attempts, created_at) "
            "VALUES (:id, :org_id, :flow_id, 'epic-1', 'wave-1', :node_ref, :kind, :title, 'pending', 0, CURRENT_TIMESTAMP)"
        ),
        {"id": node.id, "org_id": flow.org_id, "flow_id": flow.id, "node_ref": node_ref, "kind": kind, "title": f"Node {node_ref}"},
    )
    return flow, node
