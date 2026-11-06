"""Upgrade existing execution storage and keep recovery metadata aligned."""

import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from l5_services import migrations
from l5_services.migrations.versions import (
    l5_admission_recovery, l5_bindings_and_setup, l5_configurations, l5_devops_integration_observation,
    l5_devops_resource_observation, l5_devops_knowledge_publication, l5_devops_native_runs,
    l5_devops_prevention_observation, l5_finops_retained_scope, l5_finops_setup_receipts,
    l5_finops_publication_merge, l5_finops_wa_merge, l5_finops_retry_merge, l5_wa_retry_merge,
    l5_finops_inquiry_attempt, l5_finops_inquiry_retry_merge, l5_wa_finops_inquiry_merge,
    l5_devops_space_intents, l5_events, l5_publication_prevention_merge, l5_publication_retry_receipts, l5_publications,
    l5_finops_transform_merge, l5_transform_retry_merge, l5_transform_cleanup,
    l5_transform_inquiry_merge, l5_wa_transform_merge,
    l5_cost_transform_merge, l5_finops_cost_collections, l5_wa_cost_merge,
    l5_results_and_triage, l5_runs_and_work, l5_source_retention,
    l5_wa_generation_request, l5_wa_generation_collection, l5_wa_collection_details,
    l5_wa_collection_exclusions, l5_wa_usage, l5_wa_reuse_authorization, l5_wa_qualification,
    l5_worker_attempts, l5_worker_evidence, l5_worker_fences, l5_worker_repair, l5_worker_runtime,
    l5_wa_profile_claims, l5_setup_work_fences, l5_setup_wa_merge, l5_setup_authority_continuations, l5_schedule_dispatch, l5_wa_periodic_evidence, l5_knowledge_effects,
)
from l5_services.models.base import Base
from tests.migrations.conftest_postgres import pg_server as pg_server
from tests.migrations.conftest_postgres import pg_url as pg_url


def test_execution_migrations_have_one_head():
    scripts = ScriptDirectory(str(Path(migrations.__file__).parent))
    assert scripts.get_current_head() == "l5_knowledge_effects"
    assert l5_setup_authority_continuations.down_revision == l5_wa_qualification.revision
    assert l5_wa_qualification.down_revision == l5_wa_reuse_authorization.revision
    assert l5_setup_wa_merge.down_revision == (l5_setup_work_fences.revision, l5_wa_usage.revision)
    assert l5_setup_work_fences.down_revision == l5_cost_transform_merge.revision
    assert l5_wa_usage.down_revision == l5_wa_collection_exclusions.revision
    assert l5_wa_collection_exclusions.down_revision == l5_wa_collection_details.revision
    assert l5_wa_collection_details.down_revision == l5_wa_generation_collection.revision
    assert l5_wa_generation_collection.down_revision == l5_wa_generation_request.revision
    assert l5_wa_generation_request.down_revision == l5_source_retention.revision
    assert l5_source_retention.down_revision == l5_wa_cost_merge.revision
    assert l5_wa_transform_merge.down_revision == (
        l5_wa_finops_inquiry_merge.revision, l5_transform_inquiry_merge.revision)
    assert l5_finops_wa_merge.down_revision == (
        l5_finops_publication_merge.revision, l5_wa_profile_claims.revision)
    assert l5_wa_profile_claims.down_revision == l5_publication_prevention_merge.revision
    assert l5_wa_cost_merge.down_revision == (
        l5_wa_transform_merge.revision, l5_cost_transform_merge.revision)
    assert l5_cost_transform_merge.down_revision == (
        l5_finops_cost_collections.revision, l5_transform_inquiry_merge.revision)
    assert l5_finops_cost_collections.down_revision == l5_finops_inquiry_retry_merge.revision
    assert l5_transform_inquiry_merge.down_revision == (
        l5_transform_retry_merge.revision, l5_finops_inquiry_retry_merge.revision)
    assert l5_transform_retry_merge.down_revision == (
        l5_finops_transform_merge.revision, l5_finops_retry_merge.revision)
    assert l5_finops_transform_merge.down_revision == (
        l5_finops_publication_merge.revision, l5_transform_cleanup.revision)
    assert l5_transform_cleanup.down_revision == l5_publication_prevention_merge.revision
    assert l5_finops_inquiry_retry_merge.down_revision == (
        l5_finops_inquiry_attempt.revision, l5_finops_retry_merge.revision)
    assert l5_finops_inquiry_attempt.down_revision == l5_finops_publication_merge.revision
    assert l5_wa_finops_inquiry_merge.down_revision == (
        l5_wa_retry_merge.revision, l5_finops_inquiry_retry_merge.revision)
    assert l5_finops_retry_merge.down_revision == (
        l5_finops_publication_merge.revision, l5_publication_retry_receipts.revision)
    assert l5_publication_retry_receipts.down_revision == l5_publication_prevention_merge.revision
    assert l5_wa_retry_merge.down_revision == (
        l5_finops_wa_merge.revision, l5_finops_retry_merge.revision)
    assert l5_finops_publication_merge.down_revision == (
        l5_finops_setup_receipts.revision, l5_publication_prevention_merge.revision)
    assert l5_publication_prevention_merge.down_revision == (
        l5_publications.revision, l5_devops_prevention_observation.revision)
    assert l5_publications.down_revision == l5_devops_native_runs.revision
    assert l5_devops_prevention_observation.down_revision == l5_devops_native_runs.revision
    assert l5_results_and_triage.down_revision == l5_devops_resource_observation.revision
    revisions = [script.revision for script in scripts.walk_revisions()]
    assert revisions.index(l5_bindings_and_setup.revision) < revisions.index(l5_devops_resource_observation.revision)
    assert revisions.index(l5_bindings_and_setup.revision) < revisions.index(l5_devops_space_intents.revision)


