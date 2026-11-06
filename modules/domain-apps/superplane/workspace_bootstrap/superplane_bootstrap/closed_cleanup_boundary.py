"""Shared forward-only checks for a qualified closed cleanup."""

import copy
import json
from datetime import datetime, timezone

from .closed_cleanup_permit import require


def expired(descriptor, value):
    require(
        value["original_authority_expired"] is True
        and value["issued_sessions_expired"] is True
    )
    require(
        datetime.fromisoformat(value["earliest_owner_evidence_at"])
        <= datetime.now(timezone.utc)
    )
    for key in (
        "operation_id",
        "source_key",
        "source_fence",
        "source_principal",
        "settled_bootstrap",
    ):
        require(value[key] == descriptor[key])


def cleanup_revalidator(qualification, descriptor, *, read_snapshot, read_expiry):
    """Keep the qualified starting point immutable while proving forward cleanup.

    The snapshot reader retains the native registration reservation until cleanup
    succeeds and accepts only this current generation's revocation progress.
    Every callback reads expiry, all journals and expiry again; no result cache
    supplies current authority. Native cleanup independently locks/checks progress.
    """
    from harness_jobs import settled_bootstrap_repeat as repeat

    expected = copy.deepcopy(qualification)
    closure = repeat.validate(expected["settled_bootstrap"])
    plan, initial = expected["retained_plan"], expected["retained_progress"]
    keys = {g["key"] for g in plan["grants"]}
    markers = {
        k: v for k, v in initial.items() if k not in keys | {"phase", "complete"}
    }

    def revalidate():
        first = read_expiry()
        expired(descriptor, first)
        snapshot = read_snapshot()
        row = repeat.journals_bound(closure, snapshot["journals"])
        require(
            all(
                row[k] == expected[k]
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
        require(json.loads(row["plan_json"]) == plan)
        progress = json.loads(row["progress_json"])
        require(progress["phase"] in {"revoking", "revoked"})
        complete = progress["phase"] == "revoked"
        require(
            row["revoked"] is complete
            and (not complete or progress.get("complete") is True)
        )
        require(
            set(progress)
            == keys | set(markers) | {"phase"} | ({"complete"} if complete else set())
        )
        require(all(progress[k] == v for k, v in markers.items()))
        for key in keys:
            before, after = initial[key], progress[key]
            require(
                set(after) == set(before)
                and after.get("identity") == before.get("identity")
            )
            allowed = (
                {before["phase"]}
                if before["phase"] in {"adopted", "revoked"}
                else {before["phase"], "revoke_intent", "revoked"}
            )
            require(after["phase"] in allowed)
            require(
                not complete
                or key == "workspace-namespace"
                or after["phase"] == "revoked"
            )
        require(first == read_expiry())
        return copy.deepcopy(expected)

    revalidate()
    return revalidate
