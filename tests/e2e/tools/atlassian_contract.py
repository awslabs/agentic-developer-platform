"""Validate the provisional Atlassian tool contract before provider comparison.

Public method names do not establish input JSON Schemas. Every schema name is
reserved but non-executable until independent authenticated discovery pins it.
"""

import json
from pathlib import Path

from tests.e2e.tools.atlassian_oauth_probe import IDENTITY_SCOPES, READ_SCOPES, RESOURCE, ProbeBlocked


CONTRACT_PATH = Path(__file__).resolve().parents[3] / "docs/tools/atlassian-mcp/required-operations.json"
METHODS = {
    "atlassian___search_jira": "searchJiraIssuesUsingJql",
    "atlassian___get_jira_issue": "getJiraIssue",
    "atlassian___search_confluence": "searchConfluence",
    "atlassian___get_confluence_page": "getConfluenceContent",
    "atlassian___create_jira_issue": "createJiraIssue",
    "atlassian___update_jira_issue": "editJiraIssue",
    "atlassian___comment_jira_issue": "addOrEditJiraIssueComment",
    "atlassian___transition_jira_issue": "transitionJiraIssue",
}


def load_contract(path=CONTRACT_PATH):
    contract = json.loads(Path(path).read_text())
    if contract.get("resource") != RESOURCE or contract.get("discovery_url") != RESOURCE + "?tools=all":
        raise ProbeBlocked("MCP v2 resource and discovery endpoint must be pinned")
    if contract.get("schema_state") != "unobserved_requires_authenticated_discovery":
        raise ProbeBlocked("Do not declare live schemas verified from public documentation")
    if contract.get("identity_scopes") != list(IDENTITY_SCOPES):
        raise ProbeBlocked("Subject/site observation and Identity-managed refresh scopes must be explicit")
    operations = contract.get("operations", [])
    if len(operations) != len(METHODS) or {entry["alias"] for entry in operations} != set(METHODS):
        raise ProbeBlocked("The complete eight-operation contract is required")
    for entry in operations:
        if entry["method"] != METHODS[entry["alias"]] or not entry.get("schema_name"):
            raise ProbeBlocked("Upstream method or named schema is missing")
        if entry["mode"] == "read" and entry["scope"] not in READ_SCOPES:
            raise ProbeBlocked("Legacy or unknown scope cannot authorize an MCP read")
        if entry["mode"] == "write" and entry["scope"] != "write:jira:agent-interface":
            raise ProbeBlocked("An MCP write requires separate Jira write scope")
        if entry["site"] not in {"selected_jira_site", "selected_confluence_site"}:
            raise ProbeBlocked("Site authority must be explicit")
        if not entry.get("producer") or not entry.get("consumer"):
            raise ProbeBlocked("Both schema producer and execution consumer must be named")
    retries = contract["retry"]
    if retries["read_retry_count"] > 1 or retries["write_retry_after_dispatch"] != 0:
        raise ProbeBlocked("Automatic write replay or unbounded read retry is forbidden")
    if retries["total_upstream_seconds"] > 20 or retries["read_seconds"] > 5 or retries["connect_seconds"] > 2:
        raise ProbeBlocked("Upstream deadlines must fit the bridge request budget")
    if retries["catalogue_max_pages"] > 32 or retries["catalogue_max_tools"] > 1024:
        raise ProbeBlocked("Catalogue pagination must be bounded")
    for error in ("scope_not_granted", "site_denied", "outcome_unknown", "catalogue_incomplete", "provider_contract_unverified"):
        if error not in contract["errors"].values():
            raise ProbeBlocked("A required denial or uncertain outcome has no mapping")
    return contract


def require_observed_schema(operation, observed_sha256):
    if not isinstance(observed_sha256, str) or len(observed_sha256) != 64 or any(char not in "0123456789abcdef" for char in observed_sha256):
        raise ProbeBlocked(f"{operation['schema_name']} lacks an authenticated SHA-256 schema pin")
    return {"schema_name": operation["schema_name"], "observed_schema_sha256": observed_sha256}
