"""One separately owned worker; signed abort plus independent exit readback."""

from copy import deepcopy
from decimal import Decimal
import json
import time
from types import SimpleNamespace
from uuid import uuid4

from tests.e2e.orchestration.fixtures import FixtureRequest, provision, readback
from tests.e2e.orchestration.report import Intervention
from .definitions import DEFINITION_HASH
from .http import Unsupported
from .kubernetes import ScopedKubernetes, KubernetesUnavailable
from .native_probe import execute_source
from .providers import IssueProvider, MARKER
from .runtime import read_runtime
from .workers import WorkerProvider

CAPABILITY_SOURCE = """import json
from src.activity.control_service import SUPPORTED_ACTIONS
print("ADP_Q2_RESULT:"+json.dumps({"supported_actions":sorted(SUPPORTED_ACTIONS)}),flush=True)
"""
STOP_INSTRUCTION = (
    "This is an isolated qualification stop fixture. Do not edit code, create a "
    "branch/PR, launch children, deploy, or change any service. Run one local "
    "python command that sleeps for 120 seconds, then finish. The qualification "
    "operator may abort this exact worker while it waits. Do not retry or restart."
)


class StopIssueProvider(IssueProvider):
    kind = "qualification-stop-issue"

    def create(self, *, intended_identity, ownership_tags, idempotency_token):
        qid = ownership_tags["adp:qualification-id"]
        if intended_identity != qid + "/stop-issue":
            raise Unsupported("invalid stop fixture issue")
        actor = self.client.get("/user", github=True)
        metadata = dict(
            identity=intended_identity,
            ownership_tags=ownership_tags,
            correlation=idempotency_token,
            creator_id=actor["id"],
        )
        status, issue = self.client.request(
            "POST",
            self.path + "/issues",
            github=True,
            body={
                "title": f"[{qid}] Disposable worker stop qualification",
                "body": STOP_INSTRUCTION
                + "\n"
                + MARKER
                + json.dumps(metadata, sort_keys=True)
                + " -->",
            },
        )
        if status != 201 or issue["user"]["id"] != actor["id"]:
            raise Unsupported("stop issue creation not verified; reconcile inventory")
        return str(issue["number"])


