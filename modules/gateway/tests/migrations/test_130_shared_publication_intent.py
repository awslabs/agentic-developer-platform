"""An unresolved registry request cannot be erased by schema rollback."""

import sqlalchemy as sa

from tests.migrations.conftest_postgres import run_alembic, upgrade


def test_unsettled_publication_preserved_on_downgrade(pg_url):
    upgrade(pg_url, "130_shared_publication_intent")
    engine = sa.create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("""
                INSERT INTO orchestration_environment_leases (
                    id, canonical_target_key, evidence_source, evidence_verified_at, state,
                    owner_org_id, owner_action_id, owner_generation, manifest_entry_id,
                    release_ref, revision, created_at, publication_pending_digest
                ) VALUES (
                    'lease', 'fictional-surface', 'controlled-test-readback', now(), 'held',
                    'fictional-org', 'fictional-action', 1, 'reviewed-entry',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1, now(), :digest
                )
            """),
                {"digest": "sha256:" + "b" * 64},
            )
        refused = run_alembic(pg_url, "downgrade", "129_merge_native_epic_atlassian")
        assert refused.returncode != 0
        assert "Unsettled shared publication must be reconciled before rollback" in refused.stderr
        with engine.begin() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "130_shared_publication_intent"
            connection.execute(sa.text("UPDATE orchestration_environment_leases SET publication_pending_digest = NULL"))
        upgrade_result = run_alembic(pg_url, "downgrade", "129_merge_native_epic_atlassian")
        assert upgrade_result.returncode == 0
        upgrade(pg_url, "head")
        with engine.connect() as connection:
            assert "publication_pending_digest" in {
                column["name"] for column in sa.inspect(connection).get_columns("orchestration_environment_leases")
            }
    finally:
        engine.dispose()
