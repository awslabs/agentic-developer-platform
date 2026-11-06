"""Add bounded repository refusal display without changing live V1 indexes.

Number 106 is reserved by the independently prepared OAuth scope migration.
This branch depends on the current main head; reconcile heads at integration.
"""

from importlib.resources import files

import sqlalchemy as sa

from alembic import op

revision = "107_repository_eval_display"
down_revision = "105_flow_cost_covering"
branch_labels = None
depends_on = None

INDEX = "ix_decision_display_category_v2"
CATEGORY = "adp_decision_category_v2(kind, actor_id, actor_kind, reason, rejection_reason)"
INDEXES = {INDEX: (CATEGORY, f"{CATEGORY} IS NOT NULL")}
# PostgreSQL's canonical definition includes the varchar-to-text casts. Refuse
# another object's name, schema, table, expression, predicate, or index options;
# an invalid index alone is not proof this migration owns it.
EXPRESSION = "adp_decision_category_v2((kind)::text, (actor_id)::text, (actor_kind)::text, reason, rejection_reason)"
DEFINITION = (
    f"CREATE INDEX {INDEX} ON public.orchestration_decisions USING btree "
    f'(org_id, flow_id, node_id, {EXPRESSION}, created_at DESC, id COLLATE "C" DESC) '
    f"WHERE ({EXPRESSION} IS NOT NULL)"
)


def _existing_index():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT current_schema()")) != "public":
        raise RuntimeError("Repository display migration requires the public schema")
    # Inspect every schema, rather than letting search_path silently select one
    # of several same-named relations. All mutative index SQL is schema-qualified.
    rows = (
        bind.execute(
            sa.text("""SELECT namespace.nspname AS schema, relation.relkind AS kind,
                indexes.indisvalid AS valid, indexes.indisready AS ready, indexes.indislive AS live,
                CASE WHEN relation.relkind = 'i' THEN pg_get_indexdef(relation.oid) END AS definition
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            LEFT JOIN pg_index AS indexes ON indexes.indexrelid = relation.oid
            WHERE relation.relname = :name"""),
            {"name": INDEX},
        )
        .mappings()
        .all()
    )
    if not rows:
        return None
    if len(rows) != 1 or rows[0]["schema"] != "public" or rows[0]["definition"] != DEFINITION:
        raise RuntimeError("Repository display index identity or definition differs; refusing to replace it")
    row = rows[0]
    if row["live"] is not True or (row["valid"] is True and row["ready"] is not True):
        raise RuntimeError("Repository display index is undergoing another operation")
    return row


def _index(*, drop=False):
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        settings = dict(
            bind.execute(sa.text("SELECT current_setting('lock_timeout') AS lock_timeout, current_setting('statement_timeout') AS statement_timeout"))
            .mappings()
            .one()
        )
        try:
            # Match migration 105: concurrent builds wait for ordinary writers
            # and old snapshots without blocking their reads/writes. One second
            # aborts normal traffic; both this wait and the statement stay bounded.
            op.execute("SET lock_timeout = '30s'")
            op.execute("SET statement_timeout = '5min'")
            existing = _existing_index()
            if existing is not None and (drop or existing["valid"] is False):
                op.execute(f'DROP INDEX CONCURRENTLY public."{INDEX}"')
                existing = None
            if not drop and existing is None:
                op.execute(f'''CREATE INDEX CONCURRENTLY "{INDEX}" ON public.orchestration_decisions
                    (org_id, flow_id, node_id, ({CATEGORY}), created_at DESC, id COLLATE "C" DESC)
                    WHERE {CATEGORY} IS NOT NULL''')
                observed = _existing_index()
                if observed is None or observed["valid"] is not True or observed["ready"] is not True:
                    raise RuntimeError("Repository display index build did not become valid and ready")
        finally:
            # Restore caller settings on success and on failed concurrent builds;
            # RESET would discard a nondefault session timeout supplied by a caller.
            bind.execute(
                sa.text("SELECT set_config('lock_timeout', :lock_timeout, false), set_config('statement_timeout', :statement_timeout, false)"),
                settings,
            )


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _existing_index()
    source = files("src.orchestration").joinpath("decision_repository_projection.sql").read_text()
    op.get_bind().exec_driver_sql(source)
    _index()


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _index(drop=True)
    op.execute("DROP FUNCTION IF EXISTS adp_decision_category_v2(text, text, text, text, text)")
