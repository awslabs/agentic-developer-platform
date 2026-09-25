"""Later evaluation registration must not manufacture qualification evidence."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "task_contract_registration", ROOT / "scripts/task-api/check-contracts.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.mark.parametrize(
    "fault", [None, "missing", "malformed", "failed", "wrong_criterion"]
)
def test_later_evaluation_requires_its_actual_pass_record(monkeypatch, fault):
    manifest_path = ROOT / "docs/task-api/evaluation-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    proof_path = ROOT / manifest["criteria"]["V1-01"]["qualification_report"]
    original = Path.read_text
    proof = json.loads(proof_path.read_text())
    if fault == "missing":
        manifest["criteria"]["V1-01"]["qualification_report"] = (
            "docs/task-api/missing-proof-fixture.json"
        )
    elif fault == "failed":
        proof["criteria"]["V1-01"]["status"] = "FAIL"
    elif fault == "wrong_criterion":
        del proof["criteria"]["V1-01"]

    def read(path, *args, **kwargs):
        if path == manifest_path:
            return json.dumps(manifest)
        if path == proof_path:
            return "{" if fault == "malformed" else json.dumps(proof)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    results = checker.Results()
    checker.check_manifest(results)
    gate = next(
        row
        for row in results.checks
        if row["check"]
        == "runnable evaluations require versioned criterion PASS evidence"
    )
    assert gate["passed"] == (fault is None)
