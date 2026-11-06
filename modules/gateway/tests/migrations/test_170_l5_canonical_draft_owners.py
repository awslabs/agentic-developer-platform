"""Only unstarted, unambiguous legacy drafts can have ownership repaired."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.migrations.head_contract import GATEWAY_HEAD


@pytest.fixture
def repair():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/170_l5_canonical_draft_owners.py"
    spec = importlib.util.spec_from_file_location("canonical_drafts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.repair_drafts


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE users (id TEXT, cognito_sub TEXT, org_id TEXT, user_kind TEXT, is_shadow BOOLEAN)",
            "CREATE TABLE l5_service_configurations (tenant_id TEXT, configuration_id TEXT, owner_id TEXT, current_revision INTEGER)",
            "CREATE TABLE l5_service_config_versions (tenant_id TEXT, configuration_id TEXT, revision INTEGER, state TEXT, hash TEXT)",
            "CREATE TABLE l5_service_config_receipts (tenant_id TEXT, configuration_id TEXT, principal_id TEXT, request_hash TEXT)",
            *(
                f"CREATE TABLE {table} (tenant_id TEXT, configuration_id TEXT)"
                for table in ("l5_service_bindings", "l5_service_setup_intents", "l5_service_runs")
            ),
        ):
            connection.execute(text(statement))
        connection.execute(text("INSERT INTO users VALUES ('user-a', 'subject-a', 'tenant-a', 'human', false)"))
        connection.execute(text("INSERT INTO l5_service_configurations VALUES ('tenant-a', 'config-a', 'subject-a', 1)"))
        connection.execute(text("INSERT INTO l5_service_config_versions VALUES ('tenant-a', 'config-a', 1, 'disabled', 'retained-hash')"))
        connection.execute(text("INSERT INTO l5_service_config_receipts VALUES ('tenant-a', 'config-a', 'subject-a', 'request-hash')"))
        yield connection
    engine.dispose()


def test_draft_repair_preserves_settings_and_idempotency(repair, db):
    assert repair(db) == 1
    assert repair(db) == 0
    assert db.scalar(text("SELECT owner_id FROM l5_service_configurations")) == "user-a"
    assert tuple(db.execute(text("SELECT principal_id, request_hash FROM l5_service_config_receipts")).one()) == ("user-a", "request-hash")
    assert tuple(db.execute(text("SELECT revision, hash FROM l5_service_config_versions")).one()) == (1, "retained-hash")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE users SET org_id = 'tenant-b'",
        "UPDATE users SET user_kind = 'service'",
        "UPDATE users SET is_shadow = true",
        "INSERT INTO users VALUES ('ambiguous', 'subject-a', 'tenant-a', 'human', false)",
        "INSERT INTO users VALUES ('subject-a', 'another-subject', 'tenant-a', 'human', false)",
        "UPDATE l5_service_configurations SET current_revision = 2",
        "UPDATE l5_service_config_versions SET state = 'ready'",
        "INSERT INTO l5_service_bindings VALUES ('tenant-a', 'config-a')",
        "INSERT INTO l5_service_setup_intents VALUES ('tenant-a', 'config-a')",
        "INSERT INTO l5_service_runs VALUES ('tenant-a', 'config-a')",
    ],
)
def test_existing_effects_or_unproven_identity_prevent_repair(repair, db, statement):
    db.execute(text(statement))
    assert repair(db) == 0
    assert db.scalar(text("SELECT owner_id FROM l5_service_configurations")) == "subject-a"


def test_real_upgrade_preserves_draft_version_and_receipt(pg_url):
    from l5_services.models.configuration import Configuration, ConfigurationReceipt, ConfigurationVersion
    from sqlalchemy import select

    from src.shared.models.organization import User
    from tests.migrations.conftest_postgres import upgrade

    upgrade(pg_url, "169_merge_collection")
    engine = create_engine(pg_url)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example", "service_id": "security"}
    try:
        with engine.begin() as connection:
            connection.execute(
                User.__table__.insert().values(
                    id="owner-example", cognito_sub="subject-example", org_id=scope["tenant_id"], team_id="team-example", email="owner@example.test"
                )
            )
            connection.execute(
                Configuration.__table__.insert().values(
                    **scope,
                    configuration_id="draft-example",
                    owner_id="subject-example",
                    relationship_revision="1",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                ConfigurationVersion.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    configuration_id="draft-example",
                    revision=1,
                    settings={"capabilities": ["code_review"]},
                    inherited_fields=[],
                    gaps=[],
                    state="disabled",
                    hash="a" * 64,
                )
            )
            connection.execute(
                ConfigurationReceipt.__table__.insert().values(
                    **scope,
                    principal_id="subject-example",
                    operation="create",
                    idempotency_key="create-example",
                    request_hash="b" * 64,
                    configuration_id="draft-example",
                    revision=1,
                )
            )
            version_before = dict(connection.execute(select(ConfigurationVersion)).mappings().one())
            receipt_before = dict(connection.execute(select(ConfigurationReceipt)).mappings().one())
        upgrade(pg_url)
        upgrade(pg_url)
        with engine.connect() as connection:
            assert connection.scalar(select(Configuration.owner_id)) == "owner-example"
            assert dict(connection.execute(select(ConfigurationVersion)).mappings().one()) == version_before
            assert dict(connection.execute(select(ConfigurationReceipt)).mappings().one()) == {**receipt_before, "principal_id": "owner-example"}
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
    finally:
        engine.dispose()
