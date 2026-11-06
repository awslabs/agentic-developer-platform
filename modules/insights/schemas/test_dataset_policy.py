"""Descriptor contract for qualified shared result snapshots."""

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


SCHEMA = json.loads((Path(__file__).parent / "results/v1/dataset-policy.schema.json").read_text())


def shared_descriptor():
    identity = {
        "tenant_id": "example-tenant", "project_id": "example-project",
        "source_connection_id": "example-source", "record_type": "work",
        "record_id": "example-work", "record_revision": "example-revision-1",
    }
    return {
        "schema_version": "results-policy/1.0", "tenant_id": "example-tenant",
        "project_id": "example-project", "dataset_version": "example-dataset-1",
        "snapshot_sha256": "a" * 64, "policy_epoch": 2,
        "envelopes": [{
            "envelope_version": 1, "identity": identity, "realm_id": "example-shared-realm",
            "realm_revision": 3, "descriptor_digest": "b" * 64, "audience": "shared",
            "required_policies": [
                {"ref": name, "revision": 1, "visibility": "shared",
                 "audience_expression": "current:project-readers"}
                for name in ("source", "evidence")
            ],
            "dependency_identities": [identity],
            "dependency_versions": [{"identity": identity, "revision": 1, "metadata_digest": "c" * 64}],
        }],
    }


class DatasetPolicyContractTests(unittest.TestCase):
    def test_shared_descriptor_and_denied_unsafe_selectors(self):
        Draft202012Validator.check_schema(SCHEMA)
        validator = Draft202012Validator(SCHEMA)
        expected = shared_descriptor()
        self.assertTrue(validator.is_valid(expected))
        for change in (
            lambda item: item.pop("snapshot_sha256"),
            lambda item: item.update(storage_path="results/v1/foreign/published.json"),
            lambda item: item.update(policy_epoch=0),
            lambda item: item["envelopes"][0].update(audience="private"),
            lambda item: item["envelopes"][0]["required_policies"][1].update(
                audience_expression="current:team-readers"
            ),
            lambda item: item["envelopes"][0]["dependency_versions"].clear(),
        ):
            with self.subTest(change=change):
                altered = copy.deepcopy(expected)
                change(altered)
                self.assertFalse(validator.is_valid(altered))


if __name__ == "__main__":
    unittest.main()
