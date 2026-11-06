"""Bounded settled bootstraps preserving their retained component retry lineage.

The original closure and retry remain immutable provenance. This deliberately
does not authorize arbitrary terminal retries or extend the original attempt cap.
"""

import json

KIND = "settled-bootstrap-component-successor-v1"
COMPONENTS_KIND = "bootstrap-reader-components-revoked-v1"
ORIGINAL_COMPONENTS_KIND = "bootstrap-management-components-revoked-v1"
MAX_PREDECESSORS = 4


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def lineage(value):
    """Bound recursion before validating nested immutable receipts."""
    from .settled_bootstrap import KIND as FIRST_KIND
    from .settled_bootstrap import require

    result = []
    for _ in range(MAX_PREDECESSORS + 1):
        require(isinstance(value, dict))
        if value.get("kind") == FIRST_KIND:
            return list(reversed(result))
        require(value.get("kind") == KIND and len(result) < MAX_PREDECESSORS)
        prior = value["predecessor"]
        require(isinstance(prior, dict))
        result.append(prior)
        value = decoded(prior["proof"])["settled_bootstrap"]
    require(False)


def validate(value):
    from . import settled_bootstrap as original
    from .provider_retry import bootstrap_boundary_identity

    require = original.require
    require(isinstance(value, dict) and value.get("kind") == KIND)
    lineage(value)
    base = {k: v for k, v in value.items() if k != "predecessor"}
    base["kind"] = original.KIND
    original.validate(base)
    prior = value["predecessor"]
    require(isinstance(prior, dict))
    proof = decoded(prior["proof"])
    require(proof.get("evidence_kind") in {ORIGINAL_COMPONENTS_KIND, COMPONENTS_KIND})
    bootstrap_boundary_identity(proof)
    old = original.validate(proof["settled_bootstrap"])
    require(
        old["kind"]
        == (
            original.KIND
            if proof["evidence_kind"] == ORIGINAL_COMPONENTS_KIND
            else KIND
        )
    )
    operation, call = value["operation"], value["call"]
    require(prior["recovery_scope"] == "unstarted-bootstrap")
    require(prior["original_key"] == old["call"]["idempotency_key"])
    require(prior["source_fence"] == old["call"]["fence_token"])
    require(prior["source_attempt_id"] == old["call"]["attempt_id"])
    require(prior["expected_version"] == old["operation"]["version"])
    require(prior["restored_version"] == prior["expected_version"] + 1)
    require(operation["version"] == prior["restored_version"] + 1)
    require(prior["replacement_key"] == call["idempotency_key"])
    require(old["lease"]["fence_token"] < call["fence_token"])
    require(value["lease"]["attempts"] == old["lease"]["attempts"] + 1)
    require(value["lease"]["max_attempts"] == old["lease"]["max_attempts"])
    require(
        all(
            operation[k] == old["operation"][k]
            for k in (
                "operation_id",
                "org_id",
                "workspace_id",
                "job_id",
                "attempt_id",
                "action",
                "created_at",
                "request_payload",
                "plan_digest",
            )
        )
    )
    require(
        all(
            prior[k] == operation[k]
            for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
        )
    )
    source = decoded(prior["source_intent"])
    require(
        all(
            source[k] == old["call"][k]
            for k in (
                "operation_id",
                "org_id",
                "workspace_id",
                "job_id",
                "attempt_id",
                "fence_token",
                "idempotency_key",
                "provider",
                "operation_kind",
                "target",
                "stage",
                "outcome",
                "provider_ref",
            )
        )
    )
    require(
        all(
            call[k] == source[k]
            for k in ("job_id", "provider", "operation_kind", "target")
        )
    )
    require(value["settlement"]["receipt_id"] != old["settlement"]["receipt_id"])
    return value


async def predecessor(connection, operation, receipts):
    """Read every immutable predecessor and its still-retained settlement."""
    from .settled_bootstrap import require, snapshot
    from .settled_bootstrap import validate as original_validate

    require(1 <= len(receipts) <= MAX_PREDECESSORS)
    prior = receipts[-1]
    proof = decoded(prior["proof"])
    require(proof.get("evidence_kind") in {ORIGINAL_COMPONENTS_KIND, COMPONENTS_KIND})
    old = original_validate(proof["settled_bootstrap"])
    require([snapshot(dict(r)) for r in receipts[:-1]] == lineage(old))
    for receipt in receipts:
        closure = original_validate(decoded(receipt["proof"])["settled_bootstrap"])
        retained = await connection.fetchrow(
            "SELECT * FROM harness_recovery_settlements WHERE "
            "receipt_id=$1 AND operation_id=$2",
            closure["settlement"]["receipt_id"],
            operation["operation_id"],
        )
        require(
            retained is not None and snapshot(dict(retained)) == closure["settlement"]
        )
    return snapshot(dict(prior))


def retained_journals(value):
    validate(value)
    return decoded(value["predecessor"]["proof"])["revoked_bootstrap"]["journals"]


def component_anchor(value):
    """The original complete roster is history, never a new component journal."""
    validate(value)
    first = lineage(value)[0]
    proof = decoded(first["proof"])
    journals = proof["revoked_bootstrap"]["journals"]
    from .settled_bootstrap import require

    require(proof["evidence_kind"] == ORIGINAL_COMPONENTS_KIND and len(journals) == 1)
    return journals[0]


