#!/usr/bin/env python3
"""The staged lifecycle must be EXECUTABLE, and a second stage must not be adoption.

Root, requirement 2: "Step 10 currently needs an edge URL but edge creation needs the
gateway; rerunning step 10 refuses existing objects. Wire a real staged lifecycle/
run-all and shared ledger with UID-safe cleanup."

The deadlock is not a matter of taste. `--worker-job` refuses up front without a
control endpoint; #5836's edge cannot publish one until the fixture Service exists.
So gateway-then-worker is the only workable order -- and the second invocation hit the
absence check and refused everything the first had created. The sequence in the
runbook could not be run.

The tempting fix is to skip objects that already exist. That is exactly the adoption
this tooling refuses: a name match admits anything wearing the name, and the ledger
would then license deleting it. What separates a legitimate second stage from adoption
is the recorded uid, so these tests are mostly about the cases a name check cannot
tell apart.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/test_stage_gate.py -q
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

WAVE2 = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("w2_stage_gate", WAVE2 / "lib" / "stage_gate.py")
sg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sg)

RUN_ID = "w2-20260924-0130"
NONCE = "a1b2c3d4e5f60718"
ACCOUNT = "879318057152"
GW_NS = "adp-gateway"
AGENT_NS = "adp-agents"
GW_NAME = "w2-fixture-gateway-20260924-0130"
POLICY_NAME = "w2-fixture-policy-20260924-0130"
# The SECOND policy, in the agent namespace. It confines the fixture worker -- the one
# pod in this fixture holding protected authority -- and it was the object a kind-keyed
# expectation map could not express alongside the gateway policy (5810412904).
WORKER_POLICY_NAME = f"{POLICY_NAME}-worker"
JOB_NAME = "w2-fixture-worker-20260924-0130"
QUEUE_URL = f"https://sqs.us-east-1.amazonaws.com/{ACCOUNT}/adp-dev-w2-fixture-x.fifo"

DEPLOY_UID = "11111111-1111-1111-1111-111111111111"
SVC_UID = "22222222-2222-2222-2222-222222222222"
POLICY_UID = "33333333-3333-3333-3333-333333333333"
WORKER_POLICY_UID = "44444444-4444-4444-4444-444444444444"

# Keyed by ROLE -> (kind, namespace-qualified identity). Two roles may share a kind;
# that is the whole point, and it is why the previous kind-keyed map silently held only
# one of the two NetworkPolicies.
EXPECT = {
    sg.GATEWAY_POLICY: ("NetworkPolicy", POLICY_NAME, GW_NS),
    sg.WORKER_POLICY: ("NetworkPolicy", WORKER_POLICY_NAME, AGENT_NS),
    sg.DEPLOYMENT: ("Deployment", GW_NAME, GW_NS),
    sg.SERVICE: ("Service", GW_NAME, GW_NS),
    sg.WORKER_JOB: ("Job", JOB_NAME, AGENT_NS),
}


def ledger(*, k8s=None, queues=None, **over) -> dict:
    """A v2 ledger as `ownership.py` writes it."""
    base = {
        "ledger_version": 2,
        "run_id": RUN_ID,
        "run_nonce": NONCE,
        "account_id": ACCOUNT,
        "region": "us-east-1",
        "synthetic_rows": [],
        "k8s": k8s if k8s is not None else gateway_entries(),
        "queues": queues if queues is not None else [{
            "name": "adp-dev-w2-fixture-x.fifo", "url": QUEUE_URL,
            "owner_tag_nonce": NONCE, "delete": True, "created_by_this_run": True,
        }],
    }
    base.update(over)
    return base


def gateway_entries() -> list[dict]:
    return [
        {"kind": "NetworkPolicy", "name": POLICY_NAME, "namespace": GW_NS,
         "uid": POLICY_UID, "delete": True, "created_by_this_run": True},
        {"kind": "NetworkPolicy", "name": WORKER_POLICY_NAME, "namespace": AGENT_NS,
         "uid": WORKER_POLICY_UID, "delete": True, "created_by_this_run": True},
        {"kind": "Deployment", "name": GW_NAME, "namespace": GW_NS,
         "uid": DEPLOY_UID, "delete": True, "created_by_this_run": True},
        {"kind": "Service", "name": GW_NAME, "namespace": GW_NS,
         "uid": SVC_UID, "delete": True, "created_by_this_run": True},
    ]


def entries_without(role: str) -> list[dict]:
    """The ledger minus one role's entry, addressed by role.

    Addressed by role rather than by kind because `kind != "NetworkPolicy"` would drop
    BOTH policies -- and a test meaning to remove one while silently removing two proves
    less than it claims.
    """
    kind, name, ns = EXPECT[role]
    return [e for e in gateway_entries()
            if (e["kind"], e["name"], e["namespace"]) != (kind, name, ns)]


def key_of(role: str) -> str:
    """The observation key for a role, so tests never rebuild it from parts."""
    kind, name, ns = EXPECT[role]
    return sg.object_key(kind, name, ns)


def observed(**over) -> dict:
    """The gateway stage's objects standing, with the uids this run recorded.

    Overrides are keyed by ROLE, so a test that varies the worker policy cannot
    accidentally vary the gateway policy instead.
    """
    base = {
        key_of(sg.GATEWAY_POLICY): {"status": sg.PRESENT, "uid": POLICY_UID},
        key_of(sg.WORKER_POLICY): {"status": sg.PRESENT, "uid": WORKER_POLICY_UID},
        key_of(sg.DEPLOYMENT): {"status": sg.PRESENT, "uid": DEPLOY_UID},
        key_of(sg.SERVICE): {"status": sg.PRESENT, "uid": SVC_UID},
        key_of(sg.WORKER_JOB): {"status": sg.ABSENT},
    }
    base.update({key_of(role): reply for role, reply in over.items()})
    return base


def nothing_exists() -> dict:
    return {key: {"status": sg.ABSENT} for key in observed()}


def gate(stage, *, led=None, obs=None, expect=None):
    return sg.stage_problems(
        stage=stage,
        ledger=led if led is not None else ledger(),
        observations=obs if obs is not None else observed(),
        expected=expect if expect is not None else EXPECT,
    )


# ---------------------------------------------------------------------------
# the deadlock, stated as a test: both stages must be runnable in sequence
# ---------------------------------------------------------------------------
def test_the_gateway_stage_runs_on_an_empty_cluster() -> None:
    assert gate("gateway", led=ledger(k8s=[], queues=[]), obs=nothing_exists()) == []


def test_the_worker_stage_runs_after_the_gateway_stage() -> None:
    """The case that was previously impossible.

    Everything the gateway stage created is standing; the worker stage may proceed.
    Before this, the second invocation hit `w2_check_absent` on the Deployment and
    refused -- so the endpoint the edge had just been built to publish could never be
    supplied to anything.
    """
    assert gate("worker") == []


def test_the_worker_stage_does_not_require_a_worker_to_be_absent_it_created() -> None:
    """Sanity on the split: the worker stage creates the Job, not the gateway."""
    assert sg.WORKER_JOB in sg.creates("worker")
    assert sg.WORKER_JOB not in sg.creates("gateway")
    assert sg.requires_present("gateway") == ()
    # BOTH policies are prerequisites of the worker stage, not one "NetworkPolicy".
    # Keyed by kind, the gateway-side and worker-side policies collided on the same
    # key and only one of them was ever checked -- and the one that silently went
    # unchecked was the worker-side policy, the object that confines the pod holding
    # protected authority.
    assert sg.requires_present("worker") == (
        sg.GATEWAY_POLICY, sg.WORKER_POLICY, sg.DEPLOYMENT, sg.SERVICE)


def test_the_all_stage_still_means_what_every_existing_caller_means() -> None:
    assert set(sg.creates("all")) == {
        sg.GATEWAY_POLICY, sg.WORKER_POLICY, sg.DEPLOYMENT, sg.SERVICE, sg.WORKER_JOB}
    assert gate("all", led=ledger(k8s=[], queues=[]), obs=nothing_exists()) == []


def test_an_unknown_stage_is_refused_rather_than_treated_as_a_default() -> None:
    with pytest.raises(sg.StageGateError):
        gate("gatway")  # typo
    with pytest.raises(sg.StageGateError):
        sg.creates("edge")


# ---------------------------------------------------------------------------
# a second stage must not become adoption
# ---------------------------------------------------------------------------
def test_a_prerequisite_this_run_never_recorded_is_refused() -> None:
    """The whole reason the absence check exists, moved rather than removed.

    A Deployment of the right name that this run did not create is someone else's.
    Building on it would measure their fixture, and this run's teardown would then
    delete it -- the failure root found already sitting in the account.
    """
    problems = gate("worker", led=ledger(k8s=entries_without(sg.DEPLOYMENT)))
    assert any("THIS RUN'S LEDGER DOES NOT RECORD IT" in p for p in problems), problems


def test_a_replacement_prerequisite_is_refused_even_though_the_name_matches() -> None:
    """The case a name check cannot see at all.

    The gateway was deleted and rebuilt (by a rerun, by a controller, by hand) after
    this run recorded its uid. The name is right, the object is running, and it is not
    the composition this fixture was reviewed against.
    """
    problems = gate("worker", obs=observed(**{
        sg.DEPLOYMENT: {
            "status": sg.PRESENT, "uid": "99999999-9999-9999-9999-999999999999"}}))
    assert any("DIFFERENT object wearing the same name" in p for p in problems), problems


def test_a_prerequisite_present_without_a_uid_cannot_be_admitted() -> None:
    problems = gate("worker", obs=observed(**{
        sg.SERVICE: {"status": sg.PRESENT, "uid": ""}}))
    assert any("A name match is not identity" in p for p in problems), problems


def test_a_ledger_entry_without_a_uid_cannot_admit_a_live_object() -> None:
    """Symmetric to the above: the recorded side must identify an instance too."""
    kind, name, ns = EXPECT[sg.DEPLOYMENT]
    entries = [dict(e) for e in gateway_entries()]
    for e in entries:
        if (e["kind"], e["name"], e["namespace"]) == (kind, name, ns):
            e["uid"] = ""
    problems = gate("worker", led=ledger(k8s=entries))
    assert any("without a uid" in p for p in problems), problems


def test_a_deleted_prerequisite_is_refused_with_the_reason_it_matters() -> None:
    """Recorded, but gone now: the edge fronts nothing.

    Distinguished from "never created" because the operator's next action differs --
    one is "run the gateway stage", the other is "something deleted your fixture".
    """
    problems = gate("worker", obs=observed(**{sg.SERVICE: {"status": sg.ABSENT}}))
    assert any("deleted between stages" in p for p in problems), problems
    assert any("bootstrap into a void" in p for p in problems), problems


def test_a_prerequisite_that_was_never_created_says_to_run_the_gateway_stage() -> None:
    problems = gate("worker", led=ledger(k8s=[], queues=[]), obs=nothing_exists())
    assert any("Run the gateway stage first" in p for p in problems), problems


# ---------------------------------------------------------------------------
# the worker-side NetworkPolicy is its own prerequisite
#
# Every negative test above varies the Deployment or the Service. Those were never
# the objects that went unchecked: keyed by kind, `NetworkPolicy` resolved to ONE
# entry, and the one that survived was the gateway-side policy. So the worker-side
# policy -- the object that confines the only pod in this fixture holding protected
# authority -- was verified by nothing at all. These are the cases that distinguish
# the fix from the defect, and each one must produce a refusal rather than a Job.
# ---------------------------------------------------------------------------
def test_the_worker_policy_is_verified_and_not_shadowed_by_the_gateway_policy() -> None:
    """The defect exactly: gateway policy standing, worker policy never observed.

    Under the kind-keyed map this returned [] -- proceed -- because the gateway
    policy's observation answered for the key both policies shared. Both are present
    in the ledger, so nothing else in the decision notices the absence either.
    """
    obs = observed()
    del obs[key_of(sg.WORKER_POLICY)]
    problems = gate("worker", obs=obs)
    assert problems, "the worker policy was never observed and the stage proceeded"
    assert any("never observed" in p for p in problems), problems
    assert any(WORKER_POLICY_NAME in p for p in problems), problems


def test_a_deleted_worker_policy_stops_the_worker_being_created() -> None:
    """Recorded by this run, gone now: creating the worker would leave it unconfined."""
    problems = gate("worker", obs=observed(**{sg.WORKER_POLICY: {"status": sg.ABSENT}}))
    assert any("deleted between stages" in p for p in problems), problems
    assert any("unconfined protected worker" in p for p in problems), problems


def test_a_replaced_worker_policy_is_refused_though_its_name_matches() -> None:
    """Same name, different object: its rules are whatever the replacer chose.

    This is the case a name check cannot see, and it matters more for this policy
    than for any other object in the fixture -- an egress rule swapped underneath a
    matching name is precisely how a confined worker stops being confined.
    """
    problems = gate("worker", obs=observed(**{
        sg.WORKER_POLICY: {"status": sg.PRESENT,
                           "uid": "88888888-8888-8888-8888-888888888888"}}))
    assert any("DIFFERENT object wearing the same name" in p for p in problems), problems


def test_an_unreadable_worker_policy_is_not_treated_as_a_present_one() -> None:
    problems = gate("worker", obs=observed(**{
        sg.WORKER_POLICY: {"status": sg.UNREADABLE,
                           "detail": "Error from server (Forbidden): networkpolicies"}}))
    assert any("not a confirmed one" in p for p in problems), problems
    assert any("Forbidden" in p for p in problems), problems


def test_a_worker_policy_this_run_never_recorded_is_refused() -> None:
    """A policy of the right name this run did not create is someone else's.

    Admitting it would both measure their confinement and license this run's teardown
    to delete it.
    """
    problems = gate("worker", led=ledger(k8s=entries_without(sg.WORKER_POLICY)))
    assert any("THIS RUN'S LEDGER DOES NOT RECORD IT" in p for p in problems), problems
    assert any(WORKER_POLICY_NAME in p for p in problems), problems


def test_the_two_policies_are_distinguished_by_namespace_not_just_name() -> None:
    """A worker policy observed in the GATEWAY namespace is not the one required.

    Both policies share a kind and their names differ only by a suffix, so identity
    here rests on the full kind/namespace/name triple. This pins that the gate is
    reading the namespace too, rather than getting the right answer by accident from
    two names that happen to differ.
    """
    obs = observed()
    del obs[key_of(sg.WORKER_POLICY)]
    obs[sg.object_key("NetworkPolicy", WORKER_POLICY_NAME, GW_NS)] = {
        "status": sg.PRESENT, "uid": WORKER_POLICY_UID}
    problems = gate("worker", obs=obs)
    assert any("never observed" in p for p in problems), problems


def test_a_worker_policy_that_was_not_even_named_cannot_be_checked() -> None:
    """The gate can only refuse what it is told about, so an omitted role refuses.

    This is the seam that failed before: the caller passed four `--expect` specs for
    five objects and nothing complained. A caller that forgets the worker policy must
    now be refused rather than quietly gated on one object fewer.
    """
    expect = {k: v for k, v in EXPECT.items() if k != sg.WORKER_POLICY}
    problems = gate("worker", expect=expect)
    assert any("supplied no kind/name/namespace" in p for p in problems), problems
    assert any(sg.WORKER_POLICY in p for p in problems), problems


# ---------------------------------------------------------------------------
# "could not check" is never "checked and fine"
# ---------------------------------------------------------------------------
def test_an_unreadable_prerequisite_is_refused_not_assumed_present() -> None:
    problems = gate("worker", obs=observed(**{
        sg.DEPLOYMENT: {
            "status": sg.UNREADABLE, "detail": "Unable to connect to the server"}}))
    assert any("not a confirmed one" in p for p in problems), problems
    assert any("Unable to connect" in p for p in problems), problems


def test_an_unreadable_creation_target_is_refused_not_assumed_absent() -> None:
    """The rule `w2_check_absent` already applies, kept for the staged path."""
    problems = gate("gateway", led=ledger(k8s=[], queues=[]), obs={
        **nothing_exists(),
        sg.object_key("Deployment", GW_NAME, GW_NS): {
            "status": sg.UNREADABLE, "detail": "error: timed out"}})
    assert any("not an empty one" in p for p in problems), problems


def test_a_missing_observation_is_a_problem_and_not_a_pass() -> None:
    """A stage decision needs a reply about every object.

    Omitting one is how a check gets silently skipped: the loop finds nothing to
    complain about and the stage proceeds having verified less than it claims.
    """
    obs = observed()
    del obs[sg.object_key("Deployment", GW_NAME, GW_NS)]
    problems = gate("worker", obs=obs)
    assert any("never observed" in p for p in problems), problems


def test_an_unrecognised_status_is_refused_rather_than_guessed() -> None:
    problems = gate("worker", obs=observed(**{
        sg.DEPLOYMENT: {"status": "maybe", "uid": DEPLOY_UID}}))
    assert any("unrecognised status" in p for p in problems), problems


def test_a_prerequisite_with_no_supplied_name_cannot_be_checked() -> None:
    expect = {k: v for k, v in EXPECT.items() if k != sg.DEPLOYMENT}
    problems = gate("worker", expect=expect)
    assert any("supplied no kind/name/namespace" in p for p in problems), problems


# ---------------------------------------------------------------------------
# rerunning a completed stage is not a resume
# ---------------------------------------------------------------------------
def test_rerunning_the_gateway_stage_is_refused_and_says_to_go_on() -> None:
    """The precise thing root reported: "rerunning step 10 refuses existing objects".

    It must still refuse -- creating them twice is not a resume -- but the refusal now
    names the actual situation and the next action, instead of reading as a collision
    with a stranger's resource.
    """
    problems = gate("gateway")
    assert any("has already run" in p for p in problems), problems
    assert any("Continue with the next stage" in p for p in problems), problems


def test_a_creation_target_belonging_to_someone_else_still_says_use_a_new_run_id() -> None:
    problems = gate("gateway", led=ledger(k8s=[], queues=[]))
    assert any("Use a different --run-id" in p for p in problems), problems
    assert not any("has already run" in p for p in problems), problems


def test_the_worker_stage_refuses_a_job_that_already_exists() -> None:
    problems = gate("worker", obs=observed(**{
        sg.WORKER_JOB: {"status": sg.PRESENT, "uid": "j"}}))
    assert any("Job/" in p and "already exists" in p for p in problems), problems


def test_an_anonymous_ledger_entry_does_not_admit_a_live_object() -> None:
    """`k8s: [{}]` must not index under a partial key and accidentally match."""
    problems = gate("worker", led=ledger(k8s=[{}] + entries_without(sg.DEPLOYMENT)))
    assert any("DOES NOT RECORD IT" in p for p in problems), problems


# ---------------------------------------------------------------------------
# the nonce is read, never retyped
# ---------------------------------------------------------------------------
def test_the_nonce_comes_from_the_ledger() -> None:
    assert sg.nonce_from_ledger(ledger()) == NONCE


def test_an_agreeing_supplied_nonce_is_accepted_as_a_cross_check() -> None:
    assert sg.nonce_from_ledger(ledger(), supplied=NONCE) == NONCE
    assert sg.nonce_from_ledger(ledger(), supplied=f" {NONCE} ") == NONCE


def test_a_disagreeing_supplied_nonce_refuses_both() -> None:
    """A typo produces a PLAUSIBLE nonce, which is why this cannot be a warning.

    The later stage would tag its resources with one nonce while the earlier stage's
    teardown looks for another, and nothing would ever find them.
    """
    with pytest.raises(sg.StageGateError) as exc:
        sg.nonce_from_ledger(ledger(), supplied="ffffffffffffffff")
    assert "two different runs" in str(exc.value)


def test_a_ledger_with_no_nonce_cannot_carry_a_run_across_stages() -> None:
    with pytest.raises(sg.StageGateError) as exc:
        sg.nonce_from_ledger(ledger(run_nonce=""))
    assert "records no run_nonce" in str(exc.value)


# ---------------------------------------------------------------------------
# the queue is carried in the ledger, not re-derived by name
# ---------------------------------------------------------------------------
def test_the_worker_stage_reuses_the_queue_this_run_created() -> None:
    assert sg.queue_from_ledger(ledger())["url"] == QUEUE_URL


def test_no_recorded_queue_means_there_is_no_fixture_to_join() -> None:
    with pytest.raises(sg.StageGateError) as exc:
        sg.queue_from_ledger(ledger(queues=[]))
    assert "no queue created by this run" in str(exc.value)


def test_an_adopted_queue_is_not_a_created_one() -> None:
    """`created_by_this_run` false means CreateQueue returned an existing queue."""
    with pytest.raises(sg.StageGateError):
        sg.queue_from_ledger(ledger(queues=[{
            "name": "q", "url": QUEUE_URL, "created_by_this_run": False}]))


def test_two_recorded_queues_are_refused_rather_than_picked_between() -> None:
    with pytest.raises(sg.StageGateError) as exc:
        sg.queue_from_ledger(ledger(queues=[
            {"name": "a", "url": QUEUE_URL, "created_by_this_run": True},
            {"name": "b", "url": QUEUE_URL + "2", "created_by_this_run": True},
        ]))
    assert "two runs share this ledger" in str(exc.value)


# ---------------------------------------------------------------------------
# the handoff document — what replaces the runbook's <angle brackets>
# ---------------------------------------------------------------------------
def handoff(**over):
    kwargs = dict(
        ledger=ledger(), run_id=RUN_ID, namespace=GW_NS, agent_namespace=AGENT_NS,
        service_name=GW_NAME, deployment_name=GW_NAME,
        evidence_dir="/ev", ledger_path="/ev/cleanup-ledger.json",
    )
    if "led" in over:
        over["ledger"] = over.pop("led")
    kwargs.update(over)
    return sg.handoff_document(**kwargs)


def test_the_handoff_carries_every_value_5836_would_otherwise_be_retyped() -> None:
    """Each of these is a value whose only purpose is to bind two runs together.

    The runbook asked the operator to transcribe the nonce, the Service name, the
    Deployment name and the ledger path into four further commands. Transcription is
    precisely what must not be in the loop for binding values.
    """
    doc = handoff()
    assert doc["run_nonce"] == NONCE
    assert doc["run_id"] == RUN_ID
    assert doc["account_id"] == ACCOUNT
    assert doc["fixture"]["service"] == GW_NAME
    assert doc["fixture"]["deployment"] == GW_NAME
    assert doc["fixture"]["queue_url"] == QUEUE_URL
    assert doc["ledger"] == "/ev/cleanup-ledger.json"
    assert doc["next_stage"] == "worker"


def test_the_handoff_publishes_the_created_uids() -> None:
    """So #5836's ALB step and the worker stage act on the same object INSTANCES."""
    doc = handoff()
    assert doc["created_uids"][sg.object_key("Deployment", GW_NAME, GW_NS)] == DEPLOY_UID
    assert doc["created_uids"][sg.object_key("Service", GW_NAME, GW_NS)] == SVC_UID


