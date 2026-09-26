"""Run the actual release dispatcher against declared transport fixtures in remote CI."""

import copy
import json
from pathlib import Path

import _release_path  # noqa: F401
import pytest
import yaml
from releases import build_paid_worker as release
from releases.resolve_lock import resolve_build_inputs

MODULE = Path(__file__).resolve().parents[1]


@pytest.fixture
def config():
    return {
        "account": "111122223333",
        "region": "us-east-1",
        "environment": "dev",
        "source_sha": "a" * 40,
        "python_image": "python:3.12-slim@sha256:" + "b" * 64,
    }


class Transport:
    def __init__(self, config):
        self.config = config
        self.project, self.bucket = release.validate(config)
        self.registry = f"{config['account']}.dkr.ecr.{config['region']}.amazonaws.com"
        self.role = f"arn:aws:iam::{config['account']}:role/adp-dev-codebuild-superplane-paid-worker"
        self.fixed = {
            "ACCOUNT_ID": config["account"],
            "REGISTRY": self.registry,
        }
        self.project_doc = {
            "name": self.project,
            "arn": f"arn:aws:codebuild:{config['region']}:{config['account']}:project/{self.project}",
            "serviceRole": self.role,
            "artifacts": {"type": "NO_ARTIFACTS"},
            "timeoutInMinutes": 60,
            "queuedTimeoutInMinutes": 480,
            "source": {
                "type": "S3",
                "buildspec": release.BUILDSPEC,
                "location": f"{self.bucket}/codebuild/src/{self.project}/explicit-source-required.zip",
            },
            "environment": {
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/amazonlinux2-x86_64-standard:5.0",
                "computeType": "BUILD_GENERAL1_MEDIUM",
                "privilegedMode": True,
                "imagePullCredentialsType": "CODEBUILD",
                "environmentVariables": self.variables(self.fixed),
            },
        }
        self.objects = {}
        self.lose_after_id = False
        self.lifecycle = [
            {
                "Status": "Enabled",
                "Filter": {"Prefix": "codebuild/src/"},
                "Expiration": {"Days": 7},
            }
        ]
        self.calls = []
        self.starts = 0
        self.unknown = False
        self.merged = True
        self.build_change = None
        self.build_id = self.project + ":12345678-1234-1234-1234-123456789abc"
        self.key = f"codebuild/src/{self.project}/{config['source_sha']}-12345-678.zip"

    @staticmethod
    def variables(values):
        return [
            {"name": key, "value": value, "type": "PLAINTEXT"}
            for key, value in values.items()
        ]

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return self.config["source_sha"]
        if argv[:2] == ["git", "status"]:
            return ""
        if argv[0] == "gh":
            return json.dumps(
                {
                    "status": "ahead" if self.merged else "diverged",
                    "merge_base_commit": {"sha": self.config["source_sha"]},
                }
            )
        if argv[0] == "bash":
            assert argv[1] == str(release.ROOT / "platform/scripts/codebuild-run.sh")
            assert kwargs["env"]["ADP_RELEASE_BUILD"] == "true"
            assert kwargs["env"]["AWS_MAX_ATTEMPTS"] == "1"
            assert kwargs["env"]["AWS_RETRY_MODE"] == "standard"
            assert kwargs["env"]["SOURCE_SHA"] == self.config["source_sha"]
            assert kwargs["env"]["STATE_BUCKET"] == self.bucket
            self.starts += 1
            self.overrides = dict(
                value.removeprefix("name=").split(",value=", 1) for value in argv[3:]
            )
            if self.unknown:
                raise release.BuildRefused("fixture transport lost after start")
            kwargs["on_line"](f"  Build: {self.build_id} (source: {self.key})\n")
            if self.lose_after_id:
                raise release.BuildRefused("lost polling after identity")
            return ""
        operation = tuple(argv[1:3])
        if operation == ("s3api", "get-bucket-versioning"):
            return json.dumps({"Status": "Enabled"})
        if operation == ("s3api", "get-bucket-lifecycle-configuration"):
            return json.dumps({"Rules": self.lifecycle})
        if operation == ("s3api", "put-object"):
            key = argv[argv.index("--key") + 1]
            if "--if-none-match" in argv and key in self.objects:
                raise release.BuildRefused("conditional claim already exists")
            if "--if-match" in argv:
                assert self.objects[key]["etag"] == argv[argv.index("--if-match") + 1]
            value = json.loads(Path(argv[argv.index("--body") + 1]).read_text())
            etag = str(len(self.calls))
            self.objects[key] = {"etag": etag, "value": value}
            return json.dumps(
                {
                    "VersionId": etag,
                    "ETag": etag,
                    "ChecksumSHA256": argv[argv.index("--checksum-sha256") + 1],
                }
            )
        if operation == ("sts", "get-caller-identity"):
            return json.dumps({"Account": self.config["account"]})
        if operation == ("codebuild", "batch-get-projects"):
            return json.dumps({"projects": [self.project_doc]})
        if operation == ("ecr", "describe-repositories"):
            return json.dumps(
                {
                    "repositories": [
                        {
                            "registryId": self.config["account"],
                            "repositoryName": release.REPOSITORY,
                            "repositoryArn": f"arn:aws:ecr:{self.config['region']}:{self.config['account']}:repository/{release.REPOSITORY}",
                            "imageTagMutability": "IMMUTABLE",
                        }
                    ]
                }
            )
        if operation == ("codebuild", "batch-get-builds"):
            doc = {
                "id": self.build_id,
                "projectName": self.project,
                "serviceRole": self.role,
                "buildStatus": "SUCCEEDED",
                "timeoutInMinutes": 60,
                "queuedTimeoutInMinutes": 480,
                "source": {
                    "type": "S3",
                    "location": f"{self.bucket}/{self.key}",
                    "buildspec": release.BUILDSPEC,
                },
                "environment": {
                    **self.project_doc["environment"],
                    "environmentVariables": self.variables(
                        {
                            **self.fixed,
                            **self.overrides,
                            "ADP_SOURCE_SHA": self.config["source_sha"],
                        }
                    ),
                },
            }
            if self.build_change:
                self.build_change(doc)
            return json.dumps({"builds": [doc]})
        if operation == ("ecr", "describe-images"):
            return json.dumps(
                {
                    "imageDetails": [
                        {
                            "registryId": self.config["account"],
                            "repositoryName": release.REPOSITORY,
                            "imageTags": [self.config["source_sha"]],
                            "imageDigest": "sha256:" + "c" * 64,
                        }
                    ]
                }
            )
        raise AssertionError("unexpected release command")


