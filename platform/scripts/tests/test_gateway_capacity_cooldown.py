"""Finite, synthetic cooldown and configuration evidence; no target access."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "gateway-capacity-cooldown.py"
spec = importlib.util.spec_from_file_location("gateway_capacity_cooldown", SCRIPT)
cooldown = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cooldown)
STOPPED = "2026-01-01T00:00:00Z"


def snapshot():
    def item(kind, name, spec):
        return {"kind": kind, "metadata": {"name": name, "namespace": "adp-gateway", "uid": kind + "-uid"},
                "spec": spec, "status": {"replicas": 12}}

    return {"items": [
        item("Deployment", "bedrockgateway", {"replicas": 12, "strategy": {"rollingUpdate": {"maxUnavailable": 0}},
                                             "template": {"spec": {"containers": [{"image": "example.test/gateway:v1"}]}}}),
        item("HorizontalPodAutoscaler", "bedrockgateway", {"minReplicas": 2, "maxReplicas": 12,
                                                              "behavior": {"scaleDown": {"stabilizationWindowSeconds": 600}}}),
        item("PodDisruptionBudget", "bedrockgateway-pdb", {"maxUnavailable": 1}),
    ]}


def sample(second, desired, current, ready, *, metrics=True, restarts=0, serving=True):
    from datetime import datetime, timedelta, timezone

    observed = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=second)
    return {"observed_at": observed.isoformat(),
            "replicas": {"desired": desired, "current": current, "ready": ready,
                         "hpa_desired": desired, "hpa_current": current},
            "metrics_available": metrics,
            "pods": [{"ready": STOPPED, "endpoint_ready": serving, "endpoint_serving": serving,
                      "restarts": restarts} for _ in range(current)]}


class CapacityCooldownTests(unittest.TestCase):
    def test_two_fresh_baseline_observations_are_only_a_candidate(self):
        before = snapshot()
        after = copy.deepcopy(before)
        after["items"][0]["spec"]["replicas"] = 2
        after["items"][0]["status"]["replicas"] = 2
        timeline = [sample(0, 5, 5, 5), sample(60, 3, 3, 2), sample(120, 2, 2, 2), sample(180, 2, 2, 2)]
        result = cooldown.evaluate(timeline, before, after, STOPPED, 180)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["configuration_changes"], [])
        self.assertEqual(result["timeline"][0]["replicas"]["ready"], 5)
        self.assertEqual(result["timeline"][-1]["replicas"], {"desired": 2, "current": 2,
                                                                  "ready": 2, "hpa_desired": 2, "hpa_current": 2})
        self.assertTrue(result["manual_health_and_no_forced_scaling_receipts_required"])

    def test_late_recovery_missing_metrics_and_restart_cannot_be_a_candidate(self):
        before = snapshot()
        healthy = sample(0, 5, 5, 5)
        returned = sample(601, 2, 2, 2)
        result = cooldown.evaluate([healthy, returned, sample(602, 2, 2, 2)], before, before, STOPPED, 600)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("two_ready_replicas_not_observed_within_deadline", result["reasons"])

        for altered, reason in ((sample(60, 2, 2, 2, metrics=False), "metrics_unavailable"),
                                (sample(60, 2, 2, 2, restarts=1), "restart_or_unknown"),
                                (sample(60, 2, 2, 2, serving=False), "endpoint_unhealthy"),
                                (sample(60, 2, 3, 2), "two_ready_replicas_not_observed_within_deadline")):
            with self.subTest(reason=reason):
                result = cooldown.evaluate([sample(30, 2, 2, 2), altered], before, before, STOPPED, 120)
                self.assertEqual(result["status"], "incomplete")
                self.assertIn(reason, result["reasons"])

    def test_config_diff_and_wrong_target_cannot_claim_baseline(self):
        before = snapshot()
        after = copy.deepcopy(before)
        after["items"][1]["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] = 0
        result = cooldown.evaluate([sample(30, 2, 2, 2), sample(60, 2, 2, 2)], before, after, STOPPED, 120)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["configuration_changes"], ["hpa.spec"])
        after = copy.deepcopy(before)
        after["items"][0]["metadata"]["uid"] = "replacement"
        self.assertIn("deployment.uid", cooldown.evaluate([sample(30, 2, 2, 2), sample(60, 2, 2, 2)],
                                                          before, after, STOPPED, 120)["configuration_changes"])
        after["items"][0]["metadata"]["namespace"] = "other"
        with self.assertRaisesRegex(ValueError, "bound namespace"):
            cooldown.evaluate([], before, after, STOPPED, 120)
        before["items"][1]["spec"]["minReplicas"] = 1
        after = copy.deepcopy(before)
        result = cooldown.evaluate([sample(30, 2, 2, 2), sample(60, 2, 2, 2)], before, after, STOPPED, 120)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("hpa.minReplicas", result["configuration_changes"])
        before["items"][1]["spec"]["minReplicas"] = 2
        with self.assertRaisesRegex(ValueError, "timestamp order"):
            cooldown.evaluate([sample(60, 2, 2, 2), sample(30, 2, 2, 2)], before, before, STOPPED, 120)

    def test_cli_writes_private_receipt_without_overwrite_or_target_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.json"
            after = root / "after.json"
            samples = root / "samples.jsonl"
            receipt = root / "receipt.json"
            before.write_text(json.dumps(snapshot()))
            after.write_bytes(before.read_bytes())
            samples.write_text("\n".join(json.dumps(sample(second, 2, 2, 2)) for second in (30, 60)))
            command = ["python3", str(SCRIPT), "--samples", str(samples), "--before", str(before),
                       "--after", str(after), "--admissions-stopped-at", STOPPED,
                       "--deadline-seconds", "120", "--output", str(receipt)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(receipt.read_text())["status"], "candidate")
            initial = receipt.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(receipt.read_bytes(), initial)
            self.assertNotEqual(subprocess.run(command[:-3] + ["601", "--output", str(root / "late.json")],
                                               capture_output=True).returncode, 0)
            self.assertFalse((root / "late.json").exists())
