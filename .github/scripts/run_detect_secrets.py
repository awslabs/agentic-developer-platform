"""Run pinned detect-secrets with complete identities and a redacted audit schema."""

import contextlib
import copy
import hashlib
import io
import json
import logging
import re
import sys
from importlib.metadata import version
from pathlib import Path

EXPECTED_VERSION = "1.5.0"
VERIFICATION_FILTER = (
    "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
)
POLICY_VERSION = 1
AUDIT_SCHEMA = "adp.detect-secrets.audit/v2"


def policy_metadata():
    return {
        "version": POLICY_VERSION,
        "detect_secrets_version": EXPECTED_VERSION,
        "candidate_identity": "complete matched token, SHA1 per detect-secrets schema",
        "credential_verification": "disabled",
        "launcher_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


@contextlib.contextmanager
def corrected_scanner():
    """Keep scan/audit matching identical; never persist changes in site-packages."""
    from detect_secrets.core import baseline
    from detect_secrets.plugins.github_token import GitHubTokenDetector
    from detect_secrets.plugins.jwt import JwtTokenDetector

    if version("detect-secrets") != EXPECTED_VERSION:
        raise ValueError("Unreviewed detect-secrets version")
    original_github = GitHubTokenDetector.denylist
    original_jwt = JwtTokenDetector.denylist
    original_jwt_validator = JwtTokenDetector.is_formally_valid
    original_jwt_descriptor = JwtTokenDetector.__dict__["is_formally_valid"]
    configure = baseline.configure_settings_from_baseline

    def offline_settings(document, **kwargs):
        settings = copy.deepcopy(document)
        settings["filters_used"] = [
            item
            for item in settings.get("filters_used", [])
            if item.get("path") != VERIFICATION_FILTER
        ]
        return configure(settings, **kwargs)

    # RegexBasedDetector uses findall(), so a capturing prefix group discards
    # the actual GitHub token. JWT's lazy last segment discards the signature.
    GitHubTokenDetector.denylist = [
        re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36}")
    ]
    JwtTokenDetector.denylist = [
        re.compile(r"eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+(?:\.[A-Za-z0-9_+/=-]*)?")
    ]
    # The old lazy matcher validated only header/payload, never the signature.
    # Preserve that detection scope while retaining the whole signature in the
    # identity; malformed example signatures must not silently disappear.
    JwtTokenDetector.is_formally_valid = staticmethod(
        lambda token: original_jwt_validator(".".join(token.split(".")[:2]))
    )
    baseline.configure_settings_from_baseline = offline_settings
    try:
        yield
    finally:
        GitHubTokenDetector.denylist = original_github
        JwtTokenDetector.denylist = original_jwt
        JwtTokenDetector.is_formally_valid = original_jwt_descriptor
        baseline.configure_settings_from_baseline = configure


def redact_audit(document):
    results = document.get("results")
    if not isinstance(results, list):
        raise ValueError("Audit report has no results list")  # noqa: TRY004 - invalid external report
    redacted = []
    for record in results:
        candidate = record.get("secrets")
        lines = record.get("lines")
        types = record.get("types")
        if (
            not isinstance(candidate, str)
            or not candidate
            or not isinstance(lines, dict)
            or not lines
        ):
            raise ValueError("Audit record lacks candidate/line evidence")
        if (
            not isinstance(types, list)
            or not types
            or not all(isinstance(t, str) for t in types)
        ):
            raise ValueError("Audit record lacks detector evidence")
        if not all(re.fullmatch(r"[1-9][0-9]*", str(line)) for line in lines):
            raise ValueError("Audit record has invalid source line numbers")
        redacted.append(
            {
                "category": record["category"],
                "filename": record["filename"],
                "hashed_secret": hashlib.sha1(candidate.encode()).hexdigest(),
                "lines": {str(line): "[redacted]" for line in lines},
                "types": types,
            }
        )
    return {
        "schema_version": AUDIT_SCHEMA,
        "repository_matcher_policy": policy_metadata(),
        "results": redacted,
    }


