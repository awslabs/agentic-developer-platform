"""The production `RegistrationStore`: reserve, finalize and release, over a real store.

Issue #5533 (w6-10), EPIC #4910. Added by the F1 repair, shaped by F5.

## What this is

`RegistrationStore` was a Protocol with no implementation (F1), so nothing could
actually record a workspace. This module provides the production one, in two halves:

- `SqlRegistrationStore` — the real store, over an injected connection whose single
  method executes SQL. It implements the reserve/finalize/release contract F5 requires,
  using the locking pattern `app/installation_bootstrap.py` already establishes.
- `TransactionalStore` — a Protocol for that connection, so the whole store is testable
  offline with a scripted double and so this package does not import a database driver.

## Why the locking pattern is copied rather than invented

`src/superplane-api/app/installation_bootstrap.py` already writes exactly these rows —
`Cluster` keyed on `eks_cluster_arn`, `Workspace` keyed on `namespace_name` — and it
already solved the concurrency problem this module faces:

```
SELECT pg_advisory_xact_lock(hashtextextended(:binding, 0))
```

then `SELECT ... FOR UPDATE` on the rows, refusing when the binding differs. Two
bootstraps racing for one workspace serialize on the advisory lock, and the second one
sees the first one's row.

Reproducing that shape matters more than elegance. A second, different concurrency
strategy against the same two tables would be a correctness problem that only appears
under load: one path taking a transaction-scoped advisory lock and another relying on a
unique constraint can both be correct alone and interleave wrongly together.

## Why the reservation is a row and not an in-memory claim

The F5 interruption case is a process that DIES between clearing the taint and writing
the registration. A claim held in memory is gone at exactly the moment it is needed, so
the reservation is a durable row with a state column: `reserved` after step 3,
`registered` after step 9. `recover_interrupted_bootstrap` can then find a reservation
that was never finalized, which is the fact that tells it the nodes are schedulable for
a bootstrap that never completed.

## F10: the lock serializes, the TOKEN fences — and only the token survives the commit

Review finding F10 verbatim: "After the transaction-scoped advisory lock is released, a
second process with the same identity encounters the existing `reserved` row and receives
`reserved: True`. Both processes can then mutate the namespace, controller, taint, state,
and registration concurrently. [...] A losing process may also release the shared
reservation while the winner is active, causing the winner's finalization to fail and
re-taint the workspace."

That is the difference between serialization and exclusion, and the first revision
confused them. `pg_advisory_xact_lock` is transaction-scoped precisely so a crashed
process leaks nothing — which means it is gone the instant `reserve` commits, and the
eight steps the reservation exists to guard all run AFTER that. The lock makes two
`reserve` calls take turns; it cannot make the second one lose, and "the second one sees
the first one's row" was treated as evidence of a retry rather than of a live competitor.

Nothing distinguished the two, because a `reserved` row recorded WHAT was claimed and not
WHO holds it. So the row now carries an `attempt_token`: opaque material generated here,
inside the same transaction as the insert, and returned to exactly one caller.

- `reserve` hands a token to the attempt that CREATED the row, and to no one else. A
  second attempt finding a matching `reserved` row is refused as a live claim.
- `finalize` and `release` require that token and check it against the row under the
  lock. An attempt cannot complete or drop a claim it does not hold, so the loser-release
  the finding describes is not a race to win — it is unauthorized.
- A matching `registered` row is still a direct replay, because a COMPLETED registration
  is not a live attempt and re-running a finished bootstrap must stay idempotent.

The cost is that a resumed bootstrap no longer walks into its own abandoned claim. That
is deliberate and it is the point: from inside `reserve` a claim abandoned by a dead
process and one held by a live one are the same row. Taking over is a decision that needs
what only the durable state file knows — see `release_claim` below and
`workspace.recover_interrupted_bootstrap`.

## F13: the recovery release is fenced too, on a fingerprint

`release_claim` was `release_abandoned`, and it took only a workspace id: it deleted
whichever `reserved` row existed. The justification was that the durable state file
authorized it — but that file held a BOOLEAN, so it could say a claim was outstanding and
not WHICH one. A stale record from a long-dead attempt was therefore accepted as authority
over a LIVE successor's claim, and deleting it admitted a third concurrent writer. F10's
defect, reached around the fence instead of through it.

The recovery path cannot hold the token — that is the situation it exists for — so it holds
a one-way fingerprint of it (`state.claim_fingerprint`) and the statement digests the column
to compare. Recovery proves which claim it is clearing without the record ever becoming an
authorization, and a fingerprint that matches nothing releases nothing.

## Atomicity is the store's job, stated explicitly

`reserve` must be atomic: two concurrent callers must produce one reservation and one
refusal. Here that is the advisory lock plus the `FOR UPDATE` read inside one
transaction. `TransactionalStore.transaction()` exists so the three statements cannot
be executed outside one — a reserve split across three autocommitted statements would
have exactly the race the lock is for.

## No credential, and no SQL built from a caller's string

Every statement below is a constant with bound parameters. There is no f-string
interpolation into SQL anywhere in this module, so a workspace id containing SQL cannot
change the statement's meaning. The `identity` mapping is serialized as JSON and bound
as one parameter for the same reason.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import BootstrapRefused
from .state import CLAIM_DIGEST_PREFIX

# Reservation lifecycle states. `RESERVED` is a claim taken before mutation; `REGISTERED`
# is a completed registration. The distinction is what makes an interrupted bootstrap
# detectable: a row still in RESERVED after the taint was cleared is precisely the F5
# interruption.
RESERVED = "reserved"
REGISTERED = "registered"

# The advisory-lock key prefix, matching the `"superplane-bootstrap:" + org_id` shape
# `installation_bootstrap.py` uses. Keyed per WORKSPACE here rather than per org,
# because two workspaces in one org may legitimately bootstrap concurrently and an
# org-wide lock would serialize them for no reason.
_LOCK_PREFIX = "superplane-workspace-bootstrap:"

# Transaction-scoped, so it releases on commit or rollback without an explicit unlock.
# A session-scoped lock leaked by a crashed process would block every later attempt for
# that workspace until someone found and cleared it by hand.
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(:binding, 0))"

# The number of random BYTES in an attempt token, before hex encoding. 32 bytes is
# `secrets.token_hex`'s own documented "comfortably enough for any reasonable use"
# threshold. The token authorizes `finalize` and `release` on a claim, so guessing one is
# guessing permission to unpublish a workspace — the same reason it comes from `secrets`
# and not from `uuid4` or a counter.
_TOKEN_BYTES = 32

_SELECT_FOR_UPDATE = """
SELECT workspace_id, state, identity_json, attempt_token
FROM workspace_bootstrap_reservations
WHERE workspace_id = :workspace_id
FOR UPDATE
"""

_INSERT_RESERVATION = """
INSERT INTO workspace_bootstrap_reservations
    (workspace_id, state, identity_json, attempt_token)
