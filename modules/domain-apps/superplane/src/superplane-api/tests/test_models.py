"""Tests verifying all SQLAlchemy models are importable and have correct table names."""

import app.models  # noqa: F401 — triggers model registration with Base
from app.database import Base


def test_all_tables_registered():
    """All tables from design doc sections 6.1, 15.7, and auth are registered."""
    expected_tables = {
        "organizations",
        "workspaces",
        "clusters",
        "node_pools",
        "nodes",
        "deployments",
        "events",
        "credential_registry",
        "cluster_vault_assignments",
        "credential_audit_log",
        "cloud_accounts",
        "reconcile_locks",
        "api_keys",
        "research_findings",
        "research_proposals",
        "budget_alerts",
        "users",
        # Per-workspace authorization grants (issue #5055, U14 — R6). The record
        # that a named principal may act on one workspace. Before it there was no
        # schema able to express that, so authority was the caller's organization
        # and every org-mate reached every workspace in it.
        "workspace_grants",
    }
    actual_tables = set(Base.metadata.tables.keys())
    assert expected_tables == actual_tables, (
        f"Missing: {expected_tables - actual_tables}, Extra: {actual_tables - expected_tables}"
    )


def test_organization_columns():
    """Organization table has required columns."""
    table = Base.metadata.tables["organizations"]
    col_names = {c.name for c in table.columns}
    assert {
        "id",
        "name",
        "cognito_sub",
        "billing_plan",
        "quotas_json",
        "created_at",
    } <= col_names


def test_workspace_has_isolation_mode():
    """Workspace table includes isolation_mode from section 15.7."""
    table = Base.metadata.tables["workspaces"]
    col_names = {c.name for c in table.columns}
    assert "isolation_mode" in col_names
    assert "shared_cluster_id" in col_names
    assert "namespace_name" in col_names


def test_cluster_has_heartbeat_fields():
    """Cluster table has endpoint, health_status, last_heartbeat from issue requirements."""
    table = Base.metadata.tables["clusters"]
    col_names = {c.name for c in table.columns}
    assert {"endpoint", "health_status", "last_heartbeat"} <= col_names


def test_credential_registry_stores_arns_not_values():
    """Credential registry has secret_arn but no secret_value column."""
    table = Base.metadata.tables["credential_registry"]
    col_names = {c.name for c in table.columns}
    assert "secret_arn" in col_names
    assert "secret_value" not in col_names
