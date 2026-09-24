#!/usr/bin/env python3
"""Behavioural tests for Wave 2 teardown (issue #3968, W2-10).

Root's review named the negative paths that must be covered: permission failure,
same-name replacement, malformed/foreign ledger, partial creation, and
asynchronous deletion. Each test below drives the real cleanup logic through a
scripted command runner and asserts on the OUTCOME, not on which commands ran --
a test that only checks the argv would pass against code that ignores the reply.

The property under test throughout: absence is only ever reported when a specific
not-found signal was observed. "The call failed" must never become "it is gone".

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

import cleanup  # noqa: E402

TABLE = "adp-dev-webhook-events"
NONCE = "a1b2c3d4e5f60718"
UID = "11111111-2222-3333-4444-555555555555"


class ScriptedRunner:
    """A command runner driven by a list of canned replies, matched by substring.

    Each rule is (fragment, CommandResult). The first rule whose fragment appears
    in the joined argv wins, and rules can be consumed in sequence so a resource
    can be PRESENT on the first probe and ABSENT on a later one -- which is how
    asynchronous deletion is simulated without sleeping.
    """

    def __init__(self, rules: list[tuple[str, cleanup.CommandResult]]) -> None:
        self.rules = list(rules)
        self.calls: list[str] = []

    def __call__(self, argv):
        joined = " ".join(argv)
        self.calls.append(joined)
        for index, (fragment, result) in enumerate(self.rules):
            if fragment in joined:
                self.rules.pop(index)
                return result
        raise AssertionError(f"no scripted reply for: {joined}")


def ok(stdout: str = "") -> cleanup.CommandResult:
    return cleanup.CommandResult(0, stdout, "")


def err(stderr: str, code: int = 1) -> cleanup.CommandResult:
    return cleanup.CommandResult(code, "", stderr)


def k8s_json(uid: str) -> str:
    return json.dumps({"metadata": {"uid": uid, "name": "x"}})


def queue_entry(**over) -> dict:
    entry = {"name": "adp-dev-w2-fixture.fifo", "url": "https://sqs/q",
             "owner_tag_nonce": NONCE, "delete": True, "created_by_this_run": True}
    entry.update(over)
    return entry


def k8s_entry(**over) -> dict:
    entry = {"kind": "Deployment", "name": "w2-fixture-gateway", "namespace": "adp-gateway",
             "uid": UID, "delete": True, "created_by_this_run": True}
    entry.update(over)
    return entry


NO_SLEEP = cleanup.Sleeper()


# ---------------------------------------------------------------------------
# permission failure must never read as absence
# ---------------------------------------------------------------------------
def test_queue_access_denied_is_not_absence() -> None:
    """THE headline defect: the published code treated any error as "already gone".

    An AccessDenied on get-queue-url left a live fixture queue -- with a control
    listener attached -- reported as cleaned up.
    """
    run = ScriptedRunner([("get-queue-url", err(
        "An error occurred (AccessDenied) when calling the GetQueueUrl operation"))])
    presence, detail = cleanup.queue_presence(run, "q")
    assert presence is cleanup.Presence.UNKNOWN
    assert "AccessDenied" in detail


def test_queue_not_found_code_is_absence() -> None:
    """The one error that DOES mean absence, so UNKNOWN is not merely a blanket."""
    run = ScriptedRunner([("get-queue-url", err(
        "An error occurred (AWS.SimpleQueueService.NonExistentQueue) when calling "
        "the GetQueueUrl operation: The specified queue does not exist"))])
    assert cleanup.queue_presence(run, "q")[0] is cleanup.Presence.ABSENT


def test_expired_credential_on_queue_delete_fails_cleanup() -> None:
    """A whole-run credential expiry must surface as a FAILED cleanup, not a clean one."""
    run = ScriptedRunner([("get-queue-url", err("ExpiredToken: The security token expired"))])
    record = cleanup.delete_queue(run, queue_entry(), run_nonce=NONCE, sleep=NO_SLEEP)
    assert record["confirmed_absent"] is False
    assert "ExpiredToken" in record["error"]


def test_unreachable_cluster_is_not_absence() -> None:
    """`kubectl get` exits 1 for NotFound AND for an unreachable API server.

    The published code read only whether stdout was empty, so a dead kubeconfig
    reported every fixture object as gone.
    """
    run = ScriptedRunner([("kubectl get", err(
        "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"))])
    presence, detail = cleanup.k8s_presence(run, "Deployment", "d", "ns")
    assert presence is cleanup.Presence.UNKNOWN
    assert "Unable to connect" in detail


def test_k8s_forbidden_is_not_absence() -> None:
    run = ScriptedRunner([("kubectl get", err(
        'Error from server (Forbidden): deployments.apps "d" is forbidden'))])
    assert cleanup.k8s_presence(run, "Deployment", "d", "ns")[0] is cleanup.Presence.UNKNOWN


def test_k8s_notfound_is_absence() -> None:
    run = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): deployments.apps "d" not found'))])
    assert cleanup.k8s_presence(run, "Deployment", "d", "ns")[0] is cleanup.Presence.ABSENT


# ---------------------------------------------------------------------------
# same-name replacement must be skipped, not destroyed
# ---------------------------------------------------------------------------
def test_same_name_different_uid_is_not_deleted() -> None:
    """A recreated object of the same name is a DIFFERENT object.

    Deleting by name alone -- what the published cleanup did -- would destroy
    somebody else's workload that happens to share the fixture's name.
    """
    run = ScriptedRunner([("kubectl get", ok(k8s_json("a-completely-different-uid")))])
    record = cleanup.delete_k8s(run, k8s_entry(), sleep=NO_SLEEP)
    assert record["skipped"] is True
    assert record["confirmed_absent"] is False
    assert not any("kubectl delete" in call for call in run.calls), \
        "must not issue a delete for an object this run did not create"


def test_queue_with_foreign_nonce_tag_is_not_deleted() -> None:
    """The protected probe queue is protected by evidence, not by a hardcoded name.

    A name-based exception list only ever covers the resource somebody remembered;
    a missing owner tag excludes every queue this run cannot prove it created.
    """
    run = ScriptedRunner([
        ("get-queue-url", ok("https://sqs/probe")),
        ("list-queue-tags", ok(json.dumps({"Tags": {"adp-w2-nonce": "someone-elses-nonce"}}))),
    ])
    record = cleanup.delete_queue(run, queue_entry(), run_nonce=NONCE, sleep=NO_SLEEP)
    assert record["skipped"] is True
    assert not any("delete-queue" in call for call in run.calls)


def test_queue_with_no_tags_at_all_is_not_deleted() -> None:
    """An untagged queue of a matching name is exactly the probe-queue scenario."""
    run = ScriptedRunner([
        ("get-queue-url", ok("https://sqs/probe")),
        ("list-queue-tags", ok("{}")),
    ])
    record = cleanup.delete_queue(run, queue_entry(), run_nonce=NONCE, sleep=NO_SLEEP)
    assert record["skipped"] is True
    assert record["live_tag_nonce"] is None
    assert not any("delete-queue" in call for call in run.calls)


def test_unreadable_tags_block_the_delete() -> None:
    """If ownership cannot be READ it has not been proven; do not delete."""
    run = ScriptedRunner([
        ("get-queue-url", ok("https://sqs/q")),
        ("list-queue-tags", err("AccessDenied")),
    ])
    record = cleanup.delete_queue(run, queue_entry(), run_nonce=NONCE, sleep=NO_SLEEP)
    assert record["deleted"] is False
    assert record["confirmed_absent"] is False
    assert not any("delete-queue" in call for call in run.calls)


# ---------------------------------------------------------------------------
# asynchronous deletion
# ---------------------------------------------------------------------------
def test_queue_absence_is_polled_not_assumed() -> None:
    """SQS documents up to 60s before a deleted queue is really gone.

    The published code set absent_confirmed from delete-queue's own exit code,
    confirming a state nobody observed.
    """
    run = ScriptedRunner([
        ("get-queue-url", ok("https://sqs/q")),
        ("list-queue-tags", ok(json.dumps({"Tags": {"adp-w2-nonce": NONCE}}))),
        ("delete-queue", ok()),
        ("get-queue-url", ok("https://sqs/q")),               # still there
        ("get-queue-url", ok("https://sqs/q")),               # still there
        ("get-queue-url", err("AWS.SimpleQueueService.NonExistentQueue")),  # now gone
    ])
    sleeper = cleanup.Sleeper()
    record = cleanup.delete_queue(run, queue_entry(), run_nonce=NONCE, sleep=sleeper)
    assert record["deleted"] is True
    assert record["confirmed_absent"] is True
    assert len(sleeper.calls) == 2, "must have waited between re-probes"


def test_queue_still_present_after_bounded_polling_fails() -> None:
    """Give up honestly rather than reporting an absence that never arrived."""
    rules = [("get-queue-url", ok("https://sqs/q")),
             ("list-queue-tags", ok(json.dumps({"Tags": {"adp-w2-nonce": NONCE}}))),
             ("delete-queue", ok())]
    rules += [("get-queue-url", ok("https://sqs/q")) for _ in range(10)]
    run = ScriptedRunner(rules)
    record = cleanup.delete_queue(run, queue_entry(), run_nonce=NONCE,
                                 sleep=NO_SLEEP, attempts=4)
    assert record["confirmed_absent"] is False
    assert "still resolvable" in record["error"]


def test_finalizer_held_object_is_reported_not_confirmed() -> None:
    """A Job with a finalizer can outlive its delete call indefinitely."""
    rules = [("kubectl get", ok(k8s_json(UID))),
             ("w2-k8s-delete-with-preconditions", ok("deployment.apps deleted"))]
    rules += [("kubectl get", ok(k8s_json(UID))) for _ in range(10)]
    run = ScriptedRunner(rules)
    record = cleanup.delete_k8s(run, k8s_entry(), sleep=NO_SLEEP, attempts=3)
    assert record["deleted"] is True
    assert record["confirmed_absent"] is False
    assert "finalizer" in record["error"]


def test_k8s_absence_after_delete_is_confirmed_by_reread() -> None:
    run = ScriptedRunner([
        ("kubectl get", ok(k8s_json(UID))),
        ("w2-k8s-delete-with-preconditions", ok("deployment.apps deleted")),
        ("kubectl get", err('Error from server (NotFound): deployments.apps '
                            '"w2-fixture-gateway" not found')),
    ])
    record = cleanup.delete_k8s(run, k8s_entry(), sleep=NO_SLEEP)
    assert record["confirmed_absent"] is True


def test_unreachable_cluster_during_confirmation_is_unverified() -> None:
    """The delete succeeded but the confirming read could not be made.

    That is not a clean teardown: the object's state is unknown.
    """
    rules = [("kubectl get", ok(k8s_json(UID))),
             ("w2-k8s-delete-with-preconditions", ok())]
    rules += [("kubectl get", err("Unable to connect to the server")) for _ in range(10)]
    run = ScriptedRunner(rules)
    record = cleanup.delete_k8s(run, k8s_entry(), sleep=NO_SLEEP, attempts=3)
    assert record["confirmed_absent"] is False
    assert "NOT as absent" in record["error"]


# ---------------------------------------------------------------------------
# partial creation
# ---------------------------------------------------------------------------
def test_partial_creation_cleans_what_exists_and_confirms_what_never_did() -> None:
    """A run that failed midway leaves some objects created and some not.

    Both must end CONFIRMED absent: one by deletion, one by observing it was
    never there. Neither may be silently skipped.
    """
    ledger = {
        "ledger_version": 2, "run_id": "r", "run_nonce": NONCE,
        "account_id": "879318057152", "synthetic_rows": [],
        "k8s": [k8s_entry(name="created-ok"), k8s_entry(name="never-created", uid="uid-2")],
        "queues": [],
    }
    run = ScriptedRunner([
        ("kubectl get Deployment created-ok", ok(k8s_json(UID))),
        ("w2-k8s-delete-with-preconditions", ok()),
        ("kubectl get Deployment created-ok", err(
            'Error from server (NotFound): deployments.apps "created-ok" not found')),
        ("kubectl get Deployment never-created", err(
            'Error from server (NotFound): deployments.apps "never-created" not found')),
    ])
    outcome = cleanup.run_cleanup(ledger, table=TABLE, run=run, sleep=NO_SLEEP)
    assert outcome["cleanup_ok"] is True
    assert all(r["confirmed_absent"] for r in outcome["k8s"])
    assert outcome["unverified"] == []


def test_entry_without_uid_is_refused_not_deleted_by_name() -> None:
    """A partially-recorded entry (created, uid never captured) must not be guessed at."""
    run = ScriptedRunner([])  # no command may be issued at all
    record = cleanup.delete_k8s(run, k8s_entry(uid=""), sleep=NO_SLEEP)
    assert record["skipped"] is True
    assert record["confirmed_absent"] is False
    assert run.calls == []


def test_adopted_entry_is_never_deleted() -> None:
    run = ScriptedRunner([])
    record = cleanup.delete_k8s(run, k8s_entry(created_by_this_run=False), sleep=NO_SLEEP)
    assert record["skipped"] is True
    assert run.calls == []


def test_queue_run_bound_does_not_default_to_true() -> None:
    """The published code used `q.get("run_bound", True)`.

    A ledger entry that never proved ownership was therefore deletable by default.
    Absence of evidence must deny, not permit.
    """
    entry = {"name": "adp-dev-w2.fifo", "delete": True}  # no ownership fields at all
    run = ScriptedRunner([])
    record = cleanup.delete_queue(run, entry, run_nonce=NONCE, sleep=NO_SLEEP)
    assert record["skipped"] is True
    assert run.calls == []


# ---------------------------------------------------------------------------
# rows: both key halves, consistent read
# ---------------------------------------------------------------------------
def test_row_delete_requires_both_key_halves() -> None:
    """Deleting on a guessed range key can match an unrelated real webhook event."""
    run = ScriptedRunner([])
    record = cleanup.delete_row(run, TABLE, {"event_id": "evt-1"}, sleep=NO_SLEEP)
    assert record["confirmed_absent"] is False
    assert run.calls == []


def test_row_delete_uses_consistent_read_for_confirmation() -> None:
    """An eventually-consistent read can report an item gone before it is."""
    run = ScriptedRunner([
        ("delete-item", ok()),
        ("get-item", ok("{}")),
    ])
    record = cleanup.delete_row(
        run, TABLE, {"event_id": "evt-1", "arrived_at": "2026-09-24T00:00:00Z"}, sleep=NO_SLEEP)
    assert record["confirmed_absent"] is True
    assert any("--consistent-read" in call for call in run.calls)


def test_row_still_present_is_reported() -> None:
    rules = [("delete-item", ok())]
    rules += [("get-item", ok(json.dumps({"Item": {"event_id": {"S": "evt-1"}}})))
              for _ in range(10)]
    run = ScriptedRunner(rules)
    record = cleanup.delete_row(
        run, TABLE, {"event_id": "evt-1", "arrived_at": "2026-09-24T00:00:00Z"},
        sleep=NO_SLEEP, attempts=3)
    assert record["confirmed_absent"] is False


def test_row_get_item_error_is_not_absence() -> None:
    rules = [("delete-item", ok())]
    rules += [("get-item", err("ProvisionedThroughputExceededException")) for _ in range(10)]
    run = ScriptedRunner(rules)
    record = cleanup.delete_row(
        run, TABLE, {"event_id": "evt-1", "arrived_at": "2026-09-24T00:00:00Z"},
        sleep=NO_SLEEP, attempts=2)
    assert record["confirmed_absent"] is False
    assert "NOT as absent" in record["error"]


# ---------------------------------------------------------------------------
# whole-run reporting honesty
# ---------------------------------------------------------------------------
def test_cleanup_ok_is_false_when_anything_is_unverified() -> None:
    """The evaluation reads cleanup_ok. It must be False on doubt.

    W2-10 gates on this flag; a cleanup that reports ok while a protected fixture
    survives is worse than one that reports failure.
    """
    ledger = {
        "ledger_version": 2, "run_id": "r", "run_nonce": NONCE,
        "account_id": "879318057152", "synthetic_rows": [],
        "k8s": [], "queues": [queue_entry()],
    }
    run = ScriptedRunner([("get-queue-url", err("AccessDenied"))])
    outcome = cleanup.run_cleanup(ledger, table=TABLE, run=run, sleep=NO_SLEEP)
    assert outcome["cleanup_ok"] is False
    assert outcome["unverified"][0]["bucket"] == "queues"
    assert "AccessDenied" in outcome["unverified"][0]["reason"]


def test_skipped_foreign_resource_also_fails_cleanup_ok() -> None:
    """Refusing to delete is correct, but it still leaves the fixture standing.

    Reporting ok here would claim an isolation teardown that did not happen.
    """
    ledger = {
        "ledger_version": 2, "run_id": "r", "run_nonce": NONCE,
        "account_id": "879318057152", "synthetic_rows": [], "queues": [],
        "k8s": [k8s_entry()],
    }
    run = ScriptedRunner([("kubectl get", ok(k8s_json("different-uid")))])
    outcome = cleanup.run_cleanup(ledger, table=TABLE, run=run, sleep=NO_SLEEP)
    assert outcome["cleanup_ok"] is False


def test_dry_run_issues_no_commands() -> None:
    ledger = {
        "ledger_version": 2, "run_id": "r", "run_nonce": NONCE,
        "account_id": "879318057152",
        "synthetic_rows": [{"event_id": "e", "arrived_at": "a"}],
        "k8s": [k8s_entry()], "queues": [queue_entry()],
    }
    run = ScriptedRunner([])
    outcome = cleanup.run_cleanup(ledger, table=TABLE, run=run, dry_run=True, sleep=NO_SLEEP)
    assert run.calls == []
    assert outcome["dry_run"] is True


def test_dry_run_does_not_claim_a_verified_cleanup() -> None:
    """A dry run deletes nothing, so it cannot report cleanup_ok: true.

    Otherwise a plan-only invocation could be filed as the W2-10 cleanup proof --
    the same class of defect as treating an error as absence.
    """
    ledger = {
        "ledger_version": 2, "run_id": "r", "run_nonce": NONCE,
        "account_id": "879318057152", "synthetic_rows": [],
        "k8s": [k8s_entry()], "queues": [queue_entry()],
    }
    outcome = cleanup.run_cleanup(ledger, table=TABLE, run=ScriptedRunner([]),
                                  dry_run=True, sleep=NO_SLEEP)
    assert outcome["cleanup_ok"] is None
    assert outcome["cleanup_ok"] is not True


def test_empty_ledger_is_ok_but_not_evidence_of_cleanup() -> None:
    """An empty ledger reports ok because there is nothing outstanding.

    Recorded deliberately: combined with the foreign/malformed-ledger refusals in
    test_ownership.py, an empty ledger cannot be reached by MISREADING a populated
    one -- a truncated ledger raises instead of parsing as empty.
    """
    ledger = {"ledger_version": 2, "run_id": "r", "run_nonce": NONCE,
              "account_id": "879318057152", "synthetic_rows": [], "k8s": [], "queues": []}
    outcome = cleanup.run_cleanup(ledger, table=TABLE, run=ScriptedRunner([]), sleep=NO_SLEEP)
    assert outcome["cleanup_ok"] is True
    assert outcome["unverified"] == []


@pytest.mark.parametrize("stdout", ["not json at all", "{truncated"])
def test_unparseable_kubectl_output_is_unknown(stdout: str) -> None:
    run = ScriptedRunner([("kubectl get", ok(stdout))])
    assert cleanup.k8s_presence(run, "Deployment", "d", "ns")[0] is cleanup.Presence.UNKNOWN


# ---------------------------------------------------------------------------
# Root's reproductions (issue #3968 comment 5805941649). Each of these FAILED
# against the previously published revision; they are the regression floor.
# ---------------------------------------------------------------------------
def test_credential_exec_failure_is_not_kubernetes_absence() -> None:
    """Root's repro 1: a missing credential helper read as "the object is gone".

    `exec: executable aws not found` comes from the credential exec PLUGIN. The
    API server was never contacted, so nothing was learned about the object. The
    published substring test for "not found" matched the word inside that message
    and reported ABSENT -- so a live fixture gateway was recorded as torn down.
    """
    runner = ScriptedRunner([("kubectl get", err(
        "Unable to connect to the server: getting credentials: "
        "exec: executable aws not found"))])
    presence, detail = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN, (
        "a credential-plugin failure must never be classified as absence")
    assert "aws not found" in detail


@pytest.mark.parametrize("stderr", [
    # Every one of these contains "not found" somewhere but says nothing about
    # whether the object exists.
    "Unable to connect to the server: getting credentials: exec: executable aws not found",
    "error: exec plugin not found in PATH",
    'error: unable to recognize "STDIN": no matches for kind "Deployment"',
    "Unable to connect to the server: dial tcp: lookup eks.amazonaws.com: no such host",
    "error: You must be logged in to the server (Unauthorized)",
    'Error from server (Forbidden): deployments.apps "w2-fixture-gateway" is forbidden',
    "Unable to connect to the server: net/http: TLS handshake timeout",
])
def test_transport_and_auth_failures_are_never_absence(stderr: str) -> None:
    runner = ScriptedRunner([("kubectl get", err(stderr))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN, f"misclassified: {stderr!r}"


def test_notfound_about_a_different_object_is_not_this_objects_absence() -> None:
    """A NotFound naming some OTHER resource proves nothing about ours."""
    runner = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): deployments.apps "some-other-deployment" not found'))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_typed_notfound_status_naming_this_object_is_absence() -> None:
    """The real signal still works: a typed Status for THIS object."""
    status = json.dumps({"kind": "Status", "reason": "NotFound", "code": 404,
                         "details": {"name": "w2-fixture-gateway", "kind": "deployments"}})
    runner = ScriptedRunner([("kubectl get", cleanup.CommandResult(1, status, ""))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.ABSENT


def test_kubectl_rendered_notfound_for_this_object_is_absence() -> None:
    runner = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): deployments.apps "w2-fixture-gateway" not found'))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.ABSENT


def test_present_object_without_uid_is_unknown_not_present() -> None:
    """A read-back with no uid cannot support an ownership comparison."""
    runner = ScriptedRunner([("kubectl get", ok(json.dumps({"metadata": {"name": "x"}})))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_same_name_replacement_is_refused_by_the_server_not_deleted() -> None:
    """Root's repro 2: the read/delete-by-name race deleted a FOREIGN object.

    Sequence root reproduced against the published code:
      get -> owned uid  ->  replacement appears  ->  delete BY NAME removes the
      replacement  ->  confirming re-read finds nothing  ->  confirmed_absent=True

    The confirming read could not detect the problem, because the delete it was
    confirming is what produced the absence it observed.

    With a server-side uid precondition the delete is rejected (409 Conflict) and
    nothing is removed, so the foreign object survives and cleanup reports a
    failure rather than a false success.
    """
    conflict = json.dumps({
        "kind": "Status", "status": "Failure", "reason": "Conflict", "code": 409,
        "message": 'Precondition failed: UID in precondition: owned-uid-111, '
                   'UID in object meta: foreign-uid-222',
    })
    runner = ScriptedRunner([
        ("kubectl get", ok(k8s_json(UID))),                      # our object is live
        ("w2-k8s-delete-with-preconditions", cleanup.CommandResult(1, conflict, "HTTP 409")),
    ])
    record = cleanup.delete_k8s(runner, k8s_entry(), sleep=NO_SLEEP)

    assert record["delete_status"] == "uid_mismatch"
    assert record["deleted"] is False, "a replacement must NOT be deleted"
    assert record["confirmed_absent"] is False, (
        "refusing to delete is not the same as having deleted; this must fail cleanup")
    # And crucially: no delete-by-name was ever issued.
    assert not any("kubectl delete" in call for call in runner.calls), (
        "delete-by-name cannot enforce ownership and must not be used")


def test_delete_carries_the_recorded_uid_as_a_server_precondition() -> None:
    """The uid must actually be in the request body, not merely compared locally."""
    runner = ScriptedRunner([
        ("kubectl get", ok(k8s_json(UID))),
        ("w2-k8s-delete-with-preconditions", ok(json.dumps({"kind": "Status",
                                                            "status": "Success"}))),
        ("kubectl get", err('Error from server (NotFound): deployments.apps '
                            '"w2-fixture-gateway" not found')),
    ])
    record = cleanup.delete_k8s(runner, k8s_entry(), sleep=NO_SLEEP)
    assert record["confirmed_absent"] is True

    delete_call = next(c for c in runner.calls if "w2-k8s-delete-with-preconditions" in c)
    assert UID in delete_call, "the recorded uid must travel in the DeleteOptions body"
    assert "preconditions" in delete_call
    assert "/namespaces/adp-gateway/deployments/w2-fixture-gateway" in delete_call


def test_unmapped_kind_refuses_rather_than_deleting_by_name() -> None:
    """An unknown kind has no API path, so no precondition can be built.

    Falling back to delete-by-name here would reintroduce the race for exactly
    the resources nobody thought about.
    """
    status, detail = cleanup.delete_k8s_with_uid_precondition(
        ScriptedRunner([]), kind="CustomThing", name="x", namespace="ns", uid="u")
    assert status == "unsupported"
    assert "delete-by-name" in detail


def test_preconditioned_delete_transport_error_is_not_absence() -> None:
    runner = ScriptedRunner([
        ("kubectl get", ok(k8s_json(UID))),
        ("w2-k8s-delete-with-preconditions",
         cleanup.CommandResult(1, "", "proxy request failed: Connection refused")),
    ])
    record = cleanup.delete_k8s(runner, k8s_entry(), sleep=NO_SLEEP)
    assert record["delete_status"] == "error"
    assert record["confirmed_absent"] is False


def test_retained_skipped_resource_is_not_a_successful_cleanup() -> None:
    """Root: intentionally retained items cannot count as full fixture cleanup."""
    ledger = {
        "run_id": "w2-x", "run_nonce": NONCE, "synthetic_rows": [], "queues": [],
        "k8s": [k8s_entry(delete=False)],
    }
    outcome = cleanup.run_cleanup(ledger, table="t", run=ScriptedRunner([]),
                                  sleep=NO_SLEEP)
    assert outcome["cleanup_ok"] is False, (
        "a ledger entry that was never removed cannot be reported as cleaned up")
    assert outcome["retained"], "the retained item must be named in the record"


# ---------------------------------------------------------------------------
# teardown ORDER: isolation outlives what it contains
# ---------------------------------------------------------------------------
def policy_entry(**over) -> dict:
    entry = {"kind": "NetworkPolicy", "name": "w2-fixture-policy",
             "namespace": "adp-gateway", "uid": "uid-policy-0001",
             "delete": True, "created_by_this_run": True}
    entry.update(over)
    return entry


class OrderedCluster:
    """A cluster that actually honours deletes, so ORDER is observable.

    ScriptedRunner matches by substring and consumes rules, which is ideal for
    "present then absent" but cannot express "absent because I deleted it". Teardown
    ordering is only meaningful against a runner whose reads reflect prior writes.
    """

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.calls: list[str] = []

    # The NotFound replies must name the real resource: is_k8s_not_found matches the
    # kind AND name deliberately, so a generic "not found" is UNKNOWN rather than
    # absence. A stub that cut that corner would make absence unconfirmable and this
    # test would fail for a reason having nothing to do with ordering.
    ABSENT = {
        "policy": 'Error from server (NotFound): networkpolicies.networking.k8s.io '
                  '"w2-fixture-policy" not found',
        "workload": 'Error from server (NotFound): deployments.apps '
                    '"w2-fixture-gateway" not found',
    }

    def __call__(self, argv):
        joined = " ".join(argv)
        self.calls.append(joined)
        kind = ("policy" if "networkpolicies" in joined or "NetworkPolicy" in joined
                else "workload")
        if "w2-k8s-delete-with-preconditions" in joined:
            self.deleted.append(kind)
            return ok(json.dumps({"status": "Success"}))
        if "get" in joined:
            if kind in self.deleted:
                return err(self.ABSENT[kind])
            return ok(k8s_json("uid-policy-0001" if kind == "policy" else UID))
        return ok("{}")

    @property
    def delete_order(self) -> list[str]:
        return list(self.deleted)


def ordered_ledger(*entries) -> dict:
    return {"run_id": "w2-x", "run_nonce": NONCE, "synthetic_rows": [], "queues": [],
            "k8s": list(entries)}


def test_the_workload_is_removed_before_the_isolation_that_contains_it() -> None:
    """A teardown must not create the window that creation was careful to avoid.

    10-create-fixture.sh applies the NetworkPolicies FIRST, "before anything can
    listen", so the fixture is never reachable-but-unprotected. Tearing down in
    ledger (creation) order inverts that and deletes the policy while the
    control-enabled workload is still running -- an unrestricted control listener in
    the live environment this evaluation exists to keep contained.

    The ledger is deliberately given in creation order here, policy first, because
    that is the order the defect read from.
    """
    cluster = OrderedCluster()
    outcome = cleanup.run_cleanup(
        ordered_ledger(policy_entry(), k8s_entry()),
        table="t", run=cluster, sleep=NO_SLEEP)

    assert cluster.delete_order == ["workload", "policy"], (
        "the isolation was removed before the workload it contains")
    assert outcome["cleanup_ok"] is True, outcome["unverified"]


def test_isolation_is_retained_when_the_workload_cannot_be_confirmed_gone() -> None:
    """The ordering alone is not enough: the second step must be CONDITIONAL.

    If the workload survives (a finalizer, a failed delete), deleting the policy
    anyway strips containment from something still running -- converting a failed
    teardown into an exposure. So the policy delete must not be ATTEMPTED, not merely
    reported afterwards.
    """
    # The workload is present on every read, so its absence is never confirmed.
    runner = ScriptedRunner([
        ("kubectl get", ok(k8s_json(UID))),
        ("w2-k8s-delete-with-preconditions", ok(json.dumps({"status": "Success"}))),
    ] + [("kubectl get", ok(k8s_json(UID)))] * 20)

    outcome = cleanup.run_cleanup(
        ordered_ledger(policy_entry(), k8s_entry()),
        table="t", run=runner, sleep=NO_SLEEP)

    policy_deletes = [c for c in runner.calls
                      if "delete-with-preconditions" in c and "networkpolicies" in c]
    assert not policy_deletes, (
        "the isolation was deleted even though the workload could not be confirmed gone")
    record = next(r for r in outcome["k8s"] if r["kind"] == "NetworkPolicy")
    assert record["skipped"] is True and record["confirmed_absent"] is False
    assert "unrestricted" in record["reason"], "the record must say WHY it was retained"
    assert outcome["cleanup_ok"] is False, (
        "a fixture whose isolation had to be retained is not fully cleaned up")


def test_a_dry_run_states_the_real_teardown_order() -> None:
    """The plan has to describe what would actually happen, or it is not a plan.

    A dry run that listed the policy first would tell the operator the opposite of
    the order the real teardown uses.
    """
    outcome = cleanup.run_cleanup(
        ordered_ledger(policy_entry(), k8s_entry()),
        table="t", run=ScriptedRunner([]), dry_run=True, sleep=NO_SLEEP)
    kinds = [record["kind"] for record in outcome["k8s"]]
    assert kinds == ["Deployment", "NetworkPolicy"], kinds
    assert outcome["cleanup_ok"] is None, "a dry run verifies nothing"


# ---------------------------------------------------------------------------
# a 404 is only absence when it identifies the object we asked about
# ---------------------------------------------------------------------------
# Root's finding (5806825628, restated against d92ee87b9): "generic Status404/
# missing details or wrong-object NotFound can qualify as absent, including
# delete_k8s_with_uid_precondition matching any404. This could bypass the new
# policy retention."
#
# The bypass is the part that makes this a SAFETY defect rather than a reporting
# one. `not_found` is the only delete status that sets confirmed_absent WITHOUT a
# delete having occurred, so a 404 from anywhere -- a wrong API path, a pruned API
# group, a proxy that answered before reaching the API server, RBAC that hides a
# resource behind NotFound -- empties the surviving-workload list, and the policy
# retention added in d92ee87b9 then removes the isolation protecting a workload
# that is still running.

DETAILLESS_404 = json.dumps({"kind": "Status", "reason": "NotFound", "code": 404,
                             "message": "the server could not find the requested resource"})


def test_a_404_with_no_details_is_not_absence() -> None:
    """A reason without details names nothing, so it cannot name our object."""
    runner = ScriptedRunner([("kubectl get", cleanup.CommandResult(1, DETAILLESS_404, ""))])
    presence, detail = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN, (
        f"a detail-less 404 was read as absence: {detail}")


def test_a_404_naming_a_different_kind_is_not_this_objects_absence() -> None:
    """Names are unique only within a resource.

    `configmaps "w2-fixture-gateway" not found` is a true statement that says
    nothing about `Deployment/w2-fixture-gateway`. Matching on name alone -- which
    both classifier paths did -- accepts it as absence of the Deployment.
    """
    status = json.dumps({"kind": "Status", "reason": "NotFound", "code": 404,
                         "details": {"name": "w2-fixture-gateway", "kind": "configmaps"}})
    runner = ScriptedRunner([("kubectl get", cleanup.CommandResult(1, status, ""))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_a_rendered_404_naming_a_different_kind_is_not_absence() -> None:
    """The same requirement on kubectl's prose rendering of that Status."""
    runner = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): configmaps "w2-fixture-gateway" not found'))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_a_rendered_404_whose_name_contains_our_plural_is_not_absence() -> None:
    """Root's executed reproduction (5807547817).

    `configmaps "deployments-probe" not found` satisfied BOTH independent substring
    tests for the absence of `Deployment/deployments-probe`: the expected plural
    `deployments` appears in the message (inside the NAME), and the quoted name
    appears too. The message is in fact about a ConfigMap, so it says nothing about
    the Deployment -- which may still be running, with the policy retention then
    clearing its isolation.

    The parts must be matched in their grammatical positions, not looked for
    anywhere in the text.
    """
    runner = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): configmaps "deployments-probe" not found'))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "deployments-probe", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_a_rendered_404_from_a_different_api_group_is_not_absence() -> None:
    """Root's executed reproduction (5808156074).

    `deployments.other.example "deployments-probe" not found` is a statement about
    the `deployments` resource in the group `other.example`. The grammar was already
    capturing that group and then discarding it, so the sentence was read as the
    absence of `Deployment` in `apps`.

    A plural is only unique within a group: a custom resource can share a plural
    with a built-in and be a completely different resource. So the group, when the
    server states one, has to be the group we asked about -- otherwise cleanup
    reports a workload gone on the evidence of some unrelated resource's absence.
    """
    runner = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): deployments.other.example '
        '"deployments-probe" not found'))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "deployments-probe", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_a_rendered_404_stating_our_own_group_reads_as_absence() -> None:
    """The positive half: the group is compared, not merely required to be absent."""
    runner = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): networkpolicies.networking.k8s.io '
        '"w2-fixture-policy" not found'))])
    presence, _ = cleanup.k8s_presence(
        runner, "NetworkPolicy", "w2-fixture-policy", "adp-gateway")
    assert presence is cleanup.Presence.ABSENT