VALUES (:workspace_id, :state, :identity_json, :attempt_token)
"""

# `attempt_token` is carried through unchanged, and the statement is deliberately narrowed
# by it. Even having re-read the row under the lock, the UPDATE names the token so the
# database itself refuses a write against a row that changed identity between the read and
# the write — the check and the mutation are one statement rather than two.
_MARK_REGISTERED = """
UPDATE workspace_bootstrap_reservations
SET state = :state, identity_json = :identity_json
WHERE workspace_id = :workspace_id AND attempt_token = :attempt_token
"""

# `RETURNING` so the caller can distinguish "released a claim" from "there was nothing
# to release". Without it a DELETE yields no rows either way, and `release` would have
# to report success unconditionally — which is exactly the kind of unearned True this
# package refuses elsewhere. `workspace.py::_release_reservation` records the answer in
# `BootstrapOutcome.reservation_released`, so a wrong one would be reported as fact.
#
# F10: `AND attempt_token = :attempt_token` is the fence, and it is in the statement
# rather than in a preceding read for the same reason as above. The finding's second half
# is a LOSER releasing the winner's claim; here that DELETE matches no row, `RETURNING`
# yields nothing, and the loser is told it released nothing — which is true.
_DELETE_RESERVATION = """
DELETE FROM workspace_bootstrap_reservations
WHERE workspace_id = :workspace_id
  AND state = :state
  AND attempt_token = :attempt_token
