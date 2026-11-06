"""Owner-issued, bounded authority to revoke one already closed bootstrap.

This is not an execution grant or provider enrollment renewal. The owner-only
authority table protects the permit; service progress belongs in the separate
evidence table. No credential, request-selected role or Kubernetes document is
accepted from the service invocation.
"""

import copy
import hashlib
import json
import re
from uuid import UUID
from datetime import datetime, timezone

from .errors import BootstrapRefused

KIND = "closed-bootstrap-cleanup-v1"
PREFIX = "sp-cleanup:"
MAX_SECONDS = 3600
VIEW_POLICY = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminViewPolicy"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(value):
    if not value:
        raise BootstrapRefused("closed bootstrap cleanup authority refused")


def validate(value, *, now=None, fresh=True):
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "kind",
            "permit_id",
            "attempt_id",
            "status",
            "created_at",
            "expires_at",
            "owner_role_arn",
            "owner_role_id",
            "authority",
            "qualification",
            "descriptor",
            "operation",
            "lease",
            "artifact",
            "service_source_sha256",
            "verifier_policy_arn",
            "registrar",
        }
    )
    require(str(UUID(value["attempt_id"])) == value["attempt_id"])
    require(value["kind"] == KIND and value["status"] == "active")
    created, expires = (
        datetime.fromisoformat(value[k]) for k in ("created_at", "expires_at")
    )
    require(created.tzinfo is not None and expires.tzinfo is not None)
    require(900 < (expires - created).total_seconds() <= MAX_SECONDS)
    clock = now or datetime.now(timezone.utc)
    require(not fresh or created <= clock < expires)
    require(value["verifier_policy_arn"] == VIEW_POLICY)
    require(re.fullmatch(r"[0-9a-f]{64}", value["service_source_sha256"]))
    authority, qualified = value["authority"], value["qualification"]
    require(
        authority["status"] == "active"
    )  # An expired enrollment is a pinned baseline, never authority.
    require(qualified["evidence_kind"] == "bootstrap-reader-components-revoked-v1")
    require(
        qualified["original_authority_expired"] is True
        and qualified["issued_sessions_expired"] is True
    )
    require(
        "attempt_extension" not in qualified
        and "attempt_extension" not in value["descriptor"]
    )
    for key in ("workspace_id", "org_id"):
        require(qualified[key] == authority[key] == value["operation"][key])
    require(qualified["operation_id"] == value["operation"]["operation_id"])
    require(value["operation"]["state"] == "unknown")
    lease = value["lease"]
    require(lease["closed_at"] is not None and lease["holder"] is None)
    require(lease["expires_at"] is None and lease["runtime_deadline"] is None)
    require(0 < lease["attempts"] <= lease["max_attempts"])
    require(value["operation"]["budget_state"] == "retained")
    require(value["operation"]["requester"] == authority["subject"])
    require(qualified["settled_bootstrap"] == value["descriptor"]["settled_bootstrap"])
    require(qualified["plan_sha256"] == digest(qualified["retained_plan"]))
    registrar = [
        g["principal_arn"]
        for g in qualified["retained_plan"]["grants"]
        if g["key"] == "registrar-entry"
    ]
    require(registrar == [value["registrar"]["role_arn"]])
    require(set(value["registrar"]) == {"role_arn", "role_id"})
    require(re.fullmatch(r"AROA[A-Z0-9]{17}", value["registrar"]["role_id"]))
    require(value["permit_id"] == PREFIX + binding_digest(value))
    return copy.deepcopy(value)


def binding_digest(value):
    return digest(
        {
            k: v
            for k, v in value.items()
            if k not in {"permit_id", "status", "created_at", "expires_at"}
        }
    )


def entry_spec(permit):
    q, authority = permit["qualification"], permit["authority"]
    # Only an ephemeral read mapping of the already enrolled provider. Original
    # registrar/installer/supervisor entries remain native journal-owned targets.
    return {
        "clusterName": q["cluster_arn"].rsplit("/", 1)[1],
        "principalArn": authority["role_arn"],
        "type": "STANDARD",
        "kubernetesGroups": [],
        "username": "sp-cleanup-view:"
        + permit["permit_id"][len(PREFIX) : 24 + len(PREFIX)]
        + ":{{SessionName}}",
        "tags": {
            "OrgId": authority["org_id"],
            "WorkspaceId": authority["workspace_id"],
            "superplane-cleanup-permit": permit["permit_id"],
            "superplane-generation": q["generation"],
        },
    }


