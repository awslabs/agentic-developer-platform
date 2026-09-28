"""Exercise the real deployment driver with deterministic fake AWS storage."""

import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location(
    "driver", Path(__file__).resolve().parents[1] / "deploy-webhook-code.py"
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-test-webhook"
FUNCTION = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adp-test-webhook"
MANIFEST = {
    "account_id": ACCOUNT,
    "region": "us-east-1",
    "archive_prefix": "lambda-artifacts/webhook",
    "targets": [
        {"artifact": "github.zip", "function_arn": FUNCTION, "execution_role": ROLE}
    ],
}
SHA = "a" * 40


class S3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_terminal = False
        self.missing_version = False

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*":
            assert key not in self.objects, "Already claimed; no replay"
        else:
            assert kwargs["IfMatch"] == self.objects[key]["etag"]
        body = kwargs["Body"]
        is_receipt = "/receipts/" in key
        if is_receipt and json.loads(body)["complete"] and self.fail_terminal:
            raise TimeoutError("Unknown terminal write result")
        etag = hashlib.sha256(body).hexdigest()
        self.objects[key] = {"body": body, "etag": etag}
        return {
            "ETag": etag,
            "VersionId": None
            if self.missing_version and not is_receipt
            else "version-" + etag,
        }


class Lambda:
    def __init__(self, s3):
        self.s3 = s3
        self.calls = []
        self.role = ROLE
        self.revision = "revision-before"
        self.digest = "old-digest"
        self.fail_update = False
        self.readback_drift = False

    def get_function_configuration(self, **kwargs):
        assert kwargs["FunctionName"] == FUNCTION
        return {
            "Role": self.role,
            "RevisionId": self.revision,
            "CodeSha256": "drift"
            if self.readback_drift and self.calls
            else self.digest,
            "LastUpdateStatus": "Successful",
        }

    def update_function_code(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["RevisionId"] == self.revision
        assert kwargs["Publish"] is False
        value = self.s3.objects[kwargs["S3Key"]]
        assert kwargs["S3ObjectVersion"] == "version-" + value["etag"]
        journal = json.loads(
            self.s3.objects[f"{MANIFEST['archive_prefix']}/receipts/{SHA}.json"]["body"]
        )
        assert journal["operations"][-1]["update_intent"] is True
        if self.fail_update:
            raise TimeoutError("Unknown update result")
        self.digest = base64.b64encode(hashlib.sha256(value["body"]).digest()).decode()
        self.revision = "revision-after"
        return {
            "Role": self.role,
            "RevisionId": self.revision,
            "CodeSha256": self.digest,
        }


@pytest.fixture
def setup(tmp_path):
    with zipfile.ZipFile(tmp_path / "github.zip", "w") as package:
        package.writestr("handler.py", "pass\n")
    s3 = S3()
    lambdas = Lambda(s3)

    def run(manifest=MANIFEST):
        return driver.deploy(
            manifest,
            tmp_path,
            SHA,
            "100",
            s3,
            lambdas,
            tmp_path / "receipt.json",
            sleep=lambda _: None,
        )

    return tmp_path, s3, lambdas, run


def test_pinned_success_and_durable_claim(setup):
    path, s3, lambdas, run = setup
    result = run()
    assert result["complete"] and result["operations"][0]["verified"]
    assert len(lambdas.calls) == 1
    local = json.loads((path / "receipt.json").read_text())
    assert local["commit_acknowledged"] and local["receipt"]["complete"]
    with pytest.raises(AssertionError, match="Already claimed"):
        run()
    assert len(lambdas.calls) == 1


def test_ambiguous_update_never_replayed_or_followed_by_other_write(setup):
    _, s3, lambdas, run = setup
    lambdas.fail_update = True
    with pytest.raises(TimeoutError):
        run()
    count = len(s3.calls)
    assert len(lambdas.calls) == 1
    receipt = json.loads(s3.calls[-1]["Body"])
    assert receipt["operations"][0]["update_intent"] and not receipt["complete"]
    assert len(s3.calls) == count


def test_missing_s3_version_prevents_lambda_mutation(setup):
    _, s3, lambdas, run = setup
    s3.missing_version = True
    with pytest.raises(AssertionError, match="Versioned code archive"):
        run()
    assert not lambdas.calls


def test_wrong_execution_role_prevents_update(setup):
    _, _, lambdas, run = setup
    lambdas.role = "other-role"
    with pytest.raises(AssertionError, match="Execution role drift"):
        run()
    assert not lambdas.calls


def test_code_drift_cannot_report_success(setup):
    _, _, lambdas, run = setup
    lambdas.readback_drift = True
    with pytest.raises(AssertionError, match="Code or role drift"):
        run()
    assert len(lambdas.calls) == 1


def test_uncertain_terminal_commit_not_acknowledged(setup):
    path, s3, _, run = setup
    s3.fail_terminal = True
    with pytest.raises(TimeoutError):
        run()
    assert (
        json.loads((path / "receipt.json").read_text())["commit_acknowledged"] is False
    )
    remote = json.loads(
        s3.objects[f"{MANIFEST['archive_prefix']}/receipts/{SHA}.json"]["body"]
    )
    assert remote["complete"] is False


@pytest.mark.parametrize("artifact", ["../outside.zip", "/tmp/outside.zip"])
def test_manifest_path_escape_refused_before_any_write(setup, artifact):
    _, s3, lambdas, run = setup
    manifest = copy.deepcopy(MANIFEST)
    manifest["targets"][0]["artifact"] = artifact
    with pytest.raises(AssertionError):
        run(manifest)
    assert not s3.calls and not lambdas.calls


def test_zip_path_escape_refused_before_any_write(setup):
    path, s3, _, run = setup
    with zipfile.ZipFile(path / "github.zip", "w") as package:
        package.writestr("../escape", "x")
    with pytest.raises(AssertionError):
        run()
    assert not s3.calls


def test_archive_symlink_refused(setup):
    path, s3, _, run = setup
    archive = path / "github.zip"
    archive.rename(path / "actual.zip")
    archive.symlink_to(path / "actual.zip")
    with pytest.raises(AssertionError):
        run()
    assert not s3.calls


def test_unbound_manifest_refused_before_any_write(setup):
    _, s3, _, run = setup
    manifest = copy.deepcopy(MANIFEST)
    manifest["targets"] = []
    with pytest.raises(AssertionError, match="Unbound"):
        run(manifest)
    assert not s3.calls


def test_concurrent_revision_change_prevents_completion(setup):
    _, _, lambdas, run = setup
    original = lambdas.get_function_configuration

    def changed(**kwargs):
        result = original(**kwargs)
        if lambdas.calls:
            result["RevisionId"] = "competing-writer-revision"
        return result

    lambdas.get_function_configuration = changed
    with pytest.raises(AssertionError, match="Concurrent revision"):
        run()
    assert len(lambdas.calls) == 1


def test_bad_update_hash_stops_before_poll_or_second_mutation(setup):
    _, s3, lambdas, run = setup
    original = lambdas.update_function_code

    def changed(**kwargs):
        result = original(**kwargs)
        result["CodeSha256"] = "wrong-archive"
        return result

    lambdas.update_function_code = changed
    with pytest.raises(AssertionError):
        run()
    assert len(lambdas.calls) == 1
    remote = json.loads(
        s3.objects[f"{MANIFEST['archive_prefix']}/receipts/{SHA}.json"]["body"]
    )
    assert not remote["complete"] and "update_response" not in remote["operations"][0]
