"""Offline failures at real Product-bound session, provider and report boundaries."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration import run as runner
from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.report import write_report
from tests.e2e.orchestration.scenarios import (
    audit,
    delivery,
    manifest as manifest_module,
    runtime,
)
from tests.e2e.orchestration.scenarios.delivery import FlowProvider, LiveSession
from tests.e2e.orchestration.scenarios.delivery_resources import (
    DeliveryResourceProvider,
)
from tests.e2e.orchestration.scenarios.http import Unsupported
from tests.e2e.orchestration.scenarios.providers import IssueProvider

from tests.e2e.orchestration.config import load_config
from tests.e2e.orchestration.test_product_epic_config import PRODUCT_EPIC
from tests.e2e.orchestration.test_product_epic_wiring import ProviderTransport

QUALIFICATION_ID = "q-0123456789abcdef"


@pytest.fixture
def wired(write_config):
    config = load_config(
        write_config({"product_epic": PRODUCT_EPIC, "bounds": {"max_resources": 25}})
    )
    return config, ProviderTransport(config)


def make_session(wired, monkeypatch):
    config, transport = wired
    manifest = SimpleNamespace(
        runtime={"engine": "engine", "worker": "worker"},
        execution_policy={"limits": {"max_attempts_per_node": 1}},
        evaluation={},
        planned_gates=["release", "refusal"],
        required_checks=["fixture-unittests"],
        deployment_workflows=[".github/workflows/fixture.yml"],
        model_dump=lambda **_: {"offline": True},
    )
    monkeypatch.setattr(delivery, "load_manifest", lambda _: (manifest, "a" * 64))
    monkeypatch.setattr(
        manifest_module, "load_manifest", lambda _: (manifest, "a" * 64)
    )
    monkeypatch.setattr(
        runtime,
        "read_runtime",
        lambda client, name: {"actual_revision": config.versions[name]},
    )
    monkeypatch.setattr(LiveSession, "versions", lambda self: config.versions)
    monkeypatch.setattr(
        audit, "collect", Mock(side_effect=Unsupported("offline audit unavailable"))
    )
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    provider = IssueProvider(transport)
    session = LiveSession(config, manifest, inventory, {provider.kind: provider})
    session.client = transport
    session.live = False
    return config, transport, inventory, session


@pytest.mark.parametrize("stage", ["publication", "native_link", "dispatch"])
def test_real_session_lost_response_recovers_without_duplicate(
    wired, monkeypatch, stage
):
    config, transport, inventory, session = make_session(wired, monkeypatch)
    original_request, original_get = transport.request, transport.get
    dropped = False

    def get(path, **kwargs):
        if path.startswith("/orchestration/flows?q="):
            flows = (
                [{"id": "flow-1", "slug": QUALIFICATION_ID}]
                if transport.accepted
                else []
            )
            return {"flows": flows, "total": len(flows)}
        return original_get(path, **kwargs)

    def request(method, path, **kwargs):
        nonlocal dropped
        expected = {
            "publication": path.endswith("/issues"),
            "native_link": path.endswith("/sub_issues"),
            "dispatch": path == "/orchestration/flows",
        }[stage]
        if method == "POST" and expected and not dropped:
            dropped = True
            original_request(method, path, **kwargs)
            raise OSError("lost provider acknowledgement")
        return original_request(method, path, **kwargs)

    transport.get, transport.request = get, request
    report = delivery.execute(
        config, inventory, session.providers, session_factory=lambda *_: session
    )
    assert dropped
    assert report.live is False
    assert all(
        result.status != "PASS"
        for result in report.results
        if result.id == "A6-2.development"
    )
    assert report.spend_usd is None
    assert (inventory.path.parent / "evidence/0001-accepted-inputs.json").exists()
    retained = Inventory.load(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    assert len(retained.fixtures) >= 1
    assert len(transport.issues) == (1 if stage != "dispatch" else 2)

    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda _: SimpleNamespace(verified=True, to_json=lambda: {}),
    )
    monkeypatch.setattr(
        runner,
        "load_providers",
        lambda _: {
            IssueProvider.kind: IssueProvider(transport),
            FlowProvider.kind: FlowProvider(transport),
            **{
                kind: DeliveryResourceProvider(transport, kind)
                for kind in ("qualification-pr", "qualification-branch")
            },
        },
    )
    recovered = runner.resume_qualification(config, QUALIFICATION_ID)
    assert recovered.status == runner.STATUS_FAILED, recovered.report
    assert {item["fixture_id"] for item in recovered.report["reconcile_failed"]} == {
        f"story-{index}-{resource}" for index in (1, 2) for resource in ("pr", "branch")
    }
    assert recovered.report["native_parent_verified"] == [
        f"story-{index}" for index in range(1, len(transport.issues) + 1)
    ]
    assert len(transport.issues) == (1 if stage != "dispatch" else 2)
    assert sum(
        method == "POST" and path.endswith("/issues")
        for method, path in transport.calls
    ) == len(transport.issues)
    assert sum(
        method == "POST" and path.endswith("/sub_issues")
        for method, path in transport.calls
    ) == len(transport.issues)
    assert sum(
        method == "POST" and path == "/orchestration/flows"
        for method, path in transport.calls
    ) == (1 if stage == "dispatch" else 0)


@pytest.mark.parametrize("stage", ["review", "evidence"])
def test_session_failure_after_dispatch_keeps_report_and_owned_resources(
    wired, monkeypatch, stage
):
    config, transport, inventory, session = make_session(wired, monkeypatch)
    if stage == "review":
        session.await_completion = lambda: {
            "plans": [
                {
                    "superseded_at": None,
                    "version": 1,
                    "plan_document": {"execution_policy": {}},
                }
            ],
            "cost": {"status": "unknown", "partial": True},
            "graph": {
                "nodes": [
                    {
                        "node_ref": "first",
                        "bound_pull_request": {
                            "repo": "example/foreign",
                            "pr_number": 10,
                        },
                    }
                ]
            },
        }
    else:
        original_save = delivery.Evidence.save

        def save(evidence, kind, data, source):
            if kind == "plan":
                raise OSError("offline evidence write failure")
            return original_save(evidence, kind, data, source)

        monkeypatch.setattr(delivery.Evidence, "save", save)
    report = delivery.execute(
        config, inventory, session.providers, session_factory=lambda *_: session
    )
    assert report.spend_usd is None
    assert report.live is False
    assert (
        next(
            result for result in report.results if result.id == "A6-2.development"
        ).status
        != "PASS"
    )
    retained = Inventory.load(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    assert len(retained.fixtures) >= 2
    assert all(
        retained.get(f"story-{index}").native_parent_verified for index in (1, 2)
    )
    assert len(transport.issues) == 2
    assert len(transport.parents) == 2
    assert list((inventory.path.parent / "evidence").glob("*.json"))
    if stage == "review":
        assert not any(
            method == "POST" and path.endswith("/pulls")
            for method, path in transport.calls
        )


@pytest.mark.parametrize("fault", ["report_write", "missing_evidence"])
def test_report_and_evidence_faults_preserve_owned_inventory(wired, monkeypatch, fault):
    config, transport, inventory, session = make_session(wired, monkeypatch)
    session.await_completion = lambda: {
        "plans": [
            {
                "superseded_at": None,
                "version": 1,
                "plan_document": {"execution_policy": {}},
            }
        ],
        "cost": {"status": "unknown", "partial": True},
        "graph": {
            "nodes": [
                {"node_ref": "first", "bound_pull_request": {"repo": "example/foreign"}}
            ]
        },
    }
    report = delivery.execute(
        config, inventory, session.providers, session_factory=lambda *_: session
    )
    if fault == "report_write":
        original_write_text = Path.write_text

        def fail_report(path, *args, **kwargs):
            if path.name == "report.json":
                raise OSError("offline report write failed")
            return original_write_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_report)
        with pytest.raises(OSError, match="report write failed"):
            write_report(
                report, config=config, inventory=inventory, manifest_hash="a" * 64
            )
        monkeypatch.setattr(Path, "write_text", original_write_text)
    else:
        evidence = next(
            result.evidence["inventory"]
            for result in report.results
            if result.id == "A6-5.isolation"
        )
        (inventory.path.parent / evidence.path).unlink()
    written = write_report(
        report, config=config, inventory=inventory, manifest_hash="a" * 64
    )
    assert written["overall"] == "INCOMPLETE"
    if fault == "missing_evidence":
        assert any("artifact missing" in blocker for blocker in written["blockers"])
    assert any("A6-2.development" in blocker for blocker in written["blockers"])
    assert (inventory.path.parent / "report.json").exists()
    assert (
        len(
            Inventory.load(
                config.artifact_directory, QUALIFICATION_ID, config.environment
            ).fixtures
        )
        >= 3
    )
    assert len(transport.issues) == 2


@pytest.mark.parametrize("change", ["product", "planning", "story", "superseded"])
def test_lost_dispatch_reconciliation_refuses_changed_authority(
    wired, monkeypatch, change
):
    config, transport, inventory, session = make_session(wired, monkeypatch)
    session.start()
    inventory.mark_reconcile_failed("flow", "dispatch acknowledgement missing")
    if change == "product":
        transport.accepted["product_id"] = "other-product"
    elif change == "planning":
        transport.accepted["origin_ref"]["issue_number"] = 99
    elif change == "story":
        transport.accepted["nodes"][0]["repository_identity"] = "example/foreign"
    original_get = transport.get

    def get(path, **kwargs):
        if path.startswith("/orchestration/flows?q="):
            return {"flows": [{"id": "flow-1", "slug": QUALIFICATION_ID}], "total": 1}
        if change == "superseded" and path == "/orchestration/flows/flow-1/plans":
            return [
                {"superseded_at": "earlier", "plan_document": transport.accepted},
                {
                    "superseded_at": None,
                    "plan_document": {
                        **transport.accepted,
                        "product_id": "other-product",
                    },
                },
            ]
        return original_get(path, **kwargs)

    transport.get = get
    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda _: SimpleNamespace(verified=True, to_json=lambda: {}),
    )
    monkeypatch.setattr(
        runner,
        "load_providers",
        lambda _: {
            IssueProvider.kind: IssueProvider(transport),
            FlowProvider.kind: FlowProvider(transport),
            **{
                kind: DeliveryResourceProvider(transport, kind)
                for kind in ("qualification-pr", "qualification-branch")
            },
        },
    )
    result = runner.resume_qualification(config, QUALIFICATION_ID)
    assert result.status == runner.STATUS_FAILED
    assert "flow" in {item["fixture_id"] for item in result.report["reconcile_failed"]}
    assert inventory.get("flow").observed_resource_id == "flow-1"
    assert len(transport.issues) == 2 and len(transport.parents) == 2
    assert sum(path == "/orchestration/flows" for _, path in transport.calls) == 1
