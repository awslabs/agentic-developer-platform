"""The Door NetworkPolicy exists AND both delivery paths apply it (#4073, #8).

Three docstrings in this module claimed an in-cluster NetworkPolicy made the
Door's identity headers trustworthy. No NetworkPolicy existed anywhere in
agent-context, so the trust boundary was documented and never enforced.

Why these tests are about *delivery*, not just content: a manifest that is not
referenced by any apply path is indistinguishable from no manifest at all.
agent-context has two independent delivery paths that each enumerate manifests
by hand — ``deploy.sh`` (self-managed) and ``.github/workflows/agent-context-deploy.yml``
(CI). Adding the file to one and forgetting the other is the realistic
regression, and it is invisible in a code review of the manifest itself.

Deliberately parses YAML/text rather than talking to a cluster: these run in the
default unit suite with no kubectl and no AWS.
"""

from __future__ import annotations

from pathlib import Path

# Hard import, not pytest.importorskip: pyyaml is in the agent-context CI test
# dependency set (agent-context-ci.yml). importorskip would turn a missing dep
# into six silently-skipped security assertions, which reads as green.
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = MODULE_ROOT.parents[1]
POLICY_PATH = MODULE_ROOT / "manifests" / "networkpolicy.yaml"
DEPLOY_SH = MODULE_ROOT / "deploy.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-context-deploy.yml"


def _policy_docs() -> list[dict]:
    """Parse the NetworkPolicy manifest, substituting the templated namespace.

    ``${NAMESPACE}`` and ``${RUNNER_NAMESPACE}`` are expanded by
    ``template_file`` at apply time; envsubst output is not valid YAML to a
    strict parser only in the sense that the raw ``${...}`` is an ordinary
    string, so substitution keeps the assertions below readable.

    Values match config.env's defaults. Note the ordering: ``${RUNNER_NAMESPACE}``
    is substituted FIRST, because replacing ``${NAMESPACE}`` first would rewrite
    the tail of ``${RUNNER_NAMESPACE}`` and leave a mangled ``${RUNNER_agent-...``
    behind.
    """
    text = (
        POLICY_PATH.read_text()
        .replace("${RUNNER_NAMESPACE}", "arc-runners")
        .replace("${NAMESPACE}", "agent-context")
    )
    assert "${" not in text, (
        f"Unsubstituted template variable left in the parsed manifest: {text!r}. "
        "Add it to this helper, or the assertions below silently test a literal "
        "'${VAR}' string instead of a namespace name."
    )
    return [d for d in yaml.safe_load_all(text) if d]


class TestPolicyExists:
    def test_manifest_file_exists(self):
        assert POLICY_PATH.is_file(), (
            f"{POLICY_PATH.relative_to(REPO_ROOT)} is missing. acl.py, "
            "personal_context/identity.py and README.md all claim a NetworkPolicy "
            "restricts access to the Door (issue #4073 finding #8)."
        )

    def test_is_a_networkpolicy_targeting_the_door(self):
        policies = [d for d in _policy_docs() if d.get("kind") == "NetworkPolicy"]
        assert policies, "No NetworkPolicy document in the manifest."
        selector = policies[0]["spec"]["podSelector"]["matchLabels"]
        assert selector.get("app.kubernetes.io/name") == "context-mcp", (
            f"podSelector {selector} does not match the Door pods. The label must "
            "match the Deployment template in manifests/context-mcp.yaml, or the "
            "policy applies to nothing and silently protects nothing."
        )


