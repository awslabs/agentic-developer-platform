"""Gateway-owned refusal for clients that cannot consume the live policy.

Retained from the operator's independent boundary test against the previous
checkpoint, which found the real bootstrap route returning HTTP 200 and a valid
run credential to the previously shipped request shape for an enforcing proposal,
an enforcing refusal, and an unknown posture.

The property under test is *version skew*, so the transport has to be real: HTTP,
the TokenReview exchange, pod binding, persisted authority and credential
issuance are the actual route and fixtures with AWS mocked.  Only the policy
evaluation result is substituted, because the point is which results the route
may issue authority for — not how those results are computed, which
``test_model_policy.py`` covers against a real database.

No provider or inference call is made anywhere in this module.
"""

from unittest.mock import AsyncMock

import pytest

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.model_policy import MODEL_POLICY_CONTRACT_VERSION
from src.agentauth.workload import WORKLOAD_HEADER
from tests.agentauth.test_bootstrap_routes import (
    http_client,
    kubernetes,  # noqa: F401 - fixture, re-exported for this module to request
    provision,
    store,  # noqa: F401 - fixture, re-exported for this module to request
)

HEADERS = {"X-Caller-Identity": "registered-worker-transport", WORKLOAD_HEADER: "pod-token"}

#: A verified posture with a signed proposal — the only shape an enforcing client
#: may act on.
ENFORCING_PROPOSAL = {"posture": "enforcing", "posture_verified": True, "status": "proposed"}
#: Enforcing, but nothing admissible to enforce.  Under enforcement this must
#: stop the run; letting it continue on the legacy assignment is the bypass.
ENFORCING_REFUSAL = {
    "posture": "enforcing",
    "posture_verified": True,
    "status": "unavailable",
    "reason": "evidence_stale",
}
#: The platform cannot establish what it is enforcing.
UNKNOWN_POSTURE = {
    "posture": None,
    "posture_verified": False,
    "status": "unavailable",
    "reason": "runtime_posture_unavailable",
}
#: The current live configuration: report-only, with no admissible proposal.
#: Legacy behaviour is exactly correct here and must be preserved.
REPORT_ONLY_UNAVAILABLE = {
    "posture": "report_only",
    "posture_verified": True,
    "status": "unavailable",
    "reason": "evidence_stale",
}


