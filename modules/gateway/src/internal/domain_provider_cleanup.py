"""Exact generation-owned bootstrap entry policy; no caller-supplied IAM policy."""

import hashlib
import json

from fastapi import HTTPException

from src.internal.domain_operation_store import domain_connect


async def cleanup_policy(binding, operation, entry_arn):
    if not entry_arn:
        return None, None
    async with domain_connect(binding) as connection:
        rows = await connection.fetch(
            "SELECT a.*, r.state AS reservation_status,r.identity_json,r.attempt_token "
            "FROM workspace_bootstrap_authority a JOIN workspace_bootstrap_reservations r USING(workspace_id) "
            "WHERE a.workspace_id=$1 AND a.org_id=$2 AND a.operation_id=$3 AND NOT a.revoked",
            operation["workspace_id"],
            binding.org_id,
            operation["operation_id"],
        )
    if len(rows) != 1:
        raise HTTPException(403, "owned bootstrap entry unavailable")
    row = rows[0]
    claim = hashlib.sha256(b"superplane-workspace-bootstrap-claim:v1:" + row["attempt_token"].encode()).hexdigest()
    registered = json.loads(row["identity_json"])
    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    if (
        row["reservation_status"] != "reserved"
        or row["claim"] != claim
        or any(registered.get(key) != row[key] for key in ("workspace_id", "org_id", "cluster_arn"))
        or row["generation"] != hashlib.sha256((operation["operation_id"] + ":" + claim).encode()).hexdigest()
        or progress.get("phase") != "revoking"
    ):
        raise HTTPException(403, "owned bootstrap entry changed")
    matches = []
    for spec in plan.get("grants", []):
        prior = progress.get(spec.get("key"), {})
        identity = prior.get("identity", {})
        if (
            spec.get("kind") in {"eks-entry", "eks-policy"}
            and spec.get("generation") == row["generation"]
            and spec.get("cluster_arn") == row["cluster_arn"]
            and identity.get("arn") == entry_arn
            and identity.get("generation") == row["generation"]
            and prior.get("phase") == "revoke_intent"
            and spec.get("lifetime") != "resource"
            and not (spec.get("lifetime") == "workspace" and progress.get("retain_workspace"))
        ):
            matches.append((spec, identity))
    if not matches or len({spec["principal_arn"] for spec, _ in matches}) != 1:
        raise HTTPException(403, "owned bootstrap entry refused")
    cluster = row["cluster_arn"]
    if not entry_arn.startswith(cluster.replace(":cluster/", ":access-entry/") + "/"):
        raise HTTPException(403, "owned bootstrap entry target differs")
    policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "eks:DeleteAccessEntry",
                        "eks:DisassociateAccessPolicy",
                        "eks:DescribeAccessEntry",
                        "eks:ListAssociatedAccessPolicies",
                    ],
                    "Resource": entry_arn,
                }
            ],
        },
        separators=(",", ":"),
    )
    return policy, (row["generation"], cluster, matches[0][0]["principal_arn"], matches[0][1])
