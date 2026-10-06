"""Existing adapter references and verified activation are distinct installer states."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from installation import adapter_staging
from installation.api_adapters import (
    image_contract_valid,
    project,
    validate,
    verify_role,
)
from installation.config import Refusal
from installation.manifests import render


@pytest.fixture
def adapters(environment):
    environment["api_adapters"] = {
        "vault": {
            "url": "https://abcdefghij.execute-api.us-east-1.amazonaws.com/dev",
            "auth": "api-producer-iam",
            "transport": {
                "namespace": "gateway",
                "service": "gateway",
                "port": 80,
                "target_port": 8080,
                "selector": {"app": "gateway"},
                "security": "reviewed-cluster-http",
            },
        },
        "dispatcher": {
            "endpoint": "https://abcdefghij.execute-api.us-east-1.amazonaws.com/dev",
            "api_id": "abcdefghij",
            "region": "us-east-1",
            "stage": "dev",
            "role_arn": f"arn:aws:iam::{environment['account_id']}:role/api-producer",
        },
        "verification": {
            "workspace_id": environment["workspace_id"],
            "connection_id": "50000000-0000-0000-0000-000000000005",
            "credential_id": "existing-credential",
            "service": "aws",
            "label": "existing",
        },
    }
    return environment


def test_omission_does_not_change_any_manifest(environment, release):
    docs = render(environment, release, control_plane_only=True)
    original = copy.deepcopy(docs)
    project(environment, docs)
    assert docs == original


@pytest.mark.parametrize(
    "path,value",
    [
        (("vault", "url"), "http://external.example:80"),
        (("vault", "secret_key_ref"), {"name": "forbidden", "key": "key"}),
        (("vault", "auth"), "shared-key"),
        (("vault", "transport", "port"), True),
        (("vault", "transport", "selector"), {}),
        (("vault", "transport", "security"), "http"),
        (
            ("dispatcher", "endpoint"),
            "https://abcdefghij.execute-api.us-east-1.amazonaws.com/dev?key=bad",
        ),
        (("dispatcher", "region"), "us-west-2"),
        (("dispatcher", "role_arn"), "arn:aws:iam::111111111111:role/api-producer"),
        (("verification", "workspace_id"), "other"),
    ],
)
def test_adapter_schema_refuses_drift_and_inline_authority(adapters, path, value):
    target = adapters["api_adapters"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(Refusal):
        validate(adapters)


def test_only_api_gets_exact_secret_role_and_gateway_egress(adapters, release):
    legacy = copy.deepcopy(adapters)
    legacy.pop("api_adapters")
    before = render(legacy, release)
    docs = render(adapters, release)

    def non_api(values):
        return [d for d in values if d["metadata"]["name"] != "superplane-api"]

    assert non_api(docs) == non_api(before)
    api = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
    )
    values = {
        v["name"]: v for v in api["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "ADP_GATEWAY_INTERNAL_API_KEY" not in values
    assert values["ADP_GATEWAY_EVIDENCE_AUTH"]["value"] == "api-producer-iam"
    assert (
        values["ADP_GATEWAY_INTERNAL_URL"]["value"]
        == adapters["api_adapters"]["dispatcher"]["endpoint"]
    )
    assert values["SUPERPLANE_OPERATION_DISPATCH_ENABLED"]["value"] == "false"
    assert values["SUPERPLANE_MANAGEMENT_ONLY"]["value"] == "true"
    role = next(
        d
        for d in docs
        if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "superplane-api"
    )
    assert (
        role["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
        == adapters["api_adapters"]["dispatcher"]["role_arn"]
    )
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "superplane-api"
    )
    old_policy = next(
        d
        for d in before
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "superplane-api"
    )
    assert policy == old_policy


def test_image_contract_cannot_be_live_capabilities():
    assert not image_contract_valid({"capabilities": {"credential_evidence": True}})
    assert not image_contract_valid(
        {"image_contract_version": 1, "production_ready": True}
    )


@pytest.mark.parametrize("state", [None, "activation-pending", "disabled-restored"])
def test_activation_without_verified_stage_has_no_mutation(state):
    installer = SimpleNamespace(
        env={}, receipt={"adapter_stage": {"state": state}}, apply=Mock(), save=Mock()
    )
    with pytest.raises(Refusal):
        adapter_staging.activate(installer)
    installer.apply.assert_not_called()
    installer.save.assert_not_called()


def test_secret_or_role_drift_refuses_before_activation_intent(monkeypatch):
    installer = SimpleNamespace(
        env={},
        receipt={
            "adapter_stage": {
                "state": "verified-disabled",
                "verified_at": datetime.now(UTC).isoformat(),
                "credential_metadata": {
                    "evidence_expires_at": (
                        datetime.now(UTC) + timedelta(minutes=5)
                    ).isoformat()
                },
                "binding": {"secret": "old"},
            }
        },
        apply=Mock(),
        save=Mock(),
    )
    monkeypatch.setattr(adapter_staging, "snapshot", lambda _: {"secret": "rotated"})
    with pytest.raises(Refusal):
        adapter_staging.activate(installer)
    installer.apply.assert_not_called()
    installer.save.assert_not_called()


def test_dedicated_role_does_not_accept_worker_or_wildcard_authority(adapters):
    issuer = "oidc.eks.us-east-1.amazonaws.com/id/selected"
    role = {
        "Arn": adapters["api_adapters"]["dispatcher"]["role_arn"],
        "AssumeRolePolicyDocument": {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Principal": {
                        "Federated": f"arn:aws:iam::{adapters['account_id']}:oidc-provider/{issuer}"
                    },
                    "Condition": {
                        "StringEquals": {
                            issuer + ":aud": "sts.amazonaws.com",
                            issuer
                            + ":sub": "system:serviceaccount:superplane:superplane-api",
                        }
                    },
                }
            ]
        },
    }
    prefix = f"arn:aws:execute-api:us-east-1:{adapters['account_id']}:abcdefghij/dev/POST/internal/v1/controller-execution/"
    policy = {
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "execute-api:Invoke",
                "Resource": [
                    prefix + r
                    for r in (
                        "producer-readiness",
                        "verify-run",
                        "dispatch",
                        "binding-proof",
                        "current-identity",
                        "current-identity/readiness",
                    )
                ],
            }
        ]
    }
    policy["Statement"][0]["Resource"].append(
        prefix.removesuffix("controller-execution/") + "credential-evidence"
    )
    verify_role(adapters, role, [policy], "https://" + issuer)
    policy["Statement"][0]["Resource"].append("*")
    with pytest.raises(Refusal):
        verify_role(adapters, role, [policy], "https://" + issuer)


def test_activation_records_intent_before_mutation(monkeypatch):
    binding = {"secret": "same-generation"}
    stage = {
        "deployment_uid": "selected-api",
        "state": "verified-disabled",
        "verified_at": datetime.now(UTC).isoformat(),
        "credential_metadata": {
            "evidence_expires_at": (
                datetime.now(UTC) + timedelta(minutes=5)
            ).isoformat()
        },
        "binding": binding,
    }
    events = []
    installer = SimpleNamespace(
        receipt={"adapter_stage": stage},
        env={},
        docs=[{"kind": "Deployment", "metadata": {"name": "superplane-api"}}],
    )
    installer.save = lambda: events.append(("save", stage["state"]))
    installer.apply = lambda docs: events.append(("apply", stage["state"]))
    monkeypatch.setattr(adapter_staging, "snapshot", lambda _: binding)
    monkeypatch.setattr(
        adapter_staging,
        "project",
        lambda *a, **kw: events.append(("project", kw["active"])),
    )
    monkeypatch.setattr(
        adapter_staging, "wait_api", lambda _: {"metadata": {"uid": "selected-api"}}
    )
    adapter_staging.activate(installer)
    assert events == [
        ("save", "activation-pending"),
        ("project", True),
        ("apply", "activation-pending"),
        ("save", "activated-awaiting-full-verification"),
    ]


def test_credential_expiring_before_activation_never_enables_dispatch(monkeypatch):
    stage = {
        "state": "verified-disabled",
        "verified_at": datetime.now(UTC).isoformat(),
        "credential_metadata": {
            "evidence_expires_at": (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat()
        },
        "binding": {},
    }
    installer = SimpleNamespace(
        env={}, receipt={"adapter_stage": stage}, save=Mock(), apply=Mock()
    )
    monkeypatch.setattr(adapter_staging, "snapshot", lambda _: {})
    with pytest.raises(Refusal):
        adapter_staging.activate(installer)
    installer.save.assert_not_called()
    installer.apply.assert_not_called()


@pytest.mark.parametrize("fallback", [False, True])
def test_secret_transport_negotiates_metadata_and_never_uses_kubectl(
    environment, monkeypatch, fallback
):
    import base64
    import httpx
    import ssl

    certificate = ssl.create_default_context().get_ca_certs(binary_form=True)[0]
    cluster = {
        "arn": f"arn:aws:eks:{environment['region']}:{environment['account_id']}:cluster/{environment['cluster']}",
        "endpoint": "https://eks.example.test",
        "certificateAuthority": {
            "data": base64.b64encode(
                ssl.DER_cert_to_PEM_cert(certificate).encode()
            ).decode()
        },
    }
    received = []

    def respond(request):
        received.append(request)
        assert request.headers["accept"] == adapter_staging.PARTIAL_METADATA
        assert request.headers["authorization"] == "Bearer test-operator-transport"
        if fallback:
            return httpx.Response(
                200, json={"apiVersion": "v1", "kind": "Secret", "data": {}}
            )
        return httpx.Response(
            200,
            json={
                "apiVersion": "meta.k8s.io/v1",
                "kind": "PartialObjectMetadata",
                "metadata": {
                    "name": "existing-secret",
                    "namespace": environment["namespace"],
                    "uid": "secret-uid",
                    "resourceVersion": "42",
                    "annotations": {"legacy": "private-metadata-not-retained"},
                },
            },
        )

    original = httpx.Client

    def client(**kwargs):
        assert isinstance(kwargs["verify"], ssl.SSLContext)
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(adapter_staging.httpx, "Client", client)
    installer = SimpleNamespace(
        env=environment,
        aws=Mock(return_value={"status": {"token": "test-operator-transport"}}),
        json=lambda value: value,
        kube=Mock(),
    )
    if fallback:
        with pytest.raises(Refusal, match="PartialObjectMetadata"):
            adapter_staging.secret_metadata(installer, cluster, "existing-secret")
    else:
        assert adapter_staging.secret_metadata(
            installer, cluster, "existing-secret"
        ) == {"uid": "secret-uid", "resourceVersion": "42"}
    assert len(received) == 1
    installer.kube.assert_not_called()


def test_snapshot_refuses_different_cluster_before_role_lookup(adapters, monkeypatch):
    transport = adapters["api_adapters"]["vault"]["transport"]
    installer = SimpleNamespace(
        env=adapters, release="reviewed", kube=Mock(), aws=Mock()
    )
    installer.json = Mock(
        side_effect=[
            {
                "metadata": {"uid": "service", "resourceVersion": "1"},
                "spec": {
                    "selector": transport["selector"],
                    "ports": [{"port": 80, "targetPort": 8080}],
                },
            },
            {"cluster": {"arn": "arn:aws:eks:us-east-1:000000000000:cluster/other"}},
        ]
    )
    role_lookup = Mock()
    monkeypatch.setattr(adapter_staging, "role_identity", role_lookup)
    with pytest.raises(Refusal, match="cluster identity"):
        adapter_staging.snapshot(installer)
    role_lookup.assert_not_called()


@pytest.mark.parametrize(
    "count,legacy,accepted",
    [
        (3, True, True),
        (6, True, True),
        (7, False, True),
        (3, False, False),
        (6, False, False),
        (7, True, False),
        (4, True, False),
        (5, True, False),
    ],
)
def test_only_exact_original_route_sets_can_be_upgraded(
    adapters, count, legacy, accepted
):
    from installation.producer_role import documents

    issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
    adapters["api_producer_role"] = {"api_id": "abcdefghij", "stage": "dev"}
    trust, policy = documents(adapters, issuer)
    role = {
        "Arn": adapters["api_adapters"]["dispatcher"]["role_arn"],
        "AssumeRolePolicyDocument": trust,
    }
    policy["Statement"][0]["Resource"] = policy["Statement"][0]["Resource"][:count]
    if accepted:
        verify_role(adapters, role, [policy], issuer, legacy_routes=legacy)
    else:
        with pytest.raises(Refusal):
            verify_role(adapters, role, [policy], issuer, legacy_routes=legacy)
