"""Synthetic read-only EC-04 audit tests; no Terraform apply or GitHub access."""
import importlib.util
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ec04_audit", ROOT / "ec04.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
FIXTURES = {"run_id": "12345678-1234-4234-8234-123456789012", "source_revision": "a" * 40,
            "account_id": "123456789012"}
ROLE = "arn:aws:iam::123456789012:role/synthetic-audit"


def approved(backend, variables):
    return ROLE, hashlib.sha256(backend.read_bytes()).hexdigest(), hashlib.sha256(variables.read_bytes()).hexdigest()


def identity(role="synthetic-audit"):
    return CompletedProcess([], 0, json.dumps({"Account": FIXTURES["account_id"],
                            "Arn": f"arn:aws:sts::123456789012:assumed-role/{role}/session"}).encode(), b"")


def private(tmp_path, name, contents=b"synthetic-safe-content"):
    path = tmp_path / name
    path.write_bytes(contents)
    path.chmod(0o600)
    return path


def test_instance_plan_uses_exact_private_inputs_and_requires_zero_drift(tmp_path):
    backend, variables = private(tmp_path, "backend.tfvars"), private(tmp_path, "variables.tfvars")
    run = Mock(side_effect=[identity(), CompletedProcess([], 0, b"synthetic initialized", b""),
                            CompletedProcess([], 0, b"synthetic no changes", b"")])
    receipt = audit.plan("present", FIXTURES, backend, variables, *approved(backend, variables), run=run,
                         now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert receipt["stage"] == "present" and receipt["exit_code"] == 0
    assert receipt["backend_sha256"] != receipt["output_sha256"]
    assert run.call_args_list[0].args[0] == ["aws", "sts", "get-caller-identity", "--output", "json"]
    assert run.call_args_list[1].args[0] == ["terraform", "init", "-input=false", "-reconfigure",
                                              f"-backend-config={backend}"]
    assert run.call_args_list[2].args[0] == ["terraform", "plan", "-detailed-exitcode", "-input=false",
                                              "-no-color", f"-var-file={variables}"]
    assert all(call.kwargs["cwd"] == audit.INSTANCE and call.kwargs["capture_output"] is True
               for call in run.call_args_list[1:])
    run = Mock(side_effect=[identity(), CompletedProcess([], 0, b"", b""),
                            CompletedProcess([], 2, b"private drift", b"")])
    with pytest.raises(audit.AuditError, match="instance_plan_not_zero_drift"):
        audit.plan("after_teardown", FIXTURES, backend, variables, *approved(backend, variables), run=run)


def test_unreviewed_or_symlinked_plan_inputs_never_start_terraform(tmp_path):
    backend, variables = private(tmp_path, "backend.tfvars"), private(tmp_path, "variables.tfvars")
    runner = Mock()
    backend.chmod(0o644)
    with pytest.raises(audit.AuditError, match="private_audit_input_required"):
        audit.plan("absent", FIXTURES, backend, variables, *approved(backend, variables), run=runner)
    backend.chmod(0o600)
    link = tmp_path / "backend-link.tfvars"
    link.symlink_to(backend)
    with pytest.raises(audit.AuditError, match="private_audit_input_required"):
        audit.plan("absent", FIXTURES, link, variables, *approved(backend, variables), run=runner)
    runner.assert_not_called()


def test_plan_rejects_wrong_role_and_unapproved_inputs(tmp_path):
    backend, variables = private(tmp_path, "backend.tfvars"), private(tmp_path, "variables.tfvars")
    role, backend_digest, vars_digest = approved(backend, variables)
    runner = Mock()
    with pytest.raises(audit.AuditError, match="instance_plan_inputs_not_approved"):
        audit.plan("present", FIXTURES, backend, variables, role, "f" * 64, vars_digest, run=runner)
    runner.assert_not_called()
    runner.side_effect = [identity("other-role")]
    with pytest.raises(audit.AuditError, match="instance_plan_identity_wrong"):
        audit.plan("present", FIXTURES, backend, variables, role, backend_digest, vars_digest, run=runner)
    runner.assert_called_once()


def sample_scan(tmp_path, log=b"synthetic log with no credentials"):
    runs = [{"run_id": 30, "workflow": "gitlab-infra-apply.yml", "head_sha": FIXTURES["source_revision"],
             "conclusion": "success", "job_ids": [100]},
            {"run_id": 31, "workflow": "gitlab-runners-apply.yml", "head_sha": FIXTURES["source_revision"],
             "conclusion": "failure", "job_ids": [101]}]
    logs = {(30, 100): private(tmp_path, "first.log"), (31, 101): private(tmp_path, "second.log", log)}
    state = [private(tmp_path, "state.json")]
    artifacts = [private(tmp_path, "artifact.json")]
    return runs, logs, state, artifacts


def test_scan_reads_all_job_logs_state_and_artifacts_without_returning_contents(tmp_path):
    runs, logs, state, artifacts = sample_scan(tmp_path)
    receipt = audit.scan(FIXTURES, runs, logs, state, artifacts, ["synthetic-private-canary"])
    assert receipt["log_job_ids"] == [100, 101] and receipt["state_count"] == receipt["artifact_count"] == 1
    assert len(receipt["sha256s"]) == 4
    assert "synthetic-safe-content" not in str(receipt)


@pytest.mark.parametrize("mutation,reason", [
    ("missing-log", "secret_scan_coverage_missing"),
    ("missing-state", "secret_scan_coverage_missing"),
    ("wrong-revision", "secret_scan_run_wrong"),
    ("canary", "secret_material_exposed"),
    ("token", "secret_material_exposed"),
    ("ca", "secret_material_exposed"),
])
def test_scan_fails_closed_on_missing_inputs_or_real_leaks(tmp_path, mutation, reason):
    runs, logs, state, artifacts = sample_scan(tmp_path)
    if mutation == "missing-log":
        logs.pop((31, 101))
    elif mutation == "missing-state":
        state.clear()
    elif mutation == "wrong-revision":
        runs[0]["head_sha"] = "b" * 40
    elif mutation in {"canary", "token", "ca"}:
        logs[(31, 101)].write_bytes({"canary": b"synthetic-private-canary", "token": b"glrt-syntheticprivatetoken",
                                       "ca": b"BEGIN CERTIFICATE"}[mutation])
    with pytest.raises(audit.AuditError, match=reason):
        audit.scan(FIXTURES, runs, logs, state, artifacts, ["synthetic-private-canary"])


def test_github_job_logs_use_reviewed_run_and_complete_provider_inventory(tmp_path):
    tmp_path.chmod(0o700)
    def gh(command, **kwargs):
        endpoint = command[-1]
        if endpoint.endswith("runs/30"):
            payload = {"id": 30, "path": ".github/workflows/gitlab-infra-apply.yml", "head_branch": "main",
                       "event": "workflow_dispatch", "head_sha": FIXTURES["source_revision"],
                       "conclusion": "success"}
        elif endpoint.endswith("runs/30/jobs?per_page=100&page=1"):
            payload = {"total_count": 2, "jobs": [{"id": 100}, {"id": 101}]}
        else:
            assert endpoint.endswith(("jobs/100/logs", "jobs/101/logs"))
            return CompletedProcess([], 0, b"synthetic complete log", b"")
        return CompletedProcess([], 0, json.dumps(payload).encode(), b"")
    provider = Mock(side_effect=gh)
    runs, logs = audit.github_logs(FIXTURES, [30], tmp_path, gh=provider)
    assert runs[0]["job_ids"] == [100, 101] and set(logs) == {(30, 100), (30, 101)}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in logs.values())
    assert provider.call_count == 4


