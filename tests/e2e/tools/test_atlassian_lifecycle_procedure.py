"""Generated SCN-01 procedure: fake expiry is not a real refresh result."""

import pytest

from tests.e2e.tools.atlassian_oauth_probe import ProbeBlocked, proof_auth_lifecycle
from tests.e2e.tools.test_atlassian_oauth_probe import IdentityFixture
from tests.e2e.tools.test_atlassian_probe_ledger import ledger_for


class ExpiringIdentity(IdentityFixture):
    def __init__(self, oracle, stale=False, denied=False):
        super().__init__("adapter")
        self.oracle = oracle
        self.stale = stale
        self.denied = denied

    def get_workload_access_token_for_user_id(self, **kwargs):
        if self.denied:
            raise PermissionError("adapter role denied")
        return super().get_workload_access_token_for_user_id(**kwargs)

    def get_resource_oauth2_token(self, **kwargs):
        self.calls.append(("resource", kwargs))
        generation = 0 if self.stale else self.oracle.generation
        return {"sessionStatus": "COMPLETE", "accessToken": f"transient-sentinel-{generation}"}


class IndependentOracle:
    generation = 0

    def __init__(self, subject="subject-a", site="site-a"):
        self.subject = subject
        self.site = site
        self.calls = 0

    def observe(self, token):
        self.calls += 1
        if token != f"transient-sentinel-{self.generation}":
            raise ProbeBlocked("Fixture rejected the expired access token")
        return {"subject": self.subject, "site": self.site}

    def call_count(self):
        return self.calls

    def expire(self, owner):
        assert owner == "owner-a"
        self.generation += 1
        return True


class ConsentFixture:
    def __init__(self, approved=True):
        self.approved = approved
        self.calls = []

    def authorize(self, owner, url, state):
        assert owner == "owner-a" and url == "https://id.example.test/consent" and state
        self.calls.append(owner)
        return self.approved

    def call_count(self):
        return len(self.calls)


def test_proof_auth_lifecycle_is_generated_only_and_denies_after_cleanup(tmp_path):
    oracle = IndependentOracle()
    adapter = ExpiringIdentity(oracle)
    core = IdentityFixture("core")
    ledger = ledger_for(tmp_path / "attempt.json", core, adapter)
    report = proof_auth_lifecycle(ledger, "owner-a", "owner-b", ConsentFixture(), oracle, oracle, "subject-a", "site-a")
    assert report == {
        "scenario_id": "SCN-01", "controlled_status": "pass", "evidence_kind": "generated-fixture",
        "provider_status": "unproven", "managed_refresh": "simulated-only", "remote_revocation": "unproven",
        "cleanup": "pending-remote-verification", "role_boundary": "sdk-clients-distinct; live-role-arns-unverified",
    }
    assert oracle.call_count() == 2
    assert len([call for call in core.calls if call[0] == "resource"]) == 1
    assert len([call for call in adapter.calls if call[0] == "resource"]) == 2
    assert ledger.status("owner-a") == "cleanup_pending"
    assert "transient-sentinel" not in ledger.path.read_text()
    with pytest.raises(ProbeBlocked):
        ledger.observe("owner-a", oracle.observe)


@pytest.mark.parametrize("fault", ["wrong_subject", "wrong_site", "stale_after_expiry", "adapter_role_denied", "consent_denied"])
def test_generated_faults_fail_closed_and_keep_cleanup_pending(tmp_path, fault):
    oracle = IndependentOracle(subject="different" if fault == "wrong_subject" else "subject-a", site="different" if fault == "wrong_site" else "site-a")
    adapter = ExpiringIdentity(oracle, stale=fault == "stale_after_expiry", denied=fault == "adapter_role_denied")
    ledger = ledger_for(tmp_path / "attempt.json", IdentityFixture("core"), adapter)
    with pytest.raises(ProbeBlocked, match="Generated Identity lifecycle controls failed"):
        proof_auth_lifecycle(ledger, "owner-a", "owner-b", ConsentFixture(approved=fault != "consent_denied"), oracle, oracle, "subject-a", "site-a")
    assert ledger.status("owner-a") == "cleanup_pending"
    assert "transient-sentinel" not in ledger.path.read_text()


def test_same_owner_cannot_count_as_foreign_denial(tmp_path):
    ledger = ledger_for(tmp_path / "attempt.json", IdentityFixture("core"), ExpiringIdentity(IndependentOracle()))
    with pytest.raises(ValueError):
        proof_auth_lifecycle(ledger, "owner-a", "owner-a", ConsentFixture(), IndependentOracle(), IndependentOracle(), "subject-a", "site-a")
    assert not ledger.path.exists()


def test_lost_core_sdk_reply_stays_pending_without_duplicate_consent(tmp_path):
    class TimedOutCore(IdentityFixture):
        def get_resource_oauth2_token(self, **kwargs):
            self.calls.append(("resource", kwargs))
            raise TimeoutError("SDK reply lost")

    core = TimedOutCore("core")
    ledger = ledger_for(tmp_path / "attempt.json", core, ExpiringIdentity(IndependentOracle()))
    with pytest.raises(ProbeBlocked):
        proof_auth_lifecycle(ledger, "owner-a", "owner-b", ConsentFixture(), IndependentOracle(), IndependentOracle(), "subject-a", "site-a")
    assert ledger.status("owner-a") == "starting"
    with pytest.raises(ProbeBlocked):
        ledger.begin("owner-a")
    assert len([call for call in core.calls if call[0] == "resource"]) == 1
