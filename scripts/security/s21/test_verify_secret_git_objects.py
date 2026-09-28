"""Negative regressions for exact original source-commit adjudication."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "git_object_verifier", Path(__file__).with_name("verify_secret_git_objects.py")
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.fixture
def case(tmp_path):
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
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Synthetic source",
    )
    candidate = git("rev-parse", "HEAD")
    (source / "receipt.json").write_text(
        json.dumps({"release_source": candidate}, indent=2) + "\n"
    )
    git("add", "receipt.json")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Synthetic receipt",
    )
    revision = git("rev-parse", "HEAD")
    digest = hashlib.sha1(candidate.encode(), usedforsecurity=False).hexdigest()
    scan = {
        "results": {
            "receipt.json": [
                {
                    "line_number": 2,
                    "type": "Hex High Entropy String",
                    "hashed_secret": digest,
                }
            ]
        }
    }
    audit = {
        "results": [{"filename": "receipt.json", "lines": [2], "secrets": candidate}]
    }
    receipt = {
        "source_revision": revision,
        "verified_delta": 1,
        "verified_records": [
            {
                "selector": "detect-secrets|receipt.json|ri=0",
                "file": "receipt.json",
                "line": 2,
                "detector": "Hex High Entropy String",
                "candidate_hash_prefix": digest[:16],
                "git_object_type": "commit",
                "context_path": ["release_source"],
            }
        ],
    }

    def run():
        paths = []
        for name, document in [("scan", scan), ("audit", audit), ("receipt", receipt)]:
            path = tmp_path / (name + ".json")
            path.write_text(json.dumps(document))
            paths.append(path)
        verifier.verify(source, *paths)

    return source, scan, audit, receipt, candidate, run


def test_original_commit_and_immutable_line_pass_despite_dirty_worktree(case, capsys):
    source, _, _, _, candidate, run = case
    (source / "receipt.json").write_text("dirty working copy must not supply evidence")
    run()
    output = capsys.readouterr().out
    assert "Verified 1" in output and candidate not in output


@pytest.mark.parametrize(
    "mutation",
    [
        "index",
        "path",
        "line",
        "detector",
        "prefix",
        "full_hash",
        "audit_line",
        "duplicate",
        "field",
        "object_type",
    ],
)
def test_inexact_original_or_context_fails(case, mutation):
    _, scan, audit, receipt, _, run = case
    record = receipt["verified_records"][0]
    if mutation == "index":
        record["selector"] = "detect-secrets|receipt.json|ri=1"
    elif mutation == "path":
        record["selector"] = "detect-secrets|other.json|ri=0"
    elif mutation == "line":
        record["line"] = 1
    elif mutation == "detector":
        record["detector"] = "Secret Keyword"
    elif mutation == "prefix":
        record["candidate_hash_prefix"] = "0" * 16
    elif mutation == "full_hash":
        original = scan["results"]["receipt.json"][0]
        original["hashed_secret"] = original["hashed_secret"][:16] + "0" * 24
    elif mutation == "audit_line":
        audit["results"][0]["lines"] = [1]
    elif mutation == "duplicate":
        receipt["verified_records"].append(dict(record))
        receipt["verified_delta"] = 2
    elif mutation == "field":
        record["context_path"] = ["password"]
    else:
        record["git_object_type"] = "blob"
    with pytest.raises((ValueError, KeyError, IndexError)):
        run()


def test_nonexistent_object_fails_without_using_source_as_substitute(case):
    _, scan, audit, receipt, _, run = case
    candidate = "f" * 40
    digest = hashlib.sha1(candidate.encode(), usedforsecurity=False).hexdigest()
    scan["results"]["receipt.json"][0]["hashed_secret"] = digest
    audit["results"][0]["secrets"] = candidate
    receipt["verified_records"][0]["candidate_hash_prefix"] = digest[:16]
    with pytest.raises(ValueError, match="object unavailable"):
        run()


def test_commit_looking_substring_in_another_field_fails():
    candidate = "1" * 40
    source = json.dumps(
        {"release_source": candidate, "password": candidate}, indent=2
    ).encode()
    record = {"file": "receipt.json", "line": 3, "context_path": ["release_source"]}
    with pytest.raises(ValueError, match="complete revision field"):
        verifier.verify_context(source, record, candidate)


def test_cli_failure_does_not_publish_private_input(case):
    source, _, _, receipt, candidate, run = case
    receipt["verified_records"][0]["context_path"] = [candidate]
    with pytest.raises(ValueError):
        run()
    command = ["python3", str(Path(verifier.__file__)), "--source", str(source)]
    for name in ("scan", "audit", "receipt"):
        command.extend(["--" + name, str(source.parent / (name + ".json"))])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert candidate not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