def test_exact_build_records_real_response_digest_without_promoting(config, tmp_path):
    transport = Transport(config)
    receipt = tmp_path / "receipt.json"
    result = release.build(config, receipt, run=transport)
    assert result["state"] == "built-awaiting-image-review"
    assert result["digest"] == "sha256:" + "c" * 64
    assert result["promoted"] is False
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert transport.starts == 1
    assert transport.overrides["ECR_REPO"] == release.REPOSITORY
    assert transport.overrides["PYTHON_IMAGE"] == config["python_image"]


def test_unknown_start_is_durable_and_cannot_be_repeated(config, tmp_path):
    transport = Transport(config)
    transport.unknown = True
    receipt = tmp_path / "receipt.json"
    with pytest.raises(release.BuildRefused):
        release.build(config, receipt, run=transport)
    assert json.loads(receipt.read_text())["state"] == "dispatch-outcome-unknown"
    with pytest.raises(release.BuildRefused, match="receipt already exists"):
        release.build(config, receipt, run=transport)
    assert transport.starts == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "another-project"),
        ("serviceRole", "arn:aws:iam::111122223333:role/broad"),
        ("source", {"type": "S3", "buildspec": "executor.yml"}),
    ],
)
def test_project_drift_refuses_before_upload_or_build(config, tmp_path, field, value):
    transport = Transport(config)
    transport.project_doc[field] = value
    with pytest.raises(release.BuildRefused):
        release.build(config, tmp_path / "receipt.json", run=transport)
    assert transport.starts == 0


