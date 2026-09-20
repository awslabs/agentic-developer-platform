"""Fixture-scoped fault adapters and service-boundary outcome assertions.

An environment may not provide isolated event/tick injection. Such a case is
NOT_RUN, never a mock fallback. A backend must implement the named operation;
there is intentionally no shell command, shared queue purge or controller stop.
"""

from tests.e2e.orchestration.fixtures import readback
from .definitions import FAULTS, CONTROLS
from .http import Unsupported


RESOURCE_KINDS = {
    "wait-exit": {"qualification-worker"},
    "worker-loss": {"qualification-worker"},
    "tick-restart": {"qualification-isolated-tick", "qualification-flow"},
    "missed-wakeup": {"qualification-isolated-tick", "qualification-flow"},
    "duplicate-events": {
        "qualification-event-source",
        "qualification-isolated-tick",
        "qualification-flow",
    },
    "out-of-order": {
        "qualification-event-source",
        "qualification-isolated-tick",
        "qualification-flow",
    },
    "competing-launches": {"qualification-event-source", "qualification-flow"},
    "timeout-after-success": {
        "qualification-provider-proxy",
        "qualification-isolated-tick",
        "qualification-flow",
    },
    "failed-ci": {"qualification-branch"},
    "stale-image": {"qualification-runtime"},
    "failed-deploy": {"qualification-runtime"},
    "revocation": {"qualification-authority"},
    "fanout-budget": {"qualification-flow"},
    "repair-budget": {"qualification-flow"},
    "halt-stop": {"qualification-worker"},
    "human-refusal": {"qualification-flow"},
}


def inject(criterion, *, fixture_id, inventory, config, providers):
    name = criterion.id.split(".", 1)[1]
    record = inventory.get(fixture_id)
    if record.kind not in RESOURCE_KINDS.get(name, set()):
        raise ValueError(
            "fault cannot target a shared controller or unrelated resource kind"
        )
    if not record.intended_identity.startswith(inventory.qualification_id + "/"):
        raise ValueError("fault target is not namespaced to this inventory")
    provider = providers.get(record.kind)
    if provider is None or not callable(getattr(provider, "inject", None)):
        raise Unsupported(f"{name}: no isolated injection provider")
    if (
        name == "tick-restart"
        and getattr(provider, "isolation", None) != "new-interpreter"
    ):
        raise ValueError("restart may terminate only the newly launched interpreter")
    readback(
        inventory,
        provider,
        fixture_id,
        config.ownership_tags(inventory.qualification_id),
    )
    # Persist the injection intent before the effect. A crash does not erase the
    # intervention or cause an automatic repeat of an ambiguous mutation.
    journal = inventory.path.parent / ("fault-" + name + ".json")
    import json

    with journal.open("x") as stream:
        json.dump(
            {"criterion": criterion.id, "fixture_id": fixture_id, "status": "started"},
            stream,
        )
        stream.flush()
        import os

        os.fsync(stream.fileno())
    return provider.inject(name, record.observed_resource_id)


