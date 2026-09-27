import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, text

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_codex_policy_seed_is_enforcing_and_preserves_operator_settings():
    path = Path(__file__).parents[2] / "alembic/versions/077_codex_runtime_posture.py"
    spec = importlib.util.spec_from_file_location("codex_posture_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("""CREATE TABLE persona_model_policy_settings (
            compatibility_class TEXT PRIMARY KEY, enforcement_posture TEXT NOT NULL,
            active_default_model_id TEXT, revision INTEGER DEFAULT 1)""")
        )
        connection.execute(
            text("""INSERT INTO persona_model_policy_settings
            VALUES ('claude-agent-sdk', 'report_only', 'existing-model', 3)""")
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(
                text("""SELECT enforcement_posture,
                active_default_model_id FROM persona_model_policy_settings
                WHERE compatibility_class = 'codex-sdk'""")
            ).one() == ("enforcing", None)
            connection.execute(
                text("""UPDATE persona_model_policy_settings
                SET enforcement_posture='disabled', revision=4
                WHERE compatibility_class='codex-sdk'""")
            )
            migration.upgrade()
            migration.downgrade()
        assert connection.execute(
            text("""SELECT enforcement_posture, revision
            FROM persona_model_policy_settings WHERE compatibility_class='codex-sdk'""")
        ).one() == ("disabled", 4)
        assert connection.execute(
            text("""SELECT enforcement_posture,
            active_default_model_id, revision FROM persona_model_policy_settings
            WHERE compatibility_class='claude-agent-sdk'""")
        ).one() == ("report_only", "existing-model", 3)
    engine.dispose()
