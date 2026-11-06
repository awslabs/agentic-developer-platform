"""A held Product is an outstanding owned fixture, never a passed evaluation."""

from copy import deepcopy

import pytest

from tests.e2e.cli_uplift import config, ports
from tests.e2e.product_onboarding import product_execution


@pytest.fixture
def hold(monkeypatch):
    cfg = config.load(config.EXAMPLE_PATH)
    cfg.update(
        expected_revision="a" * 40,
        product_cases="api_product_lifecycle",
        hold_for_rollback=True,
    )
    inventory = {
        "source_revision": cfg["expected_revision"],
        "digest": "sha256:" + "b" * 64,
        "pods": [{"uid": "owned-pod"}],
    }
    native = {
        "source_revision": cfg["expected_revision"],
        "database_heads": ["native-head"],
        "image_heads": ["native-head"],
        "tick_verified": True,
        "tick_digest": inventory["digest"],
        "pod_uids": ["owned-pod"],
        "observation_ref": "runtime:sha256:" + "c" * 64,
        "release_ref": "s3://private/release?versionId=one",
        "target": {
            "account_id": str(cfg["platform_account"]),
            "region": cfg["region"],
            "resource_id": "example-cluster/adp-gateway",
            "repository_id": 42,
        },
        "runtime_receipt": {
            "actual_revision": cfg["expected_revision"],
            "image_digest": inventory["digest"],
            "migration_head": "native-head",
        },
        "release_artifact": {
            "run_id": 42,
            "run_attempt": 1,
            "source_revision": cfg["expected_revision"],
            "workflow_revision": "d" * 40,
            "image_digest": inventory["digest"],
            "migration_head": "native-head",
            "account_id": str(cfg["platform_account"]),
            "region": cfg["region"],
            "resource_id": "example-cluster/adp-gateway",
            "repository_id": 42,
            "component": "gateway-backend",
            "workflow_path": ".github/workflows/gateway-deploy.yml",
            "product_profile_enabled": True,
            "product_onboarding_enabled": False,
            "cli_manifest_sha256": "f" * 64,
        },
    }
    from tests.e2e.product_onboarding import product_rollback_artifacts

    baseline = {
        **native["release_artifact"],
        "run_id": product_rollback_artifacts.BASELINE_RUN,
        "source_revision": product_rollback_artifacts.BASELINE_SOURCE,
        "image_digest": "sha256:" + "e" * 64,
    }
    monkeypatch.setattr(
        product_rollback_artifacts,
        "qualify_baseline",
        lambda *_: (
            native["release_artifact"],
            baseline,
            "s3://private/baseline?versionId=one",
        ),
    )
    monkeypatch.setattr(product_execution, "native_observation", lambda *_: native)
    monkeypatch.setattr(
        product_execution.product_inventory, "observe", lambda *_: inventory
    )
    monkeypatch.setattr(product_execution, "recovery_available", lambda _: None)
    monkeypatch.setattr(
        product_execution.runner,
        "new_evaluation_id",
        lambda *_: "adp-e2e-20261021-000000-abcdef",
    )
    document = {
        "evaluation_id": "adp-e2e-20261021-000000-abcdef",
        "cleanup_ok": False,
        "product_run_id": "product-42-1",
        "stages": {
            stage: "complete" if stage != "cleanup" else "failed"
            for stage in product_execution.runner.STAGES
        },
        "product_evidence": {
            "run_id": "product-42-1",
            "source_revision": cfg["expected_revision"],
            "held": {
                "run_id": "product-42-1",
                "product_id": "owned-product",
                "tenant_id": "owned-tenant",
                "actor_id": "owned-actor",
                "idempotency_key": "product-create-original",
                "project_id": "owned-project",
                "source_revision": cfg["expected_revision"],
                "history_ref": "sha256:" + "a" * 64,
                "idempotency_receipt": "sha256:" + "b" * 64,
            },
        },
    }
    manifest = {
        "prefix": document["evaluation_id"],
        "resources": [
            {
                "kind": "product_fixture",
                "id": "product-42-1",
                "status": "pending",
                "prefix": document["evaluation_id"],
                "account": str(cfg["platform_account"]),
                "region": cfg["region"],
                "detail": {
                    "tenant_id": "owned-tenant",
                    "actor_id": "owned-actor",
                    "idempotency_key": "product-create-original",
                    "source_revision": cfg["expected_revision"],
                },
            }
        ],
    }

    class Store:
        def __init__(self):
            self.values = {}

        def _get(self, key):
            return deepcopy(self.values.get(key))

        def _put(self, key, value):
            self.values[key] = deepcopy(value)

        def load(self, evaluation_id):
            assert evaluation_id == document["evaluation_id"]
            return deepcopy(document), deepcopy(manifest)

    store = Store()
    monkeypatch.setattr(product_execution.runner, "main", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        product_execution.runner,
        "State",
        lambda *_: type("State", (), {"read": lambda self: deepcopy(document)})(),
    )
    return cfg, inventory, native, document, manifest, store


def test_held_product_is_durably_owned_and_kept_separate_from_accepted_run(hold):
    cfg, inventory, _, _, _, store = hold
    result = product_execution.evaluate(
        cfg, inventory, "product-42-1", object(), store=store
    )
    assert result["product"]["held"]["product_id"] == "owned-product"
    assert any("hold-evaluation.json" in key for key in store.values)
    assert any("/recovery.json" in key for key in store.values)
    assert not any(key.endswith("/evaluation.json") for key in store.values)


