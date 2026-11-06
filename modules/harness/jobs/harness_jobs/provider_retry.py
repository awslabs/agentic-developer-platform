"""Freshly authorized planning retries backed by independent protected proof.

The original call remains in the intent ledger, reconciled with owner evidence.
An immutable receipt chain selects a new key for the same admitted descriptor. Worker
restarts, lease acquisition and arbitrary provider failures never create keys.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from .identity import OperationRefused


async def receipts_for(connection, operation_id):
    return await connection.fetch(
        "SELECT * FROM harness_provider_retries WHERE operation_id=$1 "
        "ORDER BY source_fence",
        operation_id,
    )


async def receipt_for(connection, operation_id):
    return await connection.fetchrow(
        "SELECT * FROM harness_provider_retries WHERE operation_id=$1 "
        "ORDER BY source_fence DESC LIMIT 1",
        operation_id,
    )


async def verify_partial_source_claim(connection, lease, call, *, closed=False):
    """Accept only an uninterrupted chain of authenticated observation claims.

    A recovery observer fences a crashed execution without consuming an attempt
    or acquiring provider credentials. Its expired claim does not replace the
    original intent's producer. Never rewind the live lease to recover that
    provenance, or infer observation-only authority from a holder's name.
    Callers hold the operation and lease locks and separately require expiry.
    """
    if any(lease[k] != call[k] for k in ("operation_id", "org_id", "workspace_id")):
        raise OperationRefused("partial source scope changed")
    if call["fence_token"] == lease["fence_token"]:
        if call["attempt_id"] != lease["attempt_id"]:
            raise OperationRefused("partial source attempt changed")
        return
    source, current = call["fence_token"], lease["fence_token"]
    if not 1 <= source < current <= 32:
        raise OperationRefused("partial source recovery chain unavailable")
    audits = await connection.fetch(
        "SELECT event,actor,attempt_id,fence_token FROM harness_execution_audit "
        "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 AND allowed "
        "AND fence_token BETWEEN $4 AND $5 "
        "AND event IN ('lease.acquire','record_intent','recovery.claim')",
        call["operation_id"],
        call["org_id"],
        call["workspace_id"],
        source,
        current,
    )
    grants = [r for r in audits if r["event"] == "lease.acquire"]
    if (
        len(grants) != 1
        or grants[0]["fence_token"] != source
        or grants[0]["attempt_id"] != call["attempt_id"]
        or not any(
            r["event"] == "record_intent"
            and r["fence_token"] == source
            and r["attempt_id"] == call["attempt_id"]
            for r in audits
        )
    ):
        raise OperationRefused("partial source execution provenance changed")
    claims = await connection.fetch(
        "SELECT * FROM harness_recovery_claim_bindings WHERE operation_id=$1 "
        "AND fence_token>$2 AND fence_token<=$3 ORDER BY fence_token",
        call["operation_id"],
        source,
        current,
    )
    if [r["fence_token"] for r in claims] != list(range(source + 1, current + 1)):
        raise OperationRefused("partial source recovery chain incomplete")
    for claim in claims:
        matching = [
            r
            for r in audits
            if r["event"] == "recovery.claim"
            and r["fence_token"] == claim["fence_token"]
        ]
        if (
            any(claim[k] != call[k] for k in ("org_id", "workspace_id"))
            or not claim["subject"]
            or len(matching) != 1
            or matching[0]["actor"] != claim["holder"]
            or matching[0]["attempt_id"] != claim["attempt_id"]
        ):
            raise OperationRefused("partial source recovery provenance changed")
    if any(
        claims[-1][k] != lease[("closed_" if closed else "") + k]
        for k in ("holder", "attempt_id")
    ):
        raise OperationRefused("partial source current claim changed")


async def effective_step_key(connection, record, step):
    from .execution_plan import step_key

    key = step_key(record, step)
    receipts = await receipts_for(connection, record.operation_id)
    for receipt in receipts:
        if receipt["recovery_scope"] == "partial-apply-repair":
            partial_repair_identity(json.loads(receipt["proof"]))
        if receipt["recovery_scope"] == "unstarted-bootstrap":
            bootstrap_boundary_identity(json.loads(receipt["proof"]))
        if receipt["original_key"] != key or any(
            receipt[name] != getattr(record, name)
            for name in ("org_id", "workspace_id", "plan_digest")
        ):
            raise OperationRefused("provider retry differs from original admission")
        key = receipt["replacement_key"]
    if receipts:
        await active_call_rows(connection, record.operation_id, [])
    return key


async def active_call_rows(connection, operation_id, rows):
    """Exclude only exact calls covered by the complete immutable proof chain."""
    from .store import stored_outcome

    receipts = await receipts_for(connection, operation_id)
    previous, excluded = None, set()
    for receipt in receipts:
        if receipt["recovery_scope"] == "partial-apply-repair":
            partial_repair_identity(json.loads(receipt["proof"]))
        if receipt["recovery_scope"] == "unstarted-bootstrap":
            bootstrap_boundary_identity(json.loads(receipt["proof"]))
            bootstrap_retained_predecessor(json.loads(receipt["proof"]), previous)
            bootstrap_revoked_predecessor(json.loads(receipt["proof"]), previous)
        original = await connection.fetchrow(
            "SELECT * FROM harness_provider_call_intent WHERE idempotency_key=$1",
            receipt["original_key"],
        )
        source = json.loads(receipt["source_intent"])
        if (
            original is None
            or original["operation_id"] != operation_id
            or original["stage"] != "reconciled"
            or stored_outcome(original["outcome"]) != "failed"
            or original["provider_ref"] is not None
            or any(original[key] != receipt[key] for key in ("org_id", "workspace_id"))
            or original["fence_token"] != receipt["source_fence"]
            or original["attempt_id"] != receipt["source_attempt_id"]
            or any(
                original[key] != source[key]
                for key in ("job_id", "provider", "operation_kind", "target")
            )
            or (
                previous is not None
                and (
                    previous["replacement_key"] != receipt["original_key"]
                    or previous["restored_version"] > receipt["expected_version"]
                    or previous["source_fence"] >= receipt["source_fence"]
                    or any(
                        previous[key] != receipt[key]
                        for key in (
                            "org_id",
                            "workspace_id",
                            "plan_digest",
                            "original_approval_id",
                            "requester",
                            "approved_by",
                        )
                    )
                )
            )
        ):
            raise OperationRefused("original planning recovery evidence changed")
        excluded.add(receipt["original_key"])
        previous = receipt
    return [row for row in rows if row["idempotency_key"] not in excluded]


def bootstrap_no_effects(proof):
    """The denied boundary records a mutation attempt honestly, never as unstarted."""
    if proof.get("evidence_kind") in {
        "bootstrap-pre-namespace-revoked-v1",
        "bootstrap-retained-namespace-v1",
        "bootstrap-acquisition-revoked-v1",
    }:
        owner = proof.get("owner_evidence", {})
        revoked = proof.get("revoked_bootstrap", {})
        if not isinstance(owner, dict) or not isinstance(revoked, dict):
            return False
        journals = revoked.get("journals", [])
        generation = revoked.get("current_generation")
        return (
            isinstance(revoked, dict)
            and set(revoked) == {"journals", "current_generation", "predecessor"}
            and revoked == owner.get("revoked_bootstrap")
            and isinstance(journals, list)
            and 1 <= len(journals) <= 20
            and proof.get("bootstrap_mutations_started") is (generation is not None)
            and proof.get("bootstrap_grants_created") is (generation is not None)
            and proof.get("bootstrap_journal_revoked") is True
            and proof.get("bootstrap_components_started")
            is (
                proof["evidence_kind"]
                in {
                    "bootstrap-retained-namespace-v1",
                    "bootstrap-acquisition-revoked-v1",
                }
            )
            and any("retained_namespace" in j for j in journals)
            is (
                proof["evidence_kind"]
                in {
                    "bootstrap-retained-namespace-v1",
                    "bootstrap-acquisition-revoked-v1",
                }
            )
            and not {
                "denied_grant",
                "retained_revocation",
            }.intersection(proof)
            and (
                "attempt_extension" not in proof
                or proof["evidence_kind"] == "bootstrap-acquisition-revoked-v1"
            )
            and all(
                isinstance(j, dict)
                and all(
                    j.get(k) == owner.get(k)
                    for k in ("operation_id", "org_id", "workspace_id")
                )
                and all(
                    isinstance(j.get(k), str) and re.fullmatch(r"[a-f0-9]{64}", j[k])
                    for k in ("generation", "claim", "plan_sha256")
                )
                and j.get("cluster_arn")
                for j in journals
            )
            and [j["generation"] for j in journals]
            == sorted({j["generation"] for j in journals})
            and (
                generation is None
                or any(
                    j["generation"] == generation
                    and isinstance(j.get("revoked_progress_sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", j["revoked_progress_sha256"])
                    for j in journals
                )
            )
            and type(owner.get("source_fence")) is int
        )
    if proof.get("evidence_kind") == "bootstrap-journal-barrier-v1":
        return (
            proof.get("bootstrap_mutations_started") is False
            and "denied_grant" not in proof
            and "retained_revocation" not in proof
            and "revoked_bootstrap" not in proof
        )
    if proof.get("evidence_kind") == "bootstrap-retained-revocation-barrier-v1":
        owner = proof.get("owner_evidence", {})
        retained = proof.get("retained_revocation", {})
        journal = retained.get("journal", {})
        return (
            proof.get("bootstrap_mutations_started") is False
            and "denied_grant" not in proof
            and "revoked_bootstrap" not in proof
            and retained == owner.get("retained_revocation")
            and bool(retained.get("reauthorization_id"))
            and type(retained.get("source_fence")) is int
            and type(owner.get("source_fence")) is int
            and 0 < retained["source_fence"] < owner["source_fence"]
            and isinstance(retained.get("proof_sha256"), str)
            and bool(re.fullmatch(r"[a-f0-9]{64}", retained["proof_sha256"]))
            and all(
                journal.get(k) == owner.get(k)
                for k in ("operation_id", "org_id", "workspace_id")
            )
            and all(
                isinstance(journal.get(k), str)
                and re.fullmatch(r"[a-f0-9]{64}", journal[k])
                for k in ("generation", "claim", "plan_sha256")
            )
        )
    if proof.get("evidence_kind") != "bootstrap-denied-first-grant-v1":
        return False
    denial = proof.get("denied_grant", {})
    owner = proof.get("owner_evidence", {})
    return (
        proof.get("bootstrap_mutations_started") is True
        and "revoked_bootstrap" not in proof
        and proof.get("bootstrap_grants_created") is False
        and proof.get("bootstrap_journal_revoked") is True
        and denial == owner.get("denied_grant")
        and denial.get("denied_request_verified") is True
        and denial.get("absence_verified") is True
        and isinstance(denial.get("event_ids"), list)
        and len(denial["event_ids"]) == 1
        and bool(denial["event_ids"][0])
        and all(
            denial.get(k) == owner.get(k)
            for k in ("operation_id", "org_id", "workspace_id")
        )
        and all(
            isinstance(denial.get(k), str) and re.fullmatch(r"[a-f0-9]{64}", denial[k])
            for k in (
                "generation",
                "claim",
                "plan_sha256",
                "cloudtrail_sha256",
                "inventory_sha256",
            )
        )
    )


def bootstrap_recoverable(proof):
    """A retained component retry is never described as a no-effects boundary.

    Gateway independently validates the protected native journal and qualified
    source. These receipt checks bind its complete plan and ownership to the
    original settled call; they confer no new authority or attempt allowance.
    """
    from . import settled_bootstrap_repeat as repeated

    if proof.get("evidence_kind") == repeated.COMPONENTS_KIND:
        return repeated.recoverable(proof)
    kind = "bootstrap-management-components-revoked-v1"
    if proof.get("evidence_kind") != kind:
        return bootstrap_no_effects(proof)
    from .settled_bootstrap import digest, validate

    try:
        owner = proof["owner_evidence"]
        closure = validate(proof["settled_bootstrap"])
        revoked = proof["revoked_bootstrap"]
        if (
            owner.get("evidence_kind") != kind
            or owner.get("settled_bootstrap") != closure
            or revoked != owner.get("revoked_bootstrap")
            or set(revoked) != {"journals", "current_generation", "predecessor"}
            or revoked["predecessor"] is not None
            or len(revoked["journals"]) != 1
            or any(
                proof.get(k) is not True
                for k in (
                    "bootstrap_mutations_started",
                    "bootstrap_grants_created",
                    "bootstrap_journal_revoked",
                    "bootstrap_components_started",
                    "bootstrap_components_retained",
                )
            )
            or proof.get("bootstrap_admission_started") is not False
            or any(
                {
                    "attempt_extension",
                    "denied_grant",
                    "retained_revocation",
                    "precomponent_failures",
                }.intersection(v)
                for v in (proof, owner)
            )
        ):
            return False
        journal = revoked["journals"][0]
        plan, progress = journal["retained_plan"], journal["retained_progress"]
        grants = plan["grants"]
        if (
            journal["generation"] != revoked["current_generation"]
            or any(
                journal[k] != owner[k] or owner[k] != closure["operation"][k]
                for k in ("operation_id", "org_id", "workspace_id")
            )
            or owner["source_key"] != closure["call"]["idempotency_key"]
            or owner["source_fence"] != closure["call"]["fence_token"]
            or journal["plan_sha256"] != digest(plan)
            or journal["revoked_progress_sha256"] != digest(progress)
            or plan["version"] != 1
            or len(grants) != 25
            or progress["phase"] != "revoked"
            or progress["complete"] is not True
            or "retain_workspace" in progress
            or progress["component_inventory_complete"] is not True
            or progress["component_inventory_mode"] != "management"
            or len(progress["components"]) != 5
            or progress["workspace-namespace"]["phase"] != "granted"
            or not progress["workspace-namespace"]["identity"]["uid"]
            or any(
                g["generation"] != journal["generation"]
                or g["cluster_arn"] != journal["cluster_arn"]
                for g in grants
            )
            or any(
                progress[g["key"]]["phase"] != "revoked"
                or not progress[g["key"]]["identity"]
                for g in grants
                if g["key"] != "workspace-namespace"
            )
            or any(
                c["phase"] != "owned"
                or not c["identity"]["uid"]
                or c["creation"] != c["identity"]["creation"]
                for c in progress["components"].values()
            )
        ):
            return False
        return True
    except (KeyError, TypeError, ValueError, OperationRefused):
        return False


def bootstrap_journals(proof):
    """The exact immutable journal identities retained by a consumed receipt."""
    kind = proof.get("evidence_kind")
    if kind == "bootstrap-journal-barrier-v1":
        return []
    if kind == "bootstrap-denied-first-grant-v1":
        return [
            {
                k: proof["denied_grant"][k]
                for k in (
                    "operation_id",
                    "org_id",
                    "workspace_id",
                    "cluster_arn",
                    "generation",
                    "claim",
                    "plan_sha256",
                    "first_grant",
                )
            }
        ]
    if kind == "bootstrap-retained-revocation-barrier-v1":
        return [proof["retained_revocation"]["journal"]]
    if kind in {
        "bootstrap-pre-namespace-revoked-v1",
        "bootstrap-retained-namespace-v1",
        "bootstrap-acquisition-revoked-v1",
        "bootstrap-management-components-revoked-v1",
        "bootstrap-reader-components-revoked-v1",
    }:
        return proof["revoked_bootstrap"]["journals"]
    raise OperationRefused("bootstrap journal history is unavailable")


def bootstrap_revoked_predecessor(proof, prior):
    if proof.get("evidence_kind") not in {
        "bootstrap-pre-namespace-revoked-v1",
        "bootstrap-retained-namespace-v1",
        "bootstrap-acquisition-revoked-v1",
        "bootstrap-management-components-revoked-v1",
        "bootstrap-reader-components-revoked-v1",
    }:
        return
    if not bootstrap_recoverable(proof):
        raise OperationRefused("bootstrap registrar revocation is unproven")
    revoked = proof["revoked_bootstrap"]
    previous = json.loads(prior["proof"]) if prior is not None else None
    expected = (
        None
        if prior is None
        else {
            "reauthorization_id": prior["reauthorization_id"],
            "source_fence": prior["source_fence"],
            "proof_sha256": hashlib.sha256(
                json.dumps(previous, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    )
    retained = [] if previous is None else bootstrap_journals(previous)
    current = revoked["current_generation"]
    if (
        revoked["predecessor"] != expected
        or prior is not None
        and proof["owner_evidence"].get("source_key") != prior["replacement_key"]
        or expected is not None
        and not 0 < expected["source_fence"] < proof["owner_evidence"]["source_fence"]
        or current in {j["generation"] for j in retained}
        or [j for j in revoked["journals"] if j["generation"] != current] != retained
        or len(revoked["journals"]) != len(retained) + int(current is not None)
    ):
        raise OperationRefused("bootstrap revoked journal history changed")


def bootstrap_retained_predecessor(proof, prior):
    if proof.get("evidence_kind") != "bootstrap-retained-revocation-barrier-v1":
        return
    retained = proof["retained_revocation"]
    previous = json.loads(prior["proof"]) if prior is not None else {}
    if (
        prior is None
        or previous.get("evidence_kind")
        not in {
            "bootstrap-denied-first-grant-v1",
            "bootstrap-retained-revocation-barrier-v1",
        }
        or not bootstrap_no_effects(previous)
        or retained["reauthorization_id"] != prior["reauthorization_id"]
        or retained["source_fence"] != prior["source_fence"]
        or proof["owner_evidence"]["source_key"] != prior["replacement_key"]
        or retained["proof_sha256"]
        != hashlib.sha256(
            json.dumps(previous, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        or any(
            (
                previous["denied_grant"]
                if previous["evidence_kind"] == "bootstrap-denied-first-grant-v1"
                else previous["retained_revocation"]["journal"]
            ).get(k)
            != v
            for k, v in retained["journal"].items()
        )
    ):
        raise OperationRefused(
            "retained revocation differs from prior bootstrap receipt"
        )


def _complete_acquisition_extension(owner):
    """The exhausted-ten exception cannot use a partial/retained boundary."""
    revoked = owner.get("revoked_bootstrap", {})
    if not 1 <= len(revoked.get("journals", [])) <= 10:
        return False
    generation = revoked.get("current_generation")
    current = [
        j for j in revoked.get("journals", []) if j.get("generation") == generation
    ]
    if not generation or len(current) != 1:
        return False
    journal = current[0]
    prefix = journal.get("acquisition_prefix", [])
    order = (
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
        "retirement-fence-policy",
        "retirement-fence-binding",
        "cleanup-cluster-role",
        "cleanup-cluster-binding",
        "cleanup-namespace-role",
        "cleanup-namespace-binding",
        "cleanup-system-role",
        "cleanup-system-binding",
    )
    if journal.get("reader_renewal") is True:
        order = (
            order[:3]
            + (
                "reader-issuer-cluster-role",
                "reader-issuer-cluster-binding",
                "reader-issuer-namespace-role",
                "reader-issuer-namespace-binding",
            )
            + order[3:]
        )
    return (
        tuple(g.get("key") for g in prefix) == order
        and all(g.get("generation") == generation for g in prefix)
        and bool(
            re.fullmatch(r"[a-f0-9]{64}", journal.get("revoked_progress_sha256", ""))
        )
        and bool(
            journal.get("retained_namespace", {})
            .get("progress", {})
            .get("identity", {})
            .get("uid")
        )
        and bool(
            re.fullmatch(
                r"[a-f0-9]{64}",
                owner.get("source_hashes", {}).get(
                    "workspace_bootstrap/superplane_bootstrap/retirement_fence.py", ""
                ),
            )
        )
    )


def bootstrap_boundary_identity(proof):
    from .bootstrap_provenance_assumption import validate_proof

    owner = proof.get("owner_evidence", {})
    try:
        validate_proof({**proof, "owner_evidence": owner})
    except ValueError as error:
        raise OperationRefused(
            "bootstrap diagnostic assumption differs from its decision"
        ) from error
    if "settled_bootstrap" in proof or "settled_bootstrap" in owner:
        from .settled_bootstrap import validate

        closure = validate(proof.get("settled_bootstrap"))
        if (
            closure != owner.get("settled_bootstrap")
            or proof.get("evidence_kind")
            not in {
                "bootstrap-acquisition-revoked-v1",
                "bootstrap-management-components-revoked-v1",
                "bootstrap-reader-components-revoked-v1",
            }
            or any(
                closure["operation"][k] != owner.get(k)
                for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
            )
            or closure["call"]["idempotency_key"] != owner.get("source_key")
            or closure["call"]["fence_token"] != owner.get("source_fence")
            or "attempt_extension" in proof
        ):
            raise OperationRefused(
                "settled bootstrap closure differs from owner evidence"
            )
    if "attempt_extension" in proof or "attempt_extension" in owner:
        extension = proof.get("attempt_extension")
        acquisition = proof.get("evidence_kind") == "bootstrap-acquisition-revoked-v1"
        if (
            proof.get("evidence_kind")
            not in {
                "bootstrap-retained-revocation-barrier-v1",
                "bootstrap-acquisition-revoked-v1",
            }
            or not isinstance(extension, dict)
            or extension != owner.get("attempt_extension")
            or set(extension)
            != {
                "authorized_by",
                "authorization",
                "previous_limit",
                "recorded_attempts",
                "additional_attempts",
            }
            or not isinstance(extension["authorized_by"], str)
            or not extension["authorized_by"].strip()
            or not isinstance(extension["authorization"], str)
            or not 1 <= len(extension["authorization"].strip()) <= 2048
            or any(
                type(extension[k]) is not int
                for k in ("previous_limit", "recorded_attempts", "additional_attempts")
            )
            or not 1 <= extension["additional_attempts"] <= (10 if acquisition else 5)
            or not 1 <= extension["recorded_attempts"] == extension["previous_limit"]
            or (
                (
                    extension["previous_limit"],
                    extension["recorded_attempts"],
                )
                != (10, 10)
                or not _complete_acquisition_extension(owner)
                if acquisition
                else extension["previous_limit"] + extension["additional_attempts"] > 10
            )
        ):
            raise OperationRefused("bootstrap attempt extension is not owner-bound")
    if (
        not bootstrap_recoverable(proof)
        or proof.get("original_authority_expired") is not True
        or proof.get("issued_sessions_expired") is not True
        or not proof.get("owner_evidence_id")
        or proof["owner_evidence_id"] != owner.get("record_id")
        or owner.get("evidence_kind") != proof["evidence_kind"]
        or not owner.get("source_image")
        or not owner.get("source_hashes")
        or any(
            not isinstance(proof.get(key), str)
            or not re.fullmatch(r"[a-f0-9]{64}", proof[key])
            for key in ("source_artifact_id", "source_artifact_sha256")
        )
    ):
        raise OperationRefused("protected bootstrap mutation barrier unavailable")
    return owner


def saved_completion_identity(proof):
    """A new owner inspection may finish only the same retained proposal bytes."""
    observation = proof.get("preparation_boundary", {}).get("observation", {})
    files = observation.get("files")
    names = {
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-inventory.json",
        "workspace-estimate.json",
        "workspace-authorization.proposed.json",
    }
    if (
        not isinstance(files, dict)
        or set(files) != names
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in files.values()
        )
        or not isinstance(observation.get("target"), dict)
        or not observation["target"]
        or not isinstance(observation.get("terraform_version"), str)
        or not observation["terraform_version"]
        or observation.get("offline_guard_passed") is not True
    ):
        raise OperationRefused(
            "saved completion chain has no complete proposal identity"
        )
    return {key: observation[key] for key in ("files", "target", "terraform_version")}


def partial_repair_identity(proof):
    """This scope explicitly records a partial mutation and a different saved plan."""
    repair = proof.get("repair", {})
    owner = proof.get("owner_evidence", {})
    if (
        proof.get("evidence_kind") != "partial-apply-state-v1"
        or proof.get("apply_started") is not True
        or proof.get("original_authority_expired") is not True
        or proof.get("issued_sessions_expired") is not True
        or not proof.get("owner_evidence_id")
        or proof["owner_evidence_id"] != owner.get("record_id")
        or "attempt_extension" in proof
    ):
        raise OperationRefused(
            "protected partial apply source authority is unavailable"
        )
    return validate_partial_repair_plan(owner, repair)


def validate_partial_repair_plan(owner, repair):
    """Pure plan shape check; it establishes no expiry, authority or execution fact."""
    if (
        not isinstance(repair, dict)
        or set(repair)
        != {
            "artifact_id",
            "preparation_operation_id",
            "plan_file_sha256",
            "plan_json_sha256",
            "snapshot_sha256",
            "changes",
        }
        or any(
            not isinstance(repair.get(k), str)
            or not re.fullmatch(r"[a-f0-9]{64}", repair[k])
            for k in (
                "artifact_id",
                "plan_file_sha256",
                "plan_json_sha256",
                "snapshot_sha256",
            )
        )
        or not repair.get("preparation_operation_id")
        or repair["artifact_id"] == owner.get("original_artifact_id")
        or repair["preparation_operation_id"] == owner.get("operation_id")
        or not isinstance(owner.get("snapshot", {}).get("resources"), dict)
        or not owner["snapshot"]["resources"]
        or not isinstance(repair.get("changes"), dict)
        or not repair["changes"].get("preserved")
        or repair["changes"].get("replaced")
        != [
            {
                "address": "aws_eip.nat[0]",
                "id": owner["snapshot"]["resources"]
                .get("aws_eip.nat[0]", {})
                .get("id"),
                "actions": ["delete", "create"],
            }
        ]
    ):
        raise OperationRefused(
            "partial apply requires protected retained inventory "
            "and a separately approved repair plan"
        )
    snapshot_sha = hashlib.sha256(
        json.dumps(
            owner["snapshot"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    if repair["snapshot_sha256"] != snapshot_sha or set(
        repair["changes"]["preserved"]
    ) != set(owner["snapshot"]["resources"]) - {"aws_eip.nat[0]"}:
        raise OperationRefused(
            "repair does not preserve the complete protected partial inventory"
        )
    return repair


async def recover_uncredentialed(
    connection,
    *,
    principal,
    operation_id,
    expected_version,
    reauthorization_id,
    authorize,
    prove_not_started,
    recovery_scope="uncredentialed-planning",
    settled_bootstrap=False,
):
    """Trusted owner callbacks, never worker-supplied absence or approval claims.

    The source authority must have expired, including its maximum runtime. The
    owner proves either no credential delivery or an independent planning start
    barrier with all possible issued sessions expired. A dispatch advisory lock
    and operation row lock serialize dispatch, provider calls and recovery.
    """
    from .execution import CallOutcome, audit, reconcile
    from .execution_plan import admitted_steps
    from .interrupted_plan import valid_proof as valid_interrupted_plan
    from .store import _record

    if (
        recovery_scope
        not in {
            "uncredentialed-planning",
            "unstarted-planning",
            "interrupted-planning",
            "saved-plan-completion",
            "partial-apply-repair",
            "unstarted-bootstrap",
        }
        or settled_bootstrap
        and recovery_scope != "unstarted-bootstrap"
        or connection.is_in_transaction()
        or "workspace:provision" not in principal.permissions
    ):
        raise OperationRefused("original requester and owned transaction required")
    lock = f"harness-provider-dispatch:{operation_id}"
    if not await connection.fetchval(
        "SELECT pg_try_advisory_lock(hashtextextended($1,0))", lock
    ):
        raise OperationRefused("original provider dispatch is still active")
    try:
        async with connection.transaction():
            operation = await connection.fetchrow(
                (
                    "SELECT * FROM harness_operations WHERE operation_id=$1 AND "
                    "org_id=$2 AND workspace_id=$3 FOR UPDATE"
                ),
                operation_id,
                principal.org_id,
                principal.workspace_id,
            )
            paid = await connection.fetchrow(
                (
                    "SELECT * FROM harness_approval_consumption "
                    "WHERE operation_id=$1 FOR UPDATE"
                ),
                operation_id,
            )
            if (
                operation is None
                or paid is None
                or paid["requester"] != principal.subject
            ):
                raise OperationRefused("original requester admission unavailable")
            record = _record(operation)
            receipts = await receipts_for(connection, operation_id)
            previous = next(
                (r for r in receipts if r["reauthorization_id"] == reauthorization_id),
                None,
            )
            if previous is not None:
                if (
                    previous["expected_version"],
                    previous["requester"],
                    previous["recovery_scope"],
                ) != (expected_version, principal.subject, recovery_scope):
                    raise OperationRefused(
                        "recovery decision differs from retained receipt"
                    )
                if (
                    bool(json.loads(previous["proof"]).get("settled_bootstrap"))
                    != settled_bootstrap
                ):
                    raise OperationRefused(
                        "recovery boundary differs from retained receipt"
                    )
                await active_call_rows(connection, operation_id, [])
                return dict(previous)
            if receipts and recovery_scope == "uncredentialed-planning":
                raise OperationRefused(
                    "original operation already used its credential recovery"
                )
            if receipts and recovery_scope == "partial-apply-repair":
                raise OperationRefused(
                    "partial apply repair already has its single immutable receipt"
                )
            lease = await connection.fetchrow(
                (
                    "SELECT * FROM harness_operation_leases WHERE "
                    "operation_id=$1 FOR UPDATE"
                ),
                operation_id,
            )
            now = await connection.fetchval("SELECT clock_timestamp()")
            if (
                operation["state"] != ("unknown" if settled_bootstrap else "running")
                or operation["version"] != expected_version
                or operation["cancel_requested_at"] is not None
                or operation["cleanup_required"]
                or paid["reservation_state"] != "confirmed"
                or paid["plan_digest"] != record.plan_digest
                or (paid["org_id"], paid["workspace_id"])
                != (principal.org_id, principal.workspace_id)
                or lease is None
                or not settled_bootstrap
                and (
                    lease["closed_at"] is not None
                    or lease["holder"] is None
                    or lease["runtime_deadline"] is None
                    or lease["runtime_deadline"] > now
                    or lease["expires_at"] > now
                )
            ):
                raise OperationRefused(
                    "original execution is not eligible for credential recovery"
                )
            steps = admitted_steps(record)
            calls = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
                operation_id,
            )
            calls = await active_call_rows(connection, operation_id, calls)
            if len(steps) != 1 or len(calls) != 1:
                raise OperationRefused(
                    "credential recovery requires one original admitted call"
                )
            call, step = calls[0], steps[0]
            closure = None
            if settled_bootstrap:
                from .settled_bootstrap import read

                closure = await read(connection, operation, lease, call)
            elif recovery_scope in {"partial-apply-repair", "unstarted-bootstrap"}:
                await verify_partial_source_claim(connection, lease, call)
            if recovery_scope == "interrupted-planning":
                from .interrupted_plan import require_initial_step

                require_initial_step(record, step, receipts)
            if recovery_scope == "unstarted-bootstrap" and (
                record.action != "provision"
                or step.provider != "superplane-lifecycle"
                or step.step_id != "bootstrap-workspace"
                or step.operation_kind != "bootstrap-workspace"
            ):
                raise OperationRefused(
                    "bootstrap recovery requires the original bootstrap step"
                )
            if recovery_scope == "partial-apply-repair" and (
                record.action != "provision"
                or step.provider != "superplane-lifecycle"
                or step.step_id != "apply-infrastructure"
                or step.operation_kind != "apply-infrastructure"
            ):
                raise OperationRefused(
                    "partial repair is restricted to the original infrastructure apply"
                )
            if (
                call["idempotency_key"]
                != await effective_step_key(connection, record, step)
                or (call["provider"], call["operation_kind"], call["target"])
                != (step.provider, step.operation_kind, step.target)
                or not settled_bootstrap
                and (call["stage"] != "intended" or call["outcome"] is not None)
                or call["provider_ref"] is not None
                or (call["org_id"], call["workspace_id"], call["job_id"])
                != (record.org_id, record.workspace_id, record.job_id)
                or recovery_scope not in {"partial-apply-repair", "unstarted-bootstrap"}
                and (
                    call["fence_token"] != lease["fence_token"]
                    or call["attempt_id"] != lease["attempt_id"]
                )
            ):
                raise OperationRefused("original provider intent cannot be retried")
            for table in (
                "harness_provider_report",
                "harness_allocation_resource",
                "harness_recovery_settlements",
            ):
                if settled_bootstrap and table == "harness_recovery_settlements":
                    continue  # Exact acknowledged retain receipt is bound above.
                if await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM " + table + " WHERE operation_id=$1)",
                    operation_id,
                ):
                    raise OperationRefused(
                        "provider or settlement evidence prevents credential recovery"
                    )
            deadline = await authorize(dict(operation), dict(paid))
            proof = await prove_not_started(dict(operation), dict(lease), dict(call))
            deadline = min(deadline, await authorize(dict(operation), dict(paid)))
            if proof.get("settled_bootstrap") != closure:
                raise OperationRefused(
                    "protected settled source differs from current closure"
                )
            if settled_bootstrap and "runtime_amendment" not in proof:
                raise OperationRefused(
                    "settled bootstrap requires an explicit fresh runtime"
                )
            from .bootstrap_runtime_authorization import runtime_authorization

            runtime_seconds, runtime_deadline = runtime_authorization(
                proof, paid["max_runtime_seconds"], recovery_scope
            )
            if runtime_deadline is not None and runtime_deadline != deadline:
                raise OperationRefused(
                    "bootstrap runtime differs from the fresh authorization deadline"
                )
            if recovery_scope == "unstarted-bootstrap":
                bootstrap_revoked_predecessor(proof, receipts[-1] if receipts else None)
                owner = bootstrap_boundary_identity(proof)
                bootstrap_retained_predecessor(
                    proof, receipts[-1] if receipts else None
                )
                if (
                    any(
                        owner.get(k) != operation[k]
                        for k in (
                            "operation_id",
                            "org_id",
                            "workspace_id",
                            "plan_digest",
                        )
                    )
                    or owner.get("source_fence") != call["fence_token"]
                    or owner.get("source_key") != call["idempotency_key"]
                    or proof["source_artifact_id"] != step.target
                ):
                    raise OperationRefused("bootstrap proof names another intent")
            if recovery_scope == "partial-apply-repair":
                partial_repair_identity(proof)
                owner = proof["owner_evidence"]
                if (
                    any(
                        owner.get(k) != operation[k]
                        for k in (
                            "operation_id",
                            "org_id",
                            "workspace_id",
                            "plan_digest",
                        )
                    )
                    or owner.get("source_fence") != call["fence_token"]
                    or owner.get("source_version") != expected_version
                    or owner.get("source_key") != call["idempotency_key"]
                    or owner.get("original_artifact_id") != step.target
                ):
                    raise OperationRefused(
                        "partial inventory belongs to another source execution"
                    )
            if (
                not isinstance(proof, dict)
                or proof.get("original_authority_expired") is not True
                or not (
                    recovery_scope == "uncredentialed-planning"
                    and proof.get("evidence_kind") == "credential-delivery-barrier-v1"
                    and proof.get("credentials_delivered") is False
                    or recovery_scope == "unstarted-planning"
                    and proof.get("evidence_kind") == "planning-start-barrier-v1"
                    and proof.get("planning_started") is False
                    and proof.get("issued_sessions_expired") is True
                    and proof.get("owner_evidence_id")
                    or recovery_scope == "interrupted-planning"
                    and valid_interrupted_plan(proof)
                    or recovery_scope == "saved-plan-completion"
                    and proof.get("evidence_kind") == "saved-plan-finalization-v1"
                    and proof.get("planning_started") is True
                    and proof.get("apply_started") is False
                    and proof.get("issued_sessions_expired") is True
                    and proof.get("owner_evidence_id")
                    and proof.get("preparation_boundary", {}).get("kind")
                    == "saved-plan-finalization-v1"
                    and proof.get("preparation_boundary", {})
                    .get("observation", {})
                    .get("offline_guard_passed")
                    is True
                    or recovery_scope == "unstarted-bootstrap"
                    and bootstrap_recoverable(proof)
                    and proof.get("issued_sessions_expired") is True
                    and proof.get("owner_evidence_id")
                    and proof.get("source_artifact_id") == step.target
                    or recovery_scope == "partial-apply-repair"
                    and proof.get("evidence_kind") == "partial-apply-state-v1"
                    and proof.get("apply_started") is True
                    and proof.get("issued_sessions_expired") is True
                    and proof.get("owner_evidence_id")
                )
                or deadline <= datetime.now(UTC) + timedelta(seconds=900)
            ):
                raise OperationRefused(
                    "fresh authority or protected credential refusal proof unavailable"
                )
            if recovery_scope == "saved-plan-completion":
                completions = [
                    item
                    for item in receipts
                    if item["recovery_scope"] == "saved-plan-completion"
                ]
                if completions:
                    identity = saved_completion_identity(proof)
                    for item in completions:
                        prior_proof = json.loads(item["proof"])
                        if (
                            saved_completion_identity(prior_proof) != identity
                            or prior_proof["owner_evidence_id"]
                            == proof["owner_evidence_id"]
                        ):
                            raise OperationRefused(
                                "repeated completion requires fresh proof "
                                "of the identical saved plan"
                            )
            if recovery_scope == "unstarted-bootstrap":
                # Every stopped attempt needs its own protected source proof and
                # human decision. Preserve the complete receipt chain and the
                # unchanged apply artifact. An explicit owner-bound extension
                # is checked against the current lease and human below.
                for item in receipts:
                    prior = json.loads(item["proof"])
                    if (
                        item["recovery_scope"] != recovery_scope
                        or prior["owner_evidence_id"] == proof["owner_evidence_id"]
                        or any(
                            prior[key] != proof[key]
                            for key in ("source_artifact_id", "source_artifact_sha256")
                        )
                    ):
                        raise OperationRefused(
                            "bootstrap retry requires fresh proof "
                            "of the unchanged apply"
                        )
            ceiling = lease["max_attempts"]
            extension = proof.get("attempt_extension")
            if extension is not None:
                extension_cap = 10
                additional_cap = 5
                if recovery_scope == "unstarted-bootstrap":
                    bootstrap_boundary_identity(proof)
                    if proof["evidence_kind"] == "bootstrap-acquisition-revoked-v1":
                        extension_cap = 20
                        additional_cap = 10
                if (
                    recovery_scope not in {"unstarted-planning", "unstarted-bootstrap"}
                    or not proof.get("owner_evidence_id")
                    or not isinstance(extension, dict)
                    or type(extension.get("additional_attempts")) is not int
                    or not 1 <= extension["additional_attempts"] <= additional_cap
                    or type(extension.get("previous_limit")) is not int
                    or type(extension.get("recorded_attempts")) is not int
                    or extension["previous_limit"] != ceiling
                    or extension["recorded_attempts"] != lease["attempts"]
                    or extension.get("authorized_by") != principal.subject
                    or not isinstance(extension.get("authorization"), str)
                    or not extension["authorization"].strip()
                    or ceiling + extension["additional_attempts"] > extension_cap
                ):
                    raise OperationRefused(
                        "protected attempt extension differs from authorization"
                    )
                ceiling += extension["additional_attempts"]
            if lease["attempts"] >= ceiling:
                raise OperationRefused(
                    "original operation exhausted its recorded attempt ceiling"
                )
            if runtime_deadline is not None:
                await audit(
                    connection,
                    operation_id=operation_id,
                    org_id=record.org_id,
                    workspace_id=record.workspace_id,
                    attempt_id=call["attempt_id"],
                    fence_token=lease["fence_token"] + 1,
                    event="bootstrap-runtime-extended",
                    actor=principal.subject,
                    allowed=True,
                    detail=(
                        f"Fresh bootstrap runtime {runtime_seconds}s; original "
                        f"{paid['max_runtime_seconds']}s admission retained; "
                        f"receipt {reauthorization_id}"
                    ),
                )
            # The owner established either the start barrier or the saved-plan
            # completion boundary. Neither permits replay of arbitrary planning.
            # Preserve this original key/producer; the next worker uses a distinct,
            # receipt-bound key, never a deleted or rewritten original intent.
            if settled_bootstrap:
                # Only this human-authorized boundary may supersede UNKNOWN.
                # The unchanged original row, including its unknown outcome,
                # is retained in the immutable receipt's source_intent/proof.
                tag = await connection.execute(
                    "UPDATE harness_provider_call_intent SET stage='reconciled', "
                    "outcome=$2,updated_at=now() WHERE idempotency_key=$1 "
                    "AND stage='unresolved' AND outcome=$3 AND provider_ref IS NULL",
                    call["idempotency_key"],
                    "failed: Fresh decision and positive native bootstrap "
                    "revocation; prior unknown retained in receipt",
                    call["outcome"],
                )
                if tag != "UPDATE 1":
                    raise OperationRefused("settled bootstrap intent changed")
            else:
                await reconcile(
                    connection,
                    idempotency_key=call["idempotency_key"],
                    outcome=CallOutcome.FAILED,
                    detail=(
                        "Protected owner verified revoked temporary authority and "
                        "retained "
                        "namespace intent; normal bootstrap must observe and "
                        "validate the "
                        "namespace before continuing; all history is retained"
                        if recovery_scope == "unstarted-bootstrap"
                        and proof.get("evidence_kind")
                        in {
                            "bootstrap-retained-namespace-v1",
                            "bootstrap-acquisition-revoked-v1",
                        }
                        else "Protected owner verified native revocation before any "
                        "namespace intent; all bootstrap journals and created-grant "
                        "history are retained"
                        if recovery_scope == "unstarted-bootstrap"
                        and proof.get("evidence_kind")
                        == "bootstrap-pre-namespace-revoked-v1"
                        else "Protected owner verified the first bootstrap grant "
                        "was denied; "
                        "native revocation completed with no grants; journal retained"
                        if recovery_scope == "unstarted-bootstrap"
                        and proof.get("evidence_kind")
                        == "bootstrap-denied-first-grant-v1"
                        else "Protected owner qualified the later unstarted bootstrap; "
                        "the predecessor's positively revoked journal is unchanged"
                        if recovery_scope == "unstarted-bootstrap"
                        and proof.get("evidence_kind")
                        == "bootstrap-retained-revocation-barrier-v1"
                        else "Protected owner qualified the bootstrap journal barrier; "
                        "expired authority and absent protected mutation journals "
                        "permit a retry"
                        if recovery_scope == "unstarted-bootstrap"
                        else "Protected owner observed retained resources "
                        "from failed partial apply; "
                        "a separately approved new repair plan may finish this "
                        "allocation"
                        if recovery_scope == "partial-apply-repair"
                        else "Protected owner verified the retained initial proposal; "
                        "only final validation remains"
                        if recovery_scope == "saved-plan-completion"
                        else "Protected owner qualified interrupted read-only planning;"
                        "retained files and the abandoned lock are bound to this retry"
                        if recovery_scope == "interrupted-planning"
                        else "Protected owner proved initial planning did not start "
                        "before source authority expired"
                    ),
                )
            replacement = (
                "operation-retry:"
                + hashlib.sha256(
                    (call["idempotency_key"] + ":" + reauthorization_id).encode()
                ).hexdigest()
            )
            receipt = await connection.fetchrow(
                "INSERT INTO harness_provider_retries("
                "operation_id,org_id,workspace_id,plan_digest,"
                "reauthorization_id,original_approval_id,requester,approved_by,"
                "expected_version,restored_version,"
                "original_key,replacement_key,source_fence,source_attempt_id,"
                "source_intent,proof,recovery_scope) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) "
                "RETURNING *",
                operation_id,
                record.org_id,
                record.workspace_id,
                record.plan_digest,
                reauthorization_id,
                paid["approval_id"],
                principal.subject,
                paid["approved_by"],
                expected_version,
                expected_version + 1,
                call["idempotency_key"],
                replacement,
                call["fence_token"],
                call["attempt_id"],
                json.dumps(dict(call), default=str),
                json.dumps(proof, default=str),
                recovery_scope,
            )
            if settled_bootstrap:
                tag = await connection.execute(
                    "UPDATE harness_operation_leases SET "
                    "closed_at=NULL,closed_reason=NULL,"
                    "closed_holder=NULL,closed_attempt_id=NULL WHERE operation_id=$1 "
                    "AND fence_token=$2 AND closed_at=$3 AND "
                    "closed_reason='recovery: unknown'",
                    operation_id,
                    lease["fence_token"],
                    lease["closed_at"],
                )
                if tag != "UPDATE 1":
                    raise OperationRefused("settled bootstrap closure changed")
            await connection.execute(
                "UPDATE harness_operation_leases SET "
                "fence_token=fence_token+1,holder=NULL,attempt_id=NULL,"
                "expires_at=NULL,runtime_deadline=NULL,acquired_at=NULL,"
                "max_attempts=$2,updated_at=clock_timestamp() WHERE operation_id=$1",
                operation_id,
                ceiling,
            )
            tag = await connection.execute(
                "UPDATE harness_operations SET "
                "state='running',version=version+1,detail='Original "
                "operation continuation authorized from protected owner evidence',"
                "updated_at=clock_timestamp() WHERE operation_id=$1 "
                "AND version=$2 AND state=$3",
                operation_id,
                expected_version,
                "unknown" if settled_bootstrap else "running",
            )
            if tag != "UPDATE 1":
                raise OperationRefused("recovery operation version changed")
            if extension is not None:
                await audit(
                    connection,
                    operation_id=operation_id,
                    org_id=record.org_id,
                    workspace_id=record.workspace_id,
                    attempt_id=call["attempt_id"],
                    fence_token=lease["fence_token"] + 1,
                    event="execution-attempt-limit-extended",
                    actor=principal.subject,
                    allowed=True,
                    detail=(
                        f"Explicit protected authorization: limit "
                        f"{lease['max_attempts']} to {ceiling}; "
                        f"{lease['attempts']} recorded attempts retained; "
                        f"receipt {reauthorization_id}"
                    ),
                )
            await audit(
                connection,
                operation_id=operation_id,
                org_id=record.org_id,
                workspace_id=record.workspace_id,
                attempt_id=call["attempt_id"],
                fence_token=lease["fence_token"] + 1,
                event=(
                    "bootstrap-settled-reopened"
                    if settled_bootstrap
                    else "provider-credential-recovery"
                    if recovery_scope == "uncredentialed-planning"
                    else "provider-planning-recovery"
                ),
                actor=principal.subject,
                allowed=True,
                detail=(
                    "Original intent reconciled from protected owner "
                    "evidence; fresh decision and retry key retained"
                ),
            )
            return dict(receipt)
    finally:
        await connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended($1,0))", lock
        )
