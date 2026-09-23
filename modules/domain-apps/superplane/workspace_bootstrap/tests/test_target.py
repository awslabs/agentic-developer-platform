"""Target identity verification — Issue #5533 (w6-10), AC-01 negative cases.

Every test here is a refusal case except the two controls. That ratio is the point:
the gate's value is entirely in what it rejects, and a suite that only proved the
happy path would pass just as well against a function that returned its input.

The stale/wrong-CA, mismatched-account and inactive-cluster cases are named
explicitly in AC-01.
"""

from __future__ import annotations

import dataclasses

import pytest
from superplane_bootstrap.access import ClusterIdentity, ProviderIdentity
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.target import verify_target

from .conftest import (
    ACCOUNT_ID,
    CA_DATA,
    CLUSTER_ARN,
    CLUSTER_NAME,
    ORG_ID,
    PRINCIPAL_ARN,
    REGION,
    STALE_CA,
    WORKSPACE_ID,
)


def _verify(binding, provider, observed, expected, **overrides):
    arguments = {
        "binding": binding,
        "provider": provider,
        "observed": observed,
        "cluster_ownership": "adp-created",
        **expected,
        **overrides,
    }
    return verify_target(**arguments)


# --- Controls --------------------------------------------------------------------


def test_a_matching_cluster_verifies_and_carries_the_bound_identity(
    binding, provider_identity, observed_cluster, expected_target
):
    """The control. Identity comes from the binding, not from any parameter."""
    target = _verify(binding, provider_identity, observed_cluster, expected_target)

    assert target.org_id == ORG_ID
    assert target.workspace_id == WORKSPACE_ID
    assert target.cluster_arn == CLUSTER_ARN
    assert target.cluster_name == CLUSTER_NAME
    assert target.account_id == ACCOUNT_ID
    assert target.region == REGION
    assert target.principal_arn == PRINCIPAL_ARN
    assert target.certificate_authority_data == CA_DATA
    assert target.is_adopted is False


def test_a_supplied_cluster_verifies_and_records_adopted_ownership(
    binding, provider_identity, observed_cluster, expected_target
):
    """BYOC mode passes the same identity gate; only the recorded ownership differs.

    AC-02 depends on this being recorded rather than inferred — `retire.py`
    branches on `is_adopted`, and a bootstrap that guessed would guess wrong for a
    supplied cluster inside an ADP-managed account.
    """
    target = _verify(
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        cluster_ownership="adopted",
    )

    assert target.is_adopted is True
    assert target.cluster_ownership == "adopted"


# --- Certificate identity (AC-01: stale/wrong CA) --------------------------------


def test_a_stale_certificate_authority_is_refused(
    binding, provider_identity, observed_cluster, expected_target
):
    """The cluster was replaced since the plan was reviewed. Refused, not reconciled."""
    observed = dataclasses.replace(
        observed_cluster, certificate_authority_data=STALE_CA
    )

    with pytest.raises(BootstrapRefused, match="certificate authority does not match"):
        _verify(binding, provider_identity, observed, expected_target)


def test_a_substituted_certificate_authority_is_refused(
    binding, provider_identity, observed_cluster, expected_target
):
    """Right account, right region, right name and ARN — different server.

    This is the case no other gate catches: every field a human would eyeball
    agrees, and only the certificate disagrees.
    """
    expected = {
        **expected_target,
        "expected_certificate_authority_data": "c3Vic3RpdHV0ZWQtc2VydmVyLWNlcnQ=",
    }

    with pytest.raises(BootstrapRefused, match="certificate authority does not match"):
        _verify(binding, provider_identity, observed_cluster, expected)


def test_an_absent_expected_certificate_authority_does_not_default_to_trust(
    binding, provider_identity, observed_cluster, expected_target
):
    """An empty expectation verifies nothing, so it must refuse rather than pass.

    Worth its own test because the natural implementation — compare the two
    strings — passes when BOTH are empty, which is precisely the case where no
    expectation was supplied at all.
    """
    expected = {**expected_target, "expected_certificate_authority_data": ""}

    with pytest.raises(BootstrapRefused, match="expected cluster certificate"):
        _verify(binding, provider_identity, observed_cluster, expected)


# --- Account and region (AC-01: mismatched account) ------------------------------


def test_a_caller_in_a_different_account_is_refused(
    binding, observed_cluster, expected_target
):
    """The credential resolves elsewhere, so every later observation is about the
    wrong world. Refused before any mutation."""
    provider = ProviderIdentity(
        account_id="999999999999", principal_arn="arn:aws:iam::999999999999:role/Other"
    )

    with pytest.raises(BootstrapRefused, match="different AWS account"):
        _verify(binding, provider, observed_cluster, expected_target)


