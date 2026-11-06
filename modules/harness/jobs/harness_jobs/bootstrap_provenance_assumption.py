"""One explicitly accepted provenance gap; never proof of executed stdin.

Only digests of the privately reviewed inputs are packaged. The original human
recovery decision must carry this same distinction through its immutable proof.
This contract cannot authorize execution, cleanup, a budget, or another attempt.
"""

import hashlib
import json

APPROVAL_SHA256 = "a0b3e7da123fd023aeb581d1501939ce6488cbc7349693e734c7ec66f5c01ba4"
PROPOSAL_SHA256 = "dee666949bbedfa3d04b034ab05b06c67505408cc61428377b7f05c84ae5ddf7"
MANIFEST_SHA256 = "65bbf8c5b456aeb3294ab03817cc7ace6e396058621a34bdc15a1af4e8f00466"
CLOSURE_SHA256 = "c9dd5ce4a8c104090f3e9e01552ef4c54db47633e05d3ba8ad9118bf4c5b431b"
SOURCE_BINDING_SHA256 = (
    "ae451fc1d0d8a301c3be4d0d1c1eb4be3af4864f95b21e3046ad43960bef0e41"
)
JOURNAL_BINDING_SHA256 = (
    "c601436301771bef6fd059cc62a1881417b38da2b583e70ed4cc7f5bab4fcc9d"
)
CLOSED_JOURNAL_SHA256 = (
    "e872ea7a72e8b3dd2a9bc89e88a7c7bd07dcbfc2c9acc09fd85bb9174d2e204e"
)
KIND = "approved-two-session-provenance-assumption-v1"
FIELD = "provenance_assumption"
SOURCE_FIELDS = (
    "operation_id",
    "workspace_id",
    "source_pod_name",
    "source_pod_uid",
    "source_image",
)
JOURNAL_FIELDS = (
    "generation",
    "claim",
    "operation_id",
    "org_id",
    "workspace_id",
    "cluster_arn",
    "plan_sha256",
)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def require(value):
    if not value:
        raise ValueError("approved diagnostic assumption differs from reviewed inputs")


def metadata(closure_sha256):
    if closure_sha256 != CLOSURE_SHA256:
        return None
    return {
        "kind": KIND,
        "classification": "accepted-operator-attested-stdin-assumption",
        "executed_stdin_verified": False,
        "approval_sha256": APPROVAL_SHA256,
        "proposal_sha256": PROPOSAL_SHA256,
        "manifest_sha256": MANIFEST_SHA256,
        "closure_sha256": CLOSURE_SHA256,
        "source_binding_sha256": SOURCE_BINDING_SHA256,
        "journal_binding_sha256": JOURNAL_BINDING_SHA256,
        "closed_source_journal_sha256": CLOSED_JOURNAL_SHA256,
    }


def validate_review(terms):
    expected = metadata(terms["closure_sha256"])
    require(terms.get(FIELD) == expected)
    require((FIELD in terms) == (expected is not None))
    return expected


def validate_owner(value):
    expected = metadata(digest(value.get("settled_bootstrap")))
    binding = digest({k: value.get(k) for k in SOURCE_FIELDS})
    require(binding != SOURCE_BINDING_SHA256 or expected is not None)
    require(value.get(FIELD) == expected)
    require((FIELD in value) == (expected is not None))
    require(value.get("source_audit", {}).get(FIELD) == expected)
    if expected is None:
        return None
    require(value["evidence_kind"] == "bootstrap-reader-components-revoked-v1")
    require(binding == SOURCE_BINDING_SHA256)
    revoked = value["revoked_bootstrap"]
    current = [
        j
        for j in revoked["journals"]
        if j["generation"] == revoked["current_generation"]
    ]
    require(len(current) == 1)
    require(
        digest({k: current[0][k] for k in JOURNAL_FIELDS}) == JOURNAL_BINDING_SHA256
    )
    return expected


def validate_proof(proof):
    expected = validate_owner(proof["owner_evidence"])
    require(proof.get(FIELD) == expected)
    require((FIELD in proof) == (expected is not None))
    return expected
