"""Protect record grain, immutable references and the v1 compatibility boundary."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import check_traceability_v2
import validate


class RequirementsLineageTests(unittest.TestCase):
    def fixture(self, kind):
        return json.loads((validate.ROOT / "fixtures/v2" / f"{kind}.json").read_text())

    def errors(self, record):
        return validate.validate_record(record, record["record_type"], "v2")

    def test_every_fixture_validates_and_required_fields_cannot_be_omitted(self):
        for kind in validate.V2_RECORD_TYPES:
            record = self.fixture(kind)
            with self.subTest(kind=kind):
                self.assertEqual(self.errors(record), [])
                schema = validate.load_schema(kind, "v2")
                for field in schema["required"]:
                    invalid = {k: v for k, v in record.items() if k != field}
                    self.assertTrue(self.errors(invalid), field)
                self.assertTrue(self.errors({**record, "invented_field": True}))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(validate.main(["--version", "v2"]), 0)

    def test_versions_are_not_silently_reinterpreted(self):
        old = json.loads((validate.FIXTURE_DIR / "test_run.json").read_text())
        self.assertEqual(validate.validate_record(old, "test_run"), [])
        self.assertTrue(validate.validate_record(old, "test_run", "v2"))
        self.assertTrue(validate.validate_record(self.fixture("test_run"), "test_run"))
        with self.assertRaises(ValueError):
            validate.load_schema("test_run", "v3")

    def test_run_result_and_selection_have_distinct_grains(self):
        run = self.fixture("test_run")
        self.assertTrue(self.errors({**run, "test_id": "TEST-001", "result": "passed"}))
        self.assertTrue(self.errors({**run, "selection_snapshot": None}))
        self.assertEqual(
            self.errors(
                {**run, "selection_snapshot": None, "selection_status": "unknown"}
            ),
            [],
        )
        planned = self.fixture("run_test")
        self.assertTrue(self.errors({**planned, "criterion_refs": []}))
        result = self.fixture("test_result")
        self.assertTrue(self.errors({**result, "attempt": 0}))
        self.assertTrue(self.errors({**result, "outcome": "not_run"}))
        self.assertTrue(self.errors({**result, "duration_ms": -1}))
        self.assertTrue(self.errors({**result, "ended_at": "2026-10-09T10:00:00Z"}))

    def test_references_are_scoped_typed_and_pinned(self):
        record = self.fixture("run_test")
        for field, value in [
            ("tenant_id", "foreign"),
            ("project_id", "foreign"),
            ("record_type", "test_result"),
            ("record_revision", "latest"),
        ]:
            invalid = copy.deepcopy(record)
            invalid["criterion_refs"][0][field] = value
            with self.subTest(field=field):
                self.assertTrue(self.errors(invalid))
        # A registered tracker and a CI source can differ within the same project.
        record["criterion_refs"][0]["source_connection_id"] = "another-source"
        self.assertEqual(self.errors(record), [])
        story = self.fixture("story_revision")
        story["source_snapshot"]["version_id"] = "null"
        self.assertTrue(self.errors(story))

    def test_mapping_types_and_scenario_fields_are_enforced(self):
        mapping = self.fixture("coverage_mapping")
        mapping["from_ref"]["record_type"] = "test_definition"
        self.assertTrue(self.errors(mapping))
        module = self.fixture("coverage_definition")
        self.assertTrue(self.errors({**module, "definition_kind": "scenario"}))

    def test_acceptance_requires_evidence_and_policy_identity(self):
        record = self.fixture("acceptance_assessment")
        self.assertTrue(self.errors({**record, "result_refs": [], "artifact_refs": []}))
        self.assertTrue(self.errors({**record, "policy_snapshot": None}))
        manual = {
            **record,
            "assessor_type": "human",
            "policy_snapshot": None,
            "result_refs": [],
            "artifact_refs": [record["policy_snapshot"]],
        }
        self.assertEqual(self.errors(manual), [])
        self.assertEqual(
            self.errors({**record, "verdict": "inconclusive", "result_refs": []}), []
        )

    def test_bundle_has_real_relationships_and_preserves_failed_retry(self):
        rows = json.loads(
            (validate.ROOT / "fixtures/v2/traceability-bundle.json").read_text()
        )
        fields = (
            "tenant_id",
            "project_id",
            "source_connection_id",
            "record_type",
            "record_id",
            "record_revision",
        )
        keys = {tuple(row[f] for f in fields): row for row in rows}
        self.assertEqual(len(keys), len(rows))

        def resolve(value):
            if isinstance(value, dict):
                if set(value) == set(fields):
                    self.assertIn(tuple(value[f] for f in fields), keys)
                for child in value.values():
                    resolve(child)
            elif isinstance(value, list):
                for child in value:
                    resolve(child)

        for row in rows:
            self.assertEqual(self.errors(row), [], row["record_type"])
            resolve(row)
        passed = next(r for r in rows if r["record_type"] == "test_result")
        failed = {**passed, "outcome": "failed"}
        retried = {**passed, "record_id": passed["record_id"] + "/retry", "attempt": 2}
        self.assertEqual(self.errors(failed), [])
        self.assertEqual(self.errors(retried), [])
        self.assertEqual(failed["run_test_ref"], retried["run_test_ref"])
        self.assertNotEqual(failed["record_id"], retried["record_id"])

    def test_traceability_rejects_undocumented_field_and_missing_document(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check_traceability_v2.main(), 0)
        with tempfile.TemporaryDirectory() as directory:
            import shutil

            target = Path(directory) / "v2"
            shutil.copytree(check_traceability_v2.SCHEMA_DIR, target)
            path = target / "story_revision.schema.json"
            schema = json.loads(path.read_text())
            schema["properties"]["undocumented_probe"] = {"type": "string"}
            path.write_text(json.dumps(schema))
            with patch.object(check_traceability_v2, "SCHEMA_DIR", target):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(check_traceability_v2.main(), 1)
            with patch.object(
                check_traceability_v2, "SOURCE_DOCUMENT", Path(directory) / "missing.md"
            ):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(check_traceability_v2.main(), 1)


if __name__ == "__main__":
    unittest.main()
