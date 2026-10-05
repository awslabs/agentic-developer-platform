"""Gates for the one-off scan's tool invocations (S19, #5618).

The 2026-09-21 manual run reported success while three of its tools produced
nothing usable. Each failure was in the *invocation*, not in the tool, and each
was hidden by `|| true`, `2>/dev/null` or `continue-on-error`. Unit tests of the
parsing scripts cannot catch any of them, because the broken part is the command
line in the workflow. These gates bind the commands to their required shape.

Covered, one gate per observed defect:

* detect-secrets emitted its *detector name list* ("AWSKeyDetector", ...) instead
  of findings, because `--list-all-plugins` prints that list and exits 0. There is
  a second, independent defect underneath: `scan --baseline FILE` writes results
  INTO that file and prints nothing on stdout, so the old `> results.json`
  redirect captured zero bytes even without the plugin flag. Fixing only the flag
  would still have produced an empty artifact.
* The detect-secrets audit crashed on every run: `audit --report` re-reads every
  file named in the baseline, and the baseline still named a file untracked by
  #1192. `2>/dev/null || true` swallowed the traceback and left an empty report.
* cfn-nag aborted on all three templates and still reported green. Its FATAL
  record carries `id=FATAL` with `type=FAIL` (see cfn_nag's
  Violation.fatal_violation), so the workflow's grep for a *type* of FATAL could
  never match. The deny-list was also rejected for two independent reasons.
"""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO / ".github/workflows/security-scan.yml"
SHELL_DEFINITION_PATHS = (
    WORKFLOW_PATH,
    REPO / ".github/actions/codebuild-run/action.yml",
    REPO / ".github/actions/publish-findings-s3/action.yml",
)
BASELINE_PATH = REPO / ".github/security/.secrets.baseline"
DENY_LIST_PATH = REPO / ".github/security/cfn-nag-suppressions.yml"
SCRIPT_TESTS_WORKFLOW = REPO / ".github/workflows/script-tests.yml"
S19_DISPOSITION = REPO / "docs/security/runs/2026-09-21/S19-disposition.md"
sys.path.insert(0, str(REPO / ".github/scripts"))

RECONCILE_SOURCE = REPO / ".github/scripts/reconcile_security_scan.py"
RECONCILE_SPEC = importlib.util.spec_from_file_location("reconcile_security_scan", RECONCILE_SOURCE)
reconcile = importlib.util.module_from_spec(RECONCILE_SPEC)
RECONCILE_SPEC.loader.exec_module(reconcile)

from diff_security_findings import process_tool_findings


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def job_script(workflow: dict, job: str) -> str:
    return "\n".join(step.get("run", "") for step in workflow["jobs"][job]["steps"])


def strip_comments(script: str) -> str:
    """Drop `#` comment lines so a gate matches commands, not prose about them.

    These jobs document the defects they fix, so the fix's own explanation would
    otherwise satisfy an assertion that the broken flag is absent.
    """
    return "\n".join(
        line for line in script.split("\n") if not line.lstrip().startswith("#")
    )


def step_script(workflow: dict, job: str, name_fragment: str) -> str:
    """The `run` body of the single step whose name contains `name_fragment`."""
    matches = [
        step for step in workflow["jobs"][job]["steps"]
        if name_fragment.lower() in step.get("name", "").lower() and "run" in step
    ]
    assert len(matches) == 1, f"expected one {job!r} step matching {name_fragment!r}, got {len(matches)}"
    return matches[0]["run"]


def shell_bodies(node):
    if isinstance(node, dict):
        if isinstance(node.get("run"), str):
            yield node["run"]
        for value in node.values():
            yield from shell_bodies(value)
    elif isinstance(node, list):
        for value in node:
            yield from shell_bodies(value)


# --------------------------------------------------------------------------
# detect-secrets
# --------------------------------------------------------------------------


def test_scan_does_not_ask_for_the_plugin_list(workflow):
    """`--list-all-plugins` prints detector names and exits 0 — not findings."""
    script = strip_comments(job_script(workflow, "detect-secrets"))
    assert "--list-all-plugins" not in script
    assert "python3 .github/scripts/run_detect_secrets.py scan" in script


