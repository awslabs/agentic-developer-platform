"""Connector authority and billing collections converge from either deployed head."""

import pytest

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT
from tests.migrations.head_contract import GATEWAY_HEAD


def test_join_preserves_connector_and_billing_parent_histories():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("163_merge_connector_cost").down_revision == (
        "162_merge_connector_transform",
        "158_l5_cost_collections",
    )
    assert scripts.get_revision("162_merge_connector_transform").down_revision == (
        "161_merge_connector_finops",
        "157_merge_transform_inquiry",
    )
    assert scripts.get_revision("158_l5_cost_collections").down_revision == "157_merge_transform_inquiry"
    assert all(len(revision.revision) <= 32 for revision in scripts.walk_revisions())


@pytest.mark.parametrize(
    "previous_heads, pending_revisions",
    [
        (
            ("162_merge_connector_transform",),
            {"158_l5_cost_collections", "163_merge_connector_cost", "164_l5_work_delegation_parent"},
        ),
        (
            ("158_l5_cost_collections",),
            {
                "156_connector_cell_authority",
                "157_connector_operation_contexts",
                "158_connector_policy_deps",
                "159_connector_module_admission",
                "160_connector_cell_replay",
                "161_merge_connector_finops",
                "162_merge_connector_transform",
                "163_merge_connector_cost",
                "164_l5_work_delegation_parent",
            },
        ),
        (
            ("162_merge_connector_transform", "158_l5_cost_collections"),
            {"163_merge_connector_cost", "164_l5_work_delegation_parent"},
        ),
    ],
)
def test_upgrade_plan_applies_only_missing_branch_revisions(previous_heads, pending_revisions):
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    pending_revisions = pending_revisions | {
        "155_l5_wa_profile_claims",
        "156_merge_wa_publication_retry",
        "157_merge_wa_finops_inquiry",
        "158_merge_wa_transform",
        "159_merge_wa_cost",
        "164_merge_wa_connector",
        "165_l5_source_retention",
        "166_l5_wa_generation",
        "167_connector_installation",
        "167_l5_wa_results",
        "168_merge_security_wa",
        "169_merge_collection",
        "170_l5_canonical_draft_owners",
        "171_l5_linked_draft_owners",
        "172_story_task_board",
        "173_l5_execution_scope",
        "174_l5_wa_owner_records",
        "175_l5_setup_authority",
        "176_l5_schedule_dispatch",
        "177_l5_wa_periodic_evidence",
        "178_security_manual_evidence",
        "179_l5_knowledge_effects",
        "180_l5_knowledge_evidence",
        "169_atlassian_system",
        "181_atlassian_forge",
        "182_product_delivery_home",
        GATEWAY_HEAD,
    }
    revisions = list(scripts.iterate_revisions("head", previous_heads, implicit_base=True))
    assert {revision.revision for revision in revisions} == pending_revisions
    assert len(revisions) == len(pending_revisions)
