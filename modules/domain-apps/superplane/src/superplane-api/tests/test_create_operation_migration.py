"""Schema checks for replay-safe workspace and deployment creation."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_create_operation_migration_is_the_single_head():
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))

    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["039_controller_node_commands"]
    assert (
        scripts.get_revision("039_controller_node_commands").down_revision
        == "038_cluster_grant_scopes"
    )
    assert (
        scripts.get_revision("038_cluster_grant_scopes").down_revision
        == "037_shared_cluster_membership"
    )
    assert (
        scripts.get_revision("037_shared_cluster_membership").down_revision
        == "036_users_cognito_sub_per_org"
    )
    assert (
        scripts.get_revision("036_users_cognito_sub_per_org").down_revision
        == "035_controller_network_journal"
    )
    assert (
        scripts.get_revision("035_controller_network_journal").down_revision
        == "034_provider_request_region"
    )
    assert (
        scripts.get_revision("034_provider_request_region").down_revision
        == "033_retained_batch_results"
    )
    assert (
        scripts.get_revision("031_controller_deployment_registry").down_revision
        == "030_merge_audit_lifecycle"
    )
    assert set(scripts.get_revision("030_merge_audit_lifecycle").down_revision) == {
        "029_lifecycle_control_registry",
        "029_add_event_principal_outcome",
    }
    assert (
        scripts.get_revision("029_add_event_principal_outcome").down_revision
        == "028_deployment_namespace_quota"
    )
    assert (
        scripts.get_revision("028_deployment_namespace_quota").down_revision
        == "027_cli_bootstrap_foundation"
    )
    assert set(scripts.get_revision("027_cli_bootstrap_foundation").down_revision) == {
        "021_deployment_identity",
        "018_bootstrap_read_tokens",
    }
    assert (
        scripts.get_revision("029_lifecycle_control_registry").down_revision
        == "028_merge_lifecycle_foundation"
    )
    assert set(
        scripts.get_revision("028_merge_lifecycle_foundation").down_revision
    ) == {
        "026_merge_lifecycle_effects",
        "027_cli_bootstrap_foundation",
    }
    assert set(scripts.get_revision("026_merge_lifecycle_effects").down_revision) == {
        "025_merge_governed_runtime",
        "020_lifecycle_effects",
    }
    assert set(scripts.get_revision("025_merge_governed_runtime").down_revision) == {
        "024_approval_creation_scope",
        "018_bootstrap_read_tokens",
        "018_controller_executions",
    }
    assert (
        scripts.get_revision("023_operation_governance").down_revision
        == "022_merge_budget_cli"
    )
    assert set(scripts.get_revision("022_merge_budget_cli").down_revision) == {
        "018_add_operation_budget_reservations",
        "021_deployment_identity",
    }
    assert (
        scripts.get_revision("021_deployment_identity").down_revision
        == "020_merge_workspace_cli"
    )
    head = scripts.get_revision("020_merge_workspace_cli")
    assert set(head.down_revision) == {
        "019_workspace_operation_state",
        "017_add_workspace_bootstrap_reservations",
    }
    revision = scripts.get_revision("019_workspace_operation_state")
    assert revision.down_revision == "018_create_operation_idempotency"