class TestPolicyRestrictsIngress:
    def test_policy_types_is_ingress_only(self):
        """Egress must NOT be listed.

        Naming Egress — even with no egress rules — denies ALL outbound traffic
        from the Door, which needs Zoekt, LiteLLM, Postgres, S3, S3 Vectors,
        Neptune and Bedrock. That failure is a total verb outage.
        """
        spec = _policy_docs()[0]["spec"]
        assert spec.get("policyTypes") == ["Ingress"], (
            f"policyTypes is {spec.get('policyTypes')!r}; expected ['Ingress'] only."
        )

    def test_no_rule_admits_every_source(self):
        """The load-bearing assertion: no rule may have an empty/absent ``from``.

        A NetworkPolicy ingress rule carrying only ``ports`` matches EVERY
        source. Such a rule re-opens port 5100 cluster-wide while the manifest
        still *looks* like it restricts access — the policy becomes decoration.
        This is an easy mistake to make when adding an exemption for kubelet
        probes, which is exactly why it is pinned here.
        """
        for i, rule in enumerate(_policy_docs()[0]["spec"].get("ingress", [])):
            sources = rule.get("from")
            assert sources, (
                f"ingress rule {i} has no 'from' selector, so it admits every pod "
                f"in the cluster on {rule.get('ports')}. That negates the whole "
                "policy. Scope it with a namespaceSelector/podSelector/ipBlock."
            )

    def test_agent_namespace_is_admitted(self):
        """The real callers must not be locked out.

        Agent workers in adp-agents are the Door's primary client
        (knowledge-layer-config.ts, experience-save-hook.ts,
        recall-at-task-start.ts). A policy that omits them breaks agent context
        retrieval with no error at the Door.
        """
        namespaces = {
            src["namespaceSelector"]["matchLabels"].get("kubernetes.io/metadata.name")
            for rule in _policy_docs()[0]["spec"].get("ingress", [])
            for src in rule.get("from", [])
            if "namespaceSelector" in src
        }
        assert "adp-agents" in namespaces, (
            f"adp-agents is not admitted (admitted: {sorted(namespaces)}). Agent "
            "worker pods live there and are the Door's main caller."
        )

    def test_runner_namespace_is_templated_not_hardcoded(self):
        """The ARC runner namespace must come from ``RUNNER_NAMESPACE``.

        ARC namespaces are per-repo (``arc-runners-<repo>``), and config.env
        already exposes ``RUNNER_NAMESPACE`` as the knob — ingestion-rbac.yaml
        templates the same value. Hardcoding "arc-runners" here would silently
        drop agent-context-verb-ops.yml's traffic on any deployment that
        overrides it: the workflow's POST /call would simply hang.
        """
        raw = POLICY_PATH.read_text()
        assert "${RUNNER_NAMESPACE}" in raw, (
            "The ARC runner namespace is not templated from ${RUNNER_NAMESPACE}. "
            "A literal namespace name breaks any deploy that overrides it."
        )

    def test_run_service_bridge_requires_gateway_namespace_and_pod(self):
        peers = [
            peer
            for rule in _policy_docs()[0]["spec"]["ingress"]
            for peer in rule["from"]
            if peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "adp-gateway"
        ]
        assert len(peers) == 1
        assert peers[0]["podSelector"] == {"matchLabels": {"app": "bedrockgateway"}}


class TestBothDeliveryPathsApplyIt:
    """A manifest no apply path references is the same as no manifest."""

    @staticmethod
    def _uncommented(path: Path) -> str:
        """File text with ``#`` comment lines stripped.

        Without this a commented-out reference — or this test suite's own
        explanatory comments in the deploy scripts — would satisfy the
        assertions below.
        """
        lines = []
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lines.append(line.split(" #")[0] if " #" in line else line)
        return "\n".join(lines)

    def test_deploy_sh_applies_it(self):
        text = self._uncommented(DEPLOY_SH)
        assert "manifests/networkpolicy.yaml" in text, (
            "deploy.sh never applies manifests/networkpolicy.yaml, so a "
            "self-managed deploy leaves the Door unrestricted. deploy.sh "
            "enumerates every manifest by hand — it does not glob the directory."
        )

    def test_deploy_workflow_applies_it(self):
        text = self._uncommented(DEPLOY_WORKFLOW)
        assert "manifests/networkpolicy.yaml" in text, (
            "agent-context-deploy.yml never applies manifests/networkpolicy.yaml, "
            "so no CI deploy installs it. This workflow enumerates manifests "
            "independently of deploy.sh; both lists must be updated."
        )


class TestDoorKeyIsSeeded:
    """The primary control needs its secret, or the Door 503s every verb."""

    def test_deployment_mounts_door_api_key(self):
        text = (MODULE_ROOT / "manifests" / "context-mcp.yaml").read_text()
        docs = [d for d in yaml.safe_load_all(text.replace("${NAMESPACE}", "agent-context")) if d]
        deployment = next(d for d in docs if d.get("kind") == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        names = {e["name"] for e in container.get("env", [])}
        assert "DOOR_API_KEY" in names, (
            "The context-mcp container has no DOOR_API_KEY env var, so "
            "door/auth.py finds no configured key and fails closed with 503 on "
            "every authenticated path (issue #4073 finding #8)."
        )

    def test_health_probes_are_unauthenticated_paths(self):
        """Probes must target /health, the one path auth.py exempts.

        Pointing a probe at any other path would make the kubelet's credential-free
        request 401 and CrashLoop the Deployment.
        """
        from door.auth import _is_public_path

        text = (MODULE_ROOT / "manifests" / "context-mcp.yaml").read_text()
        docs = [d for d in yaml.safe_load_all(text.replace("${NAMESPACE}", "agent-context")) if d]
        deployment = next(d for d in docs if d.get("kind") == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        probes = [p for p in (container.get("readinessProbe"), container.get("livenessProbe")) if p]
        assert probes, "context-mcp has no probes; expected readiness + liveness."
        for probe in probes:
            path = probe["httpGet"]["path"]
            assert _is_public_path(path), (
                f"Probe path {path!r} is not exempt from Door authentication. The "
                "kubelet cannot present the shared secret, so the probe would get "
                "401 and the Deployment would CrashLoop."
            )
