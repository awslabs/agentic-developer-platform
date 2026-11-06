"""Upgrade either service history without losing owned cleanup evidence."""

from datetime import UTC, datetime

import pytest
from l5_services.models import Configuration, ConfigurationVersion, CostCollection, ProfileClaim, PublicationRetryReceipt, Run, SetupIntent
from l5_services.models.base import Base as L5Base
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.products.models import ConnectorModuleAdmission
from src.shared.models.organization import Organization
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_finops_and_transform_histories_join_without_rewriting_parents():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("164_merge_wa_connector").down_revision == ("159_merge_wa_cost", "163_merge_connector_cost")
    assert scripts.get_revision("159_merge_wa_cost").down_revision == ("158_merge_wa_transform", "158_l5_cost_collections")
    assert scripts.get_revision("158_l5_cost_collections").down_revision == "157_merge_transform_inquiry"
    assert scripts.get_revision("158_merge_wa_transform").down_revision == ("157_merge_wa_finops_inquiry", "157_merge_transform_inquiry")
    assert scripts.get_revision("157_merge_transform_inquiry").down_revision == ("156_merge_transform_retry", "156_l5_finops_inquiry")
    assert scripts.get_revision("156_merge_transform_retry").down_revision == ("155_merge_finops_transform", "155_l5_publication_retry")
    assert scripts.get_revision("155_merge_finops_transform").down_revision == ("154_l5_finops_ownership", "154_l5_transform_cleanup")
    for revision in ("154_l5_finops_ownership", "154_l5_transform_cleanup"):
        assert scripts.get_revision(revision).down_revision == "153_l5_publications"
    assert all(len(revision.revision) <= 32 for revision in scripts.walk_revisions())