def test_the_handoff_describes_the_run_the_LEDGER_describes() -> None:
    """Not the run this process thinks it is.

    If the ledger belongs to another run the document must not launder this process's
    variables into looking like agreement -- the ledger is the shared record.
    """
    doc = handoff(led=ledger(run_id="w2-somebody-else"), run_id=RUN_ID)
    assert doc["run_id"] == "w2-somebody-else"


def test_the_handoff_states_that_the_fixture_is_still_running() -> None:
    """A staged lifecycle means a flag-ON gateway and a live queue persist between
    stages. That cost is stated, not discovered."""
    doc = handoff()
    assert "STILL RUNNING" in doc["_next"][0]
    assert "flag-ON gateway" in doc["_why_not_torn_down"]
    assert any("90-cleanup-ledger.sh" in line for line in doc["_next"])


def test_the_handoff_refuses_to_describe_a_run_with_no_nonce() -> None:
    with pytest.raises(sg.StageGateError):
        handoff(led=ledger(run_nonce=""))


# ---------------------------------------------------------------------------
# the CLI seam the shell uses
# ---------------------------------------------------------------------------
def write(tmp_path, name, doc) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


def expect_args(*roles: str) -> list[str]:
    """`--expect` flags in the form the shell passes them, built from EXPECT.

    Built from the same map the in-process tests use so the CLI surface and the
    library surface cannot drift: if a role is added to one, the CLI tests carry it
    too rather than silently continuing to check one object fewer.
    """
    chosen = roles or tuple(EXPECT)
    args = []
    for role in chosen:
        kind, name, ns = EXPECT[role]
        args.append(f"--expect={role}={kind}/{ns}/{name}")
    return args


