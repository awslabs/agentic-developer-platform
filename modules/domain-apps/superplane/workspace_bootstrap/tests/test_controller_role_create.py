"""Role collection POST checks held RBAC rules, not a body-named escalate SSAR.

Kubernetes v1.35 requestinfo.go leaves RequestInfo.Name empty on collection POST.
RoleEscalationAuthorized consequently checks unnamed escalate, then policybased
Role/ClusterRole storage falls back to ConfirmNoEscalationInternal. Exercise that
fallback beneath the production component generator and kubectl create adapter.
"""

import json
from copy import deepcopy
from itertools import product
from types import SimpleNamespace

import pytest

from superplane_bootstrap.adapters import CommandResult
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.grant_plan import BootstrapRelease, compile_grants

from .conftest import (
    ACCOUNT_ID,
    CLUSTER_ARN,
    CONTROLLER_NAME,
    ENFORCE_VERSION,
    NAMESPACE,
    REGION,
    WORKSPACE_CRDS,
    WORKSPACE_ID,
)
from .test_adapters import _access
from .test_authority_runtime_postgres import Cloud


RBAC = ".rbac.authorization.k8s.io"
MISSING_READS = (
    ("Role", "", "pods/log", "get"),
    ("Role", "apps", "replicasets", "list"),
    ("Role", "batch", "jobs", "get"),
    ("Role", "superplane.ai", "superplanenodes", "list"),
    ("Role", "superplane.ai", "superplanenodes", "watch"),
    ("ClusterRole", "superplane.ai", "nodepools", "list"),
    ("ClusterRole", "superplane.ai", "nodepools", "watch"),
)


class RbacCollectionApi:
    def __init__(self, cloud, principal):
        self.cloud, self.principal = cloud, principal
        self.created = []
        self.missing = []
        self.request_names = []

    def run(self, argv, *, data=None, timeout=120):
        assert "create" in argv and "-f" in argv
        body = json.loads(data)
        resource = {"Role": "roles", "ClusterRole": "clusterroles"}[body["kind"]]
        namespace = body["metadata"].get("namespace")
        # URL /roles or /clusterroles is a collection: body.name is not Name.
        request_name = None
        self.request_names.append(request_name)
        arguments = dict(
            resource=resource + RBAC, namespace=namespace, name=request_name
        )
        allowed = self.cloud.allowed(self.principal, verb="create", **arguments)
        if allowed and not self.cloud.allowed(
            self.principal, verb="escalate", **arguments
        ):
            groups = self.cloud.entries[self.principal]["kubernetesGroups"]
            for rule in body["rules"]:
                for group, resource, verb, name in product(
                    rule["apiGroups"],
                    rule["resources"],
                    rule["verbs"],
                    rule.get("resourceNames") or [None],
                ):
                    # Explicit groups select RBAC rule resolution, excluding
                    # ordinary additive EKS authorizer allows.
                    if not self.cloud.allowed(
                        self.principal,
                        verb=verb,
                        resource=resource + ("." + group if group else ""),
                        namespace=namespace,
                        name=name,
                        groups=groups,
                    ):
                        self.missing.append((group, resource, verb, name))
            allowed = not self.missing
        if not allowed:
            return CommandResult(
                tuple(argv),
                1,
                stderr="attempting to grant RBAC permissions not currently held",
            )
        self.created.append(deepcopy(body))
        body["metadata"]["uid"] = "synthetic-created-role"
        return CommandResult(tuple(argv), 0, stdout=json.dumps(body))


