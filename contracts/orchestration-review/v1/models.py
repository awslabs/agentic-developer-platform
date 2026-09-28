"""Orchestration review-result contract v1 — the normative validator (issue #5146).

A review result is what a reviewer run produces about a pull request: which exact
commit it read, what it found, which stages of review it actually completed, and
whether its verdict was recorded where repository rules can see it. This module IS
the contract — the golden fixture beside it is validated against these models, so a
change here that the fixture does not follow fails CI, and vice versa.

Why this exists
---------------

"The reviewer run succeeded" is not evidence that the current change was reviewed.
Every observation recorded on #5146 is a variant of that same gap:

* A run reproduced a real correctness defect, then published only a security report
  saying nothing was at or above its reporting threshold — the functional blocker
  stayed inside the execution trace and never reached a consumable artifact.
* A run published functional prose saying APPROVE and a separate security report,
  and still recorded no verdict against the commit under review.
* A run analysed one commit, published its findings, and the provider attached the
  result to a *different* commit that the run never inspected.
* A run claimed every enumerated blocker was fixed having re-tested only one of the
  sub-behaviours the blocker named.

Those are four failure modes with one shape: prose and process success are not
bound to a revision, so nothing downstream can tell complete review evidence from
incomplete review evidence. This contract is the written answer. Three of its rules
carry the whole argument:

1. **A verdict is bound to one commit.** ``reviewed_head_sha`` is required, and
   findings and test evidence are scoped to it. When the pull request's head moves,
   `invalidate_for_head` exists to say what survives (the observation that a
   finding was once seen) and what does not (its disposition, and any test result
   tied to the old commit).

2. **A stage that did not run cannot be silently absent.** ``stages`` must describe
   the functional stage explicitly, including when its outcome is ``not-run`` or
   ``failed``. An omitted stage is a validation error, not an implied pass — that
   is the difference between "reviewed and clean" and "only the security scanner
   ran".

3. **Approval is computed, never asserted.** `approval_blockers` is the only
   supported way to ask "is this evidence good enough to approve on?", and it
   returns reasons rather than a boolean so a caller cannot lose them. A verdict
   field alone would let clean security prose and a successful process exit read as
   approval, which is exactly what happened.

Scope
-----

This contract describes the *artifact*. It does not dispatch repair, decide merge
eligibility, or substitute for GitHub's own independent-approval requirement.
`reviewer_run_id != author_run_id` is enforced here because self-review is a
property of the artifact, but satisfying it is necessary and **not** sufficient: a
provider-side approving review from a non-author is a separate requirement that
`modules/gateway/src/orchestration/pr_bindings.py` owns and this contract cannot
discharge.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

CONTRACT_NAME = "orchestration-review"
CONTRACT_VERSION = 1
CONTRACT_OWNER = "orchestration/review"

#: Every protocol/identity integer on the wire: contract version, cycle, claim
#: generation, accepted-plan version, provider repository id and PR number.
#:
#: `StrictInt`, not `int`, and the distinction is the whole point. Pydantic's
#: lax `int` accepts JSON `true` and coerces it to `1` — so a document whose
#: `claim_generation` is `true` validates as generation 1, and a
#: `provider_repository_id` of `true` becomes repository 1. These are exact
#: identity and fence values; being off by "whatever `bool` casts to" means
#: binding evidence to the wrong repository or passing a claim fence that was
#: never issued. `"1"` and `1.0` are refused for the same reason: a producer
#: that cannot emit a JSON integer here has a serialization bug, and silently
#: repairing it hides the bug until it reaches identity comparison.
#:
#: This must be the *field type* rather than an `@field_validator`. A validator
#: runs after coercion and is handed an already-converted `1`, with no way to
#: learn the input was `true` — the receipt-version defect repaired in #5144.
WireInt = StrictInt

#: A provider commit id. 40 hex for SHA-1, 64 for SHA-256, matching the pattern
#: `orchestration/pr_identity.py` already validates provider heads against. Pinned
#: as a constant because the producer, the consumer and the fixture must agree on
#: it, and a looser pattern in any one of them admits a head no provider issued.
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

#: `owner/name`, the provider's mutable display path. Immutable identity lives in
#: `ReviewRepository.provider_repository_id`; this is for humans and logs.
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ReviewStageName(StrEnum):
    """The review stages this contract can describe.

    Closed, and deliberately short. The distinction that matters is that these are
    *separate* stages with independent outcomes: a security scanner reporting
    nothing above its threshold says nothing about whether functional review
    happened, and the recorded observations show that conflating them is how a
    functional blocker gets dropped.
    """

    FUNCTIONAL = "functional"
    """Correctness, scope and behaviour review of the change itself."""

    SECURITY = "security"
    """Vulnerability review against the scanner's reporting threshold."""


