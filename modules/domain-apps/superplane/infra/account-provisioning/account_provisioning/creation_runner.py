"""Creating one AWS account, durably and at most once — Issue #5531 (w6-08).

This is the production path the reviewed revision was missing. `account_factory.creation`
decides *whether* `CreateAccount` may be called; this calls it, through the durable
executor, and records what came back.

## The failure this module is built around

`CreateAccount` is asynchronous. The reply is not an account — it is a `car-...` request id
and a state of `IN_PROGRESS`. The account id arrives later, from a second call. So there is
a window in which AWS is opening an account and the caller does not yet know its id, and if
the caller dies in that window the only recoverable pointer is the request id.

That is why the order below is not an implementation detail:

1. **Commit intent** under a stable key, before any AWS call. `DurableExecutor`
   does this inside `execute_provider`, and the row is committed — not held in an open
   transaction — precisely so a crash leaves it behind.
2. **Call `CreateAccount` exactly once.**
3. **Record the request id**, whatever the outcome, including when the call then failed.

A record written *after* the call is absent in the one case it exists for. That is not a
hypothetical: it is the concrete defect the reviewed revision's in-memory ledger had.

## Why an unobtained answer is not a failure

If the call times out, an account may be opening. Treating that as failure and retrying
produces two accounts for one workspace, both billable, with only one in any record. So
`ProviderUnavailable` becomes `CallOutcome.UNKNOWN`, which the durable store treats as
"may have happened": budget is retained and no automatic retry follows. Recovery is
`reconcile_creation` below, which reads the *stored request id* and asks AWS about that
request rather than starting a new one.

The converse matters too and is easy to get backwards: an AWS `EmailAlreadyExists` is a
real `FAILED`, because AWS established that nothing was created. Only answers of that kind
are allowed to mean failure.

## What this module does not do

It does not construct an AWS client, read a region or profile, or hold credential material
— clients arrive through `ports.CredentialSource`. It does not close or delete anything:
`ports.OrganizationsClient` has no method for either. It does not bootstrap the account it
creates; that is `bootstrap_runner.py`, and keeping the two separate is what makes "created
but not bootstrapped" a state the system can name and recover rather than a half-finished
function call.

**It also does not place the account.** `CreateAccount` takes no organizational unit, so the
account it opens is at the organization root — outside every service control policy the
approved OU imposes. `_confirm_placement` below confirms the DESTINATION exists before any
money is spent; actually putting the account there, under its own durable fence and verified
by reading the account's real parent, is `placement.py`. Those are separate modules for the
same reason creation and bootstrap are: "created but not yet placed" is a real, recoverable
state that a single function could only leave implicit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from account_factory.creation import (
    AccountCreationError,
    AttemptDisposition,
    CreateAccountFailure,
    CreateAccountObservation,
    CreateAccountStatus,
    assess_attempt,
)
from account_factory.modes import (
    AccountFactoryRequest,
    ModeError,
    OwnershipMode,
    ValidationAuthorization,
    ensure_valid,
)

from .execution import (
    CallOutcome,
    DurableExecutor,
    OutcomeVocabulary,
    ProviderCallRecord,
    ReconciliationStore,
    as_outcome,
    outcome_vocabulary,
)
from .ports import (
    CredentialSource,
    OrganizationsClient,
    ProviderDenied,
    ProviderUnavailable,
)

__all__ = [
    "CREATION_STEP",
    "MAX_CREATION_GENERATIONS",
    "AccountCreationOutcome",
    "CreationRefused",
    "create_account",
    "creation_generation_keys",
    "creation_target",
    "payload_digest",
    "reconcile_creation",
    "reconciled_outcome",
]

# The step name that, with the operation id, forms the idempotency key. A constant because
# the key must be IDENTICAL across a restart: a key derived from anything per-attempt (a
# timestamp, a uuid, the attempt number) would make every retry a fresh key, and a fresh key
# is a fresh account. This is the single most load-bearing literal in the package, so
# `tests/test_creation_runner.py` asserts a re-run derives the same one.
CREATION_STEP = "organizations-create-account"

# The provider and operation names recorded on the durable row, for operators reading it.
_PROVIDER = "aws-organizations"
_OPERATION_KIND = "create-account"

# AWS `CreateAccountStatus.FailureReason` values mapped onto the vocabulary
# `account_factory.creation` already refuses on. Only reasons that establish NOTHING WAS
# CREATED belong here; an unrecognised reason is deliberately not silently treated as one.
_FAILURE_REASONS = {
    "EMAIL_ALREADY_EXISTS": CreateAccountFailure.EMAIL_ALREADY_EXISTS,
    "ACCOUNT_LIMIT_EXCEEDED": CreateAccountFailure.ACCOUNT_LIMIT_EXCEEDED,
    "INVALID_EMAIL": CreateAccountFailure.INVALID_EMAIL,
    "CONCURRENT_ACCOUNT_MODIFICATION": (CreateAccountFailure.CONCURRENT_ACCOUNT_MODIFICATION),
    "INTERNAL_FAILURE": CreateAccountFailure.INTERNAL_FAILURE,
}


class CreationRefused(Exception):
    """This account creation must not proceed, and no AWS call was made.

    Raised before any provider effect — for a request that does not validate, a placement
    that was not confirmed, or a prior attempt whose outcome is unresolved. A caller seeing
    this knows nothing was created by this call.
    """


@dataclass(frozen=True)
class AccountCreationOutcome:
    """What one creation attempt established, and what it did not.

    `account_id` is set only when AWS confirmed one. Every other field exists so that a
    caller cannot read a partial result as a complete one — in particular `may_exist`,
    which is true exactly when an account may have been opened without its id being known.
    """

    status: CreateAccountStatus
    create_account_request_id: str | None
    account_id: str | None
    failure: CreateAccountFailure | None
    detail: str

    @property
    def succeeded(self) -> bool:
        """AWS confirmed the account and gave its id."""
        return self.status is CreateAccountStatus.SUCCEEDED and self.account_id is not None

    @property
    def may_exist(self) -> bool:
        """An account may exist that this outcome cannot name.

        True for an unresolved outcome and for a confirmed-but-unnamed one. The state that
        needs an operator, and the reason a retry is not offered automatically.
        """
        if self.status is CreateAccountStatus.SUCCEEDED:
            return self.account_id is None
        return self.status in (
            CreateAccountStatus.UNKNOWN,
            CreateAccountStatus.IN_PROGRESS,
        )

    @property
    def needs_reconciliation(self) -> bool:
        """The outcome is not settled, and a stored request id exists to settle it with."""
        return self.may_exist and self.create_account_request_id is not None


def _require_new_account_mode(request: AccountFactoryRequest) -> None:
    """Refuse any mode other than the explicitly-named new-account one.

    Creating an account must never be reachable as a side effect of onboarding an existing
    one, which the issue states as a requirement and which this asserts at the entry point
    rather than trusting a caller to have checked.
    """
    if request.mode is not OwnershipMode.NEW_ACCOUNT_MANAGED:
        raise CreationRefused(
            f"account creation requires mode "
            f"{OwnershipMode.NEW_ACCOUNT_MANAGED.value!r}, not {request.mode.value!r}. "
            f"Creating an account is never a side effect of onboarding an existing one"
        )


def _require_complete_authorization(
    executor: DurableExecutor,
    request: AccountFactoryRequest,
    authorization: ValidationAuthorization | None,
) -> list[str]:
    """Fail closed unless creation is fully authorized by the admitted operation.

    `account_factory.modes.validate` deliberately reports an absent authorization field as
    *not checked* rather than as a failure, because the same validator has to run offline
    before any authorization exists. That is right for offline analysis and wrong for
    execution: the previous revision passed `authorization=None` straight through, and
    `assess_attempt` discarded the unchecked list, so `may_create_account` came back true
    with nothing compared against anything.

    This is the separation. Offline analysis may leave comparisons unmade and say so;
    anything that can actually call `CreateAccount` must have made all of them.

    Three obligations, in order of what they protect against:

    1. **An authorization must be present.** No default, so omitting it cannot be mistaken
       for having one.
    2. **Every comparison must have been made.** `ensure_valid` returns the fields that were
       NOT checked; a non-empty list refuses here, naming them. Otherwise an authorization
       that simply omitted `permitted_organizational_units` would place an account anywhere
       in the tree and report itself verified.
    3. **The authorization must belong to THIS operation.** The workspace and organization
       are compared against the executor's lease — server-resolved identity that nothing in
       a request body can influence. Without this, a caller could present a self-consistent
       authorization for a workspace that is not the one the operation was admitted for, and
       every field-level comparison would pass.

    Returns the (necessarily empty) unchecked list, so a caller can record that nothing was
    left unverified. Raises `CreationRefused` before any credential or provider call.
    """
    if authorization is None:
        raise CreationRefused(
            "account creation requires an operation authorization and none was supplied. "
            "Creating an AWS account with no organization, workspace, mode, management "
            "identity or permitted placement compared against anything is refused; build "
            "one with ValidationAuthorization.from_operation_binding"
        )

    try:
        unchecked = ensure_valid(request, authorization)
    except ModeError as exc:
        raise CreationRefused(f"the creation request was refused before any provider call: {exc}") from exc

    if unchecked:
        raise CreationRefused(
            f"account creation requires every authorization comparison to have been made, "
            f"but {len(unchecked)} was/were not: {', '.join(sorted(unchecked))}. An "
            f"unchecked comparison is not a pass — a missing permitted-placement set would "
            f"let an account land anywhere in the organization tree while reporting itself "
            f"as verified"
        )

    # The lease is the authority. `workspace_id`/`org_id` on the executor come from the
    # admitted operation, not from anything the caller sent.
    lease_workspace = executor.workspace_id
    lease_organization = executor.org_id
    mismatches = []
    if authorization.workspace_id != lease_workspace:
        mismatches.append(f"authorized workspace {authorization.workspace_id!r} is not the operation's workspace {lease_workspace!r}")
    operation_org = authorization.operation_org_id or authorization.organization_id
    if operation_org != lease_organization:
        mismatches.append(f"authorized operation organization {operation_org!r} is not the operation's organization {lease_organization!r}")
    # The request is compared against the lease too. `ensure_valid` already compared the
    # request to the authorization, so this closes the remaining triangle: a request, an
    # authorization and a lease that all agree.
    if request.workspace_id != lease_workspace:
        mismatches.append(f"the request's workspace {request.workspace_id!r} is not the operation's workspace {lease_workspace!r}")
    # ensure_valid separately compares the AWS Organization in the request to
    # the trusted provider authorization. Never compare an AWS o-... identifier
    # to a domain UUID or relabel the admitted lease to make that comparison pass.
    if mismatches:
        raise CreationRefused(
            "the supplied authorization does not belong to this operation: "
            + "; ".join(mismatches)
            + ". Refusing rather than creating an account for a tenant this operation was not admitted for"
        )

    return unchecked


async def _confirm_placement(organizations: OrganizationsClient, request: AccountFactoryRequest) -> None:
    """Confirm the requested OU exists in this organization, before creating anything.

    A check on the DESTINATION, and only that. It establishes that the unit the approval names
    is real, so creation does not spend money opening an account that cannot then be placed
    anywhere the approval allows. Checking after creation would be checking after the money is
    spent, so this runs first.

    It does NOT place the account, and the name has been a trap worth spelling out: an earlier
    revision had exactly this function, correct in itself, and nothing else — so the request
    named an OU, this confirmed the OU existed, and the account was opened at the organization
    root and left there, outside every control the unit imposes. Performing the placement and
    verifying it against the account's actual parent is `placement.verify_placement`;
    `bootstrap_runner` refuses to write account-wide roles without its answer.

    A read that cannot be performed is a refusal, not a pass: `ProviderUnavailable`
    propagates rather than being swallowed, because "I could not check the OU" must not
    look like "the OU is fine".
    """
    unit = (request.organizational_unit_id or "").strip()
    if not unit:
        raise CreationRefused(
            "new-account-managed requires an organizational unit; an unstated placement "
            "means the organization root, which is the least restricted position"
        )

    def pages(method, **parameters):
        token = None
        seen_tokens = set()
        while True:
            page = method(**parameters, **({"NextToken": token} if token else {}))
            yield page
            token = page.get("NextToken")
            if not token:
                break
            if token in seen_tokens:
                raise CreationRefused("Organizations pagination repeated a token; placement was not established")
            seen_tokens.add(token)

    roots = [root for page in pages(organizations.list_roots) for root in page.get("Roots", [])]
    if not roots:
        raise CreationRefused("the organization reported no roots; approved placement could not be confirmed")
    known = set()
    pending = [root["Id"] for root in roots if root.get("Id")]
    visited = set()
    while pending:
        parent = pending.pop()
        if parent in visited:
            continue
        visited.add(parent)
        for page in pages(organizations.list_organizational_units_for_parent, ParentId=parent):
            for child in page.get("OrganizationalUnits", []):
                child_id = child.get("Id")
                if not child_id:
                    continue
                known.add(child_id)
                if child_id == unit:
                    return
                pending.append(child_id)
    if unit not in known:
        raise CreationRefused(
            f"organizational unit {unit!r} does not exist in this organization "
            f"(found: {', '.join(sorted(known)) or 'none'}). Refusing to create an "
            f"account that would land at an unintended position in the tree"
        )


def _observation_from_status(status: dict) -> CreateAccountObservation:
    """Read one AWS `CreateAccountStatus` structure into the module's own vocabulary.

    Unrecognised states become `UNKNOWN` rather than a failure: a state this code does not
    know is precisely a state it has not established the absence of an account for.

    ## Every un-representable reply is downgraded here, and that is load-bearing

    `CreateAccountObservation` refuses to hold a `SUCCEEDED` with no account id or a `FAILED`
    with no reason — deliberately, because such a record is an assertion rather than an
    observation. This function is the only place AWS's words become one of those, so it is the
    only place that can honour the refusal, and an earlier revision did not: it passed
    `account_id=None` straight into a `SUCCEEDED` observation and raised `AccountCreationError`
    out of the parser.

    That raise was not a loud failure, which is what made it dangerous. On the hook path the
    executor catches every exception and substitutes `UNKNOWN` with no reference at all — so a
    reply saying an account EXISTS discarded the request id that was the only handle to it. On
    the recovery path nothing catches it, so one such row aborted the sweep and every row
    behind it went unreconciled. Both are produced by a response shape AWS is entitled to
    return, and neither is distinguishable afterwards from a network error.
    """
    state = (status.get("State") or "").upper()
    request_id = status.get("Id")
    if state == "SUCCEEDED":
        account_id = status.get("AccountId")
        if not account_id:
            # An account exists and this response does not name it. `UNKNOWN` is the truthful
            # record: it keeps the reservation retained, authorizes no retry, and leaves the
            # request id as the handle to ask again with.
            return CreateAccountObservation(
                status=CreateAccountStatus.UNKNOWN,
                create_account_request_id=request_id,
                detail=("AWS reported SUCCEEDED without an AccountId; an account exists and this response does not name it"),
            )
        return CreateAccountObservation(
            status=CreateAccountStatus.SUCCEEDED,
            create_account_request_id=request_id,
            account_id=account_id,
            detail="",
        )
    if state == "FAILED":
        reason = (status.get("FailureReason") or "").upper()
        failure = _FAILURE_REASONS.get(reason)
        if failure is None:
            # An unmapped reason establishes nothing, so it must not read as FAILED.
            return CreateAccountObservation(
                status=CreateAccountStatus.UNKNOWN,
                create_account_request_id=request_id,
                detail=(
                    f"AWS reported FAILED with unrecognised reason {reason!r}; treating "
                    f"the outcome as unresolved rather than assuming no account exists"
                ),
            )
        return CreateAccountObservation(
            status=CreateAccountStatus.FAILED,
            create_account_request_id=request_id,
            failure=failure,
            detail=status.get("FailureReason") or "",
        )
    if state == "IN_PROGRESS":
        return CreateAccountObservation(
            status=CreateAccountStatus.IN_PROGRESS,
            create_account_request_id=request_id,
            detail="AWS is still opening the account",
        )
    return CreateAccountObservation(
        status=CreateAccountStatus.UNKNOWN,
        create_account_request_id=request_id,
        detail=f"unrecognised CreateAccountStatus state {state!r}",
    )


def _outcome(observation: CreateAccountObservation) -> AccountCreationOutcome:
    return AccountCreationOutcome(
        status=observation.status,
        create_account_request_id=observation.create_account_request_id,
        account_id=observation.account_id,
        failure=observation.failure,
        detail=observation.detail,
    )


def _account_name(request: AccountFactoryRequest) -> str:
    """A per-workspace account name.

    Derived from the workspace rather than fixed: the legacy flow shipped a single constant
    name, which collides across every request and makes two accounts indistinguishable in
    the Organizations console.
    """
    return f"adp-{request.workspace_id}"


def payload_digest(request: AccountFactoryRequest) -> str:
    """A digest of the fields an approval was granted over.

    ## Why this is a digest carried BESIDE the fence, not part of the fence

    `account_factory.creation._idempotency_key` derives its key from the organization, the
    OU, the workspace and the contact address. Three of those four are caller-mutable, and
    that is the whole defect: change the email by one character and the derived key changes,
    so `AttemptLedger.find` looks under a key that has no record, finds nothing, and reports
    `CREATE_PERMITTED` — while the first attempt's account is still opening under the old
    key. One edited field, two billable accounts, and the fence never engaged.

    Making the fence *narrower* fixes that: this package fences on `creation_key`, which is
    the operation id and a constant step and contains nothing a caller can vary. The same
    operation therefore lands on the same key however the request body is edited.

    But a narrow fence alone creates the opposite hole. If the key ignores the payload
    entirely, an operation approved to open `team-a@example.com` in OU `ou-sandbox` could be
    re-driven with `team-b@example.com` in OU `ou-production`, hit the same key, and — if the
    first call's outcome were not yet settled — be treated as a duplicate delivery of work
    already claimed. The approval would have been granted for one account and spent on
    another.

    So the payload travels as a digest in the store's `target` field, which `harness_jobs`
    treats as part of the **immutable binding**: it refuses to re-record a key whose
    `provider`, `operation_kind` or `target` differs from the stored row. A changed payload
    is therefore refused by a database constraint, not only by this package's own comparison
    — which is what makes the refusal hold across a restart and across two concurrent
    workers.

    ## Which fields are in it, and why the cluster inputs are not

    The four that decide WHICH account this is, exactly as `_idempotency_key` uses: the
    organization, the placement, the workspace and the address. The VPC CIDR, instance type
    and cluster version are deliberately excluded — they describe what gets built *inside*
    the account, so including them would make a corrected CIDR look like a different
    approval and refuse a legitimate resume. That is the same reasoning `_idempotency_key`
    gives for excluding them, and it is still right; it was the *fence* that was wrong, not
    the field selection.

    A digest rather than the joined values, so the contact address is not carried in the
    `target` column of every row and every audit line that mentions one.
    """
    material = "\x1f".join(
        (
            request.organization_id,
            request.organizational_unit_id or "",
            request.workspace_id,
            (request.account_email or "").strip().lower(),
        )
    )
    return "apd-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def creation_target(request: AccountFactoryRequest) -> str:
    """The immutable-binding `target` recorded for this operation's creation call.

    The account name and the payload digest together. The name is there so an operator
    reading the row sees which account it is about; the digest is there so the store refuses
    a second dispatch under the same key with a different approved payload.
    """
    return f"{_account_name(request)}#{payload_digest(request)}"


def _require_unchanged_payload(request: AccountFactoryRequest, recorded: ProviderCallRecord | None) -> None:
    """Refuse to reuse an operation's fence for a payload it was not approved for.

    Reached when a row already exists for this operation's creation key — a resume, a
    duplicate delivery, or a second worker. The narrow fence means all of those legitimately
    arrive at the same key, so the payload is what distinguishes "continue the approved work"
    from "spend this approval on a different account".

    The refusal is unconditional on the prior row's outcome. It might seem safe to allow a
    changed payload after an established FAILURE — the case `EMAIL_ALREADY_EXISTS` creates,
    where the fix genuinely is to change the address. It is not safe here, because this
    function cannot distinguish that from a *succeeded* or *unresolved* row without trusting
    the outcome field, and the unresolved case is precisely where an account may be opening.
    A corrected address is a new approval and a new operation, which costs an operator one
    dispatch and cannot produce a second account.
    """
    if recorded is None:
        return
    recorded_target = getattr(recorded, "target", None)
    if not recorded_target:
        # No binding was recorded, so there is nothing to compare. Refusing is the
        # conservative answer: a row exists for this key, which means a call may have been
        # made, and this code cannot establish which payload it was made for.
        raise CreationRefused(
            "a durable record exists for this operation's account creation but carries no "
            "recorded target, so the payload it was dispatched for cannot be established. A "
            "call may have been made; reconcile the recorded request id rather than "
            "dispatching another"
        )
    expected = creation_target(request)
    if recorded_target != expected:
        raise CreationRefused(
            f"this operation's account creation was recorded for target "
            f"{recorded_target!r}, and this request derives {expected!r}. The organization, "
            f"placement, workspace or contact address has changed since the attempt was "
            f"committed. Reusing the approval would spend it on a different account than the "
            f"one it was granted for, and — while the first attempt is unresolved — could "
            f"open a second billable account. A corrected payload needs a new operation"
        )


async def create_account(
    executor: DurableExecutor,
    credentials: CredentialSource,
    request: AccountFactoryRequest,
    *,
    authorization: ValidationAuthorization,
    history: tuple[ProviderCallRecord, ...] = (),
) -> AccountCreationOutcome:
    """Create the account this request asks for, at most once, ever.

    The sequence, and why it is this sequence:

    1. Refuse any mode but the named new-account one.
    2. Require a COMPLETE authorization and verify it against the executor's own lease. No
       default, and no partially-checked authorization: see `_require_complete_authorization`.
    3. Refuse a payload that differs from the one this operation's attempts were committed
       for. See `_require_unchanged_payload`: the fence is narrow on purpose, so the payload
       check is what stops one approval being spent on a different account.
    4. Decide which generation may be dispatched, from committed rows alone. An unresolved
       prior generation, a succeeded one, or a non-retryable failure each refuse here — before
       a credential is obtained. See `_plan_generation`.
    5. Ask `account_factory.creation.assess_attempt` the same question from the decision
       layer's own arithmetic. Two independent refusals over one dispatch is deliberate: this
       module reasons about durable rows, that module reasons about attempts, and a duplicate
       account needs both to be wrong at once.
    6. Confirm the OU placement with a read.
    7. Hand the call to the durable executor under the planned generation's key, which commits
       intent before invoking the hook and records the outcome after. One dispatch per key is
       the executor's guarantee, and a new generation's key has no row yet — which is what
       makes a permitted retry *arm a new fence* rather than reuse a spent one.

    `authorization` is REQUIRED, keyword-only, and has no default. It was previously
    `None`-defaulted, which meant a caller that simply omitted it received permission to
    create an AWS account with no organization, workspace, mode, management identity or
    placement ever compared against anything.

    `history` is this operation's creation rows in generation order, contiguous from
    generation 0, as read from the durable store — `creation_generation_keys` names the keys
    to read. It replaces a single `recorded` row, which could not express a retry at all: with
    one row and one key, a retry after `CONCURRENT_ACCOUNT_MODIFICATION` was refused by the
    store forever, and the only way forward was a new operation with a fresh fence. An empty
    tuple means no row was found, which is the only state that establishes no call was made.

    Raises `CreationRefused` when nothing was attempted. Returns an outcome — including an
    unresolved one — when a call may have been made.
    """
    _require_new_account_mode(request)
    # Before any credential is obtained and any provider call is made. An authorization
    # failure must not be discoverable only after AWS has been contacted.
    _require_complete_authorization(executor, request, authorization)
    for row in history:
        _require_unchanged_payload(request, row)

    plan = _plan_generation(request, history)

    # The decision layer's own arithmetic, over the generation being superseded. Asked even
    # though `_plan_generation` has already refused the unsafe cases: the two are independent
    # implementations of "may this call be made", and the one thing that must never happen
    # needs both of them to be wrong simultaneously.
    ledger = _ledger_for(request, plan.prior)
    decision = assess_attempt(request, ledger, None, authorization)
    if not decision.requires_durable_attempt:
        raise CreationRefused(f"creation not permitted ({decision.disposition.value}): {decision.reason}")

    management = await credentials.management(operation_id=executor.operation_id)
    await _confirm_placement(management.organizations, request)

    key = creation_key(executor, plan.generation)
    call, _ = await executor.execute_provider(
        idempotency_key=key,
        provider=_PROVIDER,
        operation_kind=_OPERATION_KIND,
        # The payload digest rides in the immutable binding, so the store refuses a second
        # dispatch under this key for a different approved account. See `payload_digest`.
        target=creation_target(request),
    )
    return _settled_outcome(call)


def creation_key(executor: DurableExecutor, generation: int = 0) -> str:
    """The idempotency key for one generation of this operation's account creation.

    Derived only from the operation id, a constant step, and a generation number that
    advances **solely** by a settled retryable failure being recorded. Nothing a caller can
    vary enters it — no timestamp, no uuid, no attempt counter from process memory — so every
    restart of the same generation derives the same key and the store's duplicate refusal
    engages.

    ## Why a generation exists at all

    A single constant key is right for safety and wrong for liveness, and both halves matter.

    `execute_provider` commits intent with `fresh=True`: it refuses a key that already has a
    row, because repeating a call whose intent is already recorded is how an effect happens
    twice. With one key per operation, that makes the FIRST attempt the only attempt —
    including after `CONCURRENT_ACCOUNT_MODIFICATION`, which is AWS saying "another account
    operation is in flight, ask again". A legitimate, explicitly-retryable failure would be
    unrecoverable except by opening a new operation, and a new operation is a new approval
    with a fresh fence — which is precisely the route that produces a second account.

    So a retry gets its own key. The generation is what makes the new key **derivable rather
    than invented**: generation N+1 exists only if generation N's row settled as a failure
    that `CreateAccountFailure.retry_can_succeed` allows. That is a claim about committed
    rows, so two concurrent workers reading the same history derive the same next key and one
    of them loses the race at the constraint.

    Generation 0 is spelled without a suffix, so a row written before generations existed
    still reads as generation 0 rather than orphaning.
    """
    if generation < 0:
        raise AccountCreationError(f"a creation generation cannot be negative, got {generation}")
    if generation == 0:
        return f"{executor.operation_id}:{CREATION_STEP}"
    return f"{executor.operation_id}:{CREATION_STEP}:retry-{generation}"


def creation_generation_keys(executor: DurableExecutor, count: int) -> tuple[str, ...]:
    """The keys a composer must read to assemble this operation's creation history.

    Exposed because the history cannot be discovered by *asking* the store: `record_intent`
    commits intent, so probing for generation N+1 would arm a fence that no dispatch ever
    settles, blocking the operation permanently. A caller therefore reads the keys it knows
    about, and this names them so the spelling is not reinvented at the call site.

    `count` is how many generations to name, starting at 0. Reading one more than the highest
    generation found is the normal pattern: the extra read returning nothing is what
    establishes the history is complete.
    """
    if count < 0:
        raise AccountCreationError(f"cannot name a negative number of generations, got {count}")
    return tuple(creation_key(executor, generation) for generation in range(count))


# A ceiling on how many times one approval may be re-dispatched. Every generation is a
# potential account if a "failure" was reported for a call that in fact allocated one, so an
# unbounded retryable loop is an unbounded number of accounts. The two reasons that reach here
# are genuinely transient, so a small number is enough for the honest case and stops a wedged
# operation from retrying forever without an operator ever looking at it.
MAX_CREATION_GENERATIONS = 3


@dataclass(frozen=True)
class _GenerationPlan:
    """Which generation may be dispatched next, and what the history established."""

    generation: int
    prior: ProviderCallRecord | None


def _plan_generation(
    request: AccountFactoryRequest,
    history: tuple[ProviderCallRecord, ...],
) -> _GenerationPlan:
    """Decide which generation may be dispatched, from committed rows alone.

    `history` is this operation's creation rows in generation order, contiguous from 0. The
    rules, and what each one is protecting:

    * **No rows.** Generation 0. Nothing can be duplicated.
    * **Every row before the last must be a settled retryable failure.** A history where an
      earlier generation succeeded or is unresolved is a contradiction — something dispatched
      a retry it was not entitled to — and it is refused rather than continued, because the
      account the earlier generation may have created is exactly what a further dispatch
      would duplicate.
    * **The last row unresolved → refuse.** No answer was obtained, so an account may be
      opening. This is the case the whole module exists for, and it must block *further
      calls* rather than merely report itself: a lost retry response leaves an unresolved row
      at the retry's own key, and the next pass finds it here.
    * **The last row succeeded → refuse.** The account exists.
    * **The last row failed non-retryably → refuse.** Repeating cannot succeed.
    * **The last row failed retryably → generation N+1,** up to `MAX_CREATION_GENERATIONS`.

    Note what is NOT here: nothing consults a clock, a retry counter, or how long ago
    anything happened. A dispatch is authorized by committed evidence or not at all.
    """
    if not history:
        return _GenerationPlan(generation=0, prior=None)

    if len(history) > MAX_CREATION_GENERATIONS:
        raise CreationRefused(
            f"this operation has already recorded {len(history)} account-creation "
            f"generations, and the ceiling is {MAX_CREATION_GENERATIONS}. Each generation is "
            f"a call that may have allocated an account, so further automatic retries are "
            f"refused; an operator must establish what exists before anything else is "
            f"dispatched"
        )

    for index, row in enumerate(history[:-1]):
        settled = _settled_outcome(row)
        if settled.status is not CreateAccountStatus.FAILED or settled.failure is None or not settled.failure.retry_can_succeed:
            raise CreationRefused(
                f"generation {index} of this operation's account creation is recorded as "
                f"{settled.status.value!r}, but a later generation was dispatched after it. "
                f"Only an established, retryable failure authorizes a further generation, so "
                f"this history is contradictory and an account may exist that nothing names. "
                f"Refusing to dispatch another"
            )

    last = history[-1]
    settled = _settled_outcome(last)
    next_generation = len(history)

    if settled.status is CreateAccountStatus.SUCCEEDED:
        raise CreationRefused(
            f"this operation already created account {settled.account_id}. Calling "
            f"CreateAccount again would open a second one, and removing the spare is a "
            f"90-day irreversible suspension"
        )
    if settled.may_exist:
        raise CreationRefused(
            f"generation {next_generation - 1} of this operation's account creation is "
            f"unresolved ({settled.detail}). An account may exist. Reconcile the recorded "
            f"request id — {settled.create_account_request_id or 'none was recorded'} — "
            f"before anything else is dispatched; a retry now risks a second billable account"
        )
    if settled.status is not CreateAccountStatus.FAILED:
        raise CreationRefused(f"an account-creation generation in status {settled.status.value!r} does not authorize another dispatch")
    if settled.failure is None or not settled.failure.retry_can_succeed:
        reason = settled.failure.value if settled.failure else "an unrecorded reason"
        raise CreationRefused(
            f"AWS refused this operation's account creation for {reason}, and repeating the "
            f"same request cannot succeed. The input must change or the organization's quota "
            f"must be raised, and a changed payload is a new operation"
        )
    if next_generation >= MAX_CREATION_GENERATIONS:
        raise CreationRefused(
            f"this operation's account creation has failed retryably "
            f"{next_generation} time(s), reaching the ceiling of "
            f"{MAX_CREATION_GENERATIONS} generations. Each one is a call that may have "
            f"allocated an account, so an operator must look before another is dispatched"
        )
    return _GenerationPlan(generation=next_generation, prior=last)


def _ledger_for(request: AccountFactoryRequest, recorded: ProviderCallRecord | None) -> object:
    """Build the attempt ledger `assess_attempt` reads, from the durable row.

    The reviewed revision's ledger was an in-memory dict that a restart emptied. This
    derives it from the row the durable store holds, so "has this been attempted?" is
    answered by PostgreSQL rather than by process memory. That is the substantive
    difference the supervisor asked for, expressed in one function.
    """
    from account_factory.creation import AttemptLedger, intended_attempt

    ledger = AttemptLedger()
    if recorded is None:
        return ledger
    attempt = intended_attempt(request)
    # DECODED, not passed through. `provider_ref` holds this module's own `request=...
    # account=...` encoding, and `CreateAccountAttempt` validates its
    # `create_account_request_id` against `^car-[0-9a-zA-Z]{8,64}$`. Handing it the raw
    # reference raised `AccountCreationError` on every restart that had a recorded row — so
    # the one path that exists to answer "was a call already made?" crashed instead of
    # answering, in exactly the state where a wrong answer opens a second account.
    recorded_account, recorded_failure, recorded_request_id = _decode_reference(recorded.provider_ref)
    # Normalized, not compared with `is`: the record carries the HARNESS enum member, which
    # is never identical to this package's copy. See `execution.as_outcome`.
    outcome = as_outcome(recorded.outcome)

    account_id = None
    failure = None
    if outcome is CallOutcome.SUCCEEDED and recorded_account:
        status = CreateAccountStatus.SUCCEEDED
        account_id = recorded_account
    elif outcome is CallOutcome.SUCCEEDED:
        # A row saying "succeeded" whose reference does not name the account. The worst
        # state there is: an account exists and nothing identifies it. `CreateAccountAttempt`
        # refuses to hold "succeeded with no id" at all — deliberately, because such a record
        # is an assertion rather than an observation — so it is recorded as UNRESOLVED, which
        # authorizes no retry and sends the caller to reconciliation.
        status = CreateAccountStatus.UNKNOWN
    elif outcome is CallOutcome.FAILED and recorded_failure is not None:
        status = CreateAccountStatus.FAILED
        failure = recorded_failure
    elif outcome is CallOutcome.FAILED:
        # `FAILED` with no recorded reason. Whether a retry could ever succeed is decided
        # entirely by which reason AWS gave, so an unnamed one establishes nothing and must
        # not read as "nothing was created".
        status = CreateAccountStatus.UNKNOWN
    elif outcome is CallOutcome.ABSENT:
        # The provider positively reported no such account under this key, so the slot is
        # free. Nothing is recorded, which lets a fresh attempt proceed.
        return ledger
    else:
        # Committed intent with no outcome, or an explicit UNKNOWN. Either way a call may
        # have been made, and `assess_attempt` must see something.
        status = CreateAccountStatus.UNKNOWN

    ledger.record(
        type(attempt)(
            workspace_id=attempt.workspace_id,
            organization_id=attempt.organization_id,
            organizational_unit_id=attempt.organizational_unit_id,
            account_email=attempt.account_email,
            idempotency_key=attempt.idempotency_key,
            create_account_request_id=recorded_request_id,
            status=status,
            failure=failure,
            account_id=account_id,
        )
    )
    return ledger


def _settled_outcome(call: ProviderCallRecord) -> AccountCreationOutcome:
    """Read the durable row's settled outcome back into this module's vocabulary.

    The row is the authority once the call has been made: it holds the request id that
    recovery needs, and it survives the process. An outcome that is not a confirmed
    success reports `may_exist`, so a caller cannot mistake it for "nothing happened".
    """
    # Normalized before every comparison. Comparing the raw value with `is` reported a real
    # created account as `UNKNOWN` and discarded its id -- see `execution.as_outcome`.
    outcome = as_outcome(call.outcome)
    reference = call.provider_ref
    if outcome is CallOutcome.SUCCEEDED:
        account_id, _, request_id = _decode_reference(reference)
        return AccountCreationOutcome(
            status=CreateAccountStatus.SUCCEEDED,
            create_account_request_id=request_id,
            account_id=account_id,
            failure=None,
            detail="",
        )
    if outcome is CallOutcome.FAILED:
        _, failure, request_id = _decode_reference(reference)
        return AccountCreationOutcome(
            status=CreateAccountStatus.FAILED,
            create_account_request_id=request_id,
            account_id=None,
            failure=failure,
            detail="AWS established that no account was created",
        )
    _, _, request_id = _decode_reference(reference)
    return AccountCreationOutcome(
        status=CreateAccountStatus.UNKNOWN,
        create_account_request_id=request_id,
        account_id=None,
        failure=None,
        detail=(
            "the creation outcome was not established; an account may exist. Reconcile "
            "the recorded CreateAccount request id before considering any retry"
        ),
    )


# The durable store holds one free-text `provider_ref` per call, so the three facts this
# module needs to survive a restart share it under an explicit prefix scheme. Parsing is
# total: an unrecognised reference yields no facts rather than a wrong one.
_REF_ACCOUNT = "account="
_REF_REQUEST = "request="
_REF_FAILURE = "failure="


def encode_reference(
    *,
    request_id: str | None,
    account_id: str | None = None,
    failure: CreateAccountFailure | None = None,
) -> str:
    """Pack the facts worth surviving a crash into the row's single reference field."""
    parts = []
    if request_id:
        parts.append(f"{_REF_REQUEST}{request_id}")
    if account_id:
        parts.append(f"{_REF_ACCOUNT}{account_id}")
    if failure is not None:
        parts.append(f"{_REF_FAILURE}{failure.value}")
    return " ".join(parts)


