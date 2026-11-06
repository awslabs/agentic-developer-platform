"""Prove protected tools identity and Kubernetes scope using read-only access reviews."""

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tests.e2e.tools.release_observer import (
    command,
    from_environment as observer_from_environment,
)


@dataclass(frozen=True)
class Case:
    name: str
    verb: str
    resource: str
    namespace: str | None
    allowed: bool
    subresource: str | None = None


def authorization_cases(components):
    """Bind the maintained tools release contract to approved workload selectors.

    The facade Deployment, Service and ServiceAccount share its resource name;
    the reconciler CronJob and ServiceAccount share its resource name. Browser
    names and namespaces come from the separately selected shared provider.
    """
    facade = components["facade and reconciler"]
    gateway = components["gateway"]
    namespace, name, cronjob = (
        facade[key] for key in ("namespace", "deployment", "cronjob")
    )
    cases = []

    def add(label, verb, resource, allowed, ns=namespace, subresource=None):
        cases.append(Case(label, verb, resource, ns, allowed, subresource))

    for verb in ("get", "patch", "update", "watch"):
        add(f"facade_deployment_{verb}", verb, f"deployments/{name}", True)
    for label, resource in (
        ("facade_service", f"services/{name}"),
        ("facade_account", f"serviceaccounts/{name}"),
        ("reconciler_account", f"serviceaccounts/{cronjob}"),
        ("reconciler_cronjob", f"cronjobs/{cronjob}"),
    ):
        for verb in ("get", "patch", "update"):
            add(f"{label}_{verb}", verb, resource, True)
    add(
        "gateway_read",
        "get",
        f"deployments/{gateway['deployment']}",
        True,
        gateway["namespace"],
    )
    add("facade_replicaset_read", "get", "replicasets/adp-authorization-probe", True)
    add("facade_pods_list", "list", "pods", True)
    add("reconciler_jobs_list", "list", "jobs", True)

    names = {value.get("deployment") for value in components.values()}
    foreign = "adp-authorization-foreign"
    while foreign in names:
        foreign += "-other"
    for label, verb, resource in (
        ("foreign_deployment_read", "get", f"deployments/{foreign}"),
        ("deployment_create", "create", "deployments"),
        ("facade_delete", "delete", f"deployments/{name}"),
        ("secret_read", "get", "secrets/adp-authorization-probe"),
        ("secrets_list", "list", "secrets"),
        ("role_update", "update", "roles/adp-tools-release"),
        ("rolebinding_create", "create", "rolebindings"),
        ("individual_pod_read", "get", "pods/adp-authorization-probe"),
        ("individual_job_read", "get", "jobs/adp-authorization-probe"),
    ):
        add(label, verb, resource, False)
    add(
        "gateway_write",
        "patch",
        f"deployments/{gateway['deployment']}",
        False,
        gateway["namespace"],
    )
    add("pod_exec", "create", "pods/adp-authorization-probe", False, subresource="exec")
    add("pod_logs", "get", "pods/adp-authorization-probe", False, subresource="log")
    add(
        "account_token", "create", f"serviceaccounts/{name}", False, subresource="token"
    )
    add("system_namespace_pods", "list", "pods", False, "kube-system")
    add("cluster_namespace_list", "list", "namespaces", False, None)

    if "shared Lambda and Browser" in components:
        browser = components["shared Lambda and Browser"]
        browser_ns, browser_name = browser["namespace"], browser["deployment"]
        add("browser_read", "get", f"deployments/{browser_name}", True, browser_ns)
        add(
            "browser_replicaset_read",
            "get",
            "replicasets/adp-authorization-probe",
            True,
            browser_ns,
        )
        add("browser_pods_list", "list", "pods", True, browser_ns)
        for label, verb, resource in (
            ("browser_write", "patch", f"deployments/{browser_name}"),
            ("browser_foreign_read", "get", f"deployments/{foreign}"),
            ("browser_deployment_create", "create", "deployments"),
            ("browser_secret_read", "get", "secrets/adp-authorization-probe"),
            ("browser_rolebinding_create", "create", "rolebindings"),
        ):
            add(label, verb, resource, False, browser_ns)
    return cases


def can_i(kubeconfig, case):
    # The requested verb is an authorization question, never an actual mutation.
    arguments = [
        "kubectl",
        "--kubeconfig",
        kubeconfig,
        "--request-timeout=15s",
        "auth",
        "can-i",
        case.verb,
        case.resource,
    ]
    if case.namespace is not None:
        arguments += ["--namespace", case.namespace]
    else:
        # Clear kubectl's default namespace for cluster-scoped reviews. Without
        # this, kubectl emits a warning and the strict diagnostic guard rejects
        # an otherwise valid denial as an incomplete access review.
        arguments += ["--all-namespaces"]
    if case.subresource is not None:
        arguments += ["--subresource", case.subresource]
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.stderr.strip():
        # Failed/evaluated-incompletely access reviews can otherwise look like
        # ordinary denials. Never treat provider diagnostics as negative proof.
        return None
    # Kubernetes may append a denial reason. Do not retain its text in evidence.
    answer = result.stdout.strip().split(" - ", 1)[0]
    if answer == "yes" and result.returncode == 0:
        return True
    if answer == "no" and result.returncode == 1:
        return False
    return None