class StageOutcome(StrEnum):
    """Whether a stage ran, and if it did, whether it reached a conclusion.

    ``not-run`` and ``failed`` are separate values for the same reason
    `hitl-ticket`'s ``unavailable`` is separate from ``rejected``: "nobody looked"
    and "looking broke" are different facts, and a consumer that cannot tell them
    apart cannot tell a skipped stage from a crashed one. Neither is a pass.
    """

    NOT_RUN = "not-run"
    """The stage was never attempted. Not a pass."""

    FAILED = "failed"
    """The stage was attempted and could not conclude. Not a pass, and not a
    finding — the review itself did not complete."""

    COMPLETED = "completed"
    """The stage ran and reached a conclusion. The ONLY value that means the stage
    contributed evidence. It does not mean the stage found nothing."""


#: The functional stage must be `completed` for a result to be approval-capable.
#: Written as a frozenset of the *accepted* value rather than a check against the
#: non-accepted ones, so adding a future outcome cannot accidentally widen what
#: counts as a completed stage.
CONCLUSIVE_STAGE_OUTCOMES: frozenset[StageOutcome] = frozenset({StageOutcome.COMPLETED})


class FindingSeverity(StrEnum):
    """How much a finding matters, as the reviewer classified it.

    ``blocking`` is the only value with contract-level consequences: an open
    blocking finding makes the result non-approval-capable regardless of any other
    field. That is the rule the security-only runs violated in spirit — they
    described a gate that "fails open" and then reported nothing above threshold,
    so the blocker had no representation a consumer could act on.
    """

    BLOCKING = "blocking"
    """Must be resolved before this change can be approved."""

    MAJOR = "major"
    """Should be addressed; does not by itself prevent approval."""

    MINOR = "minor"

    INFORMATIONAL = "informational"


class FindingDisposition(StrEnum):
    """Where a finding stands, as of ``observed_at`` and for ``reviewed_head_sha``.

    A disposition is a claim about a specific commit. When the head moves, every
    disposition recorded against the old commit describes code that is no longer
    the change under review, which is why `invalidate_for_head` rewrites them to
    ``stale-head`` rather than carrying them forward.
    """

    OPEN = "open"
    """Reproduced at this head and not fixed. Blocks approval when severity is
    ``blocking``."""

    RESOLVED = "resolved"
    """Verified fixed at this head by the reviewer, with evidence."""

    ACKNOWLEDGED = "acknowledged"
    """Recorded and accepted as not requiring action at this head. Deliberately
    NOT permissive for a blocking finding — acknowledging a blocker does not
    clear it; only `resolved` does."""

    STALE_HEAD = "stale-head"
    """The head moved after this disposition was determined, so it describes code
    that is no longer under review. Never permissive: the finding must be
    re-evaluated against the new head."""


#: Dispositions that clear a finding. `acknowledged` is excluded on purpose — see
#: its docstring. Absence from this set is what makes a blocking finding blocking.
CLEARED_DISPOSITIONS: frozenset[FindingDisposition] = frozenset(
    {FindingDisposition.RESOLVED}
)


class PublicationOutcome(StrEnum):
    """Whether the verdict reached somewhere repository rules can read it.

    This enum is the direct answer to the most-repeated observation on #5146: runs
    that completed both review stages, published prose, exited successfully, and
    left the pull request's review list empty. Prose is not a verdict a merge rule
    can consume, and "the worker exited 0" did not record that the formal step never
    happened.

    Every value except ``published`` leaves the result non-approval-capable, and
    ``not-attempted`` exists so that "we never tried" is representable rather than
    being encoded as an absent field.
    """

    PUBLISHED = "published"
    """A formal verdict was recorded against ``reviewed_head_sha``. The only
    approval-capable value."""

    REFUSED = "refused"
    """The provider declined to record it — for example a reviewer sharing the
    author's identity, which GitHub refuses. A legitimate outcome that must be
    reported, never smoothed over."""

    FAILED = "failed"
    """Publication was attempted and errored in transport."""

    NOT_ATTEMPTED = "not-attempted"
    """No formal publication was attempted. This is the state the observed runs
    were actually in while reporting success."""


