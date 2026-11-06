"""Private fixture bundle is immutable, scoped, and never a substitute for live readback."""

import argparse
import hashlib
import io
import importlib.util
import json
import tarfile
from pathlib import Path
from unittest.mock import Mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qualification_delivery import LIVE_CHECKS, publish, verify as verify_delivery

import pytest

MODULE = Path(__file__).resolve().parents[1] / "qualification_bundle.py"
SPEC = importlib.util.spec_from_file_location("qualification_bundle", MODULE)
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def inputs(tmp_path, *, extra=None):
    source = "a" * 40
    run_id = "00000000-0000-4000-8000-000000000001"
    account = "123456789012"
    role = f"arn:aws:iam::{account}:role/adp-example-trusted-deployment"
    settings = {"region": "us-east-1", "expected_role_arn": role, "gitlab_url": "https://gitlab.example",
                "admin_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:example",
                "cluster": "example", "manager_deployment": "gitlab-runner"}
    files = {"fixtures.json": json.dumps({"run_id": run_id, "source_revision": source, "account_id": account}),
             "evidence.json": "{}", "settings.json": json.dumps(settings), "ca.pem": "example-ca",
             "kubeconfig": "example-kubeconfig", "release-lock.json": "{}",
             "host-history/cold.json": "{}"}
    files.update(extra or {})
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name, value in files.items():
            data = value.encode()
            item = tarfile.TarInfo(name)
            item.size = len(data)
            output.addfile(item, io.BytesIO(data))
    archive.chmod(0o600)
    directory = tmp_path / "records"
    directory.mkdir(mode=0o700)
    args = argparse.Namespace(bundle_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                              release_lock_sha256=hashlib.sha256(b"{}").hexdigest(), run_id=run_id,
                              source_revision=source, account_id=account, region="us-east-1",
                              expected_role=role, workflow_run_id=456)
    return archive, directory, args


def test_private_bundle_binds_scope_and_refuses_unapproved_bytes(tmp_path):
    archive, directory, args = inputs(tmp_path)
    bundle.prepare(archive, directory, args)
    assert bundle.private(directory / "fixtures.json")
    args.bundle_sha256 = "f" * 64
    with pytest.raises(ValueError):
        bundle.prepare(archive, directory, args)


@pytest.mark.parametrize("invalid", ["traversal", "missing", "wrong_source", "wrong_role", "unsafe_archive"])
def test_private_bundle_refuses_missing_or_unowned_material(tmp_path, invalid):
    extras = {}
    if invalid == "traversal":
        extras["../outside.json"] = "private"
    if invalid == "missing":
        extras["release-lock.json"] = ""
    if invalid == "wrong_source":
        extras["fixtures.json"] = json.dumps({"run_id": "different", "source_revision": "b" * 40,
                                               "account_id": "123456789012"})
    if invalid == "wrong_role":
        extras["settings.json"] = json.dumps({"region": "us-east-1", "expected_role_arn": "wrong"})
    archive, directory, args = inputs(tmp_path, extra=extras)
    if invalid == "unsafe_archive":
        archive.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        bundle.prepare(archive, directory, args)
    assert not (tmp_path / "outside.json").exists()


@pytest.mark.parametrize("verdict,readback,accepted", [("pass", "passed", True),
                                                     ("incomplete", "passed", False),
                                                     ("pass", "missing", False)])
def test_final_verifier_receipt_requires_provider_readback(tmp_path, monkeypatch, verdict, readback, accepted):
    archive, directory, args = inputs(tmp_path)
    bundle.prepare(archive, directory, args)
    class Result:
        returncode = 0
        stdout = json.dumps({"verdict": verdict, "fresh_independent_api_readbacks": readback, "checks": dict.fromkeys(LIVE_CHECKS, "passed"), "network_cases": 15, "executor_cases": 9, "cleanup_cases": 4}).encode()

    def run(command, **kwargs):
        assert "--stage" in command and command[command.index("--stage") + 1] == "final"
        assert "--host-history" in command and "--aws-profile" not in command
        return Result()

    monkeypatch.setattr(bundle.subprocess, "run", run)
    env = {"ADP_SOURCE_REVISION": args.source_revision, "ACCOUNT_ID": args.account_id, "AWS_REGION": args.region,
           "GITHUB_RUN_ID": str(args.workflow_run_id), "GITHUB_RUN_ATTEMPT": "1", "ADP_CORRELATION": "accepted", "ENVIRONMENT": "dev", "GITHUB_REPOSITORY_ID": "123"}
    s3 = Mock();saved = {}
    s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
    def put(**kwargs):
        assert kwargs["IfNoneMatch"] == "*" and kwargs["ServerSideEncryption"] == "AES256"
        saved[kwargs["Key"]] = kwargs["Body"];return {"VersionId": "v1"}
    s3.put_object.side_effect = put
    s3.get_object.side_effect = lambda **kwargs: {"VersionId": "v1", "Body": io.BytesIO(saved[kwargs["Key"]])}
    if accepted:
        bundle.verify(directory, args, env=env, s3=s3)
        receipt = json.loads(bundle.private(directory / "final-verdict.json"))
        assert receipt["run_id"] == args.run_id and receipt["workflow_run_id"] == args.workflow_run_id
    else:
        with pytest.raises(ValueError):
            bundle.verify(directory, args, env=env, s3=s3)
        assert not (directory / "final-verdict.json").exists()


@pytest.mark.parametrize("changed", ["missing_check", "failed_check", "partial_cleanup", "wrong_source", "wrong_run", "readback"])
def test_partial_or_unbound_qualification_cannot_publish(changed):
    args = argparse.Namespace(source_revision="a"*40, account_id="123456789012", region="us-east-1", workflow_run_id=123, run_id="owned", bundle_sha256="b"*64)
    env = {"ADP_SOURCE_REVISION": args.source_revision, "ACCOUNT_ID": args.account_id, "AWS_REGION": args.region,
           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "ADP_CORRELATION": "accepted", "ENVIRONMENT": "dev", "GITHUB_REPOSITORY_ID": "456"}
    result = {"verdict": "pass", "fresh_independent_api_readbacks": "passed", "checks": dict.fromkeys(LIVE_CHECKS, "passed"), "network_cases": 15, "executor_cases": 9, "cleanup_cases": 4}
    if changed == "missing_check": result["checks"].pop("EC-04")
    elif changed == "failed_check": result["checks"]["EC-03"] = "incomplete"
    elif changed == "partial_cleanup": result["cleanup_cases"] = 3
    elif changed == "wrong_source": env["ADP_SOURCE_REVISION"] = "c"*40
    elif changed == "wrong_run": env["GITHUB_RUN_ID"] = "456"
    s3=Mock();s3.get_bucket_versioning.return_value={"Status":"Enabled"};s3.put_object.return_value={"VersionId":"v1"}
    s3.get_object.return_value={"VersionId":"v1","Body":io.BytesIO(b"changed")}
    with pytest.raises(ValueError):publish(result,args,env,s3)
    if changed != "readback":s3.put_object.assert_not_called()