def _decode_reference(
    reference: str | None,
) -> tuple[str | None, CreateAccountFailure | None, str | None]:
    """Read back what `encode_reference` wrote: `(account_id, failure, request_id)`."""
    if not reference:
        return None, None, None
    account_id = failure = request_id = None
    for token in reference.split():
        if token.startswith(_REF_ACCOUNT):
            account_id = token[len(_REF_ACCOUNT) :] or None
        elif token.startswith(_REF_REQUEST):
            request_id = token[len(_REF_REQUEST) :] or None
        elif token.startswith(_REF_FAILURE):
            try:
                failure = CreateAccountFailure(token[len(_REF_FAILURE) :])
            except ValueError:
                failure = None
    return account_id, failure, request_id


def creation_hook(
    credentials: CredentialSource,
    request: AccountFactoryRequest,
    *,
    outcomes: OutcomeVocabulary | None = None,
):
    """Build the provider hook the durable executor invokes for account creation.

    Returned as a closure because the executor's hook signature takes only the call record
    — the request and the credential source are composition, not call-site, concerns.

    The hook's contract, which is the whole point of this module:

    * It makes **one** `CreateAccount` call.
    * It returns the request id in the reference on **every** path, including failure and
      including an unobtained answer, because that id is what recovery needs.
    * `ProviderUnavailable` becomes `UNKNOWN`, never `FAILED`. An account may exist.

    `outcomes` is the `CallOutcome` class the executor validates this hook's answer against,
    passed in by the composer for the reason `execution.outcome_vocabulary` documents: the real
    executor `isinstance`-checks against **its own** enum and silently substitutes `UNKNOWN` for
    anything else. On this path that means a successfully created account reported as "an
    account may exist" with its id discarded — the precise failure this story exists to prevent.
    """
    outcomes = outcome_vocabulary(outcomes)

    async def hook(call: ProviderCallRecord):
        management = await credentials.management(operation_id=_operation_of(call))
        try:
            response = management.organizations.create_account(
                Email=request.account_email or "",
                AccountName=_account_name(request),
                Tags=[
                    {"Key": "adp:workspace", "Value": request.workspace_id},
                    {"Key": "adp:organization", "Value": request.organization_id},
                ],
            )
        except ProviderDenied as exc:
            # AWS refused before allocating anything. The only path allowed to report
            # FAILED, and even here a request id is preserved when one was returned.
            return (
                outcomes.FAILED,
                f"Organizations refused CreateAccount: {exc}",
                encode_reference(request_id=None),
            )
        except ProviderUnavailable as exc:
            # The answer was not obtained. An account may be opening right now, and there
            # is no request id to reconcile with, which is the worst case and must be
            # reported as exactly that rather than smoothed into a failure.
            return (
                outcomes.UNKNOWN,
                (f"CreateAccount did not return an answer: {exc}. An account may have been created; no request id was obtained to reconcile it"),
                encode_reference(request_id=None),
            )

        status = (response or {}).get("CreateAccountStatus") or {}
        observation = _observation_from_status(status)
        reference = encode_reference(
            request_id=observation.create_account_request_id,
            account_id=observation.account_id,
            failure=observation.failure,
        )
        if observation.status is CreateAccountStatus.SUCCEEDED and observation.account_id:
            return outcomes.SUCCEEDED, observation.detail or None, reference
        if observation.status is CreateAccountStatus.FAILED:
            return outcomes.FAILED, observation.detail or None, reference
        # IN_PROGRESS is the normal first reply. It is not success: the account is not
        # confirmed and has no id yet, so the row stays unresolved and reconciliation
        # finishes the job using the request id just recorded.
        return (
            outcomes.UNKNOWN,
            observation.detail or "CreateAccount accepted; the account is not yet confirmed",
            reference,
        )

    return hook


