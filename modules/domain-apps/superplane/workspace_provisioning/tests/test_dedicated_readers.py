"""Renewal survives process restart and refuses stale/foreign native identities."""

import base64
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from workspace_provisioning.dedicated_readers.renewal import (
    ReaderRefused,
    Renewal,
    readonly,
)


@pytest.fixture
def runtime():
    state = SimpleNamespace(
        now=datetime(2026, 1, 1, tzinfo=UTC),
        active=True,
        issued=0,
        writes=0,
        lost_response=False,
        conflict=False,
    )
    target = {
        "workspace_id": "22222222-2222-4222-8222-222222222222",
        "org_id": "11111111-1111-4111-8111-111111111111",
        "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
        "endpoint": "https://workspace.example",
        "namespace": "superplane-system",
        "namespace_uid": "namespace-uid",
        "service_account": "superplane-controller",
        "service_account_uid": "sa-uid",
        "generation": "a" * 64,
        "operation_id": "original-bootstrap",
        "registration_claim": "b" * 64,
    }
    selected = {
        **target,
        "certificate_authority_data": base64.b64encode(b"test-ca").decode(),
    }
    config = {
        "installation_id": "test-installation",
        "projection": {
            "secret_uid": "secret-uid",
            "secret_name": "superplane-workspace-access",
            "namespace": "superplane",
        },
    }
    state.secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "uid": "secret-uid",
            "name": "superplane-workspace-access",
            "namespace": "superplane",
            "resourceVersion": "1",
            "labels": {"adp.aws-e.io/installation": "test-installation"},
            "annotations": {"peer": "preserve"},
        },
        "data": {"other.kubeconfig": "cGVlcg=="},
    }

    class Registry:
        def read(self, workspace):
            return {
                "version": 1,
                "workspace_id": workspace,
                "active": state.active,
                "target": deepcopy(target),
                "expires_at": (state.now + timedelta(seconds=30)).isoformat(),
            }

    class Issuer:
        def issue(self, selected, current, authorize):
            authorize()
            state.issued += 1
            if getattr(state, "replace_sa", False):
                target["service_account_uid"] = "new-sa"
            return "private-test-token-" + str(state.issued), (
                state.now + timedelta(minutes=15)
            ).isoformat()

        def verify_reader(self, *args):
            if getattr(state, "overprivileged", False):
                raise ReaderRefused("Reader has mutation authority")

    class Projector:
        def read(self):
            return deepcopy(state.secret)

        def replace(self, document):
            state.writes += 1
            if state.conflict:
                state.secret["metadata"]["resourceVersion"] = "different"
            if (
                document["metadata"]["resourceVersion"]
                != state.secret["metadata"]["resourceVersion"]
            ):
                raise RuntimeError("conflict")
            state.secret = deepcopy(document)
            state.secret["metadata"]["resourceVersion"] = str(
                int(state.secret["metadata"]["resourceVersion"]) + 1
            )
            if state.lost_response:
                raise RuntimeError("lost response")

    state.renewal = lambda: Renewal(
        config, Registry(), Issuer(), Projector(), now=lambda: state.now
    )
    state.selected, state.target = selected, target
    return state


def test_rotation_restarts_from_owned_receipt_and_preserves_other_workspaces(runtime):
    assert runtime.renewal().reconcile(runtime.selected) == "renewed"
    first = deepcopy(runtime.secret)
    assert runtime.renewal().reconcile(runtime.selected) == "current"
    assert runtime.issued == 1
    runtime.now += timedelta(minutes=11)
    assert runtime.renewal().reconcile(runtime.selected) == "renewed"
    key = runtime.selected["workspace_id"] + ".kubeconfig"
    assert runtime.secret["data"][key] != first["data"][key]
    assert runtime.secret["data"]["other.kubeconfig"] == "cGVlcg=="
    assert runtime.secret["metadata"]["annotations"]["peer"] == "preserve"
    receipt = next(
        v
        for k, v in runtime.secret["metadata"]["annotations"].items()
        if k.startswith("superplane.aws-e/reader-")
    )
    assert "private-test-token" not in receipt
    kubeconfig = json.loads(base64.b64decode(runtime.secret["data"][key]))
    assert kubeconfig["current-context"] == runtime.target["cluster_arn"]


def test_lost_response_is_observed_without_repeating_mutation(runtime):
    runtime.lost_response = True
    assert runtime.renewal().reconcile(runtime.selected) == "renewed"
    assert runtime.writes == runtime.issued == 1


def test_concurrent_secret_change_is_not_overwritten(runtime):
    runtime.conflict = True
    with pytest.raises(ReaderRefused, match="no replay"):
        runtime.renewal().reconcile(runtime.selected)
    assert runtime.writes == 1
    assert set(runtime.secret["data"]) == {"other.kubeconfig"}


@pytest.mark.parametrize(
    "field",
    [
        "namespace_uid",
        "service_account_uid",
        "generation",
        "registration_claim",
        "cluster_arn",
        "org_id",
    ],
)
def test_identity_change_during_issuance_never_publishes(runtime, field):
    renewal = runtime.renewal()
    original = renewal.issuer.issue

    def replace(*args):
        result = original(*args)
        runtime.target[field] = "replaced"
        return result

    renewal.issuer.issue = replace
    with pytest.raises(ReaderRefused):
        renewal.reconcile(runtime.selected)
    assert runtime.writes == 0


