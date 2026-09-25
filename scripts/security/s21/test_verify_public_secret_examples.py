"""Evidence integrity checks use synthetic values and a temporary Git repository."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "public_verifier", Path(__file__).with_name("verify_public_secret_examples.py")
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def fixture(
    tmp_path,
    candidate="PUBLIC-EXAMPLE-123",
    source_text=None,
    document_value=None,
    kind="aws_public_example",
):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    text = source_text or f"value = {candidate!r}\n"
    (source / "example.py").write_text(text)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "synthetic frozen evidence",
        ],
        cwd=source,
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    document = tmp_path / "public.html"
    document.write_text(f"<p>Example: <code>{document_value or candidate}</code></p>")
    hashed = hashlib.sha1(candidate.encode()).hexdigest()
    scan = tmp_path / "scan.json"
    scan.write_text(
        json.dumps(
            {
                "results": {
                    "example.py": [
                        {"line_number": 1, "type": "Synthetic", "hashed_secret": hashed}
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
                    {"filename": "example.py", "secrets": candidate, "lines": {"1": ""}}
                ]
            }
        )
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "source_revision": revision,
                "verified_delta": 1,
                "public_document": {
                    "sha256": hashlib.sha256(document.read_bytes()).hexdigest()
                },
                "verified_records": [
                    {
                        "selector": "synthetic|ri=0",
                        "file": "example.py",
                        "line": 1,
                        "detector": "Synthetic",
                        "candidate_hash_prefix": hashed[:16],
                        "kind": kind,
                        "literal_lines": [1],
                    }
                ],
            }
        )
    )
    return source, scan, audit, receipt, document


def test_exact_complete_public_code_example(tmp_path):
    verifier.verify(*fixture(tmp_path))


def test_public_document_substring_is_not_an_example(tmp_path):
    with pytest.raises(AssertionError, match="complete official"):
        verifier.verify(
            *fixture(tmp_path, document_value="prefix-PUBLIC-EXAMPLE-123-suffix")
        )


def test_tampered_public_document_is_rejected(tmp_path):
    inputs = fixture(tmp_path)
    inputs[-1].write_text("changed document")
    with pytest.raises(AssertionError):
        verifier.verify(*inputs)


def test_mutable_checkout_does_not_replace_frozen_blob(tmp_path):
    inputs = fixture(tmp_path)
    (inputs[0] / "example.py").write_text("changed checkout")
    verifier.verify(*inputs)


@pytest.mark.parametrize("body", ["", "payload", "\\npayload"])
def test_delimiter_must_have_no_payload(tmp_path, body):
    marker = "BEGIN RSA " + "PRIVATE KEY"
    inputs = fixture(
        tmp_path,
        candidate=marker,
        source_text=f"value = {'-----' + marker + '-----' + body!r}",
        kind="public_pem_delimiter",
    )
    if body:
        with pytest.raises(AssertionError, match="delimiter-only"):
            verifier.verify(*inputs)
    else:
        verifier.verify(*inputs)


def test_adjacent_literal_payload_is_not_a_public_delimiter(tmp_path):
    marker = "BEGIN RSA " + "PRIVATE KEY"
    inputs = fixture(
        tmp_path,
        candidate=marker,
        source_text=f"value = {'-----' + marker + '-----'!r} 'payload'",
        kind="public_pem_delimiter",
    )
    with pytest.raises(AssertionError, match="delimiter-only"):
        verifier.verify(*inputs)


def test_duplicate_selector_is_rejected(tmp_path):
    inputs = fixture(tmp_path)
    receipt = json.loads(inputs[3].read_text())
    receipt["verified_records"] *= 2
    receipt["verified_delta"] = 2
    inputs[3].write_text(json.dumps(receipt))
    with pytest.raises(AssertionError):
        verifier.verify(*inputs)