def test_the_cli_gate_exits_zero_when_a_stage_may_proceed(tmp_path) -> None:
    rc = sg.main([
        "gate", "--stage", "worker",
        "--ledger", str(write(tmp_path, "ledger.json", ledger())),
        "--observations", str(write(tmp_path, "obs.json", observed())),
    ] + expect_args())
    assert rc == 0


def test_the_cli_gate_exits_nonzero_and_names_every_problem(tmp_path, capsys) -> None:
    rc = sg.main([
        "gate", "--stage", "worker",
        "--ledger", str(write(tmp_path, "ledger.json", ledger(k8s=[]))),
        "--observations", str(write(tmp_path, "obs.json", observed())),
    ] + expect_args(sg.DEPLOYMENT, sg.SERVICE, sg.GATEWAY_POLICY, sg.WORKER_POLICY))
    assert rc == 1
    err = capsys.readouterr().err
    assert "may not proceed" in err
    # every unrecorded object, not just the first -- and four of them, because the
    # worker-side policy is its own object rather than a duplicate key.
    assert err.count("DOES NOT RECORD IT") == 4


def test_a_partial_expect_spec_is_refused(tmp_path, capsys) -> None:
    """A spec missing any of role/kind/namespace/name would leave an object unchecked."""
    for spec in (f"deployment=Deployment/{GW_NAME}",      # no namespace
                 f"Deployment/{GW_NS}/{GW_NAME}",          # no role
                 f"deployment=Deployment//{GW_NAME}",      # empty namespace
                 f"deployment=/{GW_NS}/{GW_NAME}"):        # empty kind
        rc = sg.main([
            "gate", "--stage", "gateway",
            "--ledger", str(write(tmp_path, "ledger.json", ledger())),
            "--observations", str(write(tmp_path, "obs.json", observed())),
            f"--expect={spec}",
        ])
        assert rc == 1, spec
        assert "ROLE=KIND/NAMESPACE/NAME" in capsys.readouterr().err, spec


