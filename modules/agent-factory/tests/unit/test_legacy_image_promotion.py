import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "legacy_image_promotion", Path(__file__).parents[2] / "scripts/update-legacy-gateway-image.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
IMAGE = "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-gateway@sha256:" + "a" * 64
PUBLICATION = {"reference": IMAGE, "platform_digest": "sha256:" + "b" * 64}


def runner(fail_job=False, wrong_image=False, concurrent=None):
    calls = []
    current = {
        "metadata": {"resourceVersion": "42", "generation": 3},
        "spec": {
            "jobTargetRef": {
                "template": {
                    "spec": {"containers": [{"name": "agent-worker", "image": "old-image"}]}
                }
            }
        },
    }

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "wait" in args and fail_job:
            raise subprocess.CalledProcessError(1, args)
        if "wait" in args and concurrent:
            current["metadata"]["resourceVersion"] = "43"
            if concurrent == "spec":
                current["metadata"]["generation"] += 1
        if "get" in args and "pods" in args:
            result = {
                "items": [
                    {
                        "status": {
                            "containerStatuses": [
                                {
                                    "name": "check",
                                    "imageID": "unexpected" if wrong_image else IMAGE,
                                    "state": {"terminated": {"exitCode": 0}},
                                }
                            ]
                        }
                    }
                ]
            }
        else:
            result = current
        if "patch" in args and "--dry-run=server" not in args:
            current["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0]["image"] = IMAGE
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

    return calls, run


def test_server_dry_run_has_no_mutations():
    calls, run = runner()
    assert module.promote(PUBLICATION, True, run)["dry_run"]
    assert all(
        "--dry-run=server" in args for args, _ in calls if "create" in args or "patch" in args
    )


@pytest.mark.parametrize("fail_job,wrong_image", [(True, False), (False, True)])
def test_failed_or_wrong_image_job_prevents_promotion(fail_job, wrong_image):
    calls, run = runner(fail_job, wrong_image)
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        module.promote(PUBLICATION, run=run)
    assert not any("patch" in args and "--dry-run=server" not in args for args, _ in calls)


def test_promotion_guards_original_version_and_preserves_other_fields():
    calls, run = runner()
    assert module.promote(PUBLICATION, run=run)["desired_image_verified"]
    patch_args = next(
        args for args, _ in calls if "patch" in args and "--dry-run=server" not in args
    )
    patch = json.loads(patch_args[patch_args.index("-p") + 1])
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "42"}
    assert [p["path"] for p in patch if p["op"] == "replace"] == [
        "/spec/jobTargetRef/template/spec/containers/0/image"
    ]
    job = next(json.loads(kwargs["input"]) for args, kwargs in calls if "create" in args)
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_status_only_change_is_allowed_but_uses_fresh_version_guard():
    calls, run = runner(concurrent="status")
    assert module.promote(PUBLICATION, run=run)["desired_image_verified"]
    patch_args = next(
        args for args, _ in calls if "patch" in args and "--dry-run=server" not in args
    )
    assert json.loads(patch_args[patch_args.index("-p") + 1])[0]["value"] == "43"


def test_concurrent_spec_change_prevents_promotion():
    calls, run = runner(concurrent="spec")
    with pytest.raises(ValueError, match="spec changed"):
        module.promote(PUBLICATION, run=run)
    assert not any("patch" in args and "--dry-run=server" not in args for args, _ in calls)
