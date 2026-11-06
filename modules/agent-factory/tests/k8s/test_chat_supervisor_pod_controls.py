"""Offline controls for the supervisor; installed CEL and RBAC need #6937."""

from pathlib import Path
import ast
import re

import pytest
import yaml


K8S = Path(__file__).resolve().parents[2] / "agent/k8s"


@pytest.mark.parametrize("helper", ["helper", "restore"])
def test_checkpoint_helpers_use_full_digest_annotations_and_label_safe_metadata(helper):
    policy, _ = yaml.safe_load_all((K8S / f"chat-crew-{helper}-admission.yaml").read_text())
    expression = policy["spec"]["validations"][0]["expression"]
    metadata = {"labels": {"adp.io/owner-key": "a" * 52}, "annotations": {
        "adp.io/session-hash": "b" * 64, "adp.io/checkpoint-hash": "c" * 64,
    }}
    for key in ("session-hash", "checkpoint-hash"):
        assert f"object.metadata.annotations['adp.io/{key}'].matches('^[a-f0-9]{{64}}$')" in expression
        assert f"object.metadata.labels['adp.io/{key}']" not in expression
    for field, key, pattern in re.findall(r"object.metadata.(labels|annotations)\['([^']+)'\].matches\('([^']+)'\)", expression):
        assert re.fullmatch(pattern, metadata[field][key])
    assert all(len(value) <= 63 for value in metadata["labels"].values())


@pytest.mark.parametrize("helper,label", [
    ("helper", "adp.io/crew-checkpoint-helper"), ("restore", "adp.io/crew-restore-helper"),
])
def test_checkpoint_helpers_resolve_s3_only_through_cluster_dns(helper, label):
    policies = list(yaml.safe_load_all((K8S / f"chat-crew-{helper}-egress.yaml").read_text()))
    dns, = [policy for policy in policies if policy["metadata"]["name"].endswith("-dns-egress")]
    assert dns["spec"] == {
        "podSelector": {"matchLabels": {label: "true"}}, "policyTypes": ["Egress"],
        "egress": [{"to": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}],
    }
    policy, _ = yaml.safe_load_all((K8S / f"chat-crew-{helper}-admission.yaml").read_text())
    expressions = " ".join(rule["expression"] for rule in policy["spec"]["validations"])
    assert "object.spec.dnsPolicy == 'ClusterFirst'" in expressions
    assert "!has(object.spec.dnsConfig)" in expressions
    s3 = yaml.safe_load((K8S / f"chat-crew-{helper}-s3-egress.yaml").read_text())
    assert s3["spec"]["egress"] == [{
        "to": [{"ipBlock": {"cidr": "REPLACE_WITH_APPROVED_S3_INTERFACE_IP/32"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    }]


def test_supervisor_policy_matches_every_pod_mutation_by_that_actor():
    policy, binding = yaml.safe_load_all((K8S / "chat-supervisor-pod-policy.yaml").read_text())
    assert policy["kind"] == "ValidatingAdmissionPolicy"
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert policy["spec"]["matchConstraints"]["resourceRules"] == [
        {
            "apiGroups": [""],
            "apiVersions": ["v1"],
            "operations": ["CREATE", "UPDATE", "DELETE"],
            "resources": ["pods"],
            "scope": "Namespaced",
        }
    ]
    assert policy["spec"]["matchConditions"] == [
        {
            "name": "chat-supervisor-only",
            "expression": "request.userInfo.username == 'system:serviceaccount:adp-gateway-agents:adp-chat-supervisor'",
        }
    ]
    assert len(policy["spec"]["validations"]) == 1
    expression = policy["spec"]["validations"][0]["expression"]
    for fragment in (
        "request.operation == 'DELETE' ?",
        "oldObject != null",
        "object != null",
        "oldObject.spec.serviceAccountName == 'adp-chat-sandbox'",
        "object.spec.serviceAccountName == 'adp-chat-sandbox'",
        "oldObject.metadata.labels['adp.io/chat-sandbox'] == 'true'",
        "object.metadata.labels['adp.io/chat-sandbox'] == 'true'",
        "oldObject.metadata.name.matches('^chat-turn-",
        "object.metadata.generateName.matches('^chat-turn-",
    ):
        assert fragment in expression
    for identity in ("adp-crew-pod", "adp-crew-checkpoint-helper", "adp-crew-restore-helper"):
        assert f"object.spec.serviceAccountName == '{identity}'" in expression
        assert f"oldObject.spec.serviceAccountName == '{identity}'" in expression
    assert "object.metadata.generateName.matches('^chat-checkpoint-" in expression
    launcher = (K8S.parent / "src/complex-task-chat/sandbox-launcher.ts").read_text()
    typescript_list = launcher.split("export const SUPERVISOR_POD_EXPRESSION = [", 1)[1].split(
        "].join(' ')", 1
    )[0]
    launcher_expression = " ".join(ast.literal_eval("[" + typescript_list + "]"))
    assert " ".join(expression.split()) == launcher_expression
    assert binding["spec"] == {
        "policyName": policy["metadata"]["name"],
        "validationActions": ["Deny"],
        "matchResources": {
            "namespaceSelector": {
                "matchLabels": {
                    "kubernetes.io/metadata.name": "adp-gateway-agents",
                }
            }
        },
    }


def test_rbac_only_allows_pod_lifecycle_and_read_only_control_checks():
    resources = list(yaml.safe_load_all((K8S / "chat-supervisor-rbac.yaml").read_text()))
    assert [
        (doc["kind"], doc["metadata"]["namespace"] if "namespace" in doc["metadata"] else "cluster")
        for doc in resources
    ] == [
        ("Role", "adp-gateway-agents"),
        ("RoleBinding", "adp-gateway-agents"),
        ("Role", "kube-system"),
        ("RoleBinding", "kube-system"),
        ("ClusterRole", "cluster"),
        ("ClusterRoleBinding", "cluster"),
        ("Role", "adp-gateway"),
        ("RoleBinding", "adp-gateway"),
    ]
    # Pods: lifecycle plus a namespace-scoped read of the created pod (admit only once
    # Running). No list/watch: the supervisor cannot enumerate other tenants' pods.
    assert resources[0]["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "create", "delete"]},
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["networkpolicies"],
            "verbs": ["get", "list"],
        },
    ]
    assert resources[2]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": ["amazon-vpc-cni"],
            "verbs": ["get"],
        }
    ]
    assert resources[4]["rules"] == [
        {
            "apiGroups": ["admissionregistration.k8s.io"],
            "resources": [resource],
            "resourceNames": ["adp-chat-sandbox-template", "adp-chat-supervisor-pods", "adp-crew-pod-template", "adp-crew-checkpoint-helper-template", "adp-crew-restore-helper-template"],
            "verbs": ["get"],
        }
        for resource in ("validatingadmissionpolicies", "validatingadmissionpolicybindings")
    ]
    assert resources[6]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["services"],
            "resourceNames": ["chat-sandbox-gateway"],
            "verbs": ["get"],
        }
    ]
    assert resources[7]["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "chat-supervisor-gateway-read"
    }
    for binding in (resources[1], resources[3], resources[5], resources[7]):
        assert binding["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": "adp-chat-supervisor",
                "namespace": "adp-gateway-agents",
            }
        ]