RETURNING workspace_id
"""

# The recovery path's release. F13: keyed on the workspace, the reserved state, AND a
# FINGERPRINT of the token — so it is fenced, just by a different key than `release`.
#
# The digest is computed in SQL, over the column, rather than by selecting the token and
# hashing it here. Two reasons, and the first is the finding:
#
# - It keeps the check and the deletion in ONE statement. A read-then-delete would leave a
#   window in which the row can change between them, and the row changing is exactly the
#   case this fix is about: the recovering process is by definition working from a record
#   that may be stale, so it must not be able to observe a claim and then delete a
#   different one. `RETURNING` reports whether the claim it named was the claim it dropped.
# - The token never leaves the database. Recovery holds a digest and asks an equality
#   question; it is never in a position to learn the token it is not authorized to hold.
#
# `encode(sha256(convert_to(...)))` rather than `digest()` from pgcrypto: `sha256` is a
# core function since PostgreSQL 11, so this needs no extension — an extension the
# migration would have to create, and that may not be installable on a managed instance.
# `convert_to(..., 'UTF8')` makes the encoding explicit rather than inheriting the server
# or client encoding, because a digest computed over different bytes than
# `state.claim_fingerprint` used would silently never match, and never-matching here fails
# in the SAFE direction (no delete) — which is exactly the kind of quiet failure that
# survives review. `tests/test_registry_postgres.py` pins the two against each other
# against a real server for that reason.
_DELETE_CLAIMED_RESERVATION = """
DELETE FROM workspace_bootstrap_reservations
WHERE workspace_id = :workspace_id
  AND state = :state
  AND encode(sha256(convert_to(:digest_prefix || attempt_token, 'UTF8')), 'hex')
      = :claim_fingerprint
