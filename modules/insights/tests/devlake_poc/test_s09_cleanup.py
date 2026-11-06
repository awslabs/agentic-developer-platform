import datetime
import importlib
import os
import unittest
from unittest import mock

from modules.insights.tests.devlake_poc.test_s09_finalizer import FinalizerFixture


class CleanupFixture(FinalizerFixture):
    def setUp(self):
        super().setUp()
        self.artifacts = importlib.import_module("finalize_artifacts")
        replacement = mock.patch.object(self.artifacts, "finalize", self.finalize)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.images = {"api": {self.revision + "-api": self.digest,
                               "previous-good": "sha256:" + "d" * 64}, "worker": {}}
        self.deletions = []
        self.interrupted = 0
        self.denied = 0
        self.wrong_inventory = False
        self.actor = mock.patch.dict(os.environ, {"GITHUB_ACTOR": "example-operator"})
        self.actor.start()
        self.addCleanup(self.actor.stop)

    def aws(self, label, service, operation, *arguments):
        if service != "ecr" or operation == "describe-repositories":
            return super().aws(label, service, operation, *arguments)
        self.calls.append((service, operation, arguments))
        component = arguments[arguments.index("--repository-name") + 1].split("/")[-1]
        tag = self.revision + "-" + component
        images = self.images[component]
        if operation == "batch-get-image":
            if tag not in images:
                return {"images": [], "failures": [{"imageId": {"imageTag": tag},
                                                    "failureCode": "ImageNotFound"}]}
            return {"images": [{"registryId": self.account, "repositoryName": "devlake-poc/" + component,
                                "imageId": {"imageTag": tag, "imageDigest": images[tag]}}], "failures": []}
        if operation == "list-images":
            inventory = [{"imageTag": name, "imageDigest": digest} for name, digest in images.items()]
            if self.wrong_inventory and tag in images:
                inventory = [{"imageTag": name, "imageDigest": "sha256:" + "e" * 64 if name == tag else digest}
                             for name, digest in images.items()]
            return {"imageIds": inventory}
        if operation == "batch-delete-image":
            self.deletions.append((component, tag))
            if self.denied:
                self.denied -= 1
                raise ValueError("cleanup authority unavailable")
            digest = images.pop(tag)
            if self.interrupted:
                self.interrupted -= 1
                raise ValueError("upload or deletion response lost")
            return {"imageIds": [{"imageDigest": digest, "imageTag": tag}], "failures": []}
        raise AssertionError(operation)

    def add_worker(self):
        self.record["artifacts"]["worker"] = {
            "repository_uri": f"{self.account}.dkr.ecr.us-east-1.amazonaws.com/devlake-poc/worker",
            "digest": "sha256:" + "c" * 64,
            "tag": self.revision + "-worker", "state": "registered",
        }
        self.images["worker"][self.revision + "-worker"] = "sha256:" + "c" * 64


