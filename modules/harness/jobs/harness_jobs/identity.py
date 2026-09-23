"""What an operation *is*, before anything durable holds one.

Issue #5525 (w6-02), EPIC #4910, Wave 6. Consumes the versioned contract published
by #5524 (w6-01): port ``operation_facade``, owner ``harness_jobs``.

## Why identity is a module of its own

The store (``store.py``) and the outbox (``outbox.py``) both need to say what an
operation is bound to, and they must say the *same* thing. If each derived the
binding from its own table's columns, the two would agree until the first change
nobody propagated -- and the symptom of that divergence is an admitted operation
whose outbox row names a different tenant, which is the cross-tenant defect this
wave exists to prevent.

So the binding is one frozen type, constructed once at the boundary where identity
is resolved, and passed down. Neither the store nor the outbox can compose one from
caller input, because neither is given caller input in a shape that would let it.

## The one rule this module enforces structurally

**A caller cannot name the tenant it acts for.** `OperationRequest` -- the caller's
half -- has no `org_id` and no `workspace_id` field, and additionally *rejects*
those keys if they arrive through the parameter map, which is the dict-shaped side
channel a shape that merely omits a field still accepts. `ResolvedPrincipal` -- the
authority's half -- is the only thing carrying them, and it is built from the
authenticated context by the caller of this package, never parsed from a body.

This mirrors `ProvisioningIntent`/`OperationBinding` in the domain contracts
(`superplane_contracts.provisioning`), deliberately: B's facade and the domain
adapter must refuse the same smuggling attempt, and a second spelling of the rule
is a second thing to get wrong. The refusal is duplicated in code rather than
imported for the reason `provisioning.py:REQUIRED_PERMISSION` gives for its own
duplication -- `harness_jobs` is installed independently of the domain contracts
package and may not have it on `sys.path`; `tests/test_contract_agreement.py`
fails if the two spellings drift.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum

# The permission an operation must carry to provision or tear down. String form of
# `superplane_auth.policy.Permission.PROVISION`, and identical to
# `superplane_contracts.provisioning.REQUIRED_PERMISSION`. Agreement is asserted by
# `tests/test_contract_agreement.py` rather than secured by an import -- see the
# module docstring.
REQUIRED_PERMISSION = "workspace:provision"

# The contract version this package implements. The *same value and the same
# spelling* as `superplane_contracts.version.CONTRACT_VERSION` -- a string `"v1"`,
# not an integer 1. Two spellings of one version is the drift this constant exists
# to prevent: a receiver comparing `1` against a sender's `"v1"` finds a mismatch on
# every well-formed request, and one comparing them loosely finds a match on every
# malformed one. `tests/test_contract_agreement.py` fails if they diverge.
#
# Carried on the request so a caller built against an incompatible contract is
# refused at admission rather than misinterpreted. #5524 records that no port
# exchanged a version at its baseline (`carries_contract_version` is False on every
# entry); this port now does, which is the condition that entry documents for
# setting it True.
CONTRACT_VERSION = "v1"

# Versions this store will admit. A frozenset rather than an equality test against
# `CONTRACT_VERSION`, mirroring `superplane_contracts.version.SUPPORTED_VERSIONS` and
# for its reason: supporting two versions during a caller rollout is normal, and an
# ordering comparison would silently accept a future version this code has never
# seen. An old value stays here for as long as a deployed caller still sends it.
SUPPORTED_CONTRACT_VERSIONS: frozenset[str] = frozenset({CONTRACT_VERSION})

# Bounds. Every one of these exists because the field reaches a database column or
# a log line, and an unbounded value there is either a write that fails at the
# constraint after work was done, or a disk-filling payload accepted as valid.
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MAX_PARAMETER_COUNT = 50
MAX_PARAMETER_KEY_LENGTH = 100
MAX_PARAMETER_VALUE_LENGTH = 2000
MAX_TOTAL_PARAMETER_BYTES = 16_384
MAX_ALLOCATION_ID_LENGTH = 255

# Identity-asserting parameter keys, refused even when correct. Compact, lowercased
# comparison so `Org-ID`, `org_id` and `orgid` are the same attempt: a
# case-sensitive exact-match check is a bypass with an obvious recipe.
#
# A **superset** of `superplane_contracts.provisioning.FORBIDDEN_PARAMETER_KEYS`, and
# the direction matters: the store may refuse more than the domain contract does, but
# never fewer. A key the domain refuses and the store accepts is a smuggling route that
# the domain's own tests would report as closed -- the worst kind of gap, because it is
# covered by a passing test somewhere else.
# `tests/test_contract_agreement.py` enforces the containment.
#
# Note what is included beyond tenancy: `role`, `permission(s)` and `account_type` are
# authorization claims rather than identity claims, and they are refused for the same
# reason -- a caller that can name its own role is a caller that can grant itself one.
_FORBIDDEN_COMPACT = frozenset(
    {
        # Tenancy and organization
        "org",
        "orgid",
        "organization",
        "organizationid",
        "organisation",
        "organisationid",
        "tenant",
        "tenantid",
        "workspace",
        "workspaceid",
        "account",
        "accountid",
        "accounttype",
        # The acting party
        "user",
        "userid",
        "username",
        "principal",
        "principalid",
        "subject",
        "sub",
        "actor",
        "actorid",
        "onbehalfof",
        "impersonate",
        "impersonateas",
        # Authority the caller must not assert for itself
        "role",
        "roles",
        "permission",
        "permissions",
        "scope",
        "scopes",
    }
)

# Parameter keys the consumer's contract declares as provisioning *shape* -- what to
# build -- which the prefix families below would otherwise catch by spelling alone.
#
# `workspace_name` is the whole of it today, and it is not a hypothetical: it is what
# the only maintained caller sends. `start_provision` passes
# `{"workspace_name": ..., "isolation_mode": ...}` and `start_teardown` passes
# `{"workspace_name": ...}` (`services/provisioning.py:341-377`), and its docstring says
# so explicitly -- "``workspace_name`` and ``isolation_mode`` are provisioning *shape*,
# not identity: they describe what to build." The `workspace_` prefix family refused it,
# so every provision and every teardown the platform actually issues was rejected as a
# smuggling attempt. A facade tested only with empty parameters cannot see that.
#
# Note what this does NOT do, and why the ordering in `forbidden_parameters` is written
# the way it is: this exempts a key from the PREFIX families only, never from the
# exact-name set. `workspace_id` is refused by `_FORBIDDEN_COMPACT` and stays refused
# whatever appears here, as do `org_id`, `subject` and every permission claim. An
# exemption list that could override an exact refusal would be a hole with a
# maintenance-shaped entrance -- someone adds a convenience field and silently admits
# the identity key next to it.
#
# `tests/test_contract_agreement.py` asserts both halves of the containment: every key
# here is one the domain contract itself accepts, and none of them collides with the
# exact refusal set. So this cannot drift into admitting something the domain refuses.
_DECLARED_SHAPE_KEYS = frozenset({"workspace_name"})

# Prefix families, for the same reason. `x-`, `adp_`, `auth_` and `caller_` come from
# the domain contract's `FORBIDDEN_PARAMETER_PREFIXES`: they are the namespaces a
# header or an internal field would arrive under, and a parameter map is not where
# either belongs. Normalized to `_` so `x-` and `x_` are one rule.
#
# Broad on purpose -- "refuse only the names we thought of" is the failure mode these
# families exist to avoid -- which is why the narrow, enumerated
# `_DECLARED_SHAPE_KEYS` above is the exception mechanism rather than deleting a family.
_FORBIDDEN_PREFIXES = (
    "org_",
    "organization_",
    "organisation_",
    "tenant_",
    "workspace_",
    "actor_",
    "principal_",
    "x_",
    "adp_",
    "auth_",
    "caller_",
)

# Identifier shape for tenant/workspace values. Deliberately strict: these end up
# in a schema-qualified query and in an idempotency key, so a value containing a
# quote or a NUL is refused at construction rather than escaped at every use.
_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ContractViolation(ValueError):
    """A value that cannot be part of a well-formed operation.

    A `ValueError` subclass because it is a malformed input, not a denial. Distinct
    from `OperationRefused` (`store.py`), which is a well-formed request the store
    will not honour -- the same split `superplane_contracts` draws between
    `ContractViolation` and `ProvisioningRefused`, and it matters at the API
    boundary because the two map to different status codes.
    """


class OperationState(str, Enum):
    """Where an operation is.

    Values are identical to `superplane_contracts.provisioning.OperationState`, and
    `str`-valued for the same reason: the stored and wire form is a stable string,
    not an ordinal that shifts when a member is inserted. A stored ordinal would
    silently remap every existing row on the next insertion.
    """

    PENDING = "pending"
    """Admitted and durable. Not yet delivered to an executor."""

    RUNNING = "running"
    """An executor has taken it. Nothing may be concluded from this state."""

    SUCCEEDED = "succeeded"
    """Completed, as established through the executor's report."""

    FAILED = "failed"
    """Did not complete."""

    CANCELLED = "cancelled"
    """Withdrawn before reaching a terminal outcome."""

    UNKNOWN = "unknown"
    """The outcome cannot be established. **Not** a failure -- see below."""


