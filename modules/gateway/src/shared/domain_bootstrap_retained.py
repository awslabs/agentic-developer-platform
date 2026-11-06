"""A later unstarted attempt retains its predecessor's revoked journal."""

import re

KIND = "bootstrap-retained-revocation-barrier-v1"


def validate_retained(value):
    retained = value.get("retained_revocation", {})
    journal = retained.get("journal", {})
    if (
        value.get("evidence_kind") != KIND
        or "denied_grant" in value
        or "denial_source_hashes" in value
        or set(retained) != {"reauthorization_id", "source_fence", "proof_sha256", "journal"}
        or not isinstance(retained["reauthorization_id"], str)
        or not retained["reauthorization_id"]
        or type(retained["source_fence"]) is not int
        or not 0 < retained["source_fence"] < value["source_fence"]
        or not re.fullmatch(r"[a-f0-9]{64}", retained.get("proof_sha256", ""))
        or any(journal.get(k) != value[k] for k in ("operation_id", "org_id", "workspace_id"))
        or any(not re.fullmatch(r"[a-f0-9]{64}", journal.get(k, "")) for k in ("generation", "claim", "plan_sha256"))
        or not journal.get("cluster_arn")
    ):
        raise ValueError("prior bootstrap revocation is unproven")
    return retained
