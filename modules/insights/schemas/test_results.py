"""Contract checks for the versioned, authorized business-context read."""

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).parent / "results/v1"
RESULT = json.loads((ROOT / "business_context.schema.json").read_text())
ERROR = json.loads((ROOT / "error.schema.json").read_text())
RESULT_VALIDATOR = Draft202012Validator(RESULT, format_checker=FormatChecker())
ERROR_VALIDATOR = Draft202012Validator(ERROR, format_checker=FormatChecker())


def reference(kind, identifier):
    return {
        "tenant_id": "example-tenant", "project_id": "example-project",
        "source_connection_id": "example-source", "record_type": kind,
        "record_id": identifier, "record_revision": "example-revision-1",
    }


def complete_result():
    work = reference("work", "example-work")
    product = reference("product", "example-product")
    initiative = reference("initiative", "example-initiative")
    contribution = reference("contribution", "example-contribution")
    cost = reference("cost", "example-cost")
    relationship = {
        "relationship_id": "example-link", "relationship_revision": "example-link-1",
        "mapping_version": "example-map-1", "from_ref": work, "to_ref": product,
    }
    return {
        "schema_version": "results/1.0", "view": "business_context",
        "project_id": "example-project", "product_id": "example-product",
        "as_of": "2026-10-19T12:00:00Z", "dataset_version": "example-dataset-1",
        "mapping_versions": ["example-map-1"], "coverage": "complete",
        "grouping": "historical", "work_count": 1,
        "cost_by_currency": {"USD": "10.00"},
        "allocated_by_currency": {"USD": "6.25"},
        "unallocated_by_currency": {"USD": "3.75"},
        "contexts": [{
            "subject_ref": work, "grouping": "historical",
            "effective_at": "2026-10-19T12:00:00Z",
            "product_refs": [product], "portfolio_ids": ["example-commerce", "example-shared"],
            "initiative_refs": [initiative], "contribution_refs": [contribution],
            "relationship_versions": [relationship],
        }],
        "associations": [
            {"subject_ref": work, "portfolio_ids": ["example-commerce", "example-shared"]},
            {"subject_ref": cost, "portfolio_ids": ["example-commerce", "example-shared"]},
        ],
        "allocation_versions": [{
            "allocation_id": "example-allocation", "allocation_policy_version": "example-policy-1",
            "allocation_basis": "example-approved-basis", "source_ref": cost,
            "target_ref": initiative,
        }],
        "next_cursor": None,
    }


class ResultsContractTests(unittest.TestCase):
    def test_complete_view_is_strict_and_preserves_original_references(self):
        Draft202012Validator.check_schema(RESULT)
        result = complete_result()
        self.assertEqual(list(RESULT_VALIDATOR.iter_errors(result)), [])
        self.assertEqual(result["associations"][0]["subject_ref"]["record_type"], "work")
        for field in RESULT["required"]:
            with self.subTest(missing=field):
                changed = copy.deepcopy(result)
                del changed[field]
                self.assertTrue(list(RESULT_VALIDATOR.iter_errors(changed)))

    def test_rejects_partial_fabricated_or_unpinned_results(self):
        for field, bad in (
            ("coverage", "incomplete"), ("work_count", -1),
            ("cost_by_currency", {"USD": 10.0}),
            ("cost_by_currency", {"USD": "NaN"}),
            ("dataset_version", "latest"), ("mapping_versions", []),
            ("as_of", "2026-10-19T12:00:00"), ("extra", "secret"),
        ):
            with self.subTest(field=field, bad=bad):
                changed = complete_result()
                changed[field] = bad
                self.assertTrue(list(RESULT_VALIDATOR.iter_errors(changed)))
        changed = complete_result()
        changed["contexts"][0]["subject_ref"]["record_revision"] = "latest"
        self.assertTrue(list(RESULT_VALIDATOR.iter_errors(changed)))
        changed = complete_result()
        changed["contexts"][0]["relationship_versions"][0]["source_name"] = "private"
        self.assertTrue(list(RESULT_VALIDATOR.iter_errors(changed)))
        changed = complete_result()
        changed["contexts"] *= 101
        self.assertTrue(list(RESULT_VALIDATOR.iter_errors(changed)))

    def test_errors_only_admit_generic_codes_and_messages(self):
        Draft202012Validator.check_schema(ERROR)
        for code in ERROR["properties"]["error"]["properties"]["code"]["enum"]:
            with self.subTest(code=code):
                self.assertEqual(list(ERROR_VALIDATOR.iter_errors(
                    {"error": {"code": code, "message": "Result unavailable"}})), [])
        for invalid in (
            {"error": {"code": "forbidden", "message": "Result unavailable"}},
            {"error": {"code": "not_found", "message": "Result unavailable", "source_name": "private"}},
            {"error": {"code": "projection_pending", "message": "Result unavailable"}, "work_count": 0},
            {"error": {"code": "not_found", "message": ""}},
        ):
            self.assertTrue(list(ERROR_VALIDATOR.iter_errors(invalid)))


if __name__ == "__main__":
    unittest.main()
