"""Offline selector cases for Product recovery and Product-less legacy runs."""

from types import SimpleNamespace
from unittest.mock import Mock
import json
import shutil
from pathlib import Path

import pytest

from tests.e2e.orchestration import run as runner
from tests.e2e.orchestration.config import ConfigError, load_config
from tests.e2e.orchestration.fixtures import FixtureRequest, provision
from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios import (
    DeliveryAdapter,
    ProductEpicDeliveryAdapter,
)
from tests.e2e.orchestration.scenarios.definitions import DEFINITION_HASH
from tests.e2e.orchestration.scenarios.delivery import FlowProvider, build_plan
from tests.e2e.orchestration.scenarios.providers import (
    MARKER,
    IssueProvider,
    ensure_native_parent,
)
from tests.e2e.orchestration.scenarios.http import Unsupported
from tests.e2e.orchestration.scenarios.workers import WorkerProvider
from tests.e2e.orchestration.test_product_epic_config import PRODUCT_EPIC
from tests.e2e.orchestration.test_product_epic_wiring import ProviderTransport

QUALIFICATION_ID = "q-0123456789abcdef"


@pytest.mark.parametrize(
    "selection", [["product-epic-recovery"], ["product-epic-delivery"]]
)
def test_product_selector_requires_product_before_creating_inventory(
    write_config, selection
):
    with pytest.raises(ConfigError, match="product_epic is required"):
        load_config(write_config({"scenarios": selection}))


@pytest.mark.parametrize(
    "selection",
    [["legacy-attribution"], ["product-epic-delivery", "product-epic-recovery"]],
)
def test_product_cannot_mix_legacy_or_duplicate_cases(write_config, selection):
    with pytest.raises(ConfigError, match="avoid duplicate fixtures"):
        load_config(
            write_config({"product_epic": PRODUCT_EPIC, "scenarios": selection})
        )


def test_recovery_selector_uses_existing_resume_without_republication(
    write_config, monkeypatch
):
    config = load_config(
        write_config(
            {
                "product_epic": PRODUCT_EPIC,
                "scenarios": ["product-epic-recovery"],
                "bounds": {"max_resources": 30},
            }
        )
    )
    assert list(runner.load_scenario_adapters(config)) == ["product-epic-recovery"]
    with pytest.raises(ConfigError, match="resume/cleanup-only"):
        runner.run(config)
    assert list(config.artifact_directory.iterdir()) == []

    monkeypatch.setattr(
        runner,
        "_caller_identity",
        lambda: ({"account": config.expected_account_id}, None),
    )
    monkeypatch.setattr(
        runner,
        "verify_target",
        lambda *args: SimpleNamespace(verified=True, to_json=lambda: {}),
    )
    monkeypatch.setattr(
        runner,
        "load_connection_resolver",
        lambda _: SimpleNamespace(verify_product_epic=lambda: None),
    )
    preview = runner.preflight(config)
    assert preview.report["scenarios"] == ["product-epic-recovery"]
    assert preview.report["allowed_modes"] == ["resume", "cleanup"]
    assert preview.report["planned_resources"] == []
    assert preview.report["planned_resource_units"] == 0
    assert preview.report["mutations"] == []
    assert list(config.artifact_directory.iterdir()) == []

    transport = ProviderTransport(config)
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    provider = IssueProvider(transport)
    record = provision(
        inventory,
        config,
        provider,
        FixtureRequest("story-1", provider.kind, f"{QUALIFICATION_ID}/story-1"),
    )
    original_request = transport.request

    def lost_link_response(method, path, **kwargs):
        result = original_request(method, path, **kwargs)
        if method == "POST" and path.endswith("/sub_issues"):
            raise OSError("offline lost link acknowledgement")
        return result

    transport.request = lost_link_response
    with pytest.raises(OSError, match="lost link acknowledgement"):
        ensure_native_parent(inventory, config, provider, record)
    monkeypatch.setattr(
        ProductEpicDeliveryAdapter,
        "fixture_providers",
        lambda self, config: (IssueProvider(transport),),
    )
    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda _: SimpleNamespace(verified=True, to_json=lambda: {}),
    )
    recovered = runner.resume_qualification(config, QUALIFICATION_ID)
    assert recovered.status == runner.STATUS_READY, recovered.report
    assert recovered.report["native_parent_verified"] == ["story-1"]
    assert len(transport.issues) == 1 and len(transport.parents) == 1
    assert (
        sum(
            method == "POST" and path.endswith("/issues")
            for method, path in transport.calls
        )
        == 1
    )
    assert (
        sum(
            method == "POST" and path.endswith("/sub_issues")
            for method, path in transport.calls
        )
        == 1
    )


