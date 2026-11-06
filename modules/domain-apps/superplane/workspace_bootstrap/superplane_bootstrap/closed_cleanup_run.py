"""Closed-operation composition: fresh credentials stay in the service process."""

from .closed_cleanup_permit import require
from .component_journal import _contains, component_identity, component_key
from .component_recovery import KIND, recover_components
from .registry import registration_lock


def committed_result(
    journal, registry, kube, revalidate, verify_absence, read_component
):
    """Independently re-observe a committed cleanup after a lost service reply."""
    with registration_lock(journal.store, journal.target.workspace_id):
        qualified = revalidate()
        plan, progress = journal.read_locked()
        if progress["phase"] != "revoked":
            return None
        require(progress.get("complete") is True and plan == qualified["retained_plan"])
        require(
            not journal.store.execute(
                "SELECT workspace_id FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
                {"workspace_id": journal.target.workspace_id},
            )
        )
        for spec in plan["grants"]:
            original = qualified["retained_progress"][spec["key"]]
            current = progress[spec["key"]]
            require(current["identity"] == original["identity"])
            if spec["lifetime"] == "resource":
                require(
                    current == original and kube.observe(spec) == original["identity"]
                )
            else:
                require(current["phase"] == "revoked")
                if spec["kind"] == "kubernetes":
                    require(kube.observe(spec) is None)
        for key, component in qualified.get(
            "inherited_components", progress["components"]
        ).items():
            observed = read_component(component["desired"])
            require(isinstance(observed, dict) and component_key(observed) == key)
            require(
                _contains(observed, component["desired"])
                and component_identity(observed) == component["identity"]
            )
        require(verify_absence(plan) is True)
        revalidate()
        return {
            "evidence_kind": KIND,
            "generation": journal.generation,
            "plan_sha256": qualified["plan_sha256"],
            "revoked": True,
            "claim_released": True,
            "journal_retained": True,
            "namespace_retained": True,
            "components_retained": True,
        }


def run_closed_cleanup(
    journal,
    registry,
    eks,
    kube,
    verifier_access,
    *,
    revalidate,
    verify_absence,
    read_component,
    teardown_only=False,
):
    # The Gateway holds the original operation lock around this whole command.
    # Native SQL cleanup MUST commit before publishing its completion to DDB.
    # Nesting recover_components in another transaction would break crash recovery.
    revalidate()
    state = verifier_access.journal.read()
    if teardown_only:
        return verifier_access.teardown(abandon="result" not in state)
    if state["phase"] in {
        "complete",
        "original-cleanup-verified",
        "verifier-disassociate-intent",
        "verifier-delete-intent",
    }:
        result = state["result"]
        require(result["generation"] == journal.generation)
        with registration_lock(journal.store, journal.target.workspace_id):
            plan, progress = journal.read_locked()
            require(progress["phase"] == "revoked" and progress.get("complete") is True)
            require(
                not journal.store.execute(
                    "SELECT workspace_id FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
                    {"workspace_id": journal.target.workspace_id},
                )
            )
            require(verify_absence(plan) is True)
    else:
        with registration_lock(journal.store, journal.target.workspace_id):
            revalidate()
            verifier_access.acquire()
            if hasattr(kube, "wait_ready"):
                kube.wait_ready()
        result = committed_result(
            journal, registry, kube, revalidate, verify_absence, read_component
        )
        if result is None:
            result = recover_components(
                journal,
                registry,
                eks,
                kube,
                revalidate=revalidate,
                verify_absence=verify_absence,
                read_component=read_component,
            )
        verifier_access.record_verified(result)
    teardown = verifier_access.teardown()
    require(teardown["phase"] == "complete")
    revalidate()
    return {
        **result,
        "verifier_access_removed": True,
        "cleanup_permit_id": verifier_access.permit["permit_id"],
    }
