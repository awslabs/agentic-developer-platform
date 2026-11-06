"""Stopped grant acquisition, before component installation, with retained namespace.

This boundary never describes RBAC as absent until native revocation completes.
The exact qualified worker persists each grant intent before its provider call.
"""

import hashlib
import json
import re
from datetime import datetime

KIND = "bootstrap-acquisition-revoked-v1"
PERMISSION_FAILURE = "bootstrap-precomponent-permission-refusal-v1"
PREFIX = (
    "registrar-entry",
    "registrar-policy",
    "workspace-namespace",
    "supervisor-entry",
    "supervisor-cluster-role",
    "supervisor-cluster-binding",
    "supervisor-namespace-role",
    "supervisor-namespace-binding",
    "supervisor-system-role",
    "supervisor-system-binding",
    "installer-entry",
    "installer-cluster-role",
    "installer-cluster-binding",
    "installer-namespace-role",
    "installer-namespace-binding",
    "installer-system-role",
    "installer-system-binding",
)
CLEANUP_PREFIX = (
    "retirement-fence-policy",
    "retirement-fence-binding",
    "cleanup-cluster-role",
    "cleanup-cluster-binding",
    "cleanup-namespace-role",
    "cleanup-namespace-binding",
    "cleanup-system-role",
    "cleanup-system-binding",
)
READER_PREFIX = (
    "reader-issuer-cluster-role",
    "reader-issuer-cluster-binding",
    "reader-issuer-namespace-role",
    "reader-issuer-namespace-binding",
)


def acquisition_order(*, reader_renewal=False, cleanup=False):
    prefix = PREFIX[:3] + READER_PREFIX + PREFIX[3:] if reader_renewal else PREFIX
    return prefix + CLEANUP_PREFIX if cleanup else prefix


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(value):
    if not value:
        raise ValueError("bootstrap grant acquisition boundary is unproven")


def validate_source_prefix(source, identity):
    """Earlier image qualifications stop before the retirement grant compiler."""
    require(source.get("acquisition_prefix") is True)
    reader = identity.get("reader_renewal") is True
    if reader:
        require(source.get("reader_renewal_acquisition") is True)
        require("workspace_bootstrap/superplane_bootstrap/grant_plan.py" in source["hashes"])
    if len(identity.get("acquisition_prefix", [])) > len(acquisition_order(reader_renewal=reader)):
        require(source.get("cleanup_acquisition_prefix") is True)
        require("workspace_bootstrap/superplane_bootstrap/retirement_fence.py" in source["hashes"])
    if "prerequisite_inventory" in identity:
        require(source.get("precomponent_permission_refusal") is True)


def validate_precomponent_failures(source, value, journals):
    """Owner-authenticated failure observations supplement, never replace, expiry.

    Only the qualified original call graph establishes that this SSAR failed
    before installation. A prerequisite marker alone cannot establish that fact.
    """
    marked = {j["generation"]: j for j in journals if "prerequisite_inventory" in j}
    proofs = value.get("precomponent_failures", {})
    require(isinstance(proofs, dict) and set(proofs) == set(marked))
    for generation, identity in marked.items():
        validate_source_prefix(source, identity)
        proof = proofs[generation]
        expected = {
            "kind": PERMISSION_FAILURE,
            **{k: value[k] for k in ("source_image", "source_revision", "source_pod_name", "source_pod_uid")},
            **{k: identity[k] for k in ("operation_id", "org_id", "workspace_id", "generation", "plan_sha256", "precomponent_progress_sha256")},
            "source_hashes_sha256": digest(source["hashes"]),
            "source_audit_sha256": digest(value["source_audit"]),
            "prerequisite_inventory_sha256": digest(identity["prerequisite_inventory"]),
        }
        provenance = {
            "log_group",
            "failure_event_id",
            "failure_log_stream",
            "failure_message_sha256",
            "stopped_event_id",
            "stopped_message_sha256",
            "failed_at",
            "stopped_at",
        }
        require(isinstance(proof, dict) and set(proof) == set(expected) | provenance)
        require(all(proof[k] == v for k, v in expected.items()))
        require(all(isinstance(proof[k], str) and proof[k].strip() for k in provenance))
        require(all(re.fullmatch(r"[a-f0-9]{64}", proof[k]) for k in ("failure_message_sha256", "stopped_message_sha256")))
        audit_group = value["source_audit"]["log_group"]
        require(audit_group.startswith("/aws/eks/") and audit_group.endswith("/cluster"))
        cluster = audit_group.removeprefix("/aws/eks/").removesuffix("/cluster")
        require(proof["log_group"] == f"/aws/containerinsights/{cluster}/application")
        require(proof["failure_event_id"] != proof["stopped_event_id"])
        times = [
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            for v in (
                value["source_acquired_at"],
                proof["failed_at"],
                proof["stopped_at"],
                value["checked_at"],
            )
        ]
        require(all(t.tzinfo is not None for t in times) and times == sorted(times))
    return proofs