def assert_outcome(name, observed):
    """Assert facts read from the actual boundary; never accept `passed: true`.

    The backend preserves native responses in hashed artifacts and normalizes
    just these facts. Missing keys are failure, not benign default values.
    """
    if name == "failed-ci":
        head = observed["injection"]["head_sha"]
        assert any(
            c["name"] in observed["required_checks"]
            and c["head_sha"] == head
            and c["conclusion"] == "failure"
            for c in observed["checks"]
        )
        after = observed["after"]
        assert (
            after["pull_request"]["head"]["sha"] == head
            and after["pull_request"]["merged"] is False
        )
        assert after["execution"]["phase"] in {"awaiting_review", "repairing"}
        assert after["execution"]["mutating_owner_count"] <= 1
        assert after["execution"]["coordinator_retriggers"] == 0
        assert all(
            n["state"] != "passed"
            for n in after["graph"]["nodes"]
            if n["node_ref"] == "first"
        )
        return
    if name == "human-refusal":
        decision = observed["decisions"]
        assert decision["actor_kind"] == "human" and decision["kind"] == "gate_rejected"
        assert decision["to_state"] == "rejected_at_gate"
        nodes = {n["node_ref"]: n for n in observed["graph"]["nodes"]}
        assert decision["node_id"] == nodes["refuse"]["id"]
        assert nodes["refuse"]["state"] == "rejected_at_gate"
        assert (
            nodes["successor"]["state"] == "pending"
            and nodes["successor"]["run_id"] is None
        )
        assert observed["after"]["executions"] == []
        return
    before, after = observed["before"], observed["after"]
    assert after["flow_id"] == before["flow_id"]
    assert after["policy_id"] == before["policy_id"]
    assert after["claim_generation"] >= before["claim_generation"]
    assert after["mutating_owner_count"] <= 1
    assert after["coordinator_retriggers"] == 0
    assert len(after["effect_ids"]) == len(set(after["effect_ids"]))
    if name in {"wait-exit", "worker-loss", "tick-restart", "missed-wakeup"}:
        assert (
            before["pending_operation"]
            or (name == "worker-loss" and before.get("active_run_id"))
            or (name == "wait-exit" and before.get("next_check_at"))
        )
        assert after["pending_operation"] == before["pending_operation"] or before[
            "pending_operation"
        ] in {a["operation_key"] for a in after.get("actions", [])}
        assert after["progress_revision"] > before["progress_revision"]
        assert after["terminal"] or after["explicit_block"]
        if name == "wait-exit":
            assert (
                observed["injection"]["status"] == "complete"
                and observed["injection"]["liveness"] == "exited"
            )
        if name == "tick-restart":
            assert (
                observed["injection"]["before_process_id"]
                != observed["injection"]["after_process_id"]
            )
            assert observed["injection"]["isolated"] is True
        if name == "missed-wakeup":
            from tests.e2e.orchestration.report import timestamp

            assert observed["injection"]["missed_isolated_poll"] is True
            assert observed["injection"]["shared_scheduler_unchanged"] is True
            assert timestamp(observed["injection"]["resumed_at"]) > timestamp(
                observed["injection"]["scheduled_at"]
            )
    elif name in {"duplicate-events", "out-of-order", "competing-launches"}:
        assert len(observed["injection"]["delivery_ids"]) >= 2
        assert after["effect_ids"] == before["effect_ids"]
        assert after["progress_revision"] >= before["progress_revision"]
        if name == "competing-launches":
            deliveries = observed["injection"]["delivery_ids"]
            assert {d["lane"] for d in deliveries} == {"engine_flow", "direct_dispatch"}
            assert all(
                d["disposition"] in {"duplicate", "conflict", "blocked"}
                for d in deliveries
            )
    elif name == "timeout-after-success":
        assert observed["injection"]["transport_outcome"] == "timeout"
        remote = observed["remote"]
        assert len(remote["runs"]) == 1
        operation = observed["injection"]["remote"]["operation_key"]
        matching = [a for a in after["actions"] if a["operation_key"] == operation]
        assert len(matching) == 1 and matching[0]["status"] == "succeeded"
        assert matching[0]["dispatch_started"] is True
        assert matching[0]["workflow_run_id"] == remote["runs"][0]["id"]
        assert (
            remote["runs"][0]["display_title"]
            == "ADP deployment " + remote["correlation"]
        )
        assert remote["runs"][0]["head_sha"] == matching[0]["source_revision"]
    elif name in {"failed-ci", "stale-image", "failed-deploy"}:
        assert after["explicit_block"] and not after["accepted"]
        if name == "failed-ci":
            assert observed["checks"]["required_conclusion"] == "failure"
            assert after["merged"] is False
        elif name == "stale-image":
            assert (
                observed["runtime"]["actual_revision"]
                != observed["runtime"]["required_revision"]
            )
        else:
            assert observed["deployment"]["conclusion"] == "failure"
    elif name == "revocation":
        assert observed["injection"]["revoked_at"] <= after["observed_at"]
        assert after["boundary_status"] in {403, 404, 409}
        assert after["effect_ids"] == before["effect_ids"]
    elif name in {"fanout-budget", "repair-budget"}:
        assert (
            after["explicit_block"] and after["allowance_id"] == before["allowance_id"]
        )
        assert after["effect_ids"] == before["effect_ids"]
        assert observed["cost"]["reserved_plus_settled"] >= observed["policy"]["limit"]
        assert len(observed["cost"]["descendant_run_ids"]) >= 2
    elif name == "halt-stop":
        assert observed["graph"]["halted"] is True
        assert observed["worker"]["termination_confirmed"] is True
        assert observed["worker"]["status_code"] != 501
        assert after["effect_ids"] == before["effect_ids"]
    else:
        raise Unsupported(f"no supported outcome assertion for {name}")


def execute_case(criterion, session):
    """One executable adapter per fixed fault/control, with no synthetic fallback."""
    name = criterion.id.split(".", 1)[1]
    observed = session.exercise(name)
    assert_outcome(name, observed)
    return observed


CASES = {
    criterion.id: criterion
    for criterion in FAULTS + CONTROLS
    if criterion.id != "A6-4.tenant"
}
