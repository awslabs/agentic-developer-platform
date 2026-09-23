"""Governed AWS account creation attempts — Issue #5531 (w6-08), EPIC #4910.

## The defect this module exists to close

Opening an AWS account is asynchronous. `CreateAccount` does not return an account; it
returns a `CreateAccountStatus` id, and the outcome arrives later. The legacy flow applied an
`Account` custom resource and moved on, so there was no place to record that an attempt had
been made and no way to ask what became of it.

That has one specific, expensive consequence. When the answer is LOST — the reply never
arrives, the process handling it dies, a throttle response hides whether the call landed —
the only recovery move available is to ask again. Asking again when the first attempt in fact
succeeded opens a SECOND AWS account. Both cost money, both are real, and neither is clearly
the one the workspace belongs to. Closing the spare is not a cleanup: it is a 90-day
irreversible suspension of an account whose id cannot be reused in that window.

So this module inverts the default. An attempt is written down BEFORE the call, keyed so that
a repeat of the same logical request is recognisable as the same attempt, and a retry is
permitted only after the previous attempt's status has actually been read and established
absence. Nothing here retries, and nothing here calls AWS.

## Unknown is not failure

The distinction the whole module turns on, and the same one
`../../../contracts/superplane_contracts/reconciliation.py` draws with `ProviderPresence` and
`OperationState.UNKNOWN`: "the provider says this did not happen" and "I could not find out
what happened" are different facts with opposite safe actions.

* A provider-confirmed failure means nothing was created, so repeating is safe.
* An unreadable outcome means an account may exist that nobody is tracking. Repeating is the
  duplicate-spend bug; concluding failure and releasing the workspace is the leak.

`AttemptDisposition.UNRESOLVED` is therefore a terminal answer that authorizes NOTHING, and
`may_create_account` is false for it. A caller that asks that property instead of testing
`status != SUCCEEDED` cannot reproduce the blind-retry defect.

## What this module is not

It is offline, like the rest of this package: there is no path from here to a subprocess, a
socket, an AWS client or a Kubernetes client, enforced by
`tests/test_no_legacy_targets.py::test_the_module_cannot_fetch_execute_or_mutate_anything`.
It decides, from evidence a caller supplies, whether creating an account would be a first
attempt, a duplicate, or a permitted retry. Performing the creation is a separate, separately
authorized operation, and neither this code nor its merge authorizes one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum

from .modes import (
    AccountFactoryRequest,
    ModeError,
    OwnershipMode,
    ValidationAuthorization,
    ensure_valid,
)

__all__ = [
    "AccountCreationError",
    "AttemptDecision",
    "AttemptDisposition",
    "AttemptLedger",
    "CreateAccountAttempt",
    "CreateAccountFailure",
    "CreateAccountObservation",
    "CreateAccountStatus",
    "account_identity_key",
    "assess_attempt",
    "intended_attempt",
]


# AWS `CreateAccountStatus` ids look like `car-` followed by hex. Shape-checked so a blank or
# obviously-wrong value cannot be recorded as though the provider had answered.
_STATUS_ID_RE = re.compile(r"^car-[0-9a-zA-Z]{8,64}$")


class AccountCreationError(Exception):
    """A creation attempt was refused. No attempt is returned — the caller gets nothing."""


class CreateAccountStatus(str, Enum):
    """Where an AWS account-creation attempt is, as reported BY AWS.

    The first three mirror `CreateAccountStatus.State` in the Organizations API. `UNKNOWN` is
    ADP's and is deliberately **not** a fourth AWS state: it is the answer when AWS could not
    be consulted at all, which the API has no way to express because the API is the thing
    that did not answer.

    `str`-valued so a status survives YAML/JSON round-tripping as its wire value, matching
    `OwnershipMode` here and `OperationState` in the shared provisioning contract.
    """

    IN_PROGRESS = "in-progress"
    """AWS accepted the request and has not finished. Nothing may be concluded."""

    SUCCEEDED = "succeeded"
    """AWS reports the account was created. An account exists and costs money."""

    FAILED = "failed"
    """AWS reports the account was NOT created. Nothing was left behind."""

    UNKNOWN = "unknown"
    """AWS could not be consulted. NOT a failure — an account may exist unrecorded."""


# Statuses from which nothing may be concluded about whether an account exists. Named once so
# a caller cannot read "not succeeded" as "nothing was created".
INCONCLUSIVE_STATUSES: frozenset[CreateAccountStatus] = frozenset(
    {CreateAccountStatus.IN_PROGRESS, CreateAccountStatus.UNKNOWN}
)


class CreateAccountFailure(str, Enum):
    """Why AWS refused to create the account, for the reasons that change what to do next.

    Only a failure reason AWS actually returns is modelled, and only where the correct
    response DIFFERS. That is the point of enumerating them rather than keeping a free-text
    reason: two of these must never be retried with the same input, and retrying anyway is
    how a run burns its attempt budget against a request that cannot succeed.
    """

    EMAIL_ALREADY_EXISTS = "email-already-exists"
    """The address is already an AWS account's root user. Retrying cannot help: AWS requires
    a globally unique address per account, so the same input fails identically forever. This
    also means the address may belong to an account in ANOTHER organization, which is why it
    is reported rather than silently suffixed into a variant address."""

    ACCOUNT_LIMIT_EXCEEDED = "account-limit-exceeded"
    """The organization is at its account quota. Retrying the same request cannot help until
    the quota is raised or an account is removed — a human decision, not a backoff."""

    INVALID_EMAIL = "invalid-email"
    """AWS rejected the address itself. A different address is needed, not a retry."""

    CONCURRENT_ACCOUNT_MODIFICATION = "concurrent-account-modification"
    """Another account operation is in flight in this organization. This one genuinely is
    transient, and is the only failure here that a plain retry of the same input can fix."""

    INTERNAL_FAILURE = "internal-failure"
    """AWS reported an internal failure. Retryable, but it establishes that this attempt did
    not produce an account."""

    @property
    def retry_can_succeed(self) -> bool:
        """Whether repeating the SAME request could ever succeed.

        False means the input must change or a human must act first. Exposed as a property so
        the rule lives beside the reason rather than in each caller's own branch, which is
        where copies of it would eventually disagree.
        """
        return self in {
            CreateAccountFailure.CONCURRENT_ACCOUNT_MODIFICATION,
            CreateAccountFailure.INTERNAL_FAILURE,
        }


@dataclass(frozen=True)
class CreateAccountAttempt:
    """A durable record that ADP intended to open one specific AWS account.

    Written BEFORE the call, which is the property that makes recovery possible: a record
    created afterwards does not exist in exactly the case it is needed for, when the call
    happened and the answer was lost.

    `create_account_request_id` is therefore `None` on a freshly built attempt, for the same
    reason `ProviderHandle.provider_reference` is: it does not exist yet at the moment the
    record must be written, and a field that could only be filled in after the call would put
    the whole record after the call.

    `idempotency_key` is what makes a repeat of the SAME logical request distinguishable from
    a genuinely new one. It is derived from the request's identity rather than supplied,
    because a caller-chosen key that varied per run would make every retry look new — which
    is exactly the condition under which a retry opens a second account.
    """

    workspace_id: str
    organization_id: str
    organizational_unit_id: str
    account_email: str
    idempotency_key: str
    create_account_request_id: str | None = None
    status: CreateAccountStatus = CreateAccountStatus.IN_PROGRESS
    failure: CreateAccountFailure | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "workspace_id",
            "organization_id",
            "organizational_unit_id",
            "account_email",
            "idempotency_key",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise AccountCreationError(
                    f"{field_name} must be a non-empty string on a creation attempt: an "
                    f"attempt that cannot identify what it was trying to create cannot be "
                    f"reconciled later, which is the whole purpose of recording it"
                )
        if self.create_account_request_id is not None and not _STATUS_ID_RE.match(
            self.create_account_request_id
        ):
            # Blank or malformed is worse than absent: absent says "AWS has not answered
            # yet", while a malformed value reads as an answer that identifies nothing.
            raise AccountCreationError(
                f"create_account_request_id={self.create_account_request_id!r} is not an "
                f"AWS CreateAccountStatus id (expected car-xxxxxxxx). Absent is the correct "
                f"way to record that AWS has not answered"
            )
        if self.status is CreateAccountStatus.SUCCEEDED and not self.account_id:
            # "It succeeded" with no account id is not an observation; it is an assertion,
            # and it is unusable: the id is what a later closure record must match.
            raise AccountCreationError(
                "an attempt recorded as succeeded must carry the account id AWS created. "
                "Without it there is a paid-for account that nothing can identify"
            )
        if self.status is not CreateAccountStatus.SUCCEEDED and self.account_id:
            raise AccountCreationError(
                f"an attempt in status {self.status.value!r} must not carry an account id: "
                f"recording one would make an unconfirmed account look created"
            )
        if self.failure is not None and self.status is not CreateAccountStatus.FAILED:
            raise AccountCreationError(
                f"a failure reason belongs only to a failed attempt, not to status "
                f"{self.status.value!r}"
            )
        if self.status is CreateAccountStatus.FAILED and self.failure is None:
            # A failure with no reason cannot be acted on: whether a retry can ever succeed
            # is decided entirely by which reason AWS gave.
            raise AccountCreationError(
                "an attempt recorded as failed must carry the reason AWS gave. Whether "
                "repeating the request could ever succeed depends on which reason it was"
            )

    def observed(self, observation: CreateAccountObservation) -> CreateAccountAttempt:
        """Return a copy carrying what AWS reported. Never mutates the recorded attempt.

        A new value rather than an edit, for the same reason as
        `ProviderHandle.with_provider_reference`: the pre-call record is what a reconciliation
        after a crash will find, and it must not be retroactively rewritten into something
        that looks like it always knew the answer.

        Refuses an observation about a different request id. An observation can be entirely
        truthful and still be about another attempt, and adopting it would attach one
        account's outcome to another attempt's record.

        An observation carrying NO request id is not a conflicting one — it is an UNKNOWN,
        which by construction identifies nothing because AWS was never reached. In that case
        the recorded id is KEPT rather than overwritten with `None`: it is the only handle by
        which the outcome can be asked about later, and dropping it on the one read that
        failed would destroy the means of ever resolving the attempt.
        """
        observed_id = observation.create_account_request_id
        if (
            observed_id is not None
            and self.create_account_request_id is not None
            and observed_id != self.create_account_request_id
        ):
            raise AccountCreationError(
                f"observation identifies request {observed_id!r}, but this attempt recorded "
                f"{self.create_account_request_id!r}. Adopting it would attach another "
                f"attempt's outcome — possibly another account — to this record"
            )
        return CreateAccountAttempt(
            workspace_id=self.workspace_id,
            organization_id=self.organization_id,
            organizational_unit_id=self.organizational_unit_id,
            account_email=self.account_email,
            idempotency_key=self.idempotency_key,
            create_account_request_id=observed_id or self.create_account_request_id,
            status=observation.status,
            failure=observation.failure,
            account_id=observation.account_id,
        )

    @property
    def is_conclusive(self) -> bool:
        """Whether this attempt's outcome is settled either way."""
        return self.status not in INCONCLUSIVE_STATUSES

    def as_record(self) -> dict[str, str]:
        """The attempt as plain data, for persisting the request and status ids.

        Only strings, so the record survives YAML/JSON without a custom decoder. Absent
        fields are OMITTED rather than written as empty strings: an empty
        `create_account_request_id` would read as an answer identifying nothing, whereas its
        absence correctly says AWS has not answered.
        """
        record = {
            "workspace_id": self.workspace_id,
            "organization_id": self.organization_id,
            "organizational_unit_id": self.organizational_unit_id,
            "account_email": self.account_email,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
        }
        if self.create_account_request_id is not None:
            record["create_account_request_id"] = self.create_account_request_id
        if self.failure is not None:
            record["failure"] = self.failure.value
        if self.account_id is not None:
            record["account_id"] = self.account_id
        return record

    @classmethod
    def from_record(cls, data: object) -> CreateAccountAttempt:
        """Rebuild an attempt from persisted data, refusing anything unrecognised.

        Unknown keys are refused rather than ignored. A record written by a newer version may
        carry a field that changes what the attempt means, and silently dropping it would
        reconstruct a DIFFERENT attempt than the one that was stored — then decide a retry
        from it. Refusing is the safe failure here, because the caller can still read the
        stored record by hand.

        Every construction invariant applies, since this routes through `__init__`: a stored
        record that says "succeeded" with no account id is refused on load, not trusted.
        """
        if not isinstance(data, dict):
            raise AccountCreationError(
                f"a persisted creation attempt must be a mapping, not "
                f"{type(data).__name__}"
            )
        known = {
            "workspace_id",
            "organization_id",
            "organizational_unit_id",
            "account_email",
            "idempotency_key",
            "status",
            "create_account_request_id",
            "failure",
            "account_id",
        }
        unknown = sorted(set(data) - known)
        if unknown:
            raise AccountCreationError(
                f"persisted attempt has unrecognised field(s): {', '.join(unknown)}. "
                f"Ignoring them would rebuild a different attempt than the one stored and "
                f"then decide a retry from it"
            )
        missing = sorted(
            name
            for name in (
                "workspace_id",
                "organization_id",
                "organizational_unit_id",
                "account_email",
                "idempotency_key",
                "status",
            )
            if not data.get(name)
        )
        if missing:
            raise AccountCreationError(
                f"persisted attempt is missing required field(s): {', '.join(missing)}"
            )
        try:
            status = CreateAccountStatus(data["status"])
        except ValueError as exc:
            raise AccountCreationError(
                f"persisted attempt has status={data['status']!r}, which is not a known "
                f"creation status"
            ) from exc
        failure = None
        if data.get("failure") is not None:
            try:
                failure = CreateAccountFailure(data["failure"])
            except ValueError as exc:
                raise AccountCreationError(
                    f"persisted attempt has failure={data['failure']!r}, which is not a "
                    f"known failure reason. Whether a retry could succeed depends on it, so "
                    f"an unrecognised value is refused rather than guessed"
                ) from exc
        return cls(
            workspace_id=data["workspace_id"],
            organization_id=data["organization_id"],
            organizational_unit_id=data["organizational_unit_id"],
            account_email=data["account_email"],
            idempotency_key=data["idempotency_key"],
            create_account_request_id=data.get("create_account_request_id"),
            status=status,
            failure=failure,
            account_id=data.get("account_id"),
        )


