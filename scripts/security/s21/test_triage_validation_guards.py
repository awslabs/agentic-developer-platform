"""The real S20 attestation CLI must refuse tampered evidence under optimization."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TRIAGE = Path("docs/security/runs/2026-09-21/triage")


@pytest.fixture
def evidence_checkout(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
    # Read existing immutable Git objects without copying history or contacting a
    # remote. The fixture repository has independent config and no credentials.
    common = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True
    ).strip()
    objects = (ROOT / common / "objects").resolve()
    (checkout / ".git/objects/info/alternates").write_text(str(objects) + "\n")
    shutil.copytree(ROOT / TRIAGE, checkout / TRIAGE)
    return checkout


def run_validator(checkout, mode):
    environment = dict(os.environ)
    environment.pop("PYTHONOPTIMIZE", None)
    return subprocess.run(
        [sys.executable, *mode, str(checkout / TRIAGE / "validate.py")],
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("mode", [[], ["-O"], ["-OO"]])
def test_intact_attestation_passes(evidence_checkout, mode):
    result = run_validator(evidence_checkout, mode)
    assert result.returncode == 0, result.stderr
    assert "S20 validation passed: 860/860 source records" in result.stdout


@pytest.mark.parametrize("mode", [[], ["-O"], ["-OO"]])
@pytest.mark.parametrize(
    "tamper",
    [
        "unaccounted",
        "severity_count",
        "source_bytes",
        "ownership_schema",
        "ownership_digest",
        "readme_count",
    ],
)
def test_tampered_attestation_never_prints_pass(evidence_checkout, mode, tamper):
    directory = evidence_checkout / TRIAGE
    if tamper in {"unaccounted", "severity_count"}:
        path = directory / "s20-dispositions.json"
        value = json.loads(path.read_text())
        if tamper == "unaccounted":
            value["reconciliation"]["unaccounted"] = 1
        else:
            first = next(iter(value["severity_counts"]))
            value["severity_counts"][first] += 1
        path.write_text(json.dumps(value))
    elif tamper == "source_bytes":
        path = directory / "s20-source-records.json"
        path.write_bytes(path.read_bytes() + b"\n")
    elif tamper in {"ownership_schema", "ownership_digest"}:
        path = directory / "s20-ownership-evidence.json"
        value = json.loads(path.read_text())
        value[
            "schema_version" if tamper == "ownership_schema" else "record_key_digest"
        ] = "untrusted"
        path.write_text(json.dumps(value))
    else:
        path = directory / "README.md"
        text = path.read_text()
        # Alter a parsed table count while preserving valid markdown and fields.
        lines = text.splitlines()
        for index, line in enumerate(lines):
            normalized = line.replace("**", "")
            if (
                normalized.startswith("| `")
                and len(normalized.strip("|").split("|")) == 7
            ):
                cells = normalized.strip("|").split("|")
                cells[1] = str(int(cells[1]) + 1)
                lines[index] = "|" + "|".join(cells) + "|"
                break
        else:
            raise AssertionError("fixture table not found")
        path.write_text("\n".join(lines) + "\n")
    result = run_validator(evidence_checkout, mode)
    assert result.returncode != 0, "tampered attestation was accepted"
    assert "S20 validation passed" not in result.stdout
