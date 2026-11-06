"""Synthetic Kubernetes lifecycle and evidence-safety checks."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "gateway-capacity-report.py"
spec = importlib.util.spec_from_file_location("gateway_capacity_report", SCRIPT)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


class CapacityReportTests(unittest.TestCase):
    def test_distinct_replica_counts_and_pod_stages(self):
        pod = {
            "metadata": {"name": "pod-one", "uid": "one", "creationTimestamp": "2026-01-01T00:00:00Z"},
            "spec": {"nodeName": "node-one"},
            "status": {
                "conditions": [
                    {"type": "PodScheduled", "status": "True", "lastTransitionTime": "2026-01-01T00:00:04Z"},
                    {"type": "Ready", "status": "True", "lastTransitionTime": "2026-01-01T00:00:25Z"}],
                "initContainerStatuses": [{"state": {"terminated": {"finishedAt": "2026-01-01T00:00:15Z"}}}],
                "containerStatuses": [{"name": "bedrockgateway", "restartCount": 0,
                                       "state": {"running": {"startedAt": "2026-01-01T00:00:17Z"}}}],
            },
        }
        events = {"items": [
            {"involvedObject": {"uid": "one"}, "reason": "Pulling", "firstTimestamp": "2026-01-01T00:00:05Z", "message": "private"},
            {"involvedObject": {"uid": "one"}, "reason": "Pulled", "firstTimestamp": "2026-01-01T00:00:12Z"},
            {"involvedObject": {"uid": "other"}, "reason": "Pulling", "firstTimestamp": "2025-01-01T00:00:00Z"},
        ]}
        result = reporter.summarize(
            {"spec": {"replicas": 5}, "status": {"replicas": 4, "readyReplicas": 2}},
            {"status": {"desiredReplicas": 5, "currentReplicas": 3}}, {"items": [pod]}, events,
            {"items": [{"endpoints": [{"targetRef": {"uid": "one"},
                                         "conditions": {"ready": True, "serving": True}}]}]},
            {"items": [{"metadata": {"name": "pod-one"}, "containers": [
                {"name": "bedrockgateway", "usage": {"cpu": "200m", "memory": "1Gi"}}]}]},
            "2026-01-01T00:00:30Z",
            {"items": [{"metadata": {"name": "node-one", "creationTimestamp": "2026-01-01T00:00:02Z"},
                        "status": {"allocatable": {"cpu": "2", "memory": "4Gi", "pods": "20"},
                                   "conditions": [{"type": "MemoryPressure", "status": "False"}]}}]},
        )
        self.assertEqual(result["replicas"], {"desired": 5, "current": 4, "ready": 2,
                                              "hpa_desired": 5, "hpa_current": 3})
        self.assertEqual(result["nodes"], [{"name": "node-one", "created": "2026-01-01T00:00:02Z",
                                            "allocatable": {"cpu": "2", "memory": "4Gi", "pods": "20"},
                                            "pressure": {"MemoryPressure": "False"}}])
        stages = result["pods"][0]
        self.assertEqual(stages["scheduled"], "2026-01-01T00:00:04Z")
        self.assertEqual(stages["image"], {"Pulling": "2026-01-01T00:00:05Z", "Pulled": "2026-01-01T00:00:12Z"})
        self.assertEqual(stages["init_finished"], ["2026-01-01T00:00:15Z"])
        self.assertEqual(stages["container_started"], "2026-01-01T00:00:17Z")
        self.assertEqual(stages["ready"], "2026-01-01T00:00:25Z")
        self.assertTrue(stages["endpoint_serving"])
        self.assertIsNone(stages["first_request_served"])
        self.assertEqual(stages["resources"], [{"name": "bedrockgateway", "cpu": "200m", "memory": "1Gi"}])
        self.assertNotIn("private", json.dumps(result))

    def test_missing_metrics_and_unready_pods_remain_explicit(self):
        result = reporter.summarize(
            {"spec": {"replicas": 2}, "status": {"replicas": 1}}, {"status": {}},
            {"items": [{"metadata": {"name": "pod-one", "uid": "one"},
                        "status": {"conditions": [{"type": "Ready", "status": "False"}]}}]},
            {"items": []}, {"items": []}, None, "2026-01-01T00:00:00Z",
        )
        self.assertFalse(result["metrics_available"])
        self.assertEqual(result["replicas"]["ready"], 0)
        self.assertIsNone(result["pods"][0]["ready"])
        self.assertFalse(result["pods"][0]["endpoint_serving"])

    def test_metrics_api_outage_retains_replica_gap_without_mutating_hpa(self):
        calls = []
        responses = {
            "deployment/bedrockgateway": {"spec": {"replicas": 5}, "status": {"replicas": 4, "readyReplicas": 2}},
            "hpa/bedrockgateway": {"status": {"desiredReplicas": 5, "currentReplicas": 4}},
        }

        def kubectl(*arguments, optional=False):
            calls.append((arguments, optional))
            if "--raw" in arguments:
                self.assertTrue(optional)
                return None
            return responses.get(arguments[1], {"items": []})

        with patch.object(reporter, "kubectl", side_effect=kubectl):
            report = reporter.sample()
        self.assertFalse(report["metrics_available"])
        self.assertEqual(report["replicas"], {"desired": 5, "current": 4, "ready": 2,
                                              "hpa_desired": 5, "hpa_current": 4})
        self.assertTrue(all(arguments[0] == "get" for arguments, _ in calls))
        self.assertEqual(len(calls), 7)

    def test_private_evidence_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.jsonl"
            with patch.object(reporter, "sample", return_value={"observed_at": "2026-01-01T00:00:00Z"}), \
                 patch("sys.argv", ["gateway-capacity-report.py", "--output", str(output), "--samples", "1", "--interval", "1"]):
                reporter.main()
            self.assertEqual(json.loads(output.read_text()), {"observed_at": "2026-01-01T00:00:00Z"})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            command = ["python3", str(SCRIPT), "--output", str(output), "--samples", "1", "--interval", "1"]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(json.loads(output.read_text()), {"observed_at": "2026-01-01T00:00:00Z"})
            self.assertNotEqual(subprocess.run(command[:-3] + ["--samples", "100", "--interval", "300"],
                                               capture_output=True).returncode, 0)
