"""HITL ticket contract v1 — the normative validator (issue #4178).

A HITL ticket is the payload an agent uses to ask a human a question it cannot
answer itself, and the answer that comes back. This module IS the contract: the
golden fixture in this directory is validated against these models by
`.github/workflows/hitl-ticket-contract-tests.yml`, so a change here that the
fixture does not follow fails CI.

Why this exists (see README.md in this directory for the full argument):

ADP has two human-approval implementations that disagree. `skill-agent.ts`
polls for 30 minutes and then denies; `ApprovalService.ts` loops `while (true)`
with no timeout at all, and both treat any comment containing the substring
"/approve" from *any* author as consent. That divergence is not stylistic — an
unbounded wait satisfiable by an unauthorized commenter is a different security
posture from a bounded, fail-closed one.

The vocabulary below is the fix. Four results, not two, because "nobody could be
asked" (`unavailable`) is a materially different outcome from "a human said no"
(`rejected`), and a consumer that cannot tell them apart cannot distinguish a
transport failure from a denial.

Scope: this contract describes the *payload*. ADP's durable mechanism — the
AIDLC gate, where the worker commits state, posts a `<!-- aidlc-gate:<stage> -->`
marker comment, and exits holding zero compute while a later dispatch mints a
synthetic human turn — is explicitly unchanged. The ticket is what flows through
that gate, not a replacement for it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_NAME = "hitl-ticket"
CONTRACT_VERSION = 1
CONTRACT_OWNER = "harness/hitl"


class HitlResult(StrEnum):
    """The closed, fail-closed answer vocabulary.

    ``StrEnum``, not ``(str, Enum)``: both appear in this repo, but only StrEnum
    makes ``f"{result}"`` render the wire value. Under ``(str, Enum)`` it renders
    ``'HitlResult.REJECTED'``, so a log line, an error message, or a hand-rolled
    payload would carry a value no consumer's vocabulary contains. For a contract
    whose whole point is a closed set of wire strings, that is a real hazard, and
    `test_hitl_ticket_contract.py` pins the behaviour.

    Borrowed from the DeepSeek Harness permission-result set (see
    `docs/research/deepseek-harness-fit-assessment.md` §Q2). Closed on purpose:
    a typo in a result value must be a validation error, never a silent permit.

    There is deliberately no ``allowed-always``. A persistent grant is a policy
    decision with its own audit and revocation story — it is not an answer to a
    question, and v1 has no escape hatch for one.
    """

    ALLOWED_ONCE = "allowed-once"
    """Approval, scoped to exactly this ticket. Non-durable by construction.

    The ONLY value that means "proceed".
    """

    REJECTED = "rejected"
    """A human, whose identity is recorded, said no."""

    CANCELLED = "cancelled"
    """The asking side withdrew the request before it was answered."""

    UNAVAILABLE = "unavailable"
    """Nobody could be asked, or the ask failed in transport.

    NOT a denial by a human. Consumers must be able to tell this apart from
    ``rejected`` — conflating them is the defect this contract exists to fix.
    A retry may be appropriate here; it is not appropriate for ``rejected``.
    """


#: Every result other than ``allowed-once``. Absence of a response is also
#: non-permissive, but the contract cannot represent absence — see invariant 1
#: in this directory's README, which states it as a consumer obligation.
NON_PERMISSIVE_RESULTS: frozenset[HitlResult] = frozenset(
    r for r in HitlResult if r is not HitlResult.ALLOWED_ONCE
)

#: Results a system can produce regardless of what the ticket offered in
#: ``options``. "Nobody could be asked" and "the asker withdrew" are facts about
#: the world, not choices a ticket grants — a ticket cannot opt out of them by
#: omitting them, so they are always valid answers. Both are non-permissive, so
#: this set can never widen what is allowed to proceed.
SYSTEM_RESULTS: frozenset[HitlResult] = frozenset({HitlResult.CANCELLED, HitlResult.UNAVAILABLE})


class HitlScope(StrEnum):
    """What class of decision is being escalated.

    Closed so a consumer can route and audit by scope without string matching
    on free text.
    """

    GATE_STAGE = "gate-stage"
    """An AIDLC gate stage boundary awaiting human sign-off."""

    TOOL_USE = "tool-use"
    """Permission to invoke a specific tool or capability."""

    SPEND = "spend"
    """Authorization to incur cost above a threshold."""

    DESTRUCTIVE_ACTION = "destructive-action"
    """An irreversible or hard-to-reverse change (delete, force-push, destroy)."""


class ApproverMode(StrEnum):
    """How the set of permitted answerers is described."""

    ANY_MAINTAINER = "any-maintainer"
    """Any principal the consumer considers a maintainer of the target repo."""

    NAMED = "named"
    """Only the identities listed in ``approvers.identities``."""


class TicketTimeout(BaseModel):
    """When the ask expires, and what that expiry means.

    Timeout expiry produces a denial-shaped result, never an approval. Left
    unspecified, two implementations pick opposite defaults — which is exactly
    what happened in ADP's two existing paths.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    expires_at: datetime = Field(
        description="Absolute deadline (timezone-aware ISO-8601). Relative "
        "durations are excluded on purpose: a duration is ambiguous across the "
        "process restart that a durable ticket is designed to survive."
    )
    on_expiry: HitlResult = Field(
        description="The result a consumer must synthesize when the deadline "
        "passes unanswered. Constrained to non-permissive values."
    )

    @field_validator("expires_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        # A naive datetime silently means "whatever the reader's clock says",
        # and a ticket outlives the process that wrote it, so the reader is a
        # different machine than the writer.
        if value.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return value

    @field_validator("on_expiry")
    @classmethod
    def _expiry_must_not_permit(cls, value: HitlResult) -> HitlResult:
        if value not in NON_PERMISSIVE_RESULTS:
            raise ValueError(
                f"timeout.on_expiry must be non-permissive, got {value.value!r}; "
                "a timeout must never approve"
            )
        return value


class TicketApprovers(BaseModel):
    """Who may answer this ticket.

    This is the authorization predicate. The contract carries it; it cannot
    enforce it. A consumer MUST check the answering identity against this
    object before treating a response as ``allowed-once`` — see invariant 4.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    mode: ApproverMode
    identities: list[str] = Field(
        default_factory=list,
        description="Principal identifiers permitted to answer. Required and "
        "non-empty when mode is 'named'; ignored otherwise.",
    )

    @model_validator(mode="after")
    def _named_requires_identities(self) -> TicketApprovers:
        # A ticket with mode='named' and no identities is approvable by nobody
        # yet not obviously broken at a glance — it would sit until timeout and
        # look like human indifference rather than a malformed ask.
        if self.mode is ApproverMode.NAMED and not self.identities:
            raise ValueError(
                "approvers.identities must be non-empty when mode is 'named'; "
                "otherwise the ticket can never be approved by anyone"
            )
        if any(not identity.strip() for identity in self.identities):
            raise ValueError("approvers.identities entries must be non-empty strings")
        return self


class TicketContext(BaseModel):
    """Provenance for audit, and the scoping keys a consumer needs.

    Open (``extra='allow'``) unlike every other model here: this is the audit
    sidecar, and a surface that carries one more identifier should not have to
    bump the contract version to say so. The identifying fields below are the
    ones a multi-tenant consumer needs to scope a ticket.
    """

    model_config = ConfigDict(extra="allow")

    tenant_id: str | None = None
    org_id: str | None = None
    repo: str | None = None
    issue_number: int | None = None
    invocation_id: str | None = None
    correlation_id: str | None = None


class ContractEnvelope(BaseModel):
    """The `name` / `version` / `owner` substrate every harness contract carries.

    Per `modules/harness/contracts/README.md` — the minimum the harness needs to
    register, dedupe, and route. Pinned to literal values here so a document
    from a different contract, or a future v2, cannot validate as a v1 document.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    name: str = Field(description=f"Contract name. Must be {CONTRACT_NAME!r}.")
    version: int = Field(description="Contract version. 1 for this contract.")
    owner: str = Field(description=f"Owning surface. Must be {CONTRACT_OWNER!r}.")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if value != CONTRACT_NAME:
            raise ValueError(f"name must be {CONTRACT_NAME!r}, got {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        if value != CONTRACT_VERSION:
            raise ValueError(
                f"version must be {CONTRACT_VERSION} for this contract, got {value!r}; "
                "a different version is a different contract file"
            )
        return value

    @field_validator("owner")
    @classmethod
    def _check_owner(cls, value: str) -> str:
        if value != CONTRACT_OWNER:
            raise ValueError(f"owner must be {CONTRACT_OWNER!r}, got {value!r}")
        return value


class HitlTicket(ContractEnvelope):
    """The request: an agent asking a human a question.

    ``ticket_id`` is the named target an answer must reference. Named rather
    than positional intents are the second borrow from dsh: with positional
    approval, a human looking at request X can have their approval applied to
    request Y that arrived in the meantime.
    """

    ticket_id: str = Field(
        min_length=1,
        description="Stable, unique identifier for this ask. The named target a "
        "response must reference.",
    )
    scope: HitlScope
    prompt: str = Field(
        min_length=1,
        description="The human-readable question. Must stand alone: the reader "
        "may see it in a notification with no surrounding conversation.",
    )
    options: list[HitlResult] = Field(
        description="The answers a HUMAN may choose for this ticket, drawn from "
        "the vocabulary. A subset — e.g. an informational gate may omit "
        "'allowed-once' entirely. The system results ('cancelled', "
        "'unavailable') are always valid regardless of this list; see "
        "SYSTEM_RESULTS."
    )
    timeout: TicketTimeout
    approvers: TicketApprovers
    context: TicketContext | None = None

    @field_validator("options")
    @classmethod
    def _options_non_empty_and_unique(cls, value: list[HitlResult]) -> list[HitlResult]:
        if not value:
            raise ValueError("options must list at least one permitted result")
        if len(set(value)) != len(value):
            raise ValueError("options must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _expiry_must_be_offered(self) -> HitlTicket:
        # If on_expiry names a human choice the ticket does not offer, the answer
        # synthesized at timeout is not a valid answer to this ticket. System
        # results are exempt: they are always valid (see SYSTEM_RESULTS), so
        # expiring as 'cancelled' needs no entry in options.
        if (
            self.timeout.on_expiry not in self.options
            and self.timeout.on_expiry not in SYSTEM_RESULTS
        ):
            raise ValueError(
                f"timeout.on_expiry {self.timeout.on_expiry.value!r} must also appear "
                "in options; the synthesized expiry answer must be a valid answer to "
                "this ticket"
            )
        return self


class HitlResponse(ContractEnvelope):
    """The answer: a result bound to the ticket it answers.

    ``ticket_id`` is required. A response without one is invalid — that is the
    named-not-positional rule, and it is the single field that prevents an
    approval of X from authorizing Y.

    ``answered_by`` is required for every result, including ``unavailable`` and
    ``cancelled``, where the answerer is a system rather than a human. Recording
    identity unconditionally is what makes invariant 4 checkable: a consumer can
    always ask "was this principal permitted to answer this ticket?"
    """

    ticket_id: str = Field(
        min_length=1,
        description="The ticket this answers. REQUIRED — see invariant 2.",
    )
    result: HitlResult
    answered_by: str = Field(
        min_length=1,
        description="Identity of the answering principal. For 'unavailable' and "
        "'cancelled' this is the system component that produced the result "
        "(e.g. 'system:timeout'), not a human.",
    )
    answered_at: datetime = Field(description="When the answer was produced (tz-aware).")
    note: str | None = Field(
        default=None,
        description="Optional free text — rejection rationale, or why the ask "
        "was unavailable. Never load-bearing: a consumer must decide from "
        "'result' alone.",
    )

    @field_validator("answered_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("answered_at must be timezone-aware")
        return value

    @property
    def is_permissive(self) -> bool:
        """True only for ``allowed-once``.

        The single place a consumer should ask "may I proceed?". Comparing
        against a denial list instead of this property is how a newly added
        result value becomes an accidental permit.
        """
        return self.result is HitlResult.ALLOWED_ONCE


def answers_ticket(ticket: HitlTicket, response: HitlResponse) -> bool:
    """Whether ``response`` is a structurally valid answer to ``ticket``.

    Checks the binding (invariant 2) and that the result is one the ticket
    offered — or a system result, which is always valid. Does NOT check approver
    identity: that requires the consumer's own notion of principals and
    maintainership, and is stated as an obligation in invariant 4 rather than
    pretended-at here. A ``True`` return is therefore necessary but NOT
    sufficient to proceed.
    """
    if response.ticket_id != ticket.ticket_id:
        return False
    return response.result in ticket.options or response.result in SYSTEM_RESULTS
