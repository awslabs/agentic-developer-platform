#!/usr/bin/env python3
"""Decide whether a fixture STAGE may run, from the shared ledger plus live observation.

Root, requirement 2: "implement gateway -> edge/handoff -> worker stages. Step 10
currently needs an edge URL but edge creation needs the gateway; rerunning step 10
refuses existing objects. Wire a real staged lifecycle/run-all and shared ledger with
UID-safe cleanup."

THE DEADLOCK, EXACTLY
---------------------
`10-create-fixture.sh --worker-job` requires a control endpoint before it creates
anything (it is refused up front, deliberately -- a worker without one bootstraps
against production). #5836's edge cannot produce that endpoint until the fixture
Deployment and Service exist, because its `create-fixture-alb.sh` needs a Service to
put an ALB in front of and its Terraform gate refuses to plan against an ALB that is
not there.

So the only order that can work is gateway -> edge -> worker. But the script was
all-or-nothing: a first invocation without `--worker-job` created the gateway and
stopped, and the second invocation (now able to supply the endpoint) hit
`w2_check_absent` and refused every object the first one had just created. The
sequence the runbook describes was not executable. Not "awkward" -- not executable.

WHY "JUST SKIP WHAT EXISTS" IS THE WRONG FIX
--------------------------------------------
The absence check is not bureaucracy. It is what stops this tooling from adopting a
resource it did not create and then authorising its deletion -- the failure root
already found sitting in the account. Relaxing it to "exists, therefore fine" would
make every later stage adopt whatever wears the right name, and the ledger would
then license tearing it down.

What distinguishes a legitimate second stage from adoption is not the name. It is the
UID: this run recorded the server-assigned uid of everything it created, so a later
stage can require that what is standing there NOW is the same object instance. Three
outcomes that all look identical to a name check are separated here:

  * present, in the ledger, uid matches      -> this run's object. Proceed.
  * present, but NOT in the ledger           -> someone else's. Refuse (adoption).
  * present, in the ledger, uid DIFFERENT    -> a replacement built after this run
                                                recorded its own. Refuse: the edge
                                                the endpoint describes fronts an
                                                object this run cannot account for.
  * recorded, but ABSENT now                 -> the gateway was deleted between
                                                stages. Refuse: the receipt's ALB now
                                                terminates at nothing, so a worker
                                                would bootstrap into a void and the
                                                run would file the resulting errors
                                                as measurements.
  * unreadable                               -> refuse. "Could not check" is not
                                                "checked and fine", the same rule
                                                `w2_check_absent` already applies.

WHAT THIS MODULE DOES NOT DO
----------------------------
No cloud calls, no kubectl, no mutation. The caller observes; this decides. That
split is what makes the decision testable without a cluster, and it is why the
observations arrive as a file rather than being gathered here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The lifecycle, in the only order that can work.
#
#   gateway : policies, queue, fixture Deployment + Service.  Creates no worker and
#             requires no endpoint -- so it can run BEFORE #5836's edge exists.
#   worker  : the protected Job, against the endpoint the edge now publishes.
#             Requires the gateway stage's objects to be present AND this run's.
#   all     : both in one invocation. Only possible when an endpoint is already
#             known (a previous run's edge, re-verified), and kept because it is
#             what every existing caller means.
STAGES = ("gateway", "worker", "all")

# Which stage creates what, named by ROLE -- the job an object does in the fixture --
# rather than by Kubernetes kind.
#
# WHY NOT BY KIND (5810412904)
# ----------------------------
# The first revision keyed the expectation map by kind. The fixture has TWO
# NetworkPolicies: one on the fixture gateway in the gateway namespace, and one on the
# fixture worker in the agent namespace (render_fixture.render_network_policies emits
# both). Keyed by kind they collide on "NetworkPolicy", so the map could only ever hold
# one of them -- and the one it held was the gateway's. Root ran the real gate with the
# exact four-object shape the script supplied plus a ledger-recorded worker policy that
# was never observed, and it returned [] (proceed).
#
# The consequence is the worst available: the worker-side policy is what confines the
# one pod in this fixture holding PROTECTED AUTHORITY. Deleted between stages, or
# replaced by something else wearing the same name, and the worker was created anyway.
#
# Roles cannot collide, and each role carries its own full kind/namespace/name, so two
# policies in two namespaces are two separate required objects.
GATEWAY_POLICY = "gateway_policy"
WORKER_POLICY = "worker_policy"
DEPLOYMENT = "deployment"
SERVICE = "service"
WORKER_JOB = "worker_job"

_GATEWAY_ROLES = (GATEWAY_POLICY, WORKER_POLICY, DEPLOYMENT, SERVICE)
_WORKER_ROLES = (WORKER_JOB,)

ROLES = _GATEWAY_ROLES + _WORKER_ROLES

# The worker Job is opt-in: `--stage gateway` and a deliberately gateway-only fixture
# both legitimately never name one. Every OTHER role must be named by any caller whose
# stage touches it -- an unnamed required object is a refusal, not a skip, because that
# is exactly how the worker policy went unchecked.
_OPTIONAL_CREATE_ROLES = (WORKER_JOB,)

PRESENT = "present"
ABSENT = "absent"
UNREADABLE = "unreadable"


class StageGateError(Exception):
    """A stage may not proceed. Never a warning: the next act is creation."""


def object_key(kind: str, name: str, namespace: str) -> str:
    """The observation key. Kind is included because a uid is only unique per object,
    not per name -- a Pod and a Job can share a name in one namespace."""
    return f"{kind}/{namespace}/{name}"


def creates(stage: str) -> tuple[str, ...]:
    """The roles a stage creates, so the caller knows what to require absent."""
    if stage == "gateway":
        return _GATEWAY_ROLES
    if stage == "worker":
        return _WORKER_ROLES
    if stage == "all":
        return _GATEWAY_ROLES + _WORKER_ROLES
    raise StageGateError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")


def requires_present(stage: str) -> tuple[str, ...]:
    """The roles a stage requires to ALREADY exist, created by this same run.

    Only the worker stage has prerequisites, and they are exactly the gateway stage's
    objects: the endpoint it is handed describes an ALB that terminates at that Service
    and those pods, and BOTH policies are what confine what is about to be created.

    The worker policy is in this list for the same reason the Service is. It is not
    merely a neighbour of the worker -- it is the object that stops the pod holding
    protected authority from reaching anything beyond the fixture. A worker created
    while it is missing is an unconfined protected worker, and the run would then file
    whatever it reached as a measurement.
    """
    if stage == "worker":
        return _GATEWAY_ROLES
    return ()


# ---------------------------------------------------------------------------
# the nonce comes from the LEDGER, never from a flag
# ---------------------------------------------------------------------------
def nonce_from_ledger(ledger: dict[str, Any], *, supplied: str | None = None) -> str:
    """This run's nonce, read from the shared ledger.

    A later stage needs the nonce the earlier stage used -- it is the ownership
    evidence on everything without a uid (the queue tag, #5836's SSM path, its state
    key). Asking the operator to paste it back is the same defect as accepting an
    endpoint on their word: a typo produces a *plausible* nonce, and then the stages
    disagree about which run they are. So it is read from the ledger the earlier
    stage wrote, and a supplied value becomes a CROSS-CHECK that must agree.
    """
    recorded = (ledger.get("run_nonce") or "").strip()
    if not recorded:
        raise StageGateError(
            "the shared ledger records no run_nonce, so a later stage cannot recover the "
            "ownership evidence the earlier stage used. Without it the queue tag, the "
            "fixture's SSM path and #5836's state key cannot be shown to be this run's."
        )
    if supplied and supplied.strip() != recorded:
        raise StageGateError(
            f"--resume-nonce was given {supplied!r} but the shared ledger records "
            f"{recorded!r}. Refusing both rather than preferring one: a nonce that "
            "disagrees with the ledger means the stages are describing two different "
            "runs, and the later one would tag its resources so the earlier one's "
            "teardown never finds them."
        )
    return recorded


def queue_from_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    """The queue the gateway stage created, for the worker stage to reuse.

    The worker's env carries this URL. Re-deriving it by name would work right up
    until it silently named a queue from another run, so it is taken from the record
    of what this run actually created.
    """
    queues = ledger.get("queues") or []
    mine = [q for q in queues if q.get("created_by_this_run") and q.get("url")]
    if not mine:
        raise StageGateError(
            "the shared ledger records no queue created by this run, so the worker stage "
            "has no fixture queue to point at. The gateway stage creates it; if that stage "
            "did not complete, the worker stage has no fixture to join."
        )
    if len(mine) > 1:
        raise StageGateError(
            f"the shared ledger records {len(mine)} queues created by this run. One run "
            "creates one fixture queue; two means two runs share this ledger path, and "
            "choosing between them would be a guess."
        )
    return mine[0]


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def _why_required(role: str) -> str:
    """Why a missing prerequisite matters, per role.

    Stated per role rather than as one sentence about the Service because the roles fail
    in different ways, and an operator reading a refusal needs the one that applies. The
    worker policy's reason is the one that was missing entirely.
    """
    return {
        GATEWAY_POLICY: (
            "it is what confines the fixture gateway, so without it the fixture is "
            "reachable beyond the flows under test."
        ),
        WORKER_POLICY: (
            "it is what confines the pod about to be created -- the ONE pod in this "
            "fixture holding protected authority. Creating that worker while its policy "
            "is missing produces an unconfined protected worker, and whatever it reaches "
            "would be recorded as a measurement of the isolated fixture."
        ),
        DEPLOYMENT: (
            "the control endpoint describes an edge that terminates at those pods, so "
            "without them the worker bootstraps into a void."
        ),
        SERVICE: (
            "the control endpoint describes an ALB placed in front of that Service, so "
            "without it a worker created now would bootstrap into a void and the run "
            "would file the resulting connection errors as measurements."
        ),
    }.get(role, "the stage declares it as a prerequisite.")


def k8s_uid_from_ledger(
    ledger: dict[str, Any], *, kind: str, name: str, namespace: str,
) -> str:
    """The uid THIS RUN recorded for one object, for a later stage to check against.

    Added for the ALB policy mutation, which must prove the policy it is about to
    replace is the object the gateway stage created. Re-reading the uid from the
    cluster would defeat the purpose entirely: the live uid always matches itself, so
    a replacement policy would pass. The expectation has to come from the record of
    what this run created, which is this ledger.

    Refuses rather than returning "" for anything it cannot answer, for the reason
    every other lookup here does: an empty uid flows into the mutation's
    ``--uid`` and the downstream check treats a missing expectation as a refusal --
    but it would be refusing for an obscure reason, several steps from the cause.
    """
    entry = _ledger_index(ledger).get(object_key(kind, name, namespace))
    if entry is None:
        raise StageGateError(
            f"the shared ledger records no {kind}/{name} in {namespace} created by this run, "
            "so there is no uid to hold the live object against. Without it, a same-named "
            "policy from another run would be mutated as though it were this run's."
        )
    uid = entry.get("uid") or ""
    if not uid:
        raise StageGateError(
            f"the ledger's entry for {kind}/{name} in {namespace} carries no uid, so this "
            "run's ownership of it was never proven. Refusing to supply an empty expectation: "
            "it must not become a mutation of an object nobody can show is ours."
        )
    if not entry.get("created_by_this_run"):
        raise StageGateError(
            f"the ledger records {kind}/{name} in {namespace} but not as created by this run. "
            "An adopted object must not be mutated: this run neither composed its rules nor "
            "is authorised to tear it down."
        )
    return uid


def _ledger_index(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for entry in ledger.get("k8s") or []:
        kind, name, ns = entry.get("kind"), entry.get("name"), entry.get("namespace")
        if not (kind and name and ns):
            # An anonymous entry cannot be matched against an observation. It is a
            # real problem, but it is `ownership.py validate`'s to report -- silently
            # indexing it under a partial key would make a later lookup succeed for
            # the wrong reason.
            continue
        index[object_key(kind, name, ns)] = entry
    return index


def stage_problems(
    *,
    stage: str,
    ledger: dict[str, Any],
    observations: dict[str, Any],
    expected: dict[str, tuple[str, str, str]],
) -> list[str]:
    """Every reason this stage may not proceed. Empty list means proceed.

    ``expected`` maps ROLE -> (kind, name, namespace): the caller's own naming, so this
    module never reconstructs object names (which would be a second copy of the naming
    scheme, free to drift from the one that created them).

    Keyed by role rather than by kind because the fixture has two NetworkPolicies -- one
    per namespace -- and a kind-keyed map can hold only one of them. Root proved the
    consequence by execution (5810412904): with the worker policy recorded in the ledger
    but never observed, the gate returned "proceed", so a deleted or replaced
    worker-side policy admitted the protected worker anyway.

    ``observations`` maps ``object_key()`` -> {"status": present|absent|unreadable,
    "uid": ..., "detail": ...}. A MISSING observation is a problem, not a pass: it
    means the caller did not look, and "did not look" must never read as "absent".
    """
    if stage not in STAGES:
        raise StageGateError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")

    unknown = sorted(set(expected) - set(ROLES))
    if unknown:
        raise StageGateError(
            f"the caller named objects for unknown roles {unknown}; expected roles are "
            f"{list(ROLES)}. Refusing rather than ignoring them: a role this module does "
            "not know about is an object it would never check, which is the defect that "
            "let the worker policy go unverified."
        )

    index = _ledger_index(ledger)
    problems: list[str] = []

    def observed(role: str) -> tuple[str, dict[str, Any], str]:
        kind, name, ns = expected[role]
        key = object_key(kind, name, ns)
        obs = observations.get(key)
        if obs is None:
            problems.append(
                f"{kind}/{name} in {ns} (the fixture's {role}) was never observed. A stage "
                "decision needs a reply about each object; an absent observation is not an "
                "absent object."
            )
            return key, {}, name
        return key, obs, name

    # --- prerequisites: must be present, and must be OURS ---------------------
    for role in requires_present(stage):
        if role not in expected:
            problems.append(
                f"the {stage} stage requires the fixture's {role}, but the caller supplied no "
                "kind/name/namespace for it. It cannot be checked, so the stage cannot "
                "proceed -- an object nobody named is an object nobody verified."
            )
            continue
        key, obs, name = observed(role)
        if not obs:
            continue
        status = obs.get("status")
        recorded = index.get(key)
        if status == UNREADABLE:
            problems.append(
                f"could not determine whether {key} exists: {obs.get('detail', '<no detail>')}. "
                "Refusing: an unreadable cluster is not a confirmed one."
            )
            continue
        if status == ABSENT:
            problems.append(
                f"{key} does not exist, but the {stage} stage needs it: {_why_required(role)} "
                + (
                    "This run recorded creating it, so it was deleted between stages, and "
                    "the run would file whatever follows as a measurement."
                    if recorded
                    else "Run the gateway stage first."
                )
            )
            continue
        if status != PRESENT:
            problems.append(
                f"{key} was observed with an unrecognised status {status!r}. Refusing "
                "rather than guessing which of present/absent was meant."
            )
            continue
        if recorded is None:
            problems.append(
                f"{key} exists but THIS RUN'S LEDGER DOES NOT RECORD IT. Refusing to build "
                "on it: it belongs to something else, and continuing would both measure "
                "another run's fixture and let this run's teardown delete it. Use a new "
                "--run-id, or point --ledger at the ledger of the run that created it."
            )
            continue
        live_uid = (obs.get("uid") or "").strip()
        recorded_uid = (recorded.get("uid") or "").strip()
        if not live_uid:
            problems.append(
                f"{key} exists but no metadata.uid was read back for it, so it cannot be "
                "shown to be the instance this run created. A name match is not identity."
            )
            continue
        if not recorded_uid:
            problems.append(
                f"{key} is recorded in the ledger without a uid, so the recorded entry "
                "cannot identify an instance. It must not be used to admit the live object."
            )
            continue
        if live_uid != recorded_uid:
            problems.append(
                f"{key} exists with uid {live_uid} but this run created uid {recorded_uid}. "
                "That is a DIFFERENT object wearing the same name -- the one this run built "
                "was replaced between stages. Refusing: the endpoint describes the "
                "replacement, the ledger would delete it as though this run made it, and "
                "the composition it was reviewed against is gone."
            )

    # --- what this stage creates: must be absent ------------------------------
    for role in creates(stage):
        if role not in expected:
            if role in _OPTIONAL_CREATE_ROLES:
                # The worker Job legitimately goes unnamed: `--stage gateway` does not
                # create one, and a deliberately gateway-only fixture never does.
                continue
            problems.append(
                f"the {stage} stage creates the fixture's {role}, but the caller supplied no "
                "kind/name/namespace for it, so it was never checked for absence. Refusing: "
                "creating an object nobody confirmed absent either fails or adopts, and the "
                "ledger would then license deleting something this run did not make."
            )
            continue
        key, obs, name = observed(role)
        if not obs:
            continue
        status = obs.get("status")
        if status == UNREADABLE:
            problems.append(
                f"could not determine whether {key} exists: {obs.get('detail', '<no detail>')}. "
                "Refusing to create: an unreadable cluster is not an empty one."
            )
        elif status == PRESENT:
            recorded = index.get(key)
            problems.append(
                f"{key} already exists and the {stage} stage would create it. "
                + (
                    "This run recorded creating it, so this stage has already run. Re-running "
                    "it would not be a resume: the object would be created a second time or "
                    "adopted, and either way the ledger's uid would stop matching. Continue "
                    "with the next stage instead."
                    if recorded
                    else "This run did not create it, so it cannot be owned or torn down by "
                    "this run. Use a different --run-id."
                )
            )
        elif status != ABSENT:
            problems.append(
                f"{key} was observed with an unrecognised status {status!r}. Refusing "
                "rather than guessing."
            )

    return problems


# ---------------------------------------------------------------------------
# the handoff document — what makes the sequence executable rather than prose
# ---------------------------------------------------------------------------
def handoff_document(
    *,
    ledger: dict[str, Any],
    run_id: str,
    namespace: str,
    agent_namespace: str,
    service_name: str,
    deployment_name: str,
    evidence_dir: str,
    ledger_path: str,
) -> dict[str, Any]:
    """Everything #5836's edge needs from this run, written from the LEDGER.

    The runbook's version of this was prose with `<angle brackets>`: the operator had
    to transcribe a nonce, a Service name, a Deployment name and a ledger path into
    four more commands. Every one of those is a value whose whole purpose is to bind
    two runs together, so transcription is the one thing that must not be in the loop.

    Written from the ledger rather than from this process's variables, so the document
    cannot describe a run the ledger does not.
    """
    nonce = nonce_from_ledger(ledger)
    queue = queue_from_ledger(ledger)
    return {
        "schema": "w2-stage-handoff/v1",
        "stage_completed": "gateway",
        "next_stage": "worker",
        "run_id": ledger.get("run_id") or run_id,
        "run_nonce": nonce,
        "account_id": ledger.get("account_id"),
        "region": ledger.get("region"),
        "ledger": ledger_path,
        "evidence_dir": evidence_dir,
        "fixture": {
            "namespace": namespace,
            "agent_namespace": agent_namespace,
            "deployment": deployment_name,
            "service": service_name,
            "queue_url": queue.get("url"),
        },
        # The uids, so #5836's steps and the worker stage are talking about the same
        # object instances this run created -- not merely the same names.
        "created_uids": {
            object_key(e["kind"], e["name"], e["namespace"]): e.get("uid")
            for e in (ledger.get("k8s") or [])
            if e.get("kind") and e.get("name") and e.get("namespace")
        },
        "_next": [
            "The fixture gateway is STILL RUNNING and was NOT torn down: the next stage "
            "needs it. That means a control-flag-ON gateway and a live queue until step 5.",
            "1. #5836 creates the fixture's own internal ALB, recording its uid in THIS ledger:",
            f"     modules/gateway/infra/fixture-edge/scripts/create-fixture-alb.sh \\",
            f"       --run-id {run_id} --run-nonce <run_nonce from this file> \\",
            f"       --ledger {ledger_path} --namespace {namespace} --service {service_name}",
            "2. #5836 applies the edge (init/plan/apply), then hands the fixture its secret:",
            f"     fixture-lifecycle.sh handoff --ledger {ledger_path} \\",
            f"       --namespace {namespace} --fixture-deployment {deployment_name}",
            "3. Export ALL its outputs -- this is what binds the endpoint to this run.",
            "   Not `-json ownership` and not the apply's ownership.json: those carry the",
            "   bindings but NOT the endpoint, which is a separate top-level output.",
            f"     terraform output -json > {evidence_dir}/edge-outputs.json",
            "4. Then the worker stage, which reads the nonce from the ledger (do not retype it):",
            f"     ./10-create-fixture.sh --stage worker --run-id {run_id} \\",
            f"       --ledger {ledger_path} --evidence-dir {evidence_dir} \\",
            f"       --worker-job --edge-receipt {evidence_dir}/edge-outputs.json",
            "5. ALWAYS, whether or not the worker stage runs:",
            f"     ./90-cleanup-ledger.sh {ledger_path} {evidence_dir}",
        ],
        "_why_not_torn_down": (
            "A gateway-stage run that tore down its own fixture could never be followed by "
            "a worker stage -- the edge would front a deleted Service. The fixture is "
            "therefore deliberately left alive, which means a flag-ON gateway and a live "
            "queue are running until step 5. That is the cost of a staged lifecycle and it "
            "is stated here rather than discovered later."
        ),
    }


# ---------------------------------------------------------------------------
# CLI — the seam the shell calls
# ---------------------------------------------------------------------------
def _load_json(path: str | Path, what: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise StageGateError(f"no {what} at {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StageGateError(f"the {what} at {p} is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise StageGateError(f"the {what} at {p} must be a JSON object")
    return doc


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("gate", help="may this stage proceed?")
    p.add_argument("--stage", required=True, choices=list(STAGES))
    p.add_argument("--ledger", required=True)
    p.add_argument("--observations", required=True,
                   help="JSON: object_key -> {status, uid, detail}")
    p.add_argument("--expect", action="append", default=[],
                   metavar="ROLE=KIND/NAMESPACE/NAME",
                   help="the caller's own naming for each object, repeatable. ROLE is one "
                        f"of {', '.join(ROLES)}; the object is identified by its full "
                        "kind/namespace/name because two roles can share a kind (the "
                        "fixture has two NetworkPolicies, in two namespaces)")

    p = sub.add_parser("nonce", help="this run's nonce, from the ledger")
    p.add_argument("--ledger", required=True)
    p.add_argument("--supplied", default=None, help="optional cross-check; must agree")

    p = sub.add_parser("queue-url", help="the queue this run created, from the ledger")
    p.add_argument("--ledger", required=True)

    p = sub.add_parser("k8s-uid", help="the uid this run recorded for one object")
    p.add_argument("--ledger", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--namespace", required=True)

    p = sub.add_parser("handoff", help="write the gateway->edge handoff document")
    p.add_argument("--ledger", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--agent-namespace", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--deployment", required=True)
    p.add_argument("--evidence-dir", required=True)
    p.add_argument("--out", required=True)

    args = parser.parse_args(argv)

    try:
        ledger = _load_json(args.ledger, "shared fixture ledger")

        if args.cmd == "nonce":
            print(nonce_from_ledger(ledger, supplied=args.supplied))
            return 0

        if args.cmd == "queue-url":
            print(queue_from_ledger(ledger)["url"])
            return 0

        if args.cmd == "k8s-uid":
            print(k8s_uid_from_ledger(
                ledger, kind=args.kind, name=args.name, namespace=args.namespace))
            return 0

        if args.cmd == "handoff":
            doc = handoff_document(
                ledger=ledger, run_id=args.run_id, namespace=args.namespace,
                agent_namespace=args.agent_namespace, service_name=args.service,
                deployment_name=args.deployment, evidence_dir=args.evidence_dir,
                ledger_path=args.ledger,
            )
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
            for line in doc["_next"]:
                print(line, file=sys.stderr)
            print(str(out))
            return 0

        if args.cmd == "gate":
            expected: dict[str, tuple[str, str, str]] = {}
            for spec in args.expect:
                role, sep, rest = spec.partition("=")
                parts = rest.split("/")
                if not (role and sep and len(parts) == 3 and all(p for p in parts)):
                    raise StageGateError(
                        f"--expect {spec!r} must be ROLE=KIND/NAMESPACE/NAME. A partial "
                        "specification would leave an object unchecked, and the object is "
                        "identified by all three parts because two roles can share a kind."
                    )
                kind, namespace, name = parts
                if role in expected:
                    raise StageGateError(
                        f"--expect names the role {role!r} twice. Refusing rather than "
                        "preferring one: the second would silently replace the first and "
                        "one of the two objects would go unchecked."
                    )
                expected[role] = (kind, name, namespace)
            observations = _load_json(args.observations, "stage observations")
            problems = stage_problems(
                stage=args.stage, ledger=ledger,
                observations=observations, expected=expected,
            )
            if problems:
                print(
                    f"FAIL: the {args.stage} stage may not proceed:\n  - "
                    + "\n  - ".join(problems),
                    file=sys.stderr,
                )
                return 1
            print(f"ok   the {args.stage} stage may proceed")
            return 0
    except StageGateError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
