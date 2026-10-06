"""Offline controls for the supervisor; installed CEL and RBAC need #6937."""

from pathlib import Path
import ast

import yaml


K8S = Path(__file__).resolve().parents[2] / "agent/k8s"


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
    assert "||" not in expression
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
    assert resources[0]["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "delete"]},
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
            "resourceNames": ["adp-chat-sandbox-template", "adp-chat-supervisor-pods"],
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