@dataclass(frozen=True)
class CreateAccountObservation:
    """What AWS reported when its `CreateAccountStatus` was read.

    The provider-truth primitive this module's conclusions rest on, mirroring
    `ProviderObservation` in the shared reconciliation contract: every decision below is
    derived from one of these rather than from local state, so a report can be audited back
    to a provider answer instead of to a field somebody set.
    """

    status: CreateAccountStatus
    create_account_request_id: str | None = None
    failure: CreateAccountFailure | None = None
    account_id: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, CreateAccountStatus):
            raise AccountCreationError("status must be a CreateAccountStatus")
        if self.status is CreateAccountStatus.UNKNOWN:
            if self.account_id or self.failure is not None:
                raise AccountCreationError(
                    "an UNKNOWN observation cannot carry an account id or a failure "
                    "reason — AWS was not successfully consulted, so it reported neither"
                )
            if not self.detail.strip():
                # An unresolved observation blocks both retry and release, so why AWS could
                # not be read is the operator's entire starting point.
                raise AccountCreationError(
                    "an UNKNOWN observation must say why AWS could not be consulted"
                )
        if self.status is CreateAccountStatus.SUCCEEDED and not self.account_id:
            raise AccountCreationError(
                "a SUCCEEDED observation must carry the account id AWS created"
            )
        if self.status is CreateAccountStatus.FAILED and self.failure is None:
            raise AccountCreationError(
                "a FAILED observation must carry the failure reason AWS gave"
            )
        if (
            self.status is not CreateAccountStatus.UNKNOWN
            and self.create_account_request_id is None
        ):
            # AWS answering at all means there was a request id to answer about. An
            # answer with no id cannot be matched to the attempt it belongs to.
            raise AccountCreationError(
                f"a {self.status.value!r} observation must identify the "
                f"CreateAccountStatus id it is about; otherwise it cannot be matched to "
                f"the attempt that produced it"
            )

    @classmethod
    def unreadable(cls, detail: str) -> CreateAccountObservation:
        """The lost-answer case, named so a caller does not have to model it as a failure.

        This is the constructor for "the call may or may not have landed": a timeout, a
        throttle response that hides whether the request was accepted, a crash between the
        call and its answer. It produces `UNKNOWN`, which authorizes nothing.
        """
        return cls(status=CreateAccountStatus.UNKNOWN, detail=detail)


