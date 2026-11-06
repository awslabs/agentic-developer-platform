"""Generated Identity and MCP pages; not live Atlassian catalogue evidence."""

import hashlib

import pytest

from tests.e2e.tools.atlassian_catalogue import canonical, discover
from tests.e2e.tools.atlassian_contract import load_contract
from tests.e2e.tools.atlassian_oauth_probe import IDENTITY_SCOPES, ProbeBlocked
from tests.e2e.tools.test_atlassian_oauth_probe import IdentityFixture
from tests.e2e.tools.test_atlassian_probe_ledger import ledger_for


class Pages:
    def __init__(self, contract):
        self.contract = contract
        self.calls = []
        self.tools = [{"name": operation["method"], "inputSchema": {"type": "object", "properties": {}}} for operation in contract["operations"]]
        self.pages = [self.tools[:4], self.tools[4:]]
        self.cursors = ["page-2", None]

    def send(self, method, params, token):
        self.calls.append((method, params, token))
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-03-26"}}
        if method == "notifications/initialized":
            return None
        index = 0 if not params else 1
        return {"result": {"tools": self.pages[index], "nextCursor": self.cursors[index]}}


def ready_ledger(tmp_path):
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    ledger = ledger_for(tmp_path / "metadata.json", core, adapter)
    browser = ledger.begin("owner-a")
    ledger.complete("owner-a", browser["state"])
    return ledger, adapter


def test_paginated_identity_authorized_catalogue_and_schema_digest(tmp_path):
    ledger, adapter = ready_ledger(tmp_path)
    contract = load_contract()
    pages = Pages(contract)
    scopes = [operation["scope"] for operation in contract["operations"]] + list(IDENTITY_SCOPES)
    hashes = {tool["name"]: hashlib.sha256(canonical(tool["inputSchema"])).hexdigest() for tool in pages.tools}
    report = discover(ledger, "owner-a", "selected-site", scopes, {"selected-site"}, hashes, lambda _: pages)
    assert report["status"] == "candidate_match"
    assert len(report["schema_sha256"]) == 8 and report["tool_count"] == 8
    assert [call[1] for call in pages.calls if call[0] == "tools/list"] == [{}, {"cursor": "page-2"}]
    assert all(call[2] == "ephemeral-sentinel" for call in pages.calls)
    assert "ephemeral-sentinel" not in ledger.path.read_text()
    assert adapter.calls[1][1]["workloadIdentityToken"] == "adapter-workload"
    assert discover(ledger, "owner-a", "selected-site", scopes, {"selected-site"}, transport_factory=lambda _: Pages(contract))["status"] == "blocked_unpinned_schemas"


@pytest.mark.parametrize("fault", ["missing", "duplicate", "cursor_loop", "legacy", "bad_schema", "changed_pin", "page_error", "protocol"])
def test_incomplete_or_legacy_catalogue_is_not_accepted(tmp_path, fault):
    ledger, adapter = ready_ledger(tmp_path)
    contract = load_contract()
    pages = Pages(contract)
    scopes = {operation["scope"] for operation in contract["operations"]} | set(IDENTITY_SCOPES)
    pins = {tool["name"]: hashlib.sha256(canonical(tool["inputSchema"])).hexdigest() for tool in pages.tools}
    if fault == "missing":
        pages.pages[1] = pages.pages[1][1:]
    elif fault == "duplicate":
        pages.pages[1].append(pages.pages[0][0])
    elif fault == "cursor_loop":
        pages.cursors[1] = "page-2"
    elif fault == "legacy":
        scopes = {"read:jira-work"}
    elif fault == "bad_schema":
        pages.pages[0][0] = {"name": pages.pages[0][0]["name"], "inputSchema": {"type": "string"}}
    elif fault == "changed_pin":
        pins[pages.tools[0]["name"]] = "0" * 64
    elif fault == "page_error":
        original = pages.send
        pages.send = lambda method, params, token: {"error": {"message": "secret"}} if method == "tools/list" else original(method, params, token)
    elif fault == "protocol":
        original = pages.send
        pages.send = lambda method, params, token: {"result": {"protocolVersion": "unknown"}} if method == "initialize" else original(method, params, token)
    with pytest.raises(ProbeBlocked):
        discover(ledger, "owner-a", "selected-site", scopes, {"selected-site"}, pins, lambda _: pages)


def test_denied_site_or_owner_makes_no_provider_request(tmp_path):
    ledger, adapter = ready_ledger(tmp_path)
    contract = load_contract()
    pages = Pages(contract)
    scopes = {operation["scope"] for operation in contract["operations"]} | set(IDENTITY_SCOPES)
    with pytest.raises(ProbeBlocked):
        discover(ledger, "owner-a", "wrong-site", scopes, {"selected-site"}, transport_factory=lambda _: pages)
    with pytest.raises(ProbeBlocked):
        discover(ledger, "owner-b", "selected-site", scopes, {"selected-site"}, transport_factory=lambda _: pages)
    assert not pages.calls
    assert not adapter.calls


def test_shared_read_me_is_v2_but_lacks_site_or_refresh_without_full_grant(tmp_path):
    ledger, adapter = ready_ledger(tmp_path)
    contract = load_contract()
    pages = Pages(contract)
    product_scopes = {operation["scope"] for operation in contract["operations"]}
    with pytest.raises(ProbeBlocked):
        discover(ledger, "owner-a", "selected-site", product_scopes | {"read:me"}, {"selected-site"}, transport_factory=lambda _: pages)
    assert pages.calls == [] and adapter.calls == []
