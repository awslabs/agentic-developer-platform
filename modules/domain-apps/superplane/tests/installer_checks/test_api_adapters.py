"""Existing adapter references and verified activation are distinct installer states."""

import copy
from datetime import UTC, datetime
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
            "url": "http://gateway.gateway.svc.cluster.local:80",
            "secret_key_ref": {"name": "existing-vault-evidence", "key": "key"},
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
        (("vault", "secret_key_ref", "value"), "inline-not-permitted"),
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
    assert values["ADP_GATEWAY_INTERNAL_API_KEY"] == {
        "name": "ADP_GATEWAY_INTERNAL_API_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "existing-vault-evidence",
                "key": "key",
                "optional": False,
            }
        },
    }
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
    rule = policy["spec"]["egress"][-1]
    assert rule["to"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "gateway"}
            },
            "podSelector": {"matchLabels": {"app": "gateway"}},
        }
    ]
    assert {p["port"] for p in rule["ports"]} == {80, 8080}


def test_image_contract_cannot_be_live_capabilities():
    assert not image_contract_valid({"capabilities": {"credential_evidence": True}})
    assert not image_contract_valid(
        {"image_contract_version": 1, "production_ready": True}
    )


@pytest.mark.parametrize("state", [None, "activation-pending", "disabled-restored"])
def test_activation_without_verified_stage_has_no_mutation(state):
    installer = SimpleNamespace(
        receipt={"adapter_stage": {"state": state}}, apply=Mock(), save=Mock()
    )
    with pytest.raises(Refusal):
        adapter_staging.activate(installer)
    installer.apply.assert_not_called()
    installer.save.assert_not_called()


def test_secret_or_role_drift_refuses_before_activation_intent(monkeypatch):
    installer = SimpleNamespace(
        receipt={
            "adapter_stage": {
                "state": "verified-disabled",
                "verified_at": datetime.now(UTC).isoformat(),
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
                    prefix + r for r in ("producer-readiness", "verify-run", "dispatch")
                ],
            }
        ]
    }
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