class AttemptDisposition(str, Enum):
    """Offline classification of recorded evidence; no value grants execution."""

    CREATE_PERMITTED = "create-permitted"
    """No prior attempt exists for this request. Creating is a first attempt."""

    RETRY_PERMITTED = "retry-permitted"
    """A prior attempt was read and AWS confirmed it created nothing.
    The durable runner must still authorize and commit a new generation."""

    ALREADY_CREATED = "already-created"
    """A prior attempt succeeded and its account exists. Creating again here is the
    duplicate-account bug: adopt the recorded account instead."""

    IN_FLIGHT = "in-flight"
    """A prior attempt is still running. Nothing may be concluded and nothing may be
    launched; wait and read the status again."""

    UNRESOLVED = "unresolved"
    """The prior attempt's outcome could not be established. An account may exist that
    nothing is tracking. Authorizes neither a retry nor releasing the workspace, and needs
    an operator."""

    REFUSED_INPUT_CANNOT_SUCCEED = "refused-input-cannot-succeed"
    """AWS refused for a reason that repeating the same request cannot fix — a taken email
    address, an exhausted account quota. The input must change or a human must act."""


@dataclass(frozen=True)
class AttemptDecision:
    """The conclusion, plus the evidence it rests on.

    `attempt` is the prior attempt the conclusion is about, `None` only for a genuine first
    attempt. `observation` is the AWS answer that produced the conclusion, and is `None`
    whenever no provider answer was involved — so a report can tell a decision grounded in
    provider truth from one grounded in the absence of a record.
    """

    disposition: AttemptDisposition
    reason: str
    attempt: CreateAccountAttempt | None = None
    observation: CreateAccountObservation | None = None

    @property
    def may_create_account(self) -> bool:
        """Offline observations never authorize an AWS effect.

        Only the maintained runner can validate an admitted lease, load durable
        history and commit a fresh generation before invoking CreateAccount.
        This legacy property remains fail-closed for old automation consumers.
        """
        return False

    @property
    def requires_durable_attempt(self) -> bool:
        """A candidate for the runner's authorization and durable generation fence."""
        return self.disposition in {
            AttemptDisposition.CREATE_PERMITTED,
            AttemptDisposition.RETRY_PERMITTED,
        }

    @property
    def account_unaccounted_for(self) -> bool:
        """True while an account may exist that nothing is tracking.

        Only `UNRESOLVED` qualifies: `ALREADY_CREATED` has an account, but it is recorded and
        therefore accounted for. This is what a retention report keys on, because an
        unaccounted account is the one that goes on costing money with nobody looking at it.
        """
        return self.disposition is AttemptDisposition.UNRESOLVED


