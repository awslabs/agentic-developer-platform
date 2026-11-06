"""Versioned verifier contracts survive byte changes without changing accepted hashes."""

import pytest
from pydantic import ValidationError

from src.orchestration.repository_evaluation_contract import Runner, canonical, native_harness_compatible, native_specification


def evaluation():
    return {
        "evidence_schema": "repository-evaluation/v1",
        "runner": {
            "adapter": "engine-repository-evidence-v1",
            "repository": "example/project",
            "repository_id": 123,
            "harness_sha256": "a" * 64,
        },
        "predecessors": [{"address": "flow/epic/wave/story", "required_checks": [{"name": "Tests", "app_id": 1}]}],
    }


def test_legacy_runner_serialization_and_fingerprint_are_preserved():
    original = evaluation()["runner"]
    runner = Runner.model_validate(original)
    assert runner.harness_policy == "contract"
    assert canonical(runner.model_dump(mode="json")) == canonical(original)
    assert native_harness_compatible(native_specification(evaluation()), "b" * 64)


def test_exact_policy_is_serialized_and_enforced():
    document = evaluation()
    document["runner"]["harness_policy"] = "exact"
    spec = native_specification(document)
    assert spec.model_dump(mode="json")["runner"] == document["runner"]
    assert native_harness_compatible(spec, "a" * 64)
    assert not native_harness_compatible(spec, "b" * 64)


@pytest.mark.parametrize("field,value", [("adapter", "engine-repository-evidence-v2"), ("harness_policy", "ignore")])
def test_unsupported_runner_contract_is_refused(field, value):
    document = evaluation()
    document["runner"][field] = value
    with pytest.raises(ValidationError):
        native_specification(document)


def test_unknown_evidence_version_is_refused():
    document = evaluation()
    document["evidence_schema"] = "repository-evaluation/v2"
    with pytest.raises(ValidationError):
        native_specification(document)