@pytest.mark.parametrize("mutation,reason", [
    ("wrong-source", "github_qualification_run_wrong"),
    ("wrong-ref", "github_qualification_run_wrong"),
    ("wrong-workflow", "github_qualification_run_wrong"),
    ("missing-branch", "github_qualification_run_wrong"),
    ("incomplete", "github_job_inventory_incomplete"),
    ("unavailable-log", "github_qualification_readback_missing"),
])
def test_github_provider_readback_refuses_mismatched_or_partial_runs(tmp_path, mutation, reason):
    tmp_path.chmod(0o700)
    def gh(command, **kwargs):
        endpoint = command[-1]
        if endpoint.endswith("runs/30"):
            payload = {"id": 30, "path": ".github/workflows/gitlab-runners-apply.yml", "head_branch": "main",
                       "event": "workflow_dispatch", "head_sha": FIXTURES["source_revision"],
                       "conclusion": "success"}
            if mutation == "wrong-source":
                payload["head_sha"] = "b" * 40
            if mutation == "wrong-ref":
                payload["head_branch"] = "feature"
            if mutation == "wrong-workflow":
                payload["path"] = ".github/workflows/unrelated.yml"
            if mutation == "missing-branch":
                del payload["head_branch"]
        elif endpoint.endswith("/jobs?per_page=100&page=1"):
            payload = {"total_count": 2 if mutation == "incomplete" else 1, "jobs": [{"id": 100}]}
        else:
            return CompletedProcess([], 1 if mutation == "unavailable-log" else 0, b"", b"sensitive provider error")
        return CompletedProcess([], 0, json.dumps(payload).encode(), b"")
    with pytest.raises(audit.AuditError, match=reason):
        audit.github_logs(FIXTURES, [30], tmp_path, gh=gh)


