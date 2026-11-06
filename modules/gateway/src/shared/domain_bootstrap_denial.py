"""Closed evidence for a denied first grant; distinct from an absent journal."""

import hashlib
import json
import re

KIND = "bootstrap-denied-first-grant-v1"
SOURCE_IMAGE = "sha256:f0870380df7e74dabcd61e704b85e69cbabe6e2e821114c262d73c7498ebc2d4"
SOURCE_HASHES = {
    "workspace_bootstrap/superplane_bootstrap/eks_grants.py": "7002828b7ffba523441a72316e84cf3a538da5ac61ed0c8c8eb3abe8c20462f8",
    "workspace_bootstrap/superplane_bootstrap/grant_plan.py": "74cf8985a01f66622e29bbb48cc8d1f4b397f111775653743d9f974e781e8e4d",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(value):
    if not value:
        raise ValueError("denied bootstrap first grant is unproven")


def journal_identity(row, *, complete):
    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    expected = {"phase": "revoked" if complete else "revoking", "registrar-entry": {"phase": "revoked", "identity": None}}
    if complete:
        expected["complete"] = True
    require(row["revoked"] is complete and progress == expected)
    require(re.fullmatch(r"[a-f0-9]{64}", row["generation"]) and re.fullmatch(r"[a-f0-9]{64}", row["claim"]))
    require(hashlib.sha256((row["operation_id"] + ":" + row["claim"]).encode()).hexdigest() == row["generation"])
    grants = plan.get("grants", [])
    require(plan.get("version") == 1 and len(grants) > 1)
    first = grants[0]
    stem = "sp-bootstrap-" + row["generation"][:24]
    require(
        first
        == {
            "key": "registrar-entry",
            "kind": "eks-entry",
            "actor": "registrar",
            "cluster_arn": row["cluster_arn"],
            "generation": row["generation"],
            "principal_arn": first.get("principal_arn"),
            "groups": [stem + ":registrar"],
            "username": stem + ":registrar:{{SessionName}}",
            "client_token": hashlib.sha256((row["generation"] + ":registrar").encode()).hexdigest(),
            "lifetime": "temporary",
        }
        and isinstance(first["principal_arn"], str)
        and len({g.get("key") for g in grants}) == len(grants)
        and all(g.get("generation") == row["generation"] and g.get("cluster_arn") == row["cluster_arn"] for g in grants)
    )
    return {
        **{k: row[k] for k in ("operation_id", "org_id", "workspace_id", "cluster_arn", "generation", "claim")},
        "plan_sha256": digest(plan),
        "first_grant": first,
    }


def validate_denial(value):
    require(value.get("evidence_kind") == KIND)
    require(value.get("source_image", "").rsplit("@", 1)[-1] == SOURCE_IMAGE)
    require(value.get("denial_source_hashes") == SOURCE_HASHES)
    denial = value.get("denied_grant", {})
    require(all(denial.get(k) == value.get(k) for k in ("operation_id", "org_id", "workspace_id")))
    require(denial.get("denied_request_verified") is True and denial.get("absence_verified") is True)
    require(isinstance(denial.get("event_ids"), list) and len(denial["event_ids"]) == 1 and denial["event_ids"][0])
    for key in ("generation", "claim", "plan_sha256", "cloudtrail_sha256", "inventory_sha256"):
        require(isinstance(denial.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", denial[key]))
    require(denial.get("first_grant", {}).get("client_token"))
    return denial


def match_revoked(row, evidence):
    denial = validate_denial(evidence)
    actual = journal_identity(row, complete=True)
    require(all(denial.get(k) == v for k, v in actual.items()))
    return digest(actual)