def exercise(session):
    from .delivery import FlowProvider

    if not session.manifest.halt_stop:
        raise Unsupported("separate stop fixture is disabled in the accepted manifest")
    target = session.manifest.runtime["engine"]
    runtime = read_runtime(session.client, target)
    if runtime["actual_revision"] not in {session.config.versions["engine"]} | {
        r["actual_revision"] for r in session.runtime_observations.values()
    }:
        raise Unsupported("stop probe gateway changed")
    capabilities = execute_source(
        session.client, target, CAPABILITY_SOURCE, {}, runtime=runtime
    )
    artifact = session.evidence.save(
        "stop-capability", capabilities, "deployed:activity.control_service"
    )
    if "abort" not in capabilities["supported_actions"]:
        error = Unsupported(
            "deployed abort is unavailable; owning contract https://github.com/aws-e/adp/issues/3963"
        )
        error.evidence = artifact
        raise error
    # Never add an extra paid worker when total room is unknown. Its accepted
    # policy uses only the unspent remainder and permits one attempt.
    from .cleanup import terminal_flow

    terminal_flow(session.client, session.flow_id, session.inventory.qualification_id)
    cost = session.client.get(f"/orchestration/flows/{session.flow_id}/cost")
    if cost["status"] != "known" or cost["partial"]:
        raise Unsupported("remaining qualification allowance is unknown")
    runs = sum(
        len((n.get("execution_history") or {}).get("runs", []))
        for n in session.client.get(f"/orchestration/flows/{session.flow_id}")["nodes"]
    )
    if runs + 1 > session.config.max_runs:
        raise Unsupported("stop fixture would exceed accepted run count")
    remaining = Decimal(str(session.config.max_usd)) - Decimal(str(cost["amount_usd"]))
    if remaining <= 0:
        raise Unsupported("no qualification allowance remains for stop fixture")
    qid = session.inventory.qualification_id
    provider = StopIssueProvider(session.client)
    issue = provision(
        session.inventory,
        session.config,
        provider,
        FixtureRequest("stop-issue", provider.kind, qid + "/stop-issue"),
    )
    slug = qid + "-stop"
    policy = deepcopy(session.manifest.execution_policy)
    policy["evaluation_acceptance"] = {}
    policy["limits"].update(
        max_spend_usd=float(remaining),
        max_attempts_per_node=1,
        max_concurrent_actions=1,
    )
    plan = dict(
        flow_slug=slug,
        title=f"[{slug}] Separate worker stop",
        org_id=session.config.org_ref,
        spec_revision=DEFINITION_HASH,
        execution_policy=policy,
        nodes=[
            dict(
                address=slug + "/controls/stop/worker",
                kind="story",
                title="Disposable stop worker",
                issue_ref=issue.observed_resource_id,
            )
        ],
        edges=[],
    )
    flow_provider = FlowProvider(session.client, session.inventory, plan)
    record = provision(
        session.inventory,
        session.config,
        flow_provider,
        FixtureRequest("stop", flow_provider.kind, qid + "/stop"),
    )
    local = SimpleNamespace(
        config=session.config,
        manifest=session.manifest,
        client=session.client,
        inventory=session.inventory,
        flow_id=record.observed_resource_id,
        accepted=flow_provider.response,
    )
    worker = WorkerProvider(local)
    worker.purpose = "worker-stop"
    path = f"/orchestration/flows/{local.flow_id}"
    while time.monotonic() < session.client.deadline:
        graph = session.client.get(path)
        node = next(n for n in graph["nodes"] if n["node_ref"] == "worker")
        if (node.get("activity") or {}).get("liveness") == "live":
            worker.node = node
            break
        time.sleep(session.manifest.poll_seconds)
    else:
        raise Unsupported("stop fixture did not produce a live worker")
    record = provision(
        session.inventory,
        session.config,
        worker,
        FixtureRequest("worker-stop", worker.kind, qid + "/worker-stop"),
    )
    readback(
        session.inventory, worker, "worker-stop", session.config.ownership_tags(qid)
    )
    resource = json.loads(record.observed_resource_id)
    _, invocation = worker._binding(resource)
    run_id = invocation["run_id"]
    state_path = f"/orchestration/runs/{run_id}/state"
    before = session.client.get(state_path)
    if (
        not before["available"]
        or not before["capabilities"]["abort"]
        or before["generation"] is None
    ):
        raise Unsupported("owned worker abort capability unavailable")
    command_id = str(uuid4())
    intent = session.evidence.save(
        "stop-intent",
        {
            "command_id": command_id,
            "run_id": run_id,
            "generation": before["generation"],
            "pod_uid": resource["pod_uid"],
        },
        "harness:planned-stop",
    )
    session.interventions.append(
        Intervention(
            at=intent.observed_at,
            actor=session.config.identity_ref,
            kind="fault",
            target="halt-stop",
            evidence=intent,
        )
    )
    status, response = session.client.request(
        "POST",
        f"/orchestration/runs/{run_id}/abort",
        body={
            "command_id": command_id,
            "reason": "Predeclared isolated qualification stop",
        },
    )
    if status not in {200, 202}:
        error = Unsupported(
            f"actual abort returned HTTP {status}; graph halt cannot prove stop"
        )
        error.evidence = session.evidence.save(
            "stop-response",
            {"status": status, "response": response},
            "gateway:signed-abort",
        )
        raise error
    acknowledged = response.get("command_id") == command_id and response.get(
        "command_status"
    ) in {"applied", "delivered"}
    while time.monotonic() < session.client.deadline:
        graph = session.client.get(path)
        invocation = session.client.get(
            "/me/agent-invocations/" + resource["invocation_id"]
        )
        state = session.client.get(state_path)
        if state.get("generation") == before["generation"]:
            acknowledged |= any(
                c["command_id"] == command_id
                and c["action"] == "abort"
                and c["status"] in {"applied", "delivered"}
                for c in state["commands"]
            )
        if invocation["liveness"] == "exited":
            break
        time.sleep(session.manifest.poll_seconds)
    kube_target = session.manifest.runtime["worker"]
    try:
        pod = ScopedKubernetes(session.client, kube_target.cluster).get(
            f"/api/v1/namespaces/{resource['namespace']}/pods/{resource['pod_name']}"
        )
        gone = pod["metadata"]["uid"] == resource["pod_uid"] and pod["status"][
            "phase"
        ] in {"Succeeded", "Failed"}
    except KubernetesUnavailable as exc:
        if exc.status_code != 404:
            raise
        gone = True
        pod = {"status_code": 404, "expected_uid": resource["pod_uid"]}
    return {
        "injection": {
            "status": status,
            "response": response,
            "command_id": command_id,
            "acknowledged": acknowledged,
        },
        "graph": graph,
        "worker": {
            "invocation": invocation,
            "pod": pod,
            "termination_confirmed": gone and invocation["liveness"] == "exited",
        },
    }


def assert_stop(observed):
    assert observed["injection"]["status"] in {200, 202}
    assert observed["injection"]["acknowledged"] is True
    assert any(
        n["node_ref"] == "worker" and n["state"] == "halted"
        for n in observed["graph"]["nodes"]
    )
    assert observed["worker"]["termination_confirmed"] is True
    assert observed["worker"]["invocation"]["liveness"] == "exited"
    assert observed["worker"]["invocation"]["status"] == "aborted"