def prerequisite_inventory(row, progress, grants):
    """Closed inventory: only the already-journaled supervisor is removable."""
    value = progress["prerequisite_inventory"]
    require(isinstance(value, dict) and set(value) == {"workspace_id", "prerequisites"})
    require(value["workspace_id"] == row["workspace_id"] and isinstance(value["prerequisites"], list))
    items = value["prerequisites"]
    require(len(items) == 3)
    expected = {"EksAccessEntry": "adp-created", "EksPublicEndpoint": "adopted", "SecurityGroupRule/private-sts": "adopted"}
    require(all(isinstance(item, dict) and set(item) == {"kind", "identifier", "workspace_id", "ownership", "reason"} for item in items))
    require(all(all(isinstance(v, str) and v.strip() for v in item.values()) for item in items))
    require({item["kind"] for item in items} == set(expected))
    require(all(item["workspace_id"] == row["workspace_id"] and item["ownership"] == expected[item["kind"]] for item in items))
    indexed = {item["kind"]: item for item in items}
    require(indexed["EksAccessEntry"]["identifier"] == progress["supervisor-entry"]["identity"]["arn"])
    require(indexed["EksPublicEndpoint"]["identifier"] == row["cluster_arn"])
    require(re.fullmatch(r"sgr-[a-f0-9]{8,17}", indexed["SecurityGroupRule/private-sts"]["identifier"]))
    # Bind every immutable identity and the complete inventory across native
    # cleanup. Only known grant phases change; the original marker is retained.
    original = {"phase": "revoking", "prerequisite_inventory": value}
    for spec in grants:
        key = spec["key"]
        status = progress[key]
        require(isinstance(status, dict) and set(status) == {"phase", "identity"})
        require(isinstance(status["identity"], dict))
        if key == "workspace-namespace":
            original[key] = status
        else:
            require(status["phase"] in {"granted", "revoke_intent", "revoked"})
            original[key] = {**status, "phase": "revoke_intent" if key == grants[-1]["key"] else "granted"}
    if not row["revoked"]:
        require(digest(original) == digest(progress))
    return {"prerequisite_inventory": value, "precomponent_progress_sha256": digest(original)}


def extension_boundary(value):
    """An additional allowance requires this complete, positively revoked plan."""
    require(value.get("evidence_kind") == KIND)
    revoked = value.get("revoked_bootstrap", {})
    require(1 <= len(revoked.get("journals", [])) <= 10)
    generation = revoked.get("current_generation")
    current = [j for j in revoked.get("journals", []) if j.get("generation") == generation]
    require(bool(generation) and len(current) == 1)
    journal = current[0]
    require(re.fullmatch(r"[a-f0-9]{64}", journal.get("revoked_progress_sha256", "")))
    prefix = journal.get("acquisition_prefix", [])
    require(tuple(g.get("key") for g in prefix) == acquisition_order(reader_renewal=journal.get("reader_renewal") is True, cleanup=True))
    require(all(g.get("generation") == generation for g in prefix))
    require(journal.get("retained_namespace", {}).get("progress", {}).get("identity", {}).get("uid"))
    return journal


