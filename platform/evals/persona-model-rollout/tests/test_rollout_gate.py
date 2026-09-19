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
    value["source_revision"] = "a" * 40
    value["evidence_schema_revision"] = "1.1"
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
        """Use L5 (single-invocation kind A cell) to check the invocation
        validation still rejects incomplete evidence."""
        value = safe_evidence()
        value["cells"]["L5"] = {
            **common(),
            "status": "pass",
            "invocation": {"real_model_output": True},
        }
        report = gate.assess(manifest(), value)
        self.assertTrue(
            any("L5: invocation missing" in error for error in report["errors"])
        )
        # L5 is the 5th cell (index 4)
        self.assertEqual(report["results"][4]["status"], "fail")

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
            "surface": "ui",
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

    # --- Negative regression tests for false-pass blockers ---

    def test_shadow_rejected_events_fail_assessment(self):
        """Regression: non-empty rejected_events must fail L22 even when
        known paths have enough observations."""
        value = safe_evidence()
        paths = manifest()["required_shadow_paths"]
        value["cells"]["L22"] = {**common(), "status": "pass"}
        value["shadow_comparison"] = {
            "minimum_observations_per_path": 1,
            "observations": [
                {
                    "dispatch_path": path,
                    "mapping_exists": False,
                    "legacy_model": "model-a",
                    "proposed_model": "model-a",
                    "admission_refusal": False,
                    "persona": "developer",
                    "principal_kind": "human",
                    "tenant_id": "tenant-1",
                    "policy_revision": "policy-1",
                }
                for path in paths
            ],
            "rejected_events": [
                {"line": 99, "reason": "unmapped_path", "channel": "x", "trigger": "y"}
            ],
        }
        report = gate.assess(manifest(), value)
        errors = "\n".join(report["errors"])
        self.assertIn("rejected event(s) in shadow data", errors)
        self.assertEqual(report["results"][21]["status"], "fail")

    def test_single_invocation_cannot_pass_multi_observation_cell(self):
        """Regression: L1 claims three personas on three models. A single
        invocation object must not satisfy it."""
        value = safe_evidence()
        value["cells"]["L1"] = {
            **common(),
            "status": "pass",
            "invocation": {
                "provider_request_id": "req-1",
                "model_output_sha256": "a" * 64,
                "usage_row_id": "row-1",
                "agent_run_id": "run-1",
                "cost_usd": 0.01,
                "input_tokens": 100,
                "output_tokens": 50,
                "real_model_output": True,
            },
        }
        report = gate.assess(manifest(), value)
        errors = "\n".join(report["errors"])
        self.assertIn("multi-observation cell requires an invocations list", errors)
        self.assertEqual(report["results"][0]["status"], "fail")

    def test_single_invocation_cannot_pass_chain_cell(self):
        """Regression: L17 claims a multi-hop chain with one shared snapshot
        digest. A single invocation object must not satisfy it."""
        value = safe_evidence()
        value["cells"]["L17"] = {
            **common(),
            "status": "pass",
            "invocation": {
                "provider_request_id": "req-1",
                "model_output_sha256": "a" * 64,
                "usage_row_id": "row-1",
                "agent_run_id": "run-1",
                "cost_usd": 0.01,
                "input_tokens": 100,
                "output_tokens": 50,
                "real_model_output": True,
            },
        }
        report = gate.assess(manifest(), value)
        errors = "\n".join(report["errors"])
        self.assertIn("chain cell requires a chain evidence object", errors)
        self.assertEqual(report["results"][16]["status"], "fail")

    def test_chain_without_descendant_proof_cannot_pass_l21(self):
        """Regression: L21 claims a direct override applies to root hop only.
        A chain where every hop has the override must not satisfy it."""
        value = safe_evidence()
        inv = {
            "provider_request_id": "req-1",
            "model_output_sha256": "a" * 64,
            "usage_row_id": "row-1",
            "agent_run_id": "run-1",
            "cost_usd": 0.01,
            "input_tokens": 100,
            "output_tokens": 50,
            "real_model_output": True,
            "persona": "developer",
            "resolved_model": "model-a",
        }
        value["cells"]["L21"] = {
            **common(),
            "status": "pass",
            "chain": {
                "chain_id": "chain-1",
                "snapshot_digest": "digest-1",
                "root_principal_kind": "human",
                "hops": [
                    {**inv, "snapshot_digest": "digest-1", "has_direct_override": True},
                    {**inv, "snapshot_digest": "digest-1", "has_direct_override": True},
                ],
            },
        }
        report = gate.assess(manifest(), value)
        errors = "\n".join(report["errors"])
        self.assertIn(
            "at least one descendant hop must lack has_direct_override", errors
        )
        self.assertEqual(report["results"][20]["status"], "fail")

    def test_enforcing_cells_are_structurally_unpassable(self):
        """Regression: L23/L24 must be unpassable in the non-enforcing harness.
        complete must remain false."""
        value = safe_evidence()
        value["cells"]["L23"] = {**common(), "status": "pass"}
        value["cells"]["L24"] = {**common(), "status": "pass"}
        report = gate.assess(manifest(), value)
        errors = "\n".join(report["errors"])
        self.assertIn(
            "L23: structurally unpassable in the non-enforcing harness", errors
        )
        self.assertIn(
            "L24: structurally unpassable in the non-enforcing harness", errors
        )
        self.assertFalse(report["complete"])
        self.assertFalse(report["enforcement_ready"])
        self.assertEqual(report["results"][22]["status"], "fail")
        self.assertEqual(report["results"][23]["status"], "fail")

    def test_evidence_missing_source_revision_fails(self):
        """Regression: evidence must bind to source and schema revisions."""
        value = safe_evidence()
        del value["source_revision"]
        del value["evidence_schema_revision"]
        report = gate.assess(manifest(), value)
        errors = "\n".join(report["errors"])
        self.assertIn("evidence must include source_revision", errors)
        self.assertIn("evidence must include evidence_schema_revision", errors)


if __name__ == "__main__":
    unittest.main()
