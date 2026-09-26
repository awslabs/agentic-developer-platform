"""A member gate enforced by an explicitly installed cluster admission policy.

Only the namespace label is mutated by workspace bootstrap. Policy installation
and the issuer's EKS entry belong to the platform/cluster lifecycle.
"""

from dataclasses import dataclass
import json

from .components import BOOTSTRAP_OWNER_LABEL
from .errors import BootstrapRefused
from .kube_grants import KubeGrants, _payload

GATE_LABEL = "superplane.aws-e/member-admission"
POLICY_NAME = "superplane-member-admission"


def policy_documents(issuer_group):
    """Exact platform installation documents; this module never applies them."""
    if not isinstance(issuer_group, str) or not issuer_group.startswith("superplane:"):
        raise BootstrapRefused(
            "member admission requires an explicit platform issuer group"
        )
    return (
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": POLICY_NAME},
            "spec": {
                "failurePolicy": "Fail",
                "matchConstraints": {
                    "matchPolicy": "Equivalent",
                    "resourceRules": [
                        {
                            "apiGroups": [""],
                            "apiVersions": ["v1"],
                            "operations": ["CREATE", "UPDATE"],
                            "resources": ["pods"],
                            "scope": "Namespaced",
                        }
                    ],
                },
                "validations": [
                    {
                        "expression": (
                            "namespaceObject.metadata.labels["
                            + json.dumps(GATE_LABEL)
                            + "] == 'open' || "
                            + json.dumps(issuer_group)
                            + " in request.userInfo.groups"
                        ),
                        "message": "workspace admission is closed",
                    }
                ],
            },
        },
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": POLICY_NAME},
            "spec": {
                "policyName": POLICY_NAME,
                "validationActions": ["Deny"],
                "matchResources": {
                    "matchPolicy": "Equivalent",
                    "namespaceSelector": {
                        "matchExpressions": [
                            {
                                "key": BOOTSTRAP_OWNER_LABEL,
                                "operator": "Exists",
                            },
                            {
                                "key": GATE_LABEL,
                                "operator": "Exists",
                            },
                        ]
                    },
                },
            },
        },
    )


@dataclass(frozen=True)
class ClusterAuthorityReference:
    """Server-held installation identity, supplied by trusted service composition."""

    org_id: str
    cluster_arn: str
    principal_arn: str
    access_entry_arn: str
    username: str
    group: str
    policy_uid: str
    binding_uid: str

    def verify(self, clients, *, recovery=False):
        clients.verify(recovery=recovery)
        if (
            (self.org_id, self.cluster_arn)
            != (clients.target.org_id, clients.target.cluster_arn)
            or clients.principals != {"registrar": self.principal_arn}
            or not all(
                (
                    self.access_entry_arn,
                    self.username,
                    self.policy_uid,
                    self.binding_uid,
                )
            )
        ):
            raise BootstrapRefused(
                "shared issuer differs from registered cluster authority"
            )
        observed = clients.eks.describe_access_entry(
            clusterName=clients.target.cluster_name, principalArn=self.principal_arn
        ).get("accessEntry", {})
        if (
            observed.get("accessEntryArn") != self.access_entry_arn
            or observed.get("principalArn") != self.principal_arn
            or observed.get("username") != self.username
            or observed.get("kubernetesGroups") != [self.group]
            or observed.get("type") != "STANDARD"
        ):
            raise BootstrapRefused("registered cluster issuer access entry changed")


class NamespaceAdmission:
    def __init__(self, grants, reference, namespace, namespace_uid=None):
        if not isinstance(grants, KubeGrants) or not isinstance(
            reference, ClusterAuthorityReference
        ):
            raise BootstrapRefused("member admission requires pinned cluster authority")
        self.grants, self.reference = grants, reference
        self.namespace, self.namespace_uid = namespace, namespace_uid

    def verify_policy(self):
        for desired, uid in zip(
            policy_documents(self.reference.group),
            (self.reference.policy_uid, self.reference.binding_uid),
            strict=True,
        ):
            spec = {"cluster_arn": self.grants.target.cluster_arn, "body": desired}
            actual = self.grants._get(spec)
            if (
                actual is None
                or actual.get("metadata", {}).get("uid") != uid
                or actual.get("spec") != desired["spec"]
            ):
                raise BootstrapRefused("registered member admission policy changed")
            if desired["kind"] == "ValidatingAdmissionPolicy":
                status = actual.get("status", {})
                if (
                    status.get("observedGeneration")
                    != actual["metadata"].get("generation")
                    or not status.get("observedGeneration")
                    or "typeChecking" not in status
                    or status["typeChecking"].get("expressionWarnings")
                ):
                    raise BootstrapRefused(
                        "member admission policy is not type-checked"
                    )

    def namespace_body(self):
        value = self.grants._get(
            {
                "cluster_arn": self.grants.target.cluster_arn,
                "body": {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": self.namespace},
                },
            }
        )
        if (
            value is None
            or not self.namespace_uid
            or value.get("metadata", {}).get("uid") != self.namespace_uid
        ):
            raise BootstrapRefused("member namespace incarnation changed")
        if (
            value["metadata"].get("labels", {}).get(BOOTSTRAP_OWNER_LABEL)
            != self.grants.target.workspace_id
        ):
            raise BootstrapRefused("member namespace ownership changed")
        if value["metadata"].get("labels", {}).get(GATE_LABEL) not in {
            "closed",
            "open",
        }:
            raise BootstrapRefused("member namespace admission label changed")
        return value

    def is_closed(self):
        self.verify_policy()
        return (
            self.namespace_body()["metadata"].get("labels", {}).get(GATE_LABEL)
            == "closed"
        )

    def set_closed(self, closed):
        self.verify_policy()
        body = self.namespace_body()
        metadata = body["metadata"]
        resource_version = metadata.get("resourceVersion")
        if not resource_version:
            raise BootstrapRefused("namespace admission has no mutation precondition")
        desired = "closed" if closed else "open"
        if metadata.get("labels", {}).get(GATE_LABEL) == desired:
            return
        key = GATE_LABEL.replace("/", "~1")
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": self.namespace_uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": resource_version,
            },
            {"op": "add", "path": "/metadata/labels/" + key, "value": desired},
        ]
        self.grants._verify_transport()
        resource = self.grants.client.resources.get(api_version="v1", kind="Namespace")
        _payload(
            resource.patch(
                name=self.namespace,
                body=operations,
                content_type="application/json-patch+json",
            )
        )
        if (
            self.namespace_body()["metadata"].get("labels", {}).get(GATE_LABEL)
            != desired
        ):
            raise BootstrapRefused("namespace admission mutation is unverified")