def _bootstrap(client, store_, *, body_extra=None):
    envelope, _ = provision(store_)
    body = {"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)}
    body.update(body_extra or {})
    return client.post("/internal/v1/agent/bootstrap", json=body, headers=HEADERS)


def _with_policy(monkeypatch, policy):
    evaluate = AsyncMock(return_value=policy)
    monkeypatch.setattr("src.agentauth.model_policy.bootstrap_model_policy_live", evaluate)
    return evaluate


@pytest.mark.parametrize(
    "policy",
    [ENFORCING_PROPOSAL, ENFORCING_REFUSAL, UNKNOWN_POSTURE],
    ids=["enforcing-proposal", "enforcing-refusal", "unknown-posture"],
)
def test_old_client_gets_no_authority_for_enforcing_or_unknown_policy(store, kubernetes, monkeypatch, policy):  # noqa: F811 - re-exported fixtures, see the import above
    """A previously shipped request shape declares no contract, so it is refused.

    Such a client ignores the ``model_policy`` payload and launches on its legacy
    model assignment.  Issuing it a bound credential would mean the platform
    believed it was enforcing while that run did whatever it used to — enforcement
    defeated by version skew, with nothing in the evidence to show it.
    """
    client, _ = http_client(store, kubernetes, monkeypatch)
    evaluate = _with_policy(monkeypatch, policy)

    response = _bootstrap(client, store)

    # The policy was genuinely evaluated: the refusal is a gateway decision about
    # the result, not a short-circuit that skipped establishing it.
    evaluate.assert_awaited_once()
    assert response.status_code == 409, response.text
    assert "credential" not in response.json()
    assert "posture" not in response.text, "a refusal must not disclose the live posture to the client"


@pytest.mark.parametrize(
    "policy",
    [ENFORCING_REFUSAL, UNKNOWN_POSTURE],
    ids=["enforcing-refusal", "unknown-posture"],
)
def test_a_capable_client_is_also_refused_without_an_admissible_decision(store, kubernetes, monkeypatch, policy):  # noqa: F811 - re-exported fixtures, see the import above
    """Declaring the contract is not a way around the refusal.

    The capability claim only says the client *could* honour an enforcing
    decision. It cannot manufacture one, and it must not soften an enforcing
    failure into a permissive outcome — which is the whole point of refusing at
    the gateway rather than trusting the consumer.
    """
    client, _ = http_client(store, kubernetes, monkeypatch)
    _with_policy(monkeypatch, policy)

    response = _bootstrap(client, store, body_extra={"model_policy_contract": MODEL_POLICY_CONTRACT_VERSION})

    assert response.status_code == 409, response.text
    assert "credential" not in response.json()


def test_a_capable_client_is_admitted_for_an_enforcing_proposal(store, kubernetes, monkeypatch):  # noqa: F811 - re-exported fixtures, see the import above
    """The upgraded-client positive case: a real credential, and the decision."""
    client, _ = http_client(store, kubernetes, monkeypatch)
    _with_policy(monkeypatch, ENFORCING_PROPOSAL)

    response = _bootstrap(client, store, body_extra={"model_policy_contract": MODEL_POLICY_CONTRACT_VERSION})

    assert response.status_code == 200, response.text
    assert response.json()["credential"].startswith("adpr1.")
    assert response.json()["model_policy"] == ENFORCING_PROPOSAL


@pytest.mark.parametrize("contract", [None, MODEL_POLICY_CONTRACT_VERSION])
def test_report_only_unavailable_keeps_legacy_admission_for_every_client(store, kubernetes, monkeypatch, contract):  # noqa: F811 - re-exported fixtures, see the import above
    """The current live configuration must be completely unaffected.

    A verified ``report_only`` with an unavailable proposal is the exact state
    every deployed environment is in today. Both old and new clients keep getting
    ordinary authority, and the run proceeds on its legacy model assignment — this
    is the case the whole change must not regress.
    """
    client, _ = http_client(store, kubernetes, monkeypatch)
    _with_policy(monkeypatch, REPORT_ONLY_UNAVAILABLE)
    extra = {} if contract is None else {"model_policy_contract": contract}

    response = _bootstrap(client, store, body_extra=extra)

    assert response.status_code == 200, response.text
    assert response.json()["credential"].startswith("adpr1.")
    assert response.json()["model_policy"] == REPORT_ONLY_UNAVAILABLE


def test_disabled_posture_admits_an_old_client(store, kubernetes, monkeypatch):  # noqa: F811 - re-exported fixtures, see the import above
    """``disabled`` is non-enforcing, so version skew cannot bypass anything."""
    client, _ = http_client(store, kubernetes, monkeypatch)
    _with_policy(monkeypatch, {"posture": "disabled", "posture_verified": True, "status": "proposed"})

    response = _bootstrap(client, store)

    assert response.status_code == 200, response.text
    assert response.json()["credential"].startswith("adpr1.")


@pytest.mark.parametrize(
    "contract",
    [0, -1, True, 1.5, "1", [1], {"v": 1}, 65],
    ids=["zero", "negative", "bool", "fractional", "string", "list", "object", "over-ceiling"],
)
def test_a_malformed_contract_claim_is_rejected_at_the_boundary(store, kubernetes, monkeypatch, contract):  # noqa: F811 - re-exported fixtures, see the import above
    """The claim is validated strictly, so it cannot be smuggled past the gate.

    ``True`` is the one worth spelling out: ``bool`` is an ``int`` subclass, so a
    non-strict integer field would coerce it to ``1`` and a client sending
    ``true`` would be treated as declaring contract version 1.
    """
    client, _ = http_client(store, kubernetes, monkeypatch)
    evaluate = _with_policy(monkeypatch, ENFORCING_PROPOSAL)

    response = _bootstrap(client, store, body_extra={"model_policy_contract": contract})

    assert response.status_code == 422, response.text
    evaluate.assert_not_awaited()


#: An execution that was never enrolled in the model policy: no snapshot was ever
#: attached, so there is no compatibility class, no proposed model and nothing to
#: enforce. This is the state of every run in every environment today.
NOT_ENROLLED = {
    "posture": None,
    "posture_verified": False,
    "status": "unavailable",
    "reason": "snapshot_missing",
}
#: The same, when the execution has a snapshot but no policy revision/chain
#: binding — also raised before any compatibility class is established.
NOT_ENROLLED_UNBOUND = {
    "posture": None,
    "posture_verified": False,
    "status": "unavailable",
    "reason": "snapshot_binding_missing",
}


@pytest.mark.parametrize(
    "policy",
    [NOT_ENROLLED, NOT_ENROLLED_UNBOUND],
    ids=["snapshot-missing", "binding-missing"],
)
@pytest.mark.parametrize("contract", [None, MODEL_POLICY_CONTRACT_VERSION])
def test_a_run_not_enrolled_in_the_policy_keeps_ordinary_admission(store, kubernetes, monkeypatch, policy, contract):  # noqa: F811 - re-exported fixtures, see the import above
    """Never-enrolled is not unknown posture, and refusing it would be an outage.

    This is the regression that matters most in this file. An unverified posture
    normally means the platform cannot tell what it is enforcing, which is a
    refusal. But a run with no snapshot has no class and no selected model, so
    there is no enforcement to bypass — and since nothing enrols runs while the
    rollout is inactive, treating it as unknown posture refused *every* bootstrap
    in every environment. A gate that takes the whole platform down is not a
    safer gate.
    """
    client, _ = http_client(store, kubernetes, monkeypatch)
    evaluate = _with_policy(monkeypatch, policy)
    extra = {} if contract is None else {"model_policy_contract": contract}

    response = _bootstrap(client, store, body_extra=extra)

    evaluate.assert_awaited_once()
    assert response.status_code == 200, response.text
    assert response.json()["credential"].startswith("adpr1.")
    # Truthfully still unverified: the carve-out admits the run, it does not
    # invent a posture for it.
    assert response.json()["model_policy"] == policy


@pytest.mark.parametrize(
    "reason",
    [
        "runtime_posture_unavailable",
        "runtime_posture_unsupported",
        "posture_revision_unsupported",
        "compatibility_class_unknown",
        "parent_snapshot_missing",
        "snapshot_chain_mismatch",
        "decision_unavailable",
        "model_validation_unavailable",
        None,
        "",
    ],
)
def test_every_other_unverified_reason_is_still_refused(store, kubernetes, monkeypatch, reason):  # noqa: F811 - re-exported fixtures, see the import above
    """The carve-out is a closed allowlist, so new failure modes fail closed.

    Each reason here reaches the route only *after* a compatibility class exists,
    or means the posture read itself failed — in both cases the platform may be
    enforcing and cannot tell. Anything not named in ``_NOT_ENROLLED_REASONS``
    must keep withholding authority, including a missing or empty reason, so that
    a failure mode added later does not silently join the admitted set.
    """
    client, _ = http_client(store, kubernetes, monkeypatch)
    _with_policy(monkeypatch, {"posture": None, "posture_verified": False, "status": "unavailable", "reason": reason})

    response = _bootstrap(client, store)

    assert response.status_code == 409, response.text
    assert "credential" not in response.json()


def test_a_not_enrolled_reason_cannot_admit_an_enforcing_posture(store, kubernetes, monkeypatch):  # noqa: F811 - re-exported fixtures, see the import above
    """A verified enforcing posture is judged on the posture, not the reason.

    Guards the carve-out against being used as a bypass: the admitted set is
    defined by ``posture_verified is False`` *and* a never-enrolled reason. A
    result that claims an enforcing posture is evaluated as enforcing even if it
    also carries ``snapshot_missing``.
    """
    client, _ = http_client(store, kubernetes, monkeypatch)
    _with_policy(
        monkeypatch,
        {"posture": "enforcing", "posture_verified": True, "status": "unavailable", "reason": "snapshot_missing"},
    )

    response = _bootstrap(client, store)

    assert response.status_code == 409, response.text
    assert "credential" not in response.json()