def _derive_key(
    organization_id: str,
    organizational_unit_id: str,
    workspace_id: str,
    account_email: str,
) -> str:
    """The key derivation itself, over the four fields that decide WHICH account this is.

    Separate from `_idempotency_key` so a stored attempt can be re-derived from its own
    recorded fields, without needing the original request back.
    """
    material = "\x1f".join(
        (
            organization_id,
            organizational_unit_id,
            workspace_id,
            account_email.strip().lower(),
        )
    )
    return "afk-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def account_identity_key(request: AccountFactoryRequest) -> str:
    """The derived identity of the account a request is about — WHICH account, not what's in it.

    The public name for the derivation `_idempotency_key` performs, exported because two other
    places need to ask "are these about the same account?" and must not answer it with their
    own copy of the rule:

    * `bootstrap.bootstrap_plan` records it on the plan, so a plan carries all four identity
      fields rather than only the workspace and organization;
    * `recovery.recovery_report` compares a plan's key against a creation attempt's, which is
      what stops one account's step observations being reported as proof about another.

    Same fields and same derivation as the idempotency key, deliberately: a second spelling of
    "which account is this?" is a second thing to keep in step, and the two disagreeing is
    precisely the class of bug this is here to catch.

    Usable on any mode. The adopted modes have no OU or address, so their key derives from the
    two fields they do have — which still distinguishes workspaces and organizations from each
    other, and is what the comparison needs.
    """
    return _idempotency_key(request)


