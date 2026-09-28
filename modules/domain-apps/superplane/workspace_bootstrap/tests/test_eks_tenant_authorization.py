"""EKS policies are additive to RBAC; neither source may be omitted."""

import pytest

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.tenant_authorization import (
    eks_tenant_principals,
    POLICY_PREFIX,
)

ARN = "arn:aws:iam::000000000001:role/editor"


def reader(
    *, scope="namespace", policy="AmazonEKSEditPolicy", external=(), malformed=False
):
    calls = []

    def read(*args):
        calls.append(args)
        if args[1] == "list-identity-provider-configs":
            return {"identityProviderConfigs": list(external)}
        if args[1] == "list-access-entries":
            return {"accessEntries": [ARN]}
        if args[1] == "describe-access-entry":
            return {
                "accessEntry": {
                    "principalArn": ARN,
                    "type": "STANDARD",
                    "username": "editor:{{SessionName}}",
                    "kubernetesGroups": ["editors"],
                }
            }
        if args[1] == "list-associated-access-policies":
            return (
                {}
                if malformed
                else {
                    "associatedAccessPolicies": [
                        {
                            "policyArn": POLICY_PREFIX + policy,
                            "accessScope": {"type": scope, "namespaces": ["workspace"]},
                        }
                    ]
                }
            )
        raise AssertionError(args)

    return read, calls


def test_namespace_policy_principal_is_checked_even_without_rolebinding():
    read, calls = reader()
    assert eks_tenant_principals(read, "cluster", "") == [
        ("editor:{{SessionName}}", ("editors",))
    ]
    assert len(calls) == 4


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "cluster"},
        {"policy": "AmazonEKSClusterAdminPolicy"},
        {"policy": "UnknownPolicy"},
        {"external": [{"name": "external", "type": "oidc"}]},
        {"malformed": True},
    ],
)
def test_unknown_or_cluster_authority_cannot_prove_namespace_isolation(kwargs):
    read, _ = reader(**kwargs)
    with pytest.raises(BootstrapRefused):
        eks_tenant_principals(read, "cluster", "")


def test_provider_inventory_failure_cannot_become_empty_tenant_set():
    def denied(*args):
        raise BootstrapRefused("synthetic denied read")

    with pytest.raises(BootstrapRefused):
        eks_tenant_principals(denied, "cluster", "")


def test_named_bootstrap_principal_still_requires_tenant_rbac_review():
    # Naming an ARN is not evidence of an operation-bound temporary grant or
    # its revocation. Retained namespace authority must still reach the SARs.
    read, calls = reader()
    assert eks_tenant_principals(read, "cluster", ARN) == [
        ("editor:{{SessionName}}", ("editors",))
    ]
    assert any(call[1] == "list-associated-access-policies" for call in calls)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "cluster"},
        {"policy": "AmazonEKSClusterAdminPolicy"},
        {"malformed": True},
    ],
)
def test_named_bootstrap_principal_cannot_hide_residual_or_unknown_authority(kwargs):
    read, _ = reader(**kwargs)
    with pytest.raises(BootstrapRefused):
        eks_tenant_principals(read, "cluster", ARN)
