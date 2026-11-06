"""Controlled Jira fixture cleanup; no live-provider assertion."""

import stat

import pytest

from tests.e2e.tools.atlassian_fixture_cleanup import JiraFixtureLedger


class ProviderFixture:
    def __init__(self):
        self.issues = {}
        self.deletions = []
        self.lose_ack = False
        self.complete = True

    def add(self, record, issue_id, **changes):
        self.issues[issue_id] = {**{field: record[field] for field in (
            "tenant_id", "owner_id", "run_id", "site_id", "project_key", "marker")},
                                 "id": issue_id, **changes}

    def read_issues(self, *, site_id, project_key, marker):
        return {"complete": self.complete, "issues": [issue.copy() for issue in self.issues.values()
                if issue["site_id"] == site_id and issue["project_key"] == project_key
                and issue["marker"] == marker]}

    def read_issue(self, issue_id, *, site_id):
        issue = self.issues.get(issue_id)
        return issue.copy() if issue is not None and issue["site_id"] == site_id else None

    def delete_owned_issue(self, issue_id, **ownership):
        if any(self.issues[issue_id][field] != value for field, value in ownership.items()):
            raise ValueError("Issue ownership changed before deletion")
        self.deletions.append(issue_id)
        del self.issues[issue_id]
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError("provider committed but reply was lost")


def ledger(tmp_path, **changes):
    return JiraFixtureLedger(tmp_path / "jira.json", **{
        "tenant_id": "tenant-one", "owner_id": "owner-one", "run_id": "run-one",
        "site_id": "site-one", "project_key": "FIX", "operation_id": "operation-one", **changes,
    })


