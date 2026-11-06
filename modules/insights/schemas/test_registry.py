"""The registry admission contract is separate from raw lineage."""

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

import validate


SCHEMA = json.loads(
    (Path(__file__).parent / "registry/v1/admission.schema.json").read_text()
)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


class RegistryContractTests(unittest.TestCase):
    def record(self):
        reference = {
            "tenant_id": "example-tenant",
            "project_id": "example-project",
            "source_connection_id": "adp.profile",
            "record_type": "product",
            "record_id": "example-product",
            "record_revision": "example-event-1",
        }
        return {
            "schema_version": "registry/1.0",
            "tenant_id": "example-tenant",
            "project_id": "example-project",
            "publisher_id": "insights-publisher",
            "source_connection_id": "adp.profile",
            "source_event_id": "example-event-1",
            "source_event_time": "2026-10-19T10:00:01Z",
            "effective_at": "2026-10-19T10:00:00Z",
            "recorded_at": "2026-10-19T10:00:01Z",
            "ingested_at": "2026-10-19T10:00:02Z",
            "mapping_version": "profile-registry-1",
            "normalized_revision": "example-event-1:profile-registry-1",
            "record_type": "product",
            "subject_ref": reference,
            "source_ref": reference,
            "source_revisions": [reference],
            "source_snapshot": {"product": {"name": "Example"}},
        }

    def test_separate_contract_requires_immutable_versions_and_times(self):
        Draft202012Validator.check_schema(SCHEMA)
        record = self.record()
        self.assertEqual(list(VALIDATOR.iter_errors(record)), [])
        for field in SCHEMA["required"]:
            with self.subTest(field=field):
                self.assertTrue(list(VALIDATOR.iter_errors({key: value for key, value in record.items() if key != field})))
        for field, bad in [("schema_version", "2.0"), ("source_event_time", "yesterday"), ("normalized_revision", "latest"), ("record_type", "task")]:
            with self.subTest(field=field):
                self.assertTrue(list(VALIDATOR.iter_errors({**record, field: bad})))
        self.assertTrue(list(VALIDATOR.iter_errors({**record, "source_ref": {**record["source_ref"], "record_revision": "latest"}})))

    def test_raw_lineage_never_admits_registry_records(self):
        for version, types in (("v1", validate.SCHEMA_TYPES), ("v2", validate.V2_RECORD_TYPES)):
            self.assertNotIn("registry", types)
            self.assertNotIn("context_record", types)
            with self.assertRaises(ValueError):
                validate.load_schema("registry", version)
            for record_type in types:
                with self.subTest(version=version, record_type=record_type):
                    self.assertTrue(validate.validate_record(self.record(), record_type, version))


if __name__ == "__main__":
    unittest.main()
