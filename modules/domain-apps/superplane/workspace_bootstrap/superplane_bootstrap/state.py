"""Durable bootstrap state: what this bootstrap actually created, recorded as it goes.

Issue #5533 (w6-10), EPIC #4910. Added by the F3/F5/F6 repair.

## The one problem three findings share

An independent review of the first revision found three defects that look unrelated
and are not:

- **F3** — ownership was inferred from a namespace LABEL. The label key and the
  workspace id are not secrets, so a namespace carrying the right label was treated
  as ADP-created, and ADP-created authorizes deletion of that namespace and every
  workload in it.
- **F6** — a CRD failure after the namespace was created discarded the record of the
  namespace, so the refusal carried `cleanup=None` while an owned namespace sat on
  the cluster.
- **F5** — a process interrupted between clearing the bootstrap taint and writing the
  registration left nodes schedulable with nothing recording that it happened.

All three are the same missing thing: **nothing survived the mutation that caused
them.** Ownership, partial progress and interruption recovery are all questions about
what a PREVIOUS attempt did, and the first revision had no way to answer any of them
except by re-observing the cluster — which is exactly what cannot distinguish "I
created this" from "somebody else created something that looks like this".

So this module records each mutation immediately after it succeeds, and the record is
the authority on ownership. An observation can then only ever *narrow* what the record
claims (see `namespace_ownership`), never widen it.

## Why "immediately after", and never before

A record written before the mutation claims something that may not exist; a record
written after a batch of mutations loses the ones before the crash. Written
immediately after each single mutation, the worst case is a record that is missing the
very last action — and that direction is the safe one, because an unrecorded object is
treated as not-owned and therefore never deleted. The failure mode is "ADP leaves
something behind", not "ADP deletes something it did not create".

`installation/runner.py::atomic` establishes the same write discipline for the
management installer's receipt, and `Installer.phase()` calls `self.save()` after each
phase for the same reason. This module reuses that shape: temp file in the same
directory, fsync, `0o600`, atomic replace.

## Why ownership is a recorded decision and not a stored boolean

`namespace_ownership` requires BOTH the durable creation record AND a live uid match
before it will answer "owned". Two independent facts have to agree:

- The record says *this* bootstrap created a namespace of this name, and
- the namespace on the cluster right now still has the uid that was recorded at
  creation.

The uid is what distinguishes the object ADP created from a different object that
later took the same name. Either fact alone is forgeable or stale: a label is
forgeable (F3), and a creation record alone cannot see that the namespace was deleted
and recreated by somebody else in between.

## No credential is recorded here

Every field is an identifier, a name, a uid, a boolean or a one-way fingerprint. There
is deliberately no field that could hold a kubeconfig, a token or a provider key — the
same structural guarantee `access.py` makes for the seams. `state_is_credential_free` in
`tests/test_state.py` pins it by reading the dataclass fields.

`registration_claim` is the one field that touches secret material, and it holds a
SHA-256 fingerprint rather than the attempt token itself — see `claim_fingerprint`.
That distinction is the whole reason the field can exist here at all.

## F13: the record has to say WHICH claim it owns

`registration_reserved` was a bare boolean: "an attempt reserved and did not finish".
Recovery read it and then deleted whatever reservation existed for the workspace, so it
could not tell its own dead attempt from a live successor that legitimately holds the
workspace now — and deleting the successor's claim admitted a third concurrent writer,
which is the concurrent-mutation defect the attempt-token fence exists to prevent,
reached around the fence instead of through it.

The missing fact was identity, so `registration_claim` records it. The boolean says a
claim is outstanding; the fingerprint says which one. Recovery may act only on the claim
its own record names.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import BootstrapRefused
from .inventory import PrerequisiteInventory, inventory_from_mapping

# Bumped only when a field's MEANING changes. An unknown version is refused rather
# than best-effort parsed: a state file this package cannot interpret is one whose
# ownership claims it cannot trust, and guessing at ownership is how F3 happened.
STATE_VERSION = 1

# Ownership vocabulary, shared with `inventory.py` so one word does not mean two
# things in two modules.
ADP_CREATED = "adp-created"
ADOPTED = "adopted"

# Prefix mixed into the claim fingerprint so the digest recorded here cannot be
# substituted for a digest of the same token computed for any other purpose. Domain
# separation, the same reason `_LOCK_PREFIX` exists in `registry.py`.
CLAIM_DIGEST_PREFIX = b"superplane-workspace-bootstrap-claim:v1:"


def claim_fingerprint(attempt_token: str) -> str:
    """A one-way fingerprint of an attempt token, safe to write to disk (F13).

    Recovery has to prove it is clearing ITS OWN claim, which means the durable record
    must identify the claim. The obvious way to do that is to write the attempt token
    down, and that is the thing not to do: the token authorizes `finalize` and `release`,
    so a copy of it on disk is a copy of the permission to publish or unpublish this
    workspace. The state file is `0o600`, but "only the owner can read it" is a weaker
    property than "reading it grants nothing", and this module's stated guarantee is the
    second one.

    A digest gives exactly the property recovery needs and nothing more. Recovery must
    answer "is the claim in the database the one my record names?" — an equality question,
    which a one-way function answers — and must NOT be able to answer "what is the token?",
    because being able to would make the record an authorization.

    Unsalted and deterministic, which is required rather than an oversight: the comparison
    happens across processes, and a salted digest could only be checked by whoever held
    the salt. It is safe here because the input is 32 bytes from `secrets.token_hex` —
    there is no dictionary to run against a uniform 256-bit value, which is the threat
    salting addresses.

    The `AND attempt_token = :attempt_token` fence in `registry.py` is unchanged and still
    compares real tokens under the lock. This is the recovery path's separate question,
    and `registry.release_claim` is where the two meet: it digests the column in SQL and
    compares against this value, so the token never has to leave the database.
    """
    token = str(attempt_token or "")
    if not token.strip():
        raise BootstrapRefused(
            "cannot fingerprint a blank attempt token; a claim record that identifies "
            "no claim would let recovery delete whichever reservation it found, which "
            "is the defect the fingerprint exists to close"
        )
    return hashlib.sha256(CLAIM_DIGEST_PREFIX + token.encode()).hexdigest()


@dataclass(frozen=True)
class NamespaceRecord:
    """A namespace this bootstrap CREATED, with the uid observed at creation.

    Only ever written on the success path of a create. An adopted namespace gets no
    record — absence of a record is what makes adoption the default, and the default
    is the safe one because cleanup may not delete what it did not record.
    """

    name: str
    uid: str
    created_by_bootstrap: bool = True

    def __post_init__(self) -> None:
        for name in ("name", "uid"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise BootstrapRefused(
                    f"NamespaceRecord.{name} is required; a creation record without "
                    "a uid cannot establish ownership on a later attempt"
                )


@dataclass(frozen=True)
class BootstrapState:
    """The durable account of one workspace's bootstrap, across attempts.

    Read at the start of an attempt and updated after each mutation. `workspace_id`
    and `cluster_arn` are carried so a state file cannot be applied to a different
    workspace or a different cluster — reading one that names another workspace is a
    refusal, not a merge.
    """

    workspace_id: str
    cluster_arn: str
    version: int = STATE_VERSION
    namespace: NamespaceRecord | None = None
    crds_established: tuple[str, ...] = field(default_factory=tuple)
    # F2/F6: the controller and its scoped RBAC are namespaced objects this bootstrap
    # OWNS, unlike the shared CRDs. Recorded separately from `crds_established` because
    # a run interrupted between the two leaves a different set of objects behind, and
    # cleanup planned from the wrong set either misses them or deletes what it did not
    # create.
    controller_installed: bool = False
    prerequisites_recorded: bool = False
    prerequisite_inventory: PrerequisiteInventory | None = None
    registration_reserved: bool = False
    # F13: WHICH claim `registration_reserved` refers to, as a `claim_fingerprint` of the
    # attempt token — never the token itself. Empty when no claim is held, and empty on a
    # replay of a completed registration, which carries no token because there is nothing
    # left to authorize. `recovery_claim` below is the only reader.
    registration_claim: str = ""
    taint_clear_pending: bool = False
    taint_cleared: bool = False
    registration_finalized: bool = False
    taint_restored: bool = False

    def __post_init__(self) -> None:
        if self.prerequisite_inventory is not None:
            if (
                not isinstance(self.prerequisite_inventory, PrerequisiteInventory)
                or self.prerequisite_inventory.workspace_id != self.workspace_id
            ):
                raise BootstrapRefused(
                    "durable prerequisite inventory belongs to another workspace or is malformed"
                )
        for name in ("workspace_id", "cluster_arn"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise BootstrapRefused(f"BootstrapState.{name} is required")
        if self.version != STATE_VERSION:
            raise BootstrapRefused(
                f"bootstrap state version {self.version} is not {STATE_VERSION}; "
                "refusing to interpret a state file written by a different version, "
                "because its ownership claims may not mean what they appear to"
            )

    @property
    def interrupted_after_taint_cleared(self) -> bool:
        """The HISTORICAL fact: an attempt cleared the interlock and never registered.

        A previous attempt cleared the interlock and did not finish registering. The
        nodes were made schedulable for a bootstrap that never completed, which is the
        state the review names as "process interruption between taint removal and
        registration".

        **This is not the recovery decision, and finding F12 was the consequence of
        using it as one.** It says an interruption HAPPENED; it cannot say whether
        anything is still wrong, because it does not read `taint_restored`. Recovery
        gated on it re-applied the taint and re-reported the same interruption on every
        subsequent call, so the record never converged — the exact defect the recovery
        test claimed to rule out.

        Kept, with the name it has, because the fact itself is real and an operator
        reading `state` wants it: "this workspace was interrupted mid-bootstrap once" is
        the difference between a clean history and one that needs explaining. What
        changed is that nothing DECIDES from it. `recovery_pending` below is the
        decision, and the two properties beneath it are the two independent halves of it.
        """
        return self.taint_cleared and not self.registration_finalized

    @property
    def interlock_restoration_pending(self) -> bool:
        """Nodes are schedulable for a bootstrap that never completed. Put the taint back.

        The F12 split, first half. `taint_restored` is what makes this converge: once
        recovery has restored the interlock and recorded it, there is nothing left to do
        here, and a second recovery call must not re-apply a taint that is already on.
        Re-applying is not harmless — `restore_bootstrap_taint` is a write against every
        node, and an operator watching a recovery repeat itself has no way to tell a
        loop from a cluster that keeps losing the taint.
        """
        return (
            (self.taint_clear_pending or self.taint_cleared)
            and not self.registration_finalized
            and not self.taint_restored
        )

    @property
    def reservation_release_pending(self) -> bool:
        """An abandoned attempt's claim is still recorded as held. Release it.

        The F12 split, second half, and deliberately NOT conditioned on `taint_cleared`.
        A reservation is taken at step 3, before the first mutation, so an attempt that
        refused at step 4 strands one just as thoroughly as one interrupted at step 9 —
        and a stranded reservation blocks every later attempt for this workspace, which
        is precisely what recovery exists to unblock.

        Independent of the half above because the two fail independently. The release
        can fail while the restoration succeeds (a database is unreachable while the
        cluster is fine) and the reverse. Conflating them meant the restoration's success
        masked the release's failure: the state converged on "recovered" with a claim
        still out there, and nothing ever revisited it.

        F13: this says a claim is outstanding. It does NOT say recovery may delete one —
        `reservation_release_authorized` is that decision, and the split is the fix. Using
        this property as the authority is what let a stale record delete a live
        successor's claim.
        """
        return self.registration_reserved and not self.registration_finalized

    @property
    def reservation_release_authorized(self) -> bool:
        """Whether recovery may attempt a release at all — the F13 authority check.

        A claim is outstanding AND this record can say which one. The second half is new
        and is the fix: `reservation_release_pending` alone was treated as permission to
        delete whatever reservation existed, which is not the same question and is not
        answerable from a boolean.

        False with a pending release means the record knows a claim was taken and cannot
        identify it — a record written before the fingerprint existed. That is not
        "nothing to do" and not "delete it anyway": it is a claim no automated path may
        take over, because the only way to act on it would be the unfenced delete this
        finding is about. Recovery says so and leaves it to an operator. See
        `recover_interrupted_bootstrap`.
        """
        return self.reservation_release_pending and bool(
            self.registration_claim.strip()
        )

    @property
    def recovery_pending(self) -> bool:
        """Whether `recover_interrupted_bootstrap` has any genuinely pending work.

        The recovery decision, replacing `interrupted_after_taint_cleared` in that role.
        False means recovery is a no-op — which is what lets it be called
        unconditionally at the start of every attempt AND converge, two requirements
        that were in conflict while the decision ignored `taint_restored`.
        """
        return self.interlock_restoration_pending or self.reservation_release_pending

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


def _namespace_from_mapping(value: object) -> NamespaceRecord | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BootstrapRefused(
            "bootstrap state namespace record is not an object; refusing to infer "
            "ownership from an unreadable record"
        )
    try:
        return NamespaceRecord(
            name=str(value.get("name", "")),
            uid=str(value.get("uid", "")),
            created_by_bootstrap=bool(value.get("created_by_bootstrap", True)),
        )
    except BootstrapRefused:
        raise
    except (TypeError, ValueError) as error:  # pragma: no cover - defensive
        raise BootstrapRefused(
            "bootstrap state namespace record could not be read"
        ) from error


def state_from_mapping(value: Mapping[str, object]) -> BootstrapState:
    """Rebuild state from its serialized form, refusing anything unreadable.

    Every unknown or malformed field is a refusal rather than a default. A state file
    that cannot be read is not the same as no state file: no state means "nothing was
    created", and defaulting to that for a file that EXISTS would re-introduce F6 —
    a real namespace with no cleanup plan.
    """
    if not isinstance(value, Mapping):
        raise BootstrapRefused("bootstrap state must be a mapping")
    known = {spec.name for spec in fields(BootstrapState)}
    unknown = sorted(set(value) - known)
    if unknown:
        raise BootstrapRefused(
            "bootstrap state carries unknown fields (" + ", ".join(unknown) + "); "
            "refusing rather than ignoring state this version cannot interpret"
        )
    return BootstrapState(
        workspace_id=str(value.get("workspace_id", "")),
        cluster_arn=str(value.get("cluster_arn", "")),
        version=int(value.get("version", 0)),
        namespace=_namespace_from_mapping(value.get("namespace")),
        crds_established=tuple(str(name) for name in value.get("crds_established", ())),
        controller_installed=bool(value.get("controller_installed", False)),
        prerequisites_recorded=bool(value.get("prerequisites_recorded", False)),
        prerequisite_inventory=inventory_from_mapping(value["prerequisite_inventory"])
        if value.get("prerequisite_inventory") is not None
        else None,
        registration_reserved=bool(value.get("registration_reserved", False)),
        # F13: absent in records written before the claim fingerprint existed, and they
        # are the reason this defaults rather than refusing. Such a record is readable —
        # every ownership claim in it still means what it says, which is what
        # `state_from_mapping` refuses over — it simply cannot identify its reservation.
        # Defaulting to "" makes that visible as an unauthorized release rather than
        # silently granting the token-free delete, and refusing the whole file instead
        # would strand the namespace cleanup plan the rest of the record carries.
        registration_claim=str(value.get("registration_claim", "") or ""),
        taint_clear_pending=bool(value.get("taint_clear_pending", False)),
        taint_cleared=bool(value.get("taint_cleared", False)),
        registration_finalized=bool(value.get("registration_finalized", False)),
        taint_restored=bool(value.get("taint_restored", False)),
    )


@runtime_checkable
class StateStore(Protocol):
    """Durable storage for one workspace's bootstrap state.

    A Protocol for the same reason the cluster seams are: the tests use an in-memory
    implementation, and `FileStateStore` below is the production one. Both are
    exercised — an in-memory fake alone could not show that a record survives the
    process, which is the entire point of this module.
    """

    def load(self) -> BootstrapState | None:
        """The recorded state, or None if this workspace has no prior attempt."""

    def save(self, state: BootstrapState) -> None:
        """Persist the state durably before the next mutation is attempted."""


class FileStateStore:
    """State on local disk, written atomically with the installer's discipline.

    `installation/runner.py::atomic` is the precedent and this matches it: a temp file
    in the destination directory, `fsync` before rename, `0o600`, then an atomic
    `replace`. The rename is what makes a crash mid-write leave the PREVIOUS state
    rather than a truncated file — and a truncated file would be a refusal on the next
    read, which would strand the workspace.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> BootstrapState | None:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise BootstrapRefused(
                f"bootstrap state at {self.path} exists but could not be read; "
                "refusing to treat an unreadable record as 'nothing was created'"
            ) from error
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise BootstrapRefused(
                f"bootstrap state at {self.path} is not valid JSON; refusing to "
                "treat a corrupt record as 'nothing was created', because an owned "
                "namespace would then have no cleanup plan"
            ) from error
        return state_from_mapping(parsed)

    def save(self, state: BootstrapState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.path.parent, delete=False
        ) as stream:
            json.dump(state.to_mapping(), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


def load_state(
    store: StateStore, *, workspace_id: str, cluster_arn: str
) -> BootstrapState:
    """The prior attempt's state, or a fresh one. Refuses a mismatched record.

    A state file naming a different workspace or cluster is refused rather than
    overwritten: it is either the wrong file or evidence that this workspace was
    bootstrapped against a different cluster, and both need an operator, not a
    silent reset that would orphan whatever the other record owned.
    """
    existing = store.load()
    if existing is None:
        return BootstrapState(workspace_id=workspace_id, cluster_arn=cluster_arn)
    if existing.workspace_id != workspace_id:
        raise BootstrapRefused(
            "bootstrap state records a different workspace "
            f"({existing.workspace_id!r}, not {workspace_id!r}); refusing to reuse "
            "it, because its ownership claims describe another workspace's objects"
        )
    if existing.cluster_arn != cluster_arn:
        raise BootstrapRefused(
            "bootstrap state records a different cluster "
            f"({existing.cluster_arn!r}, not {cluster_arn!r}); refusing to reuse it. "
            "A workspace previously bootstrapped against another cluster needs an "
            "operator decision, not a silent rebind"
        )
    return existing


def record(
    store: StateStore, state: BootstrapState, **changes: object
) -> BootstrapState:
    """Update and persist state in one step, returning the persisted value.

    One function so a caller cannot update the in-memory value and forget to write it
    — that omission is invisible until a crash, which is the worst time to discover
    it. The write happens BEFORE this returns, so the caller's next mutation always
    follows a durable record of the previous one.
    """
    updated = replace(state, **changes)  # type: ignore[arg-type]
    store.save(updated)
    return updated


def namespace_ownership(state: BootstrapState, *, name: str, observed_uid: str) -> str:
    """Whether ADP may delete this namespace: `ADP_CREATED` or `ADOPTED`.

    **This function is the F3 fix.** Ownership requires two independent facts to
    agree, and returns `ADOPTED` — the safe answer — whenever they do not:

    1. The durable record says this bootstrap created a namespace of this name.
    2. The uid observed on the cluster right now equals the uid recorded at creation.

    A label is deliberately not consulted. The review's finding was that
    `superplane.aws-e/bootstrap-owner == workspace_id` is forgeable: neither the key
    nor the workspace id is secret, so a BYOC owner or an unrelated prior process can
    create a namespace carrying it, and the first revision then recorded the observed
    uid "as if ADP had created it" and planned to delete it.

    Fact 2 catches the other direction: a namespace ADP genuinely created, then
    deleted and recreated by somebody else, has the recorded NAME but a new uid, so it
    is adopted and preserved.
    """
    recorded = state.namespace
    if recorded is None:
        return ADOPTED
    if not recorded.created_by_bootstrap:
        return ADOPTED
    if recorded.name != name:
        return ADOPTED
    if not observed_uid.strip() or recorded.uid != observed_uid:
        return ADOPTED
    return ADP_CREATED
