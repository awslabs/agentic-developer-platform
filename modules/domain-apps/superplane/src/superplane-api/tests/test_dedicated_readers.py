"""Dedicated renewal changes from native operation to canonical registration."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.routers.dedicated_readers import native_identity
from superplane_bootstrap.state import claim_fingerprint


@pytest.fixture
def native():
    identity = {
        "workspace_id": "22222222-2222-4222-8222-222222222222",
        "org_id": "11111111-1111-4111-8111-111111111111",
        "namespace": "superplane-system",
        "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
    }
    reservation = SimpleNamespace(
        identity_json=json.dumps(identity),
        attempt_token="real-original-claim",
        state="reserved",
    )
    authority = SimpleNamespace(
        **{k: v for k, v in identity.items() if k != "namespace"},
        claim=claim_fingerprint(reservation.attempt_token),
        generation="a" * 64,
        operation_id="original-operation",
        revoked=False,
    )
    progress = {
        "phase": "active",
        "component_inventory_complete": True,
        "component_inventory_mode": "management",
        "management_observation": {
            **identity,
            "operation_id": authority.operation_id,
            "registration_claim": authority.claim,
            "endpoint": "https://workspace.example",
        },
        "workspace-namespace": {
            "phase": "granted",
            "identity": {"uid": "namespace-uid"},
        },
        "components": {
            json.dumps(
                ["ServiceAccount", identity["namespace"], "superplane-controller"],
                separators=(",", ":"),
            ): {"phase": "owned", "identity": {"uid": "reader-sa-uid"}}
        },
    }
    for component in (
        "reader-issuer-cluster-role",
        "reader-issuer-cluster-binding",
        "reader-issuer-namespace-role",
        "reader-issuer-namespace-binding",
    ):
        progress[component] = {"phase": "granted", "identity": {"uid": "grant-uid"}}

    def resolve():
        authority.progress_json = json.dumps(progress)
        return native_identity(reservation, authority)

    return reservation, authority, progress, resolve


def test_registered_renewal_requires_positive_revocation_and_preserves_identity(native):
    reservation, authority, progress, resolve = native
    original = resolve()
    assert original["service_account_uid"] == "reader-sa-uid"
    reservation.state = "registered"
    assert resolve() is None
    authority.revoked = True
    progress.update(phase="revoked", complete=True, retain_workspace=True)
    assert resolve() == {**original, "registered": True}


@pytest.mark.parametrize("phase", ["acquiring", "revoking", "revoked"])
def test_failed_or_incomplete_bootstrap_cannot_issue(native, phase):
    _, _, progress, resolve = native
    progress["phase"] = phase
    assert resolve() is None


def test_retained_transition_remains_observable_and_other_claim_refuses(native):
    reservation, _, progress, resolve = native
    progress.update(phase="revoking", retain_workspace=True)
    assert resolve() is not None
    reservation.attempt_token = "another-claim"
    assert resolve() is None


def test_missing_or_revoked_issuer_delegation_refuses(native):
    _, _, progress, resolve = native
    old = deepcopy(progress)
    del progress["reader-issuer-namespace-role"]
    assert resolve() is None
    progress.update(old)
    progress["reader-issuer-namespace-role"]["phase"] = "revoked"
    assert resolve() is None


def test_shared_membership_cannot_enter_dedicated_path(native):
    reservation, _, _, resolve = native
    identity = json.loads(reservation.identity_json)
    identity["membership_generation"] = "shared"
    reservation.identity_json = json.dumps(identity)
    assert resolve() is None
