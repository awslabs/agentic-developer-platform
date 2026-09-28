#!/usr/bin/env python3
"""The staged lifecycle, driven END TO END through the real script (issue #3968).

Root, requirement 2: "Step 10 currently needs an edge URL but edge creation needs the
gateway; rerunning step 10 refuses existing objects. Wire a real staged lifecycle/run-all
and shared ledger."

tests/test_stage_gate.py covers the DECISION in isolation -- fast, exhaustive, no
subprocess. It cannot cover the thing root actually reported, because that defect was
not in any single decision: it was that TWO INVOCATIONS could not be composed. The
first created the gateway, the second refused everything the first had built, and no
unit test of either invocation would notice.

So every test here runs `10-create-fixture.sh` TWICE against ONE ledger, which is the
only shape in which the reported failure exists. The stub harness carries the ledger
and evidence directory between invocations while resetting the scripted-rule cursor
and the call log per invocation (see conftest.run_create).

The second invocation's live replies are built from the FIRST invocation's recorded
uids, not from constants: a test that fed back invented uids would pass against a gate
that ignored them.
"""

from __future__ import annotations

import json

from conftest import base_rules, not_found

from test_create_fixture import (
    FIXED_NONCE,
    FIXTURE_ALB_ARN,
    JOB_UID,
    _args_fixed_nonce,
    _create_ok_rules,
    _edge_receipt,
    _worker_rules,
)

GW_NS = "adp-gateway"
AGENT_NS = "adp-agents"
GW_NAME = "w2-fixture-gateway-fixture-test"
POLICY_NAME = "w2-fixture-policy-fixture-test"
# The second policy, in the AGENT namespace, confining the protected worker. Derived
# here the same way the script derives it, so a rename in one place fails these tests
# rather than silently leaving the object unchecked.
WORKER_POLICY_NAME = f"{POLICY_NAME}-worker"
JOB_NAME = "w2-fixture-worker-fixture-test"
QUEUE_NAME = "adp-dev-w2-fixture-fixture-test.fifo"


# ---------------------------------------------------------------------------
# helpers: the second invocation's world is the first invocation's LEDGER
# ---------------------------------------------------------------------------
def _uid_of(ledger: dict, kind: str, name: str, namespace: str) -> str:
    """The uid the FIRST invocation recorded for an object it created.

    Read out of the ledger rather than asserted as a constant, so the replies the
    second invocation sees are the uids that actually exist. If the script recorded
    nothing, this raises and the test says so -- which is the right failure, because a
    stage gate fed invented uids proves nothing about a gate that reads real ones.
    """
    for entry in ledger.get("k8s") or []:
        if (entry.get("kind"), entry.get("name"), entry.get("namespace")) == (
                kind, name, namespace):
            uid = entry.get("uid")
            assert uid, f"{kind}/{name} is recorded without a uid: {entry}"
            return uid
    raise AssertionError(
        f"the gateway stage did not record {kind}/{name} in {namespace}: "
        f"{json.dumps(ledger.get('k8s'), indent=2)}")


def _gateway_present_rules(ledger: dict, *, overrides: dict | None = None) -> list[dict]:
    """Observation replies for a SECOND invocation: the gateway stage's objects standing.

    `overrides` replaces one object's reply, which is how the adoption / replacement /
    deleted / unreadable cases are expressed -- each is a single changed answer against
    an otherwise legitimate continuation, so what the test varies is exactly the thing
    under test.
    """
    overrides = overrides or {}
    replies: list[dict] = []
    # BOTH NetworkPolicies. The worker-side one is in a different namespace, so the
    # match includes `-n <namespace>`: matched on name alone, the gateway policy's rule
    # would answer for the worker policy too and a test claiming the worker policy was
    # absent would be silently answered "present".
    for kind, name, ns in (("Deployment", GW_NAME, GW_NS),
                           ("Service", GW_NAME, GW_NS),
                           ("NetworkPolicy", POLICY_NAME, GW_NS),
                           ("NetworkPolicy", WORKER_POLICY_NAME, AGENT_NS)):
        key = f"{kind}/{ns}/{name}"
        match = ["get", kind, name, "-n", ns]
        if key in overrides:
            replies.append({"tool": "kubectl", "match": match, **overrides[key]})
            continue
        replies.append({"tool": "kubectl", "match": match,
                        "stdout": _uid_of(ledger, kind, name, ns) + "\n"})
    # The Job is the worker stage's creation TARGET, so it must still be absent.
    replies.append({"tool": "kubectl", "match": ["get", "Job", "w2-fixture-worker"],
                    **not_found("jobs.batch", JOB_NAME)})
    return replies


