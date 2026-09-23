"""`SqlRegistrationStore`: the reserve/finalize/release contract, over real SQL — F1.

F1 verbatim: "`ClusterAccess` and `RegistrationStore` were Protocols with no
implementation anywhere". `registry.py` is the store half of that repair, and this file
is what makes it more than an assertion.

## What the double is, and what it deliberately does NOT smooth over

`_Recorder` is a `TransactionalStore` that records statements and replies from a
scripted table. It is intentionally literal:

- A statement issued OUTSIDE `transaction()` is recorded as such, so the atomicity
  claim in the module docstring is checkable rather than asserted. A reserve split
  across three autocommitted statements has exactly the race the advisory lock exists
  to close, and it would pass every test that only looked at return values.
- `replies` is keyed per STATEMENT, so a test can make the `SELECT ... FOR UPDATE`
  return a `registered` row and see what `reserve` does with it. That is the fifth
  production defect this suite pins.

## The fifth defect: `reserve` compared over the UNION of keys

`reserve` compared `recorded` against `identity` over both sides' keys combined. A row
in state `registered` holds the twelve-field record `finalize` wrote; a reservation
identity holds only the seven `_RESERVATION_FIELDS`, because `namespace_uid` does not
exist until the namespace is created. Over the union, the five extra fields all read as
"recorded but not requested" and therefore divergent — so re-running an ALREADY
COMPLETED bootstrap was refused as an attempted rebinding, naming five fields that had
not changed at all, with an operator-facing message saying the workspace "binds
differently" when it binds identically. Re-running a completed bootstrap is the single
most likely thing an operator does.

No credential appears in this file and none can: the store takes an injected connection
and has no field that could hold a DSN or a password.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.registration import (
    _RESERVATION_FIELDS,
    RegistrationStore,
)
from superplane_bootstrap.registry import (
    REGISTERED,
    RESERVED,
    SqlRegistrationStore,
    TransactionalStore,
)
from superplane_bootstrap.state import claim_fingerprint

from .conftest import (
    ACCOUNT_ID,
    CLUSTER_ARN,
    CLUSTER_NAME,
    CREDENTIAL_ID,
    ENDPOINT,
    NAMESPACE,
    ORG_ID,
    REGION,
    WORKSPACE_ID,
)

NAMESPACE_UID = "namespace-uid-0001"
CONTRACT_VERSION = "v1"

RESERVATION_IDENTITY: dict[str, str] = {
    "workspace_id": WORKSPACE_ID,
    "org_id": ORG_ID,
    "account_id": ACCOUNT_ID,
    "region": REGION,
    "cluster_name": CLUSTER_NAME,
    "cluster_arn": CLUSTER_ARN,
    "namespace": NAMESPACE,
}


@dataclass
class _Target:
    """A `WorkspaceTarget`-shaped record, read by attribute as `_target_mapping` does.

    Duck-typed here for the same reason `registry._target_mapping` duck-types it: the
    dependency runs one way, from registration to storage, and a test that imported
    `WorkspaceTarget` would be asserting the two modules agree on a type rather than on
    a field list.
    """

    workspace_id: str = WORKSPACE_ID
    org_id: str = ORG_ID
    account_id: str = ACCOUNT_ID
    region: str = REGION
    cluster_name: str = CLUSTER_NAME
    cluster_arn: str = CLUSTER_ARN
    endpoint: str = ENDPOINT
    namespace: str = NAMESPACE
    namespace_uid: str = NAMESPACE_UID
    cluster_ownership: str = "adp-created"
    credential_reference_id: str = CREDENTIAL_ID
    contract_version: str = CONTRACT_VERSION


@dataclass
class _Recorder:
    """A literal `TransactionalStore`: records every statement, replies from a table.

    `replies` is keyed on a substring of the statement, because the statements are
    module constants and pinning their whitespace would make these tests fail on a
    reformat while proving nothing about behaviour. Where the statement text itself is
    the thing under test (parameter binding, no interpolation), the test reads
    `self.statements` directly.
    """

    replies: Mapping[str, Sequence[Mapping[str, object]]] = field(default_factory=dict)
    statements: list[tuple[str, Mapping[str, object], bool]] = field(
        default_factory=list
    )
    depth: int = 0
    committed: int = 0

    def transaction(self):
        return _Transaction(self)

    def execute(
        self, statement: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        self.statements.append((statement, dict(parameters), self.depth > 0))
        if "FROM organizations" in statement:
            return [{"id": ORG_ID, "adp_org_id": ORG_ID}]
        if "FROM workspaces" in statement or "FROM clusters" in statement:
            return []
        for marker, rows in self.replies.items():
            if marker in statement:
                return list(rows)
        if "FROM organizations" in statement:
            return [{"id": ORG_ID, "adp_org_id": ORG_ID}]
        return []

    # --- readers the tests use ------------------------------------------------

    def issued(self, marker: str) -> list[tuple[str, Mapping[str, object], bool]]:
        return [entry for entry in self.statements if marker in entry[0]]


class _Transaction:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def __enter__(self):
        self._recorder.depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self._recorder.depth -= 1
        if exc_type is None:
            self._recorder.committed += 1
        return False


# The token a scripted row is held by, when a test does not care which. Distinct from
# `OTHER_TOKEN` so the fence tests cannot pass by comparing a value against itself.
HELD_TOKEN = "held-attempt-token-0123456789abcdef"
OTHER_TOKEN = "losing-attempt-token-fedcba9876543210"


def _row(
    state: str, identity: Mapping[str, object], token: str = HELD_TOKEN
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "state": state,
        "identity_json": json.dumps(dict(identity), sort_keys=True),
        "attempt_token": token,
    }


def _store(replies: Mapping[str, Sequence[Mapping[str, object]]] | None = None):
    recorder = _Recorder(replies=dict(replies or {}))
    return SqlRegistrationStore(store=recorder), recorder


# --- F1: the store must actually BE a RegistrationStore -------------------------


def test_the_sql_store_satisfies_the_registration_store_protocol():
    """The same `isinstance` gap F1's first defect slipped past. `@runtime_checkable`
    checks method NAMES only, so this is worth asserting exactly because it is weak:
    the rest of this file is what checks the behaviour behind the names."""
    store, _ = _store()

    assert isinstance(store, RegistrationStore)
    assert isinstance(store.store, TransactionalStore)


def test_the_store_holds_no_credential():
    """The connection is injected already authenticated, as `installation_bootstrap.py`
    receives its session. There is no field here that could hold a password or a DSN."""
    store, _ = _store()

    assert [f.name for f in store.__dataclass_fields__.values()] == ["store"]


# --- reserve: the three outcomes, and the atomicity they depend on --------------


def test_an_unclaimed_workspace_is_reserved():
    store, recorder = _store()

    outcome = store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert outcome["reserved"] is True
    assert outcome["replayed"] is False
    inserted = recorder.issued("INSERT INTO workspace_bootstrap_reservations")
    assert len(inserted) == 1
    assert inserted[0][1]["state"] == RESERVED
    assert json.loads(str(inserted[0][1]["identity_json"])) == RESERVATION_IDENTITY
    # F10: the token is issued to this caller AND written to the row, in the same
    # statement. Either one alone would be useless — a token nobody recorded fences
    # nothing, and a token nobody received cannot be presented later.
    issued = str(outcome["attempt_token"])
    assert issued, "a fresh claim was granted with no way to identify its holder"
    assert inserted[0][1]["attempt_token"] == issued


def test_the_reservation_is_taken_under_the_workspace_advisory_lock():
    """Two bootstraps racing for one workspace must serialize, and the pattern must be
    the one `installation_bootstrap.py` already uses against these tables. A second,
    different concurrency strategy over the same rows can be correct alone and
    interleave wrongly with the first — a defect that appears only under load."""
    store, recorder = _store()

    store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    locks = recorder.issued("pg_advisory_xact_lock")
    assert len(locks) == 1
    assert WORKSPACE_ID in str(locks[0][1]["binding"])
    assert recorder.issued("FOR UPDATE"), (
        "the row was read without FOR UPDATE, so a concurrent reserve could read the "
        "same absence and both would insert"
    )


def test_every_statement_in_a_reserve_runs_inside_one_transaction():
    """The atomicity claim, checked rather than asserted. Autocommitted statements
    would release the transaction-scoped advisory lock between the lock and the insert,
    which is precisely the window the lock exists to close."""
    store, recorder = _store()

    store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert recorder.committed == 1
    assert all(in_txn for _, _, in_txn in recorder.statements)


def test_a_lock_is_transaction_scoped_so_a_crashed_process_leaks_nothing():
    """A session-scoped lock left by a killed process would block every later attempt
    for that workspace until an operator found and cleared it by hand."""
    store, recorder = _store()

    store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    statement = recorder.issued("pg_advisory")[0][0]
    assert "pg_advisory_xact_lock" in statement
    assert "pg_advisory_lock(" not in statement


def test_a_matching_unfinalized_reservation_is_a_live_claim_and_is_refused():
    """**The F10 defect, stated as directly as it can be.**

    F10 verbatim: "After the transaction-scoped advisory lock is released, a second
    process with the same identity encounters the existing `reserved` row and receives
    `reserved: True`. Both processes can then mutate the namespace, controller, taint,
    state, and registration concurrently."

    The first revision returned `{"reserved": True, "replayed": True}` for exactly this
    input, on the reasoning that a resumed bootstrap must see its own row. But from inside
    `reserve` there is nothing to see: a row left by a dead process and a row held by a
    live one are byte-identical, and the advisory lock — transaction-scoped, released at
    commit — is already gone by the time the eight guarded steps run. So the answer that
    made resumption convenient also authorized two concurrent bootstraps of one workspace.

    Refused now, WITHOUT a token, so the loser cannot finalize or release either. Resuming
    after a genuinely dead attempt is `release_claim`, reached from the durable state
    file, which is the only thing that can tell the two rows apart.
    """
    store, recorder = _store({"FOR UPDATE": [_row(RESERVED, RESERVATION_IDENTITY)]})

    outcome = store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert outcome["reserved"] is False
    assert "attempt_token" not in outcome, (
        "the losing attempt was handed a token, so it can still finalize or release the "
        "claim it just lost"
    )
    assert "already holds an unfinalized reservation" in str(outcome["conflict"])
    assert "recover" in str(outcome["conflict"]), (
        "the refusal does not tell the operator how a claim left by a dead attempt is "
        "cleared, so the only actionable path out is undiscoverable"
    )
    assert not recorder.issued("INSERT INTO"), "a refused reserve still wrote a row"


def test_a_different_binding_is_refused_and_names_the_divergent_field():
    """The refusal a reservation exists to make, while refusing is still free."""
    other = dict(RESERVATION_IDENTITY, cluster_arn=CLUSTER_ARN + "-elsewhere")
    store, recorder = _store({"FOR UPDATE": [_row(RESERVED, other)]})

    outcome = store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert outcome["reserved"] is False
    assert "cluster_arn" in str(outcome["conflict"])
    assert not recorder.issued("INSERT INTO"), "a conflicting reserve still wrote a row"


def test_a_blank_recorded_field_diverges_from_a_populated_one():
    """A missing key reads as "" and must not compare equal to a real value. Treating
    an absent field as a match would let a partially-written row pass as the same
    binding."""
    partial = {k: v for k, v in RESERVATION_IDENTITY.items() if k != "region"}
    store, _ = _store({"FOR UPDATE": [_row(RESERVED, partial)]})

    outcome = store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert outcome["reserved"] is False
    assert "region" in str(outcome["conflict"])


# --- The fifth defect: a completed bootstrap re-run is not a rebinding ----------


def test_re_running_a_completed_bootstrap_is_a_replay_not_a_rebinding():
    """**The fifth production defect.** `reserve` compared over the UNION of the stored
    and requested keys. A `registered` row holds all twelve fields `finalize` wrote;
    a reservation identity holds only the seven `_RESERVATION_FIELDS`. Over the union
    the extra five were "recorded but not requested" and therefore divergent, so the
    most ordinary operator action there is — re-running a bootstrap that already
    succeeded — was refused as an attempted rebinding, naming five fields that were
    identical, and told the operator the workspace "binds differently" when it binds
    the same."""
    registered = {
        **RESERVATION_IDENTITY,
        "namespace_uid": NAMESPACE_UID,
        "endpoint": ENDPOINT,
        "cluster_ownership": "adp-created",
        "credential_reference_id": CREDENTIAL_ID,
        "contract_version": CONTRACT_VERSION,
    }
    store, recorder = _store({"FOR UPDATE": [_row(REGISTERED, registered)]})

    outcome = store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert outcome == {"reserved": True, "replayed": True}
    assert not recorder.issued("INSERT INTO"), "a replay inserted a second row"


def test_the_narrower_comparison_still_catches_a_rebinding_of_a_registered_row():
    """The fix must not make the comparison vacuous: a REGISTERED row whose cluster
    differs is still a rebinding, and still refused before any mutation."""
    registered = {
        **RESERVATION_IDENTITY,
        "cluster_arn": CLUSTER_ARN + "-elsewhere",
        "namespace_uid": NAMESPACE_UID,
        "endpoint": ENDPOINT,
    }
    store, _ = _store({"FOR UPDATE": [_row(REGISTERED, registered)]})

    outcome = store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert outcome["reserved"] is False
    assert "cluster_arn" in str(outcome["conflict"])


def test_the_reservation_identity_field_list_is_the_one_registration_publishes():
    """If `registration._RESERVATION_FIELDS` grew a field, this fixture would stop
    representing a real reservation and the defect above could reappear unnoticed."""
    assert set(RESERVATION_IDENTITY) == set(_RESERVATION_FIELDS)
    assert "namespace_uid" not in _RESERVATION_FIELDS, (
        "the uid does not exist before the namespace is created; a reservation that "
        "required it could not run before mutation, which is the whole of F5"
    )


# --- finalize -------------------------------------------------------------------


def test_finalize_marks_the_reserved_row_registered():
    store, recorder = _store({"FOR UPDATE": [_row(RESERVED, RESERVATION_IDENTITY)]})

    store.finalize(_Target(), HELD_TOKEN)

    updated = recorder.issued("SET state")
    assert len(updated) == 1
    assert updated[0][1]["state"] == REGISTERED
    written = json.loads(str(updated[0][1]["identity_json"]))
    assert written["namespace_uid"] == NAMESPACE_UID
    assert written["endpoint"] == ENDPOINT


def test_finalize_re_reads_the_claim_rather_than_trusting_the_earlier_one():
    """Between reserve and finalize an operator or a concurrent teardown may have
    released the claim. Publishing a record for a workspace something else now owns is
    worse than failing, so the lock is re-taken and the row re-read."""
    store, recorder = _store({"FOR UPDATE": [_row(RESERVED, RESERVATION_IDENTITY)]})

    store.finalize(_Target(), HELD_TOKEN)

    assert recorder.issued("pg_advisory_xact_lock")
    assert recorder.issued("FOR UPDATE")
    assert all(in_txn for _, _, in_txn in recorder.statements)


def test_finalizing_a_released_claim_refuses_rather_than_inserting():
    """No row means the pre-mutation claim is gone. An UPSERT here would publish a
    registration nothing ever claimed."""
    store, recorder = _store()

    with pytest.raises(BootstrapRefused, match="no reservation exists"):
        store.finalize(_Target(), HELD_TOKEN)

    assert not recorder.issued("SET state")


def test_finalizing_an_already_registered_row_writes_nothing():
    """Not an error — the caller's replay handling in `finalize_registration` compares
    the records and decides. Overwriting here would let a second run silently replace a
    published record without that comparison ever running."""
    store, recorder = _store({"FOR UPDATE": [_row(REGISTERED, RESERVATION_IDENTITY)]})

    store.finalize(_Target(), HELD_TOKEN)

    assert not recorder.issued("SET state")


def test_finalizing_a_blank_workspace_id_refuses():
    """A blank id would take a lock on the prefix alone and update every row whose
    workspace_id is blank."""
    store, recorder = _store()

    with pytest.raises(BootstrapRefused, match="blank workspace id"):
        store.finalize(_Target(workspace_id="   "), HELD_TOKEN)

    assert recorder.statements == []


# --- release: the safety property, not an optimization --------------------------


def test_release_drops_only_an_unfinalized_reservation():
    """`AND state = 'reserved'` is the safety property. `release` runs on a REFUSAL
    path, and a bug that deleted a completed registration would silently unpublish a
    live workspace."""
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    assert store.release(WORKSPACE_ID, HELD_TOKEN) is True

    deleted = recorder.issued("DELETE FROM")
    assert deleted[0][1]["state"] == RESERVED
    assert "AND state = :state" in deleted[0][0]


def test_releasing_nothing_reports_false_from_the_returning_clause():
    """Reported from `RETURNING`, not from the absence of an exception: a DELETE that
    matched no row yields no rows either way, and the caller records the answer in
    `BootstrapOutcome.reservation_released` as fact."""
    store, _ = _store()

    assert store.release(WORKSPACE_ID, HELD_TOKEN) is False


def test_release_takes_the_lock_too():
    """A release racing a concurrent reserve must serialize on the same key, or the
    reserve could read the row, the release delete it, and the reserve then report a
    held claim over a row that no longer exists."""
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    store.release(WORKSPACE_ID, HELD_TOKEN)

    assert recorder.issued("pg_advisory_xact_lock")
    assert all(in_txn for _, _, in_txn in recorder.statements)


# --- F10: the token fences what the lock cannot ---------------------------------
#
# F10 verbatim, second half: "A losing process may also release the shared reservation
# while the winner is active, causing the winner's finalization to fail and re-taint the
# workspace."
#
# These tests are about the DIFFERENCE between serializing and excluding. The advisory
# lock is transaction-scoped — deliberately, so a crashed process leaks nothing — which
# means it is released the moment `reserve` commits, and all eight steps it exists to
# guard run afterwards. Everything below is what has to be true once the lock is gone.


def test_a_losing_attempt_cannot_finalize_the_winners_claim():
    """The winner's token is on the row; the loser presents its own and is refused.

    This is the finalization half of the concurrent-mutation defect. Without it, the two
    attempts race to publish and the record describes whichever finished last — a cluster
    state neither attempt actually established, since both were mutating it.
    """
    store, recorder = _store({"FOR UPDATE": [_row(RESERVED, RESERVATION_IDENTITY)]})

    with pytest.raises(BootstrapRefused, match="held by a different bootstrap attempt"):
        store.finalize(_Target(), OTHER_TOKEN)

    assert not recorder.issued("SET state"), (
        "an attempt that does not hold the claim still published the registration"
    )


def test_a_losing_attempt_releasing_the_winners_claim_releases_nothing():
    """**The finding's second half, directly.**

    A loser is not an error here — it simply has no claim, and the honest answer is that
    it released nothing. That distinction matters downstream: `workspace.py` records the
    answer in `BootstrapOutcome.reservation_released`, so a True would tell an operator a
    claim was dropped while the winner still holds it.

    The fence is asserted in the STATEMENT rather than by scripting an empty reply,
    because the reply is the database's answer and this test is about what was asked. A
    version that read the row and then deleted unconditionally would pass a
    return-value-only test while leaving the race wide open between the two statements.
    """
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    store.release(WORKSPACE_ID, OTHER_TOKEN)

    deleted = recorder.issued("DELETE FROM")
    assert len(deleted) == 1
    assert "AND attempt_token = :attempt_token" in deleted[0][0], (
        "the delete is not fenced on the token, so a loser can drop the winner's claim"
    )
    assert deleted[0][1]["attempt_token"] == OTHER_TOKEN


def test_the_winner_still_finalizes_after_a_loser_tried_and_failed():
    """The fence must not be a deadlock. The loser's refusals leave the row untouched, so
    the attempt that actually holds the claim completes normally — which is the property
    that makes refusing the loser safe rather than merely strict."""
    store, recorder = _store({"FOR UPDATE": [_row(RESERVED, RESERVATION_IDENTITY)]})

    with pytest.raises(BootstrapRefused):
        store.finalize(_Target(), OTHER_TOKEN)
    store.release(WORKSPACE_ID, OTHER_TOKEN)

    store.finalize(_Target(), HELD_TOKEN)

    updated = recorder.issued("SET state")
    assert len(updated) == 1
    assert updated[0][1]["state"] == REGISTERED


@pytest.mark.parametrize("token", ["", "   ", None])
def test_an_unfenced_release_refuses_rather_than_deleting(token):
    """A blank token must not degrade to the pre-F10 statement.

    The defect is a delete keyed on the workspace alone, so that delete has to be
    unreachable from this method — not merely discouraged. A version that treated a blank
    token as "no fence" would restore the defect exactly, and would do it on the refusal
    path where nobody is watching closely.
    """
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    with pytest.raises(BootstrapRefused, match="refusing an unfenced release"):
        store.release(WORKSPACE_ID, token)

    assert not recorder.issued("DELETE FROM")


def test_the_unfenced_release_refusal_names_the_path_that_is_allowed():
    """A refusal with no way forward would make a stranded claim unrecoverable, which is
    how a fence becomes a denial of service."""
    store, _ = _store()

    with pytest.raises(BootstrapRefused, match="release_claim"):
        store.release(WORKSPACE_ID, "")


@pytest.mark.parametrize("token", ["", "   ", None])
def test_finalizing_without_a_token_refuses(token):
    """The same rule at the other end. An attempt that cannot name its reservation has
    not shown it holds one, and this is the statement that publishes the record."""
    store, recorder = _store({"FOR UPDATE": [_row(RESERVED, RESERVATION_IDENTITY)]})

    with pytest.raises(BootstrapRefused, match="no attempt token was supplied"):
        store.finalize(_Target(), token)

    assert not recorder.issued("SET state")


def test_a_reservation_row_with_no_token_cannot_be_finalized():
    """A row predating this column — or written by something that skipped it — is not a
    claim anyone can prove they hold.

    Accepting it because "there is nothing to compare against" is the vacuous-fence
    failure: every attempt would match, which is worse than no fence at all because the
    code would look like it checks.
    """
    store, recorder = _store(
        {"FOR UPDATE": [_row(RESERVED, RESERVATION_IDENTITY, token="")]}
    )

    with pytest.raises(BootstrapRefused, match="held by a different bootstrap attempt"):
        store.finalize(_Target(), HELD_TOKEN)

    assert not recorder.issued("SET state")


def test_the_update_that_publishes_is_itself_fenced_on_the_token():
    """The check and the mutation are one statement, not two.

    `finalize` re-reads the row under the lock and then updates it. If the UPDATE were
    keyed on the workspace alone, the comparison would be advisory: correct under the
    lock, and one refactor away from a read-then-write with a gap in between. Naming the
    token in the WHERE clause means the database refuses a row that changed identity,
    whatever the Python did.
    """
    store, recorder = _store({"FOR UPDATE": [_row(RESERVED, RESERVATION_IDENTITY)]})

    store.finalize(_Target(), HELD_TOKEN)

    updated = recorder.issued("SET state")
    assert "AND attempt_token = :attempt_token" in updated[0][0]
    assert updated[0][1]["attempt_token"] == HELD_TOKEN


def test_each_reservation_gets_distinct_unguessable_token_material():
    """The token authorizes `release`, so guessing one is guessing permission to strand a
    workspace. Two reserves must not produce the same token, and the value must come from
    `secrets` rather than from a counter or a timestamp — both of which would be
    predictable from outside.

    Asserted on distinctness and length rather than on the function called, because what
    matters is the property. A 64-character hex string is `secrets.token_hex(32)`; a
    reserve that returned a workspace-derived value would be both short and repeatable.
    """
    tokens = set()
    for _ in range(8):
        store, _ = _store()
        tokens.add(
            str(store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)["attempt_token"])
        )

    assert len(tokens) == 8, "reserve issued a repeating token"
    assert all(len(token) == 64 for token in tokens)
    assert all(WORKSPACE_ID not in token for token in tokens), (
        "the token is derived from the workspace id, so any attempt can compute it"
    )


# --- F13: the recovery release, fenced on a claim fingerprint --------------------
#
# This block previously pinned `release_abandoned`, which took a workspace id and deleted
# whichever `reserved` row it found. Its tests asserted that absence of a fence — one of
# them read `assert "attempt_token" not in deleted[0][0]` — so they are rewritten rather
# than extended: they were a correct description of the defective contract.


def test_the_recovery_release_names_the_claim_it_is_clearing():
    """The F13 fix at the statement level.

    The recovery path cannot present a token — that is the situation it exists for — so it
    presents a one-way fingerprint of it and the statement digests the column to compare.
    The delete is therefore narrowed by WHICH claim it is dropping, not merely by which
    workspace, and that narrowing is what makes a stale record harmless.
    """
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    assert store.release_claim(WORKSPACE_ID, claim_fingerprint(HELD_TOKEN)) is True

    deleted = recorder.issued("DELETE FROM")
    assert len(deleted) == 1
    statement, parameters, _ = deleted[0]
    assert "attempt_token" in statement, (
        "the recovery delete does not reference the claim column at all, so it drops "
        "whichever reservation exists — the F13 defect"
    )
    assert parameters["claim_fingerprint"] == claim_fingerprint(HELD_TOKEN)
    assert parameters["state"] == RESERVED


def test_the_recovery_release_compares_and_deletes_in_one_statement():
    """A read followed by a delete would leave the window this finding is about.

    The recovering process is by definition working from a record that may be stale, so it
    must not be able to observe a claim and then delete a different one. One statement
    means the database does the comparison against the row it is deleting, and `RETURNING`
    reports whether the claim named was the claim dropped.
    """
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    store.release_claim(WORKSPACE_ID, claim_fingerprint(HELD_TOKEN))

    assert len(recorder.issued("SELECT workspace_id")) == 0, (
        "the recovery release read the row before deleting it, so the row it checked "
        "and the row it deleted are not guaranteed to be the same one"
    )
    statement = recorder.issued("DELETE FROM")[0][0]
    assert "RETURNING" in statement


def test_the_recovery_release_never_sends_a_token_to_the_database():
    """The record holds a digest, and the digest is what travels.

    Recovery must be able to ask "is this the claim my record names?" without being able
    to answer "what is the token?". A parameter carrying the token would mean the state
    file had to hold one, and a durable copy of the token is a durable copy of the
    permission to publish this workspace.
    """
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    store.release_claim(WORKSPACE_ID, claim_fingerprint(HELD_TOKEN))

    for statement, parameters, _ in recorder.statements:
        assert HELD_TOKEN not in statement
        for value in parameters.values():
            assert HELD_TOKEN != str(value), (
                "the recovery path handled the raw attempt token, so the durable record "
                "it works from would have to store one"
            )


@pytest.mark.parametrize("fingerprint", ["", "   ", None])
def test_a_recovery_release_without_a_fingerprint_refuses(fingerprint):
    """Blank must refuse, not act as a wildcard.

    This is the regression guard for F13 itself: a blank fingerprint reaching the
    statement would match nothing today, but the refusal makes "delete whichever claim
    exists" unreachable rather than merely unreached. An unfenced delete on this path is
    the defect, so it must not be representable.
    """
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    with pytest.raises(BootstrapRefused, match="refusing an unfenced recovery release"):
        store.release_claim(WORKSPACE_ID, fingerprint)

    assert not recorder.issued("DELETE FROM")


def test_the_recovery_release_still_refuses_to_drop_a_completed_registration():
    """Recovery unblocks a stuck workspace; it does not unpublish a live one."""
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    store.release_claim(WORKSPACE_ID, claim_fingerprint(HELD_TOKEN))

    assert recorder.issued("DELETE FROM")[0][1]["state"] == RESERVED
    assert "AND state = :state" in recorder.issued("DELETE FROM")[0][0]


def test_the_recovery_release_takes_the_lock_like_every_other_writer():
    """It races a concurrent reserve exactly as `release` does, and a takeover that did
    not serialize could delete a row a reserve had just read as held."""
    store, recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})

    store.release_claim(WORKSPACE_ID, claim_fingerprint(HELD_TOKEN))

    assert recorder.issued("pg_advisory_xact_lock")
    assert all(in_txn for _, _, in_txn in recorder.statements)


def test_a_fingerprint_that_matches_nothing_releases_nothing():
    """The stale case, which is the whole of F13.

    A record naming a claim that is no longer in the database has nothing to clean up. The
    database reports no rows deleted and the method must pass that through — because the
    alternative, reporting success, is what told a stale recovery it had taken over a
    workspace a live successor was mutating.
    """
    store, recorder = _store({"DELETE FROM": []})

    assert store.release_claim(WORKSPACE_ID, claim_fingerprint(OTHER_TOKEN)) is False
    assert recorder.issued("DELETE FROM"), "the statement was never even attempted"


def test_the_two_releases_are_different_statements():
    """Not one statement with an optional clause.

    Both releases are fenced now, but on different keys: `release` compares the token an
    attempt holds, `release_claim` compares a digest of the token a record names. Keeping
    them as separate constants means neither fence can be reached with the other's
    parameter missing — a single statement with two optional clauses would make the
    unfenced delete one edit away, and that edit is the defect.
    """
    fenced, fenced_recorder = _store({"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]})
    fenced.release(WORKSPACE_ID, HELD_TOKEN)

    recovery, recovery_recorder = _store(
        {"DELETE FROM": [{"workspace_id": WORKSPACE_ID}]}
    )
    recovery.release_claim(WORKSPACE_ID, claim_fingerprint(HELD_TOKEN))

    assert (
        fenced_recorder.issued("DELETE FROM")[0][0]
        != recovery_recorder.issued("DELETE FROM")[0][0]
    )


def test_the_fingerprint_is_not_reversible_to_the_token():
    """What makes the record safe to write to disk.

    `claim_fingerprint` exists so the durable state file can identify a claim without
    containing the authority to act on it. If the digest carried the token — as a prefix,
    an encoding, anything recoverable — then the state file would be a copy of the
    permission to publish or unpublish the workspace, and `state.py`'s stated "no
    credential is recorded here" guarantee would be false.
    """
    fingerprint = claim_fingerprint(HELD_TOKEN)

    assert HELD_TOKEN not in fingerprint
    assert fingerprint != claim_fingerprint(OTHER_TOKEN)
    assert fingerprint == claim_fingerprint(HELD_TOKEN), (
        "the fingerprint is not deterministic, so it could not be compared across the "
        "process boundary it exists to cross"
    )
    assert len(fingerprint) == 64


# --- read: a reservation is not a registration ----------------------------------


def test_read_returns_only_a_registered_row():
    """Returning a reservation here would let `finalize_registration` mistake an
    in-flight claim for an existing record and report a replay for a bootstrap that
    never finished."""
    store, recorder = _store(
        {"SELECT identity_json": [_row(REGISTERED, _Target().__dict__)]}
    )

    record = store.read(WORKSPACE_ID)

    assert record is not None
    assert recorder.issued("SELECT identity_json")[0][1]["state"] == REGISTERED


def test_an_unregistered_workspace_reads_as_none():
    store, _ = _store()

    assert store.read(WORKSPACE_ID) is None


def test_the_record_exposes_its_fields_by_attribute():
    """`registration._refuse_conflict` reads the existing record by ATTRIBUTE so the
    store may return its own row type."""
    store, _ = _store({"SELECT identity_json": [_row(REGISTERED, _Target().__dict__)]})

    record = store.read(WORKSPACE_ID)

    assert record.cluster_arn == CLUSTER_ARN
    assert record.namespace_uid == NAMESPACE_UID


def test_a_field_absent_from_the_record_reads_as_none_not_as_a_match():
    """`_refuse_conflict` treats None as a conflict, which is right: a record that
    cannot be compared has not been shown to be the same record."""
    store, _ = _store(
        {"SELECT identity_json": [_row(REGISTERED, RESERVATION_IDENTITY)]}
    )

    record = store.read(WORKSPACE_ID)

    assert record.namespace_uid is None


# --- unreadable stored state is a refusal, never an empty default ---------------


def test_an_unparseable_stored_identity_refuses():
    """An empty default compares equal to nothing, which would turn an existing binding
    into a conflict — or, with the comparison over the requested keys, a conflict into a
    match."""
    store, _ = _store(
        {
            "FOR UPDATE": [
                {
                    "workspace_id": WORKSPACE_ID,
                    "state": RESERVED,
                    "identity_json": "{not json",
                }
            ]
        }
    )

    with pytest.raises(BootstrapRefused, match="not readable"):
        store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)


def test_a_stored_identity_that_is_not_an_object_refuses():
    store, _ = _store(
        {
            "FOR UPDATE": [
                {
                    "workspace_id": WORKSPACE_ID,
                    "state": RESERVED,
                    "identity_json": "[1, 2, 3]",
                }
            ]
        }
    )

    with pytest.raises(BootstrapRefused, match="not an object"):
        store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)


def test_a_driver_that_already_decoded_the_json_column_is_accepted():
    """`asyncpg`/SQLAlchemy may hand back a dict for a JSON column. Re-parsing a dict
    with `json.loads(str(...))` would raise on the Python repr and refuse a perfectly
    readable row.

    Scripted as a REGISTERED row so the assertion is a positive one: a matching `reserved`
    row is refused on F10 grounds now, and a refusal is also what an unreadable identity
    produces — so a decoding bug would be indistinguishable from correct behaviour.
    """
    store, _ = _store(
        {
            "FOR UPDATE": [
                {
                    "workspace_id": WORKSPACE_ID,
                    "state": REGISTERED,
                    "identity_json": dict(RESERVATION_IDENTITY),
                    "attempt_token": "",
                }
            ]
        }
    )

    assert store.reserve(WORKSPACE_ID, RESERVATION_IDENTITY) == {
        "reserved": True,
        "replayed": True,
    }


# --- no SQL is built from a caller's string -------------------------------------


def test_no_statement_interpolates_a_caller_supplied_value():
    """Every statement is a module constant with bound parameters, so a workspace id
    containing SQL cannot change a statement's meaning. Asserted over the statements
    actually issued rather than by reading the constants, because a future revision
    could add an f-string in the method body."""
    injection = "'; DROP TABLE workspace_bootstrap_reservations; --"
    store, recorder = _store({"DELETE FROM": [{"workspace_id": injection}]})

    store.reserve(injection, dict(RESERVATION_IDENTITY, workspace_id=injection))
    store.release(injection, injection)
    store.release_claim(injection, claim_fingerprint(HELD_TOKEN))

    assert recorder.statements
    for statement, _, _ in recorder.statements:
        assert injection not in statement, (
            "a caller-supplied value reached the statement text"
        )
    # And it did reach the BOUND parameters, so the value was genuinely carried rather
    # than dropped somewhere before the statement — which would make the loop above
    # pass for the wrong reason.
    bound = [p for _, p, _ in recorder.statements if p.get("workspace_id")]
    assert bound and all(p["workspace_id"] == injection for p in bound)