def test_removed_workspace_loses_only_its_owned_projection(runtime):
    runtime.renewal().reconcile(runtime.selected)
    runtime.active = False
    assert runtime.renewal().reconcile(runtime.selected) == "inactive"
    assert runtime.secret["data"] == {"other.kubeconfig": "cGVlcg=="}
    assert runtime.secret["metadata"]["annotations"] == {"peer": "preserve"}


def test_foreign_key_or_replaced_secret_is_never_adopted(runtime):
    key = runtime.selected["workspace_id"] + ".kubeconfig"
    runtime.secret["data"][key] = "Zm9yZWlnbg=="
    with pytest.raises(ReaderRefused, match="receipt"):
        runtime.renewal().reconcile(runtime.selected)
    del runtime.secret["data"][key]
    runtime.secret["metadata"]["uid"] = "replacement"
    with pytest.raises(ReaderRefused, match="replaced"):
        runtime.renewal().reconcile(runtime.selected)
    assert runtime.issued == runtime.writes == 0


@pytest.mark.parametrize(
    "resource,verb",
    [
        ("secrets", "get"),
        ("*", "get"),
        ("pods", "create"),
        ("serviceaccounts/token", "create"),
        ("pods/exec", "create"),
        ("pods/exec", "get"),
        ("pods/attach", "get"),
        ("nodes/proxy", "get"),
        ("services/proxy", "get"),
        ("pods/*", "get"),
    ],
)
def test_privileged_reader_is_refused(resource, verb):
    assert not readonly(
        {
            "resourceRules": [
                {"apiGroups": [""], "resources": [resource], "verbs": [verb]}
            ]
        }
    )


def test_reader_proof_failure_never_publishes(runtime):
    runtime.overprivileged = True
    with pytest.raises(ReaderRefused):
        runtime.renewal().reconcile(runtime.selected)
    assert runtime.writes == 0


def test_effective_rules_include_a_foreign_namespace_binding():
    from workspace_provisioning.dedicated_readers.transports import Issuer

    queried = []

    class Auth:
        def create_self_subject_rules_review(self, body, **kwargs):
            namespace = body["spec"]["namespace"]
            queried.append(namespace)
            return {
                "status": {
                    "resourceRules": [
                        {
                            "apiGroups": [""],
                            "resources": ["pods"],
                            "verbs": [
                                "create" if namespace == "other-tenant" else "get"
                            ],
                        }
                    ]
                }
            }

    api = SimpleNamespace(sanitize_for_serialization=lambda value: value)
    with pytest.raises(ReaderRefused, match="mutation"):
        Issuer.verify_permissions(
            Auth(), api, [("superplane-system", "a"), ("other-tenant", "b")]
        )
    assert queried == ["superplane-system", "other-tenant"]


@pytest.mark.parametrize(
    "mismatch", [None, "old-prefix", "other-workspace", "extra-group"]
)
def test_issuer_requires_the_exact_eks_compatible_group(mismatch):
    from workspace_provisioning.dedicated_readers.transports import Issuer

    workspace = "22222222-2222-4222-8222-222222222222"
    role = "arn:aws:iam::123456789012:role/reader"
    group = "superplane:reader:" + workspace
    selected = {
        "workspace_id": workspace,
        "cluster_name": "workspace",
        "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
        "endpoint": "https://workspace.example",
        "certificate_authority_data": "test-ca",
        "access_entry_arn": "exact-entry",
    }
    entry = {
        "accessEntryArn": "exact-entry",
        "principalArn": role,
        "type": "STANDARD",
        "username": group + ":{{SessionName}}",
        "kubernetesGroups": [group],
    }
    if mismatch == "old-prefix":
        entry["kubernetesGroups"] = ["superplane:workspace-reader:" + workspace]
    elif mismatch == "other-workspace":
        entry["kubernetesGroups"] = [
            "superplane:reader:33333333-3333-4333-8333-333333333333"
        ]
    elif mismatch == "extra-group":
        entry["kubernetesGroups"].append("system:masters")
    eks = SimpleNamespace(
        describe_cluster=lambda **kwargs: {
            "cluster": {
                "status": "ACTIVE",
                "arn": selected["cluster_arn"],
                "endpoint": selected["endpoint"],
                "certificateAuthority": {
                    "data": selected["certificate_authority_data"]
                },
            }
        },
        describe_access_entry=lambda **kwargs: {"accessEntry": entry},
        list_associated_access_policies=lambda **kwargs: {
            "associatedAccessPolicies": []
        },
    )
    sts = SimpleNamespace(
        get_caller_identity=lambda: {
            "Account": "123456789012",
            "UserId": "role-id:session",
        }
    )
    issuer = Issuer.__new__(Issuer)
    issuer.config = {
        "account_id": "123456789012",
        "role_id": "role-id",
        "role_arn": role,
    }
    issuer.source = SimpleNamespace(client=lambda name: {"sts": sts, "eks": eks}[name])
    if mismatch:
        with pytest.raises(ReaderRefused, match="access entry changed"):
            issuer.aws_identity(selected)
    else:
        issuer.aws_identity(selected)