def test_unmerged_source_refuses_before_cloud(config, tmp_path):
    transport = Transport(config)
    transport.merged = False
    with pytest.raises(release.BuildRefused, match="merged"):
        release.build(config, tmp_path / "receipt.json", run=transport)
    assert not any(call[0] == "aws" for call in transport.calls)


@pytest.mark.parametrize("field", ["PYTHON_IMAGE", "ADP_SOURCE_SHA", "ECR_REPO"])
def test_completed_build_must_match_base_source_and_repository(config, tmp_path, field):
    transport = Transport(config)

    def change(doc):
        next(
            entry
            for entry in doc["environment"]["environmentVariables"]
            if entry["name"] == field
        )["value"] = "wrong"

    transport.build_change = change
    receipt = tmp_path / "receipt.json"
    with pytest.raises(release.BuildRefused, match="completed build differs"):
        release.build(config, receipt, run=transport)
    assert json.loads(receipt.read_text())["digest"] is None
    assert not any(call[1:3] == ["ecr", "describe-images"] for call in transport.calls)


def test_pending_paid_lock_is_owned_by_explicit_app_infrastructure():
    manifest = json.loads((MODULE / "infra/paid-worker-project.json").read_text())
    assert set(manifest) == {release.COMPONENT}
    # Exercise the actual platform discovery glob: merely storing a manifest
    # inside the app must not add resources to ordinary platform installations.
    enrolled = {
        key
        for path in (release.ROOT / "modules/domain-apps").glob(
            "*/codebuild/projects.json"
        )
        for key in json.loads(path.read_text())
    }
    assert release.COMPONENT not in enrolled
    entry = manifest[release.COMPONENT]
    assert entry["buildspec"] == release.BUILDSPEC
    assert entry["ecr_repos"] == [release.REPOSITORY]
    assert entry["build_timeout"] == release.BUILD_TIMEOUT_MINUTES
    platform = (release.ROOT / "platform/infra/modules/codebuild/main.tf").read_text()
    assert '"superplane-paid-worker" = {' not in platform
    lock = yaml.safe_load((MODULE / "releases/superplane.lock.yaml").read_text())
    pending = lock["pending_images"][release.COMPONENT]
    assert pending["ecr_repository"] == release.REPOSITORY
    assert (
        pending["project_manifest"]
        == "modules/domain-apps/superplane/infra/paid-worker-project.json"
    )
    assert release.COMPONENT not in lock["images"]


def test_paid_build_metadata_survives_reviewed_digest_promotion(tmp_path):
    path = MODULE / "releases/superplane.lock.yaml"
    original = resolve_build_inputs(release.COMPONENT, path)
    lock = copy.deepcopy(yaml.safe_load(path.read_text()))
    metadata = lock["pending_images"].pop(release.COMPONENT)
    metadata.pop("blocked_by")
    lock["image_sources"][release.COMPONENT] = metadata
    lock["images"][release.COMPONENT] = "sha256:" + "c" * 64
    promoted = tmp_path / "promoted.yaml"
    promoted.write_text(yaml.safe_dump(lock))
    assert resolve_build_inputs(release.COMPONENT, promoted) == original


def test_project_changes_during_build_cannot_be_accepted(config, tmp_path):
    transport = Transport(config)

    def change(doc):
        doc["environment"]["image"] = "unreviewed-build-runtime"

    transport.build_change = change
    with pytest.raises(release.BuildRefused, match="completed build environment"):
        release.build(config, tmp_path / "receipt.json", run=transport)
    assert transport.starts == 1


@pytest.mark.parametrize("changed_base", [False, True])
def test_durable_claim_blocks_repeat_even_with_new_local_receipt(
    config, tmp_path, changed_base
):
    transport = Transport(config)
    transport.unknown = True
    with pytest.raises(release.BuildRefused):
        release.build(
            config, tmp_path / "lost-directory" / "receipt.json", run=transport
        )
    retry = dict(config)
    if changed_base:
        retry["python_image"] = "python:3.12-slim@sha256:" + "d" * 64
    with pytest.raises(release.BuildRefused, match="conditional claim already exists"):
        release.build(retry, tmp_path / "new-receipt.json", run=transport)
    assert transport.starts == 1
    claims = [key for key in transport.objects if key.endswith("/claim.json")]
    assert len(claims) == 1 and not claims[0].startswith("codebuild/")