def test_transform_cleanup_receipt_migration_preserves_prior_operations():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE l5_service_binding_operations (operation_id VARCHAR(36) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO l5_service_binding_operations (operation_id) VALUES ('previous-operation')"))
            with Operations.context(MigrationContext.configure(connection)):
                l5_transform_cleanup.upgrade()
                assert tuple(connection.execute(text(
                    "SELECT operation_id, cleanup_receipt FROM l5_service_binding_operations"
                )).one()) == ("previous-operation", None)
                l5_transform_cleanup.downgrade()
            assert connection.scalar(text(
                "SELECT operation_id FROM l5_service_binding_operations"
            )) == "previous-operation"
    finally:
        engine.dispose()


def test_execution_recovery_upgrade_and_downgrade():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            l5_configurations.upgrade()
            l5_runs_and_work.upgrade()
            connection.execute(text("""
                INSERT INTO l5_service_runs
                (tenant_id, run_id, product_id, project_id, service_id, configuration_id,
                 configuration_revision, binding_id, trigger_id, binding_generation, revision,
                 input, admission, state, provider_state, deadline_at)
                VALUES ('tenant-a', 'run-a', 'product-a', 'project-a', 'security', 'config-a',
                        1, 'binding-a', 'manual-a', 1, 1, '{}', '{}', 'admitted', 'not_started',
                        '2026-01-01 01:00:00')
            """))
            connection.execute(text("""
                INSERT INTO l5_service_runs
                (tenant_id, run_id, product_id, project_id, service_id, configuration_id,
                 configuration_revision, binding_id, trigger_id, binding_generation, revision,
                 input, admission, state, provider_state, deadline_at)
                VALUES ('tenant-a', 'finops-pending', 'product-a', 'project-a', 'finops', 'config-a',
                        1, 'binding-a', 'manual-a', 1, 1, '{}',
                        '{"native_turn_id":"turn-example"}', 'running', 'accepted',
                        '2026-01-01 01:00:00')
            """))
            l5_admission_recovery.upgrade()
            l5_worker_evidence.upgrade()
            l5_worker_runtime.upgrade()
            l5_worker_attempts.upgrade()
            l5_worker_repair.upgrade()
            l5_worker_fences.upgrade()
            l5_events.upgrade()
            l5_devops_space_intents.upgrade()
            l5_devops_resource_observation.upgrade()
            l5_results_and_triage.upgrade()
            connection.execute(text("""
                INSERT INTO l5_devops_space_intents
                (tenant_id, operation_id, selection, configuration_revision, intent_hash,
                 state, owned_trigger_ids, resume_trigger_ids, active_executions)
                VALUES ('tenant-a', 'operation-a', '{}', 1, 'intent-a',
                        'created', '[]', '[]', '[]')
            """))
            l5_devops_integration_observation.upgrade()
            assert tuple(connection.execute(text(
                "SELECT operation_id, integration_observation FROM l5_devops_space_intents"
            )).one()) == ("operation-a", None)
            l5_bindings_and_setup.upgrade()
            l5_devops_knowledge_publication.upgrade()
            l5_devops_native_runs.upgrade()
            l5_publications.upgrade()
            l5_publication_retry_receipts.upgrade()
            l5_devops_prevention_observation.upgrade()
            l5_wa_profile_claims.upgrade()
            l5_transform_cleanup.upgrade()
            l5_finops_retained_scope.upgrade()
            l5_finops_setup_receipts.upgrade()
            l5_finops_inquiry_attempt.upgrade()
            l5_finops_cost_collections.upgrade()
            l5_setup_work_fences.upgrade()
            l5_source_retention.upgrade()
            l5_wa_generation_request.upgrade()
            l5_wa_generation_collection.upgrade()
            connection.execute(text(
                "INSERT INTO l5_service_generation_collections "
                "(tenant_id, run_id, native_job_ref, next_token, page_count, recommendations) "
                "VALUES ('tenant-a', 'run-a', '{}', 'page-two', 1, '[]')"))
            l5_wa_collection_details.upgrade()
            assert connection.scalar(text(
                "SELECT pending_recommendations FROM l5_service_generation_collections")) == "[]"
            l5_wa_collection_exclusions.upgrade()
            assert connection.scalar(text(
                "SELECT excluded FROM l5_service_generation_collections")) == "[]"
            excluded = [{"recommendationArn": "recommendation-other", "generationId": "other-generation",
                         "reason": "different_generation"}]
            connection.execute(text(
                "UPDATE l5_service_generation_collections SET excluded = :excluded"
            ), {"excluded": json.dumps(excluded)})
            l5_wa_usage.upgrade()
            l5_wa_reuse_authorization.upgrade()
            l5_wa_qualification.upgrade()
            l5_setup_authority_continuations.upgrade()
            l5_schedule_dispatch.upgrade()
            l5_wa_periodic_evidence.upgrade()
            l5_knowledge_effects.upgrade()
            assert json.loads(connection.scalar(text(
                "SELECT excluded FROM l5_service_generation_collections"))) == excluded
            assert tuple(connection.execute(text(
                "SELECT operation_id, prevention_observation, prevention_policy, prevention_operation_id "
                "FROM l5_devops_space_intents"
            )).one()) == ("operation-a", None, None, None)
            inspector = inspect(connection)
            for table in Base.metadata.sorted_tables:
                columns = {column["name"] for column in inspector.get_columns(table.name)}
                assert columns == set(table.columns.keys())
                assert set(inspector.get_pk_constraint(table.name)["constrained_columns"]) == {
                    column.name for column in table.primary_key
                }
            row = connection.execute(text("SELECT run_id, no_effect_proof, retry_of FROM l5_service_runs WHERE run_id='run-a'")).one()
            assert tuple(row) == ("run-a", None, None)
            pending = connection.execute(text(
                "SELECT service_id, state, provider_state, admission FROM l5_service_runs WHERE run_id='finops-pending'"
            )).one()
            assert pending[:3] == ("finops", "running", "accepted")
            assert json.loads(pending[3])["native_turn_id"] == "turn-example"
            l5_knowledge_effects.downgrade()
            l5_wa_periodic_evidence.downgrade()
            l5_schedule_dispatch.downgrade()
            l5_setup_authority_continuations.downgrade()
            l5_wa_qualification.downgrade()
            l5_wa_reuse_authorization.downgrade()
            l5_wa_usage.downgrade()
            assert "l5_service_wa_usage" not in inspect(connection).get_table_names()
            assert json.loads(connection.scalar(text(
                "SELECT excluded FROM l5_service_generation_collections"))) == excluded
            with pytest.raises(RuntimeError, match="downgrade would discard retained data"):
                l5_wa_collection_exclusions.downgrade()
            assert json.loads(connection.scalar(text(
                "SELECT excluded FROM l5_service_generation_collections"))) == excluded
            connection.execute(text("DELETE FROM l5_service_generation_collections"))
            l5_wa_collection_exclusions.downgrade()
            assert "excluded" not in {
                column["name"] for column in inspect(connection).get_columns("l5_service_generation_collections")
            }
            l5_wa_collection_details.downgrade()
            assert connection.scalar(text(
                "SELECT next_token FROM l5_service_generation_collections")) is None
            l5_wa_generation_collection.downgrade()
            l5_wa_generation_request.downgrade()
            l5_source_retention.downgrade()
            l5_wa_profile_claims.downgrade()
            l5_setup_work_fences.downgrade()
            l5_finops_cost_collections.downgrade()
            l5_finops_inquiry_attempt.downgrade()
            l5_finops_setup_receipts.downgrade()
            l5_finops_retained_scope.downgrade()
            l5_transform_cleanup.downgrade()
            l5_publication_retry_receipts.downgrade()
            l5_publications.downgrade()
            l5_devops_prevention_observation.downgrade()
            assert not {"prevention_observation", "prevention_policy", "prevention_operation_id"} & {
                column["name"] for column in inspect(connection).get_columns("l5_devops_space_intents")
            }
            assert connection.scalar(text("SELECT operation_id FROM l5_devops_space_intents")) == "operation-a"
            l5_devops_native_runs.downgrade()
            l5_devops_knowledge_publication.downgrade()
            l5_bindings_and_setup.downgrade()
            l5_devops_integration_observation.downgrade()
            assert "integration_observation" not in {
                column["name"] for column in inspect(connection).get_columns("l5_devops_space_intents")
            }
            assert connection.scalar(text("SELECT operation_id FROM l5_devops_space_intents")) == "operation-a"
            l5_results_and_triage.downgrade()
            l5_devops_resource_observation.downgrade()
            l5_devops_space_intents.downgrade()
            l5_events.downgrade()
            l5_worker_fences.downgrade()
            l5_worker_repair.downgrade()
            l5_worker_attempts.downgrade()
            l5_worker_runtime.downgrade()
            l5_worker_evidence.downgrade()
            l5_admission_recovery.downgrade()
            assert set(connection.execute(text("SELECT run_id FROM l5_service_runs")).scalars()) == {
                "run-a", "finops-pending"}
            assert "l5_service_admission_preparations" not in inspect(connection).get_table_names()
            l5_runs_and_work.downgrade()
            l5_configurations.downgrade()
            assert inspect(connection).get_table_names() == []
    engine.dispose()


@pytest.mark.parametrize("initial_revision", [
    "base", "l5_events", "l5_devops_space_intents", "l5_events_devops_merge",
    "l5_devops_resource_observation", "l5_events_resource_merge", "l5_devops_integration_obs",
    "l5_bindings_and_setup", "l5_events_bindings_merge", "l5_events_integration_merge",
    "l5_results_and_triage", "l5_results_events_merge", "l5_results_bindings_merge",
    "l5_bindings_integration_merge",
    "l5_results_integration_merge", "l5_devops_knowledge_pub",
    "l5_devops_native_runs", "l5_devops_prevention_obs", "l5_finops_retained_scope", "l5_finops_setup_receipts",
    "l5_publications", "l5_publication_prevention_merge", "l5_finops_publication_merge",
    "l5_wa_profile_claims", "l5_finops_wa_merge",
    "l5_publication_retry_receipts", "l5_finops_retry_merge", "l5_wa_retry_merge",
    "l5_finops_inquiry_attempt", "l5_finops_inquiry_retry_merge", "l5_wa_finops_inquiry_merge",
    "l5_transform_cleanup", "l5_finops_transform_merge", "l5_transform_retry_merge",
    "l5_transform_inquiry_merge", "l5_wa_transform_merge",
    "l5_finops_cost_collections", "l5_cost_transform_merge", "l5_wa_cost_merge",
    "l5_source_retention", "l5_wa_generation_request", "l5_wa_generation_collection",
    "l5_wa_collection_details", "l5_wa_collection_exclusions", "l5_wa_usage",
    "l5_setup_work_fences", "l5_setup_wa_merge", "l5_wa_reuse_authorization", "l5_wa_qualification", "l5_setup_authority_continuations", "l5_schedule_dispatch",
])
def test_migration_graph_upgrades_from_either_branch_and_downgrades(initial_revision):
    _assert_migration_round_trip(create_engine("sqlite:///:memory:"), initial_revision)


@pytest.mark.parametrize("initial_revision", [
    "base", "l5_events_resource_merge", "l5_events_bindings_merge", "l5_events_integration_merge",
    "l5_results_and_triage", "l5_results_events_merge", "l5_results_bindings_merge",
    "l5_bindings_integration_merge",
    "l5_results_integration_merge", "l5_devops_knowledge_pub",
    "l5_devops_native_runs", "l5_devops_prevention_obs", "l5_finops_retained_scope", "l5_finops_setup_receipts",
    "l5_publications", "l5_publication_prevention_merge", "l5_finops_publication_merge",
    "l5_wa_profile_claims", "l5_finops_wa_merge",
    "l5_publication_retry_receipts", "l5_finops_retry_merge", "l5_wa_retry_merge",
    "l5_finops_inquiry_attempt", "l5_finops_inquiry_retry_merge", "l5_wa_finops_inquiry_merge",
    "l5_transform_cleanup", "l5_finops_transform_merge", "l5_transform_retry_merge",
    "l5_transform_inquiry_merge", "l5_wa_transform_merge",
    "l5_finops_cost_collections", "l5_cost_transform_merge", "l5_wa_cost_merge",
    "l5_source_retention", "l5_wa_generation_request", "l5_wa_generation_collection",
    "l5_wa_collection_details", "l5_wa_collection_exclusions", "l5_wa_usage",
    "l5_setup_work_fences", "l5_setup_wa_merge", "l5_wa_reuse_authorization", "l5_wa_qualification", "l5_setup_authority_continuations", "l5_schedule_dispatch",
])
def test_migration_graph_upgrades_and_downgrades_postgres(request, initial_revision):
    _assert_migration_round_trip(create_engine(request.getfixturevalue("pg_url")), initial_revision)


def test_migration_revisions_fit_alembic_version_column():
    scripts = ScriptDirectory(str(Path(migrations.__file__).parent))
    context = MigrationContext.configure(dialect_name="postgresql")
    max_length = context._version.c.version_num.type.length
    for migration in scripts.walk_revisions():
        assert len(migration.revision) <= max_length, migration.revision


def _assert_migration_round_trip(engine, initial_revision):
    scripts = ScriptDirectory(str(Path(migrations.__file__).parent))
    assert scripts.get_current_head() == "l5_knowledge_effects"
    assert l5_transform_cleanup.down_revision == l5_publication_prevention_merge.revision
    try:
        with engine.begin() as connection:
            for target in (initial_revision, "head"):
                context = MigrationContext.configure(connection, opts={
                    "fn": lambda heads, context: scripts._upgrade_revs(target, heads),
                })
                with Operations.context(context):
                    context.run_migrations()
            assert context.get_current_heads() == ("l5_knowledge_effects",)
            assert set(inspect(connection).get_table_names()) == {
                *Base.metadata.tables, "alembic_version",
            }
            for table in Base.metadata.sorted_tables:
                columns = {column["name"] for column in inspect(connection).get_columns(table.name)}
                assert columns == set(table.columns.keys())
            context = MigrationContext.configure(connection, opts={
                "fn": lambda heads, context: scripts._downgrade_revs("base", heads),
            })
            with Operations.context(context):
                context.run_migrations()
            assert context.get_current_heads() == ()
            assert inspect(connection).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()


def test_profile_claim_migration_retains_exclusive_native_identity():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            l5_wa_profile_claims.upgrade()
            table = inspect(connection).get_pk_constraint("l5_service_profile_claims")
            assert set(table["constrained_columns"]) == {"service_id", "native_identity_key"}
            connection.execute(text("""
                INSERT INTO l5_service_profile_claims
                (service_id, native_identity_key, tenant_id, product_id, project_id)
                VALUES ('well_architected', 'one-native-id', 'tenant-one', 'product-one', 'project-one')
            """))
            assert connection.execute(text("SELECT count(*) FROM l5_service_profile_claims")).scalar_one() == 1
            with pytest.raises(RuntimeError, match="downgrade would discard retained data"):
                l5_wa_profile_claims.downgrade()
            assert connection.execute(text("SELECT count(*) FROM l5_service_profile_claims")).scalar_one() == 1
            connection.execute(text("DELETE FROM l5_service_profile_claims"))
            l5_wa_profile_claims.downgrade()
            assert "l5_service_profile_claims" not in inspect(connection).get_table_names()
    engine.dispose()
