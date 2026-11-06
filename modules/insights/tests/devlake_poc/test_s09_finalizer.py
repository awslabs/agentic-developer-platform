import datetime
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/insights/poc/devlake/finalize.py"


class FinalizerFixture(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_finalize", MODULE)
        self.finalize = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.finalize)
        self.revision = "a" * 40
        self.digest = "sha256:" + "b" * 64
        self.account = "123456789012"
        self.uri = f"{self.account}.dkr.ecr.us-east-1.amazonaws.com/devlake-poc/api"
        self.pilot = "devlake-" + self.revision
        self.record = {
            "pilot_id": self.pilot, "account": self.account, "region": "us-east-1",
            "operator": "example-operator", "finalizer_revision": self.revision,
            "expires_at": (datetime.datetime.now(datetime.timezone.utc) +
                           datetime.timedelta(days=1)).isoformat(),
            "poll_seconds": 3600, "deadline_seconds": 7200,
            "artifacts": {"api": {"repository_uri": self.uri, "digest": self.digest,
                                  "tag": self.revision + "-api", "state": "registered"}},
            "probes": {},
        }
        self.calls = []
        self.role = "finalizer-role"
        self.encrypted = True
        self.recovery = True
        self.simulation = True
        self.wrong_resource = False
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        variables = {
            "ADP_DEVLAKE_PILOT_ID": self.pilot, "ADP_DEVLAKE_LEDGER_TABLE": "protected-ledger",
            "ADP_DEVLAKE_ACCOUNT": self.account, "ADP_DEVLAKE_REGION": "us-east-1",
            "ADP_DEVLAKE_FINALIZE_ROLE": "finalizer-role", "ADP_DEVLAKE_PUBLISH_ROLE": "publisher-role",
            "ADP_DEVLAKE_CONNECTION_LABEL": "fixture", "GITHUB_REPOSITORY": "aws-e/adp",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": self.revision, "ADP_DEVLAKE_FINALIZER_REVISION": self.revision,
            "GITHUB_ACTOR": "example-operator",
        }
        environment = mock.patch.dict(os.environ, variables)
        environment.start()
        self.addCleanup(environment.stop)
        for name, value in (("aws", self.aws), ("revision", lambda: self.revision)):
            patched = mock.patch.object(self.finalize, name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def aws(self, label, service, operation, *arguments):
        self.calls.append((service, operation, arguments))
        if service == "sts":
            return {"Account": self.account,
                    "Arn": f"arn:aws:sts::{self.account}:assumed-role/{self.role}/fixture"}
        if service == "dynamodb":
            if operation == "describe-table":
                return {"Table": {
                    "TableArn": f"arn:aws:dynamodb:us-east-1:{self.account}:table/protected-ledger",
                    "TableStatus": "ACTIVE", "KeySchema": [{"AttributeName": "pilot_id", "KeyType": "HASH"}],
                    "SSEDescription": {"Status": "ENABLED" if self.encrypted else "DISABLED"},
                }}
            if operation == "describe-continuous-backups":
                return {"ContinuousBackupsDescription": {"PointInTimeRecoveryDescription": {
                    "PointInTimeRecoveryStatus": "ENABLED" if self.recovery else "DISABLED"}}}
            if operation == "get-item":
                return {"Item": {"pilot_id": {"S": self.pilot},
                                 "record": {"S": json.dumps(self.record, sort_keys=True)}}}
            if operation == "update-item":
                values = json.loads(arguments[arguments.index("--expression-attribute-values") + 1])
                if json.dumps(self.record, sort_keys=True) != values[":previous"]["S"]:
                    raise ValueError("concurrent ledger change")
                self.record = json.loads(values[":next"]["S"])
                return {}
            if operation == "put-item":
                if self.record is not None:
                    raise ValueError("pilot already registered")
                item = json.loads(arguments[arguments.index("--item") + 1])
                self.record = json.loads(item["record"]["S"])
                return {}
        if service == "ecr" and operation == "batch-get-image":
            tag = self.revision + "-api"
            return {"images": [], "failures": [{"imageId": {"imageTag": tag},
                                                "failureCode": "ImageNotFound"}]}
        if service == "ecr" and operation == "list-images":
            return {"imageIds": []}
        if service == "ecr":
            return {"repositories": [{"repositoryUri": self.uri,
                                      "registryId": self.account, "imageTagMutability": "IMMUTABLE"}]}
        if service == "iam":
            return {"EvaluationResults": [
                {"EvalActionName": action, "EvalDecision": "allowed" if self.simulation else "denied",
                 "EvalResourceName": f"arn:aws:ecr:us-east-1:{self.account}:repository/devlake-poc/"
                                     + ("worker" if self.wrong_resource else "api")}
                for action in ("ecr:BatchDeleteImage", "ecr:BatchGetImage", "ecr:ListImages",
                               "ecr:DescribeImages")
            ]}
        raise AssertionError((label, service, operation))

    def run_probes(self):
        self.finalize.probe("manual")
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
            self.finalize.probe("scheduled")


class ProtectedFinalizerTest(FinalizerFixture):
    def test_register_requires_operator_intent_and_cannot_replace_it(self):
        path = Path(self.temporary.name) / "private-registration.json"
        path.write_text(json.dumps(self.record))
        self.record = None
        self.finalize.register(path)
        self.assertEqual(self.record["artifacts"]["api"]["digest"], self.digest)
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.finalize.register(path)
        self.record = None
        forged = json.loads(path.read_text())
        forged["probes"]["manual"] = {"at": "2000-01-01T00:00:00Z"}
        path.write_text(json.dumps(forged))
        with self.assertRaisesRegex(ValueError, "may not assert probes"):
            self.finalize.register(path)
        self.assertIsNone(self.record)

    def test_registration_refuses_existing_accepted_tag_or_second_pilot(self):
        path = Path(self.temporary.name) / "private-registration.json"
        path.write_text(json.dumps(self.record))
        self.record = None
        original = self.aws

        def accepted(label, service, operation, *arguments):
            if service == "ecr" and operation == "batch-get-image":
                return {"images": [{"imageId": {"imageTag": self.revision + "-api",
                                                 "imageDigest": self.digest}}], "failures": []}
            return original(label, service, operation, *arguments)

        with mock.patch.object(self.finalize, "aws", side_effect=accepted):
            with self.assertRaisesRegex(ValueError, "already exists"):
                self.finalize.register(path)
        self.assertIsNone(self.record)
        forged = json.loads(path.read_text())
        forged["pilot_id"] = "different-pilot"
        path.write_text(json.dumps(forged))
        with self.assertRaisesRegex(ValueError, "registered pilot"):
            self.finalize.register(path)
        self.assertIsNone(self.record)

    def test_probes_require_exact_role_event_and_cleanup_policy(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
            with self.assertRaisesRegex(ValueError, "mode or revision"):
                self.finalize.probe("manual")
        self.assertEqual(self.calls, [])
        with mock.patch.dict(os.environ, {"ADP_DEVLAKE_FINALIZER_REVISION": "0" * 40}):
            with self.assertRaisesRegex(ValueError, "mode or revision"):
                self.finalize.probe("manual")
        self.assertEqual(self.calls, [])
        self.role = "publisher-role"
        with self.assertRaisesRegex(ValueError, "exact finalizer"):
            self.finalize.probe("manual")
        self.role = "finalizer-role"
        self.simulation = False
        with self.assertRaisesRegex(ValueError, "cleanup authority"):
            self.finalize.probe("manual")
        self.assertEqual(self.record["probes"], {})
        self.simulation = True
        self.wrong_resource = True
        with self.assertRaisesRegex(ValueError, "cleanup authority"):
            self.finalize.probe("manual")
        self.wrong_resource = False
        with mock.patch.dict(os.environ, {"GITHUB_SHA": "0" * 40}):
            self.run_probes()
        self.assertEqual(set(self.record["probes"]), {"scheduled", "manual"})
        self.assertEqual(len([call for call in self.calls if call[1] == "update-item"]), 2)
        self.assertTrue(any("#record = :previous" in call[2] for call in self.calls if call[1] == "update-item"))

    def test_preflight_rejects_missing_or_stale_probes_and_wrong_target(self):
        self.role = "publisher-role"
        with self.assertRaisesRegex(ValueError, "scheduled and manual"):
            self.finalize.preflight(self.uri, self.digest, "api")
        self.role = "finalizer-role"
        self.run_probes()
        self.role = "publisher-role"
        with mock.patch.dict(os.environ, {"GITHUB_ACTOR": "other-operator"}):
            with self.assertRaisesRegex(ValueError, "registered operator"):
                self.finalize.preflight(self.uri, self.digest, "api")
        self.finalize.preflight(self.uri, self.digest, "api")
        for uri, digest in ((self.uri.replace("devlake-poc/api", "devlake-poc/worker"), self.digest),
                            (self.uri, "sha256:" + "c" * 64)):
            with self.assertRaisesRegex(ValueError, "destination, digest"):
                self.finalize.preflight(uri, digest, "api")
        self.record["probes"]["scheduled"]["at"] = "2000-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "scheduled and manual"):
            self.finalize.preflight(self.uri, self.digest, "api")
        self.record["artifacts"]["api"]["state"] = "held"
        self.record["artifacts"]["api"]["hold"] = {
            "operator": "example-operator", "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        with self.assertRaisesRegex(ValueError, "active intent"):
            self.finalize.preflight(self.uri, self.digest, "api")

    def test_unprotected_ledger_or_expired_pilot_refuses_before_resources(self):
        self.role = "publisher-role"
        for field in ("encrypted", "recovery"):
            setattr(self, field, False)
            with self.assertRaises(ValueError):
                self.finalize.preflight(self.uri, self.digest, "api")
            setattr(self, field, True)
        self.record["expires_at"] = "2000-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "bounded expiry"):
            self.finalize.preflight(self.uri, self.digest, "api")
        self.assertFalse(any(call[0] == "ecr" for call in self.calls))

    def test_workflow_is_optional_and_both_triggers_call_finalizer(self):
        workflow = (ROOT / ".github/workflows/insights-poc-finalize.yml").read_text()
        self.assertIn("schedule:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("--mode scheduled", workflow)
        self.assertIn("--mode manual", workflow)
        self.assertIn("environment: adp-devlake-finalizer", workflow)
        publish = (ROOT / ".github/workflows/devlake-poc.yml").read_text()
        self.assertIn("ADP_DEVLAKE_LEDGER_TABLE:", publish)
        self.assertIn("secrets.ADP_DEVLAKE_CONNECTION_LABEL", publish)
        self.assertNotIn("insights-poc-finalize.yml", publish)
