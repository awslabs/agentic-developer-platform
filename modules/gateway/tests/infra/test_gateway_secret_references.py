"""Every required gateway secret reaches the pod by reference, never by value (#5656, A05).

Two distinct failures this suite exists to prevent.

**A required key that no deployment path supplies is a self-inflicted outage.**
`src/shared/config.py` no longer falls back from the magic-link signing key to
the session-signing key, which is the security fix — but a fail-closed setting
whose value nothing populates just moves the breakage. Before the fix,
`BG_MAGIC_LINK_SECRET` was set by *no* deployment path at all (not the
deployment manifest, not the deploy workflow, not `deploy-all.sh`), so every
environment ran on the fallback. So the same change has to wire the key into
both paths that assemble the gateway's secret material, and both are asserted
here — a key wired into only one leaves the other path bringing up a service
whose identity-linking endpoints answer 503.

**Moving a credential into a reference can reintroduce the exposure it fixed.**
A secret projected as a literal value in a manifest, or passed as a container
argument, is readable by anyone with cluster read access or CI log access — a
broader audience than repository readers. So the manifest assertions check not
only that the variable is present but that it arrives via `secretKeyRef` with no
inline `value:`.

These are static assertions over committed files. They do not prove a running
pod receives the key — that requires a deployed environment and is recorded as
pending live acceptance on the issue, not asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from src.internal.tools_dynamic_catalogue import target_binding

ROOT = Path(__file__).resolve().parents[4]
DEPLOYMENT = ROOT / "modules/gateway/k8s/deployment.yaml"
CONFIGMAP = ROOT / "modules/gateway/k8s/configmap.yaml"
DEPLOY_WORKFLOW = ROOT / ".github/workflows/gateway-deploy.yml"
DEPLOY_ALL = ROOT / "platform/scripts/deploy-all.sh"

# Secret-bearing env vars the gateway requires, and the Kubernetes Secret key
# each must be projected from. Adding a secret setting to config.py without
# adding it here and to both deployment paths is the regression this catches.
REQUIRED_SECRET_ENV = {
    "BG_TOKEN_SECRET_KEY": "token-secret-key",
    "BG_INTERNAL_API_KEY": "internal-api-key",
    # Issue #5656: separated from BG_TOKEN_SECRET_KEY so the identity-linking and
    # session purposes are independently rotatable.
    "BG_MAGIC_LINK_SECRET": "magic-link-secret",
    # Issue #7901: HMAC key deriving each owner's AgentCore memory namespace. SSM
    # SecureString, read with --with-decryption by both renderers; a leaked key
    # would let anyone address another owner's namespace.
    "ADP_AGENTCORE_NAMESPACE_KEY": "agentcore-namespace-key",
}

SECRET_NAME = "bedrockgateway-secrets"


def _gateway_container() -> dict:
    """The gateway container spec from the committed deployment manifest."""
    docs = [d for d in yaml.safe_load_all(DEPLOYMENT.read_text()) if d]
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    assert len(deployments) == 1, f"expected exactly one Deployment in {DEPLOYMENT}"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    gateway = [c for c in containers if "gateway" in c["name"]]
    assert gateway, f"no gateway container found among {[c['name'] for c in containers]}"
    return gateway[0]


def _env_by_name() -> dict[str, dict]:
    return {e["name"]: e for e in _gateway_container().get("env", [])}


@pytest.mark.parametrize(
    "env_name",
    (
        "ADP_TOOLS_KNOWLEDGE_MEDIATOR_ENABLED",
        "ADP_TOOLS_KNOWLEDGE_MEDIATOR_URL",
        "ADP_TOOLS_KNOWLEDGE_EDGE_PROVENANCE",
        "ADP_TOOLS_KNOWLEDGE_GATEWAY_ID",
        "ADP_TOOLS_KNOWLEDGE_TARGET_ID",
        "ADP_TOOLS_KNOWLEDGE_SOURCE_REVISION",
    ),
)
def test_private_knowledge_setting_is_optional_and_secret_backed(env_name):
    entries = [entry for entry in _gateway_container()["env"] if entry["name"] == env_name]
    assert entries == [
        {
            "name": env_name,
            "valueFrom": {
                "secretKeyRef": {
                    "name": "adp-tools-knowledge-mediator",
                    "key": env_name,
                    "optional": True,
                }
            },
        }
    ]
    assert env_name not in CONFIGMAP.read_text()


class TestDeploymentProjectsSecretsByReference:
    @pytest.mark.parametrize("env_name,secret_key", sorted(REQUIRED_SECRET_ENV.items()))
    def test_env_var_is_present(self, env_name, secret_key):
        assert env_name in _env_by_name(), f"{env_name} is required by the gateway but no deployment manifest supplies it"

    @pytest.mark.parametrize("env_name,secret_key", sorted(REQUIRED_SECRET_ENV.items()))
    def test_env_var_comes_from_a_secret_reference_not_a_literal(self, env_name, secret_key):
        entry = _env_by_name()[env_name]
        assert "value" not in entry, f"{env_name} must not carry an inline value in the manifest"
        ref = entry.get("valueFrom", {}).get("secretKeyRef")
        assert ref is not None, f"{env_name} must be supplied via secretKeyRef"
        assert ref["name"] == SECRET_NAME
        assert ref["key"] == secret_key

    @pytest.mark.parametrize("env_name,secret_key", sorted(REQUIRED_SECRET_ENV.items()))
    def test_secret_is_not_in_the_configmap(self, env_name, secret_key):
        """A ConfigMap is not a secret store — its contents are plainly readable.

        Asserted per-variable because the original #133 incident was exactly this:
        the token-signing key sitting in the ConfigMap.

        Checks the parsed `data` KEYS rather than the file text, because the
        ConfigMap carries a deliberate comment warning maintainers not to add
        BG_TOKEN_SECRET_KEY to it. A raw-text check would flag that warning as the
        violation it warns about, and the obvious way to "fix" the test would be to
        delete the warning.
        """
        docs = [d for d in yaml.safe_load_all(CONFIGMAP.read_text()) if d]
        for doc in docs:
            if doc.get("kind") != "ConfigMap":
                continue
            assert env_name not in (doc.get("data") or {}), f"{env_name} must live in a Secret, not ConfigMap {doc['metadata']['name']!r}"


class TestBothDeploymentPathsSupplyTheKey:
    """The workflow and the self-managed script both assemble the K8s Secret.

    Asserted independently: they are separate files that drifted before (the
    magic-link key was absent from both, and the internal-api-key was added to
    deploy-all.sh only later, per its own #2824 comment). A key added to one is
    still missing on the other path's environments.
    """

    @pytest.mark.parametrize("path", [DEPLOY_WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
    @pytest.mark.parametrize("secret_key", sorted(REQUIRED_SECRET_ENV.values()))
    def test_path_populates_the_secret_key(self, path, secret_key):
        text = path.read_text()
        assert f"--from-literal={secret_key}=" in text, (
            f"{path.name} must populate {secret_key!r} into the {SECRET_NAME} Secret, "
            "or environments deployed by this path bring up a gateway missing that key"
        )

    @pytest.mark.parametrize("path", [DEPLOY_WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
    @pytest.mark.parametrize("secret_key", sorted(REQUIRED_SECRET_ENV.values()))
    def test_value_comes_from_a_shell_variable_not_a_committed_literal(self, path, secret_key):
        """`--from-literal=k=$VAR` is fine; `--from-literal=k=actualsecret` is not.

        The flag is named "from-literal" but what matters is whether the value is
        a shell expansion (resolved at deploy time from Secrets Manager) or a
        string committed to the repository.
        """
        text = path.read_text()
        for match in re.finditer(rf"--from-literal={re.escape(secret_key)}=(\S+)", text):
            value = match.group(1).strip("\"'\\")
            assert value.startswith("$"), (
                f"{path.name} assigns {secret_key!r} the committed literal {value!r}; "
                "it must expand a variable read from the secret store at deploy time"
            )


class TestNoInlineSecretValuesInGatewayManifests:
    """No committed gateway manifest carries secret material inline."""

    # 64 hex chars is what `openssl rand -hex 32` produces — the shape every one
    # of these keys is generated in — so a pasted real key is recognisable even
    # without knowing its name.
    _HEX_SECRET = re.compile(r"\b[0-9a-f]{64}\b")

    @pytest.mark.parametrize(
        "path",
        sorted((ROOT / "modules/gateway/k8s").glob("*.yaml")),
        ids=lambda p: p.name,
    )
    def test_manifest_has_no_generated_key_shaped_literal(self, path):
        found = self._HEX_SECRET.findall(path.read_text())
        assert found == [], f"{path.name} contains a value shaped like a generated key"

    def test_no_secret_is_passed_as_a_container_argument(self):
        """Arguments are visible in `kubectl describe` and in the process table.

        Env-from-secret keeps the value out of both; an argument does not.
        """
        container = _gateway_container()
        rendered = " ".join(container.get("command", []) + container.get("args", []))
        for env_name in REQUIRED_SECRET_ENV:
            assert env_name not in rendered, f"{env_name} must not be passed as a container command/argument"


@pytest.mark.parametrize(
    "env_name",
    [
        f"ADP_TOOLS_{provider}_{field}"
        for provider in ("GITHUB", "JIRA", "SLACK")
        for field in ("MEDIATOR_ENABLED", "MEDIATOR_URL", "GATEWAY_ID", "TARGET_ID", "SOURCE_REVISION")
    ]
    + ["ADP_TOOLS_SLACK_POST_ENABLED", "ADP_TOOLS_SLACK_POST_FIXTURE"],
)
def test_connection_mediator_binding_survives_canonical_rollout_without_default_enablement(env_name):
    entries = [entry for entry in _gateway_container()["env"] if entry["name"] == env_name]
    source, name = ("secretKeyRef", "adp-tools-oauth-mediators") if "_POST_" in env_name else ("configMapKeyRef", "adp-tool-adapter-runtime")
    assert entries == [
        {
            "name": env_name,
            "valueFrom": {source: {"name": name, "key": env_name, "optional": True}},
        }
    ]
    assert env_name not in CONFIGMAP.read_text()


def _connector_environment(secrets, configmaps):
    """Resolve the manifest's ordered envFrom sources, then explicit env overrides."""
    values = {}
    for entry in _gateway_container()["envFrom"]:
        for source, documents in (("secretRef", secrets), ("configMapRef", configmaps)):
            if source in entry:
                reference = entry[source]
                if reference["name"] not in documents:
                    assert reference.get("optional")
                    continue
                values.update(documents[reference["name"]])
    for entry in _gateway_container()["env"]:
        if not entry["name"].startswith(("ADP_TOOLS_GITHUB_", "ADP_TOOLS_JIRA_", "ADP_TOOLS_SLACK_")):
            continue
        if "value" in entry:
            values[entry["name"]] = entry["value"]
            continue
        for source, documents in (("secretKeyRef", secrets), ("configMapKeyRef", configmaps)):
            if source in entry.get("valueFrom", {}):
                reference = entry["valueFrom"][source]
                data = documents.get(reference["name"], {})
                if reference["key"] not in data:
                    assert reference.get("optional")
                    continue
                values[entry["name"]] = data[reference["key"]]
    return values


@pytest.mark.parametrize("provider", ("github", "jira", "slack"))
@pytest.mark.parametrize("phase", ("before-configmap", "empty-configmap", "active-redeploy", "rollback-redeploy"))
def test_legacy_connector_binding_survives_rollout_and_runtime_takes_precedence(provider, phase, monkeypatch):
    prefix = f"ADP_TOOLS_{provider.upper()}_"

    def settings(release):
        return {
            prefix + "MEDIATOR_ENABLED": "true",
            prefix + "MEDIATOR_URL": f"https://{release}.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/{provider}/mcp",
            prefix + "GATEWAY_ID": f"{release}-gateway",
            prefix + "TARGET_ID": f"{release}-{provider}",
            prefix + "SOURCE_REVISION": ("a" if release == "legacy" else "b") * 40,
        }

    legacy = settings("legacy")
    runtime = settings("active") if phase == "active-redeploy" else {}
    if phase == "rollback-redeploy":
        runtime = {"ADP_TOOL_ADAPTER_RELEASE": '{"group_generation":3}', "ADP_TOOL_ADAPTER_LEGACY_ENABLED": "false"}
    maps = {"bedrockgateway-config": {}}
    if phase != "before-configmap":
        maps["adp-tool-adapter-runtime"] = runtime
    environment = _connector_environment({"adp-tools-oauth-mediators": legacy}, maps)
    expected = settings("active") if phase == "active-redeploy" else legacy
    for name in legacy:
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    role = "arn:aws:iam::123456789012:role/fixture-tools"
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", role)
    gateway = {
        "gatewayId": expected[prefix + "GATEWAY_ID"],
        "gatewayArn": "fixture-gateway-arn",
        "roleArn": role,
        "status": "READY",
        "authorizerType": "AWS_IAM",
        "interceptorConfigurations": [{"fixture": "interceptor"}],
        "updatedAt": "2026-01-01T00:00:00Z",
    }
    target = {
        "targetId": expected[prefix + "TARGET_ID"],
        "gatewayArn": gateway["gatewayArn"],
        "name": provider,
        "status": "READY",
        "updatedAt": "2026-01-01T00:00:00Z",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": expected[prefix + "MEDIATOR_URL"], "listingMode": "DYNAMIC"}}},
        "privateEndpoint": {"managedVpcResource": {"vpcIdentifier": "vpc-00000000"}},
        "credentialProviderConfigurations": [
            {"credentialProviderType": "GATEWAY_IAM_ROLE", "credentialProvider": {"iamCredentialProvider": {"service": "execute-api"}}}
        ],
    }
    binding = target_binding(gateway["gatewayId"], target, gateway)
    assert binding["target_id"] == expected[prefix + "TARGET_ID"]
    assert binding["source_revision"] == expected[prefix + "SOURCE_REVISION"]


def test_runtime_disablement_overrides_legacy_enablement():
    name = "ADP_TOOLS_JIRA_MEDIATOR_ENABLED"
    environment = _connector_environment(
        {"adp-tools-oauth-mediators": {name: "true"}},
        {"bedrockgateway-config": {}, "adp-tool-adapter-runtime": {name: "false"}},
    )
    assert environment[name] == "false"


def test_optional_connector_sources_do_not_enable_fresh_installations():
    assert _connector_environment({}, {"bedrockgateway-config": {}}) == {}
