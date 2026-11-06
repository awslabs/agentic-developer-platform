"""Gateway upgrades preserve inquiry attempts and their original question digest."""

from datetime import UTC, datetime
from hashlib import sha256

import pytest
from l5_services.models import Configuration, ConfigurationVersion, Run, WorkerAttempt
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text, update

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_finops_inquiry_bridge_extends_publication_retry_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("157_merge_transform_inquiry").down_revision == ("156_merge_transform_retry", "156_l5_finops_inquiry")
    assert scripts.get_revision("156_l5_finops_inquiry").down_revision == "155_l5_publication_retry"
    assert scripts.get_revision("155_l5_publication_retry").down_revision == "154_l5_finops_ownership"


@pytest.mark.parametrize("upgrade_target", ["156_l5_finops_inquiry", "head"])
@pytest.mark.parametrize("initial_revision", ["155_l5_publication_retry", "156_merge_transform_retry", "156_l5_finops_inquiry"])
def test_gateway_upgrade_preserves_attempt_and_question_digest(pg_url, initial_revision, upgrade_target):
    upgrade(pg_url, initial_revision)
    engine = create_engine(pg_url)
    question = "What was spent?"
    question_hash = sha256(question.encode()).hexdigest()
    expected_question_hash = question_hash if initial_revision == "156_l5_finops_inquiry" else None
    started_at = datetime(2026, 10, 28, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            old_attempts = Table(WorkerAttempt.__tablename__, MetaData(), autoload_with=connection)
            assert ("question_hash" in old_attempts.c) == (initial_revision == "156_l5_finops_inquiry")
            connection.execute(
                Configuration.__table__.insert().values(
                    tenant_id="tenant",
                    configuration_id="config",
                    product_id="product",
                    project_id="project",
                    service_id="finops",
                    relationship_revision="1",
                    owner_id="owner",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                ConfigurationVersion.__table__.insert().values(
                    tenant_id="tenant",
                    configuration_id="config",
                    revision=1,
                    settings={},
                    inherited_fields=[],
                    gaps=[],
                    state="ready",
                    hash="a" * 64,
                )
            )
            connection.execute(
                Run.__table__.insert().values(
                    tenant_id="tenant",
                    run_id="run",
                    product_id="product",
                    project_id="project",
                    service_id="finops",
                    configuration_id="config",
                    configuration_revision=1,
                    binding_id="binding",
                    trigger_id="manual",
                    binding_generation=1,
                    input={"question": question},
                    admission={"limits": {"max_polls": 4}},
                    deadline_at=started_at,
                )
            )
            connection.execute(
                old_attempts.insert().values(
                    tenant_id="tenant",
                    attempt_id="attempt",
                    run_id="run",
                    operation="submit",
                    first_work_id="work",
                    first_fence=1,
                    idempotency_token="b" * 64,
                    input_hash="c" * 64,
                    started_at=started_at,
                    status="unknown",
                    reconcile_checks=1,
                    native_job_ref={"conversation_id": "conversation", "turn_id": "turn"},
                    **({"question_hash": expected_question_hash} if expected_question_hash else {}),
                )
            )
            attempt_before = dict(connection.execute(select(old_attempts)).mappings().one())
            run_before = dict(connection.execute(select(Run)).mappings().one())

        upgrade(pg_url, upgrade_target)
        with engine.begin() as connection:
            columns = {column["name"]: column for column in inspect(connection).get_columns(WorkerAttempt.__tablename__)}
            expected_columns = set(WorkerAttempt.__table__.columns.keys())
            if upgrade_target != "head":
                expected_columns.remove("prepared_request")
            assert set(columns) == expected_columns
            assert columns["question_hash"]["nullable"]
            assert columns["question_hash"]["type"].length == 64
            attempts = Table(WorkerAttempt.__tablename__, MetaData(), autoload_with=connection)
            attempt_after = dict(connection.execute(select(attempts)).mappings().one())
            assert {key: attempt_after[key] for key in attempt_before} == attempt_before
            assert attempt_after["question_hash"] == expected_question_hash
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
            connection.execute(
                update(attempts)
                .where(
                    attempts.c.tenant_id == "tenant",
                    attempts.c.attempt_id == "attempt",
                )
                .values(question_hash=question_hash)
            )
            saved_attempt = dict(connection.execute(select(attempts)).mappings().one())
            assert saved_attempt["question_hash"] == question_hash

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "155_l5_publication_retry")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(WorkerAttempt)).mappings().one()) == {**saved_attempt, "prepared_request": None}
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
    finally:
        engine.dispose()
