import copy

import pytest
from benchmark import (
    model_input,
    require_aws_runtime,
    s3_location,
    score,
    validate_manifest,
)


def manifest():
    labels = [
        "phishing",
        "phishing",
        "phishing",
        "legitimate",
        "legitimate",
        "unavailable",
    ]
    return {
        "schema_version": "cyber-evaluation/1",
        "cases": [
            {
                "id": str(i),
                "label": label,
                "split": "holdout",
                "group_id": f"group-{i}",
                "case_uri": f"s3://evidence/case-{i}/case.json",
                "sha256": str(i) * 64,
                "reviewed_by": "Synthetic fixture author",
                "reviewed_at": "2026-01-01T00:00:00Z",
            }
            for i, label in enumerate(labels)
        ],
    }


def results():
    verdicts = [
        "malicious",
        "inconclusive",
        "no_adverse_behavior_observed",
        "suspicious",
        "no_adverse_behavior_observed",
        "inconclusive",
    ]
    return [
        {
            "id": str(i),
            "verdict": verdict,
            "evidence_valid": True,
            "successful_pages": int(i not in (1, 5)),
            "elapsed_seconds": 2,
        }
        for i, verdict in enumerate(verdicts)
    ]


@pytest.mark.parametrize(
    "negative", ["no_specific_concern", "no_adverse_behavior_observed"]
)
def test_metrics_preserve_unavailable_and_abstained_cases_in_denominators(negative):
    cases = results()
    cases[4]["verdict"] = negative
    value = score(manifest(), cases, "holdout")
    assert value["precision"] == 0.5
    assert value["recall_all_phishing"] == 1 / 3
    assert value["recall_reachable_phishing"] == 0.5
    assert value["false_positive_rate"] == 0.5
    assert value["availability_rate"] == 4 / 6
    assert value["inconclusive_rate"] == 2 / 6
    assert value["human_evidence_correctness"] is None


def test_missing_duplicate_and_invalid_results_cannot_inflate_metrics():
    for actual in (results()[:-1], results() + [results()[0]]):
        with pytest.raises(ValueError, match="every selected case"):
            score(manifest(), actual, "holdout")
    actual = results()
    actual[0]["evidence_valid"] = False
    with pytest.raises(ValueError, match="Unsupported"):
        score(manifest(), actual, "holdout")


def test_campaign_and_artifact_leakage_is_rejected():
    data = manifest()
    data["cases"][1]["group_id"] = data["cases"][0]["group_id"]
    data["cases"][1]["split"] = "development"
    with pytest.raises(ValueError, match="leaked"):
        validate_manifest(data)
    data = manifest()
    data["cases"][1]["sha256"] = data["cases"][0]["sha256"]
    with pytest.raises(ValueError, match="Duplicate snapshots"):
        validate_manifest(data)


def test_model_input_excludes_reference_labels_previous_assessments_and_reputation():
    case = {
        "target_url": "https://synthetic.test",
        "observations": [],
        "probes": [],
        "assessment": {"verdict": "malicious"},
        "label": "phishing",
        "corroboration": [{"source": "PhishTank"}],
        "reviews": ["private reference reasoning"],
    }
    value = model_input(copy.deepcopy(case))
    assert set(value) == {"objective", "target_url", "observations", "collection"}
    assert "PhishTank" not in str(value) and "malicious" not in str(value)


def test_dataset_operations_refuse_local_execution_and_local_paths(monkeypatch):
    for key in (
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "CODEBUILD_BUILD_ID",
        "AWS_EXECUTION_ENV",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="local dataset downloads are disabled"):
        require_aws_runtime()
    for uri in (
        "/tmp/dataset.json",
        "https://example.test/data",
        "s3://bucket",
        "s3://bucket/key?secret=x",
    ):
        with pytest.raises(ValueError):
            s3_location(uri)


def test_model_failure_is_pending_separately_from_inconclusive():
    cases = results()
    cases[1].update(verdict=None, evidence_valid=False, model_failure=True)
    value = score(manifest(), cases, "holdout")
    assert value["inconclusive_rate"] == 1 / 6
    assert value["assessment_pending_rate"] == 1 / 6
    assert value["recall_all_phishing"] == 1 / 3
