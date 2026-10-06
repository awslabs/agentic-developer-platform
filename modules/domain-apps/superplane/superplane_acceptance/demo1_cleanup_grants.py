"""Observe the recorded cleanup EKS entry without acquiring mutation authority."""

import re
from types import SimpleNamespace

from superplane_bootstrap.eks_grants import EksGrants
from superplane_bootstrap.errors import BootstrapRefused

from workspace_provisioning.artifacts import digest

from .demo1_evidence import EvidenceError
from .demo1_report import reference


def require(condition):
    if not condition:
        raise EvidenceError(
            "cleanup grants: current EKS authority unavailable or changed"
        )


def observe_grants(reader, selected, grants, recorded_digest):
    try:
        return _observe(reader, selected, grants, recorded_digest)
    except (BootstrapRefused, AttributeError, KeyError, TypeError, ValueError):
        raise EvidenceError(
            "cleanup grants: current EKS authority unavailable or changed"
        ) from None


def _observe(reader, selected, grants, recorded_digest):
    require(
        all(
            getattr(reader, actual) == getattr(selected, expected)
            for actual, expected in (
                ("connection_id", "connection_id"),
                ("account", "account"),
                ("region", "region"),
                ("role_name", "role"),
            )
        )
        and isinstance(grants, list)
        and len(grants) == 1
        and digest(grants) == recorded_digest
        and isinstance(grants[0], dict)
        and set(grants[0]) == {"spec", "identity"}
    )
    spec, identity = grants[0]["spec"], grants[0]["identity"]
    require(
        isinstance(spec, dict)
        and spec.get("key") == "cleaner-entry"
        and spec.get("kind") == "eks-entry"
        and isinstance(identity, dict)
        and set(identity) == {"arn", "generation", "groups", "username"}
    )
    cluster = re.fullmatch(
        rf"arn:aws:eks:{re.escape(selected.region)}:{selected.account}:cluster/([A-Za-z0-9][A-Za-z0-9_-]{{0,99}})",
        spec["cluster_arn"],
    )
    require(
        cluster is not None
        and re.fullmatch(
            rf"arn:aws:iam::{selected.account}:role/[A-Za-z0-9+=,.@_/-]+",
            spec["principal_arn"],
        )
        and re.fullmatch(r"[a-f0-9]{64}", spec["generation"])
    )
    target = SimpleNamespace(
        account_id=selected.account,
        cluster_arn=spec["cluster_arn"],
        cluster_name=cluster[1],
    )

    def read(operation):
        require(
            selected.authorized_at <= reader.clock() < selected.deadline
            and reader._identity()
            and selected.authorized_at <= reader.clock() < selected.deadline
        )
        code, value, _ = reader._execute(
            "eks",
            operation,
            "--cluster-name",
            target.cluster_name,
            "--principal-arn",
            spec["principal_arn"],
            "--region",
            selected.region,
            "--no-paginate",
        )
        require(
            selected.authorized_at <= reader.clock() < selected.deadline
            and code == 0
            and isinstance(value, dict)
        )
        return value

    class ReadOnlyEntries:
        def describe_access_entry(self, **arguments):
            require(
                arguments
                == {
                    "clusterName": target.cluster_name,
                    "principalArn": spec["principal_arn"],
                }
            )
            return read("describe-access-entry")

    validator = EksGrants(ReadOnlyEntries(), target, entry_client=None)
    validator.verify(spec, identity)
    require(validator.observe(spec) == identity)
    policies = read("list-associated-access-policies")
    require(
        policies.get("associatedAccessPolicies") == []
        and policies.get("nextToken") in (None, "")
        and policies.get("clusterName") == target.cluster_name
        and policies.get("principalArn") == spec["principal_arn"]
    )
    require(validator.observe(spec) == identity)
    return {
        "status": "OBSERVED",
        "scope": "current cleanup EKS entry only; Kubernetes grants, fence, inventory and deletion authority unverified",
        "grant_set_ref": reference(recorded_digest),
        "observed_at": reader.clock().isoformat(),
        "grant_count": 1,
        "associated_policy_count": 0,
    }
