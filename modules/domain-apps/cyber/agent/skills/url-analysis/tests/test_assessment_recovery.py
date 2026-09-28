"""Rejected source-only drafts must remain publishable and correctable."""

import copy
import json

import domain_investigation as cli
import pytest
from research_case import verify_case


@pytest.mark.parametrize(
    "invalid_references",
    [{}, {"source_ids": ["corroboration-999"]}, {"evidence_ids": ["obs-999"]}],
)
def test_source_only_draft_survives_rejection_close_and_correction(
    tmp_path, invalid_references
):
    out = tmp_path / "case"
    cli.prepare(
        out,
        "https://fixture.test/",
        "Synthetic report recovery",
        lookup_fn=lambda *_: {"kind": "archive_index", "status": "available"},
    )
    finding = {
        "kind": "benign_context",
        "statement": "Synthetic archived context supports the assessment.",
        "basis": "reported",
        "source_ids": ["corroboration-001"],
    }
    bad = {
        "kind": "other",
        "statement": "A draft claim with invalid references.",
        "basis": "reported",
        **invalid_references,
    }
    candidate = {
        "verdict": "clean",
        "assessor": "synthetic",
        "findings": [finding, {**finding, "evidence_ids": []}, bad],
    }
    original = copy.deepcopy(candidate)
    with pytest.raises(ValueError, match="reference|unknown"):
        cli.finish(out, candidate, "Synthetic stop")

    closed = cli.close(out, "Preserve the interrupted draft")
    assert closed["assessment"]["verdict"] is None
    retained = closed["assessment"]["findings"]
    assert len(retained) == 1
    assert retained[0]["evidence_ids"] == []
    assert retained[0]["evidence_refs"] == []
    assert retained[0]["source_ids"] == ["corroboration-001"]
    assert closed["assessment_attempts"][0]["assessment"] == original
    assert candidate == original
    assert verify_case(out) >= 4
    assert "Synthetic archived context" in (out / "report.html").read_text()

    corrected = {**candidate, "findings": [finding]}
    cli.finish(out, corrected, "Corrected draft references verified")
    saved = json.loads((out / "case.json").read_text())
    assert saved["assessment"]["verdict"] == "clean"
    assert len(saved["assessment_attempts"]) == 1
    assert verify_case(out) >= 4
