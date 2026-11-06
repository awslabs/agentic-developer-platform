"""WA07 rollback refuses to discard recovery or reuse records."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from l5_services.migrations.versions import (
    l5_source_retention, l5_wa_collection_details, l5_wa_collection_exclusions,
    l5_wa_generation_collection, l5_wa_generation_request, l5_wa_profile_claims,
    l5_wa_usage, l5_wa_reuse_authorization, l5_wa_qualification,
)


@pytest.mark.parametrize("revision,table_name,column_name", [
    (l5_source_retention, "l5_service_source_retention", "tenant_id"),
    (l5_wa_profile_claims, "l5_service_profile_claims", "tenant_id"),
    (l5_wa_generation_request, "l5_service_worker_attempts", "prepared_request"),
    (l5_wa_generation_collection, "l5_service_generation_collections", "tenant_id"),
    (l5_wa_collection_details, "l5_service_generation_collections", "tenant_id"),
    (l5_wa_collection_exclusions, "l5_service_generation_collections", "tenant_id"),
    (l5_wa_usage, "l5_service_wa_usage", "tenant_id"),
    (l5_wa_reuse_authorization, "l5_service_wa_reuse_authorizations", "tenant_id"),
    (l5_wa_qualification, "l5_service_wa_qualification", "tenant_id"),
])
def test_rollback_retains_provider_and_reuse_records(revision, table_name, column_name):
    engine = sa.create_engine("sqlite:///:memory:")
    table = sa.Table(table_name, sa.MetaData(), sa.Column(column_name, sa.String))
    with engine.begin() as connection:
        table.create(connection)
        connection.execute(table.insert().values({column_name: "retained"}))
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="downgrade would discard retained data"):
                revision.downgrade()
        assert connection.execute(sa.select(table.c[column_name])).scalar_one() == "retained"
    engine.dispose()


def test_prior_worker_attempt_without_prepared_request_can_downgrade():
    engine = sa.create_engine("sqlite:///:memory:")
    attempts = sa.Table("l5_service_worker_attempts", sa.MetaData(),
                        sa.Column("tenant_id", sa.String), sa.Column("prepared_request", sa.JSON))
    with engine.begin() as connection:
        attempts.create(connection)
        connection.execute(attempts.insert().values(tenant_id="older-service"))
        with Operations.context(MigrationContext.configure(connection)):
            l5_wa_generation_request.downgrade()
        assert connection.execute(sa.text("SELECT tenant_id FROM l5_service_worker_attempts")).scalar_one() == "older-service"
        assert "prepared_request" not in {column["name"] for column in sa.inspect(connection).get_columns(attempts.name)}
    engine.dispose()
