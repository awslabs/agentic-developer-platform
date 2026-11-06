"""Transactionally invalidate shared flow display projections, including tick writes."""

import sqlalchemy as sa

from alembic import op

revision = "143_flow_read_revisions"
down_revision = "142_conversation_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "orchestration_read_revisions",
        sa.Column("org_id", sa.String(255), primary_key=True),
        sa.Column("flow_id", sa.String(255), primary_key=True),
        sa.Column("revision", sa.String(36), nullable=False),
    )
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite readers never publish shared structural projections.
    # Statement triggers update once per affected flow, including bulk SQL
    # and Lambda writers. UUIDs cannot collide with old Redis keys after a row is
    # deleted or the additive migration is rolled back and installed again.
    flow_key = "CASE WHEN TG_TABLE_NAME = 'orchestration_flows' THEN to_jsonb(r)->>'id' ELSE COALESCE(to_jsonb(r)->>'flow_id', '*') END"
    new_rows = f"SELECT org_id, {flow_key} AS flow_id FROM new_rows r"
    old_rows = f"SELECT org_id, {flow_key} AS flow_id FROM old_rows r"
    for operation, selection in (("insert", new_rows), ("delete", old_rows), ("update", f"{new_rows} UNION {old_rows}")):
        op.execute(f"""
            CREATE FUNCTION adp_flow_revision_{operation}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO orchestration_read_revisions(org_id, flow_id, revision)
                SELECT org_id, flow_id, gen_random_uuid()::text
                FROM ({selection}) affected WHERE org_id IS NOT NULL
                GROUP BY org_id, flow_id ORDER BY org_id, flow_id
                ON CONFLICT (org_id, flow_id) DO UPDATE SET revision = EXCLUDED.revision;
                RETURN NULL;
            END $$
        """)
    op.execute("""
        CREATE FUNCTION adp_flow_revision_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE orchestration_read_revisions SET revision = gen_random_uuid()::text;
            RETURN NULL;
        END $$
    """)
    inspector = sa.inspect(bind)
    for table in inspector.get_table_names():
        if not table.startswith("orchestration_") or table == "orchestration_read_revisions":
            continue
        if not any(column["name"] == "org_id" for column in inspector.get_columns(table)):
            continue
        quoted = bind.dialect.identifier_preparer.quote(table)
        for operation, transition in (
            ("insert", "NEW TABLE AS new_rows"),
            ("delete", "OLD TABLE AS old_rows"),
            ("update", "OLD TABLE AS old_rows NEW TABLE AS new_rows"),
        ):
            op.execute(f"""
                CREATE TRIGGER adp_flow_revision_{operation} AFTER {operation.upper()} ON {quoted}
                REFERENCING {transition} FOR EACH STATEMENT
                EXECUTE FUNCTION adp_flow_revision_{operation}()
            """)
        op.execute(f"""
            CREATE TRIGGER adp_flow_revision_truncate AFTER TRUNCATE ON {quoted}
            FOR EACH STATEMENT EXECUTE FUNCTION adp_flow_revision_truncate()
        """)
    op.execute("""
        INSERT INTO orchestration_read_revisions(org_id, flow_id, revision)
        SELECT org_id, id, gen_random_uuid()::text FROM orchestration_flows
    """)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for operation in ("insert", "delete", "update", "truncate"):
            op.execute(f"DROP FUNCTION IF EXISTS adp_flow_revision_{operation}() CASCADE")
    op.drop_table("orchestration_read_revisions")
