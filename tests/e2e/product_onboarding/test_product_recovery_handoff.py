"""A protected switch needs the original signed release and held Product, not a new fixture."""

from copy import deepcopy
from inspect import signature

import pytest

from tests.e2e.cli_uplift import ports
from tests.e2e.cli_uplift.statestore import StateStoreError
from tests.e2e.product_onboarding import product_automatic_identity, product_execution, product_recovery_handoff
from tests.e2e.product_onboarding.product_rollback_artifacts import BASELINE_SOURCE

pytest_plugins = ("tests.e2e.product_onboarding.test_product_hold_entrypoint",)


@pytest.fixture
def handoff(hold):
    cfg, inventory, native, document, manifest, store = hold
    native["runtime_receipt"].update(
        cli_manifest_sha256="f" * 64,
        pod_uids=["owned-pod"],
        product_profile_enabled=True,
        product_onboarding_enabled=False,
        observed_at="first-observation",
    )
    product_execution.evaluate(cfg, inventory, "product-42-1", object(), store=store)
    baseline = {
        **native["release_artifact"],
        "run_id": 39537019013,
        "source_revision": BASELINE_SOURCE,
        "image_digest": "sha256:" + "e" * 64,
    }
    current = deepcopy(native)
    current["runtime_receipt"]["observed_at"] = "second-observation"

    class Aws:
        def call(self, service, operation):
            assert (service, operation) == ("sts", "get_caller_identity")
            return {
                "Account": str(cfg["platform_account"]),
                "Arn": f"arn:aws:sts::{cfg['platform_account']}:assumed-role/adp-cli-uplift-eval-orchestrator/fixture",
            }

    return cfg, inventory, native, current, baseline, document, manifest, store, Aws()


def prepare(
    handoff,
    *,
    current=None,
    cfg=None,
    aws=None,
    reference="s3://private/baseline?versionId=one",
    accepted_reader=None,
):
    (
        original_cfg,
        inventory,
        native,
        current_native,
        baseline,
        _,
        _,
        store,
        original_aws,
    ) = handoff
    selected = current or current_native
    return product_recovery_handoff.prepare(
        cfg or original_cfg,
        "product-42-1",
        aws or original_aws,
        store,
        inventory_reader=lambda *_: inventory,
        native_reader=lambda *_: selected,
        qualify=lambda *_: (native["release_artifact"], baseline, reference),
        accepted_reader=accepted_reader or (lambda *_: None),
    )


def test_signed_held_release_survives_fresh_runtime_observation(handoff):
    result = prepare(handoff)
    assert result["run_id"] == "product-42-1"
    assert (
        result["source_revision"] == handoff[2]["release_artifact"]["source_revision"]
    )
    assert result["baseline_source_revision"] == BASELINE_SOURCE
    assert result["candidate_image_digest"] != result["baseline_image_digest"]
    assert result["account_id"] == handoff[2]["release_artifact"]["account_id"]
    report = next(value["report"] for key, value in handoff[7].values.items() if key.endswith("/recovery.json"))
    assert [entry["stage"] for entry in report["attempts"]] == ["created", "quiesce_started"]
    assert report["owned"] == handoff[5]["product_evidence"]["held"]
    assert prepare(handoff) == result


def test_handoff_requires_original_completed_automatic_identity(handoff):
    assert signature(product_recovery_handoff.prepare).parameters["accepted_reader"].default is product_automatic_identity.require_qualified
    cfg, _, _, current, _, _, _, store, _ = handoff

    def reject(selected_store, run_id, source_revision, observed):
        assert selected_store is store
        assert run_id == "product-42-1"
        assert source_revision == cfg["expected_revision"]
        assert observed is current
        raise ports.PortError("original automatic checks are incomplete")

    with pytest.raises(ports.PortError, match="original automatic checks"):
        prepare(handoff, accepted_reader=reject)


def test_retried_handoff_refuses_swapped_original_fixture(handoff):
    prepare(handoff)
    handoff[5]["product_evidence"]["held"]["product_id"] = "foreign"
    with pytest.raises(StateStoreError, match="original|ownership"):
        prepare(handoff)


@pytest.mark.parametrize(
    "change",
    [
        "target",
        "source",
        "revision",
        "flags",
        "pods",
        "reference",
        "missing_hold",
        "owned_cleanup",
    ],
)
def test_stale_or_foreign_evidence_cannot_start_protected_switch(handoff, change):
    cfg, _, _, current, _, document, manifest, store, _ = handoff
    selected = deepcopy(current)
    reference = "s3://private/baseline?versionId=one"
    if change == "target":
        selected["target"]["resource_id"] = "other-cluster/adp-gateway"
    elif change == "source":
        selected["release_artifact"]["source_revision"] = "0" * 40
    elif change == "revision":
        selected["runtime_receipt"]["migration_head"] = "other-head"
    elif change == "flags":
        selected["runtime_receipt"]["product_profile_enabled"] = False
    elif change == "pods":
        selected["runtime_receipt"]["pod_uids"] = ["foreign-pod"]
    elif change == "reference":
        reference = "s3://private/baseline?versionId=changed"
    elif change == "missing_hold":
        store.values.pop(
            next(key for key in store.values if key.endswith("/hold-evaluation.json"))
        )
    else:
        document["cleanup_ok"] = True
    with pytest.raises((ValueError, ports.PortError, StateStoreError)):
        prepare(handoff, current=selected, reference=reference)


def test_wrong_role_or_release_selection_refuses_before_reading_state(handoff):
    cfg, _, _, _, _, _, _, _, _ = handoff

    class ForeignAws:
        def call(self, *_):
            return {"Account": str(cfg["platform_account"]), "Arn": "foreign-role"}

    with pytest.raises(ports.PortError, match="protected evaluator"):
        prepare(handoff, aws=ForeignAws())
    with pytest.raises(ports.PortError, match="original evaluation"):
        prepare(handoff, cfg={**cfg, "product_cases": "api_denial_oracle"})


def test_protected_entrypoint_emits_only_bound_nonsecret_switch_inputs(
    handoff, monkeypatch, tmp_path
):
    cfg, _, _, _, _, _, _, store, aws = handoff
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(product_recovery_handoff.live, "resolved_config", lambda _: cfg)
    monkeypatch.setattr(
        product_recovery_handoff.live, "workflow_run_id", lambda: "product-42-1"
    )
    monkeypatch.setattr(
        product_recovery_handoff.ports, "default_ports", lambda _: {"aws": aws}
    )
    monkeypatch.setattr(
        product_recovery_handoff.statestore, "default_store", lambda *_: store
    )
    monkeypatch.setattr(
        product_recovery_handoff,
        "prepare",
        lambda *_: {
            "run_id": "product-42-1",
            "candidate_image_digest": "sha256:" + "a" * 64,
        },
    )
    product_recovery_handoff.main(["--expected-revision", cfg["expected_revision"]])
    assert output.read_text() == (
        "run_id=product-42-1\ncandidate_image_digest=sha256:" + "a" * 64 + "\n"
    )
    monkeypatch.delenv("GITHUB_OUTPUT")
    with pytest.raises(ports.PortError, match="protected job outputs"):
        product_recovery_handoff.main(["--expected-revision", cfg["expected_revision"]])