def _operation_of(call: ProviderCallRecord) -> str:
    """Use the durable operation identity, never parse a generation's call key."""
    operation_id = call.operation_id
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise CreationRefused("creation credentials require a durable operation identity")
    return operation_id


def reconciled_outcome(observation: CreateAccountObservation, outcomes: OutcomeVocabulary | None = None):
    """The store-vocabulary `(outcome, detail, provider_ref)` one observation settles to.

    The translation `reconcile_creation` persists, written separately so the mapping is
    readable on its own and so a test can assert it without a database.

    `IN_PROGRESS` deliberately has **no** settled form and is not handled here: AWS is still
    opening the account, so the row must stay `intended` and be swept again. Writing anything
    for it would close a call that has not finished — the one thing the durable row's whole
    purpose is to keep open.

    `SUCCEEDED` without an account id settles as `UNKNOWN`, not as success. A success whose
    reference cannot name the account is the worst row in the system: it would release the
    reservation and report the operation complete while nothing anywhere identifies the
    account that is billing.
    """
    vocabulary = outcome_vocabulary(outcomes)
    request_id = observation.create_account_request_id
    if observation.status is CreateAccountStatus.SUCCEEDED and observation.account_id:
        return (
            vocabulary.SUCCEEDED,
            observation.detail or f"reconciled: AWS confirmed account {observation.account_id}",
            encode_reference(request_id=request_id, account_id=observation.account_id),
        )
    if observation.status is CreateAccountStatus.FAILED and observation.failure is not None:
        return (
            vocabulary.FAILED,
            observation.detail or f"reconciled: AWS established no account was created ({observation.failure.value})",
            encode_reference(request_id=request_id, failure=observation.failure),
        )
    # Everything else: a failure whose reason AWS did not name, a success with no id, an
    # unreadable state. None of them establishes that nothing was created, so none may
    # release the reservation. `UNKNOWN` settles the row as `unresolved`, which is the state
    # that says a person has to look — and which the store then protects from being
    # overwritten by a later sweep.
    return (
        vocabulary.UNKNOWN,
        observation.detail or "reconciled: the creation outcome was not established and an account may exist",
        encode_reference(request_id=request_id),
    )


