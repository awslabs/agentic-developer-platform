import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from modules.tools.scripts.release_journal import bindings_sha256, decision, finish, initialize, load, prerequisites, start


SOURCE = "a" * 40
ACCOUNT = "123456789012"
TARGET = {"account": ACCOUNT, "region": "us-east-1", "environment": "dev", "source": SOURCE}
DIGEST = "sha256:" + "b" * 64
BINDINGS = {"ADP_TOOLS_ACCOUNT_ID": ACCOUNT, "ADP_TOOLS_AWS_REGION": "us-east-1",
            "ADP_TOOLS_ENVIRONMENT": "dev", "ADP_TOOLS_STATE_BUCKET": "fictional-state",
            "ADP_TOOLS_TFVARS_JSON": json.dumps({"aws_account_id": ACCOUNT, "name": "fictional-tools"}),
            "ADP_TOOLS_DEPLOY_ROLE_ARN": "fictional-protected-role",
            "ADP_TOOLS_EKS_CLUSTER_NAME": "fictional-cluster"}


def receipt(**overrides):
    return {**{"stage": "infra", "source_revision": SOURCE, "account": ACCOUNT,
               "region": "us-east-1", "environment": "dev", "mode": "apply",
               "run_id": 18, "run_attempt": 2, "bindings_sha256": bindings_sha256(BINDINGS),
               "evidence_type": "independent-runtime",
               "expected_runtime": {"gateway": DIGEST}, "observed_runtime": {"gateway": DIGEST}},
            **overrides}


def journal_and_receipt(tmp_path):
    path = tmp_path / "release.json"
    initialize(path, **TARGET, bindings=BINDINGS)
    start(path, "infra", run_id=18, run_attempt=2, bindings=BINDINGS)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    manifest_path = receipts / "manifest.json"
    manifest_path.write_text(json.dumps({"source_revision": SOURCE, "account": ACCOUNT,
                                         "region": "us-east-1", "environment": "dev",
                                         "bindings_sha256": bindings_sha256(BINDINGS), "stages": {"infra": {"gateway": DIGEST}}}))
    manifest_path.chmod(0o600)
    receipt_path = receipts / "infra.json"
    receipt_path.write_text(json.dumps(receipt()))
    receipt_path.chmod(0o600)
    return path, receipts, receipt_path


def test_interrupted_stage_is_saved_before_apply_and_skipped_only_after_readback(tmp_path):
    journal, receipts, receipt_path = journal_and_receipt(tmp_path)
    saved = json.loads(journal.read_text())
    assert journal.stat().st_mode & 0o077 == 0
    assert saved["stages"] == {"infra": {"run_id": 18, "run_attempt": 2, "status": "started"}}
    with pytest.raises(ValueError, match="Coordinator: independently verify infra"):
        decision(journal, "infra", bindings=BINDINGS, resume=True)
    assert json.loads(journal.read_text()) == saved
    assert decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts) == "skip"
    assert decision(journal, "targets", bindings=BINDINGS, resume=True) == "run"
    verified = load(journal)
    assert verified["stages"]["infra"]["status"] == "receipt-verified"
    assert len(verified["stages"]["infra"]["receipt_sha256"]) == 64
    assert decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts) == "skip"
    receipt_path.write_text(json.dumps(receipt(observed_runtime={"gateway": "sha256:" + "c" * 64})))
    with pytest.raises(ValueError, match="partial or does not match"):
        decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts)
    assert load(journal) == verified


def test_command_success_is_not_runtime_verification(tmp_path):
    journal, receipts, _ = journal_and_receipt(tmp_path)
    finish(journal, "infra", bindings=BINDINGS)
    assert load(journal)["stages"]["infra"]["status"] == "command-succeeded"
    with pytest.raises(ValueError, match="before any repeated stage"):
        decision(journal, "infra", bindings=BINDINGS, resume=False, receipts_dir=receipts)
    with pytest.raises(ValueError, match="never implicitly repeat"):
        initialize(journal, **TARGET, bindings=BINDINGS)
    with pytest.raises(ValueError, match="never resume or overwrite another source"):
        initialize(journal, **{**TARGET, "source": "d" * 40}, bindings=BINDINGS, resume=True)
    assert load(journal)["stages"]["infra"]["status"] == "command-succeeded"


@pytest.mark.parametrize("changes", [
    {"mode": "plan"},
    {"run_attempt": 3},
    {"source_revision": "d" * 40},
    {"account": "999999999999"},
    {"expected_runtime": {"gateway": DIGEST, "worker": DIGEST}},
    {"observed_runtime": {}},
    {"evidence_type": "build-only"},
])
def test_partial_or_foreign_receipt_cannot_skip(tmp_path, changes):
    journal, receipts, path = journal_and_receipt(tmp_path)
    path.write_text(json.dumps(receipt(**changes)))
    saved = load(journal)
    with pytest.raises(ValueError, match="partial or does not match"):
        decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts)
    assert load(journal) == saved


