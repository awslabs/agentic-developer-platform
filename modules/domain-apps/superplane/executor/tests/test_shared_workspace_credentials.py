"""Projected tokens require current exact membership authority at every reopen."""

import base64
import json
from datetime import UTC, datetime, timedelta
import ssl
from types import SimpleNamespace
from uuid import uuid4

import pytest
from harness_jobs.identity import OperationRefused
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
    member = SimpleNamespace(
        namespace="sp-ws-fixture",
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
    metadata = {
        "org_id": member.org_id,
        "workspace_id": member.workspace_id,
        "cluster_id": member.cluster_id,
        "cluster_arn": member.cluster_arn,
        "generation": "a" * 64,
        "namespace": member.namespace,
        "namespace_uid": "namespace-uid",
        "service_account_uid": "service-account-uid",
        "revision": 1,
        "scope": "mutator",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    }
    target = {
        "cluster_id": member.cluster_id,
        "cluster_arn": member.cluster_arn,
        "namespace": member.namespace,
        "endpoint": member.endpoint,
        "platform_eligible": True,
        "membership_credential": dict(metadata),
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
        metadata["scope"] = "reader"
    elif change == "expired":
        metadata["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        target["membership_credential"] = dict(metadata)
    elif change == "eligibility":
        target["platform_eligible"] = False
    elif change == "unregistered":
        target.pop("membership_credential")
    path = tmp_path / (member.workspace_id + ".kubeconfig")
    path.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": member.cluster_arn,
                "clusters": [
                    {
                        "name": "target",
                        "cluster": {
                            "server": member.endpoint,
                            "certificate-authority-data": ca,
                        },
                    }
                ],
                "contexts": [
                    {
                        "name": member.cluster_arn,
                        "context": {
                            "cluster": "target",
                            "user": "member",
                            "namespace": member.namespace,
                        },
                    }
                ],
                "users": [{"name": "member", "user": {"token": "fixture-token"}}],
                "extensions": [
                    {"name": "superplane.aws-e/membership", "extension": metadata}
                ],
            }
        )
    )
    client = Workspace(tmp_path, member.endpoint)
    if change:
        with pytest.raises(OperationRefused):
            client.credentials(operation, target)

    else:
        assert client.credentials(operation, target)[0] == "fixture-token"
        target["membership_credential"]["revision"] = 2
        with pytest.raises(OperationRefused):
            client.credentials(operation, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_uid", [False, True])
async def test_shared_verification_uses_server_identity_without_fleet_reads(
    tmp_path, wrong_uid
):
    client = Workspace(tmp_path, "https://management.example")
    metadata = {
        "generation": "a" * 64,
        "revision": 1,
        "service_account_uid": "sa-uid",
        "namespace_uid": "ns-uid",
    }
    target = {"namespace": "member-ns", "membership_credential": metadata}
    calls = []

    async def request(operation, actual_target, method, path, **kwargs):
        calls.append((method, path))
        if path.endswith("selfsubjectreviews"):
            payload = {
                "status": {
                    "userInfo": {
                        "uid": "wrong" if wrong_uid else "sa-uid",
                        "username": "system:serviceaccount:member-ns:sp-mutator-"
                        + "a" * 24
                        + "-1",
                    }
                }
            }
            return SimpleNamespace(status_code=201, json=lambda: payload)
        return SimpleNamespace(status_code=200, json=lambda: {"items": []})

    client.request = request
    if wrong_uid:
        with pytest.raises(OperationRefused, match="identity"):
            await client.verify(None, target)
        assert len(calls) == 1
    else:
        await client.verify(None, target)
        assert calls == [
            ("POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews"),
            (
                "GET",
                "/apis/superplane.ai/v1/namespaces/member-ns/superplanenodes?limit=1",
            ),
        ]
