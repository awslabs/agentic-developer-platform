"""Projected tokens require current exact membership authority at every reopen."""

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import ssl
from types import SimpleNamespace
from uuid import uuid4

import pytest
from harness_jobs.identity import OperationRefused
from superplane_bootstrap.membership import SharedMembership
from workspace_provisioning.member_credentials.binding import (
    CredentialBinding,
    IssuedCredential,
)

from superplane_executor.workspace import Workspace


@pytest.mark.parametrize(
    "change",
    [
        None,
        "generation",
        "namespace_uid",
        "reader",
        "expired",
        "eligibility",
        "unregistered",
        "revision",
    ],
)
def test_projected_member_credential_cannot_authorize_itself(tmp_path, change):
    member = SharedMembership.create(
        org_id=str(uuid4()),
        workspace_id=str(uuid4()),
        cluster_id=str(uuid4()),
        request_id=str(uuid4()),
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/management",
        endpoint="https://management.example",
    )
    ca = base64.b64encode(
        ssl.DER_cert_to_PEM_cert(
            ssl.create_default_context().get_ca_certs(binary_form=True)[0]
        ).encode()
    ).decode()
    binding = CredentialBinding(member, "namespace-uid", 1, "mutator")
    credential = IssuedCredential(
        binding,
        "service-account-uid",
        datetime.now(UTC) + timedelta(minutes=15),
        "fixture-token",
        ca,
    )
    target = {
        "cluster_id": member.cluster_id,
        "cluster_arn": member.cluster_arn,
        "namespace": member.namespace,
        "endpoint": member.endpoint,
        "platform_eligible": True,
        "membership_credential": credential.metadata,
    }
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                workspace_id=member.workspace_id, org_id=member.org_id
            )
        )
    )
    if change in {"generation", "namespace_uid", "revision"}:
        target["membership_credential"][change] = (
            2 if change == "revision" else "changed"
        )
    elif change == "reader":
        credential = replace(credential, binding=replace(binding, scope="reader"))
    elif change == "expired":
        credential = replace(
            credential, expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        target["membership_credential"] = credential.metadata
    elif change == "eligibility":
        target["platform_eligible"] = False
    elif change == "unregistered":
        target.pop("membership_credential")
    path = tmp_path / (member.workspace_id + ".kubeconfig")
    path.write_text(credential.kubeconfig(ca))
    client = Workspace(tmp_path, member.endpoint)
    if change:
        with pytest.raises(OperationRefused):
            client.credentials(operation, target)
    else:
        assert client.credentials(operation, target)[0] == "fixture-token"
        target["membership_credential"]["revision"] = 2
        with pytest.raises(OperationRefused):
            client.credentials(operation, target)