def validate_audit_coverage(scan, audit):
    """Require each current candidate identity, not merely its source location."""
    if audit.get("schema_version") != AUDIT_SCHEMA:
        raise ValueError("Current run requires the redacted full-identity audit schema")
    scan_policy = scan.get("repository_matcher_policy")
    if (
        not isinstance(scan_policy, dict)
        or scan_policy.get("version") != POLICY_VERSION
        or scan_policy != audit.get("repository_matcher_policy")
    ):
        raise ValueError("Scan and audit matcher provenance differ")
    for entries in scan["results"].values():
        for entry in entries:
            if not re.fullmatch(r"[0-9a-f]{40}", entry.get("hashed_secret", "")):
                raise ValueError("Current scan candidate lacks a full hash identity")
    for record in audit["results"]:
        if (
            "secrets" in record
            or not re.fullmatch(r"[0-9a-f]{40}", record.get("hashed_secret", ""))
            or not record.get("lines")
            or any(value != "[redacted]" for value in record["lines"].values())
        ):
            raise ValueError("Audit record violates the redacted full-identity schema")
    expected = {
        (
            entry.get("filename") or filename,
            entry["type"],
            str(entry["line_number"]),
            entry["hashed_secret"],
        )
        for filename, entries in scan["results"].items()
        for entry in entries
        if entry.get("is_secret") is not False
    }
    observed = {
        (record["filename"], detector, str(line), record["hashed_secret"])
        for record in audit["results"]
        if record["category"] != "FALSE_POSITIVE"
        for detector in record["types"]
        for line in record["lines"]
    }
    missing = expected - observed
    if missing:
        raise ValueError(f"Audit omitted {len(missing)} current candidate identities")


def run(argv):
    from detect_secrets.main import main as native_main

    if not argv or argv[0] not in {"scan", "audit"}:
        raise ValueError("Only CI scan and JSON audit report modes are supported")
    forbidden_modes = {
        "--string",
        "-s",
        "-v",
        "--verbose",
        "--list-all-plugins",
        "--only-allowlisted",
    }
    if any(
        arg.split("=", 1)[0] in forbidden_modes
        or (arg.startswith("-s") and not arg.startswith("--"))
        or re.fullmatch(r"-v+", arg)
        for arg in argv
    ):
        raise ValueError("Interactive/raw diagnostic modes are not supported")
    args = list(argv)
    if args[0] == "scan":
        if "--baseline" not in args:
            raise ValueError("Scan requires an explicit baseline output")
        artifact = Path(args[args.index("--baseline") + 1])
        if "--no-verify" not in args:
            args.append("--no-verify")
    else:
        if len(args) != 4 or args[1:3] != ["--report", "--json"]:
            raise ValueError(
                "Audit requires --report --json and one current scan artifact"
            )
        artifact = Path(args[3])
    output, diagnostics = io.StringIO(), io.StringIO()
    previous_logging_disable = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with (
            corrected_scanner(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(diagnostics),
        ):
            result = native_main(args)
    finally:
        logging.disable(previous_logging_disable)
    if result:
        raise ValueError("Scanner execution failed")
    scan = json.loads(artifact.read_text())
    if args[0] == "scan":
        if not isinstance(scan.get("results"), dict) or not scan.get("plugins_used"):
            raise ValueError("Scanner did not produce a meaningful baseline")
        scan["repository_matcher_policy"] = policy_metadata()
        artifact.write_text(json.dumps(scan, indent=2) + "\n")
        print("detect-secrets scan completed with full-token identity policy v1")
    else:
        audit = redact_audit(json.loads(output.getvalue()))
        validate_audit_coverage(scan, audit)
        print(json.dumps(audit, indent=2))
    return 0


def main(argv=None):
    try:
        return run(list(sys.argv[1:] if argv is None else argv))
    except (Exception, SystemExit):  # noqa: BLE001 - native errors may contain credentials
        # Native diagnostics can include source lines. Never forward them or
        # exception messages into CI logs; hashed artifacts are the evidence.
        print("detect-secrets failed; raw diagnostics withheld", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