def test_recovery_adapter_cannot_execute_a_new_delivery(write_config):
    config = load_config(
        write_config(
            {"product_epic": PRODUCT_EPIC, "scenarios": ["product-epic-recovery"]}
        )
    )
    adapter = runner.load_scenario_adapters(config)["product-epic-recovery"]
    assert adapter.planned_fixtures(config) == ()
    with pytest.raises(ConfigError, match="resume/cleanup-only"):
        adapter.execute(config=config, inventory=Mock(), providers={})


def test_recovery_cli_restores_latest_attempt_for_cleanup(
    write_config, tmp_path, monkeypatch, capsys
):
    path = write_config(
        {"product_epic": PRODUCT_EPIC, "scenarios": ["product-epic-recovery"]}
    )
    config = load_config(path)
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    inventory.start_attempt("product-epic-delivery")
    inventory.finish_attempt("failed")
    inventory.record_planned(
        fixture_id="story-1",
        kind="qualification-issue",
        intended_identity=f"{QUALIFICATION_ID}/story-1",
        ownership_tags=config.ownership_tags(QUALIFICATION_ID),
        idempotency_token=f"{QUALIFICATION_ID}-story-1",
    )
    original = tmp_path / "original-artifact"
    shutil.copytree(config.artifact_directory, original)
    inventory.path.unlink()

    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda _: SimpleNamespace(verified=True, to_json=lambda: {}),
    )
    monkeypatch.setattr(runner, "load_providers", lambda _: {})
    assert (
        runner.main(
            [
                "--config",
                str(path),
                "--resume",
                QUALIFICATION_ID,
                "--restore-from",
                str(original),
            ]
        )
        == runner.EXIT_FAILED
    )
    assert "reconcile_failed" in capsys.readouterr().out
    preceding = tmp_path / "resume-artifact"
    shutil.copytree(config.artifact_directory, preceding)
    inventory.path.unlink()

    assert (
        runner.main(
            [
                "--config",
                str(path),
                "--cleanup",
                QUALIFICATION_ID,
                "--restore-from",
                str(preceding),
            ]
        )
        == runner.EXIT_INCOMPLETE
    )
    assert "manual verification required" in capsys.readouterr().out
    retained = Inventory.load(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    assert retained.attempts == [
        {"scenario": "product-epic-delivery", "status": "failed"}
    ]
    assert "no provider registered" in retained.get("story-1").detail


def test_manual_recovery_procedure_requires_latest_inventory():
    procedure = Path(__file__).with_name("PRODUCT_EPIC_RECOVERY.md").read_text()
    assert "procedure v1" in procedure
    assert "product-epic-recovery" in procedure
    assert "mode=preflight" in procedure
    assert "mode=resume" in procedure
    assert "mode=cleanup" in procedure
    assert "resume run ID" in procedure
    assert "latest artifact" in procedure


def test_explicit_legacy_selector_runs_and_resumes_without_product(
    write_config, monkeypatch
):
    from tests.e2e.orchestration.scenarios import delivery

    config = load_config(write_config({"scenarios": ["legacy-attribution"]}))
    assert list(runner.load_scenario_adapters(config)) == ["legacy-attribution"]
    plan = build_plan(
        config,
        SimpleNamespace(execution_policy={}, evaluation={}),
        QUALIFICATION_ID,
        ["10", "11"],
    )
    assert "product_id" not in plan and "origin_ref" not in plan
    monkeypatch.setattr(DeliveryAdapter, "fixture_providers", lambda self, config: ())
    monkeypatch.setattr(delivery, "execute", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda _: SimpleNamespace(verified=True, to_json=lambda: {}),
    )
    outcome = runner.run(config)
    assert outcome.status == runner.STATUS_INCOMPLETE
    assert outcome.report["attempt_history"] == [
        {"scenario": "legacy-attribution", "status": "incomplete"}
    ]
    assert (
        runner.resume_qualification(config, outcome.report["qualification_id"]).status
        == runner.STATUS_READY
    )
    assert (
        runner.cleanup_qualification(config, outcome.report["qualification_id"]).status
        == runner.STATUS_PASS
    )


def test_explicit_legacy_readbacks_refuse_unexpected_product(write_config):
    config = load_config(write_config({"scenarios": ["legacy-attribution"]}))
    tags = config.ownership_tags(QUALIFICATION_ID)
    metadata = {
        "creator_id": 99,
        "identity": f"{QUALIFICATION_ID}/story-1",
        "correlation": f"{QUALIFICATION_ID}-story-1",
        "ownership_tags": tags,
    }
    issue = {"user": {"id": 99}, "body": ""}
    graph = {"slug": QUALIFICATION_ID}
    plan = {"spec_revision": DEFINITION_HASH}
    client = Mock(config=config)

    def get(path, **kwargs):
        if path.endswith("/issues/10"):
            issue["body"] = MARKER + json.dumps(metadata) + " -->"
            return issue
        if path == "/orchestration/flows/flow-1":
            return graph
        if path == "/orchestration/flows/flow-1/plans":
            return [{"plan_document": plan}]
        raise AssertionError(f"unexpected transport read: {path}")

    client.get.side_effect = get
    assert IssueProvider(client).read_tags("10") == tags
    assert FlowProvider(client).read_tags("flow-1") == tags
    metadata["product_id"] = PRODUCT_EPIC["product_id"]
    graph["product_id"] = PRODUCT_EPIC["product_id"]
    plan["product_id"] = PRODUCT_EPIC["product_id"]
    assert IssueProvider(client).read_tags("10") is None
    assert FlowProvider(client).read_tags("flow-1") is None
    client.request.assert_not_called()


def test_explicit_legacy_worker_rejects_changed_product_attribution(write_config):
    config = load_config(write_config({"scenarios": ["legacy-attribution"]}))
    node = {
        "id": "node-1",
        "issue_ref": "10",
        "run_id": "run-1",
        "execution_history": {
            "history_complete": True,
            "runs": [{"invocation_id": "invocation-1"}],
        },
    }
    graph = {"slug": QUALIFICATION_ID, "nodes": [node]}
    plan = {"spec_revision": DEFINITION_HASH}
    client = Mock()
    client.get.side_effect = lambda path: {
        "/orchestration/flows/flow-1": graph,
        "/orchestration/flows/flow-1/plans": [
            {"superseded_at": None, "plan_document": plan}
        ],
        "/me/agent-invocations/invocation-1": {
            "repo": config.repository,
            "issue_number": 10,
            "correlation_id": "run-1",
        },
    }[path]
    provider = WorkerProvider(
        SimpleNamespace(
            config=config,
            client=client,
            inventory=SimpleNamespace(qualification_id=QUALIFICATION_ID),
        )
    )
    resource = {
        "qualification_id": QUALIFICATION_ID,
        "flow_id": "flow-1",
        "node_id": "node-1",
        "invocation_id": "invocation-1",
    }
    assert provider._binding(resource)[0] is node
    graph["product_id"] = PRODUCT_EPIC["product_id"]
    with pytest.raises(Unsupported, match="Product attribution"):
        provider._binding(resource)
    graph.pop("product_id")
    plan["product_id"] = PRODUCT_EPIC["product_id"]
    with pytest.raises(Unsupported, match="Product attribution"):
        provider._binding(resource)
