import json
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import validate
from jsonschema import Draft202012Validator


class LineageSchemaTests(unittest.TestCase):
    def test_all_fixtures_validate(self):
        for record_type in validate.RECORD_TYPES:
            with self.subTest(record_type=record_type):
                record = json.loads((validate.FIXTURE_DIR / f"{record_type}.json").read_text())
                self.assertEqual(validate.validate_record(record, record_type), [])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(validate.main([]), 0)

    def test_missing_fixture_fails_default_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(validate, "FIXTURE_DIR", Path(directory)):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(validate.main([]), 1)

    def test_project_id_is_required_and_named_by_cli(self):
        record = json.loads((validate.FIXTURE_DIR / "story.json").read_text())
        del record["project_id"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "story.json"
            path.write_text(json.dumps(record))
            result = subprocess.run(
                [sys.executable, str(validate.ROOT / "validate.py"), "--schema", "story", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project_id", result.stderr)

    def test_rejects_wrong_version_invalid_time_and_undefined_fields(self):
        record = json.loads((validate.FIXTURE_DIR / "story.json").read_text())
        for key, value in (("schema_version", "2.0"), ("event_time", "yesterday"), ("extra", "drift")):
            with self.subTest(key=key):
                invalid = {**record, key: value}
                self.assertTrue(validate.validate_record(invalid, "story"))

    def test_consumer_can_load_every_schema_from_module_files(self):
        for record_type in validate.SCHEMA_TYPES:
            with self.subTest(record_type=record_type):
                schema = validate.load_schema(record_type)
                Draft202012Validator.check_schema(schema)
                self.assertTrue(schema["properties"])
        envelope = json.loads((validate.SCHEMA_DIR / "envelope.schema.json").read_text())
        Draft202012Validator.check_schema(envelope)
        self.assertIn("project_id", envelope["required"])

    def test_quarantine_requires_reason_and_scope(self):
        record = json.loads((validate.FIXTURE_DIR / "story.json").read_text())
        standard = json.loads((validate.SCHEMA_DIR / "envelope.schema.json").read_text())
        quarantine = {key: record[key] for key in standard["required"]}
        self.assertTrue(validate.validate_record(quarantine, "quarantine"))
        quarantine["reason"] = "unknown source reference"
        self.assertEqual(validate.validate_record(quarantine, "quarantine"), [])

    def test_ci_runs_unit_tests_and_all_fixtures(self):
        workflow = (validate.ROOT.parents[2] / ".github/workflows/insights-ci.yml").read_text()
        self.assertIn("python -m unittest discover -s modules/insights/schemas", workflow)
        self.assertIn("python modules/insights/schemas/validate.py", workflow)


if __name__ == "__main__":
    unittest.main()
