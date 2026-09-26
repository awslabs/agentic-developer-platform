"""Reject a digest match without its exact canonical payload and source binding."""

import base64
import copy
import hashlib
import json
import subprocess

import pytest
import verify_fixture_payload_digests as verifier


@pytest.fixture
def case():
    content = b"Synthetic artifact fixture\n"
    canonical = {
        "$fixture": {
            "expect": "valid",
            "schema": "internal-adapters.schema.json#/$defs/artifact_response",
        },
        "artifact_id": "synthetic-artifact",
        "content_type": "text/plain",
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content),
        "content_base64": base64.b64encode(content).decode(),
    }
    path = verifier.PREFIX + "valid/" + verifier.READ
    record = {"file": path, "input_file": path, "output_path": ["content_sha256"]}
    documents = {path: canonical}
    return record, documents


def test_complete_payload_recomputes_digest(case):
    record, documents = case
    assert (
        verifier.derive(record, documents.__getitem__)
        == documents[record["file"]]["content_sha256"]
    )


@pytest.mark.parametrize(
    "mutation", ["payload", "length", "digest", "schema", "expect", "field", "family"]
)
def test_canonical_evidence_mismatch_fails(case, mutation):
    record, documents = case
    canonical = documents[record["file"]]
    if mutation == "payload":
        canonical["content_base64"] = base64.b64encode(
            b"different same-length bytes"
        ).decode()
    elif mutation == "length":
        canonical["byte_length"] += 1
    elif mutation == "digest":
        canonical["content_sha256"] = "0" * 64
    elif mutation in {"schema", "expect"}:
        canonical["$fixture"][mutation] = "unreviewed"
    elif mutation == "field":
        record["output_path"] = ["password"]
    else:
        record["input_file"] = "unrelated.json"
    with pytest.raises(ValueError):
        verifier.derive(record, documents.__getitem__)


def test_corrupted_variant_requires_only_declared_mutation(case):
    record, documents = case
    variant = copy.deepcopy(documents[record["file"]])
    variant["$fixture"]["expect"] = "invalid"
    variant["content_base64"] = "intentionally invalid base64"
    record["file"] = (
        verifier.PREFIX + "invalid/artifact-read-response-invalid-content_base64.json"
    )
    documents[record["file"]] = variant
    assert verifier.derive(record, documents.__getitem__) == variant["content_sha256"]
    variant["artifact_id"] = "different-artifact"
    with pytest.raises(ValueError, match="Unexpected fixture mutation"):
        verifier.derive(record, documents.__getitem__)


def test_same_digest_on_unreviewed_variant_is_not_accepted(case):
    record, documents = case
    original = documents[record["file"]]
    record["file"] = verifier.PREFIX + "invalid/unreviewed.json"
    documents[record["file"]] = copy.deepcopy(original)
    with pytest.raises(ValueError, match="Unsupported fixture variant"):
        verifier.derive(record, documents.__getitem__)


def test_whole_original_join_and_frozen_source(case, tmp_path, capsys):
    record, documents = case
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return (
            subprocess.check_output(
                ["git", *args], cwd=source, stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )

    git("init")
    path = source / record["file"]
    path.parent.mkdir(parents=True)
    text = json.dumps(documents[record["file"]], indent=2) + "\n"
    path.write_text(text)
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Synthetic fixture",
    )
    revision = git("rev-parse", "HEAD")
    candidate = documents[record["file"]]["content_sha256"]
    digest = hashlib.sha1(candidate.encode()).hexdigest()
    line = next(i for i, s in enumerate(text.splitlines(), 1) if candidate in s)
    record.update(
        selector="detect-secrets|" + record["file"] + "|ri=0",
        line=line,
        detector="Hex High Entropy String",
        candidate_hash_prefix=digest[:16],
    )
    scan = {
        "results": {
            record["file"]: [
                {
                    "line_number": line,
                    "type": record["detector"],
                    "hashed_secret": digest,
                }
            ]
        }
    }
    audit = {
        "results": [{"filename": record["file"], "lines": [line], "secrets": candidate}]
    }
    receipt = {
        "source_revision": revision,
        "verified_delta": 1,
        "verified_records": [record],
    }
    paths = [tmp_path / (name + ".json") for name in ("scan", "audit", "receipt")]

    def run():
        for filename, document in zip(paths, (scan, audit, receipt)):
            filename.write_text(json.dumps(document))
        verifier.verify(source, *paths)

    path.write_text("dirty file must not supply evidence")
    run()
    assert candidate not in capsys.readouterr().out
    scan["results"][record["file"]][0]["hashed_secret"] = digest[:16] + "0" * 24
    with pytest.raises(KeyError):
        run()
    scan["results"][record["file"]][0]["hashed_secret"] = digest
    record["selector"] = record["selector"].replace("ri=0", "ri=1")
    with pytest.raises(IndexError):
        run()
    record["selector"] = record["selector"].replace("ri=1", "ri=0")
    record["line"] += 1
    with pytest.raises(ValueError, match="Original scan identity"):
        run()
    record["line"] -= 1
    receipt["source_revision"] = "0" * 40
    with pytest.raises(ValueError, match="Git object unavailable"):
        run()


def test_artifact_reference_requires_exact_identity_length_and_family():
    content = b"Synthetic chunk"
    digest = hashlib.sha256(content).hexdigest()
    canonical = {
        "$fixture": {
            "expect": "valid",
            "schema": "process-protocol.schema.json#/$defs/artifact_chunk_frame",
        },
        "artifact_id": "synthetic-artifact",
        "content_type": "text/plain",
        "content_sha256": digest,
        "total_bytes": len(content),
        "data_base64": base64.b64encode(content).decode(),
    }
    reference = {
        "$fixture": {
            "expect": "valid",
            "schema": "process-protocol.schema.json#/$defs/start_frame",
        },
        "artifacts": [
            {
                "artifact_id": canonical["artifact_id"],
                "content_type": canonical["content_type"],
                "content_sha256": digest,
                "byte_length": len(content),
            }
        ],
    }
    record = {
        "file": verifier.PREFIX + "valid/" + verifier.START,
        "input_file": verifier.PREFIX + "valid/" + verifier.CHUNK,
        "output_path": ["artifacts", 0, "content_sha256"],
    }
    documents = {record["file"]: reference, record["input_file"]: canonical}
    assert verifier.derive(record, documents.__getitem__) == digest
    for field in ("artifact_id", "byte_length", "content_sha256"):
        saved = reference["artifacts"][0][field]
        reference["artifacts"][0][field] = "mismatch"
        with pytest.raises(ValueError, match="reference identity"):
            verifier.derive(record, documents.__getitem__)
        reference["artifacts"][0][field] = saved
    reference["artifacts"].append(dict(reference["artifacts"][0]))
    with pytest.raises(ValueError, match="Ambiguous artifact"):
        verifier.derive(record, documents.__getitem__)
