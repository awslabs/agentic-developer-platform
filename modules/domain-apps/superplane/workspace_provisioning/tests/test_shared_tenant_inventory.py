"""Complete SDK inventory cannot hide a peer or broaden the issuer exemption."""

from types import SimpleNamespace

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.namespace_admission import ClusterAuthorityReference
from superplane_bootstrap.shared_authority import SharedWorkspaceAuthority

from workspace_provisioning.shared_tenant_inventory import installed_tenant_principals

ISSUER = "arn:aws:iam::123456789012:role/issuer"
PEER = "arn:aws:iam::123456789012:role/peer"
NODE = "arn:aws:iam::123456789012:role/node"
POLICY = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "repeated-page",
        "external-identity",
        "missing-issuer",
        "issuer-entry",
        "issuer-policy",
        "peer-admin-policy",
        "peer-issuer-group",
    ],
)
def test_installed_issuer_exemption_preserves_complete_tenant_inventory(drift):
    reference = ClusterAuthorityReference(
        "org",
        "arn:aws:eks:us-east-1:123456789012:cluster/shared",
        ISSUER,
        "arn:aws:eks:us-east-1:123456789012:access-entry/shared/issuer/original",
        "issuer",
        "superplane:issuer",
        "policy-uid",
        "binding-uid",
    )
    calls, verified = [], []

    class Eks:
        def list_identity_provider_configs(self, **kwargs):
            calls.append(("identity-configs", kwargs))
            return {
                "identityProviderConfigs": [{}] if drift == "external-identity" else []
            }

        def list_access_entries(self, **kwargs):
            calls.append(("entries", kwargs))
            if not kwargs.get("nextToken"):
                return {
                    "accessEntries": [] if drift == "missing-issuer" else [ISSUER],
                    "nextToken": "second",
                }
            return {
                "accessEntries": [PEER, NODE],
                **({"nextToken": "second"} if drift == "repeated-page" else {}),
            }

        def describe_access_entry(self, **kwargs):
            calls.append(("entry", kwargs))
            principal = kwargs["principalArn"]
            if principal == NODE:
                return {"accessEntry": {"principalArn": NODE, "type": "EC2_LINUX"}}
            return {
                "accessEntry": {
                    "principalArn": principal,
                    "type": "STANDARD",
                    "accessEntryArn": "replacement"
                    if drift == "issuer-entry" and principal == ISSUER
                    else reference.access_entry_arn,
                    "username": reference.username
                    if principal == ISSUER
                    else "peer:{{SessionName}}",
                    "kubernetesGroups": [reference.group]
                    if principal == ISSUER or drift == "peer-issuer-group"
                    else ["peer-readers"],
                }
            }

        def list_associated_access_policies(self, **kwargs):
            calls.append(("policies", kwargs))
            if kwargs["principalArn"] == ISSUER:
                return {
                    "associatedAccessPolicies": [{"policyArn": POLICY}]
                    if drift == "issuer-policy"
                    else []
                }
            if not kwargs.get("nextToken"):
                return {"associatedAccessPolicies": [], "nextToken": "peer-policies"}
            return {
                "associatedAccessPolicies": [
                    {
                        "policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
                        if drift == "peer-admin-policy"
                        else POLICY,
                        "accessScope": {
                            "type": "namespace",
                            "namespaces": ["peer-workspace"],
                        },
                    }
                ]
            }

    authority = SharedWorkspaceAuthority(
        SimpleNamespace(target=SimpleNamespace(cluster_name="shared")),
        SimpleNamespace(
            reference=reference,
            clients=SimpleNamespace(eks=Eks()),
            verify_worker_binding=lambda: verified.append(True),
        ),
    )
    if drift not in {None, "peer-issuer-group"}:
        with pytest.raises(BootstrapRefused):
            installed_tenant_principals(authority)
    else:
        group = reference.group if drift else "peer-readers"
        assert installed_tenant_principals(authority) == [
            ("peer:{{SessionName}}", (group,))
        ]
        assert any(
            method == "entries" and args.get("nextToken") == "second"
            for method, args in calls
        )
        assert any(
            method == "policies" and args.get("nextToken") == "peer-policies"
            for method, args in calls
        )
        assert all(args["clusterName"] == "shared" for _, args in calls)
        assert not any(
            method == "policies" and args["principalArn"] == NODE
            for method, args in calls
        )
    assert verified


def test_unbound_inventory_cannot_supply_an_empty_tenant_proof():
    with pytest.raises(BootstrapRefused, match="installed bootstrap authority"):
        installed_tenant_principals(SimpleNamespace())
