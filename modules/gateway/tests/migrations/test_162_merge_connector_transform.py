"""Connector authority and Transform cleanup remain reachable from either head."""

import pytest

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT
from tests.migrations.head_contract import GATEWAY_HEAD


def test_join_keeps_both_published_parent_histories():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("162_merge_connector_transform").down_revision == (
        "161_merge_connector_finops",
        "157_merge_transform_inquiry",
    )
    assert scripts.get_revision("161_merge_connector_finops").down_revision == (
        "160_connector_cell_replay",
        "156_l5_finops_inquiry",
    )
    assert scripts.get_revision("157_merge_transform_inquiry").down_revision == (
        "156_merge_transform_retry",
        "156_l5_finops_inquiry",
    )


@pytest.mark.parametrize(
    "previous_heads, pending_revisions",
    [
        (
            ("157_merge_transform_inquiry",),
            {
                "156_connector_cell_authority",
                "157_connector_operation_contexts",
                "158_connector_policy_deps",
                "159_connector_module_admission",
                "160_connector_cell_replay",
                "161_merge_connector_finops",
                "162_merge_connector_transform",
            },
        ),
        (
            ("161_merge_connector_finops",),
            {
                "154_l5_transform_cleanup",
                "155_merge_finops_transform",
                "156_merge_transform_retry",
                "157_merge_transform_inquiry",
                "162_merge_connector_transform",
            },
        ),
        (
            ("157_merge_transform_inquiry", "161_merge_connector_finops"),
            {"162_merge_connector_transform"},
        ),
    ],
)
def test_upgrade_plan_applies_only_missing_branch_revisions(previous_heads, pending_revisions):
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    revisions = list(scripts.iterate_revisions("162_merge_connector_transform", previous_heads, implicit_base=True))
    assert {revision.revision for revision in revisions} == pending_revisions
    assert len(revisions) == len(pending_revisions)
