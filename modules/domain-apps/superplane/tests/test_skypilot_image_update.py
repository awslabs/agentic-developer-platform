import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "skypilot_image_update",
    Path(__file__).parents[1] / "infra/scripts/update_skypilot_image.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
IMAGE = "example.invalid/skypilot@sha256:" + "a" * 64


def runner(fail_rollout=False, conflict=False):
    calls = []
    current = {
        "metadata": {"resourceVersion": "12", "generation": 3},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "authenticated-transport", "image": "unchanged"},
                        {"name": "skypilot-api", "image": "previous"},
                    ]
                }
            }
        },
    }

    def run(args, **kwargs):
        calls.append(args)
        if (conflict and "patch" in args) or (
            fail_rollout and "rollout" in args and len(calls) == 3
        ):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, json.dumps(current), "")

    return calls, run


def test_dry_run_is_server_only_and_preserves_transport():
    calls, run = runner()
    result = module.update("skypilot", IMAGE, dry_run=True, run=run)
    assert result["rollout_verified"] is False
    assert len(calls) == 2 and "--dry-run=server" in calls[1]
    patch = json.loads(calls[1][calls[1].index("-p") + 1])
    changes = [p for p in patch if p["op"] != "test"]
    assert changes == [
        {
            "op": "replace",
            "path": "/spec/template/spec/containers/1/image",
            "value": IMAGE,
        }
    ]


def test_concurrent_deployment_conflict_stops_without_rollout_or_rollback():
    calls, run = runner(conflict=True)
    with pytest.raises(subprocess.CalledProcessError):
        module.update("skypilot", IMAGE, run=run)
    assert len(calls) == 2


def test_failed_rollout_restores_previous_image_with_generation_guard():
    calls, run = runner(fail_rollout=True)
    with pytest.raises(subprocess.CalledProcessError):
        module.update("skypilot", IMAGE, run=run)
    rollback = json.loads(calls[3][calls[3].index("-p") + 1])
    assert rollback[0] == {"op": "test", "path": "/metadata/generation", "value": 3}
    assert rollback[1]["value"] == IMAGE
    assert rollback[2]["value"] == "previous"
    assert "rollout" in calls[4]


@pytest.mark.parametrize(
    "image", ["example.invalid/skypilot:latest", "x@sha256:abc", IMAGE + "\n"]
)
def test_unpinned_or_malformed_image_is_rejected_without_cluster_call(image):
    calls, run = runner()
    with pytest.raises(ValueError):
        module.update("skypilot", image, run=run)
    assert not calls


def test_transport_is_updated_in_same_guarded_patch_and_rollback():
    calls, run = runner(fail_rollout=True)
    transport = "example.invalid/api@sha256:" + "b" * 64
    with pytest.raises(subprocess.CalledProcessError):
        module.update("skypilot", IMAGE, run=run, transport_image=transport)
    patch = json.loads(calls[1][calls[1].index("-p") + 1])
    rollback = json.loads(calls[3][calls[3].index("-p") + 1])
    assert patch[-1] == {
        "op": "replace",
        "path": "/spec/template/spec/containers/0/image",
        "value": transport,
    }
    assert rollback[-2]["value"] == transport
    assert rollback[-1]["value"] == "unchanged"


def test_invalid_transport_image_rejected_before_cluster_calls():
    calls, run = runner()
    with pytest.raises(ValueError):
        module.update("skypilot", IMAGE, run=run, transport_image="api:latest")
    assert not calls
