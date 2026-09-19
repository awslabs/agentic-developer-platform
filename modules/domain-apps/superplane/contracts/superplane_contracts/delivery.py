"""B's scoped trusted-delivery contract, as the provider executor consumes it.

Issue #5048 (U10), EPIC #4910. R8, A half, offline acceptances 2-7.

## The failure this replaces

The reviewed `KubernetesExternalSecretClient` in Superplane's `vault_sync.py` builds
an ExternalSecret manifest, never applies it, and returns `{"synced": True}`. Every
caller reading that flag believes a working credential reached a workload. Nothing
was delivered and nothing was checked. The design note (§6) names it directly: "do
not treat its `synced` flag as proof of working delivery. Replace/retire that broad
secret-replication pattern."

So this module and the executor in `delivery_executor.py` are deliberately shaped so
that *no* status flag can stand in for delivery. The executor's `DeliveryOutcome` has
no `synced` field, no `ok` and no `delivered` boolean — the same discipline
`ValidationReport` follows in `connections.py`, where collapsing independent readings
into one aggregate boolean was the conflation the acceptance forbade. What a caller
gets instead is whatever the **provider** reported when the credential was actually
used, plus an explicit marker saying whether that reading was live or mocked.

## Where authority comes from, and what is not a substitute

Credential authority is a **lease** issued by B's scoped trusted-delivery contract:
bound to one recipient executor, one credential, one workspace, one active
run/allocation/workspace operation, and an expiry. Two things are explicitly *not*
substitutes for it, and both are tempting because both are reachable today:

* **The vault's credential-management endpoints** (`POST`/`DELETE /auth/credentials`).
  They administer a *user's* stored credentials. No run binding, no expiry, no
  run-tied revocation. Reaching for them would let this unit pass its own checks by
  acquiring **more** privilege than the design permits.
* **A raw read of the stored secret value.** A credential obtained that way is one
  no revocation reaches — precisely what R8's isolation and rotation criteria exist
  to prevent.

Neither is reachable from here, and that is enforced by *absence*: this module and
`delivery_executor.py` import no HTTP client and no secrets client, so there is no
code path to remove and no check to accidentally delete. `tests/test_executor_delivery.py`
asserts the absence with an import-graph check rather than trusting this paragraph.

## What is mocked, and recorded as mocked

**B's scoped trusted-delivery contract does not exist in ADP.** `TrustedDeliveryChannel`
is therefore a `Protocol` — structural, so the test double satisfies exactly the
surface the executor uses and cannot drift into offering conveniences a real channel
would not. `TRUSTED_DELIVERY_IS_MOCKED` is True and every outcome carries the marker.

This follows `acceptance-split.md` rule 5 and the precedent set twice already in this
module: `vault_client.py` records `EXACT_BINDING_IS_MOCKED`, and U17a's
`provisioning_adapter.py` records its facade mock. The reason is the same each time —
a mock that returns plausible values with no marker is indistinguishable from a live
reading to whoever consumes it, and R8 acceptance 1 (a real read-only provider call
by the bound executor) stays open until a named account, credential label, spend
authorization and cleanup owner are supplied. None of those is invented here.

## Why the secret value has its own type

`SecretMaterial` wraps the value and refuses to render it: `__repr__`, `__str__`,
`__format__` return a placeholder and pickling raises. That is not defensive
decoration. The acceptance is that a credential "appears in no tool result", and the
realistic leak is not someone writing `print(secret)` — it is an f-string in a log
line, an exception message that interpolates its context, or a dict that gets
serialized into an agent transcript three frames up. A `str` cannot defend itself in
any of those. This type is the same choice `VaultCredential` makes by having no
`value` attribute at all, applied to the one place a value genuinely must exist.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .connections import CredentialReference
from .health import ContractViolation
from .provisioning import ResolvedPrincipal

# True while B's scoped trusted-delivery contract does not exist in ADP. Asserted by
# the tests, so flipping it without a real channel fails CI rather than silently
# upgrading a mocked delivery into a claimed guarantee.
TRUSTED_DELIVERY_IS_MOCKED = True

# The permission a delivery requires, pinned as a string rather than imported from
# `superplane_auth` for the reason `connections.py` gives: the two packages ship
# separately and a hard import would make this contract unusable without U9. The
# tests assert this against `Permission.PROVISION`, whose own docstring is "create or
# destroy capacity, **and obtain cluster credentials**" — so obtaining a credential
# for use is PROVISION, while registering or rotating one is RENEW_CREDENTIAL. A
# lease carrying the management permission is refused below: managing a credential is
# not authority to use it inside a run.
DELIVERY_PERMISSION = "workspace:provision"
CREDENTIAL_MANAGEMENT_PERMISSION = "workspace:renew_credential"

# What a lease expiry does *not* accomplish, surfaced in the refusal and in the
# outcome rather than documented here alone. The design note is explicit: "a delivery
# lease does not make a provider's long-lived key expire." An operator who believes
# disabling a connection contained a leaked key will skip the provider-side
# revocation that actually contains it.
REVOCATION_LIMITATION = (
    "A delivery lease bounds this executor's access, not the credential itself. A "
    "long-lived provider key already delivered to a running workload stays usable "
    "until it is revoked at the provider and existing sessions are terminated; "
    "expiring or disabling the lease here does not revoke it."
)

# The broad secret-replication pattern this unit retires. Kept as a constant so the
# retirement is a value the tests can assert, not a claim in a docstring: the
# acceptance is that the pattern is "replaced or retired, **not extended**", and a
# guard that can be asserted is the only form of that which survives a later edit.
EXTERNAL_SECRETS_REPLICATION_RETIRED = True
_EXTERNAL_SECRETS_REFUSAL = (
    "cluster-wide secret replication is retired: it copies a credential to a "
    "namespace-visible Secret with no run binding, no expiry and no run-tied "
    "revocation, and its success flag reported delivery it never performed. Deliver "
    "through a recipient-bound lease instead"
)

# Environment variable names that carry provider-*management* authority. These must
# not reach a training or inference container: that container runs tenant workload
# code, and a management key there can register, rotate or delete credentials for
# everything the key can see. Dataset and MLflow access use separate,
# least-privileged workload credentials (design note §6).
#
# Stated as a prefix family as well as an exact set, for the reason U9's
# `strip_identity_headers` gives: "the ones we thought of" is the failure mode.
_MANAGEMENT_ENV_KEYS: frozenset[str] = frozenset(
    {
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "adp_vault_token",
        "adp_service_token",
        "superplane_vault_token",
        "kubeconfig",
    }
)
_MANAGEMENT_ENV_PREFIXES: tuple[str, ...] = (
    "adp_vault_",
    "adp_admin_",
    "vault_",
    "secretsmanager_",
)


class DeliveryRefused(PermissionError):
    """The executor refused to deliver or to run. Carries a caller-safe reason.

    A `PermissionError` subclass, matching `ProvisioningRefused` in U17a's adapter and
    `AuthorizationDeniedError` in U9's policy, so a caller that handles authorization
    failures uniformly catches this too. Distinct from `ContractViolation`, which is a
    malformed shape rather than a well-formed request the executor will not run.
    """


class SecretMaterial:
    """A credential value that refuses to render itself. See the module docstring.

    Not a dataclass: a generated `__repr__` would print the value, which is the whole
    problem. Not a `str` subclass either — every string operation would then produce
    an unprotected `str`, so the protection would end at the first `+` or `%`.

    `reveal()` is the single accessor and it is named to be conspicuous at a call
    site and in review. There is exactly one caller of it in this package: the
    materialization below, which writes the value to an executor-only file.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ContractViolation("secret material must be a non-empty string")
        self._value = value

    def reveal(self) -> str:
        """Return the raw value. The only accessor, deliberately conspicuous."""
        return self._value

    def __repr__(self) -> str:
        return "SecretMaterial([REDACTED])"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        # Without this, `f"{material:>20}"` would fall through to `__format__` on
        # object, which calls `str()` — protected — but an explicit spec on a str
        # subclass would not have been. Overridden so no format spec can differ.
        return repr(self)

    def __reduce__(self) -> Any:
        # Pickling is how a value ends up in a queue message, a cache or a
        # cross-process tool result. Refused rather than supported: nothing in this
        # design needs to serialize a credential, so the capability is pure risk.
        raise ContractViolation("secret material cannot be serialized")

    def __eq__(self, other: object) -> bool:
        # Provided so tests can compare material without revealing it in an
        # assertion failure message. Deliberately not a hash: a hashable secret can
        # be a dict key, and dict keys get logged when the dict does.
        return isinstance(other, SecretMaterial) and self._value == other._value

    __hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ExecutorIdentity:
    """The registered identity of one provider executor.

    An executor is a *recipient*, which is why this exists separately from the
    principal an operation runs as: a lease is issued to this identity and to no
    other, so a second executor holding a copy of the lease is refused. Design note
    §6: "deliver only the selected credential through a recipient-bound, short-lived
    channel".
    """

    executor_id: str
    """Opaque registered id. Never parsed for meaning."""

    def __post_init__(self) -> None:
        if not isinstance(self.executor_id, str) or not self.executor_id.strip():
            raise ContractViolation("executor identity requires an executor_id")


