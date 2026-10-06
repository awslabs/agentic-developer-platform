from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parents[2] / "agent" / "k8s"


def test_admission_catches_account_substitution_before_a_pod_is_scheduled():
    policy, binding = yaml.safe_load_all((K8S / "chat-sandbox-admission.yaml").read_text())
    assert policy["apiVersion"] == binding["apiVersion"] == "admissionregistration.k8s.io/v1"
    assert policy["kind"] == "ValidatingAdmissionPolicy"
    assert binding["kind"] == "ValidatingAdmissionPolicyBinding"
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert policy["spec"]["matchConstraints"]["resourceRules"][0]["operations"] == ["CREATE", "UPDATE"]
    match = policy["spec"]["matchConditions"][0]["expression"]
    assert "serviceAccountName == 'adp-chat-sandbox'" in match
    assert "'adp.io/chat-sandbox' in object.metadata.labels" in match
    assert binding["spec"]["policyName"] == policy["metadata"]["name"]
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "adp-gateway-agents"
    }


def test_fixed_template_rejects_host_privilege_credentials_and_other_images():
    policy, _ = yaml.safe_load_all((K8S / "chat-sandbox-admission.yaml").read_text())
    conditions = "\n".join(rule["expression"] for rule in policy["spec"]["validations"])
    for fragment in (
        "object.spec.serviceAccountName == 'adp-chat-sandbox'",
        "object.spec.hostNetwork == false",
        "object.spec.hostPID == false",
        "object.spec.hostIPC == false",
        "object.spec.automountServiceAccountToken == false",
        "!has(object.spec.initContainers)",
        "size(object.spec.volumes) == 3",
        "volume.configMap.name.matches('^chat-sandbox-gateway-ca-[a-f0-9]{16}$')",
        "entry.name == 'NODE_EXTRA_CA_CERTS' && entry.value == '/var/run/adp-chat-ca/ca.crt'",
        "object.spec.hostAliases[0].hostnames == ['chat-sandbox-gateway.adp-gateway.svc']",
        "has(v.projected)",
        "serviceAccountToken.audience == 'adp-agent-bootstrap'",
        "object.spec.containers[0].command == ['/app/chat-sandbox-entrypoint']",
        "object.spec.containers[0].workingDir == '/tmp'",
        "!has(object.spec.containers[0].envFrom)",
        "!has(e.valueFrom)",
        "object.spec.containers[0].securityContext.allowPrivilegeEscalation == false",
        "object.spec.containers[0].securityContext.capabilities.drop == ['ALL']",
    ):
        assert fragment in conditions
    assert "@sha256:" in conditions


def test_sandbox_policy_denies_all_traffic_until_an_authorized_gateway_route_exists():
    policy = yaml.safe_load((K8S / "chat-sandbox-network-policy.yaml").read_text())
    assert policy["metadata"]["namespace"] == "adp-gateway-agents"
    assert policy["spec"] == {
        "podSelector": {"matchLabels": {"adp.io/chat-sandbox": "true"}},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [],
        "egress": [],
    }


def test_gateway_exception_allows_only_the_dedicated_namespace_and_pods_on_tls_port():
    policy = yaml.safe_load((K8S / "chat-sandbox-gateway-egress.yaml").read_text())
    assert policy["metadata"] == {
        "name": "chat-sandbox-gateway-egress", "namespace": "adp-gateway-agents"
    }
    assert policy["spec"] == {
        "podSelector": {"matchLabels": {"adp.io/chat-sandbox": "true"}},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "adp-gateway"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "chat-sandbox-gateway"}},
            }],
            "ports": [{"protocol": "TCP", "port": 8443}],
        }],
    }
