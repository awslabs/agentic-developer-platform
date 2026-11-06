"""Retained build evidence must be complete and bound to the qualified source."""

import hashlib
import io
import json
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "emit_image_evidence.py"
emitter = runpy.run_path(str(SCRIPT))
SOURCE = "a" * 40


@pytest.fixture
def evidence(tmp_path):
    hashes = {"modules/tools/adapters/tool_adapters/server.py": "b" * 64}
    report = {
        "source_revision": SOURCE,
        "source_files_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "passed": True,
        "cleanup_confirmed": True,
        "docker_reported_image_size_bytes": 161656644,
        "image_size_metric": "docker.image.inspect.Size",
    }
    for name, value in {
        "image-smoke.json": report,
        "source-files.json": hashes,
        "sbom.cdx.json": {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []},
    }.items():
        (tmp_path / name).write_text(json.dumps(value))
    return tmp_path


def invoke(path):
    output = io.StringIO()
    emitter["emit"](source_revision=SOURCE, evidence_dir=path, output=output)
    return output.getvalue().splitlines()


def test_complete_evidence_roundtrips_with_source_and_content_hashes(evidence):
    records = invoke(evidence)
    assert len(records) == 3
    report_record = json.loads(records[0].removeprefix(emitter["PREFIX"]))["data"]
    assert report_record["image_size_metric"] == "docker.image.inspect.Size"
    assert "image_size_bytes_uncompressed" not in report_record
    for line in records:
        assert line.startswith(emitter["PREFIX"])
        record = json.loads(line.removeprefix(emitter["PREFIX"]))
        assert record["source_revision"] == SOURCE
        assert record["data"] == json.loads((evidence / record["file"]).read_text())
        canonical = json.dumps(record["data"], sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(canonical.encode()).hexdigest() == record["sha256"]


@pytest.mark.parametrize("name", emitter["FILES"])
def test_missing_evidence_cannot_pass(evidence, name):
    (evidence / name).unlink()
    with pytest.raises(OSError):
        invoke(evidence)


@pytest.mark.parametrize(
    "field,value", [("source_revision", "c" * 40), ("passed", False), ("cleanup_confirmed", False)]
)
def test_wrong_source_or_failed_qualification_cannot_pass(evidence, field, value):
    path = evidence / "image-smoke.json"
    report = json.loads(path.read_text())
    path.write_text(json.dumps({**report, field: value}))
    with pytest.raises(ValueError):
        invoke(evidence)


def test_changed_source_inventory_is_not_retained_as_same_proof(evidence):
    (evidence / "source-files.json").write_text("{}")
    with pytest.raises(ValueError, match="inventory mismatch"):
        invoke(evidence)


def test_oversized_inventory_is_bounded_before_output(evidence):
    (evidence / "sbom.cdx.json").write_text("x" * (emitter["MAX_FILE_BYTES"] + 1))
    output = io.StringIO()
    with pytest.raises(ValueError, match="exceeds bound"):
        emitter["emit"](source_revision=SOURCE, evidence_dir=evidence, output=output)
    assert output.getvalue() == ""
