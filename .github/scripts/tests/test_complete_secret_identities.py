"""Real pinned-plugin/consumer regressions use synthetic tokens, never credentials."""

import base64
import copy
import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import diff_security_findings as diff
import reconcile_security_scan as reconcile
import run_detect_secrets as launcher
from detect_secrets.plugins.github_token import GitHubTokenDetector
from detect_secrets.plugins.jwt import JwtTokenDetector


def synthetic_tokens():
    def b64(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    github = [
        "ghp_" + hashlib.sha256(f"synthetic-github-{n}".encode()).hexdigest()[:36]
        for n in (1, 2)
    ]
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"sub": "synthetic-test-subject"}).encode())
    jwt = [
        header
        + "."
        + payload
        + "."
        + b64(hashlib.sha256(f"synthetic-signature-{n}".encode()).digest())
        for n in (1, 2)
    ]
    return github, jwt


def test_pinned_upstream_plugins_reproduce_collapsed_identities():
    github, jwt = synthetic_tokens()
    assert len(set(GitHubTokenDetector().analyze_string(" ".join(github)))) == 1
    assert len(set(JwtTokenDetector().analyze_string(" ".join(jwt)))) == 1
    with launcher.corrected_scanner():
        assert set(GitHubTokenDetector().analyze_string(" ".join(github))) == set(
            github
        )
        assert set(JwtTokenDetector().analyze_string(" ".join(jwt))) == set(jwt)


