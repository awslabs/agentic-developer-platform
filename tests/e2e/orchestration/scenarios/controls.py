"""Owned gate-only fixtures for accepted-policy withdrawal and shared allowance."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import time

from tests.e2e.orchestration.fixtures import FixtureRequest, provision, readback
from tests.e2e.orchestration.report import Intervention
from .definitions import DEFINITION_HASH
from .http import Unsupported
from .native_probe import execute_source
from .runtime import read_runtime


def native(session, record, accepted, mode):
    if time.monotonic() >= session.client.deadline:
        raise Unsupported("control probe duration exhausted")
    target = session.manifest.runtime["engine"]
    runtime = read_runtime(session.client, target)
    permitted = {session.config.versions["engine"]} | {
        r["actual_revision"] for r in session.runtime_observations.values()
    }
    if runtime["actual_revision"] not in permitted:
        raise Unsupported("control probe gateway revision changed")
    return execute_source(
        session.client,
        target,
        Path(__file__).with_name("control_process.py").read_text(),
        dict(
            mode=mode,
            org_id=session.config.org_ref,
            flow_id=record.observed_resource_id,
            qualification_id=session.inventory.qualification_id,
            definition_hash=DEFINITION_HASH,
            plan_version=accepted["plan_version"],
            plan_hash=accepted["plan_hash"],
        ),
        runtime=runtime,
    )


def create(session, name):
    from .delivery import FlowProvider

    slug = session.inventory.qualification_id + "-" + name
    policy = deepcopy(session.manifest.execution_policy)
    policy["evaluation_acceptance"] = {}
    if name == "allowance":
        limit = session.manifest.allowance_fixture_usd
        if limit is None:
            raise Unsupported("accepted allowance fixture cap is missing")
        policy["limits"]["max_spend_usd"] = limit
    nodes = [
        dict(
            address=f"{slug}/controls/probe/gate-{i}",
            kind="gate",
            title="Non-dispatching control fixture",
        )
        for i in range(3)
    ]
    plan = dict(
        flow_slug=slug,
        title=f"[{slug}] Qualification control",
        org_id=session.config.org_ref,
        spec_revision=DEFINITION_HASH,
        execution_policy=policy,
        nodes=nodes,
        edges=[
            dict(from_address=a["address"], to_address=b["address"])
            for a, b in zip(nodes, nodes[1:])
        ],
    )
    artifact = session.evidence.save(
        name + "-plan", plan, "reviewed-harness:fixed-control-plan"
    )
    session.interventions.append(
        Intervention(
            at=artifact.observed_at,
            actor="qualification-harness",
            kind="fault",
            target=name,
            evidence=artifact,
        )
    )
    provider = FlowProvider(session.client, session.inventory, plan)
    record = provision(
        session.inventory,
        session.config,
        provider,
        FixtureRequest(
            name, provider.kind, session.inventory.qualification_id + "/" + name
        ),
    )
    readback(
        session.inventory,
        provider,
        name,
        session.config.ownership_tags(session.inventory.qualification_id),
    )
    return record, provider.response, plan


def exercise(name, session):
    if not session.manifest.native_faults:
        raise Unsupported("native control probes are disabled in the accepted manifest")
    if name in {"fanout-budget", "repair-budget"}:
        if "allowance" not in session.fault_observations:
            record, accepted, _ = create(session, "allowance")
            value = native(session, record, accepted, "budget")
            session.fault_observations["allowance"] = {
                "injection": {"flow_id": record.observed_resource_id},
                "cost": value,
                "policy": {"limit": value["limit"], "policy_id": value["policy_id"]},
                "after": value,
            }
        return session.fault_observations["allowance"]
    record, accepted, plan = create(session, "revocation")
    before = native(session, record, accepted, "policy")
    if before["policy"] is None or before["refusal"] is not None:
        raise Unsupported(
            "fixture did not have accepted policy authority before withdrawal"
        )
    amendment = deepcopy(plan)
    amendment["execution_policy"] = None
    artifact = session.evidence.save(
        "revocation-intent", amendment, "harness:planned-policy-withdrawal"
    )
    session.interventions.append(
        Intervention(
            at=artifact.observed_at,
            actor=session.config.identity_ref,
            kind="fault",
            target="revocation",
            evidence=artifact,
        )
    )
    status, result = session.client.request(
        "POST",
        f"/orchestration/flows/{record.observed_resource_id}/amendments?reason=qualification-policy-withdrawal",
        body=amendment,
    )
    if status not in {200, 201}:
        raise Unsupported(f"policy withdrawal returned HTTP {status}")
    session.planned_decision_ids = getattr(session, "planned_decision_ids", set()) | {
        result["decision_id"]
    }
    session.evidence.save("revocation-response", result, "gateway:amendment-decision")
    after = native(session, record, result, "policy")
    return {
        "before": before,
        "after": after,
        "injection": result,
        "policy": before["policy"],
    }


def assert_control_outcome(name, observed):
    after = observed["after"]
    if name == "revocation":
        before = observed["before"]
        assert before["policy"] and before["refusal"] is None
        assert before["flow_id"] == after["flow_id"]
        assert after["plan_version"] > before["plan_version"]
        assert before["plan_version"] in after["previous_policy_versions"]
        assert after["policy"] is None
        assert after["refusal"]["permitted"] is False
        assert after["refusal"]["reason"] == "stale_policy_version"
        assert after["executions"] == before["executions"] == []
        return
    assert after["executions"] == []
    assert len(set(after["node_ids"])) == 3
    assert Decimal(after["settled_usd"]) + Decimal(after["reserved_usd"]) == Decimal(
        after["limit"]
    )
    assert after["reserved_usd"] == after["after_usd"]
    assert Decimal(after["cancellation"]["remaining_usd"]) == 0
    decision = after["fanout" if name == "fanout-budget" else "repair"]
    assert decision["admitted"] is False and decision["degraded"] is False
    assert decision["allowance_id"] == after["allowance_id"]