# Terminal states, named once so a poll loop asks "is it terminal" rather than
# testing `== SUCCEEDED`. UNKNOWN is terminal: it is an outcome that will not
# resolve by waiting, so a loop that excluded it would spin forever.
#
# UNKNOWN is deliberately not a synonym for failure. A consumer that collapses the
# two either leaks resources it believes were never created, or retries a provision
# that actually succeeded -- and for operations that provision cloud capacity, the
# second is duplicated spend. Same distinction `OperationState.UNKNOWN` draws in
# the domain contract.
TERMINAL_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.UNKNOWN,
    }
)


def forbidden_parameters(parameters: dict[str, str]) -> tuple[str, ...]:
    """Parameter keys that assert an identity, in the order supplied.

    Returned rather than raised so a caller can report every offending key at once
    instead of one per round trip.

    The three checks are ordered, and the order is the safety property:

    1. **exact-name refusal, unconditional.** `workspace_id`, `org_id`, `subject`,
       `role`, `permissions` and the rest of `_FORBIDDEN_COMPACT` are refused here and
       nothing downstream can un-refuse them.
    2. **declared-shape exemption**, consulted only for keys that survived (1). This is
       what lets the real caller's `workspace_name` through without deleting the
       `workspace_` family that protects `workspace_id`.
    3. **prefix families**, the broad catch-all for everything else.

    Written as `continue` after the exact check rather than as one boolean expression
    because the precedence has to be readable: a single `and not exempt` clause
    spanning both checks would make the exemption look like it applies to the exact set
    too, and a later edit would make that true.
    """
    offending: list[str] = []
    for key in parameters:
        lowered = key.strip().lower().replace("-", "_")
        compact = lowered.replace("_", "")
        if compact in _FORBIDDEN_COMPACT:
            # Never exemptible. An identity claim is refused on its exact name
            # regardless of what the shape allowlist says.
            offending.append(key)
            continue
        if lowered in _DECLARED_SHAPE_KEYS:
            # Declared shape: what to build, not who for. See `_DECLARED_SHAPE_KEYS`.
            continue
        if any(lowered.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES):
            offending.append(key)
    return tuple(offending)