def object_scope(permit):
    qualified = permit["qualification"]
    plan, progress = qualified["retained_plan"], qualified["retained_progress"]
    reads, deletes = {}, {}
    retirement = None
    for spec in plan["grants"]:
        if spec["kind"] != "kubernetes":
            continue
        body = spec["body"]
        metadata = body["metadata"]
        key = (
            body["apiVersion"],
            body["kind"],
            metadata.get("namespace", ""),
            metadata["name"],
        )
        reads[key] = copy.deepcopy(body)
        if spec["lifetime"] != "resource" and progress[spec["key"]]["phase"] not in {
            "adopted",
            "revoked",
        }:
            if body["kind"] not in {
                "Role",
                "RoleBinding",
                "ClusterRole",
                "ClusterRoleBinding",
            }:
                if retirement is None:
                    retirement = dormant_retirement_specs(qualified)
                require(spec in retirement)
            deletes[key] = copy.deepcopy(progress[spec["key"]]["identity"])
    for component in qualified.get(
        "inherited_components", progress["components"]
    ).values():
        body = component["desired"]
        metadata = body["metadata"]
        reads[
            (
                body["apiVersion"],
                body["kind"],
                metadata.get("namespace", ""),
                metadata["name"],
            )
        ] = copy.deepcopy(body)
    return reads, deletes


def dormant_retirement_specs(qualified):
    """Only the native dormant pair in this sealed original grant plan."""
    from .retirement_fence import documents

    generation = qualified["generation"]
    require(isinstance(generation, str) and re.fullmatch(r"[0-9a-f]{64}", generation))
    grants = qualified["retained_plan"]["grants"]
    pair = [
        s
        for s in grants
        if s["key"] in {"retirement-fence-policy", "retirement-fence-binding"}
    ]
    require(len(pair) == 2)
    allocation = pair[0].get("original_allocation_id")
    # This is the native plan's allocation, not the operation/lifecycle root ID.
    require(isinstance(allocation, str) and bool(allocation))
    require(
        all(
            s["original_allocation_id"] == allocation
            for s in grants
            if "original_allocation_id" in s
        )
    )
    expected = [
        {
            "key": "retirement-fence-" + suffix,
            "kind": "kubernetes",
            "actor": "registrar",
            "cluster_arn": qualified["cluster_arn"],
            "generation": generation,
            "body": body,
            "lifetime": "workspace",
            "original_allocation_id": allocation,
        }
        for body, suffix in zip(
            documents(
                "sp-bootstrap-" + generation[:24] + "-retirement",
                generation,
                active=False,
            ),
            ("policy", "binding"),
            strict=True,
        )
    ]
    require(pair == expected)
    return pair


# A third retained closure carries both predecessor proofs twice in the sealed
# descriptor/qualification. Keep the full bytes and the 380 KB stored-item cap.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


def encode_document(value):
    """Keep full original provenance inside DynamoDB's 400 KiB item limit."""
    import base64
    import gzip

    raw = canonical(value).encode()
    require(len(raw) <= MAX_DOCUMENT_BYTES)
    encoded = canonical(
        {
            "encoding": "gzip-base64-v1",
            "value": base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
        }
    )
    require(len(encoded.encode()) <= 380000)
    return encoded


def decode_document(document):
    import base64
    import gzip
    import io

    require(isinstance(document, str) and len(document.encode()) <= 380000)
    envelope = json.loads(document)
    require(
        set(envelope) == {"encoding", "value"}
        and envelope["encoding"] == "gzip-base64-v1"
    )
    with gzip.GzipFile(
        fileobj=io.BytesIO(base64.b64decode(envelope["value"], validate=True))
    ) as stream:
        raw = stream.read(MAX_DOCUMENT_BYTES + 1)
    require(len(raw) <= MAX_DOCUMENT_BYTES)
    value = json.loads(raw)
    require(isinstance(value, dict) and raw == canonical(value).encode())
    return value
