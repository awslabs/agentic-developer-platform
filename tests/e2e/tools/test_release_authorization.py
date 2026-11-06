"""Protected identity and permission proof, including the legacy EKS edit regression."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.e2e.tools import release_authorization as auth
from tests.e2e.tools.release_observer import Observer
from tests.e2e.tools.test_release_observer import ACCOUNT, REGION, SOURCE, fixtures


ROOT = Path(__file__).resolve().parents[3]
ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-tools-trusted-deployment"
GROUP = "adp:tools-release-dev"
RUN_ID, ATTEMPT = "12345", "2"
CALLER = f"arn:aws:sts::{ACCOUNT}:assumed-role/adp-dev-tools-trusted-deployment/deploy-{RUN_ID}-{ATTEMPT}"
MARKER = "synthetic-private-provider-value"

# Independent policy fixture: the maintained namespaced grants, not values
# taken from the proof's generated expected decisions.
RULES = [
    ["adp-gateway", "deployments", "tools-facade", ["get", "patch", "update", "watch"]],
    ["adp-gateway", "deployments", "bedrockgateway", ["get"]],
    ["adp-gateway", "services", "tools-facade", ["get", "patch", "update"]],
    ["adp-gateway", "serviceaccounts", "tools-facade", ["get", "patch", "update"]],
    [
        "adp-gateway",
        "serviceaccounts",
        "tools-facade-reconciler",
        ["get", "patch", "update"],
    ],
    ["adp-gateway", "cronjobs", "tools-facade-reconciler", ["get", "patch", "update"]],
    ["adp-gateway", "replicasets", "*", ["get"]],
    ["adp-gateway", "pods", "*", ["list"]],
    ["adp-gateway", "jobs", "*", ["list"]],
    ["fictional-agents", "deployments", "fictional-browser", ["get"]],
    ["fictional-agents", "replicasets", "*", ["get"]],
    ["fictional-agents", "pods", "*", ["list"]],
]


def permitted(namespace, verb, resource, subresource=None, *, legacy_edit=False):
    kind, _, name = resource.partition("/")
    if (
        legacy_edit
        and namespace == "adp-gateway"
        and kind in {"deployments", "secrets", "pods", "jobs"}
    ):
        return True
    return not subresource and any(
        ns == namespace
        and kind == resource_kind
        and (selected == "*" or selected == name)
        and verb in verbs
        for ns, resource_kind, selected, verbs in RULES
    )


class Provider:
    def __init__(self, *, legacy_edit=False):
        self.sts = {"Account": ACCOUNT, "Arn": CALLER}
        self.user = {"username": CALLER, "groups": [GROUP, "system:authenticated"]}
        self.legacy_edit = legacy_edit
        self.calls = []

    def read(self, *arguments):
        self.calls.append(arguments)
        if arguments[0] == "aws":
            return self.sts
        assert arguments[-4:] == ("auth", "whoami", "-o", "json")
        return {"status": {"userInfo": self.user}}

    def access(self, kubeconfig, case):
        self.calls.append(case)
        return permitted(
            case.namespace,
            case.verb,
            case.resource,
            case.subresource,
            legacy_edit=self.legacy_edit,
        )


def proof(tmp_path, provider, **changes):
    selectors, _, _ = fixtures()
    observer = Observer(
        selectors,
        account=ACCOUNT,
        region=REGION,
        cluster=selectors["cluster"],
        kubeconfig=tmp_path / "kubeconfig",
        evidence=tmp_path,
    )
    options = dict(
        expected_role=ROLE,
        expected_group=GROUP,
        definition_revision=SOURCE,
        run_id=RUN_ID,
        run_attempt=ATTEMPT,
        read=provider.read,
        access=provider.access,
    )
    options.update(changes)
    return auth.verify(observer, **options)


def test_exact_canonical_permissions_pass_and_receipt_contains_no_provider_identifiers(
    tmp_path,
):
    provider = Provider()
    result = proof(tmp_path, provider)
    assert result["status"] == "pass" and result["reason"] == "verified"
    assert result["expected_checks"] == len(result["checks"]) == 43
    assert sum(case["expected"] for case in result["checks"]) == 23
    assert result["identity"] == {"aws_matches": True, "kubernetes_matches": True}
    assert all(case["pass"] for case in result["checks"])
    serialized = json.dumps(result)
    assert CALLER not in serialized and ROLE not in serialized
    assert "fictional-browser" not in serialized and len(result["binding_sha256"]) == 64


def test_legacy_namespace_edit_policy_cannot_satisfy_protected_acceptance(tmp_path):
    result = proof(tmp_path, Provider(legacy_edit=True))
    assert (
        result["status"] == "fail"
        and result["reason"] == "authorization_scope_mismatch"
    )
    failures = {case["name"] for case in result["checks"] if not case["pass"]}
    assert {
        "gateway_write",
        "deployment_create",
        "secret_read",
        "individual_pod_read",
    } <= failures


@pytest.mark.parametrize(
    "case",
    [
        "aws_account",
        "aws_role",
        "aws_session",
        "kube_role",
        "kube_session",
        "missing_group",
        "extra_admin_group",
        "extra_shared_group",
    ],
)
def test_wrong_identity_or_extra_groups_fail_before_access_reviews(tmp_path, case):
    provider = Provider()
    if case == "aws_account":
        provider.sts["Account"] = "000000000000"
    elif case == "aws_role":
        provider.sts["Arn"] = CALLER.replace("-tools-", "-gateway-")
    elif case == "aws_session":
        provider.sts["Arn"] = CALLER.replace("deploy-12345-2", "deploy-12345-1")
    elif case == "kube_role":
        provider.user["username"] = CALLER.replace("-tools-", "-shared-tools-")
    elif case == "kube_session":
        provider.user["username"] = CALLER.replace("deploy-12345-2", "old-session")
    elif case == "missing_group":
        provider.user["groups"] = ["system:authenticated"]
    else:
        provider.user["groups"].append(
            "system:masters"
            if case == "extra_admin_group"
            else "adp:shared-tools-release-dev"
        )
    result = proof(tmp_path, provider)
    assert result["status"] == "fail" and result["checks"] == []
    assert not any(isinstance(call, auth.Case) for call in provider.calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_role": ROLE.replace("-tools-", "-gateway-")},
        {"expected_role": ROLE.replace("adp-dev-", "adp-prod-")},
        {"expected_role": ROLE.replace(ACCOUNT, "000000000000")},
        {"expected_group": "system:masters"},
        {"run_id": "not-a-run"},
        {"run_attempt": "0"},
    ],
)
def test_expected_inputs_cannot_relabel_a_different_trusted_role_as_tools(
    tmp_path, changes
):
    with pytest.raises(ValueError, match="configuration is invalid"):
        proof(tmp_path, Provider(), **changes)


def test_provider_failure_or_non_boolean_access_answer_is_not_a_successful_denial(
    tmp_path,
):
    provider = Provider()
    provider.read = lambda *args: (_ for _ in ()).throw(ValueError(MARKER))
    result = proof(tmp_path, provider)
    assert result["status"] == "fail" and MARKER not in json.dumps(result)
    provider = Provider()
    result = proof(tmp_path, provider, access=lambda *args: MARKER)
    assert (
        result["status"] == "fail" and result["reason"] == "access_review_unavailable"
    )
    assert MARKER not in json.dumps(result)


@pytest.mark.parametrize(
    "stdout,stderr,code,expected",
    [
        ("yes\n", "", 0, True),
        ("no\n", "", 1, False),
        ("no - " + MARKER, "", 1, False),
        ("no\n", "evaluation failed " + MARKER, 1, None),
        ("yes\n", "", 1, None),
        ("", "unauthorized " + MARKER, 1, None),
        ("no\n", "", 2, None),
    ],
)
def test_access_transport_requires_unambiguous_successful_review(
    monkeypatch, stdout, stderr, code, expected
):
    calls = []

    def execute(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)

    monkeypatch.setattr(auth.subprocess, "run", execute)
    case = auth.Case("exec", "create", "pods/probe", "selected", False, "exec")
    assert auth.can_i("private-kubeconfig", case) is expected
    assert calls[0][4:8] == ["auth", "can-i", "create", "pods/probe"]
    assert calls[0][-2:] == ["--subresource", "exec"]


def test_cluster_access_review_clears_kubectl_default_namespace(monkeypatch):
    calls = []

    def execute(arguments, **kwargs):
        calls.append(arguments)
        warning = (
            ""
            if "--all-namespaces" in arguments
            else "Warning: resource 'namespaces' is not namespace scoped"
        )
        return SimpleNamespace(stdout="no\n", stderr=warning, returncode=1)

    monkeypatch.setattr(auth.subprocess, "run", execute)
    case = auth.Case("namespace_list", "list", "namespaces", None, False)
    assert auth.can_i("private-kubeconfig", case) is False
    assert "--all-namespaces" in calls[0] and "--namespace" not in calls[0]


def test_evidence_is_private_and_refuses_symlink(tmp_path):
    path = tmp_path / "proof" / "authorization.json"
    auth.write_evidence(path, {"status": "fail"})
    assert path.stat().st_mode & 0o777 == 0o600
    path.unlink()
    target = tmp_path / "other"
    target.write_text("preserve")
    path.symlink_to(target)
    with pytest.raises(OSError):
        auth.write_evidence(path, {"status": "pass"})
    assert target.read_text() == "preserve"


FAKE_TOOL = r"""import json,os,sys
from pathlib import Path
fixture=json.loads(Path(os.environ["FAKE_AUTHORIZATION_FIXTURE"]).read_text())
if Path(sys.argv[0]).name=="aws":
 print(json.dumps(fixture["sts"]));sys.exit(0)