def _idempotency_key(request: AccountFactoryRequest) -> str:
    """Derive the key that makes a repeat of this logical request recognisable.

    Over the fields that decide WHICH account is being created — the organization, the
    placement, the workspace it is for and the address it gets. Two calls agreeing on all
    four are the same logical request, and the second must be fenced rather than opening a
    second account.

    Deliberately excludes the cluster inputs (VPC CIDR, instance type, cluster version).
    Those describe what gets built INSIDE the account, so including them would make a
    corrected CIDR look like a different account request — and the retry after that
    correction would open a second account, which is the precise failure this key prevents.

    A hash rather than the joined values, so the key can be stored and compared without
    carrying the contact address around in every log line that mentions it.
    """
    return _derive_key(
        request.organization_id,
        request.organizational_unit_id or "",
        request.workspace_id,
        request.account_email or "",
    )


def intended_attempt(
    request: AccountFactoryRequest,
    authorization: ValidationAuthorization | None = None,
) -> CreateAccountAttempt:
    """Build the record to persist BEFORE calling CreateAccount.

    Validates first, so an attempt cannot be recorded for a request that would be refused —
    a persisted attempt for an unauthorized workspace would later look like evidence that
    ADP had legitimately tried to open that account.

    Refuses any mode that does not create an account. This is the "named mode, never a side
    effect" rule from the issue's Design item 3: adopting an existing account must not be
    able to produce a creation attempt, because the request that reaches here is the only
    thing standing between an onboarding flow and an unexpected new AWS bill.
    """
    if request.mode is not OwnershipMode.NEW_ACCOUNT_MANAGED:
        raise AccountCreationError(
            f"refusing to record an account-creation attempt for mode "
            f"{request.mode.value!r}: only {OwnershipMode.NEW_ACCOUNT_MANAGED.value} creates "
            f"an account. Creating one must be a named mode, never a side effect of "
            f"onboarding an account that already exists"
        )
    try:
        ensure_valid(request, authorization)
    except ModeError as exc:
        raise AccountCreationError(
            f"refusing to record a creation attempt for a request that does not validate: "
            f"{exc}"
        ) from exc

    return CreateAccountAttempt(
        workspace_id=request.workspace_id,
        organization_id=request.organization_id,
        # Guaranteed present for this mode by validation; the attempt records the placement
        # so a reconciliation can tell whether the account it finds went where it was meant to.
        organizational_unit_id=request.organizational_unit_id or "",
        account_email=request.account_email or "",
        idempotency_key=_idempotency_key(request),
        status=CreateAccountStatus.IN_PROGRESS,
    )


