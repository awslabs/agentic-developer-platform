"""Dormant, dedicated-cluster retirement fence installed by bootstrap authority.

Retirement can patch only these already-owned names. It never receives CREATE
permission over arbitrary admission policies or adopts an existing policy.
"""

GROUP = "admissionregistration.k8s.io"
WORKLOADS = (
    ("v1", "Pod", "pods"),
    ("v1", "PersistentVolumeClaim", "persistentvolumeclaims"),
    ("v1", "PersistentVolume", "persistentvolumes"),
    ("v1", "Service", "services"),
    ("batch/v1", "Job", "jobs"),
    ("batch/v1", "CronJob", "cronjobs"),
    ("apps/v1", "Deployment", "deployments"),
    ("apps/v1", "ReplicaSet", "replicasets"),
    ("apps/v1", "StatefulSet", "statefulsets"),
    ("apps/v1", "DaemonSet", "daemonsets"),
)


def documents(name, generation, *, active=False):
    metadata = {
        "name": name,
        "annotations": {"superplane.aws-e/authority-generation": generation},
    }
    rules = []
    for group in ("", "apps", "batch"):
        resources = [
            resource
            for version, _kind, resource in WORKLOADS
            if (version.split("/")[0] if "/" in version else "") == group
        ]
        rules.append(
            {
                "apiGroups": [group],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE"],
                "resources": resources,
                "scope": "*",
            }
        )
    return (
        {
            "apiVersion": GROUP + "/v1",
            "kind": "ValidatingAdmissionPolicy",
            "metadata": metadata,
            "spec": {
                "failurePolicy": "Fail",
                "matchConstraints": {
                    "matchPolicy": "Equivalent",
                    "namespaceSelector": {},
                    "objectSelector": {},
                    "resourceRules": rules,
                },
                "validations": [
                    {
                        "expression": "false" if active else "true",
                        "message": "dedicated workspace retirement has closed workload admission",
                    }
                ],
            },
        },
        {
            "apiVersion": GROUP + "/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": metadata,
            "spec": {"policyName": name, "validationActions": ["Deny"]},
        },
    )