def _worker_stage_rules(ledger: dict, *, overrides: dict | None = None) -> list[dict]:
    """The full rule set for a worker-stage invocation.

    The live facts after the observations are the SAME ones the all-in-one worker path
    reads -- reused from test_create_fixture._worker_rules rather than copied, so the
    staged worker cannot quietly come to depend on a different contract than the
    unstaged one. What differs is only the prefix: no queue creation (it is read from
    the ledger) and no gateway creates (they exist).
    """
    base = base_rules()
    # base_rules ends with the four all-absent observations; the continuation sees the
    # gateway standing instead. Rules are matched first-unconsumed-wins, so inserting
    # ahead of them is how they are replaced.
    return _worker_rules(base=_gateway_present_rules(ledger, overrides=overrides) + base)


def _gateway_args(tmp_path) -> list[str]:
    return _args_fixed_nonce() + ["--stage", "gateway"]


def _worker_stage_args(tmp_path, *, alb_arn: str = FIXTURE_ALB_ARN,
                       **receipt_kw) -> list[str]:
    """The worker stage's flags. No --resume-nonce: the nonce comes from the ledger.

    The ALB ARN accompanies the receipt (the script refuses one without the other) and
    defaults to the value the receipt carries -- it is the expectation the receipt's
    addresses are checked against, so deriving it FROM the receipt would be self-
    confirming.
    """
    return ["--stage", "worker", "--worker-job",
            "--edge-receipt", _edge_receipt(tmp_path, alb_arn=alb_arn, **receipt_kw),
            "--edge-alb-arn", alb_arn]