@dataclass
class AttemptLedger:
    """The prior attempts known for a workspace, keyed by idempotency key.

    A deliberately small in-memory stand-in for whatever holds provisioning state, in the
    same spirit as `cleanup.ProvisionedAccountRecord`: this module is offline and owns no
    database. What matters for review is the DECISION rule, which is `assess_attempt` and is
    independent of where the records are stored.

    `record` refuses to overwrite a conclusive attempt with a different one. That refusal is
    the fence: losing a succeeded attempt is losing the only pointer to a real account.
    """

    attempts: dict[str, CreateAccountAttempt] = field(default_factory=dict)

    def record(self, attempt: CreateAccountAttempt) -> None:
        for recorded in self.attempts.values():
            if (recorded.organization_id, recorded.workspace_id) == (
                attempt.organization_id,
                attempt.workspace_id,
            ) and recorded.idempotency_key != attempt.idempotency_key:
                raise AccountCreationError(
                    "the workspace already has an account attempt with a different approved payload"
                )
        existing = self.attempts.get(attempt.idempotency_key)
        if (
            existing is not None
            and existing.status is CreateAccountStatus.SUCCEEDED
            and attempt.account_id != existing.account_id
        ):
            raise AccountCreationError(
                f"refusing to overwrite the succeeded attempt for workspace "
                f"{existing.workspace_id!r}, which recorded account "
                f"{existing.account_id!r}. That record is the only thing identifying a real "
                f"account, and replacing it would leave the account paid for and untracked"
            )
        self.attempts[attempt.idempotency_key] = attempt

    def find(self, request: AccountFactoryRequest) -> CreateAccountAttempt | None:
        """The prior attempt for this logical request, if any."""
        matches = [
            attempt
            for attempt in self.attempts.values()
            if (attempt.organization_id, attempt.workspace_id)
            == (request.organization_id, request.workspace_id)
        ]
        if len(matches) > 1:
            raise AccountCreationError(
                "ambiguous account attempt history for this workspace"
            )
        if matches and matches[0].idempotency_key != _idempotency_key(request):
            raise AccountCreationError(
                "the workspace account attempt has a different approved payload; changing email or OU cannot bypass its fence"
            )
        return matches[0] if matches else None

    def as_records(self) -> list[dict[str, str]]:
        """The ledger as plain data, ordered by key so a stored file diffs cleanly."""
        return [self.attempts[key].as_record() for key in sorted(self.attempts)]

    @classmethod
    def from_records(cls, data: object) -> AttemptLedger:
        """Rebuild a ledger from persisted records, refusing a malformed or ambiguous store.

        A record whose stored key does not match the key its own fields derive to is refused.
        Such a record would be findable under one key and fence under another, so the fence
        would silently not engage — the duplicate-account failure, reintroduced through the
        store rather than through the decision.
        """
        if data is None:
            return cls()
        if not isinstance(data, list):
            raise AccountCreationError(
                f"a persisted attempt ledger must be a list of records, not "
                f"{type(data).__name__}"
            )
        ledger = cls()
        for entry in data:
            attempt = CreateAccountAttempt.from_record(entry)
            derived = _derive_key(
                attempt.organization_id,
                attempt.organizational_unit_id,
                attempt.workspace_id,
                attempt.account_email,
            )
            if derived != attempt.idempotency_key:
                raise AccountCreationError(
                    f"persisted attempt for workspace {attempt.workspace_id!r} is stored "
                    f"under key {attempt.idempotency_key!r} but its own fields derive to "
                    f"{derived!r}. It would be findable under one key and fence under "
                    f"another, so the duplicate-account fence would silently not engage"
                )
            if attempt.idempotency_key in ledger.attempts:
                raise AccountCreationError(
                    f"the persisted ledger holds two attempts under the same key "
                    f"{attempt.idempotency_key!r}. Which one is authoritative decides "
                    f"whether an account already exists, so this is refused rather than "
                    f"resolved by load order"
                )
            ledger.record(attempt)
        return ledger


