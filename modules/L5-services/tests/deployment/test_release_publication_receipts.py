"""Hosted publication retains real attempt receipts without borrowing ARC IRSA."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/L5-services/scripts"))
spec = importlib.util.spec_from_file_location(
    "release_publication_receipts", ROOT / "modules/L5-services/scripts/release-publication-receipts.py")
receipts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receipts)


@pytest.fixture
def selected(tmp_path):
    environment = {
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "example/project", "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_WORKFLOW_REF": "example/project/.github/workflows/l5-services-release.yml@refs/heads/main",
        "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_WORKFLOW_SHA": "b" * 40,
        "L5_CHECKOUT_REVISION": "a" * 40, "REVIEWED_SOURCE_SHA": "a" * 40,
        "L5_SERVICE": "transform", "L5_ENVIRONMENT": "dev", "L5_RELEASE_OPERATION": "install",
        "L5_MUTATION_ROLE_ARN": "arn:aws:iam::123456789012:role/deploy", "RELEASE_BUCKET": "private-release",
    }
    identity = receipts.release_identity(environment)
    prefix = f"l5/security/{'a' * 40}/{'c' * 64}"
    artifacts = {**identity, "bucket": "private-release",
                 "manifest": {"key": prefix + "/manifest.json", "version_id": "m1"},
                 "artifacts": {"wheel": {"key": prefix + "/wheel.whl", "version_id": "w1", "sha256": "c" * 64},
                               "archive": {"key": prefix + "/workers.zip", "version_id": "a1", "sha256": "d" * 64}}}
    images = {**identity, "wheel_sha256": "c" * 64, "archive_sha256": "d" * 64,
              "images": {"provider": "registry/image@sha256:" + "e" * 64,
                         "checks": "registry/image@sha256:" + "f" * 64}, "builds": {}}
    for name, value in (("publication-context.json", identity), ("release-artifacts.json", artifacts),
                        ("validation-images.json", images)):
        (tmp_path / name).write_text(json.dumps(value))
    return tmp_path, environment


class Store:
    def __init__(self):
        self.calls, self.objects = [], {}
        self.account, self.role = "123456789012", "deploy"
        self.fault = None

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return {"Account": self.account, "Arn": f"arn:aws:sts::{self.account}:assumed-role/{self.role}/session"}
        key = args[args.index("--key") + 1]
        assert key.startswith(f"l5/security/{'a' * 40}/{'c' * 64}/publication/123/456/2/")
        if args[1] == "put-object":
            assert args[args.index("--if-none-match") + 1] == "*"
            if self.fault == "denied":
                raise subprocess.CalledProcessError(1, "aws", stderr=b"AccessDenied")
            if key in self.objects:
                raise subprocess.CalledProcessError(1, "aws", stderr=b"PreconditionFailed")
            self.objects[key] = Path(args[args.index("--body") + 1]).read_bytes()
        if args[1] == "get-object":
            assert args[args.index("--version-id") + 1] == "v1"
            Path(args[-1]).write_bytes(self.objects[key] + (b"changed" if self.fault == "bytes" else b""))
        return {"VersionId": "other" if self.fault == "version" and args[1] == "get-object" else "v1"}


def retain(selected, store, outcome="success"):
    directory, environment = selected
    return receipts.retain(directory, outcome, environment, read=store, authorize=lambda *_: None)


def test_retains_original_three_receipts_and_immutable_replay(selected):
    store = Store()
    first = retain(selected, store)
    assert retain(selected, store) == first
    payload = json.loads(next(iter(store.objects.values())))
    assert payload["run_attempt"] == 2 and payload["qualification"] is False
    assert payload["publication_outcome"] == "success" and payload["images_published"] is True
    for name, record in payload["files"].items():
        raw = (selected[0] / name).read_bytes()
        assert record == {"content": raw.decode(), "sha256": receipts.hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled"])
def test_failure_retains_completed_artifacts_without_claiming_images(selected, outcome):
    (selected[0] / "validation-images.json").unlink()
    store = Store()
    retain(selected, store, outcome)
    payload = json.loads(next(iter(store.objects.values())))
    assert payload["images_published"] is False and payload["publication_outcome"] == outcome
    assert set(payload["files"]) == {"publication-context.json", "release-artifacts.json"}


@pytest.mark.parametrize("mutation", ["missing", "symlink", "attempt", "bucket", "source-key", "image-wheel"])
def test_rejects_missing_linked_or_different_attempt_receipts_before_aws(selected, mutation):
    directory, _ = selected
    image = directory / "validation-images.json"
    if mutation in {"missing", "symlink"}:
        image.unlink()
        if mutation == "symlink":
            image.symlink_to(directory / "publication-context.json")
    else:
        name = "validation-images.json" if mutation == "image-wheel" else "release-artifacts.json"
        path = directory / name
        document = json.loads(path.read_text())
        if mutation == "attempt":
            document["run_attempt"] = 1
        elif mutation == "bucket":
            document["bucket"] = "other-release"
        elif mutation == "source-key":
            document["manifest"]["key"] = "l5/security/other/manifest.json"
        else:
            document["wheel_sha256"] = "b" * 64
        path.write_text(json.dumps(document))
    store = Store()
    with pytest.raises(ValueError):
        retain(selected, store)
    assert store.calls == []


@pytest.mark.parametrize("fault", ["bytes", "version"])
def test_requires_exact_published_bytes_and_storage_version(selected, fault):
    store = Store()
    store.fault = fault
    with pytest.raises(ValueError, match="readback"):
        retain(selected, store)


def test_immutable_conflict_cannot_replace_original_receipts(selected):
    store = Store()
    retain(selected, store)
    original = dict(store.objects)
    with pytest.raises(ValueError, match="readback"):
        retain(selected, store, "failure")
    assert store.objects == original


def test_wrong_role_and_denied_write_are_not_fallback_authority(selected):
    store = Store()
    store.role = "runner"
    with pytest.raises(ValueError, match="selected role"):
        retain(selected, store)
    assert not store.objects
    store.role, store.fault = "deploy", "denied"
    with pytest.raises(subprocess.CalledProcessError):
        retain(selected, store)
    assert not any(args[1] == "head-object" for args in store.calls)


def test_workflow_routes_hosted_receipts_and_preserves_downstream_manifest():
    workflow = yaml.safe_load((ROOT / ".github/workflows/l5-services-release.yml").read_text())
    publish = workflow["jobs"]["publish"]
    hosted = next(step for step in publish["steps"] if step.get("id") == "transform_receipts")
    assert hosted["if"] == "always() && inputs.service == 'transform' && steps.shared_artifacts.outcome == 'success'"
    assert "release-publication-receipts.py" in hosted["run"]
    assert hosted["env"]["PUBLICATION_OUTCOME"] == "${{ steps.transform_images.outcome }}"
    ordinary = next(step for step in publish["steps"] if step.get("uses", "").endswith("upload-ci-evidence-s3"))
    assert ordinary["if"] == "inputs.service != 'transform'"
    assert publish["outputs"]["manifest_version"] == "${{ steps.pinned.outputs.manifest_version }}"
    assert workflow["jobs"]["install"]["env"]["MANIFEST_VERSION"] == "${{ needs.publish.outputs.manifest_version }}"