def journal_identity(row, *, complete):
    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    grants = plan.get("grants", [])
    # Managed workspaces install their dormant retirement fence and cleanup
    # RBAC in this same acquisition loop. They are grants, not components.
    # Only these two complete, source-qualified plans are understood here.
    reader = len(grants) in {21, 29}
    prefix_order = acquisition_order(reader_renewal=reader)
    order = acquisition_order(reader_renewal=reader, cleanup=len(grants) in {25, 29})
    require(plan.get("version") == 1 and len(grants) == len(order))
    require(tuple(g.get("key") for g in grants) == order)
    require(len({g.get("key") for g in grants}) == len(grants))
    require(all(g.get("generation") == row["generation"] and g.get("cluster_arn") == row["cluster_arn"] for g in grants))
    require(re.fullmatch(r"[a-f0-9]{64}", row["generation"]) and re.fullmatch(r"[a-f0-9]{64}", row["claim"]))
    require(hashlib.sha256((row["operation_id"] + ":" + row["claim"]).encode()).hexdigest() == row["generation"])
    require(row["revoked"] is complete)
    require(progress.get("phase") in ({"revoked"} if complete else {"acquiring", "revoking"}))
    require(not complete or progress.get("complete") is True)
    marker = {"prerequisite_inventory"} if "prerequisite_inventory" in progress else set()
    keys = set(progress) - {"phase", "complete"} - marker
    require(4 <= len(keys) <= len(order) and keys == set(order[: len(keys)]))
    require(set(progress) == keys | marker | {"phase"} | ({"complete"} if complete else set()))
    if marker:
        require(len(grants) == len(keys) == (29 if reader else 25) and progress["phase"] == ("revoked" if complete else "revoking"))
    namespace = progress["workspace-namespace"]
    # Acquisition has passed namespace verification. It remains a resource;
    # native cleanup cannot remove it or change its adoption record.
    require(isinstance(namespace, dict) and set(namespace) == {"phase", "identity"} and namespace["phase"] in {"adopted", "granted"})
    namespace_identity = namespace["identity"]
    require(namespace_identity.get("uid") and re.fullmatch(r"[a-f0-9]{64}", namespace_identity.get("generation", "")))
    require(namespace_identity.get("labels", {}).get("superplane.aws-e/bootstrap-owner") == row["workspace_id"])
    ns = grants[2]
    require(ns.get("kind") == "kubernetes" and ns.get("lifetime") == "resource" and ns.get("actor") == "registrar")
    require(ns.get("body", {}).get("kind") == "Namespace" and ns["body"].get("apiVersion") == "v1")
    require(ns["body"].get("metadata", {}).get("labels", {}).get("superplane.aws-e/bootstrap-owner") == row["workspace_id"])
    stem = "sp-bootstrap-" + row["generation"][:24]
    if reader:
        reader_group = "superplane:reader:" + row["workspace_id"]
        for spec in grants[3:7]:
            key = spec["key"]
            cluster = key.startswith("reader-issuer-cluster-")
            kind = "ClusterRole" if cluster else "Role"
            name = stem + ("-reader-issuer-cluster" if cluster else "-reader-issuer-namespace")
            metadata = {"name": name, "annotations": {"superplane.aws-e/authority-generation": row["generation"]}}
            if not cluster:
                metadata["namespace"] = ns["body"]["metadata"]["name"]
            if key.endswith("role"):
                contents = {
                    "rules": [
                        {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get"], "resourceNames": [ns["body"]["metadata"]["name"]]},
                        {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["list"]},
                    ]
                    if cluster
                    else [
                        {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["get"], "resourceNames": ["superplane-controller"]},
                        {"apiGroups": [""], "resources": ["serviceaccounts/token"], "verbs": ["create"], "resourceNames": ["superplane-controller"]},
                    ]
                }
            else:
                contents = {
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": kind, "name": name},
                    "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": reader_group}],
                }
                kind += "Binding"
            require(
                spec
                == {
                    "key": key,
                    "kind": "kubernetes",
                    "actor": "registrar",
                    "lifetime": "workspace",
                    "generation": row["generation"],
                    "cluster_arn": row["cluster_arn"],
                    "body": {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": kind, "metadata": metadata, **contents},
                }
            )
    if len(order) > len(prefix_order):
        allocation = grants[len(prefix_order)].get("original_allocation_id")
        require(isinstance(allocation, str) and bool(allocation))
        for spec in grants[len(prefix_order) :]:
            require(spec.get("actor") == "registrar" and spec.get("lifetime") == "workspace")
            require(spec.get("kind") == "kubernetes" and spec.get("original_allocation_id") == allocation)
            body = spec.get("body", {})
            metadata = body.get("metadata", {})
            require(metadata.get("annotations", {}).get("superplane.aws-e/authority-generation") == row["generation"])
            key = spec["key"]
            if key.startswith("retirement-fence-"):
                require(body.get("apiVersion") == "admissionregistration.k8s.io/v1")
                require(metadata.get("name") == stem + "-retirement" and not metadata.get("namespace"))
                if key.endswith("policy"):
                    require(body.get("kind") == "ValidatingAdmissionPolicy")
                    require(
                        body.get("spec", {}).get("validations")
                        == [
                            {
                                "expression": "true",
                                "message": "dedicated workspace retirement has closed workload admission",
                            }
                        ]
                    )
                else:
                    require(body.get("kind") == "ValidatingAdmissionPolicyBinding")
                    require(body.get("spec") == {"policyName": stem + "-retirement", "validationActions": ["Deny"]})
            else:
                scope, kind = key.removeprefix("cleanup-").split("-")
                require(body.get("apiVersion") == "rbac.authorization.k8s.io/v1")
                require(body.get("kind") == ("ClusterRole" if scope == "cluster" else "Role") + ("Binding" if kind == "binding" else ""))
                require(metadata.get("name") == stem + "-cleanup-" + scope)
                require(metadata.get("namespace", "") == {"cluster": "", "namespace": ns["body"]["metadata"]["name"], "system": "kube-system"}[scope])
    entries = {g["actor"]: g for g in grants[: len(prefix_order)] if g.get("kind") == "eks-entry"}
    require(set(entries) == {"registrar", "supervisor", "installer"})
    for actor, entry in entries.items():
        require(entry["key"] == actor + "-entry" and entry.get("groups") == [stem + ":" + actor])
        require(entry.get("username") == stem + ":" + actor + ":{{SessionName}}")
        require(entry.get("client_token") == hashlib.sha256((row["generation"] + ":" + actor).encode()).hexdigest())
        require(entry.get("lifetime") == ("workspace" if actor == "supervisor" else "temporary"))
    require(
        grants[1]
        == {
            **entries["registrar"],
            "key": "registrar-policy",
            "kind": "eks-policy",
            "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
            "scope": {"type": "cluster"},
        }
    )
    for index, spec in enumerate(grants[: len(keys)]):
        key = spec["key"]
        if key == "workspace-namespace":
            continue
        status = progress[key]
        require(isinstance(status, dict) and set(status) <= {"phase", "identity"})
        require(status.get("phase") in ({"revoked"} if complete else {"granted", "grant_intent", "revoke_intent", "revoked"}))
        if status["phase"] == "grant_intent":
            require(index == len(keys) - 1 and "identity" not in status)
        identity = status.get("identity")
        require(identity is not None or index == len(keys) - 1 and status["phase"] != "granted")
        if identity is None:
            continue
        require(identity.get("generation") == row["generation"])
        if spec["kind"].startswith("eks-"):
            entry = entries[spec["actor"]]
            require(identity.get("groups") == entry["groups"] and identity.get("username") == entry["username"])
            require(identity.get("arn", "").startswith(row["cluster_arn"].replace(":cluster/", ":access-entry/") + "/"))
            if spec["kind"] == "eks-policy":
                require(identity["arn"] == progress["registrar-entry"]["identity"]["arn"])
                require(identity.get("policy_arn") == spec["policy_arn"] and identity.get("associated_at"))
                require(identity.get("scope") in ({"type": "cluster"}, {"type": "cluster", "namespaces": []}))
        else:
            require(spec["kind"] == "kubernetes" and spec["lifetime"] in {"temporary", "workspace"})
            require(
                spec["body"]["kind"]
                in {
                    "Role",
                    "RoleBinding",
                    "ClusterRole",
                    "ClusterRoleBinding",
                    "ValidatingAdmissionPolicy",
                    "ValidatingAdmissionPolicyBinding",
                }
            )
            require(identity.get("uid") and re.fullmatch(r"[a-f0-9]{64}", identity.get("digest", "")))
    return {
        **{k: row[k] for k in ("operation_id", "org_id", "workspace_id", "cluster_arn", "generation", "claim")},
        "plan_sha256": digest(plan),
        "acquisition_prefix": grants[: len(keys)],
        **({"reader_renewal": True} if reader else {}),
        "retained_namespace": {"grant": ns, "progress": namespace},
        **(prerequisite_inventory(row, progress, grants) if marker else {}),
        **({"revoked_progress_sha256": digest(progress)} if complete else {"acquiring_progress_sha256": digest(progress)}),
    }