def assess_attempt(
    request: AccountFactoryRequest,
    ledger: AttemptLedger,
    observation: CreateAccountObservation | None = None,
    authorization: ValidationAuthorization | None = None,
) -> AttemptDecision:
    """Decide whether CreateAccount may be called for this request, and why.

    The order of the checks is the safety property, so it is stated rather than left to be
    inferred:

    1. **Validate, and refuse a non-creating mode.** A decision made for an unauthorized or
       adopted-account request could authorize creating an account nobody asked for.
    2. **No prior attempt → a first attempt is permitted.** Nothing can be duplicated.
    3. **A prior attempt that is already conclusive is answered from its record.** A
       succeeded attempt yields `ALREADY_CREATED`, never a retry: this is the branch whose
       absence opens the second account.
    4. **An inconclusive prior attempt requires a fresh observation.** With none, the answer
       is `UNRESOLVED` — which authorizes nothing. This is the refusal that matters: a caller
       holding no AWS answer must not receive a default that permits the duplicate call.

    Passing an observation for an already-conclusive attempt is refused rather than applied,
    because reopening a settled outcome is how a succeeded attempt gets talked back into a
    retry.
    """
    if request.mode is not OwnershipMode.NEW_ACCOUNT_MANAGED:
        raise AccountCreationError(
            f"refusing to assess account creation for mode {request.mode.value!r}: only "
            f"{OwnershipMode.NEW_ACCOUNT_MANAGED.value} creates an account"
        )
    try:
        ensure_valid(request, authorization)
    except ModeError as exc:
        raise AccountCreationError(
            f"refusing to assess account creation for a request that does not validate: "
            f"{exc}"
        ) from exc

    prior = ledger.find(request)

    if prior is None:
        if observation is not None:
            # An observation with no attempt to attach it to means the record that should
            # have been written before the call is missing. Treating it as a first attempt
            # would authorize a call while an unrecorded one may already have run.
            return AttemptDecision(
                disposition=AttemptDisposition.UNRESOLVED,
                reason=(
                    "an AWS answer exists for this request but no attempt was recorded for "
                    "it. The pre-call record is missing, so whether a call already ran "
                    "cannot be established, and creating now could open a second account"
                ),
                observation=observation,
            )
        return AttemptDecision(
            disposition=AttemptDisposition.CREATE_PERMITTED,
            reason=(
                "no prior creation attempt is recorded for this request, so creating one "
                "cannot duplicate an existing account"
            ),
        )

    if prior.is_conclusive:
        if observation is not None:
            raise AccountCreationError(
                f"refusing to apply a new observation to an attempt already concluded as "
                f"{prior.status.value!r}. A settled outcome is not re-decided: re-opening a "
                f"succeeded attempt is how a real account gets created twice"
            )
        return _decide_from_conclusive(prior)

    if observation is None:
        return AttemptDecision(
            disposition=AttemptDisposition.UNRESOLVED,
            reason=(
                f"a prior attempt is recorded in status {prior.status.value!r} and no AWS "
                f"answer was supplied. Repeating the request here would risk opening a "
                f"second account for one that may already exist"
            ),
            attempt=prior,
        )

    updated = prior.observed(observation)
    if updated.status is CreateAccountStatus.IN_PROGRESS:
        return AttemptDecision(
            disposition=AttemptDisposition.IN_FLIGHT,
            reason=(
                "AWS reports the earlier request is still in progress. It is neither a "
                "failure nor an account yet, so nothing may be launched and nothing may be "
                "released; read the status again"
            ),
            attempt=updated,
            observation=observation,
        )
    if updated.status is CreateAccountStatus.UNKNOWN:
        return AttemptDecision(
            disposition=AttemptDisposition.UNRESOLVED,
            reason=(
                f"AWS could not be consulted about the earlier request "
                f"({observation.detail}). This is not a failure: an account may exist that "
                f"nothing is tracking, so neither a retry nor releasing the workspace is "
                f"authorized"
            ),
            attempt=updated,
            observation=observation,
        )
    decision = _decide_from_conclusive(updated)
    return AttemptDecision(
        disposition=decision.disposition,
        reason=decision.reason,
        attempt=updated,
        observation=observation,
    )


