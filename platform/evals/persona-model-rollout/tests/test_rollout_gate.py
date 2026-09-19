from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rollout_gate", ROOT / "rollout_gate.py")
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def manifest():
    return gate.load_json(ROOT / "matrix.json")


def safe_evidence():
    value = gate.template(manifest())
    value["deployment"] = {
        "environment": "dev",
        "account_id": "879318057152",
        "region": "us-east-1",
        "git_revision": "a" * 40,
        "gateway_image_digest": "sha256:" + "b" * 64,
        "worker_image_digest": "sha256:" + "c" * 64,
        "webhook_version": "42",
        "deploy_state_sha256": "d" * 64,
        "mixed_version_nodes": False,
        "health": {
            "frontend_200": True,
            "gateway_200": True,
            "gateway_pods_running": True,
            "rds_available": True,
            "worker_spawned": True,
        },
    }
    return value


def common():
    return {
        "account_id": "879318057152",
        "region": "us-east-1",
        "timestamp_utc": "2026-09-19T12:00:00Z",
        "persona": "developer",
        "principal_kind": "human",
        "principal_id": "person-1",
        "tenant_id": "tenant-1",
        "surface": "cli",
        "requested_model": "model-a",
        "resolved_model": "model-a",
        "resolution_source": "principal-mapping",
        "policy_revision": "policy-1",
        "posture_revision": 1,
    }


class RolloutGateTests(unittest.TestCase):
    def test_manifest_is_exactly_the_approved_25_cells(self):
        value = manifest()
        self.assertEqual(gate.validate_manifest(value), [])
        self.assertEqual(len(value["cells"]), 25)

    def test_empty_template_is_honestly_incomplete_but_structurally_safe(self):
        report = gate.assess(manifest(), safe_evidence())
        self.assertEqual(report["errors"], [])
        self.assertFalse(report["complete"])
        self.assertEqual(report["totals"]["not_run"], 25)
        self.assertFalse(report["enforcement_ready"])

    def test_real_invocation_cannot_pass_without_provider_and_usage_evidence(self):
        value = safe_evidence()
        value["cells"]["L1"] = {
            **common(),
            "status": "pass",
            "invocation": {"real_model_output": True},
        }
        report = gate.assess(manifest(), value)
        self.assertTrue(
            any("L1: invocation missing" in error for error in report["errors"])
        )
        self.assertEqual(report["results"][0]["status"], "fail")

    def test_refusal_rejects_billable_work_and_wrong_reason(self):
        value = safe_evidence()
        value["cells"]["L12"] = {
            **common(),
            "status": "pass",
            "refusal": {
                "reason_code": "not_permitted",
                "requester_delivery_id": "comment-1",
                "usage_query_id": "query-1",
                "provider_log_query_id": "query-2",
                "usage_rows": 1,
                "provider_invocations": 1,
                "provider_request_id": "bad",
            },
        }
        errors = "\n".join(gate.assess(manifest(), value)["errors"])
        self.assertIn("expected reason_code 'unknown_model'", errors)
        self.assertIn("zero usage rows and zero provider invocations", errors)
        self.assertIn("cannot carry a provider request ID", errors)

    def test_empty_scope_ui_cell_uses_non_billable_observation_not_fake_refusal(self):
        value = safe_evidence()
        value["cells"]["L8"] = {
            **common(),
            "status": "pass",
            "requested_model": None,
            "resolved_model": None,
            "non_billable_observation": {
                "request_trace_id": "trace-1",
                "usage_query_id": "query-1",
                "provider_log_query_id": "query-2",
                "scope_selector_rendered": False,
                "self_rows_only": True,
                "usage_rows": 0,
                "provider_invocations": 0,
            },
        }
        report = gate.assess(manifest(), value)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["results"][7]["status"], "pass")

    def test_shadow_gate_separates_refusals_and_requires_every_path(self):
        value = safe_evidence()
        value["cells"]["L22"] = {**common(), "status": "pass"}
        value["shadow_comparison"]["observations"] = [
            {
                "dispatch_path": "github_mention",
                "mapping_exists": False,
                "legacy_model": "model-a",
                "proposed_model": "model-b",
                "admission_refusal": True,
                "persona": "developer",
                "principal_kind": "human",
                "tenant_id": "tenant-1",
                "policy_revision": "policy-1",
            }
        ]
        errors = "\n".join(gate.assess(manifest(), value)["errors"])
        self.assertIn("mixes an admission refusal", errors)
        self.assertIn("unexplained divergence", errors)
        self.assertIn("uncovered shadow paths", errors)

    def test_harness_rejects_any_attempt_to_authorize_spend_or_enforcement(self):
        value = safe_evidence()
        value["safety"].update(
            {"enforcement_authorized": True, "probe_spend_ceiling_usd": 10}
        )
        value["feature_flags"].update(
            {"persona_model_posture": "enforcing", "model_probe_enabled": True}
        )
        errors = "\n".join(gate.assess(manifest(), value)["errors"])
        self.assertIn("enforcement_authorized=false", errors)
        self.assertIn("paid probing is not authorized", errors)
        self.assertIn("persona_model_posture must remain report_only", errors)
        self.assertIn("model_probe_enabled must remain false", errors)

    def test_structured_worker_events_become_shadow_observations(self):
        event = {
            "event": "persona_model_shadow_comparison",
            "channel": "github",
            "trigger": "issue_labeled",
            "mapping_exists": True,
            "legacy_model": "model-a",
            "proposed_model": "model-b",
            "admission_refusal": False,
            "persona": "developer",
            "principal_kind": "human",
            "tenant_id": "tenant-1",
            "policy_revision": "policy-1",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("log prefix PMM09_MODEL_SHADOW " + json.dumps(event) + "\n")
            report = gate.shadow_report(path)
        self.assertEqual(report["rejected_events"], [])
        self.assertEqual(report["observations"][0]["dispatch_path"], "label_dispatch")

    def test_unmapped_shadow_event_is_rejected_not_silently_dropped(self):
        event = {
            "event": "persona_model_shadow_comparison",
            "channel": "new",
            "trigger": "new",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(json.dumps(event))
            report = gate.shadow_report(path)
        self.assertEqual(report["observations"], [])
        self.assertEqual(report["rejected_events"][0]["reason"], "unmapped_path")


if __name__ == "__main__":
    unittest.main()