def test_build_identity_is_durable_before_polling_completes(config, tmp_path):
    transport = Transport(config)
    transport.lose_after_id = True
    with pytest.raises(release.BuildRefused):
        release.build(config, tmp_path / "receipt.json", run=transport)
    child = next(
        value["value"]
        for key, value in transport.objects.items()
        if key.endswith("/child.json")
    )
    assert child["build_id"] == transport.build_id
    assert child["source_key"] == transport.key
    assert child["digest"] is None


@pytest.mark.parametrize(
    "retention",
    [
        {"Expiration": {"Days": 7}},
        {"NoncurrentVersionExpiration": {"NoncurrentDays": 30}},
        {"Transitions": [{"Days": 1, "StorageClass": "GLACIER"}]},
    ],
)
def test_matching_lifecycle_cannot_erase_or_archive_dispatch_evidence(
    config, tmp_path, retention
):
    transport = Transport(config)
    transport.lifecycle = [
        {"Status": "Enabled", "Filter": {"Prefix": "superplane/"}, **retention}
    ]
    with pytest.raises(release.BuildRefused, match="never expire"):
        release.build(config, tmp_path / "receipt.json", run=transport)
    assert transport.starts == 0 and not transport.objects


@pytest.mark.parametrize("field", ["timeoutInMinutes", "queuedTimeoutInMinutes"])
def test_project_duration_is_finite_and_exact(config, tmp_path, field):
    transport = Transport(config)
    transport.project_doc[field] += 1
    with pytest.raises(release.BuildRefused, match="timeout/queue window"):
        release.build(config, tmp_path / "receipt.json", run=transport)
    assert transport.starts == 0


@pytest.mark.parametrize("field", ["timeoutInMinutes", "queuedTimeoutInMinutes"])
def test_completed_build_cannot_extend_reviewed_duration(config, tmp_path, field):
    transport = Transport(config)

    def change(doc):
        doc[field] += 1

    transport.build_change = change
    with pytest.raises(release.BuildRefused, match="completed build differs"):
        release.build(config, tmp_path / "receipt.json", run=transport)


@pytest.mark.parametrize("name", ["claim.json", "child.json"])
def test_lifecycle_targeting_exact_evidence_object_is_refused(config, tmp_path, name):
    transport = Transport(config)
    claim = release.DispatchClaim(
        None, transport.bucket, config["account"], transport.project, config
    )
    transport.lifecycle = [
        {
            "Status": "Enabled",
            "Filter": {"Prefix": claim.prefix + name},
            "Expiration": {"Days": 1},
        }
    ]
    with pytest.raises(release.BuildRefused, match="never expire"):
        release.build(config, tmp_path / "receipt.json", run=transport)
    assert transport.starts == 0


def test_streaming_shared_cli_output_is_observed_before_completion():
    import sys

    observed = []
    result = release.command(
        [sys.executable, "-c", "print('build identity', flush=True)"],
        on_line=observed.append,
        timeout=5,
    )
    assert result == ""
    assert observed == ["build identity\n"]


def test_failed_identity_recording_stops_local_polling_only():
    import sys

    def reject(_line):
        raise release.BuildRefused("recording unavailable")

    with pytest.raises(release.BuildRefused, match="recording unavailable"):
        release.command(
            [
                sys.executable,
                "-c",
                "import time; print('build identity', flush=True); time.sleep(60)",
            ],
            on_line=reject,
            timeout=5,
        )


def test_polling_deadline_handles_descendant_keeping_stdout_open():
    import sys

    script = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print('identity', flush=True)"
    with pytest.raises(release.BuildRefused, match="local polling deadline"):
        release.command(
            [sys.executable, "-c", script], on_line=lambda line: None, timeout=0.25
        )
