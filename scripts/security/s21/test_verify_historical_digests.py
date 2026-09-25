"""Historical digest evidence must resolve exact ancestral Git blobs."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "digest_verifier", Path(__file__).with_name("verify_nonsecret_artifact_digests.py")
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", *args], cwd=source, stderr=subprocess.DEVNULL, text=True
        ).strip()

    def commit(message):
        git("add", ".")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            message,
        )
        return git("rev-parse", "HEAD")

    git("init", "-q")
    artifact = source / "artifact.txt"
    artifact.write_text("historical reviewed bytes")
    candidate = hashlib.sha256(artifact.read_bytes()).hexdigest()
    historical = commit("historical fixture")
    blob = git("rev-parse", historical + ":artifact.txt")
    artifact.write_text("changed artifact in frozen source")
    (source / "manifest.json").write_text(json.dumps({"sha256": candidate}))
    frozen = commit("frozen fixture")
    candidate_hash = hashlib.sha1(candidate.encode()).hexdigest()
    scan = tmp_path / "scan.json"
    scan.write_text(
        json.dumps(
            {
                "results": {
                    "manifest.json": [
                        {
                            "line_number": 1,
                            "type": "Synthetic",
                            "hashed_secret": candidate_hash,
                        }
                    ]
                }
            }
        )
    )
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "filename": "manifest.json",
                        "secrets": candidate,
                        "lines": {"1": ""},
                    }
                ]
            }
        )
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "source_revision": frozen,
                "verified_delta": 1,
                "verified_records": [
                    {
                        "selector": "synthetic|ri=0",
                        "file": "manifest.json",
                        "line": 1,
                        "detector": "Synthetic",
                        "candidate_hash_prefix": candidate_hash[:16],
                        "matching_artifacts": ["artifact.txt"],
                        "historical_artifacts": [
                            {
                                "historical_path": "artifact.txt",
                                "source_revision": historical,
                                "blob_oid": blob,
                            }
                        ],
                        "context_evidence": [{"json_path": ["sha256"]}],
                    }
                ],
            }
        )
    )
    return (source, scan, audit, receipt), git, commit


def test_historical_blob_is_verified_when_frozen_file_differs(tmp_path):
    inputs, _, _ = fixture(tmp_path)
    verifier.verify(*inputs)


def test_wrong_blob_identity_rejected(tmp_path):
    inputs, _, _ = fixture(tmp_path)
    receipt = json.loads(inputs[-1].read_text())
    receipt["verified_records"][0]["historical_artifacts"][0]["blob_oid"] = "0" * 40
    inputs[-1].write_text(json.dumps(receipt))
    with pytest.raises(AssertionError, match="recorded blob"):
        verifier.verify(*inputs)


def test_missing_historical_provenance_cannot_use_unrelated_frozen_bytes(tmp_path):
    inputs, _, _ = fixture(tmp_path)
    receipt = json.loads(inputs[-1].read_text())
    del receipt["verified_records"][0]["historical_artifacts"]
    inputs[-1].write_text(json.dumps(receipt))
    with pytest.raises(AssertionError, match="artifact SHA256"):
        verifier.verify(*inputs)


def test_future_commit_is_not_accepted_as_historical_evidence(tmp_path):
    inputs, git, commit = fixture(tmp_path)
    receipt = json.loads(inputs[-1].read_text())
    (inputs[0] / "artifact.txt").write_text("historical reviewed bytes")
    future = commit("future reintroduction")
    git("checkout", "--detach", receipt["source_revision"])
    receipt["verified_records"][0]["historical_artifacts"][0]["source_revision"] = (
        future
    )
    inputs[-1].write_text(json.dumps(receipt))
    with pytest.raises(AssertionError, match="not an ancestor"):
        verifier.verify(*inputs)