def test_an_expect_spec_naming_an_unknown_role_is_refused(tmp_path, capsys) -> None:
    """A typo'd role must not be accepted and then never checked against anything.

    Silently keeping an unrecognised key is how the worker policy would go missing
    again: the gate would hold an expectation nothing consults.
    """
    rc = sg.main([
        "gate", "--stage", "worker",
        "--ledger", str(write(tmp_path, "ledger.json", ledger())),
        "--observations", str(write(tmp_path, "obs.json", observed())),
    ] + expect_args() + [f"--expect=worker_polcy=NetworkPolicy/{AGENT_NS}/x"])
    assert rc == 1
    assert "worker_polcy" in capsys.readouterr().err


def test_an_expect_spec_naming_one_role_twice_is_refused(tmp_path, capsys) -> None:
    """Two specs for one role would silently drop one object's identity."""
    rc = sg.main([
        "gate", "--stage", "worker",
        "--ledger", str(write(tmp_path, "ledger.json", ledger())),
        "--observations", str(write(tmp_path, "obs.json", observed())),
    ] + expect_args() + [f"--expect={sg.WORKER_POLICY}=NetworkPolicy/{GW_NS}/{POLICY_NAME}"])
    assert rc == 1
    assert "twice" in capsys.readouterr().err


def test_the_cli_nonce_prints_only_the_nonce(tmp_path, capsys) -> None:
    """The shell captures stdout directly."""
    rc = sg.main(["nonce", "--ledger", str(write(tmp_path, "l.json", ledger()))])
    assert rc == 0
    assert capsys.readouterr().out.strip() == NONCE


