"""Synthetic fail-closed recovery checks; live faults need authorized targets."""

from copy import deepcopy
import os

import pytest

from cleanup import verify_owned_cleanup, verify_scope_denials
from continuation import verify_recovery
from evidence import AttemptJournal
from recovery_live import LiveRecoveryDriver


@pytest.fixture
def recovery():
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    native_job_ref = {"record_type": "job", "record_id": "native-example"}
    attempt = {"attempt_id": "attempt-example", "operation": "submit", "status": "accepted",
               "native_job_ref": native_job_ref}
    before = {"run_id": "run-example", "scope": scope, "input": {"sources": ["source-example"]},
              "admission": {"source_snapshots": ["snapshot-example"]}, "attempts": [attempt]}
    after = deepcopy(before)
    after.update(state="succeeded", provider_state="completed", native_job_ref=native_job_ref,
                 work={"completed_at": "2026-01-01T00:00:00Z"})
    recorded = [{"attempt_id": "attempt-example", "run_id": "run-example", "operation": "submit",
                 "status": "accepted", "work_id": "work-example", "observed_at": "2026-01-01T00:00:00Z"}]
    publication_before = {"publication_id": "publication-example", "result_id": "result-example",
                          "result_version": 1, "schema_version": "1.0", "state": "retry_wait", "attempts": 1}
    publication_after = {**publication_before, "state": "published", "attempts": 2,
                         "receipt": {"receipt_id": "receipt-example", "contribution_id": "publication-example",
                                     "admitted_schema": "1.0"}}
    repair = {"trigger": "minute-rule", "operator_command": False, "work_progressed": True, "max_attempts": 3}
    return before, after, recorded, [{"native_job_ref": native_job_ref}], publication_before, publication_after, repair


def test_uncertain_submission_and_context_publication_recover(recovery):
    assert verify_recovery(*recovery)["attempts"] == 1


def test_attempt_journal_persists_only_allowlisted_metadata(tmp_path, recovery):
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    entry = {**recovery[2][0], "secret": "do-not-write", "native_job_id": "job-example"}
    assert journal.append(entry)["native_job_id"] == "job-example"
    journal.append({**entry, "attempt_id": "second-attempt"})
    assert len(journal.read()) == 2
    assert "do-not-write" not in journal.path.read_text()
    assert os.stat(journal.path).st_mode & 0o077 == 0


def test_attempt_journal_rejects_public_files_and_missing_ids(tmp_path, recovery):
    path = tmp_path / "attempts.jsonl"
    path.write_text("\n")
    path.chmod(0o644)
    journal = AttemptJournal(path)
    with pytest.raises(ValueError, match="private"):
        journal.append(recovery[2][0])
    with pytest.raises(ValueError, match="private"):
        journal.read()
    with pytest.raises(ValueError, match="identity"):
        AttemptJournal(tmp_path / "missing.jsonl").append({"run_id": "run-example"})


def test_live_cleanup_journal_binds_selected_run_and_work(tmp_path):
    driver = object.__new__(LiveRecoveryDriver)
    driver.run_id = "selected-run"
    driver.journal = AttemptJournal(tmp_path / "recovery.jsonl")
    driver._journal_effect("cleanup", "prepared", driver.run_id)
    driver._journal_effect("cleanup", "acknowledged", driver.run_id)
    assert [(entry["run_id"], entry["work_id"], entry["status"])
            for entry in driver.journal.read()] == [
                ("selected-run", "selected-run", "prepared"),
                ("selected-run", "selected-run", "acknowledged")]


def test_foreign_scope_and_revocation_are_denied():
    probes = [{"case": case, "status": 404, "disclosed_resources": []}
              for case in ("foreign_product", "foreign_tenant", "revoked_access")]
    assert len(verify_scope_denials(probes)["denied_cases"]) == 3
    probes[0]["status"] = 200
    with pytest.raises(AssertionError, match="disclosed"):
        verify_scope_denials(probes)


def test_owned_fixtures_removed_linked_resources_retained():
    assert verify_owned_cleanup(["owned-example"], ["linked-example"], {
        "owned-example": "absent", "linked-example": "retained"}) == {
        "owned_absent": 1, "linked_retained": 1}
    with pytest.raises(AssertionError, match="linked resource"):
        verify_owned_cleanup(["owned-example"], ["linked-example"], {
            "owned-example": "absent", "linked-example": "absent"})


@pytest.mark.parametrize("change,message", [
    (lambda before, after, recorded, jobs, prior, later, repair: after["scope"].update(product_id="other"),
     "scope or pinned source"),
    (lambda before, after, recorded, jobs, prior, later, repair: jobs.append(jobs[0]), "another native job"),
    (lambda before, after, recorded, jobs, prior, later, repair: after.update(attempts=[]), "retained attempt"),
    (lambda before, after, recorded, jobs, prior, later, repair: repair.update(operator_command=True),
     "durable recovery"),
    (lambda before, after, recorded, jobs, prior, later, repair: later.update(state="retry_wait"),
     "publication remains"),
    (lambda before, after, recorded, jobs, prior, later, repair: later.update(receipt=None),
     "publication remains"),
    (lambda before, after, recorded, jobs, prior, later, repair: later["receipt"].update(
        contribution_id="foreign-contribution"), "publication remains"),
    (lambda before, after, recorded, jobs, prior, later, repair: recorded.clear(), "journal missing"),
])
def test_recovery_rejects_failure_conditions(recovery, change, message):
    attempt = deepcopy(recovery)
    change(*attempt)
    with pytest.raises(AssertionError, match=message):
        verify_recovery(*attempt)


def test_scope_denial_body_cannot_disclose_resources():
    probes = [{"case": case, "status": 403, "disclosed_resources": []}
              for case in ("foreign_product", "foreign_tenant", "revoked_access")]
    probes[1]["disclosed_resources"] = ["private-result-example"]
    with pytest.raises(AssertionError, match="disclosed"):
        verify_scope_denials(probes)


def test_truncated_journal_rejected(tmp_path, recovery):
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    journal.append(recovery[2][0])
    with journal.path.open("ab") as output:
        output.write(b'{"attempt_id":"truncated"')
    with pytest.raises(ValueError, match="truncated"):
        journal.read()