RETURNING workspace_id
"""

_READ_REGISTRATION = """
SELECT identity_json
FROM workspace_bootstrap_reservations
WHERE workspace_id = :workspace_id AND state = :state
"""


@runtime_checkable
class TransactionalStore(Protocol):
    """A store that can execute parameterized SQL inside an explicit transaction.

    Narrow on purpose. `execute` takes a statement and bound parameters and returns
    rows; there is no method that takes a connection string, opens a session, or runs
    arbitrary DDL. The production implementation wraps the domain API's async session;
    the tests pass a scripted double, which is what keeps this module's whole surface
    offline-testable.
    """

    def transaction(self) -> object:
        """A context manager for one transaction. Commits on exit, rolls back on error."""

    def execute(
        self, statement: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        """Execute one parameterized statement, returning any rows it produced."""


_HELD_REGISTRATION_LOCKS = ContextVar(
    "superplane_registration_locks", default=frozenset()
)


@contextmanager
def registration_lock(store, workspace_id):
    """Share one real transaction when recovery calls a fenced interlock adapter.

    Reentrancy is restricted to this exact store and workspace in this execution
    context. Every caller still compares its own claim and operation under the
    lock. No nested transaction can commit or release the outer reservation.
    """
    key = (id(store), workspace_id)
    held = _HELD_REGISTRATION_LOCKS.get()
    if key in held:
        yield
        return
    with store.transaction():
        store.execute(_LOCK, {"binding": _LOCK_PREFIX + workspace_id})
        token = _HELD_REGISTRATION_LOCKS.set(held | {key})
        try:
            yield
        finally:
            _HELD_REGISTRATION_LOCKS.reset(token)


@dataclass(frozen=True)
class SqlRegistrationStore:
    """`RegistrationStore` over a real transactional store.

    Holds no credential: the connection is injected already authenticated, the same way
    `installation_bootstrap.py` receives its session. There is no field here that could
    hold a password or a DSN.
    """

    store: TransactionalStore

    def _require_authority_revoked(self, workspace_id):
        pending = self.store.execute(
            "SELECT generation FROM workspace_bootstrap_authority "
            "WHERE workspace_id=:workspace_id AND revoked=false",
            {"workspace_id": workspace_id},
        )
        if pending:
            raise BootstrapRefused(
                "temporary authority recovery is outstanding; reservation retained"
            )

    # --- the F5 reserve/finalize/release contract ---------------------------

    def reserve(
        self, workspace_id: str, identity: Mapping[str, str]
    ) -> Mapping[str, object]:
        """Atomically claim this workspace, or report what prevents it.

        Runs inside one transaction holding the workspace's advisory lock, so a
        concurrent attempt blocks until this one commits and then sees the row. Four
        outcomes, and the split between the middle two is the F10 fix:

        - **No row** — insert a reservation with a fresh `attempt_token` and return
          `reserved: True` with that token. This is the only caller that ever receives it.
        - **A matching `reserved` row** — a live claim held by another attempt. Returns
          `reserved: False`. The first revision returned `reserved: True, replayed: True`
          here, which authorized a second concurrent bootstrap of the same workspace: the
          advisory lock had already been released by the commit, so both processes went on
          to mutate the namespace, the controller, the taint and the registration. See the
          module docstring.
        - **A matching `registered` row** — a completed registration. A direct replay,
          `reserved: True, replayed: True`, with no token: there is nothing left to
          finalize or release, so there is nothing to authorize.
        - **A row with a DIFFERENT identity** — a rebinding. Returns `reserved: False`
          with the conflict, and because this runs before any mutation, nothing needs
          undoing.
        """
        payload = json.dumps(dict(identity), sort_keys=True)
        with self.store.transaction():
            self.store.execute(_LOCK, {"binding": _LOCK_PREFIX + workspace_id})
            self._require_authority_revoked(workspace_id)
            rows = self.store.execute(
                _SELECT_FOR_UPDATE, {"workspace_id": workspace_id}
            )
            if not rows:
                # Generated HERE, inside the transaction that inserts it, and returned
                # only to this caller. A token supplied by the caller would be a claim
                # the caller could also assert on somebody else's row.
                token = secrets.token_hex(_TOKEN_BYTES)
                self.store.execute(
                    _INSERT_RESERVATION,
                    {
                        "workspace_id": workspace_id,
                        "state": RESERVED,
                        "identity_json": payload,
                        "attempt_token": token,
                    },
                )
                return {
                    "reserved": True,
                    "replayed": False,
                    "attempt_token": token,
                }

            existing = rows[0]
            recorded = self._decode_identity(existing, workspace_id)
            # Compared over the RESERVATION's OWN keys, not the union of both sides.
            #
            # A row in state `registered` holds the full record `finalize` wrote —
            # `namespace_uid`, `endpoint`, `contract_version`,
            # `credential_reference_id`, `cluster_ownership` — and a reservation
            # identity by construction holds only `_RESERVATION_FIELDS`, because the uid
            # does not exist until the namespace is created. Over the union, every one of
            # those extra fields is "recorded but not requested" and therefore divergent,
            # so re-running a COMPLETED bootstrap was refused as an attempted rebinding
            # naming five fields that had not changed at all — and the operator-visible
            # message said the workspace "binds differently" when it binds identically.
            #
            # The narrower comparison loses nothing: every field this reservation does not
            # name is re-checked against the full identity by `finalize_registration`,
            # which is where `namespace_uid` is compared. What the reservation is for is
            # deciding, before any mutation, whether this is the same workspace on the
            # same cluster — and that is exactly the keys it was given.
            divergent = sorted(
                name
                for name in identity
                if str(recorded.get(name, "")) != str(identity.get(name, ""))
            )
            if divergent:
                return {
                    "reserved": False,
                    "conflict": (
                        f"workspace is already bound with a different "
                        f"{', '.join(divergent)}"
                    ),
                }

            # The identity matches. WHAT the row is now decides, which it did not before.
            if str(existing.get("state", "")) == REGISTERED:
                # Completed. Re-running a finished bootstrap is the most ordinary operator
                # action there is and stays idempotent — and it is safe precisely because
                # a registered row is not a live attempt: no token is issued, and neither
                # `finalize` nor `release` can act on it.
                return {"reserved": True, "replayed": True}

            # A matching `reserved` row: another attempt holds this workspace. Refused
            # WITHOUT a token, so this caller cannot finalize or release the claim it just
            # lost. The message names recovery rather than suggesting a retry, because a
            # retry against a live holder will be refused again and again — and if the
            # holder is dead instead, recovery is the path that can establish that from
            # the durable state file and take the claim over.
            return {
                "reserved": False,
                "conflict": (
                    "another bootstrap attempt already holds an unfinalized reservation "
                    "for this workspace with the same identity. Two attempts must not "
                    "mutate one workspace concurrently. If the holding attempt is gone, "
                    "`recover` releases its claim from the durable record"
                ),
            }

    def finalize(self, target: object, attempt_token: str = "") -> None:
        """Complete the reserved registration. The only write that publishes a record.

        Re-takes the lock and re-reads the reservation rather than trusting that the one
        taken at `reserve` is still there: between the two, an operator or a concurrent
        teardown may have released it, and completing a registration whose claim is gone
        would publish a record for a workspace something else now owns.

        F10: `attempt_token` must be the one `reserve` issued to THIS attempt. Without it
        an attempt that lost the reservation could still publish the record — which is the
        same defect as two concurrent bootstraps, arriving at the last step instead of the
        first. It is keyword-optional in the signature only so a blank token reaches the
        refusal below with a diagnosis, rather than a `TypeError` from a caller that has
        not been updated.
        """
        workspace_id = str(getattr(target, "workspace_id", "") or "")
        if not workspace_id.strip():
            raise BootstrapRefused(
                "cannot finalize a registration for a blank workspace id"
            )
        token = str(attempt_token or "")
        if not token.strip():
            raise BootstrapRefused(
                f"no attempt token was supplied to finalize workspace {workspace_id!r}; "
                "refusing to publish a registration without proving which reservation "
                "authorized it, because an attempt that lost the claim would otherwise "
                "publish over the one that holds it"
            )
        payload = json.dumps(_target_mapping(target), sort_keys=True)
        with self.store.transaction():
            self.store.execute(_LOCK, {"binding": _LOCK_PREFIX + workspace_id})
            self._require_authority_revoked(workspace_id)
            rows = self.store.execute(
                _SELECT_FOR_UPDATE, {"workspace_id": workspace_id}
            )
            if not rows:
                raise BootstrapRefused(
                    f"no reservation exists for workspace {workspace_id!r}; refusing to "
                    "publish a registration whose pre-mutation claim is gone, because "
                    "something else may now own this workspace"
                )
            if str(rows[0].get("state", "")) == REGISTERED:
                # Already published. Not an error: the caller's replay handling in
                # `finalize_registration` compares the records and decides. Checked before
                # the token, because a completed registration is the idempotent case and
                # must not become a token error — the token that published it is long gone.
                return
            # `compare_digest` rather than `!=`: the comparison is against secret material
            # over a path a caller can retry, so it is kept constant-time on principle
            # even though the surrounding lock makes timing a poor oracle here.
            recorded = str(rows[0].get("attempt_token", "") or "")
            if not recorded or not secrets.compare_digest(recorded, token):
                raise BootstrapRefused(
                    f"the reservation for workspace {workspace_id!r} is held by a "
                    "different bootstrap attempt; refusing to publish a registration "
                    "this attempt is not authorized to complete. Another attempt is "
                    "mutating this workspace, and finalizing over it would publish a "
                    "record describing a cluster state neither attempt established"
                )
            identity = _target_mapping(target)
            reserved_identity = self._decode_identity(rows[0], workspace_id)
            if any(
                identity.get(key) != value for key, value in reserved_identity.items()
            ):
                raise BootstrapRefused(
                    "finalization identity differs from the reserved target"
                )
            from .canonical import publish

            publish(self.store, identity)
            self.store.execute(
                _MARK_REGISTERED,
                {
                    "workspace_id": workspace_id,
                    "state": REGISTERED,
                    "identity_json": payload,
                    "attempt_token": token,
                },
            )

    def release(self, workspace_id: str, attempt_token: str = "") -> bool:
        """Drop THIS ATTEMPT'S unfinalized reservation. Never a completed registration.

        The `AND state = 'reserved'` in the statement is the safety property, not an
        optimization: this runs on a refusal path, and a bug that released a completed
        registration would silently unpublish a live workspace. A row already
        REGISTERED is left alone and the method reports that nothing was released.

        F10 adds `AND attempt_token = :attempt_token`, which closes the finding's second
        half: "a losing process may also release the shared reservation while the winner
        is active, causing the winner's finalization to fail and re-taint the workspace".
        A loser's release now matches no row and honestly reports that it released nothing.
        A blank token refuses rather than deleting: an unfenced delete on this path is
        exactly the defect, so it must be unreachable and not merely discouraged.

        Returns whether a claim was actually dropped, from the statement's `RETURNING`
        rather than from the absence of an exception. The caller records this in
        `BootstrapOutcome.reservation_released`, so an unconditional True would be a
        reported fact that nothing established.
        """
        token = str(attempt_token or "")
        if not token.strip():
            raise BootstrapRefused(
                f"no attempt token was supplied to release workspace {workspace_id!r}; "
                "refusing an unfenced release, because a release that does not name the "
                "attempt holding the claim can drop a live attempt's reservation. To "
                "clear a claim left by an attempt that is gone, use `release_claim`"
            )
        with self.store.transaction():
            self.store.execute(_LOCK, {"binding": _LOCK_PREFIX + workspace_id})
            self._require_authority_revoked(workspace_id)
            rows = self.store.execute(
                _DELETE_RESERVATION,
                {
                    "workspace_id": workspace_id,
                    "state": RESERVED,
                    "attempt_token": token,
                },
            )
        return bool(rows)

    def recover_claim(self, workspace_id, fingerprint, *, restore):
        """Restore the interlock while the exact reservation excludes successors.

        Returns (claim_matched, restored, released). A failed restoration retains
        the claim. No database answer means no authority to mutate the cluster.
        The callback runs under the SAME lock as reserve/finalize/release.
        """
        from .state import claim_fingerprint

        if not isinstance(fingerprint, str) or not fingerprint:
            raise BootstrapRefused("recovery requires an exact claim fingerprint")
        with registration_lock(self.store, workspace_id):
            rows = self.store.execute(
                _SELECT_FOR_UPDATE, {"workspace_id": workspace_id}
            )
            if (
                len(rows) != 1
                or rows[0]["state"] != RESERVED
                or claim_fingerprint(rows[0]["attempt_token"]) != fingerprint
            ):
                return False, False, False
            restored = restore()
            if restored is not True:
                return True, False, False
            self._require_authority_revoked(workspace_id)
            rows = self.store.execute(
                _DELETE_CLAIMED_RESERVATION,
                {
                    "workspace_id": workspace_id,
                    "state": RESERVED,
                    "digest_prefix": CLAIM_DIGEST_PREFIX.decode(),
                    "claim_fingerprint": fingerprint,
                },
            )
            if len(rows) != 1:
                raise BootstrapRefused("recovery claim changed while locked")
            return True, True, True

    def release_claim(self, workspace_id: str, claim_fingerprint: str) -> bool:
        """Drop the reservation matching this FINGERPRINT. The recovery path's release.

        **This method is the F13 fix, and it replaced `release_abandoned`.** The old one
        took only a workspace id and deleted whatever `reserved` row it found. It existed
        because a process killed between reserve and finalize takes the only copy of its
        token with it, so something has to be able to clear a claim whose holder is gone —
        otherwise one crash makes a workspace permanently unbootstrappable.

        That reasoning was right; the implementation granted far more than it needed. The
        argument for its safety was that `recover_interrupted_bootstrap` called it only
        when the durable record said this workspace's own attempt reserved and never
        finalized — but that record carried a BOOLEAN. It could say a claim was
        outstanding and not which one, so a stale record naming a long-dead attempt was
        accepted as authority over whichever claim happened to exist now. Recovery run
        from such a record deleted a LIVE successor's claim and admitted a third
        concurrent writer: the concurrent-mutation defect the fence was built to prevent,
        reached around the fence rather than through it.

        So recovery is fenced too, on the one thing a durable record can safely hold: a
        one-way fingerprint of the token (`state.claim_fingerprint`). The claim identifies
        itself without the record becoming an authorization, and the compare-and-delete is
        a single statement — see `_DELETE_CLAIMED_RESERVATION` for why that matters here
        specifically.

        What this preserves, which is the constraint that makes the fix non-trivial: a
        crashed attempt's own record still names its own claim, so the case recovery exists
        for still works. What it removes is the ability to act on a claim the record cannot
        identify. A fingerprint matching no row releases nothing and says so — that is the
        stale case, and doing nothing is the correct answer.

        Still `AND state = 'reserved'`: a completed registration is never dropped here
        either. Recovery unblocks a stuck workspace; it does not unpublish a live one.
        """
        fingerprint = str(claim_fingerprint or "")
        if not fingerprint.strip():
            raise BootstrapRefused(
                f"no claim fingerprint was supplied to release workspace "
                f"{workspace_id!r}; refusing an unfenced recovery release, because a "
                "release that cannot name the claim it is clearing will drop whichever "
                "reservation exists — including a live successor's, which admits a "
                "second concurrent bootstrap of this workspace"
            )
        with self.store.transaction():
            self.store.execute(_LOCK, {"binding": _LOCK_PREFIX + workspace_id})
            self._require_authority_revoked(workspace_id)
            rows = self.store.execute(
                _DELETE_CLAIMED_RESERVATION,
                {
                    "workspace_id": workspace_id,
                    "state": RESERVED,
                    "digest_prefix": CLAIM_DIGEST_PREFIX.decode(),
                    "claim_fingerprint": fingerprint,
                },
            )
        return bool(rows)

    def read(self, workspace_id: str) -> object | None:
        """The completed registration for this workspace, or None.

        Only REGISTERED rows. A reservation is not a registration — returning one here
        would let `finalize_registration` mistake an in-flight claim for an existing
        record and report a replay for a bootstrap that never finished.
        """
        rows = self.store.execute(
            _READ_REGISTRATION,
            {"workspace_id": workspace_id, "state": REGISTERED},
        )
        if not rows:
            return None
        recorded = self._decode_identity(rows[0], workspace_id)
        return _RecordedRegistration(recorded)

    @staticmethod
    def _decode_identity(
        row: Mapping[str, object], workspace_id: str
    ) -> Mapping[str, str]:
        """Parse a stored identity, refusing unreadable JSON.

        Unreadable is a refusal, not an empty default. An empty identity compares equal
        to nothing and would turn an existing binding into a conflict — or, worse, a
        conflict into a match.
        """
        raw = row.get("identity_json")
        if isinstance(raw, Mapping):
            return {str(k): str(v) for k, v in raw.items()}
        try:
            parsed = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError) as error:
            raise BootstrapRefused(
                f"the stored reservation for workspace {workspace_id!r} is not readable "
                "JSON; refusing to compare a binding that cannot be parsed"
            ) from error
        if not isinstance(parsed, Mapping):
            raise BootstrapRefused(
                f"the stored reservation for workspace {workspace_id!r} is not an "
                "object"
            )
        return {str(k): str(v) for k, v in parsed.items()}


class _RecordedRegistration:
    """An existing registration, exposing its fields as attributes.

    `registration._refuse_conflict` reads the existing record by ATTRIBUTE so the store
    may return its own row type. This is that type: a thin attribute view over the
    recorded identity, with a missing field reading as None — which
    `_refuse_conflict` correctly treats as a conflict rather than a match, because a
    record that cannot be compared has not been shown to be the same record.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def __getattr__(self, name: str) -> object:
        try:
            return self._values[name]
        except KeyError:
            return None

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"_RecordedRegistration({sorted(self._values)})"


def _target_mapping(target: object) -> Mapping[str, str]:
    """The persisted form of a `WorkspaceTarget`, read by attribute.

    Duck-typed rather than importing `WorkspaceTarget` so this module stays usable by a
    future store that persists a different record shape — and so the dependency runs one
    way only, from registration to storage.
    """
    names = (
        "workspace_id",
        "org_id",
        "account_id",
        "region",
        "cluster_name",
        "cluster_arn",
        "endpoint",
        "namespace",
        "namespace_uid",
        "cluster_ownership",
        "credential_reference_id",
        "contract_version",
        # Issue #6048. `getattr(..., "") or ""` falls back to "" for a target
        # shape that predates this field, which `canonical.publish` treats the
        # same as an absent key (defaults to dedicated) — so a store double
        # built before this change keeps working unmodified.
        "cluster_placement",
    )
    return {name: str(getattr(target, name, "") or "") for name in names}
