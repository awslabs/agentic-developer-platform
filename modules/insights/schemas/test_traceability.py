import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import check_traceability


class TraceabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.schemas = root / "schemas"
        shutil.copytree(check_traceability.SCHEMA_DIR, self.schemas)
        self.document = root / "lineage-schema.md"
        shutil.copyfile(check_traceability.SOURCE_DOCUMENT, self.document)

    def run_check(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(check_traceability, "SCHEMA_DIR", self.schemas):
            with patch.object(check_traceability, "SOURCE_DOCUMENT", self.document):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = check_traceability.main()
        return status, stdout.getvalue(), stderr.getvalue()

    def add_property(self, record_type, field):
        path = self.schemas / f"{record_type}.schema.json"
        schema = json.loads(path.read_text())
        schema["properties"][field] = {"type": "string"}
        path.write_text(json.dumps(schema))

    def test_all_properties_have_source_locations(self):
        status, stdout, stderr = self.run_check()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        for path in self.schemas.glob("*.schema.json"):
            record_type = path.name.removesuffix(".schema.json")
            for field in json.loads(path.read_text())["properties"]:
                self.assertIn(f"{record_type}/properties/{field} -> {self.document}:", stdout)

    def test_undocumented_property_fails_for_every_schema(self):
        for record_type in check_traceability.SCHEMA_SOURCES:
            with self.subTest(record_type=record_type):
                self.add_property(record_type, "unmapped_field_probe")
                status, _, stderr = self.run_check()
                self.assertEqual(status, 1)
                self.assertIn(f"{record_type}/properties/unmapped_field_probe: no source field", stderr)

    def test_fields_from_other_entities_or_sections_fail(self):
        for record_type, field in (
            ("story", "task_id"), ("envelope", "product_id"),
            ("test_run", "scenario_ids"), ("deployment", "incident_id"),
        ):
            with self.subTest(record_type=record_type, field=field):
                self.add_property(record_type, field)
                status, _, stderr = self.run_check()
                self.assertEqual(status, 1)
                self.assertIn(f"{record_type}/properties/{field}: no source field", stderr)

    def test_composed_schema_properties_are_checked(self):
        path = self.schemas / "story.schema.json"
        schema = json.loads(path.read_text())
        schema["allOf"].append({"properties": {"unmapped_field_probe": {"type": "string"}}})
        path.write_text(json.dumps(schema))
        status, _, stderr = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("story/allOf/1/properties/unmapped_field_probe: no source field", stderr)

    def test_cli_rejects_undocumented_property(self):
        self.add_property("story", "unmapped_field_probe")
        root = Path(self.directory.name) / "repo"
        schemas = root / "modules/insights/schemas"
        shutil.copytree(self.schemas, schemas / "lineage/v1")
        command = schemas / "check_traceability.py"
        shutil.copyfile(check_traceability.ROOT / command.name, command)
        document = root / "docs/engineering-insights/lineage-schema.md"
        document.parent.mkdir(parents=True)
        shutil.copyfile(self.document, document)
        result = subprocess.run([sys.executable, str(command)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("story/properties/unmapped_field_probe: no source field in section 3.3", result.stderr)

    def test_removing_source_field_fails_even_if_mentioned_in_prose(self):
        document = self.document.read_text().replace("| `title` | Add saved cards | Tracker |", "")
        document = document.replace("### 3.3 Story", "### 3.3 Story\n\nA `title` mentioned outside a field row.")
        self.document.write_text(document)
        status, _, stderr = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("story/properties/title: no source field in section 3.3", stderr)

    def test_quarantine_requires_its_source_requirement(self):
        document = self.document.read_text().replace("Quarantine missing required evidence with a reason.", "")
        self.document.write_text(document)
        status, _, stderr = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("quarantine/properties/reason: no source field in section 6.1", stderr)

    def test_missing_source_document_fails(self):
        self.document.unlink()
        status, _, stderr = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("lineage-schema.md", stderr)

    def test_unknown_schema_fails(self):
        (self.schemas / "unknown.schema.json").write_text('{"properties": {}}')
        status, _, stderr = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("unknown: no designated source section", stderr)

    def test_missing_schema_fails(self):
        (self.schemas / "envelope.schema.json").unlink()
        status, _, stderr = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("envelope.schema.json", stderr)

    def test_ci_checks_traceability_and_watches_source_document(self):
        workflow = (check_traceability.ROOT.parents[2] / ".github/workflows/insights-ci.yml").read_text()
        self.assertIn("- run: python modules/insights/schemas/check_traceability.py", workflow)
        self.assertIn("      - 'docs/engineering-insights/lineage-schema.md'", workflow)


if __name__ == "__main__":
    unittest.main()
