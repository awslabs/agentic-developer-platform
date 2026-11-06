import base64
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


SCHEMA = json.loads((Path(__file__).parent / "devlake-observation/v1/observation.schema.json").read_text())


class ObservationContractTest(unittest.TestCase):
    def test_sealed_native_observation_and_unlinked_source(self):
        Draft202012Validator.check_schema(SCHEMA)
        reference = {
            "CellID": "cell", "TenantID": "tenant", "ProjectID": "project", "Tool": "github",
            "ConnectionID": "github-connection", "Scope": "repo", "Kind": "issue", "NativeID": "1",
        }
        qualified = {
            "tenant_id": "tenant", "project_id": "project", "source_connection_id": "github-connection",
            "record_type": "issue", "record_id": "1", "record_revision": "native-r1",
        }
        record = {
            "schema_version": "devlake-observation/1.0", "mapping_version": "devlake-observation/1.0",
            "subject_ref": qualified, "source_ref": qualified, "source_native_key": reference,
            "source_revision": "native-r1",
            "source_snapshot": {
                "batch_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "generation": 1,
                "manifest_sha256": "a" * 64, "item_sha256": "b" * 64,
            },
            "binding": {"id": "github-binding", "generation": 3, "revision": 2},
            "native_bytes": base64.b64encode(b'{"body":"approved"}').decode(),
            "native_sha256": "2a7f39d04a552d83edc15c18b627292104b1eacc7d01b378826f7829a7f66866",
            "normalized_bytes": base64.b64encode(b'{"status":"open"}').decode(),
            "normalized_sha256": "bc99066a8b813e763201c114d47dd9300710ba0acf1c4e522ae78e2c5c473b87",
            "field_map_version": "field-map-v1", "source_schema_version": "schema-v2",
            "source_policy_version": "policy-v1", "relationships": [], "resolved_relationships": [],
            "observation_status": "observation_only",
        }
        validator = Draft202012Validator(SCHEMA)
        self.assertTrue(validator.is_valid(record))
        for edit in (lambda value: value.update(storage_path="other-cell"),
                     lambda value: value.update(schema_version="registry/1.0"),
                     lambda value: value["subject_ref"].update(project_id="")):
            candidate = json.loads(json.dumps(record))
            edit(candidate)
            self.assertFalse(validator.is_valid(candidate))


if __name__ == "__main__":
    unittest.main()