def recoverable(proof):
    """Receipt-side checks; Gateway separately verifies the exact native syntax."""
    from .identity import OperationRefused
    from .settled_bootstrap import digest, require

    try:
        require(proof.get("evidence_kind") == COMPONENTS_KIND)
        owner = proof["owner_evidence"]
        require(owner["evidence_kind"] == COMPONENTS_KIND)
        closure = validate(proof["settled_bootstrap"])
        require(owner["settled_bootstrap"] == closure)
        require(proof["revoked_bootstrap"] == owner["revoked_bootstrap"])
        revoked = proof["revoked_bootstrap"]
        require(set(revoked) == {"journals", "current_generation", "predecessor"})
        prior = closure["predecessor"]
        require(
            revoked["predecessor"]
            == {
                "reauthorization_id": prior["reauthorization_id"],
                "source_fence": prior["source_fence"],
                "proof_sha256": digest(decoded(prior["proof"])),
            }
        )
        previous = retained_journals(closure)
        journals = revoked["journals"]
        current = [
            j for j in journals if j["generation"] == revoked["current_generation"]
        ]
        require(
            1 <= len(previous) <= MAX_PREDECESSORS
            and len(journals) == len(previous) + 1
            and len(current) == 1
        )
        require([j for j in journals if j not in current] == previous)
        journal = current[0]
        require(journal["generation"] not in {j["generation"] for j in previous})
        require(
            all(
                journal[k] == owner[k] == closure["operation"][k]
                for k in ("operation_id", "org_id", "workspace_id")
            )
        )
        require(
            all(
                proof.get(k) is True
                for k in (
                    "bootstrap_mutations_started",
                    "bootstrap_grants_created",
                    "bootstrap_journal_revoked",
                    "bootstrap_components_started",
                    "bootstrap_components_retained",
                )
            )
        )
        require(proof.get("bootstrap_admission_started") is False)
        require(
            not any(
                {
                    "attempt_extension",
                    "denied_grant",
                    "retained_revocation",
                    "precomponent_failures",
                }.intersection(v)
                for v in (proof, owner)
            )
        )
        plan, progress = journal["retained_plan"], journal["retained_progress"]
        require(
            journal["plan_sha256"] == digest(plan)
            and journal["revoked_progress_sha256"] == digest(progress)
        )
        grants = plan["grants"]
        require(plan["version"] == 1 and len(grants) == 29)
        require(
            progress["phase"] == "revoked"
            and progress["complete"] is True
            and "retain_workspace" not in progress
        )
        old_progress = component_anchor(closure)["retained_progress"]
        if "inherited_components" in journal:
            inherited = journal["inherited_components"]
            require(inherited == old_progress["components"] and len(inherited) == 5)
            require(
                set(progress)
                == {g["key"] for g in grants}
                | {"phase", "complete", "components", "prerequisite_inventory"}
            )
            require(
                isinstance(progress["components"], dict)
                and 1 <= len(progress["components"]) <= 5
            )
            require(
                all(
                    k in inherited and v == inherited[k]
                    for k, v in progress["components"].items()
                )
            )
        else:
            require(
                progress["component_inventory_complete"] is True
                and progress["component_inventory_mode"] == "management"
            )
            require(progress["components"] == old_progress["components"])
        require(progress["workspace-namespace"]["phase"] == "adopted")
        require(
            progress["workspace-namespace"]["identity"]
            == old_progress["workspace-namespace"]["identity"]
        )
        require(
            all(
                g["generation"] == journal["generation"]
                and g["cluster_arn"] == journal["cluster_arn"]
                for g in grants
            )
        )
        require(
            all(
                progress[g["key"]]["phase"] == "revoked"
                and progress[g["key"]]["identity"]
                for g in grants
                if g["key"] != "workspace-namespace"
            )
        )
        return True
    except (KeyError, TypeError, ValueError, OperationRefused):
        return False


def journals_bound(value, rows):
    """Bind every stored plan, preserving all prior journals byte-for-byte."""
    from .settled_bootstrap import digest, require

    validate(value)
    previous = retained_journals(value)
    require(1 <= len(previous) <= MAX_PREDECESSORS and len(rows) == len(previous) + 1)
    old_by_generation = {j["generation"]: j for j in previous}
    require(len(old_by_generation) == len(previous))
    require(len({r["generation"] for r in rows}) == len(rows))
    current = [r for r in rows if r["generation"] not in old_by_generation]
    require(len(current) == 1)
    for old in previous:
        retained = [r for r in rows if r["generation"] == old["generation"]]
        require(len(retained) == 1)
        prior_row = retained[0]
        require(prior_row["revoked"] is True)
        require(decoded(prior_row["plan_json"]) == old["retained_plan"])
        require(decoded(prior_row["progress_json"]) == old["retained_progress"])
        require(
            all(
                prior_row[k] == old[k]
                for k in (
                    "generation",
                    "claim",
                    "operation_id",
                    "org_id",
                    "workspace_id",
                    "cluster_arn",
                )
            )
        )
    partial = decoded(value["settlement"]["accounting"])["partial_lifecycle"]
    require(
        partial["phase"] == "bootstrap-workspace"
        and partial["workflow_complete"] is False
    )
    require(partial["source_artifact_id"] == value["call"]["target"])
    sources = partial["authority_generations"]
    require(
        len(sources) == len(rows)
        and len({s["generation"] for s in sources}) == len(rows)
    )
    indexed = {s["generation"]: s for s in sources}
    require(set(indexed) == {r["generation"] for r in rows})
    for row in rows:
        require(
            all(
                row[k] == value["operation"][k]
                for k in ("operation_id", "org_id", "workspace_id")
            )
        )
        source = indexed[row["generation"]]
        require(
            source["claim"] == row["claim"]
            and source["plan_sha256"] == digest(row["plan_json"])
        )
    return current[0]