args=sys.argv[1:]
if "whoami" in args:
 print(json.dumps({"status":{"userInfo":fixture["user"]}}));sys.exit(0)
assert "can-i" in args
index=args.index("can-i");verb,resource=args[index+1:index+3]
namespace=args[args.index("--namespace")+1] if "--namespace" in args else None
subresource=args[args.index("--subresource")+1] if "--subresource" in args else None
kind,_,name=resource.partition("/")
allowed=not subresource and any(ns==namespace and k==kind and (n=="*" or n==name) and verb in verbs for ns,k,n,verbs in fixture["rules"])
if fixture["legacy_edit"] and namespace=="adp-gateway" and kind in ["deployments","secrets","pods","jobs"]:allowed=True
print("yes" if allowed else "no");sys.exit(0 if allowed else 1)
"""


@pytest.mark.parametrize("legacy_edit", [False, True])
def test_actual_workflow_step_runs_unmodified_helper_and_fails_legacy_edit(
    tmp_path, legacy_edit
):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/tools-live-evaluation.yml").read_text()
    )
    steps = workflow["jobs"]["evaluate"]["steps"]
    proof_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name")
        == "Prove the protected tools identity and authorization scope"
    )
    bind_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Bind the approved private release window"
    )
    observe_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Exercise deployed gateway through the MCP client"
    )
    step = steps[proof_index]
    assert bind_index < proof_index < observe_index
    assert (
        step["if"]
        == steps[bind_index]["if"]
        == "inputs.scenario_filter == 'SCN-8068-04' || inputs.scenario_filter == 'repair' || "
        "startsWith(inputs.scenario_filter, 'SCN-8067-') || steps.scenario-selection.outputs.atlassian_clients == 'true'"
    )
    assert "continue-on-error" not in step
    selectors, _, _ = fixtures()
    config = tmp_path / "selectors.json"
    config.write_text(json.dumps(selectors))
    config.chmod(0o600)
    kube = tmp_path / "tools-eval-kubeconfig"
    kube.write_text("owned fake kubeconfig")
    kube.chmod(0o600)
    fixture = tmp_path / "provider.json"
    provider = Provider()
    fixture.write_text(
        json.dumps(
            {
                "sts": provider.sts,
                "user": provider.user,
                "rules": RULES,
                "legacy_edit": legacy_edit,
            }
        )
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("aws", "kubectl"):
        executable = binaries / name
        executable.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
        executable.chmod(0o700)
    environment = {
        **os.environ,
        **step["env"],
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "FAKE_AUTHORIZATION_FIXTURE": str(fixture),
        "RUNNER_TEMP": str(tmp_path),
        "KUBECONFIG": str(kube),
        "ADP_TOOLS_RELEASE_SELECTORS_FILE": str(config),
        "EXPECTED_ACCOUNT": ACCOUNT,
        "EXPECTED_ROLE": ROLE,
        "AWS_REGION": REGION,
        "EKS_CLUSTER": selectors["cluster"],
        "DEFINITION_REVISION": SOURCE,
        "GITHUB_SHA": SOURCE,
        "GITHUB_RUN_ID": RUN_ID,
        "GITHUB_RUN_ATTEMPT": ATTEMPT,
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (1 if legacy_edit else 0), result.stdout + result.stderr
    evidence_path = tmp_path / "tools-eval-evidence/authorization.json"
    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == ("fail" if legacy_edit else "pass")
    assert len(evidence["checks"]) == 43
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert CALLER not in result.stdout + result.stderr


def test_invalid_private_binding_writes_sanitized_failure_and_never_uses_refresh_token(
    tmp_path, capsys
):
    environment = {
        "RUNNER_TEMP": str(tmp_path),
        "EXPECTED_ROLE": MARKER,
        "TLS_EVAL_REFRESH_TOKEN": MARKER,
        "KUBECONFIG": MARKER,
    }
    assert auth.main(environment) == 1
    evidence = (tmp_path / "tools-eval-evidence/authorization.json").read_text()
    assert MARKER not in evidence + capsys.readouterr().out
    assert json.loads(evidence)["status"] == "fail"
