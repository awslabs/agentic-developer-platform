"""Install qualification functions and recoverable concurrent display indexes."""

from importlib.resources import files

import sqlalchemy as sa

from alembic import op

revision = "104_decision_display_projection"
down_revision = "103_dev_box_consolidation"
branch_labels = None
depends_on = None

CATEGORY = "adp_decision_category_v1(kind, actor_id, actor_kind, reason, rejection_reason)"
CLEARER = "adp_decision_clearer_v1(kind, actor_id, actor_kind)"
INDEXES = {
    "ix_decision_display_category": (CATEGORY, f"{CATEGORY} IS NOT NULL"),
    "ix_decision_display_clearer": (CLEARER, f"{CLEARER} IS NOT NULL"),
    "ix_decision_display_observed": (None, "kind = 'result_observed' AND (adp_decision_payload_flags_v1(reason) & 4) = 4"),
}


def _indexes(*, drop: bool) -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '1s'")
        op.execute("SET statement_timeout = '5min'")
        try:
            for name, (expression, predicate) in INDEXES.items():
                valid = op.get_bind().scalar(sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"), {"name": name})
                if drop or valid is False:
                    op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
                if not drop and valid is not True:
                    category = f"({expression}), " if expression else ""
                    op.execute(f'''CREATE INDEX CONCURRENTLY "{name}" ON orchestration_decisions
                        (org_id, flow_id, node_id, {category}created_at DESC, id COLLATE "C" DESC)
                        WHERE {predicate}''')
        finally:
            op.execute("RESET lock_timeout")
            op.execute("RESET statement_timeout")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    source = files("src.orchestration").joinpath("decision_payload_qualification.sql").read_text()
    for statement in source.split("$function$;"):
        if statement.strip():
            op.get_bind().exec_driver_sql(statement + "$function$;")
    _indexes(drop=False)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _indexes(drop=True)
    op.execute("DROP FUNCTION IF EXISTS adp_decision_category_v1(text, text, text, text, text)")
    op.execute("DROP FUNCTION IF EXISTS adp_decision_clearer_v1(text, text, text)")
    op.execute("DROP FUNCTION IF EXISTS adp_decision_payload_flags_v1(text)")
