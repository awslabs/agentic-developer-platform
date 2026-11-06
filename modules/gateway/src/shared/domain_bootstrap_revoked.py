"""A fully revoked registrar prefix is distinct from an unstarted bootstrap."""

import hashlib
import json
import re

KIND = "bootstrap-pre-namespace-revoked-v1"
NAMESPACE_KIND = "bootstrap-retained-namespace-v1"
ACQUISITION_KIND = "bootstrap-acquisition-revoked-v1"
KINDS = {KIND, NAMESPACE_KIND, ACQUISITION_KIND}
KEYS = {"registrar-entry", "registrar-policy"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(value):
    if not value:
        raise ValueError("pre-namespace bootstrap revocation is unproven")


def journal_identity(row, *, complete, retained_namespace=False):
    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    original_progress = progress
    namespace_status = None
    if retained_namespace:
        require(complete)
        namespace_status = progress.get("workspace-namespace")
        # The resource outlives temporary authority. Preserve its intent honestly;
        # retry re-observes and validates it through normal namespace adoption.
        require(namespace_status == {"phase": "grant_intent"})
        progress = {k: v for k, v in progress.items() if k != "workspace-namespace"}
    require(row["revoked"] is complete)
    require(progress.get("phase") == ("revoked" if complete else "revoking"))
    require(set(progress) == KEYS | {"phase"} | ({"complete"} if complete else set()))
    require(not complete or progress["complete"] is True)
    require(re.fullmatch(r"[a-f0-9]{64}", row["generation"]) and re.fullmatch(r"[a-f0-9]{64}", row["claim"]))
    require(hashlib.sha256((row["operation_id"] + ":" + row["claim"]).encode()).hexdigest() == row["generation"])
    grants = plan.get("grants", [])
    require(plan.get("version") == 1 and len(grants) > 2)
    entry, policy, namespace = grants[:3]
    # Native compilation always starts with precisely these two temporary EKS
    # grants, before the first Kubernetes mutation can receive a durable intent.
    require(entry.get("key") == "registrar-entry" and entry.get("kind") == "eks-entry" and entry.get("lifetime") == "temporary")
    require(
        policy
        == {
            **entry,
            "key": "registrar-policy",
            "kind": "eks-policy",
            "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
            "scope": {"type": "cluster"},
        }
    )
    require(
        namespace.get("key") == "workspace-namespace"
        and namespace.get("kind") == "kubernetes"
        and namespace.get("body", {}).get("kind") == "Namespace"
    )
    if retained_namespace:
        require(namespace.get("lifetime") == "resource" and namespace.get("actor") == "registrar")
    require(len({g.get("key") for g in grants}) == len(grants))
    require(all(g.get("generation") == row["generation"] and g.get("cluster_arn") == row["cluster_arn"] for g in grants))
    stem = "sp-bootstrap-" + row["generation"][:24]
    require(
        entry.get("actor") == "registrar"
        and entry.get("groups") == [stem + ":registrar"]
        and entry.get("username") == stem + ":registrar:{{SessionName}}"
    )
    require(entry.get("client_token") == hashlib.sha256((row["generation"] + ":registrar").encode()).hexdigest())
    for key in KEYS:
        status = progress[key]
        require(set(status) <= {"phase", "identity"})
        require(status.get("phase") in ({"revoked"} if complete else {"granted", "revoke_intent", "revoked"}))
        identity = status.get("identity")
        if identity is None:
            # A lost delete acknowledgement can leave no policy identity. The
            # retained entry still proves created authority; native cleanup and
            # the fresh owner collector must positively establish actor absence.
            require(key == "registrar-policy" and status["phase"] in ({"revoked"} if complete else {"revoke_intent", "revoked"}))
            continue
        require(
            identity.get("generation") == row["generation"]
            and identity.get("groups") == entry["groups"]
            and identity.get("username") == entry["username"]
        )
        arn = identity.get("arn", "")
        prefix = row["cluster_arn"].replace(":cluster/", ":access-entry/") + "/"
        require(arn.startswith(prefix) and len(arn[len(prefix) :].split("/")) >= 4)
        if key == "registrar-policy":
            require(
                identity.get("policy_arn") == policy["policy_arn"]
                and identity.get("scope") in ({"type": "cluster"}, {"type": "cluster", "namespaces": []})
                and identity.get("associated_at")
            )
            require(identity["arn"] == progress["registrar-entry"]["identity"]["arn"])
    return {
        **{k: row[k] for k in ("operation_id", "org_id", "workspace_id", "cluster_arn", "generation", "claim")},
        "plan_sha256": digest(plan),
        "prefix": [entry, policy],
        **({"revoked_progress_sha256": digest(original_progress)} if complete else {}),
        **({"retained_namespace": {"grant": namespace, "progress": namespace_status}} if retained_namespace else {}),
    }


def retained_identity(row):
    from src.shared.domain_bootstrap_denial import journal_identity as denied_identity

    progress = json.loads(row["progress_json"])
    if "supervisor-entry" in progress or "reader-issuer-cluster-role" in progress:
        from src.shared.domain_bootstrap_acquisition import journal_identity as acquisition_identity

        return acquisition_identity(row, complete=True)
    if "registrar-policy" in progress:
        return journal_identity(row, complete=True, retained_namespace="workspace-namespace" in progress)
    return denied_identity(row, complete=True)


def validate_revoked(value):
    require(value.get("evidence_kind") in KINDS)
    require(not {"denied_grant", "retained_revocation", "denial_source_hashes"}.intersection(value))
    require("attempt_extension" not in value or value["evidence_kind"] == ACQUISITION_KIND)
    revoked = value.get("revoked_bootstrap", {})
    require(set(revoked) == {"journals", "current_generation", "predecessor"})
    journals = revoked["journals"]
    require(isinstance(journals, list) and 1 <= len(journals) <= 20)
    generations = [j.get("generation") for j in journals]
    require(generations == sorted(set(generations)))
    require(revoked["current_generation"] is None or revoked["current_generation"] in generations)
    for journal in journals:
        require(all(journal.get(k) == value[k] for k in ("operation_id", "org_id", "workspace_id")))
        require(all(re.fullmatch(r"[a-f0-9]{64}", journal.get(k, "")) for k in ("generation", "claim", "plan_sha256")))
        require(journal.get("cluster_arn"))
    namespaces = [j for j in journals if "retained_namespace" in j]
    require(bool(namespaces) is (value["evidence_kind"] in {NAMESPACE_KIND, ACQUISITION_KIND}))
    acquisitions = [j for j in journals if "acquisition_prefix" in j]
    require(bool(acquisitions) is (value["evidence_kind"] == ACQUISITION_KIND))
    for journal in namespaces:
        retained = journal["retained_namespace"]
        require(set(retained) == {"grant", "progress"})
        if "acquisition_prefix" in journal:
            require(retained["progress"].get("phase") in {"adopted", "granted"} and retained["progress"].get("identity", {}).get("uid"))
            require(len(journal["acquisition_prefix"]) >= 4 and retained["grant"] == journal["acquisition_prefix"][2])
        else:
            require(retained["progress"] == {"phase": "grant_intent"})
        grant = retained["grant"]
        require(
            grant.get("key") == "workspace-namespace"
            and grant.get("kind") == "kubernetes"
            and grant.get("lifetime") == "resource"
            and grant.get("actor") == "registrar"
            and grant.get("generation") == journal["generation"]
            and grant.get("cluster_arn") == journal["cluster_arn"]
            and grant.get("body", {}).get("kind") == "Namespace"
            and grant["body"].get("apiVersion") == "v1"
        )
    if revoked["current_generation"] is not None:
        current = next(j for j in journals if j["generation"] == revoked["current_generation"])
        require(re.fullmatch(r"[a-f0-9]{64}", current.get("revoked_progress_sha256", "")))
    prior = revoked["predecessor"]
    require(prior is not None or revoked["current_generation"] is not None)
    if prior is not None:
        require(set(prior) == {"reauthorization_id", "source_fence", "proof_sha256"})
        require(prior["reauthorization_id"] and type(prior["source_fence"]) is int and 0 < prior["source_fence"] < value["source_fence"])
        require(re.fullmatch(r"[a-f0-9]{64}", prior.get("proof_sha256", "")))
    return revoked
