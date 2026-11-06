"""Offline S01 measurement invariants; no live sampling or cost proof."""

import json
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def evaluate_freshness(lags, observed_hours, limits):
    if len(lags) != limits["controlled_updates"] or observed_hours < limits["minimum_observation_hours"]:
        return "incomplete"
    if any(lag is None or lag < 0 or not math.isfinite(lag) for lag in lags):
        return "unavailable"
    rank = math.ceil(0.95 * len(lags)) - 1
    return "pass" if sorted(lags)[rank] <= limits["freshness_p95_minutes"] else "fail"


def coverage(matched, expected, native_oracle):
    if not native_oracle or expected == 0:
        return None
    if matched > expected or matched < 0:
        raise ValueError("extra or negative matched records")
    return matched / expected


class MeasurementPlanTests(unittest.TestCase):
    def setUp(self):
        self.limits = json.loads((ROOT / "pilot-contract.json").read_text())["limits"]
        self.plan = (ROOT / "measurement-plan.md").read_text()

    def test_sample_nearest_rank_and_incomplete_updates(self):
        self.assertEqual(evaluate_freshness([14] * 19 + [30], 48, self.limits), "pass")
        self.assertEqual(evaluate_freshness([14] * 18 + [16, 30], 48, self.limits), "fail")
        self.assertEqual(evaluate_freshness([14] * 19, 48, self.limits), "incomplete")
        self.assertEqual(evaluate_freshness([14] * 20, 47.9, self.limits), "incomplete")
        self.assertEqual(evaluate_freshness([14] * 19 + [None], 48, self.limits), "unavailable")
        self.assertIn("nearest-rank p95 is item `ceil(0.95 × 20) = 19`", self.plan)

    def test_missing_native_denominator_or_cost_is_unavailable(self):
        self.assertIsNone(coverage(0, 0, False))
        self.assertIsNone(coverage(0, 0, True))
        self.assertEqual(coverage(19, 20, True), 0.95)
        with self.assertRaises(ValueError):
            coverage(21, 20, True)
        self.assertIn("Missing rates, ownership, billing data", self.plan)
        self.assertIn("cost **unavailable**", self.plan)

    def test_proposed_resource_limits_match_the_pilot_contract(self):
        self.assertEqual(self.limits["total_incremental_budget_usd"], 100)
        self.assertEqual(self.limits["worker_vcpu_per_job"], 2)
        self.assertEqual(self.limits["worker_memory_mib_per_job"], 4096)
        self.assertEqual(self.limits["incremental_storage_gib_per_cell"], 10)
        self.assertEqual(self.limits["staging_bytes_per_cell_batch"], 100 * 1024 * 1024)
        self.assertIn("any collection or resource provisioning", self.plan)


class MeasurementReviewProcedureTests(unittest.TestCase):
    def test_scn03_independent_oracle_and_synthetic_control(self):
        document = (ROOT / "verification.md").read_text()
        procedure = document.split("## S01 SCN-03: independent measurement-plan review", 1)[1].split("## Fixture and determinism contract", 1)[0]
        for control in ("planned manual acceptance-owner review", "both", "20 intended native updates", "48 hours", "independently of the connector", "item 19 (nearest-rank p95)", "<=15 minutes", "19 lags of 14 minutes", "one of 16", "missing twentieth update", "unavailable", "private/foreign", "itemized incremental charges", "two concurrent collections", "100,000 staging rows/100 MiB", "qualified ledger finalizer", "private evidence ref", "acceptance owner signs"):
            with self.subTest(control=control):
                self.assertIn(control, procedure)
        limits = json.loads((ROOT / "pilot-contract.json").read_text())["limits"]
        self.assertEqual(evaluate_freshness([14] * 19 + [30], 48, limits), "pass")
        self.assertEqual(evaluate_freshness([14] * 18 + [16, 30], 48, limits), "fail")
        self.assertEqual(evaluate_freshness([14] * 19 + [None], 48, limits), "unavailable")


if __name__ == "__main__":
    unittest.main()
