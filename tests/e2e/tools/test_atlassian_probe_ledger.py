"""Generated restart, replay and loss-of-reply checks for probe metadata."""

import json
import stat
from dataclasses import replace

import pytest

from tests.e2e.tools.atlassian_oauth_probe import IdentityConfig, IdentityLifecycle, ProbeBlocked
from tests.e2e.tools.atlassian_probe_ledger import ProbeLedger
from tests.e2e.tools.test_atlassian_oauth_probe import IdentityFixture, configured_identity


def ledger_for(path, core, adapter):
    lifecycle = IdentityLifecycle(
        configured_identity(), core, adapter
    )
    return ProbeLedger(path, lifecycle)


def test_restart_owner_binding_and_local_denial(tmp_path):
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    path = tmp_path / "metadata.json"
    ledger = ledger_for(path, core, adapter)
    browser = ledger.begin("owner-a")
    assert ledger.status("owner-a") == "consent_pending"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "metadata.json.lock").stat().st_mode) == 0o600
    record = json.loads(path.read_text())
    assert set(record) == {"config_digest", "owner", "session_uri", "state_digest", "status"}
    assert browser["state"] not in path.read_text()
    assert browser["authorization_url"] not in path.read_text()
    with pytest.raises(ProbeBlocked):
        ledger.complete("owner-b", browser["state"])
    with pytest.raises(ProbeBlocked):
        ledger.complete("owner-a", "other-state")
    resumed = ledger_for(path, core, adapter)
    resumed.complete("owner-a", browser["state"])
    assert resumed.status("owner-a") == "completed"
    assert resumed.observe("owner-a", lambda token: token == "ephemeral-sentinel")
    assert "ephemeral-sentinel" not in path.read_text()
    with pytest.raises(ProbeBlocked):
        resumed.complete("owner-a", browser["state"])
    with pytest.raises(ProbeBlocked):
        resumed.observe("owner-b", lambda token: token)
    resumed.deny("owner-a")
    assert resumed.status("owner-a") == "cleanup_pending"
    with pytest.raises(ProbeBlocked):
        resumed.observe("owner-a", lambda token: token)
    assert len(adapter.calls) == 2
    with pytest.raises(ProbeBlocked):
        resumed.begin("owner-a")


def test_lost_start_reply_requires_reconciliation_not_duplicate_consent(tmp_path):
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    core.session_status = "FAILED"
    path = tmp_path / "attempt.json"
    with pytest.raises(ProbeBlocked):
        ledger_for(path, core, adapter).begin("owner-a")
    assert ledger_for(path, core, adapter).status("owner-a") == "starting"
    with pytest.raises(ProbeBlocked):
        ledger_for(path, core, adapter).begin("owner-a")
    assert len([call for call in core.calls if call[0] == "resource"]) == 1


def test_lost_completion_reply_never_replays_exchange(tmp_path):
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    ledger = ledger_for(tmp_path / "attempt.json", core, adapter)
    browser = ledger.begin("owner-a")
    def lost_reply(**kwargs):
        core.calls.append(("complete", kwargs))
        raise RuntimeError("SDK reply lost")
    core.complete_resource_token_auth = lost_reply
    with pytest.raises(RuntimeError):
        ledger.complete("owner-a", browser["state"])
    assert ledger.status("owner-a") == "completing"
    with pytest.raises(ProbeBlocked):
        ledger.complete("owner-a", browser["state"])
    with pytest.raises(ProbeBlocked):
        ledger.observe("owner-a", lambda token: token)
    assert len([call for call in core.calls if call[0] == "complete"]) == 1


def test_reject_unsafe_ledger_or_changed_config(tmp_path):
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    path = tmp_path / "attempt.json"
    ledger_for(path, core, adapter).begin("owner-a")
    changed = IdentityLifecycle(replace(configured_identity(), workload_name="other-workload"), core, adapter)
    with pytest.raises(ProbeBlocked):
        ProbeLedger(path, changed).status("owner-a")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        ledger_for(path, core, adapter).status("owner-a")
    path.unlink()
    path.symlink_to(tmp_path / "target.json")
    with pytest.raises(ValueError):
        ledger_for(path, core, adapter)