# ---------------------------------------------------------------------------
# THE case root reported: two invocations, one ledger
# ---------------------------------------------------------------------------
def test_the_gateway_stage_then_the_worker_stage_completes_the_sequence(
        run_create, tmp_path) -> None:
    """The sequence that was NOT EXECUTABLE before: gateway, then edge, then worker.

    Asserted as one test on purpose. The defect only exists in the composition -- both
    invocations were individually fine -- so splitting this into "gateway works" and
    "worker works" would restore exactly the blind spot that let it ship.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output
    # The gateway stage created the gateway and NOT the worker...
    assert first.created("10-gateway"), first.calls
    assert not any("20-worker" in call for call in first.calls), first.calls
    gateway_record = json.loads((tmp_path / "evidence" / "fixture-created.json").read_text())
    assert gateway_record["stage"] == "gateway"
    assert gateway_record["worker_job_created"] is False
    # ...and said WHY, in terms of the next stage rather than "not requested".
    assert "--stage worker" in gateway_record["worker"]["reason"]

    ledger = first.ledger()
    assert ledger["run_nonce"] == FIXED_NONCE

    # The second invocation: the endpoint #5836's edge can now publish, because the
    # Service it fronts exists.
    second = run_create(_worker_stage_rules(ledger), args=_worker_stage_args(tmp_path))
    assert second.rc == 0, second.output

    # The worker was created...
    assert second.created("20-worker"), second.calls
    # ...and the gateway was NOT created a second time. This is the assertion that
    # distinguishes a staged lifecycle from a re-run: a script that recreated the
    # gateway would either fail or adopt, and the ledger's uid would stop matching.
    assert not any("10-gateway" in call for call in second.calls), second.calls
    assert not any("00-policy" in call for call in second.calls), second.calls
    # ...nor was a second queue created, which would leave the worker publishing where
    # nothing reads.
    assert not any("create-queue" in call for call in second.calls), second.calls

    worker_record = json.loads((tmp_path / "evidence" / "fixture-created.json").read_text())
    assert worker_record["stage"] == "worker"
    assert worker_record["worker_job_created"] is True
    assert worker_record["worker"]["job_uid"] == JOB_UID
    # One queue, the gateway stage's, carried into the worker's report.
    assert worker_record["queue"]["url"] == ledger["queues"][0]["url"]

    # ONE ledger, describing one run, holding both stages' objects with their uids.
    final = second.ledger()
    assert final["run_nonce"] == FIXED_NONCE
    kinds = sorted(entry["kind"] for entry in final["k8s"])
    assert kinds == ["Deployment", "Job", "NetworkPolicy", "NetworkPolicy", "Pod", "Service"], \
        kinds
    assert len(final["queues"]) == 1, final["queues"]
    # Every object the teardown may delete carries a uid, from both stages.
    assert all(entry.get("uid") for entry in final["k8s"]), final["k8s"]


def test_the_worker_stage_reads_the_nonce_from_the_ledger_and_is_not_told_it(
        run_create, tmp_path) -> None:
    """The worker invocation above passes NO --resume-nonce, and still binds.

    That is the point of reading it from the ledger: the nonce is the ownership
    evidence on everything without a uid (the queue tag, #5836's state key), so a
    retyped nonce is a *plausible* wrong answer -- the stages would tag their
    resources so the other's teardown never finds them.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output
    ledger = first.ledger()

    args = _worker_stage_args(tmp_path)
    assert "--resume-nonce" not in args
    second = run_create(_worker_stage_rules(ledger), args=args)
    assert second.rc == 0, second.output
    # The receipt is bound against FIXED_NONCE, and it was never supplied on the command
    # line -- so it can only have come from the ledger.
    provenance = json.loads(
        (tmp_path / "evidence" / "edge-endpoint-provenance.json").read_text())
    assert provenance["bound_to"]["run_nonce"] == FIXED_NONCE
    # The worker pod's label carries the same nonce, so teardown finds both stages' work.
    assert second.ledger()["run_nonce"] == FIXED_NONCE


def test_a_nonce_that_disagrees_with_the_ledger_refuses_both_stages(
        run_create, tmp_path) -> None:
    """Refusing BOTH rather than preferring one: neither is known to be this run's."""
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output

    second = run_create(
        _worker_stage_rules(first.ledger()),
        args=_worker_stage_args(tmp_path) + ["--resume-nonce", "0000badf00d00000"])
    assert second.rc == 1
    assert not second.created()
    assert "disagrees with the ledger" in second.output


# ---------------------------------------------------------------------------
# the handoff document -- what makes the sequence executable rather than prose
# ---------------------------------------------------------------------------
def test_the_gateway_stage_writes_a_handoff_the_next_stage_can_be_driven_from(
        run_create, tmp_path) -> None:
    """The runbook's version asked the operator to transcribe a nonce into four commands.

    Every value in those commands binds two runs together, so transcription is the one
    thing that must not be in the loop. The document is therefore asserted to contain
    the actual uids and the literal next commands, not placeholders.
    """
    run = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert run.rc == 0, run.output

    doc = json.loads((tmp_path / "evidence" / "stage-handoff.json").read_text())
    assert doc["schema"] == "w2-stage-handoff/v1"
    assert doc["stage_completed"] == "gateway"
    assert doc["next_stage"] == "worker"
    assert doc["run_nonce"] == FIXED_NONCE
    assert doc["fixture"]["service"] == GW_NAME
    assert doc["fixture"]["queue_url"] == run.ledger()["queues"][0]["url"]

    # The uids, so #5836's ALB step and the worker stage are talking about the same
    # object instances rather than the same names.
    ledger = run.ledger()
    assert doc["created_uids"][f"Service/{GW_NS}/{GW_NAME}"] == _uid_of(
        ledger, "Service", GW_NAME, GW_NS)
    assert doc["created_uids"][f"Deployment/{GW_NS}/{GW_NAME}"] == _uid_of(
        ledger, "Deployment", GW_NAME, GW_NS)

    steps = "\n".join(doc["_next"])
    assert "create-fixture-alb.sh" in steps
    assert "fixture-lifecycle.sh handoff" in steps
    # ALL the outputs, and specifically NOT `-json ownership`: that document carries
    # the run bindings but no endpoint (root 5809603844), so a handoff naming it
    # sends the next operator to a command that cannot produce what stage 3 needs.
    assert "terraform output -json >" in steps
    assert "terraform output -json ownership" not in steps
    assert "--stage worker" in steps
    assert "90-cleanup-ledger.sh" in steps


def test_the_handoff_says_the_fixture_is_still_running_and_why(run_create, tmp_path) -> None:
    """A gateway stage that tore its own fixture down could never be followed.

    So it deliberately does not -- which means a control-flag-ON gateway and a live
    queue persist until cleanup. That is a real cost and it has to be stated where the
    operator is looking, not discovered from a bill or a stray pod.
    """
    run = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert run.rc == 0, run.output
    assert "STILL RUNNING" in run.output
    assert "90-cleanup-ledger.sh" in run.output

    doc = json.loads((tmp_path / "evidence" / "stage-handoff.json").read_text())
    assert "STILL RUNNING" in doc["_next"][0]
    assert "deliberately left alive" in doc["_why_not_torn_down"]


# ---------------------------------------------------------------------------
# the refusals the staged path must NOT have relaxed
# ---------------------------------------------------------------------------
def test_a_prerequisite_this_run_did_not_create_is_refused_not_adopted(
        run_create, tmp_path) -> None:
    """The whole risk of a staged lifecycle: "it exists, so continue" is adoption.

    Here the Service standing in front of the edge is NOT the one this run made. A
    name-based gate would proceed, measure another run's fixture, and let this run's
    teardown delete it.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output
    as_created = first.ledger()
    # The live replies are built from what was ACTUALLY created (captured before the
    # ledger is edited below) -- so the Service standing there answers with its real
    # uid. Only the RECORD of it is removed.
    live_replies = _gateway_present_rules(as_created)

    # Forget the Service, keeping everything else: the ledger no longer records the
    # object that is standing there, which is what an adopted stranger looks like.
    edited = dict(as_created)
    edited["k8s"] = [e for e in as_created["k8s"] if e["kind"] != "Service"]
    (tmp_path / "ledger.json").write_text(json.dumps(edited))

    second = run_create(_worker_rules(base=live_replies + base_rules()),
                        args=_worker_stage_args(tmp_path))
    assert second.rc == 1
    assert not second.created()
    assert "THIS RUN'S LEDGER DOES NOT RECORD IT" in second.output


def test_a_replacement_prerequisite_is_refused_even_though_the_name_matches(
        run_create, tmp_path) -> None:
    """Same name, different instance: the reviewed composition is gone.

    The endpoint the receipt carries now fronts an object this run cannot account for,
    and the ledger would delete the replacement as though this run had made it.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output

    second = run_create(
        _worker_stage_rules(first.ledger(), overrides={
            f"Deployment/{GW_NS}/{GW_NAME}": {"stdout": "99999999-9999-9999-9999-999999999999\n"}}),
        args=_worker_stage_args(tmp_path))
    assert second.rc == 1
    assert not second.created()
    assert "DIFFERENT object wearing the same name" in second.output


def test_a_prerequisite_deleted_between_stages_is_refused_with_the_reason(
        run_create, tmp_path) -> None:
    """Recorded, but gone now. A worker created here bootstraps into a void.

    Refusing matters because the run would otherwise file the resulting connection
    errors as MEASUREMENTS of the software under review.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output

    second = run_create(
        _worker_stage_rules(first.ledger(), overrides={
            f"Service/{GW_NS}/{GW_NAME}": not_found("services", GW_NAME)}),
        args=_worker_stage_args(tmp_path))
    assert second.rc == 1
    assert not second.created()
    assert "deleted between stages" in second.output
    assert "bootstrap into a void" in second.output


def test_an_unreadable_prerequisite_is_refused_not_assumed_present(
        run_create, tmp_path) -> None:
    """"Could not check" is not "checked and fine" -- the same rule the absence
    check already applied, kept on the new present-and-ours path."""
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output

    second = run_create(
        _worker_stage_rules(first.ledger(), overrides={
            f"Deployment/{GW_NS}/{GW_NAME}": {
                "rc": 1, "stderr": "Unable to connect to the server: i/o timeout"}}),
        args=_worker_stage_args(tmp_path))
    assert second.rc == 1
    assert not second.created()
    assert "an unreadable cluster is not a confirmed one" in second.output


# ---------------------------------------------------------------------------
# the worker's own NetworkPolicy, through the real script
#
# lib/stage_gate.py can only refuse what the caller tells it about, so its unit tests
# cannot show that the SCRIPT observes the worker-side policy -- passing four
# `--expect` specs for five objects was the original defect and the gate was never
# consulted about the fifth. These drive the real two-stage sequence and assert the
# thing that actually matters: no Job is created. An unconfined pod holding protected
# authority is worse than no measurement at all.
# ---------------------------------------------------------------------------
def test_a_deleted_worker_policy_stops_the_worker_job_being_created(
        run_create, tmp_path) -> None:
    """The gateway policy is standing and only the worker-side one is gone.

    Varying the worker policy alone is the point: while the gate's expectations were
    keyed by kind, the gateway policy's reply answered for both and this scenario
    produced a Job.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output

    second = run_create(
        _worker_stage_rules(first.ledger(), overrides={
            f"NetworkPolicy/{AGENT_NS}/{WORKER_POLICY_NAME}":
                not_found("networkpolicies.networking.k8s.io", WORKER_POLICY_NAME)}),
        args=_worker_stage_args(tmp_path))
    assert second.rc == 1
    assert not second.created("20-worker"), second.calls
    assert not second.created(), second.calls
    assert "deleted between stages" in second.output
    assert "unconfined protected worker" in second.output


def test_a_replaced_worker_policy_stops_the_worker_job_being_created(
        run_create, tmp_path) -> None:
    """Right name, different object: its rules are whatever replaced it chose.

    The name the script would check is present and correct, so this is precisely the
    case identity-by-uid exists for.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output

    second = run_create(
        _worker_stage_rules(first.ledger(), overrides={
            f"NetworkPolicy/{AGENT_NS}/{WORKER_POLICY_NAME}": {
                "stdout": "88888888-8888-8888-8888-888888888888\n"}}),
        args=_worker_stage_args(tmp_path))
    assert second.rc == 1
    assert not second.created("20-worker"), second.calls
    assert "DIFFERENT object wearing the same name" in second.output


def test_an_unreadable_worker_policy_stops_the_worker_job_being_created(
        run_create, tmp_path) -> None:
    """A policy that could not be read is not a policy known to be in force."""
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output

    second = run_create(
        _worker_stage_rules(first.ledger(), overrides={
            f"NetworkPolicy/{AGENT_NS}/{WORKER_POLICY_NAME}": {
                "rc": 1,
                "stderr": 'Error from server (Forbidden): networkpolicies is forbidden'}}),
        args=_worker_stage_args(tmp_path))
    assert second.rc == 1
    assert not second.created("20-worker"), second.calls
    assert "an unreadable cluster is not a confirmed one" in second.output


def test_a_worker_policy_this_run_did_not_create_is_not_adopted(
        run_create, tmp_path) -> None:
    """A confinement policy this run did not create is not one it can vouch for.

    It would also be deleted by this run's teardown, removing confinement another run
    is relying on.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output
    as_created = first.ledger()
    live_replies = _gateway_present_rules(as_created)

    # Forget only the worker policy's RECORD, addressed by all three identity parts:
    # filtering on kind alone would drop the gateway policy too and this test would
    # then pass for the wrong reason.
    edited = dict(as_created)
    edited["k8s"] = [
        e for e in as_created["k8s"]
        if (e["kind"], e["name"], e["namespace"])
        != ("NetworkPolicy", WORKER_POLICY_NAME, AGENT_NS)]
    assert len(edited["k8s"]) == len(as_created["k8s"]) - 1, edited["k8s"]
    (tmp_path / "ledger.json").write_text(json.dumps(edited))

    second = run_create(_worker_rules(base=live_replies + base_rules()),
                        args=_worker_stage_args(tmp_path))
    assert second.rc == 1
    assert not second.created("20-worker"), second.calls
    assert "THIS RUN'S LEDGER DOES NOT RECORD IT" in second.output


def test_the_gateway_stage_records_both_policies_so_the_worker_stage_can_check_them(
        run_create, tmp_path) -> None:
    """The precondition every test above depends on: both policies are in the ledger.

    If the gateway stage recorded only one, the worker stage's verification would be
    impossible rather than merely absent -- and `_uid_of` would fail here with a clear
    reason instead of the refusal tests passing for an unrelated cause.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output
    ledger = first.ledger()

    gateway_uid = _uid_of(ledger, "NetworkPolicy", POLICY_NAME, GW_NS)
    worker_uid = _uid_of(ledger, "NetworkPolicy", WORKER_POLICY_NAME, AGENT_NS)
    # Two distinct objects, not one entry recorded twice.
    assert gateway_uid != worker_uid


def test_rerunning_the_gateway_stage_is_refused_and_points_at_the_next_stage(
        run_create, tmp_path) -> None:
    """The operator's likeliest mistake: running step 10 again to "continue".

    It must not silently adopt, and the refusal has to say what to do instead --
    otherwise the operator's next move is to delete things by hand.
    """
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output

    again = run_create(_gateway_present_rules(first.ledger()) + base_rules(),
                       args=_gateway_args(tmp_path))
    assert again.rc == 1
    assert not again.created()
    assert "this stage has already run" in again.output
    assert "Continue with the next stage instead" in again.output
    # And NOT the stranger-object advice, which would send the operator to a new run id
    # and orphan everything this ledger holds.
    assert "Use a different --run-id" not in again.output


def test_the_worker_stage_without_a_ledger_is_refused(run_create, tmp_path) -> None:
    """There is no fixture to join, so there is nothing this could legitimately do.

    Without the check it would generate a FRESH nonce, then be refused later for a
    confusing reason -- or worse, tag a worker with a nonce no gateway shares.
    """
    run = run_create(base_rules(), args=_worker_stage_args(tmp_path))
    assert run.rc == 1
    assert not run.created()
    assert "--stage worker needs the shared ledger" in run.output


# ---------------------------------------------------------------------------
# the flag combinations that cannot mean anything
# ---------------------------------------------------------------------------
def test_the_worker_stage_without_worker_job_is_refused_not_a_silent_noop(
        run_create, tmp_path) -> None:
    """It would create nothing and exit 0, which reads as a stage that ran.

    An operator seeing "ok" would go on to the evaluation, which would report
    W2-03/04/05 as not_run -- and the reason would look like a missing feature.
    """
    run = run_create(base_rules(), args=["--stage", "worker"])
    assert run.rc == 1
    assert not run.created()
    assert "would create nothing and exit 0" in run.output


def test_the_gateway_stage_with_worker_job_is_refused_as_a_contradiction(
        run_create, tmp_path) -> None:
    """Whichever flag won would be wrong.

    If --worker-job won, the gateway stage needs the endpoint it exists to run without;
    if --stage won, the operator asked for protected authority and silently got none.
    """
    run = run_create(base_rules(),
                     args=["--stage", "gateway", "--worker-job",
                           "--edge-receipt", _edge_receipt(tmp_path)])
    assert run.rc == 1
    assert not run.created()
    assert "contradict each other" in run.output


def test_an_unknown_stage_is_refused_rather_than_defaulted(run_create, tmp_path) -> None:
    """A typo falling through to the default would create authority nobody asked for."""
    run = run_create(base_rules(), args=["--stage", "wroker"])
    assert run.rc == 1
    assert not run.created()
    assert "is not a stage" in run.output


def test_prepare_worker_admits_edge_without_consuming_an_empty_queue(run_create, tmp_path):
    first = run_create(_create_ok_rules(), args=_gateway_args(tmp_path))
    assert first.rc == 0, first.output
    ledger = first.ledger()
    prepared = run_create(_worker_stage_rules(ledger),
        args=_worker_stage_args(tmp_path) + ['--prepare-worker-only'])
    assert prepared.rc == 0, prepared.output
    assert any('replace -f' in call for call in prepared.calls)
    assert not prepared.created('20-worker')
    assert not any('get pod ' in call for call in prepared.calls)
    report = json.loads((tmp_path / 'evidence/fixture-created.json').read_text())
    assert report['worker_job_created'] is False
    assert 'prepared only' in report['worker']['reason']
    assert prepared.ledger() == ledger
    assert (tmp_path / 'evidence/manifests/20-worker.json').is_file()


def test_prepare_worker_refuses_other_stages_before_mutation(run_create):
    run = run_create([], args=['--stage', 'gateway', '--prepare-worker-only'])
    assert run.rc != 0
    assert '--prepare-worker-only requires --stage worker' in run.output
    assert not run.created()