def test_a_cluster_in_a_different_account_is_refused(
    binding, provider_identity, observed_cluster, expected_target
):
    """The caller is in the right account but the cluster is not."""
    observed = dataclasses.replace(observed_cluster, account_id="999999999999")

    with pytest.raises(BootstrapRefused, match="not the selected target account"):
        _verify(binding, provider_identity, observed, expected_target)


def test_a_cluster_in_a_different_region_is_refused(
    binding, provider_identity, observed_cluster, expected_target
):
    observed = dataclasses.replace(observed_cluster, region="eu-west-1")

    with pytest.raises(BootstrapRefused, match="not the selected region"):
        _verify(binding, provider_identity, observed, expected_target)


def test_a_differently_named_cluster_is_refused(
    binding, provider_identity, observed_cluster, expected_target
):
    observed = dataclasses.replace(observed_cluster, name="adp-test-spw-something-else")

    with pytest.raises(BootstrapRefused, match="not the expected"):
        _verify(binding, provider_identity, observed, expected_target)


def test_a_mismatched_cluster_arn_is_refused(
    binding, provider_identity, observed_cluster, expected_target
):
    """An ARN mismatch names a different cluster, not a changed attribute."""
    expected = {
        **expected_target,
        "expected_cluster_arn": CLUSTER_ARN.replace("cluster/", "cluster/other-"),
    }

    with pytest.raises(BootstrapRefused, match="ARN does not match"):
        _verify(binding, provider_identity, observed_cluster, expected)


# --- Cluster readiness -----------------------------------------------------------


@pytest.mark.parametrize("status", ["CREATING", "UPDATING", "FAILED", "DELETING"])
def test_a_cluster_that_is_not_active_is_refused(
    binding, provider_identity, observed_cluster, expected_target, status
):
    """Including CREATING: the safe answer to "not yet" is to refuse and be re-run,
    not to race the control plane's readiness."""
    observed = dataclasses.replace(observed_cluster, status=status)

    with pytest.raises(BootstrapRefused, match="cluster status is"):
        _verify(binding, provider_identity, observed, expected_target)


# --- Identity provenance ---------------------------------------------------------


def test_a_binding_without_a_resolved_workspace_is_refused(
    provider_identity, observed_cluster, expected_target
):
    """A binding with no workspace has nothing tying the operation to a tenant."""

    class Unbound:
        principal = None

    with pytest.raises(BootstrapRefused, match="facade-issued OperationBinding"):
        _verify(Unbound(), provider_identity, observed_cluster, expected_target)


def test_a_binding_with_a_blank_workspace_is_refused(
    provider_identity, observed_cluster, expected_target
):
    """Blank is refused rather than treated as "any workspace"."""

    @dataclasses.dataclass
    class LooseP:
        org_id: str = ORG_ID
        workspace_id: str = "   "

    @dataclasses.dataclass
    class LooseBinding:
        principal: LooseP = dataclasses.field(default_factory=LooseP)

    with pytest.raises(BootstrapRefused, match="facade-issued OperationBinding"):
        _verify(LooseBinding(), provider_identity, observed_cluster, expected_target)


def test_an_unknown_cluster_ownership_is_refused(
    binding, provider_identity, observed_cluster, expected_target
):
    """Ownership decides whether cleanup may delete the cluster, so an
    unrecognized value must not fall through to a default."""
    with pytest.raises(BootstrapRefused, match="unknown cluster ownership"):
        _verify(
            binding,
            provider_identity,
            observed_cluster,
            expected_target,
            cluster_ownership="probably-ours",
        )


# --- Seam construction -----------------------------------------------------------


def test_an_observation_missing_the_cluster_status_cannot_be_constructed():
    """The seam refuses an incomplete observation, so a fake or a future real
    adapter that omits a field fails where it is legible rather than as a false
    pass three gates later."""
    with pytest.raises(BootstrapRefused, match="ClusterIdentity.status"):
        ClusterIdentity(
            name=CLUSTER_NAME,
            arn=CLUSTER_ARN,
            region=REGION,
            account_id=ACCOUNT_ID,
            endpoint="https://example.invalid",
            certificate_authority_data=CA_DATA,
            status="",
        )


def test_a_provider_identity_without_an_account_cannot_be_constructed():
    with pytest.raises(BootstrapRefused, match="ProviderIdentity.account_id"):
        ProviderIdentity(account_id="", principal_arn=PRINCIPAL_ARN)
