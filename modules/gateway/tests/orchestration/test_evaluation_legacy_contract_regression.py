"""Fixed legacy documents must not be rehashed or upgraded by the new selector."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.orchestration.evaluation_contract import models, specification
from src.orchestration.evaluation_evidence import specification_hash

ROOT = Path(__file__).resolve().parents[4]


def test_literal_orchestration_evaluation_golden_keeps_schema_and_hashes():
    directory = ROOT / "contracts/orchestration-evaluation/v1"
    golden_bytes = (directory / "evaluation-receipt.golden.json").read_bytes()
    schema_bytes = (directory / "evaluation-receipt.schema.json").read_bytes()
    assert hashlib.sha256(golden_bytes).hexdigest() == "5913652c9d22ce17ef05ce6a1d5b688d0d349c6d04783ec3495a9ab1a5549c7d"
    assert hashlib.sha256(schema_bytes).hexdigest() == "dce26e1354a04d229a6b10b6c84b4b7a6fe88b0b393dc28f76da14e460eb237a"
    golden = json.loads(golden_bytes)
    parsed = specification(golden["specification"])
    assert isinstance(parsed, models().EvaluationSpecification)
    assert specification_hash(parsed) == "e14d873442c6d0b5d40e2988f2343cfb315ef7d7d580f7d3520002f87d4cba92"
    without_marker = dict(golden["specification"])
    del without_marker["evidence_schema"]
    assert specification_hash(specification(without_marker)) == specification_hash(parsed)
    assert models().EvaluationReceipt.model_validate(golden["receipt"]).schema_version == 1
    assert json.loads(schema_bytes) == models().EvaluationReceipt.model_json_schema()


def test_literal_repository_evaluation_keeps_old_schema_and_hash():
    document = {
        "schema_version": 1,
        "acceptance_mode": "machine",
        "evidence_schema": "repository-evaluation/v1",
        "runner": {"adapter": "engine-repository-evidence-v1", "repository": "fixture/harness", "repository_id": 303, "harness_sha256": "a" * 64},
        "predecessors": [{"address": "flow/epic/wave/a", "required_checks": [{"name": "ci", "app_id": 7}]}],
    }
    accepted = specification(document)
    assert accepted.__class__.__name__ == "RepositoryEvaluationSpecification"
    assert specification_hash(accepted) == "457d4a474b2dea7dca8c55e529eb6df03569b1a78870d3a50ddae9622b68f9a6"
    assert accepted.predecessors[0].address == "flow/epic/wave/a"
    assert not hasattr(accepted, "mode")
    assert not hasattr(accepted, "harness")


def test_new_format_never_falls_into_legacy_document_parsing():
    path = ROOT / "contracts/multi-repository-evaluation/v1/evaluation-receipt.golden.json"
    document = json.loads(path.read_text())["specification"]
    assert specification(document).__class__.__name__ == "EvaluationSpecification"
    for version in ("multi-repository-evaluation/v2", "orchestration-evaluation/v2"):
        with pytest.raises(ValueError, match="evaluation_contract_version_unsupported"):
            specification({**document, "evidence_schema": version})
    with pytest.raises(ValidationError):
        specification({key: value for key, value in document.items() if key != "evidence_schema"})
    with pytest.raises(ValidationError):
        specification({**document, "evidence_schema": "orchestration-evaluation/v1"})
