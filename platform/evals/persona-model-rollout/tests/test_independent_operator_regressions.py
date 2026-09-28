"""Offline review cases: synthetic fixtures are never live acceptance evidence."""

from __future__ import annotations

import copy
import unittest

from test_rollout_gate import common, gate, manifest, safe_evidence


def invocation(identity="1"):
    return {
        "provider_request_id": "request-" + identity,
        "model_output_sha256": "a" * 64,
        "usage_row_id": "usage-" + identity,
        "agent_run_id": "run-" + identity,
        "cost_usd": 0.01,
        "input_tokens": 10,
        "output_tokens": 5,
        "real_model_output": True,
    }


def result(value, cell):
    report = gate.assess(manifest(), value)
    return next(row["status"] for row in report["results"] if row["id"] == cell), report


def shadow_value(mapping_exists=False):
    value = safe_evidence()
    value["cells"]["L22"] = {**common(), "status": "pass"}
    value["shadow_comparison"] = {
        "minimum_observations_per_path": 3,
        "rejected_events": [],
        "observations": [
            {
                "dispatch_path": path,
                "mapping_exists": mapping_exists,
                "legacy_model": "model-a",
                "proposed_model": "model-a",
                "admission_refusal": False,
                "persona": "developer",
                "principal_kind": "human",
                "tenant_id": "tenant-1",
                "policy_revision": "policy-1",
            }
            for path in manifest()["required_shadow_paths"]
            for _ in range(3)
        ],
    }
    return value


class IndependentOperatorRegressions(unittest.TestCase):
    def test_l1_top_level_invocation_cannot_replace_each_observation(self):
        value = safe_evidence()
        value["cells"]["L1"] = {
            **common(),
            "status": "pass",
            "invocation": invocation(),
            "invocations": [
                {"persona": persona, "resolved_model": "model-" + str(i)}
                for i, persona in enumerate(("developer", "architect", "reviewer"))
            ],
        }
        self.assertEqual(result(value, "L1")[0], "fail")

    def test_l17_top_level_invocation_cannot_replace_each_hop(self):
        value = safe_evidence()
        value["cells"]["L17"] = {
            **common(),
            "status": "pass",
            "invocation": invocation(),
            "chain": {
                "chain_id": "chain-1",
                "snapshot_digest": "d" * 64,
                "root_principal_kind": "human",
                "hops": [{"snapshot_digest": "d" * 64} for _ in range(2)],
            },
        }
        self.assertEqual(result(value, "L17")[0], "fail")

    def test_l17_one_replayed_invocation_is_not_a_two_hop_chain(self):
        value = safe_evidence()
        hop = {**invocation(), "snapshot_digest": "d" * 64}
        value["cells"]["L17"] = {
            **common(),
            "status": "pass",
            "chain": {
                "chain_id": "chain-1",
                "snapshot_digest": "d" * 64,
                "root_principal_kind": "human",
                "hops": [hop, copy.deepcopy(hop)],
            },
        }
        self.assertEqual(result(value, "L17")[0], "fail")

    def test_l5_rejects_malformed_identity_and_observation_values(self):
        invalid = [
            ("account_id", "000000000000"),
            ("principal_kind", "human"),
            ("principal_id", ""),
            ("tenant_id", ""),
            ("timestamp_utc", "not-a-date"),
            ("input_tokens", -10),
            ("output_tokens", -5),
            ("cost_usd", float("nan")),
        ]
        for field, bad in invalid:
            with self.subTest(field=field):
                value = safe_evidence()
                cell = {
                    **common(),
                    "principal_kind": "service",
                    "surface": "m2m",
                    "status": "pass",
                    "invocation": invocation(),
                }
                if field in ("input_tokens", "output_tokens", "cost_usd"):
                    cell["invocation"][field] = bad
                else:
                    cell[field] = bad
                value["cells"]["L5"] = cell
                self.assertEqual(result(value, "L5")[0], "fail")

    def test_l22_cannot_count_repeated_rows_without_observation_identity(self):
        self.assertEqual(result(shadow_value(), "L22")[0], "fail")

    def test_l22_equal_saved_choice_is_not_itself_an_unexplained_divergence(self):
        value = shadow_value(mapping_exists=True)
        # This isolates the comparison rule; future schema checks may require
        # more evidence, but equality with a saved choice is not a divergence.
        _, report = result(value, "L22")
        self.assertFalse(
            any("unexplained divergence" in error for error in report["errors"])
        )


if __name__ == "__main__":
    unittest.main()
