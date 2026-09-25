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


@pytest.mark.parametrize("kind", ["evaluation_specification", "pricing_decision", "pricing_rates"])
@pytest.mark.parametrize("mutation", [None, "input", "output", "context", "path"])
def test_canonical_json_requires_exact_input_and_output_binding(kind, mutation):
    payload = {"synthetic": "public data"}
    source = {}
    record = {"kind": kind, "file": "fixture.json", "input_file": "fixture.json"}
    if kind == "evaluation_specification":
        source = {"specification": payload, "receipt": {}}
        record.update(input_path=["specification"], output_path=["receipt", "specification_hash"])
        output = source["receipt"]
        output_key = "specification_hash"
        inputs = source
    elif kind == "pricing_decision":
        source = {"decision": dict(payload)}
        record.update(input_path=["decision"], output_path=["decision", "content_sha256"])
        output = source["decision"]
        output_key = "content_sha256"
        inputs = source
    else:
        payload = [payload]
        inputs = {"rates": payload}
        record.update(input_file="policy/snapshots/2026-09-12.1.json", input_path=["rates"], output_path=["generation_hash"])
        output = source
        output_key = "generation_hash"
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    output[output_key] = expected
    record["context_evidence"] = [{"json_path": record["output_path"]}]
    if mutation == "input":
        target = verifier.at(inputs, record["input_path"])
        if isinstance(target, list):
            target.append({"changed": True})
        else:
            target["changed"] = True
    elif mutation == "output":
        output[output_key] = "0" * 64
    elif mutation == "context":
        record["context_evidence"] = []
    elif mutation == "path":
        record["output_path"] = ["unrelated_field"]
    documents = {record["file"]: source, record["input_file"]: inputs}
    if mutation:
        with pytest.raises(AssertionError):
            verifier.derive(record, documents.__getitem__)
    else:
        assert verifier.derive(record, documents.__getitem__) == expected
