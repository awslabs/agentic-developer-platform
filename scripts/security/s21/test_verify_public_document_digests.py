"""Public bytes must bind to the exact original checksum field and source URL."""

import gzip
import hashlib
import json

import pytest
import verify_public_document_digests as verifier

URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/example.md"


@pytest.fixture
def public_document(tmp_path):
    content = b"Synthetic public documentation fixture\n"
    compressed = gzip.compress(content, mtime=0)
    descriptor = {
        "url": URL,
        "file": "public-document-01.bin.gz",
        "status": 200,
        "bytes": len(content),
        "compressed_sha512": hashlib.sha512(compressed).hexdigest(),
        "content_sha512": hashlib.sha512(content).hexdigest(),
    }
    (tmp_path / descriptor["file"]).write_bytes(compressed)
    return tmp_path, descriptor, content


def test_retained_document_reproduces_digest(public_document):
    directory, descriptor, content = public_document
    assert (
        verifier.load_document(directory, descriptor)
        == hashlib.sha256(content).hexdigest()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "archive",
        "expanded_hash",
        "length",
        "bounds",
        "userinfo",
        "host",
        "scheme",
        "path",
        "status",
    ],
)
def test_document_tampering_or_unsafe_descriptor_fails(public_document, mutation):
    directory, descriptor, _ = public_document
    if mutation == "archive":
        (directory / descriptor["file"]).write_bytes(
            gzip.compress(b"modified document", mtime=0)
        )
    elif mutation == "expanded_hash":
        descriptor["content_sha512"] = "0" * 128
    elif mutation == "length":
        descriptor["bytes"] += 1
    elif mutation == "bounds":
        descriptor["bytes"] = verifier.MAX_BYTES + 1
    elif mutation == "userinfo":
        descriptor["url"] = "https://username:password@docs.aws.amazon.com/example"
    elif mutation == "host":
        descriptor["url"] = "https://example.invalid/document"
    elif mutation == "scheme":
        descriptor["url"] = URL.replace("https:", "http:")
    elif mutation == "path":
        descriptor["file"] = "../public-document-01.bin.gz"
    else:
        descriptor["status"] = 302
    with pytest.raises(ValueError):
        verifier.load_document(directory, descriptor)


def source_case():
    candidate = hashlib.sha256(b"synthetic public bytes").hexdigest()
    text = json.dumps(
        {
            "sources": [
                {"url": URL, "sha256": candidate},
                {"url": URL, "sha256": candidate},
            ]
        },
        indent=2,
    )
    record = {"context_path": ["sources", 0, "sha256"], "line": 5}
    return text, record, candidate, {"url": URL}


def test_exact_scalar_path_and_line_bind_document():
    text, record, candidate, descriptor = source_case()
    verifier.verify_context(text, record, candidate, descriptor, candidate)


@pytest.mark.parametrize("mutation", ["line", "path", "url", "candidate", "bytes"])
def test_repeated_digest_cannot_supply_other_source_binding(mutation):
    text, record, candidate, descriptor = source_case()
    digest = candidate
    if mutation == "line":
        record["line"] = 9
    elif mutation == "path":
        record["context_path"][1] = 1
    elif mutation == "url":
        descriptor["url"] = URL + "-different"
    elif mutation == "candidate":
        candidate = "0" * 64
    else:
        digest = "0" * 64
    with pytest.raises(ValueError):
        verifier.verify_context(text, record, candidate, descriptor, digest)


def test_json_string_escapes_containers_and_nonstring_scalars():
    source = '{"a": [null, true, 3, "escaped\\nline"], "empty": {}}'
    assert verifier.json_values(source) == {
        ("a", 0): (None, 1),
        ("a", 1): (True, 1),
        ("a", 2): (3, 1),
        ("a", 3): ("escaped\nline", 1),
    }


def test_duplicate_source_field_rejected():
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        verifier.json_values('{"sha256": "one", "sha256": "two"}')


def test_verifier_requires_full_original_join_and_index(public_document, monkeypatch):
    directory, descriptor, content = public_document
    artifacts = directory / "public-pricing-documents"
    artifacts.mkdir()
    (directory / descriptor["file"]).rename(artifacts / descriptor["file"])
    candidate = hashlib.sha256(content).hexdigest()
    digest = hashlib.sha1(candidate.encode()).hexdigest()
    text = json.dumps({"url": URL, "sha256": candidate}, indent=2)
    revision = "1" * 40

    def frozen_git(source, *args):
        assert source == directory
        assert args == ("show", revision + ":source.json")
        return text.encode()

    monkeypatch.setattr(verifier, "git", frozen_git)
    record = {
        "selector": "detect-secrets|source.json|ri=0",
        "file": "source.json",
        "line": 3,
        "detector": "Hex High Entropy String",
        "candidate_hash_prefix": digest[:16],
        "context_path": ["sha256"],
        "public_document": descriptor["file"],
    }
    scan = {
        "results": {
            "source.json": [
                {"line_number": 3, "type": record["detector"], "hashed_secret": digest}
            ]
        }
    }
    audit = {
        "results": [{"filename": "source.json", "lines": [3], "secrets": candidate}]
    }
    receipt = {
        "source_revision": revision,
        "verified_delta": 1,
        "public_documents": [descriptor],
        "verified_records": [record],
    }
    paths = [directory / (name + ".json") for name in ("scan", "audit", "receipt")]

    def run():
        for path, data in zip(paths, (scan, audit, receipt)):
            path.write_text(json.dumps(data))
        verifier.verify(directory, *paths)

    run()
    scan["results"]["source.json"][0]["hashed_secret"] = digest[:16] + "0" * 24
    with pytest.raises(KeyError):
        run()
    scan["results"]["source.json"][0]["hashed_secret"] = digest
    record["selector"] = "detect-secrets|source.json|ri=1"
    with pytest.raises(IndexError):
        run()
    record["selector"] = "detect-secrets|other.json|ri=0"
    with pytest.raises(ValueError, match="selector mismatch"):
        run()
    record["selector"] = "detect-secrets|source.json|ri=0"
    receipt["verified_records"].append(dict(record))
    receipt["verified_delta"] = 2
    with pytest.raises(ValueError, match="Duplicate original selector"):
        run()
