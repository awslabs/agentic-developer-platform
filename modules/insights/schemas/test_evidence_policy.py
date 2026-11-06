import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


SCHEMA = json.loads((Path(__file__).parent / "evidence-policy/v1/descriptor.schema.json").read_text())


class PolicyDescriptorContractTest(unittest.TestCase):
    def test_gateway_derived_policy_is_separately_versioned(self):
        Draft202012Validator.check_schema(SCHEMA)
        root = {
            "tenant_id": "tenant", "project_id": "project", "source_connection_id": "github-connection",
            "record_type": "issue", "record_id": "1", "record_revision": "native-r1",
        }
        descriptor = {
            "schema_version": "evidence-policy/1.0", "observation_sha256": "a" * 64,
            "item_sha256": "b" * 64, "descriptor_sha256": "c" * 64,
            "envelope": {
                "envelope_version": 1, "identity": root, "realm_id": "realm", "realm_revision": 2,
                "descriptor_digest": "d" * 64, "audience": "private",
                "required_policies": [
                    {"ref": "source", "revision": 1, "visibility": "shared", "audience_expression": "team"},
                    {"ref": "ci", "revision": 1, "visibility": "private", "audience_expression": "maintainers"},
                ],
                "dependency_identities": [root],
                "dependency_versions": [{"identity": root, "revision": 1, "metadata_digest": "e" * 64}],
            },
        }
        validator = Draft202012Validator(SCHEMA)
        self.assertTrue(validator.is_valid(descriptor))
        for edit in (lambda value: value.update(schema_version="registry/1.0"),
                     lambda value: value.update(storage_path="foreign-cell"),
                     lambda value: value["envelope"].update(dependency_versions=[])):
            candidate = json.loads(json.dumps(descriptor))
            edit(candidate)
            self.assertFalse(validator.is_valid(candidate))


if __name__ == "__main__":
    unittest.main()
