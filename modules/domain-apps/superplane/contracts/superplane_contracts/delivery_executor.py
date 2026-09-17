"""The provider executor: runs a provider operation under a delivery lease.

Issue #5048 (U10), EPIC #4910. R8, A half.

## The one design decision worth reading

The executor has **no public method that delivers a credential**.
`ProviderExecutor.run` requires a `DeliveryLease`, and the only thing that produces a
real lease is B's scoped trusted-delivery contract. So "invoked unbound" is not a
policy this executor enforces at run time and hopes callers respect — there is no
entry point that omits the lease.

That matters because the alternative shape is the common one and it fails quietly: an
executor with `deliver(credential_id)` plus a separate `authorize()` the caller is
expected to have called first. Every such pair eventually ships a call site that
forgot the second half, and the failure is invisible because delivery still *works* —
it just works without a lease, so the credential is one no revocation reaches. U17a's
`ProvisioningAdapter` makes the same choice for the same reason, and this module
follows it deliberately rather than inventing a second shape for the same problem.

## What this is, and what it is not

A narrowly bounded **trusted executor**, not a service:

* **No domain API.** There is no route here. The upstream API (U15) owns routes.
* **No domain-DB write.** There is no database handle on this class and no DB module
  in its import graph, so a domain write is not reachable rather than merely
  discouraged. Domain records are written by the upstream API
  (`repo-path-allocation.md`).
* **No vault credential-management call and no raw secret read.** There is no HTTP
  client and no secrets client in this module at all. `POST`/`DELETE /auth/credentials`
  administer a *user's* stored credentials — no run binding, no expiry, no run-tied
  revocation — and reaching for them would let this unit pass its checks by acquiring
  **more** privilege than the design permits. Held by absence, asserted structurally
  by `tests/test_executor_delivery.py`.
* **No standing credential.** Material is resolved through the lease's reference on
  every run and never cached on the instance. That is what makes rotation work
  through the reference and revocation actually bite: a cached value is a credential
  no rotation reaches.

## Why the outcome is the provider's reading

`DeliveryOutcome` carries what the provider returned when the credential was actually
used. It has no `synced`, no `ok` and no `delivered` boolean, because the thing being
replaced is exactly such a flag: `vault_sync.py`'s `KubernetesExternalSecretClient`
returns `{"synced": True}` having applied nothing.

Two weaker successes are also refused as evidence, and the story's blast-radius table
names both. A **Secret object existing in a cluster** shows something was written, not
that the credential is the exactly-bound one, reached an authorized recipient, or
works. And this executor's **own** claim about itself is not evidence either — which
is why the outcome is built from the provider's observation, and why `provenance`
travels with it saying whether that observation was live or mocked. While B's contract
is mocked, a passing run establishes the adapter's behavior and **not** live delivery.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .delivery import (
    REVOCATION_LIMITATION,
    TRUSTED_DELIVERY_IS_MOCKED,
    DeliveryLease,
    DeliveryRefused,
    ExecutorIdentity,
    IsolationRoot,
    ProviderOperation,
    RevocationState,
    SecretMaterial,
    TrustedDeliveryChannel,
    restricted_materialization,
)
from .emission import scrub
from .health import ContractViolation
from .provisioning import FORBIDDEN_PARAMETER_KEYS, FORBIDDEN_PARAMETER_PREFIXES

# The filename a materialized credential gets inside the executor-only directory.
# One fixed name: it is scoped by the per-tenant directory around it, so a
# caller-supplied name would add a path-injection surface for no benefit.
CREDENTIAL_FILENAME = "credentials"

_FORBIDDEN_PROVIDER_SCOPE_KEYS = frozenset(
    {
        "account",
        "account_id",
        "provider_account",
        "provider_account_id",
        "project",
        "project_id",
        "subscription",
        "subscription_id",
    }
)


@dataclass(frozen=True)
class DeliveryRequest:
    """What the caller asks the executor to do. Carries no authority whatsoever.

    Note what is absent: no `user_id`, no `org_id`, no workspace the caller chose. The
    workspace and principal come from the lease's binding, so a caller cannot name
    the tenant it acts as.

    `parameters` exists because a provider operation genuinely needs shape (a region,
    an instance type) the caller does know. It is the one open-ended surface here, so
    it is also the obvious smuggling route for an identity field — hence the refusal
    in `_check_request`. Tuple-of-pairs rather than a dict, matching
    `ProvisioningIntent`: an immutable parameter set cannot be mutated between the
    authority check and the provider call.
    """

    provider: str
    operation: str
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("provider", "operation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"DeliveryRequest.{name} is required")
        if not isinstance(self.parameters, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
            or not pair[0].strip()
            for pair in self.parameters
        ):
            raise ContractViolation("parameters must be immutable string pairs")
        if len({key for key, _ in self.parameters}) != len(self.parameters):
            raise ContractViolation("duplicate delivery parameter")


def forbidden_request_parameters(request: DeliveryRequest) -> tuple[str, ...]:
    """Identity-asserting keys present in a request's parameters.

    Delegates to the same key family `provisioning.forbidden_parameters` uses, rather
    than restating the list. One list means a key added there is refused here too; two
    lists would drift, and the drift would be silent because each would still pass its
    own tests. Normalization (case, hyphens, underscores) matches that function's, for
    the reason its docstring gives: a case-sensitive check is a bypass with an obvious
    recipe.
    """
    offending: list[str] = []
    compact_forbidden = {
        name.replace("_", "")
        for name in FORBIDDEN_PARAMETER_KEYS | _FORBIDDEN_PROVIDER_SCOPE_KEYS
    }
    for key, _ in request.parameters:
        lowered = key.strip().lower().replace("-", "_")
        if lowered.replace("_", "") in compact_forbidden or any(
            lowered.startswith(prefix.replace("-", "_"))
            for prefix in FORBIDDEN_PARAMETER_PREFIXES
        ):
            offending.append(key)
    return tuple(offending)


@dataclass(frozen=True)
class DeliveryOutcome:
    """What the provider reported when the leased credential was actually used.

    No `synced`, no `ok`, no `delivered`. See the module docstring: such a flag is the
    thing being replaced. A caller wanting to know whether the operation worked reads
    `provider_observation`, which is the provider's own response, and reads
    `provenance` to know whether that response came from a live provider or a mock.

    `credential_present_in_result` is derived by checking the exact material against
    the provider observation before shape-based scrubbing. Construction is refused
    if it is ever true, so the acceptance is enforced rather than self-reported.
    """

    lease_id: str
    operation_id: str
    provider: str
    operation: str
    provider_observation: Mapping[str, Any]
    credential: InitVar[SecretMaterial]
    provenance: Mapping[str, str] = field(default_factory=dict)
    limitation: str = REVOCATION_LIMITATION
    credential_present_in_result: bool = field(init=False, repr=False)

    def __post_init__(self, credential: SecretMaterial) -> None:
        if not isinstance(self.provider_observation, Mapping):
            raise ContractViolation("provider observation must be a mapping")
        if not isinstance(credential, SecretMaterial):
            raise ContractViolation("delivery outcome requires the material leak check")
        credential_present = _contains_exact_material(
            self.provider_observation, credential.reveal()
        )
        object.__setattr__(self, "credential_present_in_result", credential_present)
        if credential_present:
            raise ContractViolation(
                "provider observation contains the delivered credential material"
            )
        # The provider's response is scrubbed on the way into the outcome, not on the
        # way out. An outcome that held unscrubbed material and cleaned it in a
        # getter would still be carrying the value in memory for anything that
        # reached the attribute directly, and `repr()` of this dataclass reaches it.
        object.__setattr__(
            self, "provider_observation", dict(scrub(dict(self.provider_observation)))
        )
        if not self.limitation:
            # The revocation limitation travels with every outcome, including the
            # successful ones. R8's acceptance is that the limitation is surfaced,
            # and an operator reading a success is exactly the operator about to
            # believe that expiring the lease contained the key.
            raise ContractViolation("a delivery outcome must surface its limitation")

    @property
    def is_mocked(self) -> bool:
        """True when the provider observation did not come from a live provider."""
        return self.provenance.get("trusted_delivery") != "live"

    def tool_result(self) -> dict[str, Any]:
        """A model-visible summary of this outcome.

        Exists so there is a supported way to report a delivery into an agent
        transcript, and so the acceptance can be tested against the thing a model
        would actually see. It names the lease and the operation and carries the
        provider's scrubbed observation. The exact delivered material was refused at
        construction, before this model-visible representation could be built.
        """
        return {
            "lease_id": self.lease_id,
            "operation_id": self.operation_id,
            "provider": self.provider,
            "operation": self.operation,
            "provider_observation": dict(self.provider_observation),
            "trusted_delivery": self.provenance.get("trusted_delivery", "mock"),
            "limitation": self.limitation,
        }


@dataclass(frozen=True)
class ProviderExecutor:
    """Runs a provider operation under a delivery lease, and only under one.

    Frozen, and holding no credential: there is no attribute on this class through
    which material could persist between runs, which is what makes rotation take
    effect through the reference and revocation actually bite. U17a's adapter is
    frozen for the adjacent reason — a mutable `self.state` is what a caller or a test
    reads instead of asking the authoritative source.

    `channel` is B's scoped trusted-delivery contract, injected and **mocked today**.
    `clock` is injected so every expiry branch is testable without patching time,
    matching U8's contracts and U17a's adapter.

    Note the absent fields: no database session, no HTTP client, no vault client. The
    three things this executor must never do are not reachable from here.
    """

    identity: ExecutorIdentity
    channel: TrustedDeliveryChannel
    isolation_base: Path
    clock: Callable[[], datetime]

    def run(
        self,
        lease: DeliveryLease,
        request: DeliveryRequest,
        operation: ProviderOperation,
    ) -> DeliveryOutcome:
        """Perform `request` under `lease`, or refuse.

        Order matters and is the same discipline as `authorize_request` in U9's policy
        and `run` in U17a's adapter: every authority check runs before any credential
        material is fetched or written, so a refusal cannot happen after a secret has
        already been materialized on disk.
        """
        self._check_lease(lease)
        self._check_request(lease, request)
        self._check_operation(lease, operation)
        revocation = self._check_revocation(lease)

        return self._execute(lease, request, operation, revocation)

    # ------------------------------------------------------------------
    # Authority checks
    # ------------------------------------------------------------------

    def _check_lease(self, lease: DeliveryLease) -> None:
        """Refuse anything that is not a currently-valid lease issued to *this* executor."""
        if lease is None:  # pragma: no cover - defensive; typing forbids it
            # Kept despite being unreachable through the annotated signature, because
            # "no lease" is THE failure this executor exists to prevent and Python
            # annotations do not enforce themselves. A caller passing None from
            # untyped code must be refused, not crash with an AttributeError that a
            # broad `except` upstream could swallow into a retry.
            raise DeliveryRefused("credential delivery requires a delivery lease")
        if not isinstance(lease, DeliveryLease):
            # A duck-typed stand-in carrying the right attribute names is not a lease.
            # This refuses the shape a caller would reach for to skip B's contract: a
            # small local object with a lease_id and a reference.
            raise DeliveryRefused(
                "delivery lease must be a DeliveryLease issued by the trusted-delivery "
                "contract"
            )
        if lease.recipient != self.identity:
            # The recipient-binding check. An executor holding a lease issued to a
            # different executor is refused even though the lease itself is valid:
            # "recipient-bound" is the property, and a lease that any executor could
            # redeem is a bearer token for the credential.
            raise DeliveryRefused("delivery lease was not issued to this executor")
        if lease.binding.recipient != self.identity:
            # Re-checked against the binding as well as the lease. `lease.recipient`
            # delegates to the binding today, so this is the same answer — asserted
            # here anyway for the reason `authorize_use` gives for taking the binding
            # separately: this function's contract is "the lease authorizes THIS
            # executor", and it should not depend on a property in another module
            # continuing to delegate.
            raise DeliveryRefused("delivery lease was not issued to this executor")

        now = self.clock()
        if lease.binding.is_expired(now):
            # An expired lease is refused, and the refusal states what expiry does
            # not do. An operator reading "expired" without that sentence concludes
            # the credential is contained, and skips the provider-side revocation
            # that would contain it.
            raise DeliveryRefused(
                f"delivery authorization has expired. {REVOCATION_LIMITATION}"
            )

    def _check_request(self, lease: DeliveryLease, request: DeliveryRequest) -> None:
        """Refuse a request that asserts an identity.

        The important case is the one that looks harmless: parameters carrying an
        `org_id` that **matches** the bound principal's org. It is still refused, and
        the reason is worth stating because "it matched, so no harm done" is the
        argument that removes this check.

        A caller-supplied identity that agrees with the binding today is a code path
        that reads the caller's value. Once that path exists, the only thing
        preventing a mismatched value from being honoured is that something else
        happens to compare them — and comparisons get reordered, cached, or made
        conditional. Refusing regardless means there is no path that reads
        caller-supplied identity at all, which is a property rather than a
        coincidence. Design §6 lines 398-407 require that body-supplied user/org IDs
        are never authority; the way to guarantee that is for the field to be a
        refusal.
        """
        if not isinstance(request, DeliveryRequest):
            raise DeliveryRefused("delivery request must be a DeliveryRequest")
        offending = forbidden_request_parameters(request)
        if offending:
            raise DeliveryRefused(
                "delivery parameters may not assert an identity; "
                f"remove: {', '.join(sorted(offending))}. "
                "The principal and workspace are resolved from the delivery lease."
            )
        if request.provider != lease.binding.provider:
            raise DeliveryRefused(
                "delivery request provider does not match the bound operation"
            )
        if request.operation != lease.binding.operation:
            raise DeliveryRefused(
                "delivery request action does not match the bound operation"
            )

    def _check_operation(
        self, lease: DeliveryLease, operation: ProviderOperation
    ) -> None:
        """Refuse an executable adapter that is not the operation B authorized."""
        if not isinstance(operation, ProviderOperation):
            raise DeliveryRefused(
                "provider operation must declare its provider, account and action"
            )
        actual = (
            operation.provider,
            operation.provider_account_id,
            operation.operation,
        )
        expected = (
            lease.binding.provider,
            lease.binding.provider_account_id,
            lease.binding.operation,
        )
        if actual != expected:
            raise DeliveryRefused(
                "provider operation does not match the bound provider, account and action"
            )

    def _check_revocation(self, lease: DeliveryLease) -> RevocationState:
        """Ask B whether this credential still admits work, and refuse if not.

        Read from the channel rather than from a status field on the lease: a lease is
        a value the caller is holding, and a credential disabled after the lease was
        issued would still look fine on it. The authoritative answer is B's, checked
        at the operation — which is what "holds no standing credential; authority is
        re-checked at the operation" means in practice.
        """
        state = self.channel.revocation_state(lease)
        if not isinstance(state, RevocationState):
            # A channel returning something else is a contract breach on B's side and
            # must surface here rather than being handed to a caller who would read
            # `.admits_work` off an arbitrary object — where a missing attribute
            # raises but a truthy one silently authorizes. Raised as a violation
            # rather than a refusal: nothing was denied, the report is malformed.
            raise ContractViolation("revocation report is not a RevocationState")
        if not state.admits_work:
            # The limitation is included in the refusal, not just logged. A disabled
            # credential blocks admissions here **and** the response says that an
            # already-delivered long-lived key stays usable until it is revoked at
            # the provider.
            raise DeliveryRefused(
                f"credential no longer admits work: {state.limitation}"
            )
        return state

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def isolation_root_for(self, lease: DeliveryLease) -> IsolationRoot:
        """The per-tenant/workspace/provider/account scope this lease's work runs in.

        Public so a consuming lane (U2's image, U3's rollout) can ask for the same
        scope the executor will use rather than deriving a parallel path convention
        that drifts from it. Every scope component comes from the lease's **binding**, not
        from the request: a caller-chosen scope directory would be a caller-chosen
        tenant boundary.
        """
        if not isinstance(lease, DeliveryLease):
            raise ContractViolation("an isolation scope is derived from a lease")
        return IsolationRoot(
            base=self.isolation_base,
            org_id=lease.binding.principal.org_id,
            workspace_id=lease.workspace_id,
            provider=lease.binding.provider,
            provider_account_id=lease.binding.provider_account_id,
        )

    def _execute(
        self,
        lease: DeliveryLease,
        request: DeliveryRequest,
        operation: ProviderOperation,
        revocation: RevocationState,
    ) -> DeliveryOutcome:
        """Materialize the leased credential, run the operation, then remove it."""
        root = self.isolation_root_for(lease)
        material = self.channel.fetch_material(lease)
        if not isinstance(material, SecretMaterial):
            # A channel handing back a bare `str` is refused. Not pedantry: a `str`
            # renders itself in every log line and exception message it reaches, so
            # accepting one would silently give up the property `SecretMaterial`
            # exists to hold, and the give-up would be invisible.
            raise ContractViolation(
                "trusted delivery must hand over SecretMaterial, not a bare value"
            )

        with restricted_materialization(
            material, root=root, filename=CREDENTIAL_FILENAME
        ) as credential_path:
            observation = operation.perform(credential_path, lease=lease)
            if not isinstance(observation, Mapping):
                raise ContractViolation(
                    "provider operation must return its observation as a mapping"
                )
            outcome = DeliveryOutcome(
                lease_id=lease.lease_id,
                operation_id=lease.binding.operation_id,
                provider=request.provider,
                operation=request.operation,
                provider_observation=observation,
                credential=material,
                provenance=self._provenance(lease),
                limitation=revocation.limitation or REVOCATION_LIMITATION,
            )
            # Audited inside the materialization window, so the audit records a
            # delivery that actually happened. Recording it before the operation
            # would audit an intent, and after cleanup would leave a successful
            # delivery unaudited if the process died during teardown.
            self.channel.record_delivered(lease)

        return outcome

    @staticmethod
    def _provenance(lease: DeliveryLease) -> dict[str, str]:
        """How this delivery was obtained, carried into the outcome.

        Composed from the lease's own provenance and from
        `TRUSTED_DELIVERY_IS_MOCKED`, so an outcome cannot be built without the
        marker. While the contract is mocked, `mock` wins even if a lease claims
        `live`: a caller-constructed lease asserting liveness is exactly the thing
        that would turn a mocked run into a claimed live acceptance.
        """
        claimed = lease.provenance.get("trusted_delivery", "mock")
        return {
            "trusted_delivery": "mock" if TRUSTED_DELIVERY_IS_MOCKED else claimed,
            "mechanism": "scoped lease from B's trusted-delivery contract",
        }


def _contains_exact_material(value: Any, material: str) -> bool:
    """Whether a provider observation contains the exact delivered credential.

    This check happens before shape-based scrubbing and covers mapping keys and
    values plus ordinary container types. It therefore proves the specific
    credential used for this operation is absent instead of assuming a redactor
    will recognize every provider-specific secret shape.
    """
    if isinstance(value, str):
        return material in value
    if isinstance(value, bytes):
        return material.encode() in value
    if isinstance(value, Mapping):
        return any(
            _contains_exact_material(key, material)
            or _contains_exact_material(item, material)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_exact_material(item, material) for item in value)
    return False


def summarize(outcome: DeliveryOutcome) -> str:
    """A one-line, caller-safe description of a delivery outcome.

    Provided so a consumer logging a delivery does not hand-roll a string — the
    hand-rolled one interpolates the outcome, and an interpolated log argument is
    where leaks happen (the reason U7's `emission.py` ships a logging filter). Names
    no parameters and no principal: a log line is the least controlled place a tenant
    identifier can end up.

    States the mock explicitly. A log line reading "delivery succeeded" with no
    qualifier is how a mocked run gets remembered as a live one.
    """
    qualifier = " (mocked trusted delivery — not live)" if outcome.is_mocked else ""
    return (
        f"operation {outcome.operation_id} ran {outcome.operation} on "
        f"{outcome.provider} under lease {outcome.lease_id}{qualifier}"
    )
