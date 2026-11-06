"""Validate real API and installed CLI reads against the published result schema."""

import importlib.util
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).parent
RESULT = json.loads((ROOT / "results/v1/business_context.schema.json").read_text())
VALIDATOR = Draft202012Validator(RESULT, format_checker=FormatChecker())
SPEC = importlib.util.spec_from_file_location(
    "i23_result_contract_fixture", ROOT.parent / "tests/test_i23_registry.py"
)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class ResultsIntegrationTests(unittest.TestCase):
    def test_installed_cli_and_api_preserve_decimal_costs_and_remainders(self):
        snapshot, _, _, _ = fixture.fixed_snapshot()
        cost = snapshot["facts"][1]
        product = fixture.registry.reference("product", "example-product", source="adp.profile")
        cases = (
            ("0.00000001", None, "0.00000001"),
            ("0.000000030000000003", "0.000000010000000001", "0.000000020000000002"),
            ("1.00000001", "1.00000000", "0.00000001"),
            ("0.00000001", "0.00000001", "0.00000000"),
        )
        with fixture.installed_gateway(snapshot) as (binary, environment, token, gateway_url):
            for amount, allocated, remainder in cases:
                with self.subTest(amount=amount, allocated=allocated):
                    cost["amount"] = amount
                    snapshot["allocations"] = [] if allocated is None else [{
                        "allocation_id": "example-allocation",
                        "allocation_policy_version": "example-policy-1",
                        "allocation_basis": "example-approved-basis",
                        "source_ref": cost["subject_ref"],
                        "target_ref": product,
                        "currency": "USD",
                        "amount": allocated,
                    }]
                    result = fixture.read_result(
                        binary, environment, token, gateway_url, fixture.registry.EVENT_TIME
                    )
                    VALIDATOR.validate(result)
                    self.assertEqual(result["cost_by_currency"], {"USD": amount})
                    self.assertEqual(result["allocated_by_currency"], {} if allocated is None else {"USD": allocated})
                    self.assertEqual(result["unallocated_by_currency"], {"USD": remainder})

    def test_installed_cli_and_api_accept_schema_valid_rfc3339_instants(self):
        snapshot, _, _, _ = fixture.fixed_snapshot()
        with fixture.installed_gateway(snapshot) as (binary, environment, token, gateway_url):
            for as_of in (
                fixture.registry.EVENT_TIME,
                "2026-10-19T13:30:00+01:30",
                "2026-10-19T08:30:00-03:30",
                "2026-10-19T12:00:00.123456789Z",
                "2026-10-19T13:30:00.123456789+01:30",
            ):
                with self.subTest(as_of=as_of):
                    result = fixture.read_result(binary, environment, token, gateway_url, as_of)
                    VALIDATOR.validate(result)
                    self.assertEqual(result["as_of"], as_of)
                    self.assertEqual(result["work_count"], 1)


if __name__ == "__main__":
    unittest.main()
