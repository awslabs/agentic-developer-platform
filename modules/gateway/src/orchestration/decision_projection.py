"""Bounded, tenant-scoped display evidence; the audit ledger is unchanged."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

CATEGORY = "adp_decision_category_v2(kind, actor_id, actor_kind, reason, rejection_reason)"
CLEARER = "adp_decision_clearer_v1(kind, actor_id, actor_kind)"
OBSERVATION = "kind = 'result_observed' AND (adp_decision_payload_flags_v1(reason) & 4) = 4"
COLUMNS = "id, node_id, kind, actor_id, actor_kind, reason, rejection_reason, created_at"


@dataclass(frozen=True)
class DisplayDecision:
    id: str
    node_id: str
    kind: str
    actor_id: str
    actor_kind: str
    reason: str | None
    rejection_reason: str | None
    created_at: datetime


def projection_sql(*, dispatch_only: bool = False, dialect: str = "postgresql") -> str:
    categories = "(2)" if dispatch_only else ", ".join(f"({category})" for category in range(1, 11))
    common = "org_id = :org_id AND flow_id = :flow_id AND node_id = selected.node_id"
    selectors = [("categories", f"{CATEGORY} = category.value AND {CATEGORY} IS NOT NULL")]
    if not dispatch_only:
        selectors.extend(
            [
                (None, OBSERVATION),
                (None, "kind = 'external_completion_accepted' AND actor_kind = 'human'"),
                ("clearers", f"{CLEARER} = category.value AND {CLEARER} IS NOT NULL"),
            ]
        )
    postgres = dialect == "postgresql"
    selected = "json_array_elements_text(CAST(:node_ids AS json))" if postgres else "value"
    source = "" if postgres else " FROM json_each(:node_ids)"
    order = 'created_at DESC, id COLLATE "C" DESC' if postgres else "created_at DESC, id COLLATE BINARY DESC"
    queries = []
    for category_table, predicate in selectors:
        category_join = f" CROSS JOIN {category_table} AS category" if category_table else ""
        if postgres:
            queries.append(f"""
                SELECT latest.* FROM selected {category_join}
                CROSS JOIN LATERAL (
                    SELECT {COLUMNS} FROM orchestration_decisions
                    WHERE {common} AND {predicate} ORDER BY {order} LIMIT 1
                ) latest
            """)
        else:
            queries.append(f"""
                SELECT {", ".join("latest." + column for column in COLUMNS.split(", "))}
                FROM selected {category_join}
                JOIN orchestration_decisions latest ON latest.id = (
                    SELECT id FROM orchestration_decisions
                    WHERE {common} AND {predicate} ORDER BY {order} LIMIT 1
                )
            """)
    return f"""WITH selected AS (SELECT {selected} AS node_id{source}),
        categories(value) AS (VALUES {categories}), clearers(value) AS (VALUES (1), (2))
        """ + " UNION ALL ".join(queries)


async def current_display_decisions(session, *, org_id: str, flow_id: str, node_ids: list[str], dispatch_only: bool = False):
    if not node_ids:
        return []
    dialect = session.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported decision projection dialect: {dialect}")
    if dialect == "sqlite":
        from src.orchestration.decision_sqlite import register

        connection = await session.connection()
        await connection.run_sync(register)
    rows = (
        (
            await session.execute(
                text(projection_sql(dispatch_only=dispatch_only, dialect=dialect)),
                {"org_id": org_id, "flow_id": flow_id, "node_ids": json.dumps(sorted(set(node_ids)))},
            )
        )
        .mappings()
        .all()
    )
    unique = {}
    for row in rows:
        values = dict(row)
        if isinstance(values["created_at"], str):
            values["created_at"] = datetime.fromisoformat(values["created_at"])
        unique[row["id"]] = DisplayDecision(**values)
    return sorted(unique.values(), key=lambda row: (row.created_at, row.id))
