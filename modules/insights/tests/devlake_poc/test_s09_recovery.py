import hashlib
import importlib.util
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


class RecoveryTest(unittest.TestCase):
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
        self.accepted_digest = "sha256:" + "b" * 64
        self.schema_digest = "c" * 64
        self.account = "123456789012"
        self.repository = "devlake-poc/api"
        self.uri = f"{self.account}.dkr.ecr.us-east-1.amazonaws.com/{self.repository}"
        self.tag = self.revision + "-api"
        self.accepted_tag = "previous-accepted-api"
        self.images = {self.accepted_tag: self.accepted_digest}
        self.calls = []
        self.pushes = 0
        self.interrupt_push = False
        self.sbom = Path(self.temporary.name) / "sbom.json"
        self.sbom.write_text('{"bomFormat":"CycloneDX","components":[{"name":"fixture"}]}')

    def checks(self, number, failed=False):
        evidence = {"revision": self.revision, "component": "api", "image_digest": self.digest,
                    "schema_sha256": self.schema_digest,
                    "sbom_sha256": hashlib.sha256(self.sbom.read_bytes()).hexdigest(),
                    "checks": {name: "passed" for name in self.publisher.REQUIRED_CHECKS}}
        if failed:
            evidence["checks"]["secrets"] = "failed"
        path = Path(self.temporary.name) / f"checks-{number}.json"
        path.write_text(json.dumps(evidence))
        return path

    def broker(self, label, *command):
        self.assertEqual(label, "fixture")
        operation = command[2]
        self.calls.append(operation)
        if operation == "get-caller-identity":
            return json.dumps({"Account": self.account,
                               "Arn": f"arn:aws:sts::{self.account}:assumed-role/devlake-publisher/session"})
        if operation == "describe-repositories":
            return json.dumps({"repositories": [{"repositoryUri": self.uri, "registryId": self.account,
                                                   "imageTagMutability": "IMMUTABLE"}]})
        if operation == "batch-get-image":
            self.assertIn("imageTag=" + self.tag, command)
            if self.tag not in self.images:
                return json.dumps({"images": [], "failures": [{"imageId": {"imageTag": self.tag},
                                                                 "failureCode": "ImageNotFound"}]})
            return json.dumps({"images": [{"registryId": self.account, "repositoryName": self.repository,
                                           "imageId": {"imageTag": self.tag,
                                                       "imageDigest": self.images[self.tag]}}], "failures": []})
        if operation == "get-login-password":
            return "fixture-password"
        if operation == "describe-images":
            return json.dumps({"imageDetails": [{"imageDigest": self.images[self.tag]}]})
        self.fail("unexpected broker operation")

    def read(self, command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return self.revision
        if command[:2] == ["crane", "digest"]:
            return self.images[self.tag]
        self.fail("unexpected read operation")

    def upload(self, command, **kwargs):
        if command[:2] == ["crane", "auth"]:
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["crane", "push"]:
            self.pushes += 1
            self.images[self.tag] = self.digest
            if self.interrupt_push:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)
        self.fail("unexpected registry command")

    def preflight(self, *scope):
        self.assertEqual(scope, (self.uri, self.digest, "api"))
        self.calls.append("finalizer")

    def invoke(self, checks, receipt):
        arguments = ["publish.py", "--publish", "--component", "api", "--layout",
                     str(Path(self.temporary.name) / "image"), "--expected-digest", self.digest,
                     "--revision", self.revision, "--checks", str(checks), "--account", self.account,
                     "--role", "devlake-publisher", "--region", "us-east-1",
                     "--repository", self.repository, "--repository-uri", self.uri,
                     "--connection-label", "fixture", "--receipt", str(receipt)]
        with mock.patch.object(sys, "argv", arguments), mock.patch.dict(sys.modules, {
                "finalize": SimpleNamespace(preflight=self.preflight)}):
            self.publisher.main()

    def assert_no_receipt(self, receipt):
        self.assertFalse(receipt.exists())
        self.assertFalse(receipt.with_suffix(".pending").exists())

    def test_interrupted_upload_requires_fresh_checks_and_preserves_accepted_image(self):
        original_check = self.publisher.check_evidence
        checked = []

        def check(*arguments):
            checked.append(arguments[0])
            return original_check(*arguments)

        with mock.patch("subprocess.check_output", side_effect=self.read), \
                mock.patch("subprocess.run", side_effect=self.upload), \
                mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest) as inspect, \
                mock.patch.object(self.publisher, "check_evidence", side_effect=check), \
                mock.patch.object(self.publisher, "broker", side_effect=self.broker):
            self.interrupt_push = True
            interrupted = Path(self.temporary.name) / "interrupted.json"
            with self.assertRaisesRegex(ValueError, "no publication receipt"):
                self.invoke(self.checks(1), interrupted)
            self.assert_no_receipt(interrupted)
            self.assertEqual(self.images[self.accepted_tag], self.accepted_digest)
            self.assertEqual(self.images[self.tag], self.digest)

            self.interrupt_push = False
            failed_check = Path(self.temporary.name) / "failed-check.json"
            before = self.calls.count("batch-get-image")
            with self.assertRaisesRegex(ValueError, "checks are missing or failed"):
                self.invoke(self.checks(2, failed=True), failed_check)
            self.assert_no_receipt(failed_check)
            self.assertEqual(self.calls.count("batch-get-image"), before)

            corrected = Path(self.temporary.name) / "corrected.json"
            self.invoke(self.checks(3), corrected)
            self.assertEqual(self.pushes, 1)
            self.assertEqual(self.images[self.accepted_tag], self.accepted_digest)
            self.assertEqual(json.loads(corrected.read_text())["image_digest"], self.digest)
            self.assertEqual(len(checked), 3)
            self.assertEqual(inspect.call_count, 3)
            self.assertEqual(self.calls.count("finalizer"), 3)
            self.assertEqual(self.calls.count("describe-images"), 1)

    def test_conflicting_immutable_tag_is_not_overwritten_or_receipted(self):
        self.images[self.tag] = "sha256:" + "d" * 64
        receipt = Path(self.temporary.name) / "conflict.json"
        with mock.patch("subprocess.check_output", side_effect=self.read), \
                mock.patch("subprocess.run", side_effect=self.upload), \
                mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                mock.patch.object(self.publisher, "broker", side_effect=self.broker):
            with self.assertRaisesRegex(ValueError, "immutable ECR tag belongs to a different digest"):
                self.invoke(self.checks(1), receipt)
        self.assert_no_receipt(receipt)
        self.assertEqual(self.pushes, 0)
        self.assertEqual(self.images[self.tag], "sha256:" + "d" * 64)
        self.assertEqual(self.images[self.accepted_tag], self.accepted_digest)

    def test_denied_or_wrong_tag_lookup_never_pushes(self):
        original = self.broker
        receipt = Path(self.temporary.name) / "denied.json"

        for failure in ("AccessDenied", "ImageNotFound"):
            with self.subTest(failure=failure):
                def broker(label, *command):
                    if command[2] == "batch-get-image":
                        return json.dumps({"images": [], "failures": [{
                            "failureCode": failure, "imageId": {"imageTag": "wrong-tag"}}]})
                    return original(label, *command)

                with mock.patch("subprocess.check_output", side_effect=self.read), \
                        mock.patch("subprocess.run", side_effect=self.upload), \
                        mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                        mock.patch.object(self.publisher, "broker", side_effect=broker):
                    with self.assertRaisesRegex(ValueError, "ECR tag lookup failed"):
                        self.invoke(self.checks(failure), receipt)
                self.assert_no_receipt(receipt)
                self.assertEqual(self.pushes, 0)
                self.assertEqual(self.images[self.accepted_tag], self.accepted_digest)

    def test_terminal_request_during_upload_refuses_new_receipt(self):
        calls = []

        def terminal_preflight(*scope):
            calls.append(scope)
            if len(calls) == 2:
                raise ValueError("terminal pilot cannot authorize publication")

        receipt = Path(self.temporary.name) / "terminal.json"
        with mock.patch("subprocess.check_output", side_effect=self.read), \
                mock.patch("subprocess.run", side_effect=self.upload), \
                mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                mock.patch.object(self.publisher, "broker", side_effect=self.broker), \
                mock.patch.object(self, "preflight", side_effect=terminal_preflight):
            with self.assertRaisesRegex(ValueError, "terminal pilot"):
                self.invoke(self.checks("terminal"), receipt)
        self.assertEqual(len(calls), 2)
        self.assert_no_receipt(receipt)
        self.assertEqual(self.images[self.accepted_tag], self.accepted_digest)

    def test_failed_required_check_never_reaches_publication(self):
        spec = importlib.util.spec_from_file_location(
            "devlake_checks", ROOT / "modules/insights/poc/devlake/scripts/checks.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        evidence = Path(self.temporary.name) / "attempt" / "checks.json"
        receipt = Path(self.temporary.name) / "failed-pipeline.json"
        arguments = ["checks.py", "--layout", str(Path(self.temporary.name) / "image"),
                     "--component", "api", "--expected-digest", self.digest,
                     "--revision", self.revision, "--account", self.account,
                     "--region", "us-east-1", "--repository-uri", self.uri,
                     "--output", str(evidence)]

        def read_check(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "HEAD"]:
                return self.revision
            if command == ["syft", "version", "-o", "json"]:
                return json.dumps({"version": checker.SYFT_VERSION})
            if command[:2] == ["syft", "scan"]:
                return json.dumps({"bomFormat": "CycloneDX", "components": [{"name": "fixture"}]})
            if command == ["gitleaks", "version"]:
                return checker.GITLEAKS_VERSION
            self.fail("unexpected check command")

        for failed_command in ("go", "gitleaks"):
            with self.subTest(check=failed_command):
                executed = []

                def run(*command, **kwargs):
                    executed.append(command[0])
                    if command[0] == failed_command:
                        raise subprocess.CalledProcessError(1, command)

                with mock.patch.object(sys, "argv", arguments), \
                        mock.patch.dict(sys.modules, {"finalize": SimpleNamespace(preflight=self.preflight)}), \
                        mock.patch("subprocess.check_output", side_effect=read_check), \
                        mock.patch.object(checker, "run", side_effect=run), \
                        mock.patch.object(checker.publish, "inspect", return_value=self.schema_digest), \
                        mock.patch.object(checker, "scan_layers"):
                    with self.assertRaises(subprocess.CalledProcessError):
                        checker.main()
                self.assertIn(failed_command, executed)
                self.assertFalse(evidence.exists())
                self.assertFalse(evidence.with_name("sbom.json").exists())
                self.assertEqual(self.calls.count("finalizer"), 0)

                with mock.patch("subprocess.check_output", side_effect=self.read), \
                        mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                        mock.patch.object(self.publisher, "broker") as broker, \
                        mock.patch("subprocess.run") as push:
                    with self.assertRaises(FileNotFoundError):
                        self.invoke(evidence, receipt)
                    broker.assert_not_called()
                    push.assert_not_called()
                self.assert_no_receipt(receipt)
                self.assertEqual(self.images[self.accepted_tag], self.accepted_digest)

        with mock.patch("subprocess.check_output", side_effect=self.read), \
                mock.patch.object(self.publisher, "inspect", return_value=self.schema_digest), \
                mock.patch.object(self.publisher, "broker") as broker, \
                mock.patch("subprocess.run") as push:
            with self.assertRaisesRegex(ValueError, "checks are missing or failed"):
                self.invoke(self.checks("tampered", failed=True), receipt)
            broker.assert_not_called()
            push.assert_not_called()
        self.assert_no_receipt(receipt)
        self.assertEqual(self.images[self.accepted_tag], self.accepted_digest)


if __name__ == "__main__":
    unittest.main()