@pytest.fixture
def scanned(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    github, jwt = synthetic_tokens()
    tokens = github + jwt
    (tmp_path / "input.txt").write_text(" ".join(tokens) + "\n")
    artifact = tmp_path / "scan.json"
    legacy = {
        "version": "1.5.0",
        "plugins_used": [{"name": "GitHubTokenDetector"}, {"name": "JwtTokenDetector"}],
        "filters_used": [{"path": launcher.VERIFICATION_FILTER}],
        "results": {
            "input.txt": [
                {
                    "type": "GitHub Token",
                    "line_number": 1,
                    "hashed_secret": hashlib.sha1(github[0][:3].encode()).hexdigest(),
                    "is_secret": False,
                },
                {
                    "type": "JSON Web Token",
                    "line_number": 1,
                    "hashed_secret": hashlib.sha1(
                        (jwt[0].rsplit(".", 1)[0] + ".").encode()
                    ).hexdigest(),
                    "is_secret": False,
                },
            ]
        },
    }
    artifact.write_text(json.dumps(legacy))

    def forbidden_verification(*args, **kwargs):
        raise AssertionError("Credential verification must not run")

    monkeypatch.setattr(GitHubTokenDetector, "verify", forbidden_verification)
    monkeypatch.setattr(JwtTokenDetector, "verify", forbidden_verification)
    assert (
        launcher.main(["scan", "--baseline", str(artifact), "--all-files", "input.txt"])
        == 0
    )
    scan_output = capsys.readouterr()
    scan = json.loads(artifact.read_text())
    assert launcher.main(["audit", "--report", "--json", str(artifact)]) == 0
    audit_output = capsys.readouterr()
    audit = json.loads(audit_output.out)
    emitted = (
        artifact.read_text()
        + scan_output.out
        + scan_output.err
        + audit_output.out
        + audit_output.err
    )
    assert not any(token in emitted for token in tokens)
    return scan, audit, legacy, tokens


def test_real_scan_and_audit_keep_all_full_token_identities(scanned):
    scan, audit, _, tokens = scanned
    expected = {hashlib.sha1(token.encode()).hexdigest() for token in tokens}
    found = [entry for entries in scan["results"].values() for entry in entries]
    assert {entry["hashed_secret"] for entry in found} == expected
    assert len(found) == 4
    # Old prefix-only false-positive labels must not transfer to new full tokens.
    assert all(entry.get("is_secret") is not False for entry in found)
    assert {record["hashed_secret"] for record in audit["results"]} == expected
    assert len(audit["results"]) == 4
    assert audit["schema_version"] == launcher.AUDIT_SCHEMA
    assert all(
        "secrets" not in r and set(r["lines"].values()) == {"[redacted]"}
        for r in audit["results"]
    )
    assert all(f["path"] != launcher.VERIFICATION_FILTER for f in scan["filters_used"])
    launcher.validate_audit_coverage(scan, audit)


def test_same_line_missing_distinct_token_fails_coverage(scanned):
    scan, audit, _, _ = scanned
    broken = copy.deepcopy(audit)
    broken["results"].pop()
    with pytest.raises(ValueError, match="candidate identities"):
        launcher.validate_audit_coverage(scan, broken)


@pytest.mark.parametrize("field", ["secrets", "lines", "hashed_secret"])
def test_v2_audit_rejects_raw_or_incomplete_records(scanned, field):
    scan, audit, _, _ = scanned
    broken = copy.deepcopy(audit)
    broken["results"][0][field] = {"1": "raw source"} if field == "lines" else "unsafe"
    with pytest.raises(ValueError, match="redacted full-identity"):
        launcher.validate_audit_coverage(scan, broken)


def test_reconciliation_diff_and_sanitized_report_consume_v2(scanned, tmp_path, capsys):
    scan, audit, legacy, tokens = scanned
    helper_spec = importlib.util.spec_from_file_location(
        "invocation_fixture", SCRIPTS / "tests/test_security_scan_tool_invocations.py"
    )
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    root = tmp_path / "findings"
    helper.valid_findings_tree(root)
    helper.write_json(root / "detect-secrets/detect-secrets-results.json", scan)
    helper.write_json(root / "detect-secrets/detect-secrets-audit.json", audit)
    reconcile.validate_findings(root, {"image"})
    assert len(diff.extract_detect_secrets_fingerprints(scan)) == 4
    assert diff.extract_detect_secrets_fingerprints(
        scan
    ) == diff.extract_detect_secrets_fingerprints(audit)
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    for entry in legacy["results"]["input.txt"]:
        entry.pop("is_secret")
    helper.write_json(baseline_dir / ".secrets.baseline", legacy)
    summary_path = tmp_path / "summary.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "diff_security_findings.py"),
            "--findings-dir",
            str(root),
            "--baseline-dir",
            str(baseline_dir),
            "--output",
            str(summary_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(summary_path.read_text())["detect-secrets"]
    assert result["new_count"] == 4 and result["resolved_count"] == 0
    assert result["legacy_partial_identity_count"] == 2
    assert len(result["legacy_partial_identities_pending_review"]) == 2
    sanitized = tmp_path / "sanitized.json"
    reconcile.sanitize_summary(summary_path, sanitized, "a" * 40, "synthetic")
    report = json.loads(sanitized.read_text())["tools"]["detect-secrets"]
    assert report["new_count"] == 4 and report["resolved_count"] == 0
    assert report["legacy_partial_identity_count"] == 2
    emitted = (
        summary_path.read_text()
        + sanitized.read_text()
        + completed.stdout
        + completed.stderr
    )
    assert not any(token in emitted for token in tokens)
    legacy_audit = copy.deepcopy(audit)
    legacy_audit.pop("schema_version")
    helper.write_json(root / "detect-secrets/detect-secrets-audit.json", legacy_audit)
    with pytest.raises(ValueError, match="redacted full-identity audit schema"):
        reconcile.validate_findings(root, {"image"})
    audit["results"].pop()
    helper.write_json(root / "detect-secrets/detect-secrets-audit.json", audit)
    with pytest.raises(ValueError, match="candidate identities"):
        reconcile.validate_findings(root, {"image"})


def test_historical_raw_audit_schema_still_matches_its_own_scan():
    token = synthetic_tokens()[0][0]
    hashed = hashlib.sha1(token.encode()).hexdigest()
    scan = {
        "results": {
            "input.txt": [
                {"type": "GitHub Token", "line_number": 1, "hashed_secret": hashed}
            ]
        }
    }
    audit = {
        "results": [
            {
                "category": "UNVERIFIED",
                "filename": "input.txt",
                "types": ["GitHub Token"],
                "lines": {"1": "historical private source"},
                "secrets": token,
            }
        ]
    }
    assert diff.extract_detect_secrets_fingerprints(
        scan
    ) == diff.extract_detect_secrets_fingerprints(audit)
    assert token not in repr(diff.extract_detect_secrets_fingerprints(audit))


def test_native_exception_and_logging_never_emit_token(monkeypatch, capsys, tmp_path):
    import detect_secrets.main

    token = synthetic_tokens()[0][0]

    def broken(*args):
        print(token)
        print(token, file=sys.stderr)
        logging.getLogger("detect-secrets").error(token)
        raise RuntimeError(token)

    monkeypatch.setattr(detect_secrets.main, "main", broken)
    assert launcher.main(["scan", "--baseline", str(tmp_path / "scan.json")]) == 1
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert "raw diagnostics withheld" in captured.err


def test_ci_runs_the_real_plugin_regression_file():
    workflow = (SCRIPTS.parents[0] / "workflows/script-tests.yml").read_text()
    assert "tests/test_complete_secret_identities.py" in workflow


def test_scan_audit_policy_mismatch_is_rejected(scanned):
    scan, audit, _, _ = scanned
    broken = copy.deepcopy(audit)
    broken["repository_matcher_policy"]["launcher_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="provenance differ"):
        launcher.validate_audit_coverage(scan, broken)


@pytest.mark.parametrize(
    "mode", ["--string=synthetic", "-ssynthetic", "--only-allowlisted", "-vv"]
)
def test_non_scan_modes_cannot_relabel_an_existing_baseline(mode, tmp_path, capsys):
    artifact = tmp_path / "scan.json"
    original = '{"results": {}}'
    artifact.write_text(original)
    assert launcher.main(["scan", "--baseline", str(artifact), mode]) == 1
    assert artifact.read_text() == original
    assert "raw diagnostics withheld" in capsys.readouterr().err


def test_complete_jwt_identity_preserves_legacy_header_payload_detection_scope():
    _, jwt = synthetic_tokens()
    prefix = jwt[0].rsplit(".", 1)[0]
    malformed_examples = [prefix + ".x", prefix + ".y"]
    assert (
        len(set(JwtTokenDetector().analyze_string(" ".join(malformed_examples)))) == 1
    )
    with launcher.corrected_scanner():
        assert set(
            JwtTokenDetector().analyze_string(" ".join(malformed_examples))
        ) == set(malformed_examples)