def _decide_from_conclusive(attempt: CreateAccountAttempt) -> AttemptDecision:
    """Turn a settled attempt into a disposition. Never returns a permitting one for success."""
    if attempt.status is CreateAccountStatus.SUCCEEDED:
        return AttemptDecision(
            disposition=AttemptDisposition.ALREADY_CREATED,
            reason=(
                f"the earlier request already created account {attempt.account_id} for this "
                f"workspace. Adopt it; calling CreateAccount again would open a second "
                f"account, and removing the spare is a 90-day irreversible suspension"
            ),
            attempt=attempt,
        )

    failure = attempt.failure
    if failure is None:
        # Unreachable while FAILED is the only other conclusive status, since the attempt
        # invariant requires a reason for it. Stated rather than assumed: if a future status
        # is added, this refuses instead of silently permitting a retry.
        raise AccountCreationError(
            f"cannot decide what to do about an attempt in status {attempt.status.value!r} "
            f"with no failure reason recorded"
        )
    if not failure.retry_can_succeed:
        return AttemptDecision(
            disposition=AttemptDisposition.REFUSED_INPUT_CANNOT_SUCCEED,
            reason=(
                f"AWS refused the earlier request: {failure.value}. Repeating the same "
                f"request cannot succeed — the input must change or the organization's "
                f"quota must be raised first. Retrying here would only spend attempts"
            ),
            attempt=attempt,
        )
    return AttemptDecision(
        disposition=AttemptDisposition.RETRY_PERMITTED,
        reason=(
            f"AWS confirmed the earlier request failed ({failure.value}) and created no "
            f"account, so repeating it cannot duplicate anything"
        ),
        attempt=attempt,
    )
