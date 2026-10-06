"""Closed review contract for a separate, disabled domain runtime installation."""

from __future__ import annotations

import re

from .api_adapters import closed
from .config import EKS_NAME, IDENTIFIER, digest, identity, require


def names(env):
    prefix = f"adp-{env['environment']}-superplane-domain"
    return {
        "queue": prefix + "-operations",
        "worker_role": prefix + "-worker",
        "observer_role": prefix + "-observer",
    }


def validate_request(request):
    closed(request, {"account_id", "region", "environment", "cluster", "namespace", "operator_role_arn"}, "runtime preparation request")
    require(
        isinstance(request["account_id"], str)
        and re.fullmatch(r"[0-9]{12}", request["account_id"])
        and isinstance(request["region"], str)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[1-9][0-9]*", request["region"])
        and isinstance(request["environment"], str)
        and IDENTIFIER.fullmatch(request["environment"])
        and len(request["environment"]) <= 31
        and isinstance(request["cluster"], str)
        and EKS_NAME.fullmatch(request["cluster"])
        and isinstance(request["namespace"], str)
        and IDENTIFIER.fullmatch(request["namespace"]),
        "Runtime preparation requires exact account, region, environment, cluster and namespace",
    )
    require(
        isinstance(request["operator_role_arn"], str)
        and re.fullmatch(rf"arn:aws:iam::{request['account_id']}:role/[A-Za-z0-9+=,.@_-]{{1,64}}", request["operator_role_arn"]),
        "Runtime preparation requires an explicit same-account operator role",
    )


def review(request, inspector):
    """Read target identities and inventory; never reserve a name or activate a worker."""
    validate_request(request)
    account, region = request["account_id"], request["region"]
    target = inspector.json(inspector.aws("sts", "get-caller-identity"))
    role = request["operator_role_arn"].split(":role/", 1)[1]
    require(
        target.get("Account") == account
        and target.get("Arn", "").startswith(f"arn:aws:sts::{account}:assumed-role/{role}/"),
        "Runtime preparation operator account or role differs from the reviewed target",
    )
    cluster = inspector.json(inspector.aws("eks", "describe-cluster", "--name", request["cluster"]))["cluster"]
    require(
        cluster.get("arn") == f"arn:aws:eks:{region}:{account}:cluster/{request['cluster']}"
        and cluster.get("status") == "ACTIVE"
        and re.fullmatch(rf"https://oidc\.eks\.{re.escape(region)}\.amazonaws\.com/id/[A-Za-z0-9]+", cluster.get("identity", {}).get("oidc", {}).get("issuer", "")),
        "Runtime preparation requires the selected active cluster and OIDC identity",
    )
    resource_names = names(request)
    queue_urls = inspector.json(inspector.aws("sqs", "list-queues", "--queue-name-prefix", resource_names["queue"])).get("QueueUrls", [])
    require(
        isinstance(queue_urls, list) and not queue_urls,
        "Runtime preparation queue name already exists or inventory is ambiguous; do not adopt it",
    )
    roles = inspector.json(inspector.aws("iam", "list-roles"))["Roles"]
    require(
        isinstance(roles, list)
        and all(isinstance(existing, dict) and existing.get("RoleName") not in (resource_names["worker_role"], resource_names["observer_role"]) for existing in roles),
        "Runtime preparation role name already exists or inventory is ambiguous; do not adopt it",
    )
    resources = {
        "queue_name": resource_names["queue"],
        "queue_arn": f"arn:aws:sqs:{region}:{account}:{resource_names['queue']}",
        "queue_url": f"https://sqs.{region}.amazonaws.com/{account}/{resource_names['queue']}",
        "worker_role_arn": f"arn:aws:iam::{account}:role/{resource_names['worker_role']}",
        "observer_role_arn": f"arn:aws:iam::{account}:role/{resource_names['observer_role']}",
    }
    return {
        "version": 1,
        "status": "review-only",
        "installation_id": identity(request),
        "request_sha256": digest(request),
        "account_id": account,
        "region": region,
        "cluster_arn": cluster["arn"],
        "oidc_issuer": cluster["identity"]["oidc"]["issuer"],
        "resources": resources,
        "worker_ready": False,
    }