class ToolCleanupTest(CleanupFixture):
    def test_partial_tool_cleanup_precedes_image_retirement_and_resumes(self):
        import tool_sources

        namespace = "s14-" + self.pilot
        resources = [{"kind": "Deployment", "name": "s14-" + tool, "namespace": namespace}
                     for tool in tool_sources.TOOLS]
        resources.append({"kind": "Namespace", "name": namespace, "namespace": ""})
        config = {"pilot_id": self.pilot, "revision": self.revision,
                  "expires_at": self.record["expires_at"], "operator": self.record["operator"],
                  "account": self.account, "region": "us-east-1", "namespace": namespace,
                  "cluster_ref": "sha256:" + "f" * 64, "context": "selected-fixture",
                  "images": {tool: "registry.invalid/" + tool + "@sha256:" + character * 64
                             for tool, character in zip(tool_sources.TOOLS, "abc")},
                  "resources": resources, "bindings": {provider: None for provider in tool_sources.TOOLS + tool_sources.EXTERNAL}}
        capacity = {"cluster_ref": "sha256:" + "f" * 64, "context": "selected-fixture", "free_cpu": 2,
                    "free_gib": 8, "free_disk_gib": 20,
                    "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        intent = tool_sources.plan(config, self.record, capacity)
        self.record["tool_sources"] = intent
        objects = {}
        for resource in resources:
            metadata = {"name": resource["name"], "labels": {"adp.dev/pilot": self.pilot,
                        "adp.dev/revision": self.revision}}
            if resource["namespace"]:
                metadata["namespace"] = resource["namespace"]
            observed = {"kind": resource["kind"], "metadata": metadata}
            if resource["kind"] == "Deployment":
                observed["spec"] = {"template": {"spec": {"containers": [
                    {"image": intent["images"][resource["name"].removeprefix("s14-")]}]}}}
            objects[tool_sources.identity(intent, resource)] = observed
        outsider = [True]
        class Client:
            def inspect(self, resource):
                return objects.get(tool_sources.identity(intent, resource))
            def delete(self, resource):
                objects.pop(tool_sources.identity(intent, resource))
            def empty(self, name):
                if outsider:
                    raise ValueError("namespace contains unrelated resource")
        with mock.patch.object(tool_sources, "finalize", self.finalize), \
             mock.patch.object(tool_sources, "Kubernetes", return_value=Client()):
            with self.assertRaisesRegex(ValueError, "unrelated"):
                self.artifacts.run("manual", "abort")
            self.assertEqual(len(self.record["tool_sources"]["receipts"]), 3)
            self.assertEqual(self.deletions, [])
            self.assertNotIn("finalization_complete", self.record)
            outsider.clear()
            self.artifacts.run("manual", "abort")
        self.assertEqual(len(self.record["tool_sources"]["receipts"]), 4)
        self.assertEqual(self.deletions, [("api", self.revision + "-api")])
        self.assertIn("finalization_complete", self.record)
        self.assertEqual(self.images["api"], {"previous-good": "sha256:" + "d" * 64})
        with mock.patch.object(tool_sources, "Kubernetes", side_effect=AssertionError("duplicate cleanup")):
            self.artifacts.run("manual", "abort")
        self.assertEqual(self.deletions, [("api", self.revision + "-api")])


class ArtifactRecoveryTest(CleanupFixture):
    def test_optional_intent_fences_abort_before_image_cleanup(self):
        from intent_ledger import connectors

        self.record["optional_resources"] = {"connectors": connectors({
            "environment": "dev", "aws_account_id": self.account, "aws_region": "us-east-1"},
            self.uri + "@" + self.digest)}
        for attempt in range(2):
            with self.assertRaisesRegex(ValueError, "optional resource cleanup requires"):
                self.artifacts.run("manual", "abort" if attempt == 0 else "probe")
        self.assertEqual(self.record["finalization_requested"]["reason"], "abort")
        self.assertEqual(self.deletions, [])
        self.assertNotIn("receipt", self.record["artifacts"]["api"])
        self.assertNotIn("finalization_complete", self.record)

    def test_optional_intent_fences_scheduled_expiry_before_image_cleanup(self):
        from intent_ledger import connectors

        self.record["optional_resources"] = {"connectors": connectors({
            "environment": "dev", "aws_account_id": self.account, "aws_region": "us-east-1"},
            self.uri + "@" + self.digest)}
        self.record["expires_at"] = (datetime.datetime.now(datetime.timezone.utc) -
                                      datetime.timedelta(seconds=30)).isoformat()
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_ACTOR": "scheduler"}):
            with self.assertRaisesRegex(ValueError, "optional resource cleanup requires"):
                self.artifacts.run("scheduled")
        self.assertEqual(self.record["finalization_requested"]["reason"], "expiry")
        self.assertEqual(self.deletions, [])
        self.assertNotIn("finalization_complete", self.record)

    def test_abort_deletes_only_owned_tag_and_keeps_approved_hold(self):
        self.add_worker()
        self.artifacts.run("manual", "hold", "worker")
        self.assertEqual(self.record["artifacts"]["worker"]["hold"]["operator"], "example-operator")
        self.artifacts.run("manual", "abort")
        self.assertEqual(self.record["finalization_requested"]["reason"], "abort")
        self.assertEqual(self.deletions, [("api", self.revision + "-api")])
        self.assertEqual(self.images["api"], {"previous-good": "sha256:" + "d" * 64})
        self.assertIn(self.revision + "-worker", self.images["worker"])
        self.assertEqual(self.record["artifacts"]["api"]["receipt"]["outcome"], "absent")
        self.assertEqual(self.record["artifacts"]["worker"]["receipt"]["outcome"], "retained")
        self.assertIn("held artifacts", self.record["finalization_complete"]["remaining_cost"])
        self.role = "publisher-role"
        with self.assertRaisesRegex(ValueError, "terminal pilot"):
            self.finalize.preflight(self.uri, self.digest, "api")

    def test_failure_finalization_stays_reachable_without_campaign_observation_batch(self):
        from pathlib import Path

        self.assertFalse((Path(self.temporary.name) / "window-01.json").exists())
        self.artifacts.run("manual", "failure")
        self.assertEqual(self.record["finalization_requested"]["reason"], "failure")
        self.assertEqual(self.record["artifacts"]["api"]["receipt"]["outcome"], "absent")
        self.assertIn("finalization_complete", self.record)
        self.assertEqual(self.deletions, [("api", self.revision + "-api")])
        self.assertEqual(self.images["api"], {"previous-good": "sha256:" + "d" * 64})
        self.artifacts.run("manual", "failure")
        self.assertEqual(self.deletions, [("api", self.revision + "-api")])

    def test_missing_held_image_and_forged_receipt_remain_incomplete(self):
        self.add_worker()
        self.artifacts.run("manual", "hold", "worker")
        self.images["worker"].clear()
        with self.assertRaisesRegex(ValueError, "held image is missing"):
            self.artifacts.run("manual", "abort")
        self.assertNotIn("receipt", self.record["artifacts"]["worker"])
        self.assertNotIn("finalization_complete", self.record)
        self.assertEqual(self.record["artifacts"]["worker"]["state"], "held")
        self.record["artifacts"]["api"]["receipt"]["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "cleanup receipt differs"):
            self.finalize.get_registered("finalizer", allow_expired=True)

    def test_scheduled_expiry_reconciles_without_operator_action(self):
        self.record["expires_at"] = (datetime.datetime.now(datetime.timezone.utc) -
                                      datetime.timedelta(seconds=30)).isoformat()
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_ACTOR": "scheduler"}):
            self.artifacts.run("scheduled")
        self.assertEqual(self.record["finalization_requested"]["reason"], "expiry")
        self.assertEqual(self.record["artifacts"]["api"]["receipt"]["outcome"], "absent")
        self.assertIn("finalization_complete", self.record)
        self.assertEqual(self.images["api"]["previous-good"], "sha256:" + "d" * 64)

    def test_late_scheduler_cleanup_escalates_and_operator_acknowledges(self):
        self.record["expires_at"] = "2000-01-01T00:00:00Z"
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_ACTOR": "scheduler"}):
            with self.assertRaisesRegex(ValueError, "scheduler missed"):
                self.artifacts.run("scheduled")
        self.assertIn("finalization_complete", self.record)
        self.assertGreater(self.record["finalization_requested"]["scheduler_delay_seconds"], 3600)
        self.assertEqual(self.record["artifacts"]["api"]["receipt"]["outcome"], "absent")
        self.artifacts.run("manual")
        self.assertIn("operator_acknowledged_at", self.record["finalization_requested"])
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_ACTOR": "scheduler"}):
            self.artifacts.run("scheduled")
        self.assertEqual(len(self.deletions), 1)

    def test_resume_after_deadline_requires_registered_operator(self):
        self.record["finalization_requested"] = {"reason": "failure", "at": "2000-01-01T00:00:00Z"}
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_ACTOR": "scheduler"}):
            with self.assertRaisesRegex(ValueError, "registered operator must resume manually"):
                self.artifacts.run("scheduled")
        self.artifacts.run("manual")
        self.assertEqual(self.record["artifacts"]["api"]["receipt"]["outcome"], "absent")

    def test_lost_delete_response_retries_exact_intent_without_second_delete(self):
        self.interrupted = 1
        with self.assertRaisesRegex(ValueError, "response lost"):
            self.artifacts.run("manual", "failure")
        artifact = self.record["artifacts"]["api"]
        self.assertNotIn("receipt", artifact)
        self.assertNotIn("finalization_complete", self.record)
        self.assertEqual(artifact["cleanup"]["attempts"], 1)
        self.assertIsNone(artifact["cleanup"]["lease"])
        self.artifacts.run("manual")
        self.assertEqual(self.deletions, [("api", self.revision + "-api")])
        self.assertEqual(artifact["cleanup"]["attempts"], 1)
        self.assertEqual(self.record["artifacts"]["api"]["cleanup"]["attempts"], 2)
        self.assertEqual(self.record["artifacts"]["api"]["receipt"]["outcome"], "absent")
        self.assertEqual(self.images["api"]["previous-good"], "sha256:" + "d" * 64)

    def test_wrong_digest_and_failed_cleanup_stop_at_bounded_retry(self):
        self.images["api"][self.revision + "-api"] = "sha256:" + "f" * 64
        for attempt in range(3):
            with self.assertRaisesRegex(ValueError, "not the registered artifact"):
                self.artifacts.run("manual", "abort" if attempt == 0 else "probe")
        before = len(self.calls)
        with self.assertRaisesRegex(ValueError, "retry limit reached"):
            self.artifacts.run("manual")
        self.assertEqual(len([call for call in self.calls[before:] if call[0] == "ecr"]), 0)
        self.assertEqual(self.record["artifacts"]["api"]["cleanup"]["attempts"], 3)
        self.assertNotIn("receipt", self.record["artifacts"]["api"])
        self.assertNotIn("finalization_complete", self.record)
        self.assertEqual(self.deletions, [])

    def test_lease_and_readback_failure_never_claim_cleanup(self):
        self.record["finalization_requested"] = {"reason": "abort", "at": "2026-01-01T00:00:00Z"}
        self.record["artifacts"]["api"]["cleanup"] = {
            "attempts": 1, "lease": {"id": "other", "expires_at": "2099-01-01T00:00:00Z"}}
        with self.assertRaisesRegex(ValueError, "another finalizer"):
            self.artifacts.run("manual")
        self.assertEqual(self.deletions, [])
        self.record["artifacts"]["api"]["cleanup"]["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
        self.wrong_inventory = True
        with self.assertRaisesRegex(ValueError, "independent ECR image readback"):
            self.artifacts.run("manual")
        self.assertEqual(self.deletions, [])
        self.assertNotIn("receipt", self.record["artifacts"]["api"])

    def test_only_registered_operator_can_request_failure_or_hold(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTOR": "other-operator"}):
            for action in ("failure", "hold"):
                with self.assertRaisesRegex(ValueError, "registered operator"):
                    self.artifacts.run("manual", action, "api" if action == "hold" else None)
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
            with self.assertRaisesRegex(ValueError, "scheduler can only"):
                self.artifacts.run("scheduled", "abort")
        self.assertNotIn("finalization_requested", self.record)
        self.assertEqual(self.deletions, [])

    def test_schedule_before_expiry_uses_probe_without_deleting(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_ACTOR": "scheduler"}):
            self.artifacts.run("scheduled")
        self.assertIn("scheduled", self.record["probes"])
        self.assertNotIn("finalization_requested", self.record)
        self.assertEqual(self.deletions, [])
        self.record["expires_at"] = (datetime.datetime.now(datetime.timezone.utc) +
                                      datetime.timedelta(minutes=30)).isoformat()
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
            self.artifacts.run("scheduled")
        self.assertNotIn("finalization_requested", self.record)