PUBLICATION_ACCEPTED: frozenset[PublicationOutcome] = frozenset(
    {PublicationOutcome.PUBLISHED}
)

#: Outcomes where the producer actually *called* the provider, whatever came back.
#:
#: The distinction this draws is between two things that look alike in a blocker
#: list and are opposites in cause. A `failed`/`refused` publication is an honest
#: report: the reviewer concluded, attempted to record the verdict, and the provider
#: answered with an error or a refusal. `not-attempted` is the silent skip this whole
#: contract was built to expose — the run completed its stages, exited 0, and never
#: reached for the provider at all.
#:
#: So `approve` is permitted over an attempted-and-failed publication, because
#: refusing it makes the situation unrepresentable and forces the producer to either
#: drop the artifact or misreport its verdict. `approve` over `not-attempted` stays
#: refused, because there the missing verdict *is* the defect and nothing was
#: observed that could support an approval claim.
#:
#: Being in this set is not approval-capability: only `PUBLICATION_ACCEPTED` is that,
#: and `approval_blockers` reports every non-published outcome regardless.
PUBLICATION_ATTEMPTED: frozenset[PublicationOutcome] = frozenset(
    {
        PublicationOutcome.PUBLISHED,
        PublicationOutcome.REFUSED,
        PublicationOutcome.FAILED,
    }
)


class ReviewVerdict(StrEnum):
    """The reviewer's conclusion about the change at ``reviewed_head_sha``.

    ``incomplete`` is a first-class verdict rather than an absent one, because a
    reviewer that could not finish must be able to say so in the same field a
    consumer already reads. A missing verdict is indistinguishable from a lost
    message; an explicit ``incomplete`` is not.
    """

    APPROVE = "approve"
    REQUEST_CHANGES = "request-changes"
    INCOMPLETE = "incomplete"
    """Review did not conclude. Never approval-capable."""