def test_missing_binding_names_coordinator_action_and_does_not_write(tmp_path):
    bindings = {"ADP_TOOLS_ACCOUNT_ID": ACCOUNT, "ADP_TOOLS_AWS_REGION": "us-east-1",
                "ADP_TOOLS_ENVIRONMENT": "dev", "ADP_SOURCE_REVISION": SOURCE,
                "ADP_TOOLS_DEPLOY_ROLE_ARN": "fictional-protected-role",
                "GITHUB_RUN_ID": "18", "GITHUB_RUN_ATTEMPT": "2"}
    with pytest.raises(ValueError, match="Coordinator: supply the approved tools state bucket and Terraform variables"):
        prerequisites(("infra",), bindings)
    bindings.update(ADP_TOOLS_STATE_BUCKET="fictional-state", ADP_TOOLS_TFVARS_JSON=json.dumps({"aws_account_id": ACCOUNT}))
    prerequisites(("infra",), bindings)
    with pytest.raises(ValueError, match="Coordinator: run protected shared-agentcore-tools-deploy.yml"):
        prerequisites(("tool-lambdas",), bindings)
    with pytest.raises(ValueError, match="Coordinator: bind the approved tools EKS cluster"):
        prerequisites(("verify",), bindings)
    assert not any(tmp_path.iterdir())


def test_legacy_journal_and_changed_receipt_are_not_silent_resume(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({**TARGET, "completed": ["infra"]}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="legacy journal"):
        initialize(path, **TARGET, bindings=BINDINGS, resume=True)
    journal, receipts, receipt_path = journal_and_receipt(tmp_path)
    assert decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts) == "skip"
    receipt_path.write_text(json.dumps(receipt(), indent=2))
    with pytest.raises(ValueError, match="receipt or manifest changed after verification"):
        decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts)


def test_partial_receipt_cannot_omit_a_reviewed_image(tmp_path):
    journal, receipts, _ = journal_and_receipt(tmp_path)
    manifest_path = receipts / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stages"]["infra"]["worker"] = "sha256:" + "e" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="partial or does not match"):
        decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts)
    assert load(journal)["stages"]["infra"]["status"] == "started"


@pytest.mark.parametrize("change", [
    {"ADP_TOOLS_STATE_BUCKET": "different-state"},
    {"ADP_TOOLS_TFVARS_JSON": json.dumps({"aws_account_id": ACCOUNT, "name": "other-tools"})},
])
def test_changed_backend_or_private_name_cannot_resume_or_mutate(tmp_path, change):
    journal, receipts, _ = journal_and_receipt(tmp_path)
    saved = load(journal)
    changed = {**BINDINGS, **change}
    assert bindings_sha256(changed) != saved["bindings_sha256"]
    with pytest.raises(ValueError, match="private tools state or configuration changed"):
        initialize(journal, **TARGET, bindings=changed, resume=True)
    with pytest.raises(ValueError, match="private tools state or configuration changed"):
        decision(journal, "infra", bindings=changed, resume=True, receipts_dir=receipts)
    with pytest.raises(ValueError, match="private tools state or configuration changed"):
        start(journal, "targets", run_id=19, run_attempt=1, bindings=changed)
    assert load(journal) == saved


def test_receipt_and_manifest_must_bind_original_configuration(tmp_path):
    journal, receipts, receipt_path = journal_and_receipt(tmp_path)
    receipt_path.write_text(json.dumps(receipt(bindings_sha256="d" * 64)))
    with pytest.raises(ValueError, match="partial or does not match"):
        decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts)
    receipt_path.write_text(json.dumps(receipt()))
    manifest_path = receipts / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["bindings_sha256"] = "e" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="approved stage manifest"):
        decision(journal, "infra", bindings=BINDINGS, resume=True, receipts_dir=receipts)


def test_journal_cli_accepts_environment_and_rejects_changed_bindings(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/release_journal.py"
    journal = tmp_path / "release.json"
    environment = {**os.environ, **BINDINGS}
    environment.pop("PYTHONPATH", None)

    def invoke(*arguments, bindings=environment):
        return subprocess.run([sys.executable, str(script), *map(str, arguments)], cwd=tmp_path,
                              env=bindings, text=True, capture_output=True)

    initialized = invoke("init", journal, ACCOUNT, "us-east-1", "dev", SOURCE, "false")
    assert initialized.returncode == 0, initialized.stderr
    assert load(journal)["bindings_sha256"] == bindings_sha256(BINDINGS)
    assert invoke("decision", journal, "infra", "false", "").stdout.strip() == "run"
    started = invoke("start", journal, "infra", "18", "2")
    assert started.returncode == 0, started.stderr
    finished = invoke("finish", journal, "infra")
    assert finished.returncode == 0, finished.stderr
    assert load(journal)["stages"]["infra"]["status"] == "command-succeeded"
    changed = {**environment, "ADP_TOOLS_STATE_BUCKET": "other-state"}
    refused = invoke("decision", journal, "infra", "true", "", bindings=changed)
    assert refused.returncode != 0 and "private tools state or configuration changed" in refused.stderr


def test_receipt_cli_supports_path_and_package_invocation_without_pythonpath(tmp_path):
    root = Path(__file__).resolve().parents[4]
    script = root / "modules/tools/scripts/verify_release.py"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    for command, working_directory in (([sys.executable, str(script)], tmp_path),
                                       ([sys.executable, "-m", "modules.tools.scripts.verify_release"], root)):
        result = subprocess.run([*command, "--help"], cwd=working_directory,
                                env=environment, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert "{shared,evaluation}" in result.stdout