@pytest.fixture(params=("management", "legacy"))
def installer(request):
    target = SimpleNamespace(
        account_id=ACCOUNT_ID,
        region=REGION,
        cluster_arn=CLUSTER_ARN,
        workspace_id=WORKSPACE_ID,
    )
    journal = SimpleNamespace(target=target, generation="a" * 64)
    principals = {
        actor: f"arn:aws:iam::{ACCOUNT_ID}:role/{actor}"
        for actor in ("registrar", "installer", "supervisor")
    }
    release = BootstrapRelease(
        NAMESPACE, CONTROLLER_NAME, CONTROLLER_NAME, ENFORCE_VERSION, WORKSPACE_CRDS
    )
    plan = compile_grants(journal, release, principals, controller_mode=request.param)
    cloud = Cloud(target, SimpleNamespace())
    for grant in plan["grants"]:
        if grant["actor"] != "installer":
            continue
        assert grant["lifetime"] == "temporary"
        if grant["kind"] == "eks-entry":
            cloud.entries[principals["installer"]] = {
                "kubernetesGroups": grant["groups"]
            }
            cloud.policies[principals["installer"]] = []
        elif grant["kind"] == "kubernetes":
            body = deepcopy(grant["body"])
            cloud.objects[
                (
                    body["kind"],
                    body["metadata"].get("namespace"),
                    body["metadata"]["name"],
                )
            ] = body
        else:
            pytest.fail("installer unexpectedly received an EKS administration policy")
    api = RbacCollectionApi(cloud, principals["installer"])
    bodies = []
    access = _access(api, component_journal=SimpleNamespace(ensure=bodies.append))
    access.establish_controller_rbac(NAMESPACE, CONTROLLER_NAME)
    return SimpleNamespace(
        cloud=cloud,
        api=api,
        access=access,
        principal=principals["installer"],
        roles={
            body["kind"]: body
            for body in bodies
            if body["kind"] in {"Role", "ClusterRole"}
        },
    )


@pytest.mark.parametrize("kind", ("Role", "ClusterRole"))
def test_collection_create_uses_held_controller_rules_in_both_scopes(installer, kind):
    body = installer.roles[kind]
    scope = body["metadata"].get("namespace")
    resource = ("roles" if kind == "Role" else "clusterroles") + RBAC
    assert installer.cloud.allowed(
        installer.principal,
        verb="escalate",
        resource=resource,
        namespace=scope,
        name=body["metadata"]["name"],
    )
    assert not installer.cloud.allowed(
        installer.principal, verb="escalate", resource=resource, namespace=scope
    )
    assert (
        installer.access.create_component(body)["metadata"]["uid"]
        == "synthetic-created-role"
    )
    assert installer.api.request_names == [None]
    assert installer.api.created == [body]


@pytest.mark.parametrize("kind,group,resource,verb", MISSING_READS)
def test_each_missing_held_rule_refuses_create_despite_named_escalate(
    installer, kind, group, resource, verb
):
    for role in installer.cloud.objects.values():
        for rule in role.get("rules", []):
            if (
                group in rule["apiGroups"]
                and resource in rule["resources"]
                and verb in rule["verbs"]
            ):
                rule["verbs"].remove(verb)
    body = installer.roles[kind]
    assert installer.cloud.allowed(
        installer.principal,
        verb="escalate",
        resource=("roles" if kind == "Role" else "clusterroles") + RBAC,
        namespace=body["metadata"].get("namespace"),
        name=body["metadata"]["name"],
    )
    with pytest.raises(BootstrapRefused, match="creating bootstrap component"):
        installer.access.create_component(body)
    assert (group, resource, verb, None) in installer.api.missing
    assert not installer.api.created


def test_named_pod_log_access_does_not_cover_controller_unrestricted_names(installer):
    for role in installer.cloud.objects.values():
        for rule in role.get("rules", []):
            if rule["resources"] == ["pods/log"]:
                rule["resourceNames"] = ["imds-probe-ipv4", "imds-probe-ipv6"]
    with pytest.raises(BootstrapRefused):
        installer.access.create_component(installer.roles["Role"])
    assert ("", "pods/log", "get", None) in installer.api.missing


@pytest.mark.parametrize(
    "change",
    (
        "secrets",
        "write",
        "wildcard",
        "foreign-namespace",
        "cluster-scope",
        "all-namespaces",
    ),
)
def test_create_cannot_grant_broader_controller_permissions(installer, change):
    body = deepcopy(
        installer.roles["ClusterRole" if change == "all-namespaces" else "Role"]
    )
    if change == "foreign-namespace":
        body["metadata"]["namespace"] = "foreign-workspace"
    elif change == "cluster-scope":
        body["kind"] = "ClusterRole"
        body["metadata"].pop("namespace")
    elif change == "all-namespaces":
        for rule in body["rules"]:
            rule.pop("resourceNames", None)
    else:
        body["rules"].append(
            {
                "apiGroups": [""],
                "resources": [
                    "secrets"
                    if change == "secrets"
                    else "*"
                    if change == "wildcard"
                    else "pods"
                ],
                "verbs": ["delete" if change == "write" else "get"],
            }
        )
    with pytest.raises(BootstrapRefused):
        installer.access.create_component(body)
    assert not installer.api.created
