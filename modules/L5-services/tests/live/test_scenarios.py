"""Offline selector checks; they cannot replace served browser/MCP execution."""

from copy import deepcopy
import json
import time

import pytest

from fixtures import load_live_fixture
from scenarios import clients_and_setup


@pytest.fixture
def setup_case():
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "relationship_revision": "1",
             "owner_id": "owner-example", "visibility": "only_me"}
    sources = [{"tenant_id": "tenant-example", "project_id": "project-example",
                "source_connection_id": "connection-example", "record_type": "document",
                "record_id": "source-example", "record_revision": "source-revision-example"}]
    selected = {"scope": scope, "configuration_id": "config-example", "binding_id": "binding-example",
                "sources": sources, "binding_state": "ready", "readiness": [],
                "connection_ref": {"kind": "credential", "id": "account-example"},
                "provider_identity": {"account_id": "000000000000", "region": "region-example"}}
    case = {"scope": scope, "selected": selected, "initial_region": "region-initial", "cli_region": "region-cli",
            "ui_region": "region-ui", "ui_reason": "independent change", "cli_settings_file": "/private/settings.json",
            "cli_receipt": "cli-receipt-example", "mcp_receipt": "mcp-receipt-example",
            "run_input": {"binding_id": "binding-example", "sources": sources, "capability": "threat_model"},
            "provider": {"label": "private-account-example"}}
    config = {"scope": scope, "configuration_id": "config-example", "revision": 1,
              "hash": "hash-initial", "etag": '"hash-initial"', "settings": {"region": "region-initial"}}
    binding = {"scope": scope, "configuration_id": "config-example", "binding_id": "binding-example",
               "revision": 2, "state": "ready", "readiness": [], "connection_ref": selected["connection_ref"],
               "provider_identity": selected["provider_identity"]}
    run = {"scope": scope, "run_id": "run-example", "revision": 1,
           "input": {"configuration_revision": 3, "binding_id": "binding-example", "sources": sources},
           "admission": {"configuration_id": "config-example", "configuration_revision": 3}}
    return case, config, binding, run


class RecordingDriver:
    def __init__(self, case, config, binding, run):
        self.case, self.config, self.binding, self.run = case, config, binding, run
        self.calls = []

    def cli_configuration(self):
        self.calls.append("cli_read")
        return deepcopy(self.config)

    def cli_binding(self, binding_id):
        self.calls.append("binding_read")
        return deepcopy(self.binding)

    def read_api(self, path):
        self.calls.append("api_read")
        if path.endswith("/configuration"):
            return deepcopy(self.config)
        if path.endswith("/bindings/binding-example"):
            return deepcopy(self.binding)
        if path.endswith("/runs/run-example"):
            return deepcopy(self.run)
        raise AssertionError("foreign readback path")

    def cli_update(self, path, etag, receipt):
        self.calls.append("cli_update")
        assert etag == '"hash-initial"' and receipt == self.case["cli_receipt"]
        self.config.update(revision=2, hash="hash-cli", etag='"hash-cli"', settings={"region": "region-cli"})
        return deepcopy(self.config)

    def ui_edit(self, region, reason):
        self.calls.append("ui_edit")
        assert region == "region-ui" and reason == self.case["ui_reason"]
        self.config.update(revision=3, hash="hash-ui", etag='"hash-ui"', settings={"region": "region-ui"})
        return deepcopy(self.config)

    def verify_provider_authority(self, provider):
        self.calls.append("aws_role_check")

    def mcp_submit(self, run_input, receipt):
        self.calls.append("mcp_submit")
        assert run_input["configuration_revision"] == 3 and receipt == self.case["mcp_receipt"]
        return deepcopy(self.run)

    def attempts(self, selected):
        self.calls.append("journal_read")
        return [{"surface": surface, "operation": operation, "effect_id": selected["effects"][operation]}
                for surface, operation in (("cli", "cli_update"), ("ui", "ui_edit"), ("mcp", "mcp_submit"))]

    def cli_observations(self, binding_id, run_id):
        self.calls.append("cli_verify")
        return deepcopy({"configuration": self.config, "binding": self.binding, "run": self.run})

    def ui_observations(self, binding_id, run_id):
        self.calls.append("ui_verify")
        return deepcopy({"configuration": self.config, "binding": self.binding, "run": self.run})

    def mcp_observations(self, binding_id, run_id):
        self.calls.append("mcp_verify")
        return deepcopy({"configuration": self.config, "binding": self.binding, "run": self.run})


