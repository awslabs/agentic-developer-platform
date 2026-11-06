"""A compatible rollback fences writers without deleting Product receipts."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("product_recovery", ROOT / "modules/gateway/scripts/product-profile-recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
BASELINE_SOURCE = "b" * 40
ROLLBACK_SOURCE = "a" * 40
IMAGE = "example.invalid/adp-gateway@sha256:" + "c" * 64


@pytest.fixture
def gateway():
    flags = [
        {"name": "BG_PRODUCT_PROFILE_ENABLED", "value": "true"},
        {"name": "BG_PRODUCT_ONBOARDING_ENABLED", "value": "false"},
    ]
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "bedrockgateway", "namespace": "dev", "uid": "same-uid", "resourceVersion": "1", "generation": 1},
        "spec": {
            "replicas": 2,
            "template": {
                "spec": {
                    "containers": [
                        {"name": "bedrockgateway", "image": IMAGE, "env": flags},
                    ]
                }
            },
        },
        "status": {"readyReplicas": 2, "observedGeneration": 1},
    }
    data = {"product": "saved", "history": ["created", "archived"], "idempotency_receipt": "same-key", "tombstone": "retained"}
    state = {"current": deployment, "candidate": copy.deepcopy(deployment), "data": data, "calls": []}
    for item in state["candidate"]["spec"]["template"]["spec"]["containers"][0]["env"]:
        item["value"] = "false"

    def run(args, payload=None):
        state["calls"].append(args)
        if "create" in args:
            return json.dumps(state["candidate"])
        if "get" in args:
            return json.dumps(state["current"])
        if "replace" in args:
            candidate = json.loads(payload)
            assert candidate["metadata"]["resourceVersion"] == state["current"]["metadata"]["resourceVersion"]
            candidate["metadata"]["generation"] += 1
            candidate["metadata"]["resourceVersion"] = "2"
            candidate["status"] = {"readyReplicas": 2, "observedGeneration": candidate["metadata"]["generation"]}
            state["current"] = candidate
            return ""
        if "rollout" in args:
            return "ready"
        raise AssertionError(args)

    state["run"] = run
    return state


def prepare(gateway, **overrides):
    kwargs = dict(
        namespace="dev",
        baseline_image=IMAGE,
        baseline_source=BASELINE_SOURCE,
        rollback_source=ROLLBACK_SOURCE,
        approved_source=ROLLBACK_SOURCE,
        candidate_manifest="candidate.yaml",
        run=gateway["run"],
    )
    kwargs.update(overrides)
    return recovery.prepare(**kwargs)


def test_quiesce_current_image_before_candidate_and_keep_data(gateway):
    original = copy.deepcopy(gateway["data"])
    receipt = prepare(gateway)
    calls = gateway["calls"]
    assert next(index for index, args in enumerate(calls) if "replace" in args) < next(index for index, args in enumerate(calls) if "rollout" in args)
    assert receipt == {
        "baseline_source_revision": BASELINE_SOURCE,
        "baseline_image_digest": IMAGE.rsplit("@", 1)[1],
        "admissions_disabled_before_rollback": True,
        "migration_mode": "retained",
        "data_readback": "pending",
    }
    assert gateway["data"] == original
    gateway["calls"].clear()
    assert prepare(gateway) == receipt
    assert not any("replace" in args for args in gateway["calls"])


@pytest.mark.parametrize(
    "override",
    [
        {"approved_source": "f" * 40},
        {"rollback_source": "short"},
        {"baseline_image": "unpinned-image"},
        {"namespace": "other/dev"},
    ],
)
def test_unapproved_or_unpinned_rollback_never_writes(gateway, override):
    with pytest.raises(ValueError):
        prepare(gateway, **override)
    assert not any("replace" in args for args in gateway["calls"])


@pytest.mark.parametrize("failure", ["baseline_image", "candidate_admission", "candidate_duplicate", "baseline_flag"])
def test_stale_or_unprotected_gateway_never_quiesces(gateway, failure):
    if failure == "baseline_image":
        gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] = "stale"
    if failure == "candidate_admission":
        gateway["candidate"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "true"
    if failure == "baseline_flag":
        gateway["current"]["spec"]["template"]["spec"]["containers"][0]["env"].pop()
    if failure == "candidate_duplicate":
        gateway["candidate"]["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "BG_PRODUCT_PROFILE_ENABLED", "value": "false"})
    with pytest.raises(ValueError):
        prepare(gateway)
    assert not any("replace" in args for args in gateway["calls"])


def test_existing_gateway_workflow_uses_recovery_before_candidate_without_schema_downgrade():
    document = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
    job = document["jobs"]["deploy-backend"]
    dispatch = document.get("on", document.get(True))["workflow_dispatch"]["inputs"]
    assert dispatch["product_rollback"]["default"] is False
    assert "inputs.product_rollback" in next(step for step in job["steps"] if step.get("id") == "deployed")["if"]
    release = next(step for step in job["steps"] if step.get("id") == "release")
    script = release["run"]
    assert "product-recovery.py" in script
    assert 'test "$ENVIRONMENT" = "dev"' in script
    assert script.index("product-recovery.py") < script.index("apply-deployment.py")
    assert "else\n  python3 modules/gateway/scripts/migrate-before-rollout.py" in script
    assert "ADP_PRODUCT_COMPATIBLE_ROLLBACK_SOURCE" in release["env"]["ADP_APPROVED_ROLLBACK_SOURCE"]
    assert "product-recovery.json" in script


CANDIDATE_IMAGE = "example.invalid/adp-gateway@sha256:" + "d" * 64


def test_protected_candidate_quiesce_is_bounded_and_retryable(gateway):
    original = copy.deepcopy(gateway["data"])
    receipt = recovery.switch_compatible("dev", IMAGE, IMAGE, restore=False, quiesce=True, run=gateway["run"])
    assert receipt["admissions_disabled"] is True
    assert receipt["from_image_digest"] == receipt["to_image_digest"]
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] == "false"
    assert gateway["data"] == original
    gateway["calls"].clear()
    recovery.switch_compatible("dev", IMAGE, IMAGE, restore=False, quiesce=True, run=gateway["run"])
    assert not any("replace" in call for call in gateway["calls"])


@pytest.mark.parametrize("tamper", ["wrong_image", "onboarding", "duplicate", "unready", "invalid_restore"])
def test_protected_quiesce_refuses_changed_candidate(gateway, tamper):
    deployment = gateway["current"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    if tamper == "wrong_image":
        container["image"] = CANDIDATE_IMAGE
    elif tamper == "onboarding":
        container["env"][1]["value"] = "true"
    elif tamper == "duplicate":
        container["env"].append(copy.deepcopy(container["env"][0]))
    elif tamper == "unready":
        deployment["status"]["readyReplicas"] = 0
    with pytest.raises(ValueError):
        recovery.switch_compatible("dev", IMAGE, IMAGE, restore=tamper == "invalid_restore", quiesce=True, run=gateway["run"])
    assert not any("replace" in call for call in gateway["calls"])


def test_protected_switch_and_idempotent_candidate_restoration(gateway):
    current = gateway["current"]["spec"]["template"]["spec"]["containers"][0]
    current["env"][0]["value"] = "false"
    receipt = recovery.switch_compatible("dev", IMAGE, CANDIDATE_IMAGE, restore=False, run=gateway["run"])
    assert receipt["admissions_enabled"] is False
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] == CANDIDATE_IMAGE
    recovery.switch_compatible("dev", CANDIDATE_IMAGE, IMAGE, restore=True, run=gateway["run"])
    gateway["calls"].clear()
    recovery.switch_compatible("dev", CANDIDATE_IMAGE, IMAGE, restore=True, run=gateway["run"])
    assert not any("replace" in call for call in gateway["calls"])
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] == "true"


@pytest.mark.parametrize("tamper", ["wrong_image", "admissions", "onboarding", "duplicate", "unready"])
def test_protected_switch_refuses_stale_or_unready_baseline(gateway, tamper):
    deployment = gateway["current"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    container["env"][0]["value"] = "false"
    if tamper == "wrong_image":
        container["image"] = "example.invalid/adp-gateway@sha256:" + "e" * 64
    elif tamper == "admissions":
        container["env"][0]["value"] = "true"
    elif tamper == "onboarding":
        container["env"][1]["value"] = "true"
    elif tamper == "duplicate":
        container["env"].append(copy.deepcopy(container["env"][0]))
    else:
        deployment["status"]["readyReplicas"] = 1
    with pytest.raises(ValueError):
        recovery.switch_compatible("dev", IMAGE, CANDIDATE_IMAGE, restore=False, run=gateway["run"])
    assert not any("replace" in call for call in gateway["calls"])


def test_protected_switch_restores_from_interrupted_unready_baseline(gateway):
    deployment = gateway["current"]
    deployment["status"]["readyReplicas"] = 0
    deployment["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "false"
    receipt = recovery.switch_compatible("dev", IMAGE, CANDIDATE_IMAGE, restore=True, run=gateway["run"])
    assert receipt["admissions_enabled"] is True
    assert any("replace" in call for call in gateway["calls"])


def test_interrupted_baseline_switch_is_resumed_then_candidate_restored(gateway):
    gateway["current"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "false"
    original_run = gateway["run"]
    attempts = 0

    def interrupted(args, payload=None):
        nonlocal attempts
        if "rollout" in args and attempts == 0:
            attempts += 1
            gateway["current"]["status"]["readyReplicas"] = 0
            raise TimeoutError("interrupted baseline readiness")
        if "rollout" in args:
            gateway["current"]["status"]["readyReplicas"] = 2
        return original_run(args, payload)

    with pytest.raises(TimeoutError):
        recovery.switch_compatible("dev", IMAGE, CANDIDATE_IMAGE, restore=False, run=interrupted)
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] == CANDIDATE_IMAGE
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] == "false"
    recovery.switch_compatible("dev", IMAGE, CANDIDATE_IMAGE, restore=False, run=interrupted)
    assert sum("replace" in call for call in gateway["calls"]) == 1
    recovery.switch_compatible("dev", CANDIDATE_IMAGE, IMAGE, restore=True, run=interrupted)
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] == "true"
