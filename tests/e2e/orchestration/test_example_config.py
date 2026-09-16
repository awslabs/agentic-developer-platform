"""Keep the shipped example and schema honest (#5156).

The example config is what an operator copies and the schema is what they read,
so both must agree with the loader that actually enforces the rules. Without
these tests the three drift apart silently and the first person to find out is
someone whose copied config is rejected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.orchestration.config import (
    ALLOWED_ENVIRONMENTS,
    BOUND_CEILINGS,
    CONFIG_VERSION,
    load_config,
)

PACKAGE = Path(__file__).resolve().parent
EXAMPLE_PATH = PACKAGE / "config.example.json"
SCHEMA_PATH = PACKAGE / "config.schema.json"


@pytest.fixture(scope="module")
def example() -> dict:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class TestExampleConfig:
    def test_example_is_accepted_by_the_loader(self, tmp_path, example, artifact_dir):
        """The file an operator copies must actually load."""
        example["artifacts"]["directory"] = str(artifact_dir)
        path = tmp_path / "copied.json"
        path.write_text(json.dumps(example), encoding="utf-8")

        config = load_config(path)

        assert config.environment in ALLOWED_ENVIRONMENTS
        assert config.max_resources >= 1

    def test_example_contains_no_credential(self, example):
        """It ships in the repo: every secret must be a reference."""
        for reference in example.get("secret_refs", {}).values():
            assert reference.split(":", 1)[0] in ("secretsmanager", "ssm", "env")

    def test_example_bounds_are_within_the_ceilings(self, example):
        """The default an operator inherits should be modest, not the maximum."""
        for key, ceiling in BOUND_CEILINGS.items():
            assert example["bounds"][key] <= ceiling

    def test_example_declares_no_scenarios(self, example):
        """Scenario adapters arrive with #5157; the example must not imply they exist."""
        assert example["scenarios"] == []


class TestSchemaAgreesWithLoader:
    def test_schema_is_valid_json_with_a_version_const(self, schema):
        assert schema["properties"]["config_version"]["const"] == CONFIG_VERSION

    def test_schema_requires_what_the_loader_requires(self, schema):
        required = set(schema["required"])
        assert required == {
            "config_version",
            "environment",
            "connection",
            "identity",
            "versions",
            "bounds",
            "artifacts",
        }

    def test_schema_environment_enum_matches_the_loader(self, schema):
        assert tuple(schema["properties"]["environment"]["enum"]) == ALLOWED_ENVIRONMENTS

    def test_schema_bound_ceilings_match_the_loader(self, schema):
        """A ceiling raised in code but not here would mislead a reader."""
        properties = schema["properties"]["bounds"]["properties"]
        for key, ceiling in BOUND_CEILINGS.items():
            assert properties[key]["maximum"] == ceiling

    def test_schema_requires_every_bound(self, schema):
        assert set(schema["properties"]["bounds"]["required"]) == set(BOUND_CEILINGS)

    def test_schema_forbids_unknown_keys(self, schema):
        """Mirrors the loader, which rejects a typo'd key rather than ignoring it."""
        assert schema["additionalProperties"] is False
        for section in ("connection", "identity", "versions", "bounds", "artifacts"):
            assert schema["properties"][section]["additionalProperties"] is False

    def test_schema_documents_that_the_loader_is_authoritative(self, schema):
        """The schema cannot express the secret scan, so it must say so."""
        assert "config.py" in schema["description"]
