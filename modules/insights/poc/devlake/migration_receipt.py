import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import publish


SCHEMA = Path(__file__).with_name("schema-manifest.json")
HOST = re.compile(r"[a-zA-Z0-9.-]+\.rds\.amazonaws\.com\Z")
RESOURCE_ID = re.compile(r"(?:db|cluster)-[A-Za-z0-9]+\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
ROLE = re.compile(r"[A-Za-z0-9+=,.@_-]{1,64}\Z")
TABLE = re.compile(r"(?:_raw_(?:github|jira|slack|sonarqube|jenkins|argocd|gitlab|pagerduty|linear)_[a-z0-9_]+|_tool_(?:github|jira|slack|sonarqube|jenkins|argocd|gitlab|pagerduty|linear)_[a-z0-9_]+|_devlake_subtask_states|[a-z][a-z0-9_]*)\Z")
STAGING_TABLES = {"collection_generations", "native_connections", "collection_batch_manifests", "collection_batch_items", "collection_checkpoints"}
RAW_TABLES = {"_raw_github_scopes", "_raw_jira_scopes", "_raw_github_api_issues",
              "_raw_github_api_pull_requests", "_raw_jira_api_boards", "_raw_jira_api_issues",
              "_raw_slack_channel", "_raw_slack_channel_message",
              "_raw_sonarqube_api_projects", "_raw_sonarqube_api_issues", "_raw_jenkins_api_jobs", "_raw_jenkins_api_builds", "_raw_argocd_api_applications",
              "_raw_argocd_api_sync_operations", "_raw_gitlab_api_pipeline_details",
              "_raw_pagerduty_services", "_raw_pagerduty_incidents",
              "_raw_linear_teams", "_raw_linear_issues"}
TOOL_TABLES = {"_tool_github_repos", "_tool_jira_boards", "_tool_github_issues",
               "_tool_github_pull_requests", "_tool_jira_issues",
               "_tool_slack_channels", "_tool_slack_channel_messages",
               "_tool_sonarqube_projects", "_tool_sonarqube_issues",
               "_tool_sonarqube_issue_code_blocks", "_tool_sonarqube_issue_impacts",
               "_tool_jenkins_builds", "_tool_jenkins_build_commits",
               "_tool_argocd_applications", "_tool_argocd_sync_operations",
               "_tool_argocd_revision_images", "_tool_gitlab_pipelines",
               "_devlake_subtask_states",
               "_tool_pagerduty_services", "_tool_pagerduty_incidents",
               "_tool_pagerduty_assignments", "_tool_pagerduty_users",
               "_tool_linear_teams", "_tool_linear_issues",
               "_tool_linear_issue_labels", "_tool_linear_accounts"}
DOMAIN_TABLES = {"repos", "boards", "issues", "pull_requests", "cq_projects", "cq_issues", "cicd_pipelines", "cicd_tasks", "cicd_scopes",
                 "cicd_deployments", "cicd_deployment_commits",
                 "board_issues", "issue_assignees"}


def checked_manifest():
    manifest = json.loads(SCHEMA.read_text())
    staging = manifest.get("staging", {})
    raw = manifest.get("raw", {})
    if not isinstance(staging, dict) or not isinstance(raw, dict):
        raise ValueError("migration_required: pinned schema inventory is invalid")
    groups = (raw.get("tables"), manifest.get("tool"), manifest.get("domain"),
              staging.get("tables"), staging.get("generation_guards"))
    if not all(isinstance(names, list) for names in groups):
        raise ValueError("migration_required: pinned schema inventory is invalid")
    tables = groups[0] + groups[1] + groups[2]
    guards = staging.get("generation_guards", [])
    digest = manifest.get("migration_sql_sha256")
    if (manifest.get("schema_version") != 2 or staging.get("status") != "ready" or
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
            not all(isinstance(name, str) and TABLE.fullmatch(name) for name in tables) or
            not all(name.startswith(("_raw_github_", "_raw_jira_", "_raw_slack_", "_raw_sonarqube_", "_raw_jenkins_", "_raw_argocd_", "_raw_gitlab_", "_raw_pagerduty_", "_raw_linear_")) for name in groups[0]) or
            not all(isinstance(name, str) and TABLE.fullmatch(name) for name in staging["tables"] + guards) or
            set(groups[0]) != RAW_TABLES or set(groups[1]) != TOOL_TABLES or set(groups[2]) != DOMAIN_TABLES or
            set(staging["tables"]) != STAGING_TABLES or
            len(tables) != len(set(tables)) or
            not set(tables).issubset(guards) or
            not (set(staging["tables"]) - {"native_connections"}).issubset(guards)):
        raise ValueError("migration_required: pinned schema lacks complete tables and generation guards")
    return manifest


def readback(host, database, cell, resource_id, username, ca_path, region, account, role, label):
    if (not HOST.fullmatch(host) or not all(IDENTIFIER.fullmatch(value) for value in (database, cell)) or
            not ROLE.fullmatch(username) or not ROLE.fullmatch(role) or
            not RESOURCE_ID.fullmatch(resource_id) or
            not re.fullmatch(r"[0-9]{12}", account) or not label or
            not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region) or
            not Path(ca_path).is_file()):
        raise ValueError("registered database target and verified CA are required")

    identity = json.loads(publish.broker(label, "aws", "sts", "get-caller-identity", "--output", "json"))
    if (identity.get("Account") != account or
            not identity.get("Arn", "").startswith(f"arn:aws:sts::{account}:assumed-role/{role}/")):
        raise ValueError("selected migration role differs from approved account or role")
    token = publish.broker(label, "aws", "rds", "generate-db-auth-token", "--hostname", host,
                           "--port", "5432", "--username", username, "--region", region)
    if not token:
        raise ValueError("fresh IAM database token is unavailable")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update(PGPASSWORD=token, PGSSLMODE="verify-full", PGSSLROOTCERT=str(ca_path))
    statement = ("SELECT manifest_sha256 FROM devlake_meta.schema_receipts "
                 f"WHERE cell_id = '{cell}' AND db_resource_id = '{resource_id}' "
                 "AND status = 'applied' AND schema_version = 2")
    try:
        completed = subprocess.run(
            ["psql", "-X", "-w", "-A", "-t", "-q", "--set=ON_ERROR_STOP=1", "--host", host,
             "--port", "5432", "--username", username, "--dbname", database, "--command", statement],
            env=environment, capture_output=True, text=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        raise ValueError("IAM/TLS migration readback failed") from None
    results = completed.stdout.splitlines()
    if len(results) != 1 or not re.fullmatch(r"[0-9a-f]{64}", results[0]):
        raise ValueError("migration receipt is absent or ambiguous")
    return results[0]


def verify_migrated(reader, **target):
    checked_manifest()
    digest = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    if reader(**target) != digest:
        raise ValueError("migration_required: database schema receipt differs from pinned manifest")
    return digest
