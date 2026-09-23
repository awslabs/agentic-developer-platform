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