class ContractEnvelope(BaseModel):
    """The `name` / `version` / `owner` substrate every ADP contract carries.

    Pinned to literal values so a document from another contract, or a future v2,
    cannot validate as a v1 document. Mirrors `contracts/hitl-ticket/v1/models.py`.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    name: str = Field(description=f"Contract name. Must be {CONTRACT_NAME!r}.")
    version: WireInt = Field(description="Contract version. 1 for this contract.")
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


class ReviewScope(BaseModel):
    """Which work this review belongs to, in the existing orchestration graph.

    Field-for-field the tenant-scoped coordinates `ExecutionIdentity` already uses
    (`modules/gateway/src/orchestration/execution_state.py`), plus ``flow_id`` for
    display. Named the same on purpose: the consumer reconstructs an
    `ExecutionIdentity` from this object, and a contract that invented its own
    spelling would require a translation layer where a mismatch could hide.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    org_id: str = Field(
        min_length=1,
        description="Tenant partition. Every read and write is scoped to it.",
    )
    flow_id: str = Field(min_length=1)
    node_id: str = Field(
        min_length=1, description="The graph node whose delivery is under review."
    )
    cycle: WireInt = Field(
        ge=1,
        description="Which delivery/repair cycle of the node this is. Cycles start at 1.",
    )
    execution_id: str | None = Field(
        default=None,
        description="The execution record this review observes, when the producer knows it. "
        "Optional because the runtime may learn it only on submission.",
    )

    @field_validator("org_id", "flow_id", "node_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("scope identifiers must be non-blank")
        return value


class ReviewAuthority(BaseModel):
    """Which accepted plan and which claim generation authorized this review.

    Both are fences owned elsewhere and reused here, never re-decided: the plan
    version is what `policy_admission` resolved (#5128), and the claim generation is
    the one `work_claims` issued (#5127). A result presenting a generation that is
    no longer current describes work someone else now owns, and the consumer refuses
    it rather than adopting it.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    accepted_plan_version: WireInt = Field(
        ge=0,
        description="Accepted-plan version in force. 0 is legal and means no accepted plan "
        "exists — the legacy path, which must stay usable.",
    )
    claim_id: str = Field(min_length=1)
    claim_generation: WireInt = Field(
        ge=1, description="The claim fence. Generations start at 1."
    )


class ReviewRepository(BaseModel):
    """The repository, by the identity a rename or transfer cannot re-point.

    ``provider_repository_id`` is the immutable key; ``repo`` is the display name.
    Both are required for the reason `pr_bindings.PullRequestIdentity` gives: a
    caller that can only supply a name is refused rather than bound on the strength
    of something mutable. The observed wrong-revision run shows why — inference
    from names and numbers is how findings get attached to code nobody read.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    provider_repository_id: WireInt = Field(
        ge=1, description="Immutable provider repository id."
    )
    repo: str = Field(description="Mutable display path, `owner/name`.")

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        if not REPO_PATTERN.match(value) or any(
            part in {".", ".."} for part in value.split("/")
        ):
            raise ValueError(f"repo must look like 'owner/name', got {value!r}")
        return value


class ReviewSubject(BaseModel):
    """The pull request and the exact commit the reviewer read.

    ``reviewed_head_sha`` is the field this whole contract is built around. It is
    the commit the reviewer actually inspected — not the branch tip at publication
    time, and not whatever the provider later attaches the verdict to. The observed
    wrong-revision run published analysis of one commit while the provider recorded
    it against another; with this field the consumer can detect that instead of
    trusting the association.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    pr_number: WireInt = Field(ge=1)
    provider_pr_node_id: str = Field(
        min_length=1, description="Immutable provider pull-request node id."
    )
    reviewed_head_sha: str = Field(
        description="The exact commit inspected. 40-hex (SHA-1) or 64-hex (SHA-256), lowercase."
    )

    @field_validator("provider_pr_node_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider_pr_node_id must be non-blank")
        return value

    @field_validator("reviewed_head_sha")
    @classmethod
    def _check_sha(cls, value: str) -> str:
        if not SHA_PATTERN.match(value):
            raise ValueError(
                f"reviewed_head_sha must be 40 or 64 lowercase hex characters, got {value!r}"
            )
        return value


class ReviewLineage(BaseModel):
    """Which run wrote the code and which run reviewed it.

    Both are run identifiers the platform issued. The consumer re-derives them from
    server-side run records and refuses a result whose claimed lineage does not
    match, which is what "arbitrary issue prose cannot manufacture evidence" means
    mechanically: a model can put any string here, and it will not survive binding.

    The ``reviewer != author`` rule is enforced in `ReviewResult`, not here, because
    it is a relation between two fields rather than a property of either.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    author_run_id: str = Field(
        min_length=1, description="The run that produced the change under review."
    )
    reviewer_run_id: str = Field(
        min_length=1, description="The run that performed this review."
    )
    reviewer_identity: str | None = Field(
        default=None,
        description="Provider login the verdict was published under, when known. Advisory: a "
        "shared bot identity is exactly why provider-side independent approval is separate.",
    )

    @field_validator("author_run_id", "reviewer_run_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("run identifiers must be non-blank")
        return value


class EvidenceRef(BaseModel):
    """A reference to something the reviewer relied on. A pointer, never a payload.

    References only, for the reason the execution ledger carries `*_ref` strings:
    the artifact lives in the store that already owns it, and copying it into the
    review result would create a second copy that can disagree with the first.

    ``head_bound`` is the field that makes stale-head invalidation possible. A test
    run against a specific commit stops being evidence when that commit is no
    longer the change under review; a link to an unchanging design document does
    not. `invalidate_for_head` uses this flag to decide which is which, so the
    producer must set it honestly.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    kind: str = Field(
        min_length=1,
        description="What sort of evidence, e.g. 'test-run', 'artifact', 'check-run'.",
    )
    ref: str = Field(
        min_length=1, description="Opaque reference resolved by the store that owns it."
    )
    head_bound: bool = Field(
        description="True when this evidence is only meaningful for `reviewed_head_sha`. "
        "Test and check results are; static documents are not.",
    )
    summary: str | None = Field(
        default=None,
        description="Optional human-readable note. Never load-bearing for a decision.",
    )
    stale: bool = Field(
        default=False,
        description="Set by `invalidate_for_head` when the head moved. A stale reference "
        "supports no conclusion.",
    )

    @field_validator("kind", "ref")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence kind and ref must be non-blank")
        return value


class ReviewFinding(BaseModel):
    """One thing the reviewer found, with its severity and where it stands.

    ``finding_id`` is stable across cycles so a finding re-reproduced at a new head
    is recognisably the same finding rather than a new one, and so a claim that "all
    six blockers are fixed" can be checked per-blocker. The observed false-negative
    run asserted exactly that while having re-tested only one sub-behaviour; stable
    ids plus per-finding evidence are what make that checkable.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    finding_id: str = Field(
        min_length=1, description="Stable identifier for this finding across cycles."
    )
    stage: ReviewStageName = Field(
        description="Which stage produced it. A correctness defect a security scanner "
        "filtered below threshold is still a functional finding."
    )
    severity: FindingSeverity
    disposition: FindingDisposition
    summary: str = Field(
        min_length=1, description="What the defect is, in one statement."
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        description="Per-finding evidence, e.g. the reproduction that showed it open or "
        "the test that showed it resolved.",
    )

    @field_validator("finding_id", "summary")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("finding_id and summary must be non-blank")
        return value

    @model_validator(mode="after")
    def _resolved_needs_evidence(self) -> ReviewFinding:
        # "Fixed, trust me" is the false-negative shape: a blocking finding declared
        # resolved with nothing behind it is indistinguishable from one that was
        # never re-tested. Non-blocking severities are not held to this, so a minor
        # note can be closed out without ceremony.
        if (
            self.severity is FindingSeverity.BLOCKING
            and self.disposition is FindingDisposition.RESOLVED
            and not self.evidence_refs
        ):
            raise ValueError(
                f"blocking finding {self.finding_id!r} declared 'resolved' must carry at "
                "least one evidence reference showing it was re-tested"
            )
        return self

    @property
    def blocks_approval(self) -> bool:
        """True when this finding must prevent approval.

        Blocking severity and any disposition other than `resolved`. Asked through
        this property rather than compared against a list of bad dispositions, so a
        disposition added in future is non-permissive by default.
        """
        return (
            self.severity is FindingSeverity.BLOCKING
            and self.disposition not in CLEARED_DISPOSITIONS
        )


class ReviewStage(BaseModel):
    """One stage of review, and whether it actually concluded.

    Carrying the outcome explicitly is the point. The observed runs all exited
    successfully; what was missing was any representation of "the functional stage
    did not produce a verdict". An absent stage cannot say that, so `ReviewResult`
    requires the functional stage to be present regardless of its outcome.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    name: ReviewStageName
    outcome: StageOutcome
    detail: str | None = Field(
        default=None,
        description="Why a stage is `not-run` or `failed`. Required for those outcomes.",
    )

    @model_validator(mode="after")
    def _inconclusive_needs_detail(self) -> ReviewStage:
        # An unexplained `not-run` is the silent skip this contract exists to make
        # noisy. Requiring a reason means the artifact says what went wrong instead
        # of leaving an operator to infer it from an empty review list.
        if (
            self.outcome not in CONCLUSIVE_STAGE_OUTCOMES
            and not (self.detail or "").strip()
        ):
            raise ValueError(
                f"stage {self.name.value!r} with outcome {self.outcome.value!r} must "
                "explain itself in 'detail'"
            )
        return self


class ReviewPublication(BaseModel):
    """Whether the verdict was recorded where repository rules can read it.

    Separate from the verdict because they fail independently, and the observed
    runs failed exactly here: functional stage complete, verdict decided, prose
    posted, formal review absent. ``detail`` is required for every non-published
    outcome so a refusal is reported rather than inferred from silence.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    outcome: PublicationOutcome
    published_head_sha: str | None = Field(
        default=None,
        description="The commit the formal verdict was recorded against. Must equal "
        "`reviewed_head_sha` — a verdict attached to a commit the reviewer did not read "
        "is not evidence about it.",
    )
    reference: str | None = Field(
        default=None,
        description="Pointer to the published verdict, e.g. a review id or URL.",
    )
    detail: str | None = Field(
        default=None,
        description="Why publication did not happen. Required unless `published`. "
        "Must never carry credentials.",
    )

    @field_validator("published_head_sha")
    @classmethod
    def _check_sha(cls, value: str | None) -> str | None:
        if value is not None and not SHA_PATTERN.match(value):
            raise ValueError(
                f"published_head_sha must be 40 or 64 lowercase hex characters, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ReviewPublication:
        if self.outcome is PublicationOutcome.PUBLISHED:
            if not self.published_head_sha:
                raise ValueError(
                    "a 'published' publication must name the commit the verdict was "
                    "recorded against"
                )
        elif not (self.detail or "").strip():
            raise ValueError(
                f"publication outcome {self.outcome.value!r} must explain itself in "
                "'detail'; an unexplained missing verdict is the defect this contract "
                "exists to expose"
            )
        return self


class ReviewResult(ContractEnvelope):
    """A reviewer run's structured conclusion about one pull request revision.

    The envelope's rules are structural. The three below are the contract's
    substance, and each maps to a recorded failure:

    * **The functional stage must be described.** A result carrying only a security
      stage is rejected outright, rather than validating and then being read as
      "nothing blocking found".
    * **The reviewer must not be the author.** Self-review is refused here, and
      `approval_blockers` repeats that this does not discharge the provider-side
      independent-approval requirement.
    * **A published verdict must name the commit it was recorded against, and it
      must be the reviewed one.** Otherwise the artifact inherits the
      wrong-revision failure it is meant to detect.
    """

    result_id: str = Field(
        min_length=1, description="Stable, unique identifier for this review result."
    )
    scope: ReviewScope
    authority: ReviewAuthority
    repository: ReviewRepository
    subject: ReviewSubject
    lineage: ReviewLineage
    verdict: ReviewVerdict
    stages: list[ReviewStage] = Field(
        description="Every stage attempted or deliberately skipped. The functional stage "
        "is mandatory."
    )
    findings: list[ReviewFinding] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        description="Result-level evidence not attributable to a single finding, e.g. the "
        "suite run for the whole change.",
    )
    publication: ReviewPublication
    observed_at: datetime = Field(
        description="When the reviewer observed this state (timezone-aware)."
    )

    @field_validator("result_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("result_id must be non-blank")
        return value

    @field_validator("observed_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        # A naive timestamp means "whatever the writer's clock said", and this
        # artifact is read by a different machine than the one that wrote it.
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    @field_validator("stages")
    @classmethod
    def _stages_unique_and_functional_present(
        cls, value: list[ReviewStage]
    ) -> list[ReviewStage]:
        if not value:
            raise ValueError(
                "stages must not be empty; a result that describes no review stage is "
                "not review evidence"
            )
        names = [stage.name for stage in value]
        if len(set(names)) != len(names):
            raise ValueError(
                "stages must not repeat a stage name; two outcomes for one stage is ambiguous"
            )
        if ReviewStageName.FUNCTIONAL not in names:
            # The security-only result, rejected at the contract boundary. Reporting
            # `not-run` with a reason is always available and is what a
            # security-only run should emit.
            raise ValueError(
                "stages must describe the 'functional' stage, even when its outcome is "
                "'not-run' or 'failed'; "
                "a result carrying only a security stage would read as 'nothing blocking found'"
            )
        return value

    @field_validator("findings")
    @classmethod
    def _findings_unique(cls, value: list[ReviewFinding]) -> list[ReviewFinding]:
        ids = [finding.finding_id for finding in value]
        if len(set(ids)) != len(ids):
            raise ValueError(
                "findings must not repeat a finding_id; a duplicate makes "
                "'is it resolved?' unanswerable"
            )
        return value

    @model_validator(mode="after")
    def _reviewer_is_not_author(self) -> ReviewResult:
        if self.lineage.reviewer_run_id == self.lineage.author_run_id:
            raise ValueError(
                f"reviewer_run_id and author_run_id are both "
                f"{self.lineage.reviewer_run_id!r}; a run cannot review its own output. "
                "Note this check is necessary but not sufficient — provider-side "
                "independent approval remains separately required."
            )
        return self

    @model_validator(mode="after")
    def _publication_matches_reviewed_head(self) -> ReviewResult:
        published = self.publication.published_head_sha
        if published is not None and published != self.subject.reviewed_head_sha:
            raise ValueError(
                f"publication.published_head_sha {published!r} differs from "
                f"subject.reviewed_head_sha {self.subject.reviewed_head_sha!r}; "
                "a verdict recorded against a commit the reviewer did not inspect is "
                "not evidence about that commit"
            )
        return self

    @model_validator(mode="after")
    def _approve_requires_conclusive_review(self) -> ReviewResult:
        # Rejecting an unsupported `approve` at validation rather than only in
        # `approval_blockers` means a producer cannot emit one at all. Approve with
        # the functional stage skipped, and approve over an open blocking finding,
        # both fail here.
        #
        # Gated on `review_blockers`, NOT `approval_blockers`, and the difference is
        # a repaired defect rather than a relaxation. A reviewer whose functional
        # stage completed cleanly and whose publication then returned HTTP 401 has
        # concluded `approve` about the code; including the publication blocker here
        # made that artifact *invalid*, so the producer's only options were to drop
        # the evidence or to restate the verdict as `incomplete`. Both were observed,
        # and both erase what the reviewer actually found — while the publication
        # failure, the thing an operator must act on, disappears entirely.
        #
        # This grants nothing. `approval_blockers` still reports the unpublished
        # verdict, so such a result remains non-approval-capable to every consumer;
        # what changes is that the failure is now *recorded* instead of unrepresentable.
        # Merge eligibility is #5148's and `pr_bindings`' decision either way.
        #
        # `not-attempted` remains refused below. That case is not a transport failure
        # but the original defect — stages complete, exit 0, provider never called —
        # and there is no attempt whose outcome could support an approval claim.
        if self.verdict is ReviewVerdict.APPROVE:
            blockers = self.review_blockers()
            if blockers:
                raise ValueError(
                    f"verdict 'approve' is not supported by this result: {'; '.join(blockers)}"
                )
            if self.publication.outcome not in PUBLICATION_ATTEMPTED:
                raise ValueError(
                    f"verdict 'approve' with publication "
                    f"{self.publication.outcome.value!r} is not supported: no formal "
                    "verdict was ever attempted, which is the silent-skip failure this "
                    "contract exists to expose. Report the attempt's real outcome, or "
                    "use verdict 'incomplete'."
                )
        return self

    def stage(self, name: ReviewStageName) -> ReviewStage | None:
        """The named stage, or None when it was not described.

        Only `security` can legitimately be absent; `functional` is required by
        validation, so a None for it cannot occur on a validated document.
        """
        return next((item for item in self.stages if item.name is name), None)

    @property
    def blocking_findings(self) -> tuple[ReviewFinding, ...]:
        """Findings that must prevent approval. See `ReviewFinding.blocks_approval`."""
        return tuple(finding for finding in self.findings if finding.blocks_approval)

    def review_blockers(self) -> tuple[str, ...]:
        """Reasons the *review itself* did not reach a clean conclusion.

        Everything in here is about the reviewing work: did the functional stage
        conclude, is a blocking finding still open, did the reviewer say
        request-changes, is result-level evidence stale. Publication is deliberately
        excluded, and that separation is the whole point of this method existing.

        A reviewer whose functional stage completed with no open blocker, and whose
        formal publication then failed in transport, has genuinely concluded
        "approve" about the code. Folding the publication failure in here would make
        that situation *unrepresentable*: the producer could not emit the artifact at
        all, leaving it to either drop the evidence or misreport the verdict as
        `incomplete`. Both were observed, and both destroy the record of what the
        reviewer actually found.

        Callers deciding anything must use :meth:`approval_blockers`, which is this
        plus publication. This method answers a narrower question and is not a
        substitute for it.
        """
        reasons: list[str] = []

        functional = self.stage(ReviewStageName.FUNCTIONAL)
        if functional is None or functional.outcome not in CONCLUSIVE_STAGE_OUTCOMES:
            outcome = functional.outcome.value if functional else "absent"
            reasons.append(
                f"the functional review stage is {outcome}, so no functional verdict was reached"
            )

        for finding in self.blocking_findings:
            reasons.append(
                f"blocking finding {finding.finding_id!r} is "
                f"{finding.disposition.value}: {finding.summary}"
            )

        if self.verdict is ReviewVerdict.REQUEST_CHANGES:
            reasons.append("the reviewer's verdict is 'request-changes'")
        elif self.verdict is ReviewVerdict.INCOMPLETE:
            reasons.append("the reviewer's verdict is 'incomplete'")

        if any(ref.stale for ref in self.evidence_refs):
            reasons.append(
                "result-level evidence was invalidated by a head change and must be re-established"
            )

        return tuple(reasons)

    def publication_blockers(self) -> tuple[str, ...]:
        """Reasons the verdict is not readable by repository rules. Empty means it is.

        Split out from :meth:`review_blockers` because the two fail independently and
        a consumer must be able to tell them apart. "The reviewer approved but we
        could not publish it" needs a retry of the publication; "the reviewer found a
        blocker" needs new code. Collapsing both into one list made the first
        indistinguishable from the second.
        """
        if self.publication.outcome in PUBLICATION_ACCEPTED:
            return ()
        detail = (self.publication.detail or "").strip() or "no detail recorded"
        reason = (
            f"the formal verdict was not published "
            f"({self.publication.outcome.value}: {detail}), so repository rules "
            f"cannot read it"
        )
        return (reason,)

    def approval_blockers(self) -> tuple[str, ...]:
        """Every reason this result cannot support approval. Empty means none found.

        The single supported way to ask "is this evidence good enough?". It returns
        reasons rather than a boolean so a caller logging the answer keeps the
        *why*, and so a caller that ignores it is visibly ignoring something.

        Both halves are included, so an unpublished approval is still not approval
        here — a functional `approve` whose publication failed carries exactly one
        blocker, which is the record the operator needs and not a grant of anything.

        An empty tuple means no blocker was found **in this artifact**. It is not a
        merge decision and not a substitute for the provider-side requirements —
        independent approving review, required checks, merge state — which
        `pr_bindings.evidence_for_binding` owns.
        """
        return self.review_blockers() + self.publication_blockers()


def invalidate_for_head(result: ReviewResult, actual_head_sha: str) -> ReviewResult:
    """Re-express a result for a pull request whose head has moved.

    What survives a head change is the *observation* that a finding was seen. What
    does not survive is its disposition — "resolved" was a claim about specific code
    — and any evidence that was only meaningful for the reviewed commit. So every
    finding becomes ``stale-head``, every ``head_bound`` reference is marked stale,
    and the verdict becomes ``incomplete``.

    Returns the result unchanged when ``actual_head_sha`` equals the reviewed head,
    so a caller can apply this unconditionally. ``subject.reviewed_head_sha`` is
    deliberately NOT rewritten to the new head: this artifact records what was
    actually inspected, and re-pointing it at code nobody read is the wrong-revision
    defect this contract exists to detect.

    Raises:
        ValueError: when ``actual_head_sha`` is not a well-formed commit id, rather
            than silently invalidating against an unusable value.
    """
    if not SHA_PATTERN.match(actual_head_sha):
        raise ValueError(
            f"actual_head_sha must be 40 or 64 lowercase hex characters, got {actual_head_sha!r}"
        )
    if actual_head_sha == result.subject.reviewed_head_sha:
        return result

    reason = f"head moved from {result.subject.reviewed_head_sha} to {actual_head_sha} after this review"
    body = result.model_dump(mode="python")
    body["verdict"] = ReviewVerdict.INCOMPLETE
    body["findings"] = [
        {
            **finding,
            "disposition": FindingDisposition.STALE_HEAD,
            "evidence_refs": [_stale(ref) for ref in finding["evidence_refs"]],
        }
        for finding in body["findings"]
    ]
    body["evidence_refs"] = [_stale(ref) for ref in body["evidence_refs"]]
    # A verdict published against the old head is not a verdict about the new one.
    # Downgrading to `failed` with the reason keeps the history and stops
    # `approval_blockers` from treating the stale publication as satisfied.
    prior = (
        body["publication"].get("detail") or ""
    ).strip() or "verdict recorded for the previously reviewed head"
    body["publication"] = {
        **body["publication"],
        "outcome": PublicationOutcome.FAILED,
        "detail": f"{prior}; invalidated: {reason}",
    }
    return ReviewResult.model_validate(body)


def _stale(ref: dict) -> dict:
    """Mark a head-bound evidence reference stale, leaving others untouched."""
    return {**ref, "stale": True} if ref.get("head_bound") else ref