def test_client_setup_orders_real_operation_contracts(setup_case):
    case, config, binding, run = deepcopy(setup_case)
    driver = RecordingDriver(case, config, binding, run)
    result = clients_and_setup(case, {"max_attempts": 3}, driver)
    assert result == {"configuration_revision": 3, "binding_revision": 2,
                      "run_revision": 1, "run_id": "run-example", "attempt_count": 3}
    assert driver.calls.index("api_read") < driver.calls.index("cli_update")
    assert driver.calls.index("aws_role_check") < driver.calls.index("mcp_submit")
    assert driver.calls.count("mcp_submit") == 1
    assert driver.calls[-3:] == ["api_read"] * 3


def test_setup_rejects_wrong_deployment_before_mutation(setup_case):
    case, config, binding, run = deepcopy(setup_case)
    driver = RecordingDriver(case, config, binding, run)
    driver.config["etag"] = '"wrong"'
    with pytest.raises(AssertionError, match="selected deployment revision"):
        clients_and_setup(case, {"max_attempts": 3}, driver)
    assert "cli_update" not in driver.calls and "mcp_submit" not in driver.calls


def test_setup_rejects_unpinned_source_before_paid_call(setup_case):
    case, config, binding, run = deepcopy(setup_case)
    case["run_input"]["sources"] = [{**case["run_input"]["sources"][0], "record_revision": "other"}]
    driver = RecordingDriver(case, config, binding, run)
    with pytest.raises(AssertionError, match="selected binding or source revisions"):
        clients_and_setup(case, {"max_attempts": 3}, driver)
    assert "aws_role_check" not in driver.calls and "cli_update" not in driver.calls and "mcp_submit" not in driver.calls


def test_private_fixture_requires_two_scopes_and_revisions(tmp_path, monkeypatch):
    path = tmp_path / "live.json"
    first_home = tmp_path / "cli-a"
    second_home = tmp_path / "cli-b"
    first_home.mkdir(mode=0o700)
    second_home.mkdir(mode=0o700)
    first_scope = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"}
    second_scope = {"tenant_id": "tenant-b", "product_id": "product-b", "project_id": "project-b"}
    first_source = {"tenant_id": "tenant-a", "project_id": "project-a",
                    "record_type": "document", "record_revision": "revision-a"}
    second_source = {**first_source, "tenant_id": "tenant-b", "project_id": "project-b"}
    identity = {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example"}
    case = {"scope": first_scope,
            "selected": {"scope": first_scope, "sources": [first_source], "binding_id": "binding-a",
                         "provider_identity": identity},
            "spend_limit_usd": "0.50", "deployment_id": "deployment-a", "api_url": "https://api.example", "ui_url": "https://ui.example",
            "cli_home": str(first_home), "cli_settings_file": str(tmp_path / "settings.json"),
            "journal_file": str(tmp_path / "journal.jsonl"), "cli_region": "region-a", "ui_region": "region-b",
            "ui_reason": "offline example", "cli_receipt": "cli-receipt-example", "ui_receipt": "ui-receipt-example",
            "mcp_receipt": "mcp-receipt-example", "provider": {"label": "example", "account_id": "000000000000",
            "role_arn": "arn:aws:iam::000000000000:role/Example"},
            "run_input": {"sources": [first_source], "binding_id": "binding-a"},
            "api_token_env": "LIVE_TEST_API", "mcp_token_env": "LIVE_TEST_MCP",
            "ui_access_token_env": "LIVE_TEST_UI", "ui_id_token_env": "LIVE_TEST_ID"}
    (tmp_path / "settings.json").write_text("{}")
    (tmp_path / "settings.json").chmod(0o600)
    for name in ("LIVE_TEST_API", "LIVE_TEST_MCP", "LIVE_TEST_UI", "LIVE_TEST_ID", "LIVE_TEST_API_B", "LIVE_TEST_MCP_B",
                 "LIVE_TEST_UI_B", "LIVE_TEST_ID_B"):
        monkeypatch.setenv(name, "synthetic-not-a-real-token-" + name)
    fixture = {"schema_version": 1, "authorized": True, "fixture_owner": "owner-example",
               "limits": {"max_attempts": 3, "deadline_seconds": 120, "max_spend_usd": "1.00"},
               "release": {name: "a" * 40 for name in ("api", "cli", "ui", "mcp", "worker")},
               "cases": [case, {**deepcopy(case), "scope": second_scope,
                                "selected": {"scope": second_scope, "sources": [second_source],
                                             "binding_id": "binding-b", "provider_identity": identity},
                                "run_input": {"sources": [second_source], "binding_id": "binding-b"},
                                "cli_home": str(second_home), "journal_file": str(tmp_path / "journal-b.jsonl"),
                                "api_token_env": "LIVE_TEST_API_B", "mcp_token_env": "LIVE_TEST_MCP_B",
                                "ui_access_token_env": "LIVE_TEST_UI_B", "ui_id_token_env": "LIVE_TEST_ID_B"}]}
    path.write_text(json.dumps(fixture))
    path.chmod(0o600)
    assert len(load_live_fixture(path)["cases"]) == 2
    path.chmod(0o644)
    with pytest.raises(ValueError, match="access-controlled"):
        load_live_fixture(path)
    path.chmod(0o600)
    fixture["release"]["api"] = "unpinned"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="pinned api revision"):
        load_live_fixture(path)
    fixture["release"]["api"] = "a" * 40
    fixture["cases"][0]["spend_limit_usd"] = "2.00"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="allocations exceed"):
        load_live_fixture(path)
    fixture["cases"][0]["spend_limit_usd"] = "0.50"
    fixture["cases"] = [case, case]
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="two-tenant"):
        load_live_fixture(path)