@pytest.mark.parametrize(
    "initial_revisions",
    [
        ("153_l5_publications",),
        ("154_l5_finops_ownership",),
        ("154_l5_transform_cleanup",),
        ("154_l5_finops_ownership", "154_l5_transform_cleanup"),
        ("155_merge_finops_transform",),
        ("155_l5_publication_retry",),
        ("155_merge_finops_transform", "155_l5_publication_retry"),
        ("156_merge_transform_retry",),
        ("156_l5_finops_inquiry",),
        ("156_merge_transform_retry", "156_l5_finops_inquiry"),
        ("157_merge_transform_inquiry",),
        ("157_merge_wa_finops_inquiry",),
        ("157_merge_wa_finops_inquiry", "157_merge_transform_inquiry"),
        ("158_merge_wa_transform",),
        ("158_l5_cost_collections",),
        ("158_merge_wa_transform", "158_l5_cost_collections"),
        ("161_merge_connector_finops",),
        ("157_merge_transform_inquiry", "161_merge_connector_finops"),
        ("159_merge_wa_cost",),
        ("163_merge_connector_cost",),
        ("159_merge_wa_cost", "163_merge_connector_cost"),
    ],
)
def test_upgrade_from_either_history_preserves_operations(pg_url, initial_revisions):
    for revision in initial_revisions:
        upgrade(pg_url, revision)
    engine = create_engine(pg_url)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    try:
        with engine.begin() as connection:
            connection.execute(
                Configuration.__table__.insert().values(
                    **scope,
                    configuration_id="config-example",
                    service_id="transform",
                    relationship_revision="1",
                    owner_id="owner-example",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                SetupIntent.__table__.insert().values(
                    **scope,
                    operation_id="operation-example",
                    configuration_id="config-example",
                    service_id="transform",
                    actor_id="owner-example",
                    idempotency_key="setup-example-key",
                    scope=scope,
                    request={},
                    authorization={},
                    intent_hash="a" * 64,
                )
            )
            operations = Table("l5_service_binding_operations", MetaData(), autoload_with=connection)
            values = {
                "tenant_id": scope["tenant_id"],
                "operation_id": "operation-example",
                "action": "cleanup",
                "state": "succeeded",
                "revision": 2,
                "gaps": [],
                "attempts": 1,
                "reconcile_checks": 1,
                "creation_receipt": {"resource_id": "definition-example", "ownership": "owned"},
            }
            if "cleanup_receipt" in operations.c:
                values["cleanup_receipt"] = {"resource_id": "definition-example", "absent": True}
            connection.execute(operations.insert().values(**values))
            before = dict(connection.execute(select(operations)).mappings().one())
            claims_before = []
            if inspect(connection).has_table(ProfileClaim.__tablename__):
                connection.execute(
                    ProfileClaim.__table__.insert().values(
                        **scope,
                        service_id="well_architected",
                        native_identity_key="c" * 64,
                    )
                )
                claims_before = connection.execute(select(ProfileClaim)).mappings().all()
            retry_before = []
            if inspect(connection).has_table(PublicationRetryReceipt.__tablename__):
                connection.execute(
                    PublicationRetryReceipt.__table__.insert().values(
                        **scope,
                        service_id="devops",
                        actor_id="actor-example",
                        idempotency_key="retry-example-key",
                        publication_id="publication-example",
                        request_hash="b" * 64,
                        response={"publication_id": "publication-example", "state": "pending"},
                    )
                )
                retry_before = connection.execute(select(PublicationRetryReceipt)).mappings().all()
            collections_before = []
            if inspect(connection).has_table(CostCollection.__tablename__):
                connection.execute(
                    ConfigurationVersion.__table__.insert().values(
                        tenant_id=scope["tenant_id"],
                        configuration_id="config-example",
                        revision=1,
                        settings={},
                        inherited_fields=[],
                        gaps=[],
                        state="ready",
                        hash="d" * 64,
                    )
                )
                connection.execute(
                    Run.__table__.insert().values(
                        **scope,
                        run_id="run-example",
                        service_id="finops",
                        configuration_id="config-example",
                        configuration_revision=1,
                        binding_id="binding-example",
                        trigger_id="manual",
                        binding_generation=1,
                        input={"query": "cost-example"},
                        admission={"limits": {"max_ce_requests": 4}},
                        deadline_at=datetime(2026, 10, 28, tzinfo=UTC),
                    )
                )
                connection.execute(
                    CostCollection.__table__.insert().values(
                        tenant_id=scope["tenant_id"],
                        run_id="run-example",
                        collection_id="collection-example",
                        query_hash="e" * 64,
                        scope_digest="f" * 64,
                        started_at=datetime(2026, 10, 28, tzinfo=UTC),
                        status="partial",
                        request_count=2,
                        page_count=1,
                        checkpoint_ref={"artifact_id": "page-example", "version_id": "version-example", "sha256": "a" * 64},
                        coverage="partial",
                        reason="billing_unavailable",
                    )
                )
                collections_before = connection.execute(select(CostCollection)).mappings().all()
            admissions_before = []
            if inspect(connection).has_table(ConnectorModuleAdmission.__tablename__):
                connection.execute(Organization.__table__.insert().values(id=scope["tenant_id"], name="Migration tenant", created_via="operator"))
                connection.execute(
                    ConnectorModuleAdmission.__table__.insert().values(
                        tenant_id=scope["tenant_id"], generation=7, enabled=False, changed_at=datetime(2026, 10, 28, tzinfo=UTC)
                    )
                )
                admissions_before = connection.execute(select(ConnectorModuleAdmission)).mappings().all()
        upgrade(pg_url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            operations = Table("l5_service_binding_operations", MetaData(), autoload_with=connection)
            after = dict(connection.execute(select(operations)).mappings().one())
            assert {key: after[key] for key in before} == before
            assert after["cleanup_receipt"] == before.get("cleanup_receipt")
            assert connection.execute(select(PublicationRetryReceipt)).mappings().all() == retry_before
            assert connection.execute(select(ProfileClaim)).mappings().all() == claims_before
            assert connection.execute(select(CostCollection)).mappings().all() == collections_before
            assert connection.execute(select(ConnectorModuleAdmission)).mappings().all() == admissions_before
            inspector = inspect(connection)
            assert set(L5Base.metadata.tables) <= set(inspector.get_table_names())
            for table in L5Base.metadata.sorted_tables:
                assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
    finally:
        engine.dispose()