@dataclass(frozen=True)
class RunBinding:
    """A server-held record that one active run, allocation or workspace operation
    authorized a credential delivery.

    The analogue of U17a's `OperationBinding`, for delivery rather than provisioning.
    Separate because delivery is bound to whichever of the three units of work is
    active — the design note names all three ("an active run, allocation or workspace
    operation") — and because the required permission differs.

    **Constructing this object is not proof of provenance.** It must come from B's
    contract, exactly as `superplane_auth`'s README says of its grant objects and as
    U17a says of its binding. No offline object can verify that, which is why R8's
    live criterion stays open.
    """

    operation_id: str
    """B's identifier for the active run/allocation/operation. The revocation handle."""

    provider: str
    """The provider B authorized for this operation (for example, ``aws``)."""

    provider_account_id: str
    """The provider account B bound to both the credential and the operation."""

    operation: str
    """The exact provider action B authorized; not a caller-selected action."""

    principal: ResolvedPrincipal
    """Server-resolved. Never assembled from anything the caller sent."""

    recipient: ExecutorIdentity
    """The one executor this authority was issued to."""

    permission: str
    """The permission B authorized. Must be `DELIVERY_PERMISSION`."""

    expires_at: datetime
    """When the authority lapses.

    Required, unlike U17a's optional binding expiry. That divergence is deliberate:
    U17a could not invent an expiry B had published no semantics for, but R8's
    acceptance is a *short-lived* channel, and an unbounded credential delivery is
    the failure being fixed rather than a case to tolerate. A caller with no expiry to
    supply has no delivery authority to express.
    """

    def __post_init__(self) -> None:
        if not isinstance(self.principal, ResolvedPrincipal):
            raise ContractViolation("run binding must carry a resolved principal")
        if not isinstance(self.recipient, ExecutorIdentity):
            raise ContractViolation("run binding must name its recipient executor")
        if not self.operation_id or not self.operation_id.strip():
            # An empty operation_id would make every delivery and every revocation
            # bind to the same empty target — the defect U17a's binding rejects for
            # the same reason.
            raise ContractViolation("run binding must carry an operation_id")
        for name in ("provider", "provider_account_id", "operation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"run binding must carry {name}")
        if self.permission == CREDENTIAL_MANAGEMENT_PERMISSION:
            # Named explicitly rather than falling through to the generic refusal,
            # because this is the confusion worth being loud about: authority to
            # register or rotate a credential is not authority to use it inside a
            # run, and a binding carrying it must not be mistaken for a delivery.
            raise ContractViolation(
                "credential-management permission does not authorize delivery: "
                f"delivery requires {DELIVERY_PERMISSION!r}"
            )
        if self.permission != DELIVERY_PERMISSION:
            raise ContractViolation(
                f"delivery requires {DELIVERY_PERMISSION!r}; "
                f"binding carries {self.permission!r}"
            )
        if self.expires_at.tzinfo is None:
            raise ContractViolation("run binding expires_at must be timezone-aware")

    def is_expired(self, now: datetime) -> bool:
        """True when this authority has lapsed as of the caller's `now`."""
        if now.tzinfo is None:
            raise ContractViolation("now must be timezone-aware")
        return now >= self.expires_at


@dataclass(frozen=True)
class DeliveryLease:
    """B's scoped trusted-delivery contract: one credential, one recipient, one run.

    Every scope this carries is a scope the executor re-checks. The pairing is
    one-credential-to-one-lease for the reason `WorkspaceBinding` is one-to-one in
    `connections.py`: a lease covering "the credentials this run needs" cannot be
    expressed by this type, deliberately, because that shape is what turns one
    delegation into all of them.

    `provenance` records how the lease was obtained. It is a required field rather
    than an optional marker so a consumer cannot receive the lease without also
    receiving the statement that it is mocked.
    """

    lease_id: str
    reference: CredentialReference
    workspace_id: str
    binding: RunBinding
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("lease_id", "workspace_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"DeliveryLease.{name} is required")
        if not isinstance(self.reference, CredentialReference):
            raise ContractViolation(
                "DeliveryLease.reference must be a CredentialReference"
            )
        if not isinstance(self.binding, RunBinding):
            raise ContractViolation("DeliveryLease.binding must be a RunBinding")
        if self.binding.principal.workspace_id != self.workspace_id:
            # The lease's workspace and the bound principal's workspace must be the
            # same workspace. A mismatch would mean the thing authorized and the
            # thing acted on are different tenants — the cross-workspace case the
            # acceptance requires be refused, refused here at construction so it
            # cannot be assembled at all.
            raise ContractViolation(
                "DeliveryLease workspace does not match the bound principal's workspace"
            )
        if self.reference.service != self.binding.provider:
            raise ContractViolation(
                "DeliveryLease credential provider does not match the bound operation"
            )

    @property
    def recipient(self) -> ExecutorIdentity:
        """The one executor this lease was issued to."""
        return self.binding.recipient

    @property
    def is_mocked(self) -> bool:
        """True when this lease did not come from a live trusted-delivery contract."""
        return self.provenance.get("trusted_delivery") != "live"


@dataclass(frozen=True)
class RevocationState:
    """Whether a lease's credential still admits work, as B reports it.

    `limitation` is populated whenever the credential has been disabled or revoked,
    and it is a field rather than a docstring because R8's acceptance is that the
    limitation is *surfaced*. `connections.py` makes the same choice with
    `DISABLEMENT_LIMITATION`, and the reason is identical: an operator who believes
    disablement was containment does not perform the provider-side revocation that is.
    """

    admits_work: bool
    limitation: str = ""

    def __post_init__(self) -> None:
        if type(self.admits_work) is not bool:
            raise ContractViolation("RevocationState.admits_work must be a boolean")
        if not self.admits_work and not self.limitation:
            raise ContractViolation(
                "a credential that no longer admits work must surface its revocation "
                "limitation"
            )


@runtime_checkable
class TrustedDeliveryChannel(Protocol):
    """B's scoped trusted-delivery contract, as this executor consumes it.

    A `Protocol` rather than a base class, for the reason U17a's `OperationFacade`
    gives: B owns the implementation and it does not exist yet, so there is nothing
    to inherit from, and structural typing means the test double provides exactly
    this surface and no conveniences a real channel would not.

    Note what is absent. There is no `read_credential(credential_id)`, no
    `register_credential` and no `delete_credential` — the executor cannot ask this
    channel for a credential it does not hold a lease for, and cannot administer one
    at all. The vault's management endpoints are unreachable from this surface by
    construction, not by a check.
    """

    def revocation_state(self, lease: DeliveryLease) -> RevocationState:
        """Whether this lease's credential still admits work, per B's record."""
        ...

    def fetch_material(self, lease: DeliveryLease) -> SecretMaterial:
        """Hand over the leased credential's value, for this lease only.

        The one call that moves secret material, and it takes the whole lease rather
        than a credential id: a channel that accepted an id would be an endpoint for
        reading any credential, which is the raw-read path the design forbids.
        """
        ...

    def record_delivered(self, lease: DeliveryLease) -> None:
        """Audit that the material was delivered to the lease's recipient."""
        ...


@runtime_checkable
class ProviderOperation(Protocol):
    """A permitted provider operation, performed with a materialized credential."""

    provider: str
    provider_account_id: str
    operation: str

    def perform(
        self, credential_path: Path, *, lease: DeliveryLease
    ) -> Mapping[str, Any]:
        """Use the credential at `credential_path` and return what the provider said.

        The return value is the provider's **observation** — what came back from the
        provider when the credential was actually used. It is what makes delivery
        checkable, and it is why this returns a mapping rather than a bool: a boolean
        return would be a status flag, and a status flag is the thing being replaced.
        """
        ...


@dataclass(frozen=True)
class IsolationRoot:
    """Per-tenant/workspace/provider filesystem scope for SDK state.

    Design note §6: "isolate SDK configuration, credentials, caches and backend state
    by tenant/workspace/provider account. Do not share one SkyPilot home across
    tenants." A shared SkyPilot home is the concrete version of that failure — its
    `~/.sky` holds cluster state and credentials, so two tenants sharing it means one
    tenant's `sky status` enumerates the other's clusters and one tenant's credential
    file is readable by the next job on the host.

    The scope quadruple is part of every derived path, so there is no way to ask this
    object for a path that is not tenant-scoped. That is the enforcement — a
    `base` plus a convention that callers append a tenant id is the shape that ships
    one call site which forgot to.
    """

    base: Path
    org_id: str
    workspace_id: str
    provider: str
    provider_account_id: str

    def __post_init__(self) -> None:
        for name in ("org_id", "workspace_id", "provider", "provider_account_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"IsolationRoot.{name} is required")
            if "/" in value or value in {".", ".."} or "\\" in value:
                # A scope component becomes a path segment. A traversal in one would
                # let a tenant id of `../other-tenant` resolve into a sibling's
                # directory, which defeats every isolation property below.
                raise ContractViolation(
                    f"IsolationRoot.{name} cannot contain a path separator"
                )

    @property
    def scope_path(self) -> Path:
        """The one directory this scope owns. Every other path derives from it."""
        return (
            Path(self.base)
            / self.org_id
            / self.workspace_id
            / self.provider
            / self.provider_account_id
        )

    @property
    def sdk_home(self) -> Path:
        """The SDK/SkyPilot home for this scope. Never shared across tenants."""
        return self.scope_path / "home"

    @property
    def credentials_dir(self) -> Path:
        """Where a materialized credential lands, for this scope only."""
        return self.scope_path / "credentials"

    @property
    def cache_dir(self) -> Path:
        """SDK cache for this scope. Separate because caches hold resolved state."""
        return self.scope_path / "cache"

    @property
    def backend_state_dir(self) -> Path:
        """Backend/cluster state for this scope, e.g. SkyPilot's cluster database."""
        return self.scope_path / "state"

    def sdk_environment(self) -> dict[str, str]:
        """Environment for an SDK invocation inside this scope.

        Returns per-scope values for every variable that would otherwise default to a
        shared location. `SKYPILOT_DIR` and `HOME` are both set: SkyPilot resolves
        `~/.sky` from `HOME`, so setting only the former leaves a code path that
        writes to a shared home.
        """
        return {
            "HOME": str(self.sdk_home),
            "SKYPILOT_DIR": str(self.sdk_home / ".sky"),
            "SKYPILOT_STATE_DIR": str(self.backend_state_dir),
            "XDG_CACHE_HOME": str(self.cache_dir),
            "XDG_CONFIG_HOME": str(self.sdk_home / ".config"),
        }

    def shares_state_with(self, other: IsolationRoot) -> bool:
        """True when two scopes would read or write each other's SDK state.

        Used by the tests to assert isolation as a property of any two distinct
        scopes rather than of the two the tests happened to construct.
        """
        mine = self.scope_path.resolve()
        theirs = other.scope_path.resolve()
        return mine == theirs or mine in theirs.parents or theirs in mine.parents


@contextmanager
def restricted_materialization(
    material: SecretMaterial, *, root: IsolationRoot, filename: str
) -> Iterator[Path]:
    """Write `material` to an executor-only file, and remove it afterwards.

    Three properties, each an acceptance rather than hygiene:

    * **Executor-only.** The directory is created `0o700` and the file `0o600`, and
      the file is opened with `O_EXCL` so an attacker-pre-created path is a failure
      rather than a write into a file someone else can read.
    * **Removed after use.** The `finally` unlinks the file and does not depend on
      the caller remembering. A credential left behind is readable by the next tenant
      scheduled on the host, which is the concrete "left on a shared filesystem"
      failure in the story's blast-radius table.
    * **Inside the tenant's isolation scope**, so no shared credential directory
      exists to leave it in.

    `tempfile.mkdtemp` under the scope's credentials directory rather than the
    system temp root: the system root is shared, and a tmpfs-backed scope directory
    (an `emptyDir` with `medium: Memory`) is what makes this in-memory in a cluster.
    The path is returned rather than the content so the provider operation reads the
    file the SDK expects, which is the case this whole path exists for.
    """
    if not isinstance(material, SecretMaterial):
        raise ContractViolation("materialization requires SecretMaterial")
    if not filename or "/" in filename or filename in {".", ".."} or "\\" in filename:
        raise ContractViolation("materialization filename must be a bare filename")

    root.credentials_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is subject to umask, and an inherited umask of 0 would leave the
    # directory world-readable. Set it explicitly afterwards so the permission is the
    # one asked for rather than the one the process happened to inherit.
    root.credentials_dir.chmod(0o700)
    scratch = Path(tempfile.mkdtemp(dir=root.credentials_dir))
    scratch.chmod(0o700)
    target = scratch / filename
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(material.reveal())
        yield target
    finally:
        # Provider SDKs sometimes tighten the mode of the directory holding their
        # config. Restore owner access before unlinking: without this, a successful
        # operation that chmods the scratch directory read-only leaves the credential
        # behind and turns its result into a cleanup PermissionError.
        try:
            root.credentials_dir.chmod(0o700)
            # The provider may consume the material by removing the entire scratch
            # directory. A missing path is already-clean, not a cleanup failure.
            with suppress(FileNotFoundError):
                scratch.chmod(0o700)
            # missing_ok: a provider operation that consumed and removed the file
            # itself must not turn cleanup into a second failure.
            target.unlink(missing_ok=True)
        finally:
            # A provider SDK may create cache/config siblings next to the credential.
            # Remove the whole per-invocation directory without allowing non-secret
            # sibling cleanup to replace the provider's original result or exception.
            shutil.rmtree(scratch, ignore_errors=True)


def file_is_executor_only(path: Path) -> bool:
    """True when `path` is readable by its owner alone.

    Exists so the acceptance is checked against the filesystem rather than against
    the constant passed to `os.open` — a mode argument is a request, and umask,
    ACLs and a pre-existing file can all make the result differ from the ask.
    """
    mode = path.stat().st_mode
    return not mode & (stat.S_IRWXG | stat.S_IRWXO)


def management_credentials_in(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Provider-management variables present in a workload environment.

    Returns the offending names so a refusal can list them; empty means the
    environment carries no management authority. Case-insensitive, because
    `AWS_SECRET_ACCESS_KEY` and `aws_secret_access_key` are the same key to every
    SDK that reads it and a case-sensitive check is a bypass with an obvious recipe.

    Used to keep management keys out of training and inference containers, which run
    tenant workload code and need only the least-privileged dataset and MLflow
    credentials.
    """
    offending: list[str] = []
    for key in environment:
        lowered = key.strip().lower()
        if lowered in _MANAGEMENT_ENV_KEYS or any(
            lowered.startswith(prefix) for prefix in _MANAGEMENT_ENV_PREFIXES
        ):
            offending.append(key)
    return tuple(offending)


def assert_workload_environment(environment: Mapping[str, str]) -> None:
    """Raise when a training/inference container environment carries management keys.

    A refusal rather than a filter, for the reason `secrets.py` gives about inbound
    payloads: a filtered-and-accepted environment starts the container, so the
    operator believes the workload is running under least privilege when the code
    that built the environment is still wrong and the next variable it adds will not
    be on the list.
    """
    offending = management_credentials_in(environment)
    if offending:
        raise ContractViolation(
            "training/inference containers may not carry provider-management "
            f"credentials; remove: {', '.join(sorted(offending))}. Dataset and MLflow "
            "access use separate least-privileged workload credentials"
        )


def refuse_external_secret_replication(*_args: Any, **_kwargs: Any) -> None:
    """Always raise. The broad ExternalSecrets replication pattern is retired.

    Present as a function rather than a deleted file because the pattern's real
    problem was that calling it *looked like it worked*: it returned
    `{"synced": True}` having applied nothing. A caller that still reaches for
    cluster-wide replication gets a refusal naming the replacement, instead of
    reintroducing a path whose success value means nothing.

    This is the "replaced or retired, **not extended**" half of the acceptance: there
    is no argument to this function that produces a manifest, so the pattern cannot
    be extended through it.
    """
    raise ContractViolation(_EXTERNAL_SECRETS_REFUSAL)
