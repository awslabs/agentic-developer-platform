"""Resolve EKS's non-RBAC rules-report gap only for an authenticated native SA.

The EKS access authorizer grants IAM principals, not Kubernetes ServiceAccounts.
Its lack of RulesFor support makes SSRR incomplete even for an authenticated SA.
An exact managed-EKS endpoint, native SA UID/groups and the journalled identity
are required before treating that one diagnostic as irrelevant to this subject.
The reported RBAC rules still have to pass the unchanged strict reader allowlist.
"""

import re
from urllib.parse import urlsplit
from uuid import UUID

from .renewal import readonly

EKS_RULES_ERROR = "webhook authorizer does not support user rule resolution"


def native_eks_service_account(target, user):
    try:
        arn = re.fullmatch(
            r"arn:(aws|aws-us-gov|aws-cn):eks:([a-z0-9-]+):[0-9]{12}:cluster/[A-Za-z0-9][A-Za-z0-9_-]{0,99}",
            target["cluster_arn"],
        )
        endpoint = urlsplit(target["endpoint"])
        suffix = (
            ".eks.amazonaws.com.cn"
            if arn and arn[1] == "aws-cn"
            else ".eks.amazonaws.com"
        )
        namespace = target["namespace"]
        uid = target["service_account_uid"]
        return bool(
            arn
            and endpoint.scheme == "https"
            and endpoint.hostname
            and endpoint.hostname.endswith("." + arn[2] + suffix)
            and not endpoint.username
            and not endpoint.password
            and not endpoint.port
            and endpoint.path in ("", "/")
            and not endpoint.query
            and not endpoint.fragment
            and str(UUID(uid)) == uid
            and user.uid == uid
            and target["service_account"] == "superplane-controller"
            and user.username
            == "system:serviceaccount:" + namespace + ":superplane-controller"
            and len(user.groups or []) == 3
            and set(user.groups)
            == {
                "system:authenticated",
                "system:serviceaccounts",
                "system:serviceaccounts:" + namespace,
            }
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def known_eks_rules_gap(status):
    return (
        status.get("incomplete") is True
        and status.get("evaluationError") == EKS_RULES_ERROR
    )


def native_eks_reader_rules(status, target, user):
    if not known_eks_rules_gap(status) or not native_eks_service_account(target, user):
        return False
    return readonly({**status, "incomplete": False, "evaluationError": ""})
