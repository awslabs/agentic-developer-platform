"""Generated fixtures verify the real-observation gate, never provider acceptance."""

import pytest

from tests.e2e.tools.atlassian_oauth_probe import RESOURCE, ProbeBlocked
from tests.e2e.tools.atlassian_probe_ledger import ProbeLedger
from tests.e2e.tools.atlassian_probe_qualification import qualify_provider_setup
from tests.e2e.tools.test_atlassian_provider_setup import setup_for


def qualified_fixture(tmp_path):
    setup, control = setup_for(tmp_path)
    ledger = ProbeLedger(tmp_path / "consent.json", setup.ledger.lifecycle)
    started = ledger.begin("owner-a")
    ledger.complete("owner-a", started["state"])

    class ProviderReader:
        independent = True
        calls = 0

        def observe(self, token):
            assert token == "ephemeral-sentinel"
            self.calls += 1
            return {
                "resource": RESOURCE,
                "subject": "expected-subject",
                "site": "expected-site",
            }

    return setup, ledger, control, ProviderReader()


def qualify(setup, ledger, reader, **changes):
    inputs = {
        "owner": "owner-a",
        "expected_subject": "expected-subject",
        "expected_site": "expected-site",
        "metadata_sha256": setup.plan.identity.metadata_sha256,
    }
    inputs.update(changes)
    return qualify_provider_setup(setup, ledger, reader, **inputs)


def test_identity_sdk_and_independent_provider_readback_are_required(tmp_path):
    setup, ledger, control, reader = qualified_fixture(tmp_path)
    report = qualify(setup, ledger, reader)
    assert report == {
        "scenario_id": "SCN-01",
        "status": "candidate",
        "evidence_kind": "identity-sdk-and-provider-readback",
        "provider_mode": "retained-deployment-client",
        "metadata_sha256": setup.plan.identity.metadata_sha256,
        "live_status": "unverified",
    }
    assert reader.calls == 1
    assert [name for name, _ in control.calls if name.startswith("create_")] == [
        "create_workload",
        "create_provider",
    ]
    assert "ephemeral-sentinel" not in str(report)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner": "foreign-owner"},
        {"metadata_sha256": "0" * 64},
        {"expected_subject": "foreign-subject"},
        {"expected_site": "foreign-site"},
        {"expected_subject": ""},
    ],
)
def test_missing_owner_pin_or_independent_provider_identity_fails_closed(
    tmp_path, changes
):
    setup, ledger, _, reader = qualified_fixture(tmp_path)
    with pytest.raises(ProbeBlocked):
        qualify(setup, ledger, reader, **changes)


def test_generated_provider_claim_and_missing_reader_do_not_qualify(tmp_path):
    setup, ledger, control, reader = qualified_fixture(tmp_path)
    for missing in (None, {"status": "ready"}, object()):
        with pytest.raises(ProbeBlocked, match="independent provider reader"):
            qualify(setup, ledger, missing)
    assert reader.calls == 0 and control.calls == []


@pytest.mark.parametrize(
    "observation",
    [
        {
            "resource": "https://api.atlassian.com/legacy",
            "subject": "expected-subject",
            "site": "expected-site",
        },
        {"resource": RESOURCE, "subject": "expected-subject", "site": "wrong-site"},
        {"status": "ready"},
        None,
    ],
)
def test_incomplete_or_wrong_provider_observation_is_never_sufficient(
    tmp_path, observation
):
    setup, ledger, _, reader = qualified_fixture(tmp_path)
    reader.observe = lambda _token: observation
    with pytest.raises(ProbeBlocked, match="observations are incomplete"):
        qualify(setup, ledger, reader)


def test_unfinished_consent_and_sdk_errors_do_not_leak_a_token(tmp_path):
    setup, ledger, _, reader = qualified_fixture(tmp_path)
    reader.observe = lambda _token: (_ for _ in ()).throw(
        RuntimeError("ephemeral-sentinel")
    )
    with pytest.raises(ProbeBlocked) as error:
        qualify(setup, ledger, reader)
    assert "ephemeral-sentinel" not in str(error.value)
    ledger.deny("owner-a")
    with pytest.raises(ProbeBlocked):
        qualify(setup, ledger, reader)
