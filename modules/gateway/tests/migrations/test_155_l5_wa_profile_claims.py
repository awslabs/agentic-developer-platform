"""Gateway upgrades install exclusive Profile claims without losing scope history."""

import pytest
from l5_services.models import Configuration, ProfileClaim
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_profile_claim_bridge_extends_finops_history():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("155_l5_wa_profile_claims").down_revision == "154_l5_finops_ownership"
    assert scripts.get_revision("154_l5_finops_ownership").down_revision == "153_l5_publications"


def test_gateway_upgrade_installs_exclusive_claims_and_preserves_history(pg_url):
    upgrade(pg_url, "154_l5_finops_ownership")
    engine = create_engine(pg_url)
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": "project"}
    claim = {**scope, "service_id": "well_architected", "native_identity_key": "a" * 64}
    try:
        with engine.begin() as connection:
            assert not inspect(connection).has_table(ProfileClaim.__tablename__)
            connection.execute(
                Configuration.__table__.insert().values(
                    **scope,
                    configuration_id="retained-config",
                    service_id="well_architected",
                    relationship_revision="1",
                    owner_id="owner",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            configuration_before = connection.execute(select(Configuration)).mappings().one()

        upgrade(pg_url)
        with engine.begin() as connection:
            inspector = inspect(connection)
            columns = {column["name"]: column for column in inspector.get_columns(ProfileClaim.__tablename__)}
            assert set(columns) == set(ProfileClaim.__table__.columns.keys())
            for column in ProfileClaim.__table__.columns:
                assert columns[column.name]["nullable"] == column.nullable
                assert columns[column.name]["type"].length == column.type.length
            assert set(inspector.get_pk_constraint(ProfileClaim.__tablename__)["constrained_columns"]) == {
                "service_id",
                "native_identity_key",
            }
            connection.execute(ProfileClaim.__table__.insert().values(**claim))
            for field in scope:
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(ProfileClaim.__table__.insert().values({**claim, field: f"foreign-{field}"}))
            assert dict(connection.execute(select(ProfileClaim)).mappings().one()) == claim
            assert connection.execute(select(Configuration)).mappings().one() == configuration_before

        upgrade(pg_url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(ProfileClaim)).mappings().one()) == claim
            assert connection.execute(select(Configuration)).mappings().one() == configuration_before

        refused = run_alembic(pg_url, "downgrade", "154_l5_finops_ownership")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(ProfileClaim)).mappings().one()) == claim
            assert connection.execute(select(Configuration)).mappings().one() == configuration_before
    finally:
        engine.dispose()
