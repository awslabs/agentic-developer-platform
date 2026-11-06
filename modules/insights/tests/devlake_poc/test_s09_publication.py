import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/insights/poc/devlake/publish.py"


class PublicationGateTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("devlake_publish", MODULE)
        self.publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.publisher)
        environment = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "aws-e/adp",
                                                "GITHUB_REF": "refs/heads/main",
                                                "GITHUB_EVENT_NAME": "workflow_dispatch"})
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        self.digest = "sha256:" + "a" * 64
        self.schema_digest = "b" * 64
        self.account = "123456789012"
        self.repository = "devlake-poc/api"
        self.uri = f"{self.account}.dkr.ecr.us-east-1.amazonaws.com/{self.repository}"
        self.checks = Path(self.temporary.name) / "checks.json"
        self.sbom = self.checks.with_name("sbom.json")
        self.sbom.write_text('{"bomFormat":"CycloneDX","components":[{"name":"fixture"}]}')
        self.sbom_digest = hashlib.sha256(self.sbom.read_bytes()).hexdigest()
        self.evidence = {
            "revision": self.revision, "component": "api", "image_digest": self.digest,
            "schema_sha256": self.schema_digest,
            "sbom_sha256": self.sbom_digest,
            "checks": {name: "passed" for name in self.publisher.REQUIRED_CHECKS},
        }
        self.checks.write_text(json.dumps(self.evidence))
        self.args = SimpleNamespace(repository_uri=self.uri, account=self.account, region="us-east-1",
                                    repository=self.repository, layout=Path(self.temporary.name) / "image",
                                    expected_digest=self.digest, revision=self.revision, component="api",
                                    checks=self.checks, role="devlake-publisher", connection_label="fixture")

    def invoke_cli(self, receipt, repository_uri=None, preflight=None):
        arguments = ["publish.py", "--publish", "--component", "api", "--layout", str(self.args.layout),
                     "--expected-digest", self.digest, "--revision", self.revision,
                     "--checks", str(self.checks), "--account", self.account,
                     "--role", self.args.role, "--region", self.args.region,
                     "--repository", self.repository, "--repository-uri", repository_uri or self.uri,
                     "--connection-label", "fixture", "--receipt", str(receipt)]
        with mock.patch.object(sys, "argv", arguments), mock.patch.dict(sys.modules, {
                "finalize": SimpleNamespace(preflight=preflight or (lambda *scope: None))}):
            self.publisher.main()

    def assert_no_receipt(self, receipt):
        self.assertFalse(receipt.exists())
        self.assertFalse(receipt.with_suffix(".pending").exists())

    def test_pr_or_non_main_workflow_refuses_publication_before_credentials(self):
        with mock.patch.object(self.publisher, "inspect") as inspect, mock.patch.object(self.publisher, "broker") as broker:
            for event, branch in (("pull_request", "refs/heads/main"),
                                  ("workflow_dispatch", "refs/heads/agent/issue-8901"),
                                  ("push", "refs/heads/main")):
                with self.subTest(event=event, branch=branch), mock.patch.dict(
                        os.environ, {"GITHUB_EVENT_NAME": event, "GITHUB_REF": branch}):
                    with self.assertRaisesRegex(ValueError, "protected manual main-branch publication"):
                        self.publisher.publish(self.args, lambda *args: None)
            inspect.assert_not_called()
            broker.assert_not_called()

    def test_reject_wrong_destination_or_failed_checks_before_credentials(self):
        with mock.patch.object(self.publisher, "inspect") as inspect, mock.patch.object(self.publisher, "broker") as broker:
            self.args.repository_uri = self.uri.replace("devlake-poc/api", "devlake-poc/other")
            with self.assertRaisesRegex(ValueError, "exact scoped ECR repository"):
                self.publisher.publish(self.args, lambda *args: None)
            inspect.assert_not_called()
            broker.assert_not_called()
            self.args.repository_uri = self.uri
            self.evidence["checks"]["secrets"] = "failed"
            self.checks.write_text(json.dumps(self.evidence))
            with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "aws-e/adp", "GITHUB_REF": "refs/heads/main",
                                                "GITHUB_EVENT_NAME": "workflow_dispatch"}):
                with mock.patch("subprocess.check_output", return_value=self.revision):
                    inspect.return_value = self.schema_digest
                    with self.assertRaisesRegex(ValueError, "checks are missing or failed"):
                        self.publisher.publish(self.args, lambda *args: None)
            broker.assert_not_called()

    def test_wrong_destination_and_local_image_digest_leave_no_receipt(self):
        receipt = Path(self.temporary.name) / "publication.json"
        self.args.layout.mkdir()
        (self.args.layout / "index.json").write_text(json.dumps({
            "manifests": [{"digest": "sha256:" + "c" * 64}],
        }))
        with mock.patch.object(self.publisher, "broker") as broker:
            with self.assertRaisesRegex(ValueError, "exact scoped ECR repository"):
                self.invoke_cli(receipt, repository_uri=self.uri.replace("devlake-poc/api", "devlake-poc/other"),
                                preflight=lambda *scope: self.fail("finalizer ran before destination check"))
            self.assert_no_receipt(receipt)
            with self.assertRaisesRegex(ValueError, "image digest differs from selected layout"):
                self.invoke_cli(receipt, preflight=lambda *scope: self.fail("finalizer ran before image check"))
            self.assert_no_receipt(receipt)
            broker.assert_not_called()

    def test_registry_or_ecr_digest_mismatch_after_upload_leaves_no_receipt(self):
        receipt = Path(self.temporary.name) / "publication.json"
        for mismatch in ("registry", "ecr"):
            with self.subTest(mismatch=mismatch):
                operations = []

                def broker(label, *command):
                    self.assertEqual(label, "fixture")
                    operations.append(command[2])
                    if command[2] == "get-caller-identity":
                        return json.dumps({"Account": self.account,
                                           "Arn": f"arn:aws:sts::{self.account}:assumed-role/devlake-publisher/fixture"})
                    if command[2] == "describe-repositories":
                        return json.dumps({"repositories": [{"repositoryUri": self.uri,
                                                               "registryId": self.account,
                                                               "imageTagMutability": "IMMUTABLE"}]})
                    if command[2] == "batch-get-image":
                        return json.dumps({"images": [], "failures": [{"failureCode": "ImageNotFound",
                                                                         "imageId": {"imageTag": self.revision + "-api"}}]})
                    if command[2] == "get-login-password":
                        return "fixture-password"
                    if command[2] == "describe-images":
                        return json.dumps({"imageDetails": [{"imageDigest": "sha256:" + "d" * 64}]})
                    self.fail("unexpected broker operation")

                def read(command, **kwargs):
                    if command[:3] == ["git", "rev-parse", "HEAD"]:
                        return self.revision
                    if command[:2] == ["crane", "digest"]:
                        return "sha256:" + "d" * 64 if mismatch == "registry" else self.digest
                    self.fail("unexpected read operation")

                with mock.patch("subprocess.check_output", side_effect=read), \
                        mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                        mock.patch.object(self.publisher, "broker", side_effect=broker), \
                        mock.patch("subprocess.run") as push:
                    with self.assertRaisesRegex(ValueError, "digest (?:readback )?differs from checked artifact"):
                        self.invoke_cli(receipt, preflight=lambda *scope: operations.append("finalizer"))
                self.assertEqual(push.call_count, 2)
                self.assertEqual(operations[:3], ["get-caller-identity", "finalizer", "describe-repositories"])
                self.assertEqual("describe-images" in operations, mismatch == "ecr")
                self.assert_no_receipt(receipt)

    def test_reject_wrong_role_and_finalizer_before_push(self):
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "aws-e/adp", "GITHUB_REF": "refs/heads/main",
                                            "GITHUB_EVENT_NAME": "workflow_dispatch"}):
            with mock.patch("subprocess.check_output", return_value=self.revision), \
                    mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                    mock.patch.object(self.publisher, "broker", return_value=json.dumps({
                        "Account": self.account,
                        "Arn": f"arn:aws:sts::{self.account}:assumed-role/wrong-role/session",
                    })):
                with self.assertRaisesRegex(ValueError, "exact assumed role"):
                    self.publisher.publish(self.args, lambda *args: None)
            with mock.patch("subprocess.check_output", return_value=self.revision), \
                    mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                    mock.patch.object(self.publisher, "broker", return_value=json.dumps({
                        "Account": self.account,
                        "Arn": f"arn:aws:sts::{self.account}:assumed-role/devlake-publisher/session",
                    })), mock.patch("subprocess.run") as push:
                with self.assertRaisesRegex(ValueError, "finalizer refused"):
                    self.publisher.publish(self.args, lambda *args: (_ for _ in ()).throw(ValueError("finalizer refused")))
                push.assert_not_called()

    def test_exact_scope_receives_digest_readback_only_after_preflight(self):
        calls = []

        def broker(label, *command):
            self.assertEqual(label, "fixture")
            calls.append(command)
            if "get-caller-identity" in command:
                return json.dumps({"Account": self.account,
                                   "Arn": f"arn:aws:sts::{self.account}:assumed-role/devlake-publisher/session"})
            if "describe-repositories" in command:
                return json.dumps({"repositories": [{"repositoryUri": self.uri, "registryId": self.account,
                                                       "imageTagMutability": "IMMUTABLE"}]})
            if "batch-get-image" in command:
                return json.dumps({"images": [], "failures": [{"failureCode": "ImageNotFound",
                                                                 "imageId": {"imageTag": self.revision + "-api"}}]})
            if "get-login-password" in command:
                return "fixture-password"
            if "describe-images" in command:
                return json.dumps({"imageDetails": [{"imageDigest": self.digest}]})
            self.fail("unexpected broker command")

        def read(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "HEAD"]:
                return self.revision
            if command[:2] == ["crane", "digest"]:
                return self.digest
            self.fail("unexpected command")

        def preflight(*arguments):
            self.assertEqual(arguments, (self.uri, self.digest, "api"))
            calls.append("finalizer")

        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "aws-e/adp", "GITHUB_REF": "refs/heads/main",
                                            "GITHUB_EVENT_NAME": "workflow_dispatch"}), \
                mock.patch("subprocess.check_output", side_effect=read), \
                mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                mock.patch.object(self.publisher, "broker", side_effect=broker), \
                mock.patch("subprocess.run") as push:
            receipt = self.publisher.publish(self.args, preflight)
        self.assertEqual(receipt["image_digest"], self.digest)
        self.assertEqual(receipt["sbom_sha256"], self.sbom_digest)
        self.assertEqual(calls[0][2], "get-caller-identity")
        self.assertEqual(calls[1], "finalizer")
        self.assertEqual(push.call_count, 2)
        self.assertEqual(push.call_args_list[1].args[0][:2], ["crane", "push"])
        self.assertEqual(calls[-2][2], "describe-images")
        self.assertEqual(calls[-1], "finalizer")


if __name__ == "__main__":
    unittest.main()
