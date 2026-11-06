"""The EKS diagnostic is accepted only with provider-authenticated native identity."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from workspace_provisioning.dedicated_readers.eks_rules import (
    EKS_RULES_ERROR,
    native_eks_reader_rules,
)
from workspace_provisioning.dedicated_readers.renewal import ReaderRefused, readonly
from workspace_provisioning.dedicated_readers.transports import Issuer


@pytest.fixture
def proof():
    target = {
        "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
        "endpoint": "https://ABC.gr7.us-east-1.eks.amazonaws.com",
        "namespace": "superplane-system",
        "service_account": "superplane-controller",
        "service_account_uid": "22222222-2222-4222-8222-222222222222",
    }
    user = SimpleNamespace(
        username="system:serviceaccount:superplane-system:superplane-controller",
        uid=target["service_account_uid"],
        groups=[
            "system:authenticated",
            "system:serviceaccounts",
            "system:serviceaccounts:superplane-system",
        ],
    )
    status = {
        "incomplete": True,
        "evaluationError": EKS_RULES_ERROR,
        "resourceRules": [
            {"apiGroups": [""], "resources": ["nodes"], "verbs": ["list"]}
        ],
    }
    return target, user, status


def test_real_eks_diagnostic_does_not_mutate_strict_checker_or_response(proof):
    target, user, status = proof
    original = deepcopy(status)
    assert not readonly(status)
    assert native_eks_reader_rules(status, target, user)
    assert status == original
    assert not readonly(status)


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "https://workspace.example"),
        ("endpoint", "http://ABC.gr7.us-east-1.eks.amazonaws.com"),
        ("endpoint", "https://ABC.gr7.us-east-1.eks.amazonaws.com.attacker.example"),
        ("endpoint", "https://user@ABC.gr7.us-east-1.eks.amazonaws.com"),
        ("endpoint", "https://ABC.gr7.us-west-2.eks.amazonaws.com"),
        ("cluster_arn", "arn:aws:ec2:us-east-1:123456789012:cluster/workspace"),
        ("service_account_uid", "33333333-3333-4333-8333-333333333333"),
        ("service_account", "default"),
    ],
)
def test_other_provider_or_native_identity_is_refused(proof, field, value):
    target, user, status = proof
    target[field] = value
    assert not native_eks_reader_rules(status, target, user)


@pytest.mark.parametrize(
    "field,value",
    [
        ("username", "arn:aws:iam::123456789012:role/operator"),
        ("uid", "aws-iam-authenticator:123456789012:role"),
        ("groups", ["system:masters"]),
        (
            "groups",
            [
                "system:authenticated",
                "system:serviceaccounts",
                "system:serviceaccounts:other",
            ],
        ),
    ],
)
def test_server_authenticated_user_must_match(proof, field, value):
    target, user, status = proof
    setattr(user, field, value)
    assert not native_eks_reader_rules(status, target, user)


@pytest.mark.parametrize(
    "error", ["", "other authorizer failed", EKS_RULES_ERROR + "; other error"]
)
def test_unknown_or_multiple_resolution_failures_remain_closed(proof, error):
    target, user, status = proof
    status["evaluationError"] = error
    assert not native_eks_reader_rules(status, target, user)


@pytest.mark.parametrize(
    "resource,verb",
    [
        ("secrets", "get"),
        ("pods", "create"),
        ("nodes/proxy", "get"),
        ("serviceaccounts/token", "create"),
        ("*", "list"),
    ],
)
def test_known_diagnostic_never_hides_unsafe_rbac(proof, resource, verb):
    target, user, status = proof
    status["resourceRules"].append(
        {"apiGroups": [""], "resources": [resource], "verbs": [verb]}
    )
    assert not native_eks_reader_rules(status, target, user)


def test_transport_authenticates_and_checks_every_namespace(proof, monkeypatch):
    from kubernetes import client

    target, user, status = proof
    authenticated, queried = [], []

    class Authentication:
        def create_self_subject_review(self, *args, **kwargs):
            authenticated.append(True)
            return SimpleNamespace(status=SimpleNamespace(user_info=user))

    class Authorization:
        def create_self_subject_rules_review(self, body, **kwargs):
            namespace = body["spec"]["namespace"]
            queried.append(namespace)
            result = deepcopy(status)
            if namespace == "other-tenant":
                result["resourceRules"].append(
                    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}
                )
            return {"status": result}

    monkeypatch.setattr(client, "AuthenticationV1Api", lambda api: Authentication())
    api = SimpleNamespace(sanitize_for_serialization=lambda value: value)
    with pytest.raises(ReaderRefused, match="mutation"):
        Issuer.verify_permissions(
            Authorization(),
            api,
            [("default", "a"), ("other-tenant", "b")],
            target=target,
        )
    assert authenticated == [True]
    assert queried == ["default", "other-tenant"]