def test_scan_reads_results_out_of_the_baseline_not_stdout(workflow):
    """`scan --baseline FILE` writes into FILE and prints nothing to stdout.

    So the artifact must BE the scanned file, never a stdout redirect.
    """
    script = strip_comments(job_script(workflow, "detect-secrets"))
    assert re.search(
        r"cp -f \.github/security/\.secrets\.baseline detect-secrets-results\.json",
        script,
    ), "must scan into a copy so the committed baseline is not modified"
    assert "--baseline detect-secrets-results.json" in script
    # The defect being locked out: capturing stdout from `scan --baseline`.
    assert not re.search(r"run_detect_secrets\.py scan[^\n]*>\s*detect-secrets-results\.json", script)


def test_scanner_failures_are_not_swallowed(workflow):
    """The scan and audit steps must not mask a crash, nor the job a failure.

    Scoped to the two scanner steps: the later artifact-collection step's
    `|| true` is deliberate (a missing optional file should not fail publishing).
    """
    assert "continue-on-error" not in workflow["jobs"]["detect-secrets"]
    for fragment in ("Run detect-secrets", "Audit for new secrets"):
        script = strip_comments(step_script(workflow, "detect-secrets", fragment))
        assert "|| true" not in script, f"{fragment} still swallows failures"
        assert "2>/dev/null" not in script, f"{fragment} still discards stderr"
        assert "set -euo pipefail" in script, f"{fragment} does not fail fast"


def test_output_is_validated_as_meaningful_json(workflow):
    """An unparseable or plugin-less artifact must fail, not read as clean."""
    script = job_script(workflow, "detect-secrets")
    assert "is not valid JSON" in script
    assert "no 'results' key" in script
    assert "plugins_used" in script


def test_audit_reads_the_current_scan_artifact(workflow):
    """The committed baseline cannot contain candidates found by this run."""
    script = strip_comments(
        step_script(workflow, "detect-secrets", "Audit for new secrets")
    )
    assert "audit --report --json detect-secrets-results.json" in script
    assert "audit --report --json .github/security/.secrets.baseline" not in script
    assert "validate_audit_coverage(scan, report)" in script


def test_audit_logs_only_aggregate_counts(workflow):
    """Secret types and locations belong in private artifacts, not run logs."""
    script = strip_comments(
        step_script(workflow, "detect-secrets", "Audit for new secrets")
    )
    assert "categories:" in script
    printed_details = re.search(r"print\([^\n]*record\.get", script)
    assert printed_details is None, "audit prints per-secret details to the Actions log"


def test_committed_baseline_names_only_files_that_exist():
    """`audit --report` opens every file in the baseline; a stale name crashes it.

    This is the actual root cause of the empty audit artifact. The entry was for
    modules/agent-factory/beads/.beads-credential-key, which #1192 untracked.
    """
    baseline = json.loads(BASELINE_PATH.read_text())
    missing = [name for name in baseline["results"] if not (REPO / name).is_file()]
    assert missing == [], (
        f"baseline references file(s) that no longer exist: {missing}. "
        "detect-secrets audit --report raises FileNotFoundError on these."
    )