async def reconcile_creation(
    executor: DurableExecutor,
    credentials: CredentialSource,
    *,
    recorded: ProviderCallRecord,
    store: ReconciliationStore | None = None,
    outcomes: OutcomeVocabulary | None = None,
    refusal_types: tuple[type[BaseException], ...] = (),
) -> AccountCreationOutcome:
    """Settle an unresolved creation by asking AWS about the recorded request id.

    This is the recovery path, and the reason the request id is stored on every branch
    above. It asks about *that* request — it never issues a new `CreateAccount`, which is
    what distinguishes reconciliation from a retry.

    A row with no request id cannot be reconciled at all: the call may have been made and
    there is no handle to ask about. That is reported as an unresolved outcome needing an
    operator rather than being quietly retried.

    ## Reading the answer is only half of reconciliation

    `store` is where the answer is WRITTEN, and without it this function was a
    recomputation rather than a recovery: it returned a correct outcome and left the durable
    row in `intended`, so the next pass asked AWS again, the operation never converged on the
    account id, and a row naming a real billable account still said only "a call was
    intended". The observation also died with the process that made it.

    So a settleable answer is persisted through `ReconciliationStore.reconcile` — the
    harness's lease-free primitive, which is the right one precisely because the worker that
    made the call is gone. The outcome is resolved through `outcome_vocabulary` for the reason
    that function documents: the store validates against its OWN enum and this package holds
    only a copy.

    Two answers are deliberately not persisted:

    * `IN_PROGRESS` — AWS is still working. The row must stay open, so nothing is written and
      the caller is told to come back. Settling here would close a call that has not finished.
    * a `store` the composer did not supply — then this reads and reports only, and says so in
      the detail. Silence would let a composition that forgot to wire the store look exactly
      like one that converged.

    `refusal_types` names the store's own refusal, for the same structural reason
    `bootstrap_account` takes it. A refusal is a NORMAL outcome here: two recovery passes
    racing is expected, and the loser losing is the store doing its job. The observation is
    still reported, with the row's existing state left alone — including an `unresolved` row,
    which records a decision to involve a human and must not be overwritten on a timer.
    """
    if _operation_of(recorded) != executor.operation_id:
        raise CreationRefused("creation recovery record belongs to a different operation")
    _, _, request_id = _decode_reference(recorded.provider_ref)
    if not request_id:
        return AccountCreationOutcome(
            status=CreateAccountStatus.UNKNOWN,
            create_account_request_id=None,
            account_id=None,
            failure=None,
            detail=(
                "no CreateAccount request id was recorded, so this attempt cannot be "
                "reconciled. An account may exist and must be found by an operator "
                "before any retry; retrying now risks a second billable account"
            ),
        )
    management = await credentials.management(operation_id=executor.operation_id)
    try:
        response = management.organizations.describe_create_account_status(CreateAccountRequestId=request_id)
    except ProviderUnavailable as exc:
        # Nothing is written. The row stays `intended`, which is what keeps it in the
        # sweepable set: settling it `unresolved` here would take it out of automatic
        # recovery because a read failed once.
        return AccountCreationOutcome(
            status=CreateAccountStatus.UNKNOWN,
            create_account_request_id=request_id,
            account_id=None,
            failure=None,
            detail=(
                f"the creation status for {request_id} could not be read: {exc}. The "
                f"outcome stays unresolved; the request id remains the handle to retry "
                f"the read with"
            ),
        )
    status = (response or {}).get("CreateAccountStatus") or {}
    observation = _observation_from_status(status)
    if observation.create_account_request_id not in (None, request_id):
        return AccountCreationOutcome(
            status=CreateAccountStatus.UNKNOWN,
            create_account_request_id=request_id,
            account_id=None,
            failure=None,
            detail="AWS returned a different CreateAccount request identity; the durable row was not changed",
        )
    if observation.create_account_request_id is None:
        # Keep the id we asked with, so a response that omits it does not lose the handle.
        observation = CreateAccountObservation(
            status=observation.status,
            create_account_request_id=request_id,
            failure=observation.failure,
            account_id=observation.account_id,
            detail=observation.detail,
        )
    outcome = _outcome(observation)
    if observation.status is CreateAccountStatus.IN_PROGRESS:
        return outcome
    if store is None:
        return _with_detail(
            outcome,
            "this observation was NOT persisted: no reconciliation store was supplied, so "
            "the durable row is still awaiting reconciliation and this answer will have to "
            "be obtained again",
        )

    settled, detail, reference = reconciled_outcome(observation, outcomes)
    try:
        await store.reconcile(
            idempotency_key=recorded.idempotency_key,
            outcome=settled,
            detail=detail,
            provider_ref=reference,
        )
    except refusal_types as exc:
        return _with_detail(
            outcome,
            f"the durable row was not settled by this pass ({exc}). What AWS said is "
            f"reported above; the row's existing state stands, and an already-unresolved "
            f"row is left for the person it was recorded for",
        )
    return _with_detail(outcome, f"the durable row was settled as {getattr(settled, 'value', settled)!r}: {detail}")