@pytest.mark.parametrize(
    "failure",
    [
        "result_pass",
        "cleanup_pass",
        "no_ownership",
        "stale_source",
        "journey_failed",
        "wrong_target",
    ],
)
def test_release_hold_refuses_cleaned_or_unowned_result(hold, monkeypatch, failure):
    cfg, inventory, _, document, manifest, store = hold
    if failure == "result_pass":
        monkeypatch.setattr(
            product_execution.runner, "main", lambda *_args, **_kwargs: 0
        )
    elif failure == "cleanup_pass":
        document["cleanup_ok"] = True
    elif failure == "no_ownership":
        manifest["resources"].clear()
    elif failure == "stale_source":
        document["product_evidence"]["source_revision"] = "0" * 40
    elif failure == "journey_failed":
        document["stages"]["journeys"] = "failed"
    else:
        manifest["resources"][0]["account"] = "000000000001"
    with pytest.raises(ports.PortError, match="not durably owned"):
        product_execution.evaluate(
            cfg, inventory, "product-42-1", object(), store=store
        )


def test_hold_cli_cannot_claim_acceptance_or_skip_cleanup_boundary(
    hold, monkeypatch, tmp_path
):
    import json

    from tests.e2e.product_onboarding import live

    cfg, inventory, native, document, _, _ = hold
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_SHA", cfg["expected_revision"])
    boundary = tmp_path / "boundary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(boundary))
    monkeypatch.setattr(live, "resolved_config", lambda _: cfg)
    monkeypatch.setattr(
        product_execution,
        "evaluate",
        lambda *_: {
            "evaluation_id": document["evaluation_id"],
            "native": native,
            "product": document["product_evidence"],
        },
    )

    class Aws:
        def call(self, *_args):
            return {
                "Account": str(cfg["platform_account"]),
                "Arn": f"arn:aws:sts::{cfg['platform_account']}:assumed-role/adp-cli-uplift-eval-orchestrator/test",
            }

    monkeypatch.setattr(ports, "default_ports", lambda _: {"aws": Aws()})
    report_file = tmp_path / "hold.json"
    live.main(
        [
            "--cases",
            "api_product_lifecycle",
            "--expected-revision",
            cfg["expected_revision"],
            "--hold-for-rollback",
            "--dispatch-report",
            str(report_file),
        ]
    )
    report = json.loads(report_file.read_text())
    assert report["status"] == "held" and report["cleanup_verified"] is False
    assert report["evaluation_id"] == document["evaluation_id"]
    assert boundary.read_text() == "cleanup_ok=false\n"
    with pytest.raises(ValueError, match="non-acceptance"):
        live.main(
            [
                "--cases",
                "api_product_lifecycle,api_denial_oracle",
                "--expected-revision",
                cfg["expected_revision"],
                "--hold-for-rollback",
            ]
        )


def test_incompatible_installed_baseline_refuses_before_creating_a_held_product(
    hold, monkeypatch
):
    from tests.e2e.product_onboarding import product_rollback_artifacts

    cfg, inventory, _, _, _, store = hold

    def reject(*_args):
        raise ValueError("Installed baseline schema differs")

    monkeypatch.setattr(product_rollback_artifacts, "qualify_baseline", reject)
    monkeypatch.setattr(
        product_execution.runner,
        "new_evaluation_id",
        lambda *_: pytest.fail("baseline check must precede evaluator allocation"),
    )
    with pytest.raises(ValueError, match="baseline schema"):
        product_execution.evaluate(
            cfg, inventory, "product-42-1", object(), store=store
        )
    assert store.values == {}


def qualification_for_hold(store):
    from tests.e2e.product_onboarding.product_qualification_state import (
        RetainedQualification,
    )

    report = next(
        value["report"]
        for key, value in store.values.items()
        if key.endswith("/recovery.json")
    )
    return RetainedQualification(store, report["candidate"], report["baseline"])


def test_held_fixture_reuses_exact_private_owned_product(hold):
    cfg, inventory, native, document, _, store = hold
    product_execution.evaluate(cfg, inventory, "product-42-1", object(), store=store)
    state = qualification_for_hold(store)
    assert state.held_fixture() == document["product_evidence"]["held"]
    assert state.held_fixture()["history_ref"] == "sha256:" + "a" * 64
    assert state.held_fixture()["idempotency_receipt"] == "sha256:" + "b" * 64
    assert state.load()["attempts"] == []
    assert (
        native["release_artifact"]["image_digest"]
        == state.load()["candidate"]["image_digest"]
    )


@pytest.mark.parametrize(
    "tamper", ["receipt", "manifest", "cleanup", "target", "release", "duplicate"]
)
def test_held_fixture_refuses_foreign_cleanup_or_changed_receipt(hold, tamper):
    from tests.e2e.cli_uplift.statestore import StateStoreError

    cfg, inventory, _, document, manifest, store = hold
    product_execution.evaluate(cfg, inventory, "product-42-1", object(), store=store)
    state = qualification_for_hold(store)
    if tamper == "receipt":
        document["product_evidence"]["held"]["idempotency_receipt"] = "changed"
    elif tamper == "manifest":
        manifest["resources"][0]["detail"]["idempotency_key"] = "wrong"
    elif tamper == "cleanup":
        document["cleanup_ok"] = True
    elif tamper == "target":
        manifest["resources"][0]["account"] = "000000000001"
    elif tamper == "release":
        key = next(key for key in store.values if key.endswith("/hold-release.json"))
        store.values[key]["release_ref"] = "s3://private/other?versionId=two"
    else:
        manifest["resources"].append(deepcopy(manifest["resources"][0]))
    with pytest.raises(StateStoreError):
        state.held_fixture()
    assert state.load()["attempts"] == []
