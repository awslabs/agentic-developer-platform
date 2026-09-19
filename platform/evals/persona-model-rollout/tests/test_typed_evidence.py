"""Synthetic validator oracles. These fixtures are never live acceptance."""

import copy
import unittest

from test_independent_operator_regressions import invocation, result
from test_rollout_gate import common, gate, manifest, safe_evidence


def multi(cell="L1"):
    value = safe_evidence()
    entry = {**common(), "status": "pass", "surface": "ui" if cell == "L1" else "cli"}
    entry["invocations"] = [
        {
            **invocation(str(i)),
            "persona": persona,
            "resolved_model": f"model-{i}",
            "tenant_id": entry["tenant_id"],
            "principal_kind": entry["principal_kind"],
            "principal_id": entry["principal_id"],
        }
        for i, persona in enumerate(("architect", "developer", "reviewer"))
    ]
    value["cells"][cell] = entry
    return value


def chain(cell="L17"):
    value = safe_evidence()
    entry = {**common(), "status": "pass", "surface": "chain"}
    if cell == "L18":
        entry["principal_kind"] = "service_account"
        entry["principal_id"] = "canonical-service"
    entry["chain"] = {
        "chain_id": "chain",
        "root_invocation_id": "run-0",
        "snapshot_digest": "d" * 64,
        "root_principal_kind": entry["principal_kind"],
        "root_principal_id": entry["principal_id"],
        "hops": [
            {
                **invocation(str(i)),
                "persona": persona,
                "resolved_model": f"model-{i}",
                "tenant_id": entry["tenant_id"],
                "principal_kind": entry["principal_kind"],
                "principal_id": entry["principal_id"],
                "chain_id": "chain",
                "snapshot_digest": "d" * 64,
                "parent_invocation_id": None if i == 0 else f"run-{i - 1}",
                "has_direct_override": cell == "L21" and i == 0,
                "resolution_source": "explicit-direct"
                if cell == "L21" and i == 0
                else "principal-mapping",
            }
            for i, persona in enumerate(("architect", "developer", "reviewer"))
        ],
    }
    value["cells"][cell] = entry
    return value


def shadow():
    value = safe_evidence()
    value["cells"]["L22"] = {**common(), "status": "pass", "surface": "shadow"}
    value["shadow_comparison"] = {
        "minimum_observations_per_path": 2,
        "rejected_events": [],
        "observations": [
            {
                "dispatch_path": path,
                "mapping_exists": True,
                "resolution_source": "principal-mapping",
                "legacy_model": "model-a",
                "actual_model": "model-a",
                "proposed_model": "model-a",
                "admission_refusal": False,
                "persona": "developer",
                "principal_kind": "human",
                "principal_id": "person-1",
                "tenant_id": "tenant-1",
                "policy_revision": "policy-1",
                "posture_revision": 1,
                "snapshot_digest": "a" * 64,
                "runtime_posture": "report_only",
                "posture_verified": True,
                "phase": "sdk_admission",
                "policy_status": "proposed",
                "invocation_id": f"run-{i}-{j}",
                "attempt": 1,
                "model_decision_id": f"{i * 10 + j:064x}",
            }
            for i, path in enumerate(manifest()["required_shadow_paths"])
            for j in range(2)
        ],
    }
    return value


class TypedEvidenceTests(unittest.TestCase):
    def assertCell(self, value, cell, expected):
        status, report = result(value, cell)
        self.assertEqual(status, expected, report["errors"])
        self.assertFalse(report["complete"])
        self.assertFalse(report["enforcement_ready"])

    def test_complete_distinct_multi_observations_are_accepted(self):
        for cell in ("L1", "L2"):
            self.assertCell(multi(cell), cell, "pass")

    def test_cli_observation_must_match_ui_mapping_for_the_same_owner(self):
        value = multi("L1")
        value["cells"]["L2"] = multi("L2")["cells"]["L2"]
        self.assertCell(value, "L2", "pass")
        value["cells"]["L2"]["invocations"][0]["resolved_model"] = "different"
        self.assertCell(value, "L2", "fail")

    def test_replay_or_missing_per_call_evidence_is_rejected(self):
        for field in ("agent_run_id", "usage_row_id", "provider_request_id"):
            value = multi()
            rows = value["cells"]["L1"]["invocations"]
            rows[1][field] = rows[0][field]
            self.assertCell(value, "L1", "fail")
        value = multi()
        del value["cells"]["L1"]["invocations"][1]["cost_usd"]
        value["cells"]["L1"]["invocation"] = invocation()
        self.assertCell(value, "L1", "fail")

    def test_distinct_chains_retain_owner_digest_and_lineage(self):
        for cell in ("L17", "L18", "L21"):
            self.assertCell(chain(cell), cell, "pass")
            for field, changed in (
                ("principal_id", "approver"),
                ("snapshot_digest", "e" * 64),
                ("parent_invocation_id", "unrelated"),
                ("chain_id", "other"),
            ):
                value = chain(cell)
                value["cells"][cell]["chain"]["hops"][1][field] = changed
                self.assertCell(value, cell, "fail")

    def test_all_descendants_must_drop_the_direct_override(self):
        value = chain("L21")
        value["cells"]["L21"]["chain"]["hops"][2]["has_direct_override"] = True
        self.assertCell(value, "L21", "fail")

    def test_equal_saved_model_is_valid_shadow_selection(self):
        self.assertCell(shadow(), "L22", "pass")

    def test_shadow_replay_missing_data_posture_or_actual_selection_change_fails(self):
        for field, bad in (
            ("actual_model", "other"),
            ("runtime_posture", "enforcing"),
            ("posture_verified", False),
            ("policy_status", "unavailable"),
            ("phase", "bootstrap"),
        ):
            value = shadow()
            value["shadow_comparison"]["observations"][0][field] = bad
            self.assertCell(value, "L22", "fail")
        value = shadow()
        value["shadow_comparison"]["observations"].append(
            copy.deepcopy(value["shadow_comparison"]["observations"][0])
        )
        self.assertCell(value, "L22", "fail")
        value = shadow()
        del value["shadow_comparison"]["rejected_events"]
        self.assertCell(value, "L22", "fail")

    def test_rejected_event_cannot_be_lost_between_report_and_assess(self):
        value = shadow()
        value["shadow_comparison"]["rejected_events"] = [{"reason": "unmapped_path"}]
        self.assertCell(value, "L22", "fail")

    def test_valid_service_observation_and_invalid_numeric_types(self):
        value = safe_evidence()
        value["cells"]["L5"] = {
            **common(),
            "status": "pass",
            "principal_kind": "service_account",
            "surface": "m2m",
            "invocation": invocation(),
        }
        self.assertCell(value, "L5", "pass")
        for field, bad in (
            ("cost_usd", float("nan")),
            ("cost_usd", float("inf")),
            ("input_tokens", True),
            ("output_tokens", -1),
        ):
            modified = copy.deepcopy(value)
            modified["cells"]["L5"]["invocation"][field] = bad
            self.assertCell(modified, "L5", "fail")

    def test_source_and_deployment_revision_must_agree(self):
        value = safe_evidence()
        value["source_revision"] = "b" * 40
        self.assertIn(
            "deployment revision must match source_revision",
            gate.assess(manifest(), value)["errors"],
        )


if __name__ == "__main__":
    unittest.main()
