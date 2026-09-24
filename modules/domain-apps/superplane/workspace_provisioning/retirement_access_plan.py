"""Finite temporary access for adopted retirement, in its own allocation.

The registrar receives namespace administrator authority, broader than the
cleaner's named-object deletion rules. Both scopes are review facts. Grants
remain inventoried until exact final revocation; this module grants no access.
"""

from dataclasses import asdict, dataclass
import json
import re
import uuid

from superplane_bootstrap.grant_plan import RBAC, rule
from superplane_bootstrap.kube_grants import GENERATION_ANNOTATION

from .artifacts import digest
from .retirement_plan import DELETE_COMPONENT, REVOKE_GRANT, compose_retirement_plan
from .runtime_config import LifecycleRefused, validate_runtime_config

PHASE = "prepare-retirement-access"
ADMIN_POLICY = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy"
KINDS = {
    "ServiceAccount": ("", "serviceaccounts"),
    "Deployment": ("apps", "deployments"),
    "Role": (RBAC, "roles"),
    "RoleBinding": (RBAC, "rolebindings"),
}


def access_identity(
    org_id, workspace_id, original_allocation_id, retirement_request_id
):
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 255
        for value in (org_id, workspace_id, original_allocation_id)
    ):
        raise LifecycleRefused("cleanup access requires the original allocation scope")
    try:
        request_id = str(uuid.UUID(str(retirement_request_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LifecycleRefused("retirement request identity must be a UUID") from exc
    scope = json.dumps([org_id, workspace_id, original_allocation_id, request_id])
    request = str(
        uuid.uuid5(uuid.NAMESPACE_URL, "superplane-retirement-access-request:" + scope)
    )
    allocation = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, "superplane-retirement-access-allocation:" + scope
        )
    )
    if allocation == original_allocation_id:
        raise LifecycleRefused("cleanup access cannot use the original allocation")
    return request, allocation


@dataclass(frozen=True)
class RetirementAccessPlan:
    request_id: str
    allocation_id: str
    original_allocation_id: str
    retirement_request_id: str
    org_id: str
    workspace_id: str
    cluster_arn: str
    namespace_uid: str
    inventory_sha256: str
    runtime_config_sha256: str
    generation: str
    registrar_namespaces: tuple[str, ...]
    owned_objects: tuple[dict, ...]
    grants: tuple[dict, ...]
    revocation_order: tuple[str, ...]

    @property
    def revision(self):
        return digest(asdict(self))

    def recipe(self):
        """Exact create descriptors; the worker cannot add an SDK effect."""
        result = {}
        account_id = self.cluster_arn.split(":")[4]
        cluster_name = self.cluster_arn.rsplit("/", 1)[-1]
        for grant in self.grants:
            arguments = {"clusterName": cluster_name}
            if grant["kind"] == "eks-entry":
                method = "create_access_entry"
                arguments.update(
                    principalArn=grant["principal_arn"],
                    type="STANDARD",
                    kubernetesGroups=grant["groups"],
                    username=grant["username"],
                    clientRequestToken=grant["client_token"],
                    tags={
                        "superplane-generation": self.generation,
                        "OrgId": self.org_id,
                        "WorkspaceId": self.workspace_id,
                    },
                )
            elif grant["kind"] == "eks-policy":
                method = "associate_access_policy"
                arguments.update(
                    principalArn=grant["principal_arn"],
                    policyArn=grant["policy_arn"],
                    accessScope=grant["scope"],
                )
            else:
                method = "create"
                arguments = {
                    "body": grant["body"],
                    "namespace": grant["body"]["metadata"]["namespace"],
                }
            result[grant["key"]] = {
                "service": "kubernetes" if grant["kind"] == "kubernetes" else "eks",
                "method": method,
                "account_id": account_id,
                "arguments": arguments,
            }
        return result


def compile_access_plan(
    inventory, runtime, *, original_allocation_id, retirement_request_id
):
    runtime = validate_runtime_config(runtime)
    deletion = compose_retirement_plan(inventory)
    if deletion.cluster_rbac_remaining:
        raise LifecycleRefused(
            "retained cluster RBAC requires independently provisioned exact-name cleanup authority"
        )
    if not deletion.completes_teardown:
        raise LifecycleRefused(
            "cleanup access requires complete adopted ownership without namespace destruction"
        )
    if inventory.namespace != runtime["namespace"]:
        raise LifecycleRefused("cleanup namespace differs from the approved runtime")
    if not re.fullmatch(
        r"arn:aws:eks:[a-z0-9-]+:[0-9]{12}:cluster/[A-Za-z0-9][A-Za-z0-9_-]*",
        inventory.cluster_arn,
    ):
        raise LifecycleRefused("cleanup cluster identity is invalid")
    request_id, allocation_id = access_identity(
        inventory.org_id,
        inventory.workspace_id,
        original_allocation_id,
        retirement_request_id,
    )
    inventory_sha = digest(asdict(inventory))
    generation = digest(
        {
            "allocation_id": allocation_id,
            "inventory_sha256": inventory_sha,
            "runtime_config_sha256": digest(runtime),
        }
    )
    stem = "sp-retire-" + generation[:24]
    names = {inventory.namespace: {}}
    owned = []
    for step in deletion.steps:
        if (
            step.operation_kind not in {DELETE_COMPONENT, REVOKE_GRANT}
            or step.provider != "superplane-kubernetes"
        ):
            continue
        descriptor = json.loads(step.target)
        kind, namespace, name, uid = (
            descriptor.get(key) for key in ("kind", "namespace", "name", "uid")
        )
        if (
            kind not in KINDS
            or namespace not in {inventory.namespace, "kube-system"}
            or not name
            or not uid
        ):
            raise LifecycleRefused(
                "cleanup access encountered unsupported namespaced ownership"
            )
        names.setdefault(namespace, {}).setdefault(KINDS[kind], set()).add(name)
        owned.append({"kind": kind, "namespace": namespace, "name": name, "uid": uid})
    namespaces = tuple(sorted(names))
    account_id = inventory.cluster_arn.split(":")[4]
    roles = runtime["actor_role_names"]
    grants = []

    def entry(actor, role_name):
        grant = {
            "key": actor + "-entry",
            "kind": "eks-entry",
            "actor": actor,
            "cluster_arn": inventory.cluster_arn,
            "generation": generation,
            "principal_arn": f"arn:aws:iam::{account_id}:role/{role_name}",
            "groups": [stem + ":" + actor],
            "username": stem + ":" + actor + ":{{SessionName}}",
            "client_token": digest({"generation": generation, "actor": actor}),
            "lifetime": "retirement",
        }
        grants.append(grant)
        return grant

    registrar = entry("registrar", roles["registrar"])
    grants.append(
        {
            **registrar,
            "key": "registrar-policy",
            "kind": "eks-policy",
            "policy_arn": ADMIN_POLICY,
            "scope": {"type": "namespace", "namespaces": list(namespaces)},
        }
    )
    entry("cleaner", roles["installer"])
    role_keys, binding_keys = [], []
    for index, namespace in enumerate(namespaces):
        name = stem + "-" + str(index)
        rules = [
            rule(group, [resource], ["get", "delete"], sorted(resources))
            for (group, resource), resources in sorted(names[namespace].items())
        ]
        if namespace == inventory.namespace:
            # The maintained drain observes these governed objects without
            # deleting by label. Secret listing is real namespace read authority
            # and is included visibly in the human-reviewed recipe.
            rules.extend(
                [
                    rule("", ["pods", "services", "secrets"], ["list"]),
                    rule("apps", ["deployments"], ["list"]),
                    rule("batch", ["jobs"], ["list"]),
                ]
            )
        for kind, contents in (
            ("Role", {"rules": rules}),
            (
                "RoleBinding",
                {
                    "roleRef": {"apiGroup": RBAC, "kind": "Role", "name": name},
                    "subjects": [
                        {"apiGroup": RBAC, "kind": "Group", "name": stem + ":cleaner"}
                    ],
                },
            ),
        ):
            key = f"cleaner-{index}-{kind.lower()}"
            grants.append(
                {
                    "key": key,
                    "kind": "kubernetes",
                    "actor": "registrar",
                    "cluster_arn": inventory.cluster_arn,
                    "generation": generation,
                    "lifetime": "retirement",
                    "body": {
                        "apiVersion": RBAC + "/v1",
                        "kind": kind,
                        "metadata": {
                            "name": name,
                            "namespace": namespace,
                            "annotations": {GENERATION_ANNOTATION: generation},
                        },
                        **contents,
                    },
                }
            )
            (role_keys if kind == "Role" else binding_keys).append(key)
    # Registrar stays live until it can delete/observe the cleaner RBAC. Its
    # final AWS revocation does not require Kubernetes authority.
    revocation = (
        tuple(reversed(binding_keys))
        + tuple(reversed(role_keys))
        + ("cleaner-entry", "registrar-policy", "registrar-entry")
    )
    return RetirementAccessPlan(
        request_id,
        allocation_id,
        original_allocation_id,
        str(uuid.UUID(str(retirement_request_id))),
        inventory.org_id,
        inventory.workspace_id,
        inventory.cluster_arn,
        inventory.namespace_uid,
        inventory_sha,
        digest(runtime),
        generation,
        namespaces,
        tuple(owned),
        tuple(grants),
        revocation,
    )
