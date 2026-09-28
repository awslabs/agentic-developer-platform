"""Gate 4: register the workspace target, only after actual readiness.

Issue #5533 (w6-10), EPIC #4910. Design item 2: "Register a non-empty immutable
workspace target/context and ADP credential reference only after actual readiness.
Do not copy raw kubeconfigs/provider keys into domain records or issue artifacts."

## What a registration means to everything downstream

A registered workspace is the domain's statement that work may be scheduled here.
`src/superplane-api/app/installation_bootstrap.py` writes `Cluster` and `Workspace`
rows with `status="Ready"`, and once that row exists, nothing downstream re-derives
whether the cluster is actually usable — it reads the record. So registering before
readiness does not produce a warning; it produces a workspace that accepts work and
drops it. That is the exact defect AC-01 names as "test cluster ready but bootstrap
failed": the cluster's own status is ACTIVE and unhelpful, because the thing that is
not ready is the bootstrap, not the cluster.

Hence `register_workspace` takes evidence, not flags. It cannot be called in a way
that asserts readiness it did not establish, because the only way to get an
`IsolationEvidence` whose `may_clear_taint` is true is to have run the proofs.

## Why the record holds a credential REFERENCE and never a credential

Design item 2 forbids copying raw kubeconfigs or provider keys into domain records
or issue artifacts. The mechanism is `superplane_contracts.connections.CredentialReference`,
which refuses an ARN at construction and runs `assert_no_secret_material` — so the
refusal happens when the reference is built, not when somebody reviews the diff.
Two independent reasons this is structural rather than a convention here:

1. There is no field on `WorkspaceTarget` typed to hold a secret, and every field is
   re-screened by `assert_no_secret_material` before the record is written — except
   `cluster_arn`, which the contract's screen would reject because it treats all ARNs
   as secret-shaped while Terraform publishes this one as a public output. That
   exemption is by explicit field name and is documented at `_ARN_BEARING_FIELDS`.
2. The `ClusterAccess` seam (see `access.py`) has no method that returns a
   kubeconfig or a token, so this package could not obtain one to copy.

The registration record is also what ends up quoted in issue comments and completion
reports, which is precisely the artifact design item 2 is about.

## Replay safety: reconcile, never overwrite, never duplicate

AC-01 names "partial registration" and "replayed registration" as distinct cases.
They need opposite handling:

- A **replay** — the same operation re-delivered, same target, same identity — must
  be a no-op that returns the existing record. Writing again would either duplicate
  rows or bump a record that downstream consumers treat as immutable.
- A **conflict** — an existing record for this workspace naming a DIFFERENT cluster
  or organization — must refuse. `installation_bootstrap.py` sets this precedent
  with "workspace binding already differs" / "cluster binding already differs", and
  refuses "legacy adoption or organization rebinding" outright. Rebinding a
  workspace to a new cluster silently re-points every tenant's work.

Distinguishing them is a field-by-field comparison of the immutable identity, which
is why `_IMMUTABLE_FIELDS` is explicit rather than implied by dataclass equality:
equality would also compare fields that legitimately vary between runs, turning
every replay into a conflict.

## Why the conflict check moved in front of the mutation (F5)

The first revision did the whole of the above — read the existing record, refuse a
rebinding, write — as one step called AFTER the bootstrap taint had been removed.
Review finding F5: on a conflict it returned failure with `taint_cleared=True` and did
not restore the taint, so a workspace that was refused a rebinding was left with
schedulable nodes; a store exception in the same place escaped identically; and a
process interrupted in that window left the same state with nothing recording it.

The refusal itself was correct. Its *position* was not. A check that can refuse must
run while refusing is still free, and after the taint is gone it no longer is.

So registration is now two calls:

- `reserve_registration` runs FIRST, before any cluster mutation, and does the
  conflict decision against the identity known at that point (workspace, org, account,
  region, cluster — everything except the namespace uid, which does not exist yet). A
  rebinding attempt is refused against a completely untouched cluster.
- `finalize_registration` runs LAST, after readiness is proved, and completes the
  record it already holds a claim on.

The namespace uid is deliberately not part of the reservation identity: it cannot be,
since the namespace is created later, and requiring it would make the reservation
impossible to take before the mutation it exists to guard. `finalize_registration`
re-checks the full identity including the uid, so nothing is checked less than before —
the expensive half simply happens after the cheap half has already refused the cases
it can.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Protocol

from .access import RegistrationStore
from .admission import IsolationEvidence
from .components import ComponentInstallation
from .errors import BootstrapRefused
from .membership import (
    REGISTRATION_FIELDS,
    SharedMembership,
    registration_membership,
    registration_fields,
)
from .target import VerifiedTarget

# The fields that identify WHICH cluster and tenant a workspace is bound to. A
# difference in any of these between an existing record and a new one is a rebinding,
# not a replay. Listed explicitly rather than derived from dataclass equality: see
# the module docstring.
_IMMUTABLE_FIELDS: tuple[str, ...] = (
    "workspace_id",
    "org_id",
    "account_id",
    "region",
    "cluster_name",
    "cluster_arn",
    "namespace",
    "namespace_uid",
    "cluster_placement",
    *REGISTRATION_FIELDS,
)

# The subset of the immutable identity that is known BEFORE the cluster is mutated,
# used for the pre-mutation reservation (F5).
#
# `namespace_uid` is absent because it does not exist yet — the namespace has not been
# created — and `namespace` is present because its NAME is chosen up front. That
# boundary is the whole reason the reservation can run first: everything that
# identifies which cluster and which tenant a workspace binds to is already known, so
# a rebinding attempt is detectable before anything has been created. The uid is the
# only field that requires the mutation, and `finalize_registration` checks it.
_RESERVATION_FIELDS: tuple[str, ...] = (
    "workspace_id",
    "org_id",
    "account_id",
    "region",
    "cluster_name",
    "cluster_arn",
    "namespace",
)


class SecretScreen(Protocol):
    """The contract's `assert_no_secret_material`, injected so it can be required.

    The signature matches `superplane_contracts.secrets.assert_no_secret_material`
    exactly — `(payload, *, what=...)` — so the tests pass the GENUINE function
    rather than an adapter. That matters: an adapter would let this package's idea of
    what counts as secret material drift from the contract's, and the drift would be
    invisible because the tests would be asserting against the adapter.

    A Protocol rather than a direct import so this package keeps its
    standard-library-only rule, and so a test can also prove the screen is actually
    consulted — a screen that is imported but never called is the failure mode a
    passing test would otherwise hide.
    """

    def __call__(self, payload: object, *, what: str = ...) -> None: ...


@dataclass(frozen=True)
class WorkspaceTarget:
    """The immutable, non-empty record of a usable workspace target.

    Core target fields are required and non-blank. Optional membership fields
    are omitted for historical records and otherwise form one complete binding.
    "Non-empty" is in the design item
    because the partial-registration failure mode is a record that exists with
    blank identity fields — downstream reads it as present and cannot tell it apart
    from a complete one.

    `credential_reference_id` is an opaque identifier resolved through the vault
    (#5528's operation-bound delivery), NOT a credential and not an ARN.
    """

    workspace_id: str
    org_id: str
    account_id: str
    region: str
    cluster_name: str
    cluster_arn: str
    endpoint: str
    namespace: str
    namespace_uid: str
    cluster_ownership: str
    credential_reference_id: str
    contract_version: str
    # Historical registrations omitted dedicated placement. Shared placement is
    # immutable; changing it requires its own separately authorized lifecycle.
    cluster_placement: str = "dedicated"
    # Omitted from historical dedicated records. Shared lifecycle reservations
    # carry these before the namespace UID exists and retain them at publication.
    membership_cluster_id: str = ""
    membership_request_id: str = ""
    membership_generation: str = ""

    def __post_init__(self) -> None:
        for spec in fields(self):
            value = getattr(self, spec.name)
            if spec.name in REGISTRATION_FIELDS and value == "":
                continue
            if not isinstance(value, str) or not value.strip():
                raise BootstrapRefused(
                    f"WorkspaceTarget.{spec.name} is required and must be non-blank; "
                    "a registration with a blank identity field is indistinguishable "
                    "downstream from a complete one"
                )

        registration_membership(
            {spec.name: getattr(self, spec.name) for spec in fields(self)}
        )

    @property
    def immutable_identity(self) -> tuple[tuple[str, str], ...]:
        """The identity a replay must match exactly."""
        return tuple((name, getattr(self, name)) for name in _IMMUTABLE_FIELDS)


@dataclass(frozen=True)
class WorkspaceRegistration:
    """The outcome of registering — and whether this call actually wrote.

    `replayed` is surfaced rather than hidden so a caller can report "already
    registered" honestly instead of claiming it performed a write it skipped.
    """

    target: WorkspaceTarget
    replayed: bool


# The two fields that legitimately hold an ARN, and the only fields exempt from the
# secret screen.
#
# `superplane_contracts.secrets` treats EVERY ARN as secret-shaped, and that is right
# for its own job: it guards credential payloads, where an ARN is a complete-enough
# pointer that leaking it is a disclosure on its own — which is why
# `CredentialReference` refuses one outright.
#
# But `cluster_arn` is a PUBLIC identifier that `../infra/workspaces/outputs.tf`
# publishes as an unmarked output, precisely so a consumer can reach the cluster
# "without being told anything out-of-band", and a workspace target that could not
# name its own cluster would be useless. So the exemption is by explicit field name,
# not by relaxing the pattern:
#
# - Named fields, so adding a field does not silently inherit the exemption. A new
#   field is screened by default and a reviewer has to come here to change that.
# - Only this one, so a credential arriving through `endpoint`, `namespace` or any
#   other field is still refused.
# - The bootstrapping identity's `principal_arn` is deliberately NOT a field on
#   `WorkspaceTarget` at all. The record names the cluster, not who installed it.
#
# `test_registration.py` asserts both directions: the cluster ARN is accepted, and a
# private key in any other field — including the credential reference — is refused.
_ARN_BEARING_FIELDS: frozenset[str] = frozenset({"cluster_arn"})


def _screen_record(target: WorkspaceTarget, screen: SecretScreen) -> None:
    """Screen the record for secret material before writing.

    `CredentialReference` already screens its own fields at construction. This is the
    second, independent check on the object that actually gets persisted and quoted
    in reports — the one that catches a credential arriving through a field that is
    not the credential reference, which is the only way it could arrive.

    Every field is screened except the explicitly named ARN-bearing ones above.
    """
    for spec in fields(target):
        if spec.name in _ARN_BEARING_FIELDS:
            continue
        screen(getattr(target, spec.name), what=f"WorkspaceTarget.{spec.name}")


def _refuse_conflict(existing: object, proposed: WorkspaceTarget) -> None:
    """Refuse a rebinding; permit an exact replay.

    Reads the existing record by attribute so the store may return its own row type
    rather than a `WorkspaceTarget` — the production store is a database row.
    A missing attribute is a conflict, not a match: a record that cannot be compared
    has not been shown to be the same record.
    """
    divergent: list[str] = []
    for name, value in proposed.immutable_identity:
        observed = getattr(
            existing, name, "dedicated" if name == "cluster_placement" else None
        )
        if name == "cluster_placement" and observed in (None, ""):
            observed = "dedicated"
        if name in REGISTRATION_FIELDS and observed is None:
            observed = ""
        if observed != value:
            divergent.append(name)
    if divergent:
        raise BootstrapRefused(
            "a registration already exists for this workspace and binds it "
            f"differently ({', '.join(divergent)} differ); refusing to rebind. "
            "Rebinding a registered workspace silently re-points every tenant's "
            "work at a different cluster"
        )


@dataclass(frozen=True)
class RegistrationReservation:
    """An exclusive pre-mutation claim on one workspace.

    Held between `reserve_registration` and `finalize_registration`. Carries the
    identity it was taken for so finalization can prove it is completing the same
    claim rather than a different one — a reservation for workspace A finalized with
    workspace B's target would be the rebinding the reservation exists to prevent,
    arriving through the mechanism meant to stop it.

    `replayed` marks an ALREADY COMPLETED registration being re-run, which is the
    idempotent case and must not be refused.

    ## F10: `attempt_token` is what makes the claim exclusive rather than merely recorded

    The token the store issued to this attempt, required by `finalize` and `release` so a
    second attempt cannot complete or drop a claim it does not hold. See
    `access.RegistrationStore.reserve`.

    It is empty exactly when `replayed` is true, and `__post_init__` pins both directions.
    A replay of a COMPLETED registration has nothing left to finalize or release, so there
    is nothing to authorize and no token to issue — while a live claim with no token would
    be a claim nothing could prove it held, which is the F10 defect restated.

    **`repr` is disabled for this field.** `BootstrapOutcome` carries the reservation, and
    `cli.py` builds its report from that outcome; a `repr` containing the token would put
    it one careless f-string away from stdout. The token is not a credential — it grants
    nothing outside this one workspace's reservation row — but it IS an authorization
    secret, and `errors.py` already establishes that this package does not print those.
    """

    workspace_id: str
    identity: tuple[tuple[str, str], ...]
    replayed: bool = False
    attempt_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise BootstrapRefused("RegistrationReservation.workspace_id is required")
        if not self.identity:
            raise BootstrapRefused(
                "RegistrationReservation.identity is required; a claim that records no "
                "identity cannot be checked against the target that finalizes it"
            )
        held = bool(str(self.attempt_token or "").strip())
        if not self.replayed and not held:
            raise BootstrapRefused(
                "RegistrationReservation.attempt_token is required for a claim this "
                "attempt holds; a reservation nothing can prove it holds cannot fence a "
                "concurrent attempt out of finalizing or releasing it"
            )
        if self.replayed and held:
            raise BootstrapRefused(
                "RegistrationReservation.attempt_token must be empty for a replay of a "
                "completed registration; there is no claim left to authorize, and a "
                "token here would suggest one is held"
            )


def _reservation_identity(
    target: VerifiedTarget, namespace: str, membership=None
) -> tuple[tuple[str, str], ...]:
    """The pre-mutation identity, read from the verified target and nothing else.

    Every value comes from `VerifiedTarget` — which `target.py` built from the bound
    operation's principal and authoritative provider reads — plus the namespace NAME
    the caller asked for. No caller-supplied identity field is trusted here.
    """
    values = {
        "workspace_id": target.workspace_id,
        "org_id": target.org_id,
        "account_id": target.account_id,
        "region": target.region,
        "cluster_name": target.cluster_name,
        "cluster_arn": target.cluster_arn,
        "namespace": namespace,
    }
    missing = [name for name in _RESERVATION_FIELDS if not values[name].strip()]
    if missing:
        raise BootstrapRefused(
            "cannot reserve a registration with blank identity field(s): "
            + ", ".join(sorted(missing))
        )
    if membership is not None:
        if not isinstance(membership, SharedMembership):
            raise BootstrapRefused(
                "shared bootstrap requires its approved membership document"
            )
        membership = SharedMembership.read(membership.encode())
        if (
            membership.workspace_id,
            membership.org_id,
            membership.cluster_arn,
            membership.endpoint,
            membership.namespace,
        ) != (
            target.workspace_id,
            target.org_id,
            target.cluster_arn,
            target.endpoint,
            namespace,
        ):
            raise BootstrapRefused("approved membership names another bootstrap target")
        values.update(registration_fields(membership))
    return tuple(values.items())


def reserve_registration(
    *,
    store: RegistrationStore,
    target: VerifiedTarget,
    namespace: str,
    membership=None,
) -> RegistrationReservation:
    """Claim this workspace BEFORE any cluster mutation. Refuses a rebinding here.

    **This is the F5 fix.** It runs first in the gate sequence, so a workspace already
    bound to a different cluster is refused while the cluster is still untouched and
    the bootstrap taint is still in place. Nothing has to be undone.

    The claim must be atomic in the store: two concurrent attempts on one workspace
    must yield one reservation and one refusal. The production implementation is the
    `pg_advisory_xact_lock` + `SELECT ... FOR UPDATE` pattern
    `app/installation_bootstrap.py` already uses, which is exactly that.

    F10: atomicity is necessary and not sufficient. The lock is transaction-scoped, so it
    is gone once `reserve` commits and every step it guards runs afterwards. The store must
    also issue an `attempt_token` that identifies the holder for the rest of the sequence,
    and this function refuses a `reserved: True` that carries none — an unfenced claim is
    one a concurrent attempt can finalize or release out from under this one.
    """
    identity = _reservation_identity(target, namespace, membership)
    outcome = store.reserve(target.workspace_id, dict(identity))

    if not isinstance(outcome, Mapping) or "reserved" not in outcome:
        raise BootstrapRefused(
            "the registration store did not report whether the workspace was "
            "reserved; an unanswered reservation is not a held one, and proceeding "
            "would mutate a cluster this bootstrap has no claim on"
        )
    if not outcome["reserved"]:
        conflict = str(outcome.get("conflict", "")).strip()
        raise BootstrapRefused(
            "this workspace is already claimed"
            + (f" ({conflict})" if conflict else " and binds differently")
            + "; refusing to proceed. Rebinding a registered workspace silently "
            "re-points every tenant's work at a different cluster, and mutating one a "
            "live attempt holds corrupts both attempts. Refused before any cluster "
            "mutation, so nothing needs to be undone"
        )

    replayed = bool(outcome.get("replayed", False))
    token = str(outcome.get("attempt_token", "") or "")
    if not replayed and not token.strip():
        # A store that reports a fresh claim and cannot say who holds it has given this
        # attempt no way to prove, eight steps later, that it is still the holder. Refused
        # here rather than at `finalize`, because by then the cluster has been mutated.
        raise BootstrapRefused(
            "the registration store reserved the workspace without issuing an attempt "
            "token; a claim that cannot identify its holder does not fence a concurrent "
            "bootstrap out of finalizing or releasing it, so proceeding would let two "
            "attempts mutate one workspace"
        )

    return RegistrationReservation(
        workspace_id=target.workspace_id,
        identity=identity,
        replayed=replayed,
        # Not carried for a replay: a completed registration has no claim to authorize,
        # and `RegistrationReservation` refuses the combination outright.
        attempt_token="" if replayed else token,
    )


def finalize_registration(
    *,
    store: RegistrationStore,
    reservation: RegistrationReservation,
    target: VerifiedTarget,
    installation: ComponentInstallation,
    evidence: IsolationEvidence,
    readiness: object,
    credential_reference_id: str,
    contract_version: str,
    screen: SecretScreen,
    membership=None,
) -> WorkspaceRegistration:
    """Complete the reserved registration, or refuse, or recognise a replay.

    Readiness is established from `evidence` and `readiness`, never asserted by the
    caller. The order matters: readiness is checked BEFORE the existing record is read,
    so a failed bootstrap cannot be turned into a successful registration by a replay
    of a previous run's record.

    `readiness` is the `RuntimeReadiness` from `readiness.py`, taken as `object` and
    duck-typed on `usable`/`failures` so this module does not import that one — the
    same reason `target.py` duck-types the operation binding. It is REQUIRED: the F2
    finding was that a workspace could be registered with no controller and no DNS, and
    the fix is only real if the thing that writes the record refuses without it.
    """
    if reservation.workspace_id != target.workspace_id:
        raise BootstrapRefused(
            f"the reservation is held for workspace {reservation.workspace_id!r} but "
            f"the target names {target.workspace_id!r}; finalizing would complete one "
            "workspace's claim with another's identity"
        )
    reserved = dict(reservation.identity)
    proposed_identity = dict(
        _reservation_identity(target, installation.namespace, membership)
    )
    divergent_claim = sorted(
        name
        for name in set(reserved) | set(proposed_identity)
        if reserved.get(name) != proposed_identity.get(name)
    )
    if divergent_claim:
        raise BootstrapRefused(
            "the identity being registered differs from the one reserved before "
            f"mutation ({', '.join(divergent_claim)} differ); the reservation did not "
            "guard the binding that is now being written"
        )

    usable = getattr(readiness, "usable", None)
    if usable is None:
        raise BootstrapRefused(
            "no runtime readiness was supplied to finalize the registration; a "
            "workspace whose controller and system services were never verified would "
            "accept work it cannot run"
        )
    if not usable:
        failures = getattr(readiness, "failures", ()) or ("no readiness checks ran",)
        raise BootstrapRefused(
            "the workspace runtime is not usable ("
            + "; ".join(str(entry) for entry in failures)
            + "); refusing to register it. The cluster being ACTIVE is not the same "
            "fact as its controller and system services being available"
        )

    if not evidence.may_clear_taint:
        raise BootstrapRefused(
            "isolation is not proved ("
            + (", ".join(evidence.unverified) or "no proofs ran")
            + "); refusing to register the workspace as usable. The cluster being "
            "ACTIVE is not the same fact as bootstrap having succeeded"
        )
    if evidence.namespace != installation.namespace:
        raise BootstrapRefused(
            f"isolation was proved for namespace {evidence.namespace!r} but the "
            f"installation established {installation.namespace!r}; the evidence does "
            "not describe the namespace being registered"
        )
    if installation.workspace_id != target.workspace_id:
        raise BootstrapRefused(
            "the component installation belongs to a different workspace than the "
            "verified target; refusing to register a mismatched pair"
        )
    if installation.cluster_arn != target.cluster_arn:
        raise BootstrapRefused(
            "the component installation was performed on a different cluster than "
            "the verified target"
        )

    proposed = WorkspaceTarget(
        workspace_id=target.workspace_id,
        org_id=target.org_id,
        account_id=target.account_id,
        region=target.region,
        cluster_name=target.cluster_name,
        cluster_arn=target.cluster_arn,
        endpoint=target.endpoint,
        namespace=installation.namespace,
        namespace_uid=installation.namespace_uid,
        cluster_ownership=target.cluster_ownership,
        credential_reference_id=credential_reference_id,
        contract_version=contract_version,
        **registration_fields(membership),
    )
    _screen_record(proposed, screen)

    existing = store.read(target.workspace_id)
    if existing is not None:
        _refuse_conflict(existing, proposed)
        # An exact match. Return the record already present without writing: the
        # record is immutable and downstream consumers treat it as such.
        return WorkspaceRegistration(target=proposed, replayed=True)

    # The one write. A failure here raises, and `workspace.py` restores the bootstrap
    # taint on the way out — the recovery F5 asked for. This is why `finalize` is the
    # last action in the sequence: everything that can refuse has already refused.
    #
    # F10: the token proves this attempt still holds the claim it took at step 3. The
    # store re-checks it under the lock, so an attempt that lost the reservation while it
    # was installing components is refused here instead of publishing over the holder.
    store.finalize(proposed, reservation.attempt_token)
    return WorkspaceRegistration(target=proposed, replayed=False)
