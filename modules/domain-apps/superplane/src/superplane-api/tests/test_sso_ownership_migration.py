"""Applied additive provider binding and deliberate legacy reconciliation (#7841)."""

import uuid

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError


def test_existing_sso_rows_are_not_backfilled_or_renamed():
    config = Config("alembic.ini")
    revision = (
        ScriptDirectory.from_config(config)
        .get_revision("055_sso_provider_ownership")
        .module
    )
    first = str(uuid.UUID("12345678-0000-4000-8000-000000000001"))
    second = str(uuid.UUID("12345678-0000-4000-8000-000000000002"))
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE organizations (id TEXT PRIMARY KEY, sso_provider_name TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizations (id, sso_provider_name) VALUES (?, ?), (?, ?)",
            (first, "First company sign-in", second, "Second company sign-in"),
        )
        context = MigrationContext.configure(connection=connection)
        with Operations.context(context):
            revision.upgrade()
        rows = connection.exec_driver_sql(
            "SELECT id, sso_provider_name, sso_cognito_provider_name FROM organizations ORDER BY id"
        ).all()
        assert rows == [
            (first, "First company sign-in", None),
            (second, "Second company sign-in", None),
        ]
        indexes = sa.inspect(connection).get_indexes("organizations")
        assert any(
            index["name"] == "ix_organizations_sso_cognito_provider_name"
            and index["unique"]
            for index in indexes
        )
        connection.exec_driver_sql(
            "UPDATE organizations SET sso_cognito_provider_name=? WHERE id=?",
            ("org-12345678", first),
        )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "UPDATE organizations SET sso_cognito_provider_name=? WHERE id=?",
                ("org-12345678", second),
            )
        with (
            Operations.context(context),
            pytest.raises(RuntimeError, match="preserve SSO provider bindings"),
        ):
            revision.downgrade()
        connection.exec_driver_sql(
            "UPDATE organizations SET sso_cognito_provider_name=NULL"
        )
        with Operations.context(context):
            revision.downgrade()
        assert "sso_cognito_provider_name" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("organizations")
        }
        names = (
            connection.exec_driver_sql(
                "SELECT sso_provider_name FROM organizations ORDER BY id"
            )
            .scalars()
            .all()
        )
        assert names == ["First company sign-in", "Second company sign-in"]
