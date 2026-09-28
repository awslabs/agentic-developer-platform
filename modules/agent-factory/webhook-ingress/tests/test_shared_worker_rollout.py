import copy
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plan-shared-worker-rollout.py"
SPEC = importlib.util.spec_from_file_location("shared_worker_rollout", SCRIPT)
ROLLOUT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROLLOUT)
ACCOUNT = "123456789012"
IMAGE = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:" + "a" * 64


def snapshot():
    return {"kind": "ScaledJob", "metadata": {"name": "agent-scaledjob", "namespace": "adp-agents", "resourceVersion": "42", "annotations": {"autoscaling.keda.sh/paused": "true"}}, "spec": {"jobTargetRef": {"template": {"spec": {"serviceAccountName": "agent-scaledjob-sa", "containers": [{"name": "sidecar", "image": "unchanged"}, {"name": "agent-worker", "image": "old", "env": [{"name": "AGENT_RUN_LOGS_BUCKET", "value": "existing-bucket"}]}]}}}}}


def prepare(source, **changes):
    return ROLLOUT.prepare(source, **({"account_id": ACCOUNT, "image": IMAGE, "control_endpoint": "https://api.example/dev/internal/v1/agent", "gitlab_url": "https://gitlab.example/"} | changes))


def test_only_worker_image_and_trusted_endpoints_are_mutated():
    source = snapshot()
    original = copy.deepcopy(source)
    patch, overlay = prepare(source)
    assert source == original
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "42"}
    assert patch[2]["value"] == "old"
    changes = [x for x in patch if x["op"] != "test"]
    base = "/spec/jobTargetRef/template/spec/containers/1"
    assert [x["path"] for x in changes] == [base + "/image", base + "/env/-", base + "/env/-"]
    assert {x["value"]["name"] for x in changes[1:]} == {"ADP_AGENT_CONTROL_ENDPOINT", "GITLAB_URL"}
    assert overlay == {"agent_image": IMAGE, "gitlab_webhook_enabled": True}


def test_existing_endpoint_updates_in_place_without_duplicate_env():
    source = snapshot()
    source["spec"]["jobTargetRef"]["template"]["spec"]["containers"][1]["env"].append({"name": "GITLAB_URL", "value": "https://old.example"})
    patch, _ = prepare(source)
    assert patch[-1]["op"] == "replace"
    assert patch[-1]["path"].endswith("/env/1")


@pytest.mark.parametrize("changes", [{"image": IMAGE.replace("@sha256:" + "a" * 64, ":latest")}, {"account_id": "999999999999"}, {"control_endpoint": "http://api.example/internal/v1/agent"}, {"gitlab_url": "https://user:password@gitlab.example"}])
def test_refuses_unreviewable_image_or_endpoint(changes):
    with pytest.raises(ValueError):
        prepare(snapshot(), **changes)


def test_refuses_protected_worker():
    source = snapshot()
    source["spec"]["jobTargetRef"]["template"]["spec"]["serviceAccountName"] = "agent-authority-worker-sa"
    with pytest.raises(ValueError):
        prepare(source)
