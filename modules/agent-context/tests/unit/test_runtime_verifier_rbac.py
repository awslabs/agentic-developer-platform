"""The runtime verifier's read-only Role exists in agent-context AND both delivery paths apply it (#7870).

The orchestration engine verifies a Knowledge Door release by reading the context-mcp
Deployment, its ReplicaSets and Pods as the environment connection role
(``ADP-Agent-<env>-platform``, EKS access-entry group ``adp:runtime-verifier-<env>``)
and by probing the Door through the API server's scoped service proxy. That role had
a Role in ``adp-gateway`` (granted live, 2026-10-15) and nothing in ``agent-context``:
the Door story's first verifier read was refused (403) and every green deploy ended
"exited without verified runtime evidence" (TLS-X02 door5, #7289).

As with ``test_door_networkpolicy.py``, these assertions are about *delivery* as much
as content: a Role that only exists in git is indistinguishable from no Role.
"""

from __future__ import annotations

from pathlib import Path

import yaml

MODULE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = MODULE_ROOT.parents[1]
MANIFEST = MODULE_ROOT / "manifests" / "ingestion-rbac.yaml"
DEPLOY_SH = MODULE_ROOT / "deploy.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-context-deploy.yml"
RBAC_GROUP = "rbac.authorization.k8s.io"


def _docs() -> list[dict]:
    text = MANIFEST.read_text().replace("${ENVIRONMENT}", "dev").replace("${NAMESPACE}", "agent-context")
    assert "${" not in text, f"unsubstituted template variable in {MANIFEST.name}: add it to this helper"
    return [d for d in yaml.safe_load_all(text) if d]


def _verifier(kind: str) -> dict:
    docs = [d for d in _docs() if d.get("kind") == kind and d["metadata"]["name"] == "adp-runtime-verifier"]
    assert len(docs) == 1, f"expected exactly one adp-runtime-verifier {kind}, found {len(docs)}"
    return docs[0]


def _rule(role: dict, group: str, resource: str) -> dict:
    rules = [r for r in role["rules"] if group in r["apiGroups"] and resource in r["resources"]]
    assert len(rules) == 1, f"expected one rule for {group or 'core'}/{resource}, found {len(rules)}"
    return rules[0]


class TestRole:
    def test_is_namespaced_to_the_door_namespace(self):
        assert _verifier("Role")["metadata"]["namespace"] == "agent-context"

    def test_reads_the_door_deployment_replicasets_pods_and_services(self):
        """The verifier lists ReplicaSets and Pods by label selector, so no resourceNames here."""
        role = _verifier("Role")
        for group, resource in (("apps", "deployments"), ("apps", "replicasets"), ("", "pods"), ("", "services")):
            rule = _rule(role, group, resource)
            assert set(rule["verbs"]) == {"get", "list"}, (group, resource, rule["verbs"])
            assert "resourceNames" not in rule, (group, resource)

    def test_probes_the_door_only_through_its_scoped_service_proxy(self):
        """``door_identity_status`` GETs ``services/http:context-mcp:5100/proxy/tools``; nothing else may be proxied."""
        rule = _rule(_verifier("Role"), "", "services/proxy")
        assert set(rule["resourceNames"]) == {"http:context-mcp:5100", "context-mcp"}
        assert set(rule["verbs"]) == {"get", "create"}

    def test_grants_no_write_verb(self):
        for rule in _verifier("Role")["rules"]:
            verbs = set(rule["verbs"])
            assert not verbs & {"patch", "update", "delete", "deletecollection", "watch"}, rule
            if "create" in verbs:
                assert rule["resources"] == ["services/proxy"], "create is only the proxy verb, never an object write"


class TestBinding:
    def test_binds_the_environment_verifier_group_to_the_role(self):
        binding = _verifier("RoleBinding")
        assert binding["metadata"]["namespace"] == "agent-context"
        assert binding["roleRef"] == {"kind": "Role", "name": "adp-runtime-verifier", "apiGroup": RBAC_GROUP}
        assert binding["subjects"] == [{"kind": "Group", "name": "adp:runtime-verifier-dev", "apiGroup": RBAC_GROUP}]

    def test_group_follows_the_environment(self):
        """The group is what the EKS access entry maps the connection role to; it is per environment."""
        assert "adp:runtime-verifier-${ENVIRONMENT}" in MANIFEST.read_text()


class TestDelivery:
    def test_both_delivery_paths_apply_the_manifest(self):
        for path in (DEPLOY_SH, DEPLOY_WORKFLOW):
            assert "manifests/ingestion-rbac.yaml" in path.read_text(), f"{path.name} does not apply ingestion-rbac.yaml"

    def test_deploy_sh_defaults_the_environment_before_rendering(self):
        """With --skip-terraform deploy.sh never set ENVIRONMENT; the group would render as ``adp:runtime-verifier-``."""
        text = DEPLOY_SH.read_text()
        apply_at = text.index('template_file "${SCRIPT_DIR}/manifests/ingestion-rbac.yaml"')
        assert 'ENVIRONMENT="${ENVIRONMENT:-dev}"' in text[max(0, apply_at - 600) : apply_at]
        assert "export RUNNER_NAMESPACE RUNNER_SERVICE_ACCOUNT ENVIRONMENT" in text[max(0, apply_at - 600) : apply_at]

    def test_workflow_renders_the_manifest_with_the_environment(self):
        text = DEPLOY_WORKFLOW.read_text()
        assert "template_file manifests/ingestion-rbac.yaml | kubectl apply" in text
        assert "ENVIRONMENT:" in text, "the deploy job must define ENVIRONMENT for template_file to render the group name"
