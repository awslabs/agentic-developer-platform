"""Retained management components after complete failed-path revocation.

This is deliberately distinct from the precomponent acquisition boundary. The
whole original plan and progress are retained, including component ownership.
Only reviewed source may establish the durable pre-admission ordering: successful
bootstrap writes retain_workspace before clearing the taint, and never removes it.
"""

import hashlib
import json
import re

KIND = "bootstrap-management-components-revoked-v1"
SOURCE_FLAG = "management_components_revoked"
MARKERS = {
    "components",
    "component_inventory_complete",
    "component_inventory_mode",
    "management_observation",
    "prerequisite_inventory",
    "system_workload_baseline",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(value):
    if not value:
        raise ValueError("retained management bootstrap is unproven")


def validate_source(source):
    require(source is not None and source.get(SOURCE_FLAG) is True)
    require(
        all(
            "workspace_bootstrap/superplane_bootstrap/" + name + ".py" in source["hashes"]
            for name in (
                "workspace",
                "temporary_authority",
                "authority_runtime",
                "authority_backend",
                "component_journal",
                "registry",
                "adapters",
                "workload_inventory",
                "grant_plan",
            )
        )
    )


def journal_identity(row, *, grant_identity=None):
    """Validate one original 25-grant journal without altering any stored byte."""
    return _journal_identity(row, grant_identity=grant_identity, reader=False, complete=True)


def reader_journal_identity(row, *, complete, grant_identity=None, inherited_components=None):
    """Distinct reader successor shape; callers also bind its prior ownership."""
    return _journal_identity(row, grant_identity=grant_identity, reader=True, complete=complete, inherited_components=inherited_components)


def _journal_identity(row, *, grant_identity, reader, complete, inherited_components=None):
    if grant_identity is None:
        from src.shared.domain_bootstrap_acquisition import journal_identity as grant_identity

    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    partial = inherited_components is not None
    require(not partial or reader)
    markers = {"components", "prerequisite_inventory"} if partial else MARKERS
    grants = plan.get("grants", [])
    require(isinstance(grants, list) and len(grants) == (29 if reader else 25))
    require(set(progress) == {g["key"] for g in grants} | markers | {"phase"} | ({"complete"} if complete else set()))
    require(row["revoked"] is complete and progress["phase"] == ("revoked" if complete else "revoking"))
    require(not complete or progress["complete"] is True)
    require(partial or progress["component_inventory_complete"] is True and progress["component_inventory_mode"] == "management")
    # Validate the unchanged historical grant syntax with the existing strict
    # reader. This local projection is never persisted or emitted as acquisition
    # evidence; the identity below binds the entire component-bearing journal.
    grant_progress = {k: v for k, v in progress.items() if k not in MARKERS}
    grant_identity({**row, "progress_json": json.dumps(grant_progress)}, complete=complete)
    namespace = progress["workspace-namespace"]
    require(namespace["phase"] == ("adopted" if reader else "granted"))
    require(reader or namespace["identity"]["generation"] == row["generation"])
    require(
        all(
            progress[g["key"]].get("phase") in ({"revoked"} if complete else {"granted", "revoke_intent", "revoked"})
            and isinstance(progress[g["key"]].get("identity"), dict)
            for g in grants
            if g["key"] != "workspace-namespace"
        )
    )
    ns = grants[2]["body"]["metadata"]["name"]
    observation = progress.get("management_observation")
    expected_observation = {
        **{k: row[k] for k in ("operation_id", "org_id", "workspace_id", "cluster_arn")},
        "registration_claim": row["claim"],
        "namespace": ns,
    }
    if not partial:
        require(isinstance(observation, dict) and set(observation) == set(expected_observation) | {"endpoint"})
        require(all(observation[k] == v for k, v in expected_observation.items()))
        require(isinstance(observation["endpoint"], str) and observation["endpoint"].startswith("https://"))
    account = "superplane-controller"
    expected_components = {
        json.dumps([kind, namespace, name], separators=(",", ":"))
        for kind, namespace, name in (
            ("ServiceAccount", ns, account),
            ("Role", ns, account + "-workspace"),
            ("RoleBinding", ns, account + "-workspace"),
            ("ClusterRole", "", account + "-" + ns + "-cluster"),
            ("ClusterRoleBinding", "", account + "-" + ns + "-cluster"),
        )
    }
    components = progress["components"]
    require(isinstance(components, dict))
    if partial:
        require(isinstance(inherited_components, dict) and set(inherited_components) == expected_components)
        require(1 <= len(components) <= len(inherited_components) and set(components).issubset(inherited_components))
        require(all(record == inherited_components[key] for key, record in components.items()))
    else:
        require(set(components) == expected_components)
    for key, record in components.items():
        require(isinstance(record, dict) and set(record) == {"desired", "phase", "creation", "identity"})
        require(record["phase"] == "owned" and re.fullmatch(r"[a-f0-9]{32}", record["creation"]))
        identity = record["identity"]
        require(isinstance(identity, dict) and set(identity) == {"uid", "digest", "creation"})
        require(isinstance(identity["uid"], str) and bool(identity["uid"]) and re.fullmatch(r"[a-f0-9]{64}", identity["digest"]))
        require(identity["creation"] == record["creation"])
        desired = record["desired"]
        require(isinstance(desired, dict) and isinstance(desired.get("metadata"), dict))
        meta = desired["metadata"]
        require(json.dumps([desired.get("kind"), meta.get("namespace", ""), meta.get("name")], separators=(",", ":")) == key)
        require(desired.get("apiVersion") == ("v1" if desired["kind"] == "ServiceAccount" else "rbac.authorization.k8s.io/v1"))
        require("superplane.aws-e/component-creation" not in meta.get("annotations", {}))
    inventory = progress["prerequisite_inventory"]
    require(isinstance(inventory, dict) and set(inventory) == {"workspace_id", "prerequisites"})
    require(inventory["workspace_id"] == row["workspace_id"] and isinstance(inventory["prerequisites"], list))
    items = inventory["prerequisites"]
    require(len(items) == 3 and all(isinstance(i, dict) and set(i) == {"kind", "identifier", "workspace_id", "ownership", "reason"} for i in items))
    require(all(all(isinstance(v, str) and v for v in i.values()) and i["workspace_id"] == row["workspace_id"] for i in items))
    indexed = {i["kind"]: i for i in items}
    require(set(indexed) == {"EksAccessEntry", "EksPublicEndpoint", "SecurityGroupRule/private-sts"})
    require(
        indexed["EksAccessEntry"]["ownership"] == "adp-created"
        and indexed["EksAccessEntry"]["identifier"] == progress["supervisor-entry"]["identity"]["arn"]
    )
    require(indexed["EksPublicEndpoint"]["ownership"] == "adopted" and indexed["EksPublicEndpoint"]["identifier"] == row["cluster_arn"])
    require(
        indexed["SecurityGroupRule/private-sts"]["ownership"] == "adopted"
        and re.fullmatch(r"sgr-[a-f0-9]{8,17}", indexed["SecurityGroupRule/private-sts"]["identifier"])
    )
    if partial:
        # Do not invent inventory-complete, management-observation or baseline
        # markers. The exact earlier roster is a separate retained history input;
        # cleanup independently reads all five live objects before and after.
        return {**_journal_result(row, plan, progress, complete), "inherited_components": inherited_components}
    baseline = progress["system_workload_baseline"]
    require(
        isinstance(baseline, dict) and set(baseline) == {"version", "cluster_arn", "operation_id", "original_allocation_id", "generation", "objects"}
    )
    require(baseline["version"] == 1 and all(baseline[k] == row[k] for k in ("cluster_arn", "operation_id", "generation")))
    require(baseline["original_allocation_id"] == grants[21 if reader else 17]["original_allocation_id"])
    objects = baseline["objects"]
    require(isinstance(objects, list) and 1 <= len(objects) <= 10000)
    require(
        all(
            isinstance(o, dict)
            and set(o) == {"identity", "owner_references"}
            and isinstance(o["identity"], list)
            and len(o["identity"]) == 4
            and all(isinstance(v, str) for v in o["identity"])
            and all(o["identity"][i] for i in (0, 2, 3))
            and isinstance(o["owner_references"], list)
            for o in objects
        )
    )
    identities = [o["identity"] for o in objects]
    require(identities == sorted(identities) and len({tuple(i) for i in identities}) == len(identities))
    return _journal_result(row, plan, progress, complete)


def _journal_result(row, plan, progress, complete):
    return {
        **{k: row[k] for k in ("operation_id", "org_id", "workspace_id", "cluster_arn", "generation", "claim")},
        "plan_sha256": digest(plan),
        ("revoked_progress_sha256" if complete else "revoking_progress_sha256"): digest(progress),
        "retained_plan": plan,
        "retained_progress": progress,
    }


def validate(value, *, grant_identity=None):
    require(value.get("evidence_kind") == KIND and "settled_bootstrap" in value)
    require(not {"attempt_extension", "denied_grant", "retained_revocation", "precomponent_failures"}.intersection(value))
    revoked = value.get("revoked_bootstrap", {})
    require(isinstance(revoked, dict) and set(revoked) == {"journals", "current_generation", "predecessor"})
    journals = revoked["journals"]
    require(isinstance(journals, list) and len(journals) == 1 and revoked["predecessor"] is None)
    identity = journals[0]
    require(revoked["current_generation"] == identity["generation"])
    require(all(identity[k] == value[k] for k in ("operation_id", "org_id", "workspace_id")))
    row = {
        **identity,
        "plan_json": json.dumps(identity["retained_plan"]),
        "progress_json": json.dumps(identity["retained_progress"]),
        "revoked": True,
    }
    require(journal_identity(row, grant_identity=grant_identity) == identity)
    return identity
