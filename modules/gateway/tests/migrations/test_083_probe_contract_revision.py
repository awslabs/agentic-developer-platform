"""The real Task revision round-trips through PostgreSQL evidence storage."""

from sqlalchemy import create_engine, inspect, text

from tests.migrations.conftest_postgres import upgrade


def test_task_contract_revision_fits_probe_and_evidence(pg_url):
    upgrade(pg_url, "083_probe_contract_revision")
    engine = create_engine(pg_url)
    revision = "task-codex-sdk-inline-responses-v2"
    try:
        with engine.begin() as connection:
            for table in ("model_probe_slots", "model_invocability_evidence"):
                columns = {c["name"]: c for c in inspect(connection).get_columns(table)}
                assert columns["harness_contract_revision"]["type"].length == 64
            connection.execute(
                text(
                    "INSERT INTO model_invocability_evidence "
                    "(account_id, region, canonical_model_id, compatibility_class, harness_contract_revision, "
                    "request_shape_sha256, outcome, verified_at, expires_at, updated_at) "
                    "VALUES ('123456789012', 'us-east-1', 'openai.gpt-6-sol', 'codex-sdk', :revision, "
                    ":shape, 'error', NOW(), NOW(), NOW())"
                ),
                {"revision": revision, "shape": "a" * 64},
            )
            assert connection.scalar(text("SELECT harness_contract_revision FROM model_invocability_evidence")) == revision
    finally:
        engine.dispose()