def verify(
    observer,
    *,
    expected_role,
    expected_group,
    definition_revision,
    run_id,
    run_attempt,
    read=command,
    access=can_i,
):
    group = re.fullmatch(r"adp:tools-release-([a-z0-9-]+)", expected_group)
    if (
        not group
        or expected_role
        != f"arn:aws:iam::{observer.account}:role/adp-{group[1]}-tools-trusted-deployment"
        or not re.fullmatch(r"[a-f0-9]{40}", definition_revision)
        or not re.fullmatch(r"[1-9][0-9]*", run_id)
        or not re.fullmatch(r"[1-9][0-9]*", run_attempt)
    ):
        raise ValueError("Protected authorization identity configuration is invalid")
    role_name = expected_role.rsplit("/", 1)[-1]
    assumed_role = f"arn:aws:sts::{observer.account}:assumed-role/{role_name}/deploy-{run_id}-{run_attempt}"
    binding = {
        "account": observer.account,
        "region": observer.region,
        "cluster": observer.cluster,
        "role": expected_role,
        "group": expected_group,
        "components": observer.components,
    }
    cases = authorization_cases(observer.components)
    evidence = {
        "version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "status": "fail",
        "definition_revision": definition_revision,
        "workflow_run_id": run_id,
        "workflow_run_attempt": run_attempt,
        "binding_sha256": hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "identity": {"aws_matches": False, "kubernetes_matches": False},
        "expected_checks": len(cases),
        "checks": [],
        "reason": "identity_unavailable",
    }
    try:
        identity = read(
            "aws",
            "sts",
            "get-caller-identity",
            "--region",
            observer.region,
            "--output",
            "json",
        )
        if (
            identity.get("Account") != observer.account
            or identity.get("Arn") != assumed_role
        ):
            evidence["reason"] = "aws_identity_mismatch"
            return evidence
        evidence["identity"]["aws_matches"] = True
        user = read(
            "kubectl",
            "--kubeconfig",
            observer.kubeconfig,
            "--request-timeout=15s",
            "auth",
            "whoami",
            "-o",
            "json",
        )["status"]["userInfo"]
        if user.get("username") != assumed_role or set(user.get("groups", [])) != {
            expected_group,
            "system:authenticated",
        }:
            evidence["reason"] = "kubernetes_identity_mismatch"
            return evidence
        evidence["identity"]["kubernetes_matches"] = True
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        return evidence
    for case in cases:
        try:
            observed = access(observer.kubeconfig, case)
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            observed = None
        if type(observed) is not bool:
            observed = None
        evidence["checks"].append(
            {
                "name": case.name,
                "expected": case.allowed,
                "observed": observed,
                "pass": observed is case.allowed,
            }
        )
        if observed is None:
            evidence["reason"] = "access_review_unavailable"
            return evidence
    evidence["status"] = (
        "pass" if all(case["pass"] for case in evidence["checks"]) else "fail"
    )
    evidence["reason"] = (
        "verified" if evidence["status"] == "pass" else "authorization_scope_mismatch"
    )
    return evidence


def write_evidence(path, evidence):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(
        path,
        "w",
        opener=lambda value, flags: os.open(value, flags | os.O_NOFOLLOW, 0o600),
    ) as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(evidence, stream, sort_keys=True)
        stream.write("\n")


def main(environment=os.environ):
    try:
        path = (
            Path(environment["RUNNER_TEMP"])
            / "tools-eval-evidence"
            / "authorization.json"
        )
        evidence = {
            "version": 1,
            "status": "fail",
            "reason": "authorization_configuration_invalid",
            "checks": [],
        }
        try:
            observer = observer_from_environment(environment)
            if environment["GITHUB_SHA"] != environment["DEFINITION_REVISION"]:
                raise ValueError("Protected workflow revision changed")
            evidence = verify(
                observer,
                expected_role=environment["EXPECTED_ROLE"],
                expected_group=environment["EXPECTED_KUBERNETES_GROUP"],
                definition_revision=environment["DEFINITION_REVISION"],
                run_id=environment["GITHUB_RUN_ID"],
                run_attempt=environment["GITHUB_RUN_ATTEMPT"],
            )
        except Exception:
            # Configuration/provider responses must never become workflow log output.
            pass
        write_evidence(path, evidence)
    except Exception:
        print("Protected tools authorization proof could not produce private evidence")
        return 2
    print("Protected tools authorization proof " + evidence["status"])
    return 0 if evidence["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