@dataclass(frozen=True)
class ResolvedPrincipal:
    """The authority's half: who this operation acts for, as the server resolved it.

    Frozen, and constructed only from an authenticated context. This type existing
    separately from `OperationRequest` is the whole of the anti-smuggling property:
    the caller's half cannot supply the authority's half because they are different
    types and only one of them is built from the request.

    ``subject`` is who asked; ``org_id``/``workspace_id`` is what they are acting
    on. All three come from the verified token, never from a body.
    """

    org_id: str
    workspace_id: str
    subject: str
    permissions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in ("org_id", "workspace_id", "subject"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _IDENTIFIER.match(value):
                raise ContractViolation(
                    f"{name} must be a short identifier of letters, digits, "
                    f"'.', '_', ':' or '-'; got {value!r}"
                )

    @property
    def may_provision(self) -> bool:
        """Whether this principal carries the permission an operation demands.

        A property rather than a check at construction: a principal without the
        permission is a legitimate object (it can read its own operations), it just
        cannot admit one. Conflating the two would mean a read path needed a write
        permission.
        """
        return REQUIRED_PERMISSION in self.permissions


@dataclass(frozen=True)
class OperationRequest:
    """The caller's half: what is being asked for.

    No ``org_id``, no ``workspace_id``, no ``subject`` -- see the module docstring.
    ``idempotency_key`` is the caller's; it is scoped by the *resolved* tenant at
    the constraint (`store.py`), never trusted to be globally unique.

    ``plan_digest`` is what makes changed-payload reuse detectable: it is a digest
    of everything the caller is asking for, computed by `payload_digest` over this
    request. Storing the digest rather than the payload means the store can refuse
    a changed retry without keeping a second copy of the request body.
    """

    action: str
    idempotency_key: str
    parameters: dict[str, str] = field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.action not in ("provision", "teardown"):
            # Teardown is here rather than in its own type because it is the same
            # authority question with the opposite effect. Giving it a separate
            # request type would invite giving it a separate, weaker check.
            raise ContractViolation(
                f"action must be 'provision' or 'teardown'; got {self.action!r}"
            )
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ContractViolation("idempotency_key is required")
        if len(self.idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ContractViolation(
                f"idempotency_key exceeds {MAX_IDEMPOTENCY_KEY_LENGTH} characters"
            )
        if "\x00" in self.idempotency_key:
            raise ContractViolation("idempotency_key must not contain NUL")
        _check_parameters(self.parameters)
        if self.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
            # Refused, not best-effort interpreted. #5524's compatibility table
            # requires "absent, blank, mismatched or unsupported -- never
            # best-effort interpretation", because a caller built against a
            # different contract has different expectations about what its fields
            # mean, and guessing produces a wrong answer that looks right.
            #
            # Membership rather than `!=` so that admitting a second version during a
            # caller rollout is a one-line change to the frozenset rather than a
            # rewrite of this check -- which is when a hurried "or" gets added and
            # stops refusing anything.
            raise ContractViolation(
                f"unsupported contract version {self.contract_version!r}; "
                f"this store implements {sorted(SUPPORTED_CONTRACT_VERSIONS)}"
            )


def _check_parameters(parameters: dict[str, str]) -> None:
    """Refuse a parameter map that asserts an identity or has no finite size.

    The identity case that matters is the one that looks harmless: an ``org_id``
    that *matches* the caller's real organization. Still refused. A caller-supplied
    identity that agrees today is a code path that reads caller-supplied identity,
    and once that path exists the only thing stopping a mismatched value from being
    honoured is that something else happens to compare them -- and comparisons get
    reordered, cached, or made conditional. Refusing regardless makes "no path
    reads caller-supplied identity" a property rather than a coincidence.
    """
    if not isinstance(parameters, dict):
        raise ContractViolation("parameters must be a mapping")
    offending = forbidden_parameters(parameters)
    if offending:
        raise ContractViolation(
            "parameters must not assert an identity; the operation's tenant is "
            "resolved from the authenticated context. Offending keys: "
            + ", ".join(sorted(offending))
        )
    if len(parameters) > MAX_PARAMETER_COUNT:
        raise ContractViolation(
            f"at most {MAX_PARAMETER_COUNT} parameters; got {len(parameters)}"
        )
    total = 0
    for key, value in parameters.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ContractViolation("parameter keys and values must be strings")
        if len(key) > MAX_PARAMETER_KEY_LENGTH:
            raise ContractViolation(
                f"parameter key {key[:32]!r} exceeds "
                f"{MAX_PARAMETER_KEY_LENGTH} characters"
            )
        if len(value) > MAX_PARAMETER_VALUE_LENGTH:
            raise ContractViolation(
                f"parameter {key!r} value exceeds "
                f"{MAX_PARAMETER_VALUE_LENGTH} characters"
            )
        total += len(key.encode()) + len(value.encode())
    if total > MAX_TOTAL_PARAMETER_BYTES:
        # Checked in addition to the per-field bounds because 50 parameters each
        # just under the value limit is 100 KB, which each individual check passes.
        raise ContractViolation(
            f"parameters exceed {MAX_TOTAL_PARAMETER_BYTES} bytes in total"
        )


def payload_digest(request: OperationRequest) -> str:
    """A stable digest of everything the caller asked for.

    Stable across processes and across dict ordering: parameters are sorted, and
    every part is length-prefixed. Length-prefixing rather than delimiter-joining
    because ``{"a": "b:c"}`` and ``{"a:b": "c"}`` join to the same string, and two
    different requests sharing a digest is exactly the collision that would let a
    changed retry pass as identical.

    SHA-256 rather than a cheaper hash: the value this protects is "a retry cannot
    smuggle a bigger budget envelope past an approval", so a caller that can find a
    collision can do precisely the thing the check exists to prevent.
    """
    import hashlib

    parts: list[str] = [
        request.contract_version,
        request.action,
        request.idempotency_key,
    ]
    for key in sorted(request.parameters):
        parts.append(key)
        parts.append(request.parameters[key])
    joined = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(joined.encode()).hexdigest()


def encode_payload(request: OperationRequest) -> str:
    """The admitted request, in a form it can be read back out of.

    The digest above is one-way and therefore cannot answer "what was accepted?". This
    can. Both are stored: the digest detects a changed retry, this reconstructs the
    request a recovering dispatcher has to perform. An earlier revision stored only the
    digest, which left an operation identifiable but not executable after a restart.

    JSON with sorted keys rather than a bespoke format, because the requirement is that
    a *different* process -- possibly a later version of this code -- can decode it, and
    a hand-rolled encoding is a second parser to keep in agreement with its writer.
    Sorted keys and no spaces so the same request encodes to the same bytes in any
    process, which is what lets the stored value be compared as a string at all.

    Deliberately does **not** carry the tenant. `org_id`/`workspace_id` are the
    server's, they live in their own non-null columns, and a copy inside the payload
    would be a second source for a value that must have exactly one -- the divergence
    this package's separate `ResolvedPrincipal` type exists to make impossible. A reader
    needing the tenant reads the column.
    """
    import json

    return json.dumps(
        {
            "contract_version": request.contract_version,
            "action": request.action,
            "idempotency_key": request.idempotency_key,
            "parameters": dict(sorted(request.parameters.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_payload(encoded: str) -> OperationRequest:
    """Rebuild the admitted request from its stored encoding.

    Returns a real `OperationRequest`, so every bound and every forbidden-key refusal
    in `__post_init__` is applied again on the way out. That is not redundant with the
    checks at admission: this value came back from a database, and re-validating is what
    makes "a row was altered in place" a refusal rather than an execution. A decoder
    that skipped validation would be a path by which stored data is trusted more than
    caller data.

    Raises `ContractViolation` for anything that is not a payload this module wrote --
    including valid JSON of the wrong shape, which is what a partially-migrated or
    hand-edited row looks like.
    """
    import json

    try:
        data = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ContractViolation(
            "the stored request payload is not decodable; the admitted request cannot "
            "be reconstructed"
        ) from error
    if not isinstance(data, dict):
        raise ContractViolation("the stored request payload is not an object")
    parameters = data.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ContractViolation("the stored request payload's parameters are not a map")
    try:
        return OperationRequest(
            action=data["action"],
            idempotency_key=data["idempotency_key"],
            parameters=parameters,
            contract_version=data.get("contract_version", CONTRACT_VERSION),
        )
    except KeyError as error:
        raise ContractViolation(
            f"the stored request payload is missing {error.args[0]!r}"
        ) from error


@dataclass(frozen=True)
class OperationBinding:
    """The immutable binding between a request and the operation admitted for it.

    This is what "durable identity" means concretely: the tenant is the server's,
    the plan digest is the request's, and neither can change for the life of the
    operation. `store.py` never updates these columns -- status and version change,
    the binding does not -- which is what lets a recovering process trust a row it
    did not write.

    ``attempt_id`` is here and is distinct from ``operation_id`` because an
    operation may be attempted more than once (an executor crashes; a lease
    expires). Attempt *lifecycle* -- issuing later attempts, leases and fences -- is
    #5527's (w6-04); this story stores the identity so the fields exist to be
    fenced rather than added later by a migration that has to rewrite live rows.

    ``job_id`` is the third leg, and the published contracts are why it is not
    optional: the domain budget hooks are specified as "idempotent on
    ``(job_id, attempt_id)``" (`INTEGRATION-CONTRACT.md:296,368`, per #4912). v1 mints
    one job per admitted operation, so the values are one-to-one today -- but they are
    different identities with different lifetimes, and collapsing them would mean
    #5526's hooks either invent a second identifier or migrate a table already keyed on
    this one.

    ``request_payload`` carries the encoded request, so the binding a recovering process
    reconstructs is executable and not merely identifiable.
    """

    operation_id: str
    attempt_id: str
    job_id: str
    org_id: str
    workspace_id: str
    action: str
    idempotency_key: str
    plan_digest: str
    request_payload: str
    contract_version: str = CONTRACT_VERSION

    @classmethod
    def issue(
        cls,
        principal: ResolvedPrincipal,
        request: OperationRequest,
        *,
        operation_id: str | None = None,
        attempt_id: str | None = None,
        job_id: str | None = None,
    ) -> OperationBinding:
        """Mint a binding from the *resolved* principal and the caller's request.

        The only constructor used in production, and the reason it is a classmethod
        here rather than a helper elsewhere: it is the single place the two halves
        are joined, so there is one place to read to confirm the tenant came from
        the principal and not the request.

        IDs default to random UUID4 and are injectable only so tests can assert on
        fixed values. A caller-chosen ``operation_id`` is not an authorization
        problem -- the tenant is still the principal's, and the uniqueness
        constraint still holds -- but it is not how production mints them.

        Note that a freshly minted ``job_id`` here is *not* what makes job identity
        stable across retries. A retry mints a new binding, its INSERT loses to the
        uniqueness constraint, and `store._resolve_conflict` returns the **stored**
        row -- so the job id a retry sees is the one admission committed, and this
        value is discarded. Stability is a property of the constraint, not of this
        function, which is the same reason the operation id is stable.
        """
        if not principal.may_provision:
            raise OperationRefused(
                f"principal lacks {REQUIRED_PERMISSION}; the operation is not admitted"
            )
        return cls(
            operation_id=operation_id or str(uuid.uuid4()),
            attempt_id=attempt_id or str(uuid.uuid4()),
            job_id=job_id or str(uuid.uuid4()),
            org_id=principal.org_id,
            workspace_id=principal.workspace_id,
            action=request.action,
            idempotency_key=request.idempotency_key,
            plan_digest=payload_digest(request),
            request_payload=encode_payload(request),
            contract_version=request.contract_version,
        )


class OperationRefused(PermissionError):
    """A well-formed request the store will not admit.

    A ``PermissionError`` subclass, matching `ProvisioningRefused` in the domain
    contracts, so a caller that handles authorization failures uniformly catches
    this too.

    Defined at the bottom of the module because `OperationBinding.issue` raises it
    and `store.py` re-exports it; keeping it here rather than in `store.py` avoids
    an import cycle between identity and the store that holds it.
    """


def admitted_credential_reference(encoded: str, digest: str) -> tuple[str, str, str]:
    """Read the exact credential selected in the approved request parameters.

    These are resource selectors, not authority claims. Admission includes them in
    the canonical plan digest just like every other parameter. Operations that do
    not select all three fields cannot use operation-bound credential delivery.
    """
    import hmac

    request = decode_payload(encoded)
    if not isinstance(digest, str) or not hmac.compare_digest(
        payload_digest(request), digest
    ):
        raise ContractViolation("stored request does not match its approved digest")
    reference = tuple(
        request.parameters.get(key, "")
        for key in ("credential_id", "credential_service", "credential_label")
    )
    if any(not value.strip() for value in reference):
        raise ContractViolation("approved operation has no exact credential reference")
    return reference


def admitted_credential_target(encoded: str, digest: str) -> tuple[str, str]:
    """The provider/account approved for credential use, covered by the same digest."""
    admitted_credential_reference(encoded, digest)
    request = decode_payload(encoded)
    target = tuple(
        request.parameters.get(key, "") for key in ("provider", "provider_account_id")
    )
    if any(not value.strip() for value in target):
        raise ContractViolation("approved operation has no provider account target")
    return target