def test_the_cli_nonce_refuses_a_disagreeing_cross_check(tmp_path, capsys) -> None:
    rc = sg.main(["nonce", "--ledger", str(write(tmp_path, "l.json", ledger())),
                  "--supplied", "ffffffffffffffff"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == ""


def test_the_cli_queue_url_prints_only_the_url(tmp_path, capsys) -> None:
    rc = sg.main(["queue-url", "--ledger", str(write(tmp_path, "l.json", ledger()))])
    assert rc == 0
    assert capsys.readouterr().out.strip() == QUEUE_URL


def test_the_cli_handoff_writes_the_document_and_prints_the_next_steps(tmp_path, capsys) -> None:
    out = tmp_path / "nested" / "handoff.json"
    rc = sg.main([
        "handoff", "--ledger", str(write(tmp_path, "l.json", ledger())),
        "--run-id", RUN_ID, "--namespace", GW_NS, "--agent-namespace", AGENT_NS,
        "--service", GW_NAME, "--deployment", GW_NAME,
        "--evidence-dir", str(tmp_path), "--out", str(out),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(out.read_text())["run_nonce"] == NONCE
    # stdout is the path alone; the instructions go to stderr so a shell capture of
    # the path is not polluted by them.
    assert captured.out.strip() == str(out)
    assert "create-fixture-alb.sh" in captured.err
    # The whole outputs document, not the endpoint-less `ownership` one.
    assert "terraform output -json >" in captured.err
    assert "terraform output -json ownership" not in captured.err


def test_the_cli_refuses_a_missing_ledger(tmp_path, capsys) -> None:
    rc = sg.main(["nonce", "--ledger", str(tmp_path / "nope.json")])
    assert rc == 1
    assert "no shared fixture ledger" in capsys.readouterr().err


def test_the_cli_refuses_an_unreadable_ledger(tmp_path, capsys) -> None:
    bad = tmp_path / "l.json"
    bad.write_text("{not json")
    rc = sg.main(["nonce", "--ledger", str(bad)])
    assert rc == 1
    assert "not valid JSON" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# k8s-uid: the expectation the ALB policy mutation is held to
# ---------------------------------------------------------------------------
# The uid must come from the LEDGER, never from the cluster. Re-reading it live would
# defeat the purpose exactly: the live uid always matches itself, so a replacement
# policy would pass the very check that exists to catch it.
def _ledger_with(entry: dict) -> dict:
    return {
        "run_id": "w2-20260924-0130",
        "account_id": "879318057152",
        "region": "us-east-1",
        "nonce": "deadbeefcafe0123",
        "k8s": [entry],
        "queues": [],
        "rows": [],
    }


POLICY_ENTRY = {
    "kind": "NetworkPolicy",
    "name": "w2-fixture-policy-20260924-0130",
    "namespace": "adp-gateway",
    "uid": "77777777-8888-9999-aaaa-bbbbbbbbbbbb",
    "delete": True,
    "created_by_this_run": True,
}


def test_the_recorded_uid_is_returned_for_a_recorded_object() -> None:
    assert sg.k8s_uid_from_ledger(
        _ledger_with(POLICY_ENTRY),
        kind="NetworkPolicy", name=POLICY_ENTRY["name"], namespace="adp-gateway",
    ) == POLICY_ENTRY["uid"]


def test_an_unrecorded_object_has_no_uid_to_offer() -> None:
    """A same-named policy from another run must not be mutated as though it were ours."""
    with pytest.raises(sg.StageGateError) as exc:
        sg.k8s_uid_from_ledger(
            _ledger_with(POLICY_ENTRY),
            kind="NetworkPolicy", name="some-other-policy", namespace="adp-gateway",
        )
    assert "no uid to hold the live object against" in str(exc.value)


def test_the_namespace_is_part_of_the_identity() -> None:
    """The fixture has TWO NetworkPolicies, in two namespaces.

    A lookup by kind and name alone would return the gateway policy's uid for the
    worker policy, and the mutation would then be held against the wrong object --
    which, since the uids differ, reads as a replacement rather than as a bug here.
    """
    with pytest.raises(sg.StageGateError):
        sg.k8s_uid_from_ledger(
            _ledger_with(POLICY_ENTRY),
            kind="NetworkPolicy", name=POLICY_ENTRY["name"], namespace="adp-agents",
        )


@pytest.mark.parametrize("uid", ["", None])
def test_an_entry_with_no_uid_refuses_rather_than_returning_empty(uid) -> None:
    """An empty expectation must not travel onward as a flag value.

    The mutation's own check does refuse a missing `--uid`, but it would be refusing
    several steps from the cause and for an obscure reason. Refused here, where the
    cause is legible: this run's ownership of the object was never proven.
    """
    entry = dict(POLICY_ENTRY, uid=uid)
    with pytest.raises(sg.StageGateError) as exc:
        sg.k8s_uid_from_ledger(
            _ledger_with(entry),
            kind="NetworkPolicy", name=entry["name"], namespace="adp-gateway",
        )
    assert "carries no uid" in str(exc.value)


def test_an_adopted_object_is_not_offered_for_mutation() -> None:
    """Recorded, but not created by this run -- so its rules are not ours to change."""
    entry = dict(POLICY_ENTRY, created_by_this_run=False)
    with pytest.raises(sg.StageGateError) as exc:
        sg.k8s_uid_from_ledger(
            _ledger_with(entry),
            kind="NetworkPolicy", name=entry["name"], namespace="adp-gateway",
        )
    assert "must not be mutated" in str(exc.value)


def test_the_cli_prints_the_uid_alone(tmp_path, capsys) -> None:
    """stdout is the uid and nothing else: the shell captures it with $(...).

    A note or a header on stdout would be captured into the variable and become part
    of the uid, which then never matches -- reported as a replacement policy.
    """
    import json

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(_ledger_with(POLICY_ENTRY)))
    rc = sg.main(["k8s-uid", "--ledger", str(ledger), "--kind", "NetworkPolicy",
                  "--name", POLICY_ENTRY["name"], "--namespace", "adp-gateway"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == POLICY_ENTRY["uid"]
