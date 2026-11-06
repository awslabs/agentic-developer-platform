"""Install gateway L5 bridge migrations without unrelated PostgreSQL-only tables."""

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT


def upgrade_l5_gateway_schema(connection, *, through_revision=None, after_revision=None):
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    with Operations.context(MigrationContext.configure(connection)):
        revisions = (
            "142_l5_security_release",
            "143_merge_l5_conversations",
            "145_l5_integration_observation",
            "148_l5_results_knowledge",
            "150_l5_devops_native_runs",
            "151_l5_devops_prevention_obs",
            "153_l5_publications",
            "154_l5_finops_ownership",
            "154_l5_transform_cleanup",
            "155_l5_publication_retry",
            "155_l5_wa_profile_claims",
            "156_merge_wa_publication_retry",
            "156_l5_finops_inquiry",
            "157_merge_wa_finops_inquiry",
            "155_merge_finops_transform",
            "156_merge_transform_retry",
            "157_merge_transform_inquiry",
            "158_merge_wa_transform",
            "158_l5_cost_collections",
            "159_merge_wa_cost",
            "165_l5_source_retention",
            "166_l5_wa_generation",
            "167_l5_wa_results",
            "168_merge_security_wa",
            "174_l5_wa_owner_records",
            "175_l5_setup_authority",
            "176_l5_schedule_dispatch",
            "177_l5_wa_periodic_evidence",
            "178_security_manual_evidence",
            "179_l5_knowledge_effects",
            "180_l5_knowledge_evidence",
        )
        start = revisions.index(after_revision) + 1 if after_revision is not None else 0
        for revision in revisions[start:]:
            scripts.get_revision(revision).module.upgrade()
            if revision == through_revision:
                break
