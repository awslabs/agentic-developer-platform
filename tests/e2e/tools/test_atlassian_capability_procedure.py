"""SCN-02 generated documentation/catalogue comparison, not live provider proof."""

import hashlib

import pytest

from tests.e2e.tools.atlassian_catalogue import canonical
from tests.e2e.tools.atlassian_contract import load_contract
from tests.e2e.tools.atlassian_oauth_probe import IDENTITY_SCOPES, ProbeBlocked, proof_capability_contract
from tests.e2e.tools.test_atlassian_catalogue import Pages, ready_ledger


DOCUMENTED_METHODS = {
    "searchJiraIssuesUsingJql": "search:jira:agent-interface",
    "getJiraIssue": "read:jira:agent-interface",
    "searchConfluence": "search:confluence:agent-interface",
    "getConfluenceContent": "read:confluence:agent-interface",
    "createJiraIssue": "write:jira:agent-interface",
    "editJiraIssue": "write:jira:agent-interface",
    "addOrEditJiraIssueComment": "write:jira:agent-interface",
    "transitionJiraIssue": "write:jira:agent-interface",
}


def inputs(contract, pages):
    scopes = set(DOCUMENTED_METHODS.values()) | set(IDENTITY_SCOPES)
    catalogue_hash = hashlib.sha256(canonical({tool["name"]: tool for tool in pages.tools})).hexdigest()
    schema_hashes = {tool["name"]: hashlib.sha256(canonical(tool["inputSchema"])).hexdigest() for tool in pages.tools}
    return {
        "owner": "owner-a",
        "selected_site": "private-fixture-site",
        "granted_scopes": scopes,
        "accessible_sites": {"private-fixture-site"},
        "documented_methods": DOCUMENTED_METHODS,
        "documented_tools_sha256": contract["documented_tools_sha256"],
        "expected_catalogue_sha256": catalogue_hash,
        "pinned_schema_hashes": schema_hashes,
        "transport_factory": lambda _: pages,
    }


def test_generated_proof_pins_full_catalogue_and_redacts_site(tmp_path):
    ledger, adapter = ready_ledger(tmp_path)
    contract = load_contract()
    pages = Pages(contract)
    report = proof_capability_contract(ledger, **inputs(contract, pages))
    assert report["scenario_id"] == "SCN-02" and report["controlled_status"] == "pass"
    assert report["evidence_kind"] == "generated-fixture" and report["live_status"] == "unproven"
    assert report["tool_count"] == 8 and len(report["schema_sha256"]) == 8
    assert "private-fixture-site" not in str(report)
    assert "ephemeral-sentinel" not in str(report) and "ephemeral-sentinel" not in ledger.path.read_text()
    assert [call[0] for call in pages.calls] == ["initialize", "notifications/initialized", "tools/list", "tools/list"]


@pytest.mark.parametrize("fault", ["legacy", "undocumented", "doc_drift", "schema_drift", "catalogue_drift", "no_schema_pin", "missing_last_page", "loop"])
def test_generated_proof_rejects_missing_documentation_or_provider_drift(tmp_path, fault):
    ledger, adapter = ready_ledger(tmp_path)
    contract = load_contract()
    pages = Pages(contract)
    params = inputs(contract, pages)
    if fault == "legacy":
        params["granted_scopes"] = {"read:jira-work", "read:me"}
    elif fault == "undocumented":
        params["documented_methods"] = {**DOCUMENTED_METHODS, "getJiraIssue": "read:jira-work"}
    elif fault == "doc_drift":
        params["documented_tools_sha256"] = "0" * 64
    elif fault == "schema_drift":
        pages.pages[1][0] = {**pages.pages[1][0], "inputSchema": {"type": "object", "properties": {"new": {"type": "string"}}}}
    elif fault == "catalogue_drift":
        params["expected_catalogue_sha256"] = "0" * 64
    elif fault == "no_schema_pin":
        params["pinned_schema_hashes"] = None
    elif fault == "missing_last_page":
        pages.pages[1] = pages.pages[1][1:]
    elif fault == "loop":
        pages.cursors[1] = "page-2"
    with pytest.raises(ProbeBlocked):
        proof_capability_contract(ledger, **params)
