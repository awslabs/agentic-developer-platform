"""An update retry may reuse only a real image with an immutable SHA tag."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "upgrade-image-cache.py"
SPEC = importlib.util.spec_from_file_location("upgrade_image_cache", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime:" + "a" * 40
DIGEST = "sha256:" + "b" * 64


def fake_aws(
    *, mutability="IMMUTABLE", image_digest=DIGEST, image_missing=False,
    repository_missing=False, error=None,
):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if error:
            return SimpleNamespace(returncode=1, stderr=error, stdout="")
        if args[2] == "describe-repositories":
            if repository_missing:
                return SimpleNamespace(returncode=1, stderr="RepositoryNotFoundException", stdout="")
            return SimpleNamespace(
                returncode=0, stderr="",
                stdout=json.dumps({"repositories": [{"imageTagMutability": mutability}]}),
            )
        if image_missing:
            return SimpleNamespace(returncode=1, stderr="ImageNotFoundException", stdout="")
        return SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"imageDetails": [{"imageDigest": image_digest}]}),
        )

    return run, calls


def test_reuses_only_an_existing_immutable_full_sha_image():
    run, calls = fake_aws()
    assert MODULE.reusable_image(IMAGE, run=run) == IMAGE.rsplit(":", 1)[0] + "@" + DIGEST
    assert [call[2] for call in calls] == ["describe-repositories", "describe-images"]
    assert "imageTag=" + "a" * 40 in calls[1]


@pytest.mark.parametrize(
    "kwargs",
    [{"mutability": "MUTABLE"}, {"image_missing": True}, {"repository_missing": True}],
)
def test_builds_when_the_tag_is_not_safely_reusable(kwargs):
    run, _ = fake_aws(**kwargs)
    assert MODULE.reusable_image(IMAGE, run=run) == ""


def test_unexpected_ecr_error_does_not_silently_rebuild():
    run, _ = fake_aws(error="AccessDeniedException")
    with pytest.raises(RuntimeError, match="AccessDeniedException"):
        MODULE.reusable_image(IMAGE, run=run)


def test_rejects_an_invalid_existing_digest():
    run, _ = fake_aws(image_digest="not-a-digest")
    with pytest.raises(ValueError, match="no valid ECR image digest"):
        MODULE.reusable_image(IMAGE, run=run)


def test_rejects_a_tag_that_is_not_the_full_source_sha():
    run, calls = fake_aws()
    with pytest.raises(ValueError, match="full source SHA"):
        MODULE.reusable_image(IMAGE.rsplit(":", 1)[0] + ":latest", run=run)
    assert calls == []