def test_a_core_resource_reported_with_a_group_suffix_is_not_absence() -> None:
    """A core resource lives in the empty group, so a named group contradicts it.

    `configmaps.example.com "x"` is a custom resource that happens to share the
    plural `configmaps`; it is not the core ConfigMap.
    """
    runner = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): configmaps.example.com '
        '"w2-fixture-config" not found'))])
    presence, _ = cleanup.k8s_presence(
        runner, "ConfigMap", "w2-fixture-config", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_group_is_derived_from_the_same_mapping_as_the_plural() -> None:
    """Core resources report "", grouped ones their group, unmapped ones None.

    Derived from K8S_API_PATHS so the plural and the group can never disagree about
    which resource a kind means.
    """
    assert cleanup.k8s_resource_group("ConfigMap") == ""
    assert cleanup.k8s_resource_group("Pod") == ""
    assert cleanup.k8s_resource_group("Deployment") == "apps"
    assert cleanup.k8s_resource_group("Job") == "batch"
    assert cleanup.k8s_resource_group("NetworkPolicy") == "networking.k8s.io"
    assert cleanup.k8s_resource_group("Widget") is None


def test_the_canonical_rendered_404_still_reads_as_absence() -> None:
    """The accepting case, in both of kubectl's renderings.

    A stricter classifier is only correct if it still recognises the real thing:
    `deployments.apps "x" not found` (grouped) and `configmaps "x" not found`
    (core, no group suffix) are what the server actually emits.
    """
    grouped = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): deployments.apps "w2-fixture-gateway" not found'))])
    presence, _ = cleanup.k8s_presence(
        grouped, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.ABSENT

    core = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): configmaps "w2-fixture-config" not found'))])
    presence, _ = cleanup.k8s_presence(
        core, "ConfigMap", "w2-fixture-config", "adp-gateway")
    assert presence is cleanup.Presence.ABSENT