def test_live_driver_journals_before_effect_without_inventing_run_id(tmp_path):
    from clients_setup import LiveSetupDriver
    from evidence import AttemptJournal

    driver = object.__new__(LiveSetupDriver)
    driver.case = {"selected": {"configuration_id": "config-example"}, "max_attempts": 3}
    driver.journal = AttemptJournal(tmp_path / "attempts.jsonl")
    driver.record("receipt-example", "mcp_submit", "prepared")
    driver.record("receipt-example", "mcp_submit", "acknowledged", "run-example", "receipt-example")
    before, after = driver.journal.read()
    assert before["run_id"] is None and before["effect_id"] is None
    assert after["run_id"] == after["effect_id"] == "run-example"
    assert after["idempotency_key"] == "receipt-example"


def test_live_driver_reconciles_cli_result_and_private_attempts(tmp_path):
    from clients_setup import LiveSetupDriver
    from evidence import AttemptJournal

    driver = object.__new__(LiveSetupDriver)
    driver.case = {"max_attempts": 3, "cli_receipt": "cli-receipt-example",
                   "ui_receipt": "ui-receipt-example", "mcp_receipt": "mcp-receipt-example"}
    driver.journal = AttemptJournal(tmp_path / "attempts.jsonl")
    driver.cli = lambda *args, **kwargs: {"detail": {"configuration_id": "config-example", "revision": 2}}
    assert driver.cli_update("/private/settings.json", '"prior"', driver.case["cli_receipt"])["revision"] == 2
    driver.record(driver.case["ui_receipt"], "ui_edit", "prepared")
    driver.record(driver.case["ui_receipt"], "ui_edit", "acknowledged", "config-example", "generated-ui-key")
    driver.record(driver.case["mcp_receipt"], "mcp_submit", "prepared")
    driver.record(driver.case["mcp_receipt"], "mcp_submit", "acknowledged", "run-example", driver.case["mcp_receipt"])
    selected = {"effects": {"cli_update": "config-example", "ui_edit": "config-example",
                            "mcp_submit": "run-example"}}
    assert len(driver.attempts(selected)) == 3
    with pytest.raises(AssertionError, match="already started"):
        driver.record(driver.case["mcp_receipt"], "mcp_submit", "prepared")
    with pytest.raises(AssertionError, match="selected effect"):
        driver.attempts({"effects": {**selected["effects"], "mcp_submit": "another-run"}})


def test_live_driver_rejects_missing_acknowledgement_and_extra_effect(tmp_path):
    from clients_setup import LiveSetupDriver
    from evidence import AttemptJournal

    driver = object.__new__(LiveSetupDriver)
    driver.case = {"max_attempts": 3, "cli_receipt": "cli-receipt-example",
                   "ui_receipt": "ui-receipt-example", "mcp_receipt": "mcp-receipt-example"}
    driver.journal = AttemptJournal(tmp_path / "attempts.jsonl")
    driver.record(driver.case["cli_receipt"], "cli_update", "prepared")
    with pytest.raises(AssertionError, match="missing an attempt"):
        driver.attempts({"effects": {"cli_update": "config-example"}})
    driver.record(driver.case["cli_receipt"], "cli_update", "acknowledged", "config-example", "cli-receipt-example")
    driver.record(driver.case["ui_receipt"], "ui_edit", "prepared")
    driver.record(driver.case["ui_receipt"], "ui_edit", "acknowledged", "config-example", "ui-key")
    driver.record(driver.case["mcp_receipt"], "mcp_submit", "prepared")
    driver.record(driver.case["mcp_receipt"], "mcp_submit", "acknowledged", "run-example", "mcp-receipt-example")
    driver.record(driver.case["mcp_receipt"], "mcp_submit", "acknowledged", "other-run", "mcp-receipt-example")
    with pytest.raises(AssertionError, match="extra effect"):
        driver.attempts({"effects": {"cli_update": "config-example", "ui_edit": "config-example",
                                    "mcp_submit": "run-example"}})