def _with_detail(outcome: AccountCreationOutcome, note: str) -> AccountCreationOutcome:
    """The same outcome, with what happened to the durable row appended to its detail.

    The status is never touched. What AWS said and what the store did are separate facts, and
    an operator needs both: a confirmed account whose row could not be settled is a different
    situation from an unconfirmed one, and collapsing either into the other loses the action.
    """
    return AccountCreationOutcome(
        status=outcome.status,
        create_account_request_id=outcome.create_account_request_id,
        account_id=outcome.account_id,
        failure=outcome.failure,
        detail=f"{outcome.detail}; {note}" if outcome.detail else note,
    )


def creation_disposition(outcome: AccountCreationOutcome) -> AttemptDisposition:
    """The `account_factory` disposition this outcome corresponds to.

    Provided so `bootstrap_runner` and the recovery report speak one vocabulary rather than
    each re-deriving it. `AccountCreationError` is raised for a state that has no honest
    disposition, instead of defaulting to a permissive one.
    """
    if outcome.succeeded:
        return AttemptDisposition.ALREADY_CREATED
    if outcome.status is CreateAccountStatus.FAILED:
        # A property, NOT a method. Calling it raised `TypeError: 'bool' object is not
        # callable` for every established failure — so the one branch that decides whether a
        # failed creation may be retried crashed instead of deciding.
        if outcome.failure is not None and not outcome.failure.retry_can_succeed:
            return AttemptDisposition.REFUSED_INPUT_CANNOT_SUCCEED
        return AttemptDisposition.CREATE_PERMITTED
    if outcome.status is CreateAccountStatus.IN_PROGRESS:
        return AttemptDisposition.IN_FLIGHT
    if outcome.status is CreateAccountStatus.UNKNOWN:
        return AttemptDisposition.UNRESOLVED
    raise AccountCreationError(f"no disposition corresponds to a {outcome.status.value!r} outcome")
