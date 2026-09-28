"""Add client_tool to usage_logs — which tool made each request.

Issue #4398 (EPIC #4324, FR-6.1–6.3): capture the client tool (Claude Code,
Codex CLI, Cursor, web chat, …) on the cost record.

**Why this ships before anything reads it.** Nothing in v1 consumes this column
(FR-6.3: no UI, no read endpoint). It is filed early because the data is **not
back-fillable** — the client identifier exists only at request time, so every day
the column is absent is a day of history that can never be attributed to a tool.
A later retrofit could only guess.

**Follows 018/031's contract, NOT the adjacent 025's.** `ADD COLUMN ... NULL`,
**no** `server_default`, plus a partial index `WHERE client_tool IS NOT NULL`.
025 is `NOT NULL` + `server_default`, where the default IS the backfill. Copying
that neighbour here would stamp a fabricated tool name onto every historical
usage row, and a fabricated value is strictly worse than a null one: null reads
as "not captured", while a fabricated one reads as a real tool and lands those
rows in some future per-tool breakdown as though they belonged there. That is the
exact "`NULL` treated as unknown-tool" failure the issue's impact table names.

**Explicit no-backfill contract: existing rows stay NULL.** There is no `UPDATE`
statement anywhere in `upgrade()`, and that absence is the contract, not an
oversight. `tests/migrations/test_033_client_tool_capture.py` asserts pre-existing
rows come out byte-identical.

Nullability is also what keeps the usage hot path writable *during* rollout. Pods
running the pre-033 image INSERT without this column; a `NOT NULL` column with no
default would fail every one of those in-flight INSERTs. And because `_log_usage`
deliberately swallows exceptions, that failure would surface as HTTP 200 with **no
usage row at all** — unmetered and unbilled, with no alarm. 028 and 031 both
record this same reasoning; it applies here too, on a populated table.

**Width.** `VARCHAR(32)` because the persisted values are a small closed set of
short identifiers (`claude_code`, `codex_cli`, …) — see `src/proxy/client_tool.py`.
A raw `User-Agent` is never stored, so the 255/512 widths used by free-text
columns elsewhere in this table would be misleading about what the column holds.

The partial index is 018/031's shape: a future per-tool rollup only ever scans
captured rows, and every historical row is null, so indexing the nulls would pay
for millions of entries no query will read. Both `postgresql_where` (where this
runs) and `sqlite_where` (where it is tested) are given — 018 used a raw
Postgres-only `CREATE INDEX ... WHERE`, which is not executable under the
SQLite-backed migration test this unit is required to ship.

**Revision numbering — resolved at implementation time, not hard-coded.** The
head set was computed across every file in `alembic/versions/` and contained
exactly one entry: revision id `032_budget_usage_org_type`, in the file
`032_budget_usage_org_entity_type.py`. Note the id differs from the filename;
`down_revision` must name the **id**, and chaining onto the filename would be a
dangling reference. A stale or wrong `down_revision` creates a SECOND HEAD, and
`alembic upgrade head` then fails outright for **everyone** — blocking every
subsequent gateway deploy, not just this feature. Asserted executably by
`test_033_client_tool_capture.py::TestRevisionChain`.

Revision id is 25 chars, inside the `alembic_version.version_num` VARCHAR(32)
ceiling that `tests/migrations/test_revision_id_length.py` guards (#4123).

Revision ID: 033_client_tool_capture
Revises: 032_budget_usage_org_type
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "033_client_tool_capture"
down_revision: str | None = "032_budget_usage_org_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A closed set of short snake_case identifiers, never a raw User-Agent.
# Kept in sync with src/proxy/client_tool.py by a model-parity test.
_CLIENT_TOOL_LEN = 32


def upgrade() -> None:
    """Add the nullable client_tool column and its partial index.

    No `UPDATE` statement anywhere in this function: that absence IS the
    no-backfill contract. Pre-existing rows keep a NULL client_tool, which
    correctly reads as "not captured".
    """
    op.add_column(
        "usage_logs",
        sa.Column("client_tool", sa.String(length=_CLIENT_TOOL_LEN), nullable=True),
    )
    # Partial index (018/031's shape): only captured rows are ever scanned by a
    # per-tool rollup, and every historical row is null.
    op.create_index(
        "ix_usage_logs_client_tool",
        "usage_logs",
        ["client_tool"],
        postgresql_where=sa.text("client_tool IS NOT NULL"),
        sqlite_where=sa.text("client_tool IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the index and column.

    Exercised by the migration test rather than assumed to work. Safe by
    construction: the column is nullable, nothing was backfilled, and no existing
    row was modified, so dropping it cannot lose data that predates the migration.

    **This is NOT the rollback plan for this change.** Per the issue and
    `delivery-plan.md`, rollback is "revert the PR to stop population" and
    deliberately does **not** auto-downgrade: the column is additive and nullable,
    so leaving it costs nothing, while running this function drops captured data
    that cannot be re-derived. It exists so `alembic downgrade` can walk past this
    revision, not as an operational step anyone should reach for.
    """
    op.drop_index("ix_usage_logs_client_tool", table_name="usage_logs")
    op.drop_column("usage_logs", "client_tool")
