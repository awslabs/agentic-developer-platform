"""Reject unsupported or mismatched derived-digest evidence."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location(
    "derived_verifier", Path(__file__).with_name("verify_derived_secret_digests.py")
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def projection_fixture():
    key = ["test-scanner", "report.json", 7]
    digest = hashlib.sha256(
        (json.dumps(key, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    source = {
        "existing_owner_mappings": [
            {"owner": "S01", "record_count": 1, "record_keys_sha256": digest}
        ]
    }
    inputs = {
        "dispositions": [
            {"owner": "S01", "tool": key[0], "artifact": key[1], "result_index": key[2]}
        ]
    }
    record = {
        "file": "summary.json",
        "input_file": "records.json",
        "kind": "record_key_projection",
        "summary_path": ["existing_owner_mappings", 0],
    }
    return record, source, inputs, digest


def test_exact_record_projection():
    record, source, inputs, expected = projection_fixture()
    assert (
        verifier.derive(
            record, {"summary.json": source, "records.json": inputs}.__getitem__
        )
        == expected
    )


@pytest.mark.parametrize("mutation", ["owner", "count", "key", "duplicate"])
def test_projection_rejects_wrong_record_population(mutation):
    record, source, inputs, _ = projection_fixture()
    if mutation == "owner":
        inputs["dispositions"][0]["owner"] = "S02"
    elif mutation == "count":
        source["existing_owner_mappings"][0]["record_count"] = 2
    elif mutation == "key":
        inputs["dispositions"][0]["result_index"] = 8
    else:
        inputs["dispositions"] *= 2
        source["existing_owner_mappings"][0]["record_count"] = 2
    with pytest.raises(AssertionError):
        verifier.derive(
            record, {"summary.json": source, "records.json": inputs}.__getitem__
        )


def test_embedded_artifact_requires_correct_path_and_bytes():
    text = "synthetic public artifact"
    expected = hashlib.sha256(text.encode()).hexdigest()
    document = {
        "artifact_text": {"report.txt": text},
        "receipt": {"artifacts": [{"path": "report.txt", "sha256": expected}]},
    }
    record = {
        "file": "fixture.json",
        "input_file": "fixture.json",
        "kind": "embedded_artifact_text",
        "input_paths": [["artifact_text", "report.txt"]],
    }
    assert verifier.derive(record, lambda _: document) == expected
    document["artifact_text"]["report.txt"] = "changed artifact"
    with pytest.raises(AssertionError):
        verifier.derive(record, lambda _: document)


def test_pricing_manifest_detects_changed_document_digest():
    sources = {"pricing_page": {"sha256": "a" * 64}, "token_map": {"sha256": "b" * 64}}
    manifest = {"pricing_page_sha256": "a" * 64, "token_map_sha256": "b" * 64}
    expected = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    document = {
        "provenance": {
            "claude_audit": {"sources": sources, "combined_source_sha256": expected}
        }
    }
    record = {
        "file": "snapshot.json",
        "input_file": "snapshot.json",
        "kind": "pricing_source_manifest",
        "input_path": ["provenance", "claude_audit", "sources"],
    }
    assert verifier.derive(record, lambda _: document) == expected
    sources["token_map"]["sha256"] = "c" * 64
    with pytest.raises(AssertionError):
        verifier.derive(record, lambda _: document)
