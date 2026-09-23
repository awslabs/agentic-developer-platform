"""EKS SDK contracts: ownership, unknown responses and immutable-ARN revocation."""

from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber
from superplane_bootstrap.eks_grants import EksGrants
from superplane_bootstrap.errors import BootstrapRefused

from .conftest import ACCOUNT_ID, CLUSTER_ARN, CLUSTER_NAME, ORG_ID, WORKSPACE_ID

ENTRY_ARN = (
    CLUSTER_ARN.replace(":cluster/", ":access-entry/")
    + "/role/000000000000/Registrar/unique"
)
PRINCIPAL = f"arn:aws:iam::{ACCOUNT_ID}:role/Registrar"
ARGS = {"clusterName": CLUSTER_NAME, "principalArn": PRINCIPAL}
SPEC = {
    "kind": "eks-entry",
    "key": "registrar",
    "cluster_arn": CLUSTER_ARN,
    "principal_arn": PRINCIPAL,
    "generation": "a" * 64,
    "groups": ["bootstrap-test"],
    "username": "bootstrap:test",
    "client_token": "unique-generation-token",
}
ENTRY = {
    **ARGS,
    "accessEntryArn": ENTRY_ARN,
    "type": "STANDARD",
    "kubernetesGroups": SPEC["groups"],
    "username": SPEC["username"],
    "tags": {
        "superplane-generation": SPEC["generation"],
        "OrgId": ORG_ID,
        "WorkspaceId": WORKSPACE_ID,
    },
}


@pytest.fixture
def clients():
    def client():
        return boto3.client(
            "eks",
            region_name="us-east-1",
            aws_access_key_id="synthetic",
            aws_secret_access_key="synthetic",
        )

    ordinary, scoped = client(), client()
    with Stubber(ordinary) as a, Stubber(scoped) as b:
        requested = []

        def narrow(arn):
            requested.append(arn)
            return scoped

        backend = EksGrants(
            ordinary,
            SimpleNamespace(
                cluster_arn=CLUSTER_ARN,
                cluster_name=CLUSTER_NAME,
                account_id=ACCOUNT_ID,
                org_id=ORG_ID,
                workspace_id=WORKSPACE_ID,
            ),
            entry_client=narrow,
        )
        yield backend, a, b, requested
        a.assert_no_pending_responses()
        b.assert_no_pending_responses()


def test_create_read_revoke_uses_real_sdk_shapes_and_scoped_delete(clients):
    backend, ordinary, scoped, requested = clients
    ordinary.add_response(
        "create_access_entry",
        {"accessEntry": ENTRY},
        {
            **ARGS,
            "type": "STANDARD",
            "kubernetesGroups": SPEC["groups"],
            "username": SPEC["username"],
            "clientRequestToken": SPEC["client_token"],
            "tags": ENTRY["tags"],
        },
    )
    identity = backend.create(SPEC)
    ordinary.add_response("describe_access_entry", {"accessEntry": ENTRY}, ARGS)
    ordinary.add_response(
        "list_associated_access_policies", {"associatedAccessPolicies": []}, ARGS
    )
    scoped.add_response("delete_access_entry", {}, ARGS)
    backend.delete(SPEC, identity)
    assert requested == [ENTRY_ARN]
    ordinary.add_client_error(
        "describe_access_entry", "ResourceNotFoundException", expected_params=ARGS
    )
    assert backend.observe(SPEC) is None


def test_replacement_entry_cannot_inherit_revocation(clients):
    backend, ordinary, _, requested = clients
    original = backend._entry_identity(SPEC, ENTRY)
    ordinary.add_response(
        "describe_access_entry",
        {"accessEntry": {**ENTRY, "accessEntryArn": ENTRY_ARN + "-replacement"}},
        ARGS,
    )
    with pytest.raises(BootstrapRefused, match="changed before revocation"):
        backend.delete(SPEC, original)
    assert requested == []


@pytest.mark.parametrize(
    "code", ["AccessDeniedException", "ServerException", "ThrottlingException"]
)
def test_provider_failure_is_not_absence(clients, code):
    backend, ordinary, _, _ = clients
    ordinary.add_client_error("describe_access_entry", code, expected_params=ARGS)
    with pytest.raises(Exception) as error:
        backend.observe(SPEC)
    assert error.value.response["Error"]["Code"] == code


def test_adopted_generation_is_not_owned(clients):
    backend, ordinary, _, _ = clients
    ordinary.add_response(
        "describe_access_entry", {"accessEntry": {**ENTRY, "tags": {}}}, ARGS
    )
    identity = backend.observe(SPEC)
    with pytest.raises(BootstrapRefused, match="not owned"):
        backend.verify(SPEC, identity)


def test_residual_policy_blocks_entry_deletion(clients):
    from datetime import datetime, UTC

    backend, ordinary, _, requested = clients
    ordinary.add_response("describe_access_entry", {"accessEntry": ENTRY}, ARGS)
    ordinary.add_response(
        "list_associated_access_policies",
        {
            "associatedAccessPolicies": [
                {
                    "policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy",
                    "accessScope": {"type": "cluster"},
                    "associatedAt": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ]
        },
        ARGS,
    )
    with pytest.raises(BootstrapRefused, match="residual policy"):
        backend.delete(SPEC, backend._entry_identity(SPEC, ENTRY))
    assert requested == [ENTRY_ARN]


def test_wrong_target_refuses_before_sdk(clients):
    backend, _, _, requested = clients
    with pytest.raises(BootstrapRefused, match="different cluster"):
        backend.observe({**SPEC, "cluster_arn": CLUSTER_ARN + "-other"})
    assert not requested