def test_operator_cli_masks_provider_errors_and_writes_no_false_receipt(tmp_path, capsys):
    fixtures = private(tmp_path, "fixtures.json", json.dumps(FIXTURES).encode())
    canaries = private(tmp_path, "canaries.json", b'["synthetic-private-canary"]')
    output = tmp_path / "receipt.json"
    tmp_path.chmod(0o700)
    args = ["ec04.py", "--fixtures", str(fixtures), "--scan-logs", "--run-id", "30",
            "--audit-directory", str(tmp_path), "--state", str(fixtures), "--artifact", str(canaries),
            "--canaries-file", str(canaries), "--output", str(output)]
    with patch("sys.argv", args), patch.object(audit, "github_logs", side_effect=RuntimeError("synthetic-private-canary")):
        assert audit.main() == 1
    assert "synthetic-private-canary" not in capsys.readouterr().out
    assert not output.exists()


def native_scope():
    workflow = 'gitlab-infra-apply.yml'
    reference = 's3://adp-dev-deployment-evidence-123456789012/deployment-evidence/v1/456/30/1/context/' + workflow + '.json?versionId=version1'
    expected = {'run_id': 30, 'run_attempt': 1, 'repository_id': 456, 'source_revision': 'b' * 40,
                'workflow_revision': 'c' * 40, 'correlation': 'orch:approved', 'contract_digest': 'd' * 64,
                'context_sha256': 'e' * 64, 'context_reference': reference, 'region': 'us-east-1'}
    fixtures = {**FIXTURES, 'workflow_sources': {workflow: expected}}
    context = {k: expected[k] for k in ('repository_id', 'run_id', 'run_attempt', 'source_revision', 'workflow_revision', 'correlation', 'region')}
    context.update(account_id=FIXTURES['account_id'], workflow_path='.github/workflows/' + workflow,
                   inputs={'adp_contract_digest': expected['contract_digest']})
    details = {'id': 30, 'run_attempt': 1, 'repository': {'id': 456}, 'path': '.github/workflows/' + workflow,
               'head_sha': expected['workflow_revision'], 'head_branch': 'main', 'event': 'workflow_dispatch', 'conclusion': 'success'}
    return workflow, fixtures, expected, context, details


def test_native_audit_authenticates_app_source_separately_from_workflow_revision(tmp_path):
    tmp_path.chmod(0o700)
    workflow, fixtures, expected, context, details = native_scope()
    store = Mock();store.read.return_value = (json.dumps(context).encode(), expected['context_sha256'], expected['context_reference'])
    def gh(command, **kwargs):
        endpoint = command[-1]
        if endpoint.endswith('runs/30'):payload = details
        elif '/jobs?' in endpoint:payload = {'total_count': 1, 'jobs': [{'id': 100}]}
        else:return CompletedProcess([], 0, b'complete private log', b'')
        return CompletedProcess([], 0, json.dumps(payload).encode(), b'')
    rows, logs = audit.github_logs(fixtures, [30], tmp_path, gh=gh, evidence_store=store)
    assert rows[0]['head_sha'] != rows[0]['source_revision'] != fixtures['source_revision']
    assert audit.workflow_run_matches(fixtures, rows[0]) and set(logs) == {(30, 100)}
    assert store.read.call_args.kwargs['workflow_revision'] == details['head_sha']
    assert store.read.call_args.kwargs['run_id'] == 30 and store.read.call_args.kwargs['attempt'] == 1
    changed = {**rows[0], 'source_revision': 'f' * 40}
    assert not audit.workflow_run_matches(fixtures, changed)


@pytest.mark.parametrize('mutation', ['source', 'correlation', 'contract', 'digest', 'reference', 'run_attempt', 'missing', 'signature'])
def test_native_audit_rejects_changed_or_unauthenticated_context(mutation):
    workflow, fixtures, expected, context, details = native_scope()
    digest, reference = expected['context_sha256'], expected['context_reference']
    if mutation == 'source':context['source_revision'] = 'f' * 40
    if mutation == 'correlation':context['correlation'] = 'orch:other'
    if mutation == 'contract':context['inputs']['adp_contract_digest'] = 'f' * 64
    if mutation == 'digest':digest = 'f' * 64
    if mutation == 'reference':reference += 'changed'
    if mutation == 'run_attempt':details['run_attempt'] = 2
    store = Mock();store.read.return_value = (json.dumps(context).encode(), digest, reference)
    if mutation == 'missing':store.read.return_value = None
    if mutation == 'signature':store.read.side_effect = ValueError('signature_invalid')
    with pytest.raises(ValueError):audit.authenticated_context(fixtures, workflow, details, store)