@pytest.mark.skipif(
    subprocess.run(["which", "detect-secrets"], capture_output=True).returncode != 0,
    reason="detect-secrets not installed",
)
def test_audit_report_actually_runs_against_the_committed_baseline():
    """Execute the real audit command. It crashed on every run before this fix."""
    proc = subprocess.run(
        ["detect-secrets", "audit", "--report", "--json", str(BASELINE_PATH)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"audit --report failed: {proc.stderr[-800:]}"
    report = json.loads(proc.stdout)
    assert "results" in report


@pytest.mark.skipif(
    subprocess.run(["which", "detect-secrets"], capture_output=True).returncode != 0,
    reason="detect-secrets not installed",
)
def test_fixed_invocation_finds_a_planted_secret(tmp_path):
    """End-to-end: a planted secret reaches the scan, audit, and reconciliation.

    The old command returned a 20-line detector-name list for this same input.
    """
    (tmp_path / ".github/security").mkdir(parents=True)
    (tmp_path / ".github/security/.secrets.baseline").write_text(BASELINE_PATH.read_text())
    (tmp_path / "planted.py").write_text(
        'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\n'
    )
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    artifact = tmp_path / "detect-secrets-results.json"
    artifact.write_text(BASELINE_PATH.read_text())
    proc = subprocess.run(
        [
            sys.executable, str(REPO / ".github/scripts/run_detect_secrets.py"), "scan",
            "--baseline", "detect-secrets-results.json",
            "--exclude-files", r"^\.github/security/\.secrets\.baseline$",
            "--exclude-files", r"^detect-secrets-results\.json$",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    report = json.loads(artifact.read_text())
    assert report["plugins_used"], "no plugins ran"
    assert "planted.py" in report["results"], f"planted secret not flagged: {report['results']}"
    assert any(
        entry["type"] == "AWS Access Key" for entry in report["results"]["planted.py"]
    ), report["results"]["planted.py"]

    audit = tmp_path / "detect-secrets-audit.json"
    proc = subprocess.run(
        [
            sys.executable, str(REPO / ".github/scripts/run_detect_secrets.py"), "audit", "--report", "--json",
            "detect-secrets-results.json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    audit.write_text(proc.stdout)
    audit_report = json.loads(proc.stdout)
    assert any(
        record["filename"] == "planted.py"
        and record["category"] == "UNVERIFIED"
        and "AWS Access Key" in record["types"]
        for record in audit_report["results"]
    ), audit_report

    summary = process_tool_findings(
        "detect-secrets", tmp_path, tmp_path / ".github/security"
    )
    assert summary["new_count"] == 2
    assert any(fp.startswith("AWS Access Key:planted.py:1:sha1:") for fp in summary["new"])
    fingerprint = next(
        fp for fp in summary["new"]
        if fp.startswith("AWS Access Key:planted.py:1:sha1:")
    )
    assert summary["new_severities"][fingerprint] == "unrated"


# --------------------------------------------------------------------------
# cfn-nag
# --------------------------------------------------------------------------


def test_fatal_is_matched_on_id_not_type(workflow):
    """cfn-nag's FATAL record is `id=FATAL, type=FAIL`.

    The old guard grepped for a *type* of FATAL, which no record ever carries, so
    it never fired while all three templates aborted unscanned.
    """
    script = strip_comments(job_script(workflow, "cfn-nag"))
    assert not re.search(r'"type"\s*\[\[:space:\]\]*:\s*\[\[:space:\]\]*"FATAL"', script)
    assert '"type"[[:space:]]*:[[:space:]]*"FATAL"' not in script
    # Parsed, not grepped.
    assert "json.loads" in script or "json.load" in script
    assert "FATAL" in script


def test_cfn_nag_failures_are_not_swallowed(workflow):
    job = workflow["jobs"]["cfn-nag"]
    assert "continue-on-error" not in job


def test_no_deny_list_is_passed_while_there_are_no_global_suppressions(workflow):
    """An empty global deny-list cannot be expressed, so it must not be passed.

    cfn_nag's DenyListLoader raises on every empty form -- `RulesToSuppress: []`
    and a comments-only file both raise, and `RuleSuppression` is a key it never
    reads -- and each raise becomes a FATAL that aborts every template. Passing
    no deny-list makes the filter a documented no-op instead.
    """
    parsed = yaml.safe_load(DENY_LIST_PATH.read_text())
    suppressions = (parsed or {}).get("RulesToSuppress")
    script = strip_comments(job_script(workflow, "cfn-nag"))

    if suppressions:
        # A real global waiver exists, so the flag must be passed and each entry
        # must be a valid, justified rule id.
        assert "--deny-list-path" in script
        for entry in suppressions:
            assert entry.get("id"), f"deny-list entry needs a rule id: {entry}"
            assert entry.get("reason"), f"entry {entry.get('id')} needs a justification"
            assert re.fullmatch(r"[FW]\d+", entry["id"]), (
                f"{entry['id']} is not a cfn_nag rule id (F<n>/W<n>); a bogus id FATALs"
            )
    else:
        assert "--deny-list-path" not in script, (
            "there are no global suppressions, and every empty deny-list form "
            "makes cfn_nag FATAL on all templates -- omit the flag instead"
        )
        # The unread key must not come back: it is what broke the 2026-09-21 run.
        assert "RuleSuppression" not in (parsed or {}), (
            "cfn_nag never reads 'RuleSuppression'; it reads 'RulesToSuppress'"
        )


def test_deny_list_is_pure_ascii():
    """cfn_nag reads the deny-list with File.read, which uses the runner locale.

    A non-ASCII byte (the file previously held an em dash in a comment) can raise
    during YAML parse and become a FATAL that aborts every template.
    """
    raw = DENY_LIST_PATH.read_bytes()
    offenders = [(i, hex(b)) for i, b in enumerate(raw) if b > 127]
    assert offenders == [], f"non-ASCII bytes in deny-list at {offenders[:5]}"


def test_every_expected_template_must_appear_in_the_results(workflow):
    """A template silently absent from the output is zero coverage, not zero findings."""
    script = job_script(workflow, "cfn-nag")  # comments may name them too
    for template in ("full-admin.cfn.yaml", "readonly.cfn.yaml", "scoped-write.cfn.yaml"):
        assert template in script, f"{template} is not asserted as scanned"


@pytest.mark.parametrize(
    "record, should_fail",
    [
        # The real shape emitted by Violation.fatal_violation.
        ({"id": "FATAL", "type": "FAIL", "message": "Deny list is malformed"}, True),
        ({"id": "FATAL", "type": "FAIL", "message": "YAML parse of deny list failed"}, True),
        # An ordinary failing rule is a finding, not a scan failure.
        ({"id": "F1000", "type": "FAIL", "message": "Missing egress rule"}, False),
        ({"id": "W2", "type": "WARN", "message": "open cidr"}, False),
    ],
)
def test_fatal_detector_matches_real_cfn_nag_records(record, should_fail, tmp_path):
    """Run the workflow's own FATAL detector over real record shapes."""
    script = job_script(workflow_module_cache(), "cfn-nag")
    detector = extract_python_heredoc(script)
    results = [{"filename": "t.yaml", "file_results": {"violations": [record]}}]
    target = tmp_path / "cfn-nag-results.json"
    target.write_text(json.dumps(results))
    proc = subprocess.run(
        [sys.executable, "-c", detector],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "CFN_NAG_EXPECTED_TEMPLATES": "t.yaml"},
    )
    assert (proc.returncode != 0) is should_fail, (
        f"record {record} -> rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )


_WORKFLOW_CACHE: dict = {}


def workflow_module_cache() -> dict:
    if not _WORKFLOW_CACHE:
        _WORKFLOW_CACHE.update(yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader))
    return _WORKFLOW_CACHE


def extract_python_heredoc(script: str) -> str:
    """Pull the inline `python3 - <<'PY' ... PY` body out of a workflow step."""
    match = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\b", script, re.S)
    assert match, "expected an inline python3 heredoc in the cfn-nag job"
    body = match.group(1)
    # The heredoc is indented inside the YAML block scalar; dedent it.
    lines = body.split("\n")
    indent = min(
        (len(line) - len(line.lstrip()) for line in lines if line.strip()),
        default=0,
    )
    return "\n".join(line[indent:] if line.strip() else "" for line in lines)

# --------------------------------------------------------------------------
# Authenticated one-off producer and complete reconciliation
# --------------------------------------------------------------------------


def test_one_off_producer_binds_definition_source_target_and_rerun_artifacts(workflow):
    dispatch = workflow["on"]["workflow_dispatch"]
    assert set(dispatch["inputs"]) == {
        "adp_correlation", "adp_source_revision", "adp_definition_revision",
        "expected_account_id", "region", "scan_scope",
    }
    assert workflow["run-name"] == "${{ format('ADP deployment {0}', inputs.adp_correlation) }}"
    assert workflow["concurrency"] == {
        "group": "security-scan-one-off",
        "cancel-in-progress": "false",
    }
    context = workflow["jobs"]["context"]
    context_script = job_script(workflow, "context")
    assert "repository-scan-receipt.py" in context_script
    assert 'test "$GITHUB_SHA" = "$EXPECTED_DEFINITION_REVISION"' in context_script
    assert workflow["env"]["EXPECTED_DEFINITION_REVISION"] == "${{ inputs.adp_definition_revision }}"
    uploads = [step for step in context["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert uploads[0]["with"]["name"] == "adp-deployment-context-security-scan.yml-${{ github.run_attempt }}"

    for name, job in workflow["jobs"].items():
        checkouts = [step for step in job.get("steps", []) if step.get("uses") == "actions/checkout@v7"]
        if checkouts:
            assert checkouts[0]["with"]["ref"] == "${{ inputs.adp_source_revision }}", name
            names = [step.get("name") for step in job["steps"]]
            assert "Verify checked-out scan source" in names, name
            if name != "context":
                assert "Verify approved AWS scan identity" in names, name
        if name not in {"context", "cleanup", "summary"}:
            needs = job.get("needs", [])
            assert "context" in needs, name


@pytest.mark.parametrize("definition_path", SHELL_DEFINITION_PATHS, ids=lambda path: path.name)
def test_workflow_inputs_are_never_interpolated_directly_into_shell(definition_path):
    definition = yaml.load(definition_path.read_text(), Loader=yaml.BaseLoader)
    scripts = list(shell_bodies(definition))
    assert scripts, f"expected shell bodies in {definition_path}"
    assert all("${{ inputs." not in script for script in scripts)


def test_reconciliation_requires_current_attempt_every_tool_and_both_npm_legs(workflow):
    assert workflow["env"]["RENDEZVOUS_PREFIX"].endswith("/${{ github.run_attempt }}")
    for name in ("checkov", "semgrep", "detect-secrets", "grype", "bandit", "cfn-nag", "npm-audit", "syft"):
        publish = next(step for step in workflow["jobs"][name]["steps"] if step.get("uses") == "./.github/actions/publish-findings-s3")
        assert "RENDEZVOUS_PREFIX" in publish["with"]["dest-prefix"]
        assert publish["with"]["source-revision"] == "${{ inputs.adp_source_revision }}"
    summary_script = job_script(workflow, "summary")
    assert "validate-findings" in summary_script
    assert "expected-images.json" in summary_script
    assert "SCAN_JOB_RESULTS" in str(workflow["jobs"]["summary"]["steps"])
    assert "npm-audit-modules-gateway-frontend.json" in RECONCILE_SOURCE.read_text()
    assert "npm-audit-modules-agent-factory-agent.json" in RECONCILE_SOURCE.read_text()


def test_codebuild_uses_verified_source_without_relabeling_dispatch_sha(workflow):
    action = yaml.load((REPO / ".github/actions/codebuild-run/action.yml").read_text(), Loader=yaml.BaseLoader)
    assert "source_revision" in action["inputs"] and "child_state_uri" in action["inputs"]
    body = "\n".join(step.get("run", "") for step in action["runs"]["steps"])
    assert "SOURCE_REVISION=\"${REQUESTED_SOURCE:-${GITHUB_SHA}}\"" in body
    assert "name=ADP_SOURCE_SHA,value=${SOURCE_REVISION}" in body
    assert "${GITHUB_RUN_ATTEMPT}-${GITHUB_JOB}.zip" in body
    assert "CHILD_STATE_URI" in body and '"build_id"' in body
    for tool in ("grype", "syft"):
        build = next(step for step in workflow["jobs"][tool]["steps"] if step.get("uses") == "./.github/actions/codebuild-run")
        assert build["with"]["source_revision"] == "${{ inputs.adp_source_revision }}"
        assert "children/" + tool + ".json" in build["with"]["child_state_uri"]


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def valid_findings_tree(root: Path):
    sarif = {"version": "2.1.0", "runs": [{"results": []}]}
    for tool, filename in {
        "checkov": "checkov-results.sarif",
        "semgrep": "semgrep-results.sarif",
        "bandit": "bandit-results.sarif",
    }.items():
        write_json(root / tool / filename, sarif)
    write_json(root / "detect-secrets/detect-secrets-results.json", {"plugins_used": [{"name": "AWSKeyDetector"}], "results": {}})
    write_json(root / "detect-secrets/detect-secrets-audit.json", {"results": []})
    write_json(root / "cfn-nag/cfn-nag-results.json", [
        {"filename": name, "file_results": {"violations": []}}
        for name in ("full-admin.cfn.yaml", "readonly.cfn.yaml", "scoped-write.cfn.yaml")
    ])
    npm = {"auditReportVersion": 2, "vulnerabilities": {}}
    write_json(root / "npm-audit/npm-audit-modules-gateway-frontend.json", npm)
    write_json(root / "npm-audit/npm-audit-modules-agent-factory-agent.json", npm)
    write_json(root / "grype/image.sarif", sarif)
    write_json(root / "syft/image.cdx.json", {"bomFormat": "CycloneDX"})


@pytest.mark.parametrize("missing", [
    "checkov/checkov-results.sarif",
    "detect-secrets/detect-secrets-audit.json",
    "npm-audit/npm-audit-modules-agent-factory-agent.json",
    "grype/image.sarif",
    "syft/image.cdx.json",
])
def test_missing_expected_output_blocks_reconciliation(tmp_path, missing):
    valid_findings_tree(tmp_path)
    (tmp_path / missing).unlink()
    with pytest.raises(ValueError):
        reconcile.validate_findings(tmp_path, {"image"})


def test_invalid_scanner_schema_blocks_reconciliation(tmp_path):
    valid_findings_tree(tmp_path)
    write_json(tmp_path / "semgrep/semgrep-results.sarif", {"runs": "not-a-list"})
    with pytest.raises(ValueError, match="SARIF"):
        reconcile.validate_findings(tmp_path, {"image"})


def test_unproven_cleanup_is_never_reported_complete(tmp_path):
    result = reconcile.cleanup_children(tmp_path, {"grype", "syft"}, "us-east-1", "state-bucket")
    assert result["cleanup_complete"] is False
    assert "coverage mismatch" in result["errors"][0]


def test_cleanup_stops_live_child_rechecks_terminal_state_and_deletes_source(tmp_path):
    source_key = "codebuild/src/adp-dev-grype-scan/" + "a" * 40 + "-10-2-grype.zip"
    write_json(tmp_path / "grype.json", {"build_id": "adp-dev-grype-scan:build", "source_key": source_key})
    statuses = iter(("IN_PROGRESS", "STOPPED"))
    calls = []

    def aws(args):
        calls.append(args)
        if args[:2] == ["codebuild", "batch-get-builds"]:
            return {"builds": [{"id": "adp-dev-grype-scan:build", "buildStatus": next(statuses)}]}
        return {}

    result = reconcile.cleanup_children(tmp_path, {"grype"}, "us-east-1", "state-bucket", aws=aws, sleep=lambda _: None)
    assert result["cleanup_complete"] is True
    assert any(call[:2] == ["codebuild", "stop-build"] for call in calls)
    assert any(call[:2] == ["s3api", "delete-object"] for call in calls)


def write_image_evidence(root: Path, tool: str, source_revision: str, digest: str):
    suffix = ".sarif" if tool == "grype" else ".cdx.json"
    artifact = root / tool / "artifacts" / f"image{suffix}"
    payload = {"version": "2.1.0", "runs": [{"results": []}]} if tool == "grype" else {"bomFormat": "CycloneDX"}
    write_json(artifact, payload)
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    provenance = {
        "artifact_sha256": artifact_sha256,
        "digest": digest,
        "name": "image",
        "source_revision": source_revision,
        "tool": tool,
    }
    write_json(root / tool / "provenance/image.json", provenance)
    write_json(root / tool / "coverage.json", {
        "tool": tool,
        "commit": source_revision,
        "expected": 1,
        "succeeded": 1,
        "targets": [{"name": "image", "status": "succeeded", "digest": digest, "artifact_sha256": artifact_sha256}],
    })


def test_observed_receipt_inputs_preserve_cleanup_and_hash_real_private_artifacts(tmp_path):
    source_revision = "a" * 40
    digest = "sha256:" + "b" * 64
    write_image_evidence(tmp_path, "grype", source_revision, digest)
    write_image_evidence(tmp_path, "syft", source_revision, digest)
    provenance = tmp_path / "receipt-provenance"
    result = reconcile.observed_results(tmp_path, provenance, source_revision, {"image"}, False)
    assert result["source_revision"] == source_revision
    assert result["coverage_complete"] is True
    assert result["cleanup_complete"] is False
    assert result["images"]["image"]["grype"]["digest"] == "sha256:" + "b" * 64
    assert result["images"]["image"]["syft"]["digest"] == "sha256:" + "b" * 64
    assert (provenance / "grype-image.json").is_file()
    assert (provenance / "syft-image.json").is_file()


def test_cross_tool_image_digest_mismatch_is_preserved_in_receipt(tmp_path):
    source_revision = "a" * 40
    write_image_evidence(tmp_path, "grype", source_revision, "sha256:" + "b" * 64)
    write_image_evidence(tmp_path, "syft", source_revision, "sha256:" + "c" * 64)

    result = reconcile.observed_results(
        tmp_path, tmp_path / "out", source_revision, {"image"}, True
    )

    assert result["images"]["image"] == {
        "grype": {
            "digest": "sha256:" + "b" * 64,
            "provenance_path": "grype-image.json",
        },
        "syft": {
            "digest": "sha256:" + "c" * 64,
            "provenance_path": "syft-image.json",
        },
    }


def test_tampered_private_image_artifact_blocks_receipt(tmp_path):
    source_revision = "a" * 40
    digest = "sha256:" + "b" * 64
    write_image_evidence(tmp_path, "grype", source_revision, digest)
    write_image_evidence(tmp_path, "syft", source_revision, digest)
    (tmp_path / "grype/artifacts/image.sarif").write_text("tampered")
    with pytest.raises(ValueError, match="artifact bytes"):
        reconcile.observed_results(tmp_path, tmp_path / "out", source_revision, {"image"}, True)


def test_missing_sibling_state_still_cleans_recorded_live_child(tmp_path):
    source_key = "codebuild/src/adp-dev-grype-scan/" + "a" * 40 + "-10-2-grype.zip"
    write_json(tmp_path / "grype.json", {"build_id": "adp-dev-grype-scan:build", "source_key": source_key})
    calls = []

    def aws(args):
        calls.append(args)
        if args[:2] == ["codebuild", "batch-get-builds"]:
            return {"builds": [{"id": "adp-dev-grype-scan:build", "buildStatus": "IN_PROGRESS" if len([c for c in calls if c[:2] == ["codebuild", "batch-get-builds"]]) == 1 else "STOPPED"}]}
        return {}

    result = reconcile.cleanup_children(tmp_path, {"grype", "syft"}, "us-east-1", "state-bucket", aws=aws, sleep=lambda _: None)
    assert result["cleanup_complete"] is False
    assert result["children"]["grype"]["terminal_status"] == "STOPPED"
    assert any(call[:2] == ["codebuild", "stop-build"] for call in calls)
    assert any(call[:2] == ["s3api", "delete-object"] for call in calls)


def _codebuild_idempotency_token(tmp_path: Path, **overrides: str) -> str:
    action = yaml.load(
        (REPO / ".github/actions/codebuild-run/action.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    start = next(step for step in action["runs"]["steps"] if step.get("id") == "start")
    invocation = tmp_path / str(len(list(tmp_path.iterdir())))
    invocation.mkdir()
    fake_bin = invocation / "bin"
    fake_bin.mkdir()
    aws_args = invocation / "aws-args"
    aws = fake_bin / "aws"
    aws.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\0' \"$@\" > \"$AWS_ARGS\"\n"
        "printf 'project:build\\n'\n"
    )
    aws.chmod(0o755)
    environment = {
        "STATE_BUCKET": "state-bucket",
        "AWS_REGION": "us-east-1",
        "PROJECT_NAME": "adp-dev-first-build",
        "SOURCE_KEY": "codebuild/src/adp-dev-grype-scan/" + "a" * 40 + "-10-1-build.zip",
        "SOURCE_REVISION": "a" * 40,
        "ENV_VARS": '"name=IMAGE_TAG,value=abc,type=PLAINTEXT"',
        "CHILD_STATE_URI": "",
        "GITHUB_RUN_ID": "10",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "build",
        "GITHUB_OUTPUT": str(invocation / "output"),
        "RUNNER_TEMP": str(invocation),
        "AWS_ARGS": str(aws_args),
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
    }
    environment.update(overrides)

    proc = subprocess.run(
        ["bash", "-c", start["run"]],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    arguments = [part.decode() for part in aws_args.read_bytes().split(b"\0") if part]
    return arguments[arguments.index("--idempotency-token") + 1]


def test_codebuild_idempotency_token_is_retry_stable_and_request_unique(tmp_path):
    original = _codebuild_idempotency_token(tmp_path)
    assert _codebuild_idempotency_token(tmp_path) == original
    assert len(original) == 64

    changed_requests = [
        {"PROJECT_NAME": "adp-dev-second-build"},
        {"SOURCE_KEY": "codebuild/src/adp-dev-grype-scan/" + "b" * 40 + "-10-1-build.zip"},
        {"SOURCE_REVISION": "b" * 40},
        {"ENV_VARS": '"name=IMAGE_TAG,value=def,type=PLAINTEXT"'},
    ]
    for changed_request in changed_requests:
        assert _codebuild_idempotency_token(tmp_path, **changed_request) != original


def test_all_s19_subjects_trigger_script_tests_for_push_and_pull_requests():
    expected_paths = {
        ".github/actions/codebuild-run/action.yml",
        ".github/actions/publish-findings-s3/action.yml",
        ".github/scripts/diff_security_findings.py",
        ".github/scripts/reconcile_security_scan.py",
        ".github/security/cfn-nag-suppressions.yml",
        ".github/workflows/security-scan.yml",
        "codebuild/bs-grype-scan.yml",
        "codebuild/bs-syft-scan.yml",
        "codebuild/scan_security_images.py",
        "codebuild/security_image_targets.py",
        "codebuild/tests/**",
        "modules/domain-apps/superplane/releases/superplane.lock.yaml",
        "modules/gateway/scripts/repository-scan-receipt.py",
        "modules/gateway/tests/orchestration/test_repository_scan_export.py",
    }
    script_tests = yaml.load(
        SCRIPT_TESTS_WORKFLOW.read_text(), Loader=yaml.BaseLoader
    )
    for event in ("push", "pull_request"):
        configured_paths = set(script_tests["on"][event]["paths"])
        assert expected_paths <= configured_paths, (
            f"{event} does not run the S19 gates for: "
            f"{sorted(expected_paths - configured_paths)}"
        )


def test_s21_handoff_binds_dispatch_ref_and_selects_run_by_correlation():
    instructions = S19_DISPOSITION.read_text()

    assert '--ref "$DISPATCH_REF"' in instructions
    assert '--ref "$DEFINITION_REVISION"' not in instructions
    assert "git rev-parse --verify 'FETCH_HEAD^{commit}'" in instructions
    assert not re.search(r"--limit\s+1(?:\s|$)", instructions)
    for binding in (
        '.displayTitle == $title',
        '.event == "workflow_dispatch"',
        '.headBranch == $branch',
        '.headSha == $revision',
        '.run_id == $run_id',
        '.run_attempt == $run_attempt',
        '.workflow_revision == $definition',
        '.source_revision == $source',
        '.correlation == $correlation',
    ):
        assert binding in instructions


def test_checkov_current_publisher_filename_is_accepted(tmp_path):
    valid_findings_tree(tmp_path)
    (tmp_path / "checkov/checkov-results.sarif").rename(tmp_path / "checkov/results_sarif.sarif")
    reconcile.validate_findings(tmp_path, {"image"})


def test_competing_checkov_reports_are_rejected(tmp_path):
    valid_findings_tree(tmp_path)
    write_json(tmp_path / "checkov/results_sarif.sarif", {"version": "2.1.0", "runs": [{"results": []}]})
    with pytest.raises(ValueError, match="exactly one"):
        reconcile.validate_findings(tmp_path, {"image"})


@pytest.mark.parametrize("tamper", [None, "raw", "summary", "metadata", "build_args", "extra", "strip"])
def test_extended_scanner_provenance_checks_all_evidence(tmp_path, tamper):
    revision = "a" * 40
    suffixes = {
        "raw_artifact_sha256": ".raw.sarif",
        "suppression_summary_sha256": ".suppression-summary.json",
        "scanner_metadata_sha256": ".scanner-metadata.json",
    }
    for tool in ("grype", "syft"):
        write_image_evidence(tmp_path, tool, revision, "sha256:" + "b" * 64)
        path = tmp_path / tool / "provenance/image.json"
        provenance = json.loads(path.read_text())
        coverage_path = tmp_path / tool / "coverage.json"
        coverage = json.loads(coverage_path.read_text())
        extras = {"build_args": {"PYTHON_IMAGE": "python@sha256:" + "c" * 64}}
        for field, suffix in suffixes.items():
            extras[field] = None
            if tool == "grype":
                artifact = tmp_path / tool / "artifacts" / ("image" + suffix)
                write_json(artifact, {"synthetic": suffix})
                extras[field] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        provenance.update(extras)
        coverage["targets"][0].update(extras)
        write_json(path, provenance)
        write_json(coverage_path, coverage)
    path = tmp_path / "grype/provenance/image.json"
    provenance = json.loads(path.read_text())
    if tamper in ("raw", "summary", "metadata"):
        suffix = {"raw": ".raw.sarif", "summary": ".suppression-summary.json", "metadata": ".scanner-metadata.json"}[tamper]
        (tmp_path / "grype/artifacts" / ("image" + suffix)).write_text("tampered")
    elif tamper == "build_args":
        provenance["build_args"] = {"PYTHON_IMAGE": "python@sha256:" + "d" * 64}
    elif tamper == "extra":
        provenance["unexpected"] = True
    elif tamper == "strip":
        for key in ("build_args", *suffixes):
            del provenance[key]
    write_json(path, provenance)
    if tamper:
        with pytest.raises(ValueError):
            reconcile.observed_results(tmp_path, tmp_path / "out", revision, {"image"}, True)
    else:
        result = reconcile.observed_results(tmp_path, tmp_path / "out", revision, {"image"}, True)
        assert result["coverage_complete"] is True
        assert json.loads((tmp_path / "out/grype-image.json").read_text()) == provenance
