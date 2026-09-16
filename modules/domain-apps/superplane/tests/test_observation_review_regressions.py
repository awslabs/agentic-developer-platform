"""Regression inputs derived from wire clients and cross-workspace attacks."""

from dataclasses import replace

from superplane_contracts import canonical_body

import _contracts_path  # noqa: F401
from conftest import OBSERVED_AT, TEST_SIGNING_KEY, W1, W2
from superplane_contracts import ClusterRef, authorize_submit, verify_signature


def test_non_ascii_signature_is_a_normal_refusal(w1_observation):
    assert not verify_signature(w1_observation, TEST_SIGNING_KEY, "sha256=é")


def test_payload_cannot_relabel_another_workspaces_cluster(
    w1_submitter, w2_observation
):
    forged = replace(
        w2_observation, subject=ClusterRef(w2_observation.subject.cluster_id, W1)
    )
    assert not authorize_submit(w1_submitter, forged, cluster_workspace=W2).allowed


def test_go_sender_signature_verifies_over_raw_wire_bytes():
    import base64
    import json
    from pathlib import Path
    from superplane_contracts import (
        AUTH_HEADER,
        SIGNATURE_HEADER,
        VERSION_HEADER,
        Submitter,
        verify_submission,
    )

    vector = json.loads(
        (Path(__file__).parent / "fixtures/go-observation-v1.json").read_text()
    )
    raw = base64.b64decode(vector["body_base64"])

    class Resolver:
        def resolve(self, credential):
            return (
                Submitter("monitor", frozenset({W1}))
                if credential == "test-credential"
                else None
            )

    headers = {
        AUTH_HEADER: "test-credential",
        SIGNATURE_HEADER: vector["signature"],
        VERSION_HEADER: "v1",
    }
    assert verify_submission(
        raw, headers, Resolver(), TEST_SIGNING_KEY, now=OBSERVED_AT
    ).authenticated
    # Re-encoding parsed JSON changes Unicode escaping, numeric formatting and
    # HTML escaping. The signature belongs only to the actual transmitted bytes.
    reencoded = json.dumps(
        json.loads(raw), sort_keys=True, separators=(",", ":")
    ).encode()
    assert reencoded != raw
    assert not verify_submission(
        reencoded, headers, Resolver(), TEST_SIGNING_KEY, now=OBSERVED_AT
    ).authenticated


def test_stale_and_future_signed_observations_are_refused(w1_observation):
    from datetime import timedelta
    from test_observation_auth import _headers, _resolver
    from superplane_contracts import verify_submission

    for delta in (timedelta(minutes=6), -timedelta(seconds=31)):
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT + delta,
        )
        assert not result.authenticated
        assert result.submitter is None
        assert result.reason == "observation outside freshness window"


def test_invalid_signature_never_returns_an_authenticated_identity(w1_observation):
    from test_observation_auth import _headers, _resolver
    from superplane_contracts import SIGNATURE_HEADER, verify_submission

    result = verify_submission(
        canonical_body(w1_observation),
        _headers(w1_observation, **{SIGNATURE_HEADER: "sha256=é"}),
        _resolver(),
        TEST_SIGNING_KEY,
        now=OBSERVED_AT,
    )
    assert not result.authenticated
    assert result.submitter is None


def test_cluster_ownership_is_required_even_with_a_workspace_grant(
    w1_submitter, w1_observation
):
    assert not authorize_submit(w1_submitter, w1_observation).allowed
    assert authorize_submit(w1_submitter, w1_observation, cluster_workspace=W1).allowed


def test_nonfinite_budget_values_are_rejected():
    from datetime import timedelta
    import pytest
    from superplane_contracts import BudgetUsage, ContractViolation

    for amount in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ContractViolation):
            BudgetUsage(W1, OBSERVED_AT, OBSERVED_AT + timedelta(hours=1), amount)


def _verify_wire(payload):
    import json
    from test_observation_auth import _resolver, _VALID_CREDENTIAL
    from superplane_contracts import (
        AUTH_HEADER,
        SIGNATURE_HEADER,
        VERSION_HEADER,
        compute_signature,
        verify_submission,
    )

    raw = json.dumps(payload).encode()
    return verify_submission(
        raw,
        {
            AUTH_HEADER: _VALID_CREDENTIAL,
            VERSION_HEADER: "v1",
            SIGNATURE_HEADER: compute_signature(raw, TEST_SIGNING_KEY),
        },
        _resolver(),
        TEST_SIGNING_KEY,
        now=OBSERVED_AT,
    )


def test_wire_versions_refuse_non_strings_before_identity_lookup(w1_observation):
    from superplane_contracts import check_version

    for value in (1, True, ["v1"], {"version": "v1"}, None):
        assert not check_version("v1", value).accepted
        assert not check_version(value, "v1").accepted
        payload = w1_observation.to_wire()
        payload["contract_version"] = value
        result = _verify_wire(payload)
        assert not result.authenticated
        assert result.submitter is None


def test_signed_wire_cannot_bypass_probe_honesty(w1_observation):
    for change in (
        {"observed_at": None},
        {"error": "connection refused"},
        {"status": "not_checked", "observed_at": None, "reason": "disabled"},
        {"observed_at": "2026-09-16T12:00:00"},
        {"detail": ["ok"]},
    ):
        payload = w1_observation.to_wire()
        payload["checks"][0].update(change)
        result = _verify_wire(payload)
        assert not result.authenticated
        assert result.reason == "invalid observation body"
        assert result.submitter is None
        assert result.observation is None


def test_received_observation_is_validated_before_use(w1_observation):
    from superplane_contracts import CheckResult, Observation

    for observation in (
        w1_observation,
        replace(w1_observation, checks=(CheckResult.not_checked("eks", "disabled"),)),
        replace(w1_observation, labels=(("region", "London"),)),
    ):
        assert Observation.from_wire(observation.to_wire()) == observation
        result = _verify_wire(observation.to_wire())
        assert result.authenticated
        assert result.observation == observation
    payload = w1_observation.to_wire()
    payload["status"] = "unreachable"
    assert not _verify_wire(payload).authenticated


def test_malformed_wire_schema_is_a_normal_refusal(w1_observation):
    for field, value in (
        ("subject", None),
        ("checks", {}),
        ("checks", [1]),
        ("labels", []),
        ("labels", {"region": 42}),
        ("reporter", 42),
        ("kind", "unknown"),
        ("checks", []),
        ("budget", None),
        ("reported_at", "invalid"),
    ):
        payload = w1_observation.to_wire()
        payload[field] = value
        assert not _verify_wire(payload).authenticated


def test_budget_wire_is_validated_including_overflow(w1_observation):
    from datetime import timedelta
    from superplane_contracts import BudgetUsage

    observation = replace(
        w1_observation,
        kind="budget_usage",
        checks=(),
        budget=BudgetUsage(W1, OBSERVED_AT - timedelta(hours=1), OBSERVED_AT, 2.5),
    )
    assert _verify_wire(observation.to_wire()).observation == observation
    for field, value in (
        ("observed_spend_usd", True),
        ("observed_spend_usd", "5"),
        ("observed_spend_usd", -1),
        ("currency", "GBP"),
        ("workspace", W2),
        ("window_start", "bad"),
    ):
        payload = observation.to_wire()
        payload["budget"][field] = value
        assert not _verify_wire(payload).authenticated
    payload = observation.to_wire()
    payload["budget"]["observed_spend_usd"] = float("inf")
    from superplane_contracts import ContractViolation, Observation
    import pytest

    with pytest.raises(ContractViolation):
        Observation.from_wire(payload)
