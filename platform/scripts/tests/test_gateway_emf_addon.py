"""Offline retention and refusal checks for the CloudWatch addon routing delta."""

import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("gateway_emf_addon", ROOT / "platform/scripts/gateway_emf_addon.py")
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)


def snapshot(configuration):
    return {"addon": {"addonName": "amazon-cloudwatch-observability", "clusterName": "example-cluster",
                      "addonVersion": "v6.7.0-eksbuild.1", "status": "ACTIVE",
                      "modifiedAt": "2026-10-19T00:00:00Z",
                      "serviceAccountRoleArn": "arn:aws:iam::123456789012:role/example-collector",
                      "configurationValues": json.dumps(configuration)}}


class GatewayEmfAddonTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.receipt = Path(directory.name) / "operation.json"
        self.retained = {
            "agent": {"podAnnotations": {"example.org/retained": "true"}},
            "containerLogs": {"fluentBit": {"podAnnotations": {"example.org/other": "keep"},
                                            "config": {"extraFiles": {"custom.conf": "[FILTER]\n    Name modify\n"}}}},
            "futureSetting": {"enabled": True},
        }

    def test_adoption_and_upgrade_keep_all_retained_values(self):
        baseline = snapshot(self.retained)
        candidate = addon.prepare(baseline, baseline)
        expected = json.loads(json.dumps(self.retained))
        expected["containerLogs"]["fluentBit"]["config"]["extraFiles"]["gateway-emf.conf"] = addon.ROUTING_FILE.read_text()
        self.assertEqual(candidate, expected)
        self.assertEqual(addon.prepare(snapshot(candidate), snapshot(candidate)), expected)
        self.assertEqual(self.retained["containerLogs"]["fluentBit"]["config"]["extraFiles"],
                         {"custom.conf": "[FILTER]\n    Name modify\n"})

    def test_cli_prepares_private_additive_candidate_without_changing_input(self):
        baseline = snapshot(self.retained)
        with tempfile.TemporaryDirectory() as directory:
            before_path = Path(directory) / "before.json"
            baseline_path = Path(directory) / "baseline.json"
            output_path = Path(directory) / "candidate.json"
            before_path.write_text(json.dumps(baseline))
            baseline_path.write_text(json.dumps(baseline))
            with patch("sys.argv", ["gateway_emf_addon.py", "prepare", "--before", str(before_path),
                                    "--baseline", str(baseline_path), "--output", str(output_path)]):
                addon.main()
            self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(output_path.read_text()), addon.prepare(baseline, baseline))
            self.assertEqual(json.loads(before_path.read_text()), baseline)

    def test_live_update_checks_role_and_live_addon_before_supported_update(self):
        baseline = snapshot(self.retained)
        calls = []

        def fake_aws(label, *arguments):
            calls.append((label, arguments))
            if arguments[:2] == ("sts", "get-caller-identity"):
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            if arguments[:2] == ("eks", "describe-addon"):
                return baseline
            self.assertEqual(arguments[:2], ("eks", "update-addon"))
            record = addon.read_record(self.receipt)
            self.assertEqual(record["state"], "intent")
            self.assertEqual(record["result"]["kind"], "intent")
            self.assertEqual(record["target"]["account"], "123456789012")
            self.assertEqual(record["before"]["configurationSha256"], addon.fingerprint(self.retained))
            self.assertEqual(record["candidate"]["configurationSha256"], addon.fingerprint(addon.prepare(baseline, baseline)))
            self.assertEqual(str(uuid.UUID(record["clientRequestToken"])), record["clientRequestToken"])
            self.assertEqual(arguments[arguments.index("--client-request-token") + 1], record["clientRequestToken"])
            self.assertIn(("--resolve-conflicts", "NONE"), list(zip(arguments, arguments[1:])))
            path = Path(arguments[arguments.index("--configuration-values") + 1].removeprefix("file://"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), addon.prepare(baseline, baseline))
            return {"update": {"id": "example-update", "status": "InProgress"}}

        result = addon.update_addon(baseline, False, "approved-label", "123456789012",
                                    "arn:aws:sts::123456789012:assumed-role/example-operator",
                                    "us-east-1", "example-cluster", run=fake_aws, receipt=self.receipt)
        self.assertEqual(result["update"]["status"], "InProgress")
        self.assertEqual([item[1][:2] for item in calls], [("sts", "get-caller-identity"),
                         ("eks", "describe-addon"), ("eks", "describe-addon"), ("eks", "update-addon"),
                         ("eks", "describe-addon")])
        self.assertEqual({item[0] for item in calls}, {"approved-label"})

    def test_unapproved_role_and_concurrent_configuration_never_call_update(self):
        baseline = snapshot(self.retained)
        calls = []

        def wrong_role(label, *arguments):
            calls.append(arguments[:2])
            return {"Account": "123456789012",
                    "Arn": "arn:aws:sts::123456789012:assumed-role/other/test-session"}

        with self.assertRaisesRegex(ValueError, "not the approved account and operator role"):
            addon.update_addon(baseline, False, "approved-label", "123456789012",
                               "arn:aws:sts::123456789012:assumed-role/example-operator",
                               "us-east-1", "example-cluster", run=wrong_role, receipt=self.receipt)
        self.assertEqual(calls, [("sts", "get-caller-identity")])

        calls.clear()
        changed = snapshot({**self.retained, "newerSetting": True})

        def concurrent(label, *arguments):
            calls.append(arguments[:2])
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            self.assertEqual(arguments[:2], ("eks", "describe-addon"))
            return baseline if calls.count(("eks", "describe-addon")) == 1 else changed

        with self.assertRaisesRegex(ValueError, "changed before update"):
            addon.update_addon(baseline, False, "approved-label", "123456789012",
                               "arn:aws:sts::123456789012:assumed-role/example-operator",
                               "us-east-1", "example-cluster", run=concurrent, receipt=self.receipt)
        self.assertEqual(calls, [("sts", "get-caller-identity"), ("eks", "describe-addon"),
                                 ("eks", "describe-addon")])

    def test_lost_update_response_reads_state_without_retry(self):
        baseline = snapshot(self.retained)
        submitted = snapshot(addon.prepare(baseline, baseline))
        calls = []

        def fake_aws(label, *arguments):
            calls.append(arguments[:2])
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            if arguments[:2] == ("eks", "update-addon"):
                raise RuntimeError("Simulated lost acknowledgement")
            self.assertEqual(arguments[:2], ("eks", "describe-addon"))
            return submitted if calls.count(("eks", "describe-addon")) == 3 else baseline

        with self.assertRaisesRegex(RuntimeError, "matching configuration is not proof of success"):
            addon.update_addon(baseline, False, "approved-label", "123456789012",
                               "arn:aws:sts::123456789012:assumed-role/example-operator",
                               "us-east-1", "example-cluster", run=fake_aws, receipt=self.receipt)
        self.assertEqual(calls, [("sts", "get-caller-identity"), ("eks", "describe-addon"),
                                 ("eks", "describe-addon"), ("eks", "update-addon"),
                                 ("eks", "describe-addon")])
        record = json.loads(self.receipt.read_text())
        self.assertEqual(record["state"], "unknown")
        self.assertEqual(record["result"]["kind"], "unknown")
        self.assertTrue(record["readback"]["matchesCandidate"])

    def test_interruption_keeps_stable_intent_and_recovery_never_resubmits(self):
        baseline = snapshot(self.retained)
        calls = []

        def interrupted(label, *arguments):
            calls.append(arguments[:2])
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            if arguments[:2] == ("eks", "update-addon"):
                journal = addon.read_record(self.receipt)
                self.assertEqual(journal["state"], "intent")
                self.assertEqual(arguments[arguments.index("--client-request-token") + 1], journal["clientRequestToken"])
                raise KeyboardInterrupt()
            return baseline

        with self.assertRaises(KeyboardInterrupt):
            addon.update_addon(baseline, False, "approved-label", "123456789012",
                               "arn:aws:sts::123456789012:assumed-role/example-operator",
                               "us-east-1", "example-cluster", run=interrupted, receipt=self.receipt)
        intent = addon.read_record(self.receipt)
        self.assertEqual(intent["state"], "intent")
        self.assertEqual(intent["result"]["kind"], "intent")
        self.assertEqual(calls, [("sts", "get-caller-identity"), ("eks", "describe-addon"),
                                 ("eks", "describe-addon"), ("eks", "update-addon")])

        def deny_aws(label, *arguments):
            self.fail("Changed recovery arguments must refuse before AWS reads")

        for changed in ({"action": "revert"}, {"cluster": "different-cluster"},
                        {"baseline": snapshot({"different": True})}):
            options = {"action": "apply", "cluster": "example-cluster", "baseline": baseline}
            options.update(changed)
            with self.assertRaisesRegex(ValueError, "Recovery arguments or source differ"):
                addon.recover_addon(options["baseline"], self.receipt, options["action"],
                                    "approved-label", "123456789012",
                                    "arn:aws:sts::123456789012:assumed-role/example-operator",
                                    "us-east-1", options["cluster"], run=deny_aws)
        self.assertEqual(addon.read_record(self.receipt)["clientRequestToken"], intent["clientRequestToken"])

        recovered_calls = []

        def read_only(label, *arguments):
            recovered_calls.append(arguments[:2])
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            self.assertEqual(arguments[:2], ("eks", "describe-addon"))
            return snapshot(addon.prepare(baseline, baseline))

        recovered = addon.recover_addon(baseline, self.receipt, "apply", "approved-label", "123456789012",
                                        "arn:aws:sts::123456789012:assumed-role/example-operator",
                                        "us-east-1", "example-cluster", run=read_only)
        self.assertEqual(recovered_calls, [("sts", "get-caller-identity"), ("eks", "describe-addon")])
        self.assertEqual(recovered["clientRequestToken"], intent["clientRequestToken"])
        self.assertEqual(recovered["state"], "unknown")
        self.assertEqual(recovered["result"]["kind"], "unknown")
        self.assertEqual(recovered["updateReadback"]["kind"], "unavailable")
        self.assertTrue(recovered["readback"]["matchesCandidate"])
        self.assertEqual(addon.read_record(self.receipt), recovered)

    def test_submitted_recovery_queries_only_its_update_id(self):
        baseline = snapshot(self.retained)

        def submitted(label, *arguments):
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            if arguments[:2] == ("eks", "update-addon"):
                return {"update": {"id": "example-update", "status": "InProgress"}}
            return baseline

        addon.update_addon(baseline, False, "approved-label", "123456789012",
                           "arn:aws:sts::123456789012:assumed-role/example-operator",
                           "us-east-1", "example-cluster", run=submitted, receipt=self.receipt)
        calls = []

        def recover_read(label, *arguments):
            calls.append(arguments[:2])
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            if arguments[:2] == ("eks", "describe-update"):
                self.assertEqual(arguments[arguments.index("--update-id") + 1], "example-update")
                return {"update": {"status": "Successful"}}
            self.assertEqual(arguments[:2], ("eks", "describe-addon"))
            return snapshot(addon.prepare(baseline, baseline))

        result = addon.recover_addon(baseline, self.receipt, "apply", "approved-label", "123456789012",
                                     "arn:aws:sts::123456789012:assumed-role/example-operator",
                                     "us-east-1", "example-cluster", run=recover_read)
        self.assertEqual(calls, [("sts", "get-caller-identity"), ("eks", "describe-update"),
                                 ("eks", "describe-addon")])
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(result["updateReadback"]["status"], "Successful")
        self.assertTrue(result["readback"]["matchesCandidate"])

    @patch.object(addon.subprocess, "run")
    def test_recover_cli_reads_unknown_intent_without_repeating_update(self, run):
        baseline = snapshot(self.retained)

        def interrupted(label, *arguments):
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            if arguments[:2] == ("eks", "update-addon"):
                raise KeyboardInterrupt()
            return baseline

        with self.assertRaises(KeyboardInterrupt):
            addon.update_addon(baseline, False, "approved-label", "123456789012",
                               "arn:aws:sts::123456789012:assumed-role/example-operator",
                               "us-east-1", "example-cluster", run=interrupted, receipt=self.receipt)
        token = addon.read_record(self.receipt)["clientRequestToken"]

        def fake_subprocess(command, **kwargs):
            self.assertEqual(command[:8], ["adp-cred", "assume", "--service", "aws",
                                           "--label", "approved-label", "--exec", "aws"])
            if command[8:10] == ["sts", "get-caller-identity"]:
                response = {"Account": "123456789012",
                            "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            else:
                self.assertEqual(command[8:10], ["eks", "describe-addon"])
                response = snapshot(addon.prepare(baseline, baseline))
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

        run.side_effect = fake_subprocess
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = Path(directory) / "baseline.json"
            baseline_path.write_text(json.dumps(baseline))
            with patch("sys.argv", ["gateway_emf_addon.py", "recover", "--baseline", str(baseline_path),
                                    "--receipt", str(self.receipt), "--expected-action", "apply",
                                    "--aws-label", "approved-label", "--expected-account", "123456789012",
                                    "--expected-assumed-role",
                                    "arn:aws:sts::123456789012:assumed-role/example-operator",
                                    "--region", "us-east-1", "--cluster", "example-cluster"]):
                addon.main()
        self.assertEqual(run.call_count, 2)
        recovered = addon.read_record(self.receipt)
        self.assertEqual(recovered["state"], "unknown")
        self.assertEqual(recovered["clientRequestToken"], token)
        self.assertTrue(recovered["readback"]["matchesCandidate"])
        self.assertEqual(recovered["updateReadback"]["kind"], "unavailable")

    def test_runbook_describes_scoped_recovery_and_live_handoff(self):
        runbook = (ROOT / "docs/runbooks/gateway-metric-export-verification.md").read_text()
        for expected in ("exclusive mutation ownership of this addon",
                         "actual Fluent Bit writer", "--expected-action apply",
                         "--expected-action revert", ".result.updateId",
                         "Poll at most six times, 30 seconds apart",
                         "matching configuration alone is not proof",
                         "human accepts live AC-04/SCN-03"):
            with self.subTest(expected=expected):
                self.assertIn(expected, runbook)

    def test_runbook_pins_bounded_live_scenario_and_protected_cleanup(self):
        runbook = (ROOT / "docs/runbooks/gateway-metric-export-verification.md").read_text()
        scoped = runbook.split("## Scoped EMF collector repair (#8337)", 1)[1].split(
            "## Reproduce the local collector serialization check", 1)[0]
        for expected in ("git rev-parse HEAD", "collector-hash.txt", "successful specific update ID",
                         "UTC timestamp immediately before", "after exactly one approved non-model producer event",
                         "baseline and post-event query windows", "at most six", "30 seconds apart",
                         "BedrockGateway/ExporterEmitAttempt", "actual collector identity",
                         "protected\ncleanup receipt", "rollback update ID and status",
                         "required audit and\naccounting logs", "incomplete cleanup or unavailable readback"):
            with self.subTest(expected=expected):
                self.assertIn(expected, scoped)

    def test_public_receipt_directory_refuses_before_addon_mutation(self):
        baseline = snapshot(self.retained)
        calls = []

        def fake_aws(label, *arguments):
            calls.append(arguments[:2])
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            self.assertEqual(arguments[:2], ("eks", "describe-addon"))
            return baseline

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o755)
            receipt = Path(directory) / "receipt.json"
            with self.assertRaisesRegex(ValueError, "private directory"):
                addon.update_addon(baseline, False, "approved-label", "123456789012",
                                   "arn:aws:sts::123456789012:assumed-role/example-operator",
                                   "us-east-1", "example-cluster", run=fake_aws, receipt=receipt)
            self.assertFalse(receipt.exists())
        self.assertEqual(calls, [("sts", "get-caller-identity"), ("eks", "describe-addon"),
                                 ("eks", "describe-addon")])

    @patch.object(addon.subprocess, "run")
    def test_stale_live_cli_does_not_create_receipt_or_submit_update(self, run):
        baseline = snapshot(self.retained)
        changed = snapshot({**self.retained, "futureSetting": {"enabled": False}})

        def fake_subprocess(command, **kwargs):
            self.assertEqual(command[:8], ["adp-cred", "assume", "--service", "aws",
                                           "--label", "approved-label", "--exec", "aws"])
            if command[8:10] == ["sts", "get-caller-identity"]:
                result = {"Account": "123456789012",
                          "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            else:
                self.assertEqual(command[8:10], ["eks", "describe-addon"])
                result = changed
            return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

        run.side_effect = fake_subprocess
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = Path(directory) / "baseline.json"
            receipt_path = Path(directory) / "receipt.json"
            baseline_path.write_text(json.dumps(baseline))
            arguments = ["gateway_emf_addon.py", "apply", "--baseline", str(baseline_path),
                         "--receipt", str(receipt_path), "--aws-label", "approved-label",
                         "--expected-account", "123456789012", "--expected-assumed-role",
                         "arn:aws:sts::123456789012:assumed-role/example-operator",
                         "--region", "us-east-1", "--cluster", "example-cluster"]
            with patch("sys.argv", arguments), self.assertRaisesRegex(ValueError, "differs from the pinned baseline"):
                addon.main()
            self.assertFalse(receipt_path.exists())
            self.assertEqual(run.call_count, 2)

    def test_already_configured_addon_is_not_restarted(self):
        original = snapshot(self.retained)
        configured = snapshot(addon.prepare(original, original))
        calls = []

        def fake_aws(label, *arguments):
            calls.append(arguments[:2])
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            self.assertEqual(arguments[:2], ("eks", "describe-addon"))
            return configured

        with self.assertRaisesRegex(ValueError, "No routing delta"):
            addon.update_addon(configured, False, "approved-label", "123456789012",
                               "arn:aws:sts::123456789012:assumed-role/example-operator",
                               "us-east-1", "example-cluster", run=fake_aws, receipt=self.receipt)
        self.assertEqual(calls, [("sts", "get-caller-identity"), ("eks", "describe-addon")])

    @patch.object(addon.subprocess, "run")
    def test_broker_never_uses_ambient_aws_credentials(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '{"Account":"123456789012"}', "")
        self.assertEqual(addon.broker_aws("approved-label", "sts", "get-caller-identity"),
                         {"Account": "123456789012"})
        self.assertEqual(run.call_args.args[0][:9], ["adp-cred", "assume", "--service", "aws",
                                                   "--label", "approved-label", "--exec", "aws", "sts"])

    @patch.object(addon.subprocess, "run")
    def test_live_cli_writes_only_a_private_update_receipt(self, run):
        baseline = snapshot(self.retained)

        def fake_subprocess(command, **kwargs):
            self.assertEqual(command[:8], ["adp-cred", "assume", "--service", "aws",
                                           "--label", "approved-label", "--exec", "aws"])
            if command[8:10] == ["sts", "get-caller-identity"]:
                response = {"Account": "123456789012",
                            "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            elif command[8:10] == ["eks", "describe-addon"]:
                response = baseline
            else:
                self.assertEqual(command[8:10], ["eks", "update-addon"])
                response = {"update": {"id": "example-update", "status": "InProgress",
                                       "params": [{"value": "private-config"}]}}
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

        run.side_effect = fake_subprocess
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = Path(directory) / "baseline.json"
            receipt_path = Path(directory) / "receipt.json"
            baseline_path.write_text(json.dumps(baseline))
            with patch("sys.argv", ["gateway_emf_addon.py", "apply", "--baseline", str(baseline_path),
                                    "--receipt", str(receipt_path), "--aws-label", "approved-label",
                                    "--expected-account", "123456789012", "--expected-assumed-role",
                                    "arn:aws:sts::123456789012:assumed-role/example-operator",
                                    "--region", "us-east-1", "--cluster", "example-cluster"]):
                addon.main()
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
            record = json.loads(receipt_path.read_text())
            self.assertEqual(record["result"], {"kind": "submitted", "updateId": "example-update",
                                                "status": "InProgress"})
            record = json.loads(receipt_path.read_text())
        self.assertEqual(record["state"], "submitted")
        self.assertEqual(record["result"]["updateId"], "example-update")
        self.assertEqual(record["readback"]["kind"], "readback")
        self.assertEqual(run.call_count, 5)

    def test_rollback_preserves_newer_settings_and_only_removes_owned_file(self):
        baseline = snapshot(self.retained)
        installed = addon.prepare(baseline, baseline)
        current = json.loads(json.dumps(installed))
        current["agent"]["podAnnotations"]["example.org/newer"] = "still-present"
        current["futureSetting"]["enabled"] = False
        current["containerLogs"]["fluentBit"]["config"]["extraFiles"]["newer.conf"] = "[FILTER]\n"
        expected = json.loads(json.dumps(self.retained))
        expected["agent"]["podAnnotations"]["example.org/newer"] = "still-present"
        expected["futureSetting"]["enabled"] = False
        expected["containerLogs"]["fluentBit"]["config"]["extraFiles"]["newer.conf"] = "[FILTER]\n"
        self.assertEqual(addon.prepare(snapshot(current), baseline, rollback=True), expected)
        self.assertEqual(self.retained["futureSetting"], {"enabled": True})

    def test_rollback_restores_absent_or_intentionally_empty_ancestors(self):
        for original in ({}, {"containerLogs": {"fluentBit": {"config": {"extraFiles": {}}}}}):
            with self.subTest(original=original):
                baseline = snapshot(original)
                installed = addon.prepare(baseline, baseline)
                self.assertEqual(addon.prepare(snapshot(installed), baseline, rollback=True), original)

    def test_rollback_rejects_changed_file_and_originally_owned_routing(self):
        baseline = snapshot(self.retained)
        installed = addon.prepare(baseline, baseline)
        changed = json.loads(json.dumps(installed))
        changed["containerLogs"]["fluentBit"]["config"]["extraFiles"]["gateway-emf.conf"] = "new owner"
        with self.assertRaisesRegex(ValueError, "absent or changed"):
            addon.prepare(snapshot(changed), baseline, rollback=True)
        self.assertEqual(changed["containerLogs"]["fluentBit"]["config"]["extraFiles"]["gateway-emf.conf"],
                         "new owner")
        with self.assertRaisesRegex(ValueError, "Baseline already owns"):
            addon.prepare(snapshot(installed), snapshot(installed), rollback=True)

    def test_live_rollback_submits_only_its_delta_with_original_iam_policy(self):
        baseline = snapshot(self.retained)
        installed = addon.prepare(baseline, baseline)
        current = json.loads(json.dumps(installed))
        current["agent"]["podAnnotations"]["example.org/newer"] = "keep"
        current["futureSetting"]["enabled"] = False
        expected = json.loads(json.dumps(self.retained))
        expected["agent"]["podAnnotations"]["example.org/newer"] = "keep"
        expected["futureSetting"]["enabled"] = False
        calls = []

        def fake_aws(label, *arguments):
            self.assertEqual(label, "approved-label")
            calls.append(arguments[:2])
            if arguments[0] == "sts":
                return {"Account": "123456789012",
                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
            if arguments[:2] == ("eks", "describe-addon"):
                return snapshot(current)
            self.assertEqual(arguments[:2], ("eks", "update-addon"))
            path = Path(arguments[arguments.index("--configuration-values") + 1].removeprefix("file://"))
            self.assertEqual(json.loads(path.read_text()), expected)
            return {"update": {"id": "example-update", "status": "InProgress"}}

        addon.update_addon(baseline, True, "approved-label", "123456789012",
                           "arn:aws:sts::123456789012:assumed-role/example-operator",
                           "us-east-1", "example-cluster", run=fake_aws, receipt=self.receipt)
        self.assertEqual(calls, [("sts", "get-caller-identity"), ("eks", "describe-addon"),
                                 ("eks", "describe-addon"), ("eks", "update-addon"),
                                 ("eks", "describe-addon")])
        terraform = (ROOT / "platform/infra/modules/eks/main.tf").read_text()
        role_section = terraform.split('resource "aws_iam_role" "cloudwatch_observability" {', 1)[1].split(
            'resource "aws_eks_addon" "cloudwatch_observability" {', 1)[0]
        self.assertIn('policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"', role_section)
        self.assertNotIn("cloudwatch:PutMetricData", role_section)

    @patch.object(addon.subprocess, "run")
    def test_cli_rollback_preserves_newer_settings_and_refuses_stale_live_state(self, run):
        baseline = snapshot(self.retained)
        current = addon.prepare(baseline, baseline)
        current["agent"]["podAnnotations"]["example.org/newer"] = "keep-me"
        current["containerLogs"]["fluentBit"]["config"]["extraFiles"]["newer.conf"] = "[FILTER]\n"
        expected = json.loads(json.dumps(self.retained))
        expected["agent"]["podAnnotations"]["example.org/newer"] = "keep-me"
        expected["containerLogs"]["fluentBit"]["config"]["extraFiles"]["newer.conf"] = "[FILTER]\n"

        with tempfile.TemporaryDirectory() as directory:
            baseline_path = Path(directory) / "baseline.json"
            before_path = Path(directory) / "before.json"
            candidate_path = Path(directory) / "candidate.json"
            receipt_path = Path(directory) / "revert-receipt.json"
            baseline_path.write_text(json.dumps(baseline))
            before_path.write_text(json.dumps(snapshot(current)))
            with patch("sys.argv", ["gateway_emf_addon.py", "rollback", "--baseline", str(baseline_path),
                                    "--before", str(before_path), "--output", str(candidate_path)]):
                addon.main()
            self.assertEqual(json.loads(candidate_path.read_text()), expected)
            run.assert_not_called()

            changed_file = json.loads(json.dumps(current))
            changed_file["containerLogs"]["fluentBit"]["config"]["extraFiles"]["gateway-emf.conf"] = "changed"
            changed_setting = json.loads(json.dumps(current))
            changed_setting["agent"]["podAnnotations"]["example.org/newer"] = "edited-again"
            for name, states, error, expected_reads in (
                ("owned-file-changed", [snapshot(changed_file)], "absent or changed", 1),
                ("concurrent-unrelated-edit", [snapshot(current), snapshot(changed_setting)],
                 "changed before update", 2),
            ):
                with self.subTest(name=name):
                    reads = []

                    def fake_subprocess(command, **kwargs):
                        self.assertEqual(command[:8], ["adp-cred", "assume", "--service", "aws",
                                                       "--label", "approved-label", "--exec", "aws"])
                        if command[8:10] == ["sts", "get-caller-identity"]:
                            response = {"Account": "123456789012",
                                        "Arn": "arn:aws:sts::123456789012:assumed-role/example-operator/test-session"}
                        else:
                            self.assertEqual(command[8:10], ["eks", "describe-addon"])
                            response = states[len(reads)]
                            reads.append(response)
                        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

                    run.reset_mock()
                    run.side_effect = fake_subprocess
                    with patch("sys.argv", ["gateway_emf_addon.py", "revert", "--baseline", str(baseline_path),
                                            "--receipt", str(receipt_path), "--aws-label", "approved-label",
                                            "--expected-account", "123456789012", "--expected-assumed-role",
                                            "arn:aws:sts::123456789012:assumed-role/example-operator",
                                            "--region", "us-east-1", "--cluster", "example-cluster"]):
                        with self.assertRaisesRegex(ValueError, error):
                            addon.main()
                    self.assertEqual(len(reads), expected_reads)
                    self.assertEqual(run.call_count, expected_reads + 1)
                    self.assertFalse(receipt_path.exists())
                    self.assertEqual(json.loads(baseline_path.read_text()), baseline)
                    self.assertEqual(json.loads(before_path.read_text()), snapshot(current))

    def test_baseline_mismatch_and_conflicting_file_refuse(self):
        baseline = snapshot(self.retained)
        changed = json.loads(json.dumps(self.retained))
        changed["agent"]["podAnnotations"]["example.org/retained"] = "updated"
        with self.assertRaisesRegex(ValueError, "differs from the pinned baseline"):
            addon.prepare(snapshot(changed), baseline)
        changed = json.loads(json.dumps(self.retained))
        changed["containerLogs"]["fluentBit"]["config"]["extraFiles"]["gateway-emf.conf"] = "unrelated"
        with self.assertRaisesRegex(ValueError, "already exists"):
            addon.prepare(snapshot(changed), snapshot(changed))

    def test_unsupported_settings_and_wrong_target_refuse(self):
        baseline = snapshot(self.retained)
        invalid = snapshot({"containerLogs": {"fluentBit": {"config": {"extraFiles": "unsupported"}}}})
        with self.assertRaisesRegex(ValueError, "Unsupported addon configuration"):
            addon.prepare(invalid, invalid)
        wrong = snapshot(self.retained)
        wrong["addon"]["serviceAccountRoleArn"] = "arn:aws:iam::123456789012:role/other"
        with self.assertRaisesRegex(ValueError, "collector identity differs"):
            addon.prepare(wrong, baseline)

    def test_terraform_keeps_configuration_outside_upgrade_ownership(self):
        terraform = (ROOT / "platform/infra/modules/eks/main.tf").read_text()
        resource = terraform.split('resource "aws_eks_addon" "cloudwatch_observability" {', 1)[1].split('\nresource "', 1)[0]
        self.assertNotIn("configuration_values =", resource)
        self.assertIn("ignore_changes = [configuration_values]", resource)
        self.assertIn('resolve_conflicts_on_update = "PRESERVE"', resource)
        self.assertIn('resolve_conflicts_on_create = "NONE"', resource)


if __name__ == "__main__":
    unittest.main()