def test_interrupted_cleanup_resumes_after_lost_delete_ack_and_preserves_unrelated(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    reserved = fixture.reserve()
    assert stat.S_IMODE(fixture.path.stat().st_mode) == 0o600
    provider = ProviderFixture()
    provider.add(reserved, "issue-owned")
    provider.add({**reserved, "marker": "unrelated"}, "issue-unrelated")
    fixture.confirm_created(provider, "issue-owned")
    provider.lose_ack = True
    with pytest.raises(ConnectionError, match="reply was lost"):
        fixture.cleanup(provider, pending_operations=lambda operation: False)
    assert ledger(private).state()["cleanup_status"] == "pending"
    assert ledger(private).cleanup(provider, pending_operations=lambda operation: False) == {
        "status": "confirmed", "owned_absent": True}
    assert provider.deletions == ["issue-owned"]
    assert "issue-unrelated" in provider.issues
    assert ledger(private).state()["issue_ids"] == ["issue-owned"]


def test_uncertain_create_and_pending_journal_keep_run_incomplete(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    reserved = fixture.reserve()
    provider = ProviderFixture()
    assert fixture.cleanup(provider, pending_operations=lambda operation: True)["status"] == "pending"
    provider.add(reserved, "issue-lost-response")
    assert ledger(private).cleanup(provider, pending_operations=lambda operation: True) == {
        "status": "pending", "owned_absent": False}
    assert provider.deletions == []
    assert ledger(private).state()["issue_ids"] == ["issue-lost-response"]
    fixture.confirm_created(provider, "issue-lost-response")
    assert ledger(private).cleanup(provider, pending_operations=lambda operation: False)["status"] == "confirmed"
    assert provider.deletions == ["issue-lost-response"]


def test_foreign_owner_site_or_marker_refuses_cleanup_before_effect(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    reserved = fixture.reserve()
    provider = ProviderFixture()
    provider.add(reserved, "issue-foreign", owner_id="foreign-owner")
    with pytest.raises(ValueError, match="Unowned"):
        fixture.cleanup(provider, pending_operations=lambda operation: False)
    assert provider.deletions == []
    with pytest.raises(ValueError, match="different run"):
        ledger(private, owner_id="foreign-owner").state()
    with pytest.raises(ValueError, match="different run"):
        ledger(private, site_id="site-two").cleanup(provider, pending_operations=lambda operation: False)
    with pytest.raises(ValueError, match="reserved"):
        fixture.reserve()
    provider.complete = False
    with pytest.raises(ValueError, match="incomplete"):
        fixture.cleanup(provider, pending_operations=lambda operation: False)
    assert fixture.state()["cleanup_status"] == "pending"


def test_orphan_and_untrusted_ledger_never_confirm_cleanup(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    record = fixture.reserve()
    provider = ProviderFixture()
    provider.add(record, "issue-orphan")
    with pytest.raises(ValueError, match="independent ownership"):
        fixture.confirm_created(provider, "different-issue")
    provider.lose_ack = True
    with pytest.raises(ConnectionError):
        fixture.cleanup(provider, pending_operations=lambda operation: False)
    assert ledger(private).state()["cleanup_status"] == "pending"
    fixture.path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        ledger(private).state()
    fixture.path.unlink()
    fixture.path.symlink_to(private / "elsewhere")
    with pytest.raises(ValueError, match="symlink|private"):
        ledger(private)


def test_changed_recorded_issue_cannot_be_deleted_or_counted_clean(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    reserved = fixture.reserve()
    provider = ProviderFixture()
    provider.add(reserved, "issue-owned")
    fixture.confirm_created(provider, "issue-owned")
    provider.issues["issue-owned"]["marker"] = "marker-was-changed"
    with pytest.raises(ValueError, match="Known Jira fixture changed"):
        ledger(private).cleanup(provider, pending_operations=lambda operation: False)
    assert provider.deletions == [] and fixture.state()["cleanup_status"] == "pending"


def test_deletion_rechecks_ownership_after_provider_readback(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    reserved = fixture.reserve()
    provider = ProviderFixture()
    provider.add(reserved, "issue-owned")
    fixture.confirm_created(provider, "issue-owned")
    original = provider.delete_owned_issue

    def changed_owner(issue_id, **ownership):
        provider.issues[issue_id]["owner_id"] = "other-owner"
        return original(issue_id, **ownership)

    provider.delete_owned_issue = changed_owner
    with pytest.raises(ValueError, match="ownership changed"):
        ledger(private).cleanup(provider, pending_operations=lambda operation: False)
    assert provider.deletions == []
    assert ledger(private).state()["cleanup_status"] == "pending"


def test_cleanup_waits_for_every_journaled_fixture_mutation(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    reserved = fixture.reserve()
    provider = ProviderFixture()
    provider.add(reserved, "issue-owned")
    fixture.confirm_created(provider, "issue-owned")
    fixture.track_operation("comment-operation")
    fixture.track_operation("comment-operation")
    assert ledger(private).state()["operation_ids"] == ["operation-one", "comment-operation"]
    assert ledger(private).cleanup(provider, pending_operations=lambda operation: operation == "comment-operation") == {
        "status": "pending", "owned_absent": False}
    assert provider.deletions == []
    assert ledger(private).state()["cleanup_status"] == "pending"
    assert ledger(private).cleanup(provider, pending_operations=lambda operation: False) == {
        "status": "confirmed", "owned_absent": True}
    assert provider.deletions == ["issue-owned"]
    with pytest.raises(ValueError, match="reservation"):
        fixture.track_operation("late-operation")


def test_restarted_cleanup_rechecks_absence_instead_of_trusting_previous_confirmation(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    reserved = fixture.reserve()
    provider = ProviderFixture()
    provider.add(reserved, "issue-owned")
    fixture.confirm_created(provider, "issue-owned")
    assert fixture.cleanup(provider, pending_operations=lambda operation: False)["status"] == "confirmed"
    provider.add(reserved, "issue-late")
    assert ledger(private).cleanup(provider, pending_operations=lambda operation: True) == {
        "status": "pending", "owned_absent": False}
    assert provider.deletions == ["issue-owned"]
    assert ledger(private).cleanup(provider, pending_operations=lambda operation: False)["status"] == "confirmed"
    assert provider.deletions == ["issue-owned", "issue-late"]


def test_unavailable_operation_status_never_deletes_the_recovery_oracle(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fixture = ledger(private)
    reserved = fixture.reserve()
    provider = ProviderFixture()
    provider.add(reserved, "issue-owned")
    fixture.confirm_created(provider, "issue-owned")
    with pytest.raises(ValueError, match="status is unavailable"):
        fixture.cleanup(provider, pending_operations=lambda operation: None)
    assert provider.deletions == []