def test_a_rendered_404_about_another_object_of_our_kind_is_not_absence() -> None:
    """Right resource slot, wrong name. The name has to match exactly too."""
    runner = ScriptedRunner([("kubectl get", err(
        'Error from server (NotFound): deployments.apps "some-other-app" not found'))])
    presence, _ = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_a_404_for_an_unmapped_kind_cannot_be_confirmed() -> None:
    """If we cannot say which plural to expect, we cannot check the reply is ours.

    Unprovable, therefore UNKNOWN -- which fails the cleanup, rather than passing it
    on a guess. The same refusal-to-guess that keeps delete_k8s from building an API
    path for an unmapped kind.
    """
    status = json.dumps({"kind": "Status", "reason": "NotFound", "code": 404,
                         "details": {"name": "w2-thing", "kind": "widgets"}})
    runner = ScriptedRunner([("kubectl get", cleanup.CommandResult(1, status, ""))])
    presence, _ = cleanup.k8s_presence(runner, "Widget", "w2-thing", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN


def test_a_zero_exit_404_is_held_to_the_same_standard() -> None:
    """A success exit code makes a non-identifying 404 look MORE trustworthy.

    Some kubectl paths print a Status to stdout and exit 0. The identity requirement
    has to live in the classifier, not in the exit-code branch, or it is bypassed by
    whichever branch forgot it.
    """
    runner = ScriptedRunner([("kubectl get", ok(DETAILLESS_404))])
    presence, detail = cleanup.k8s_presence(
        runner, "Deployment", "w2-fixture-gateway", "adp-gateway")
    assert presence is cleanup.Presence.UNKNOWN, detail
    assert "does not identify" in detail


def test_an_unidentified_404_from_the_delete_is_unverified_not_absent() -> None:
    """The delete path's own classifier: any404 => not_found was the second half.

    `not_found` asserts confirmed_absent without having deleted anything, so it is
    the one status here that must prove the server was talking about our object.
    """
    status, detail = cleanup.delete_k8s_with_uid_precondition(
        ScriptedRunner([("w2-k8s-delete-with-preconditions",
                         cleanup.CommandResult(1, DETAILLESS_404, "HTTP 404"))]),
        kind="Deployment", name="w2-fixture-gateway", namespace="adp-gateway", uid=UID)
    assert status == "error", f"an unidentified 404 was classified {status!r}"
    assert "not evidence that this object is gone" in detail


def test_an_identified_404_from_the_delete_is_still_absence() -> None:
    """The fix must not be a blanket refusal: a real race still resolves cleanly.

    An object that genuinely disappears between the probe and the delete produces an
    identifying NotFound, and that IS absence. Without this, the tightened classifier
    would turn an ordinary race into a permanent cleanup failure.
    """
    status = json.dumps({"kind": "Status", "reason": "NotFound", "code": 404,
                         "details": {"name": "w2-fixture-gateway", "kind": "deployments"}})
    verdict, _ = cleanup.delete_k8s_with_uid_precondition(
        ScriptedRunner([("w2-k8s-delete-with-preconditions",
                         cleanup.CommandResult(1, status, "HTTP 404"))]),
        kind="Deployment", name="w2-fixture-gateway", namespace="adp-gateway", uid=UID)
    assert verdict == "not_found"


def test_a_generic_404_cannot_disarm_the_policy_retention() -> None:
    """THE bypass root named, end to end.

    This is the test that matters most: the two classifier fixes above are only
    interesting because of what they protect. A workload whose delete returns an
    unidentified 404 must NOT be counted as gone, because that would empty the
    surviving list and let the NetworkPolicy be deleted underneath a workload that,
    for all we actually know, is still running and still control-enabled.

    Asserting on the classifier alone would not catch a regression that re-introduced
    the bypass somewhere else in run_cleanup -- so this asserts on the cluster calls.
    """
    class Generic404Cluster(OrderedCluster):
        def __call__(self, argv):
            joined = " ".join(argv)
            self.calls.append(joined)
            if "w2-k8s-delete-with-preconditions" in joined:
                kind = ("policy" if "networkpolicies" in joined else "workload")
                self.deleted.append(kind)
                # The server answers 404 without saying about WHAT.
                return cleanup.CommandResult(1, DETAILLESS_404, "HTTP 404")
            if "get" in joined:
                return ok(k8s_json(
                    "uid-policy-0001" if "networkpolicies" in joined else UID))
            return ok("{}")

    cluster = Generic404Cluster()
    outcome = cleanup.run_cleanup(
        ordered_ledger(policy_entry(), k8s_entry()),
        table="t", run=cluster, sleep=NO_SLEEP)

    assert "policy" not in cluster.delete_order, (
        "a generic 404 on the workload delete was read as absence, and the isolation "
        f"protecting it was removed: {cluster.delete_order}")
    workload = next(r for r in outcome["k8s"] if r["kind"] == "Deployment")
    assert workload.get("confirmed_absent") is not True, workload
    policy = next(r for r in outcome["k8s"] if r["kind"] == "NetworkPolicy")
    assert policy.get("skipped") is True and "unrestricted" in policy["reason"]
    assert outcome["cleanup_ok"] is False


@pytest.mark.parametrize('worker_drains', [False, True])
def test_abort_worker_drains_before_gateway_and_recovery_dependencies(worker_drains):
    worker = k8s_entry(kind='Pod', name='w2-worker', namespace='adp-agents', uid='worker-uid')
    gateway = k8s_entry()
    policy = policy_entry()
    # Deliberately creation order: gateway precedes the retained worker.
    ledger = ordered_ledger(gateway, policy, worker)
    if not worker_drains:
        ledger['synthetic_rows'] = [{'event_id': 'fixture-event', 'arrived_at': 'fixture-time'}]
        ledger['queues'] = [queue_entry()]
    deleted = []
    calls = []
    entries = {e['name']: e for e in (worker, gateway, policy)}

    def cluster(argv):
        calls.append(argv)
        if argv[0] == 'w2-k8s-delete-with-preconditions':
            name = argv[1].split('/')[-1]
            assert json.loads(argv[2])['preconditions']['uid'] == entries[name]['uid']
            deleted.append(name)
            return ok(json.dumps({'status': 'Success'}))
        assert argv[:2] == ['kubectl', 'get'], argv
        kind, name = argv[2:4]
        if name in deleted and (name != worker['name'] or worker_drains):
            plural = cleanup.k8s_resource_plural(kind)
            group = cleanup.k8s_resource_group(kind)
            return err(f'Error from server (NotFound): {plural}{"." + group if group else ""} "{name}" not found')
        return ok(json.dumps({'metadata': {'uid': entries[name]['uid'], 'name': name,
            'finalizers': ['adp.aws/abort-terminal-report'] if name == worker['name'] else []}}))

    result = cleanup.run_cleanup(ledger, table=TABLE, run=cluster, sleep=NO_SLEEP)
    assert deleted[0] == worker['name']
    if worker_drains:
        assert deleted == [worker['name'], gateway['name'], policy['name']]
        assert result['cleanup_ok'] is True
    else:
        assert deleted == [worker['name']]
        assert result['cleanup_ok'] is False
        gateway_result = next(r for r in result['k8s'] if r['name'] == gateway['name'])
        assert gateway_result['skipped'] is True
        assert 'abort recovery' in gateway_result['reason']
        assert result['rows'][0]['skipped'] is True
        assert result['queues'][0]['skipped'] is True
        assert not any('patch' in call for call in calls)
