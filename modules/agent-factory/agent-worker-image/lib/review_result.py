"""Emit this reviewer run's structured review result (#5146).

The producer half of ``contracts/orchestration-review/v1``. The consumer half is
``modules/gateway/src/orchestration/review_evidence.py``, and both are exercised
against the same golden fixture in CI — one artifact, not two, because the last time
a producer and a consumer each tested their own assumption about a shared shape they
disagreed for months and both suites stayed green (#4029).

--------------------------------------------------------------------------------
Why a reviewer run needs to emit anything at all
--------------------------------------------------------------------------------

A reviewer run that exits 0 has established nothing about the change it looked at.
Every failure recorded on #5146 has that shape: a run that reproduced a real
correctness blocker and published only a security report; a run whose functional
prose said APPROVE while the pull request's review list stayed empty; a run whose
findings described one commit while the verdict was attached to another. All of them
succeeded. The prose went to the pull request and the exit code went to the queue,
and neither could say *which commit was read* or *whether a verdict was recorded*.

This module writes that down: the exact commit inspected, whether each stage
concluded, what was found and where each finding stands, and — separately from the
verdict — whether the verdict actually reached somewhere repository rules can read
it.

--------------------------------------------------------------------------------
What this run is allowed to say about itself
--------------------------------------------------------------------------------

The document is **produced** here and **validated elsewhere**. Nothing in it is
believed because this module wrote it: ``review_evidence.validate_review_result``
compares every field against state this pod cannot write — the registered
pull-request binding, the execution row's accepted plan version and claim generation,
the dispatch record's author run, and the provider's current head. So the split of
responsibilities is:

* **Scope and authority** (tenant, flow, node, cycle, accepted plan version, claim)
  come from ``ADP_HANDOFF_EXPECT``, which the gateway published from protected
  records at dispatch. Never from arguments, never from agent output.
* **The authoring run** comes from ``ADP_REVIEW_EXPECT``, also server-published, and
  its absence is a **refusal**. A reviewer able to name the author could defeat the
  self-review check by pointing it at a run it is not, so this module will not let a
  caller supply it and will not invent one.
* **The reviewed commit, the repository and pull-request identity, the findings and
  the publication outcome** are supplied by the caller, because they are what this
  run actually observed. A forged value there buys nothing — the gateway diffs each
  against its own records — and refusing to record what the run saw would leave the
  artifact unable to describe the very wrong-revision case it exists to detect.

This module deliberately does not transmit anything. Producing the artifact and
recording it are separate steps: #5146's scope excludes a new mutation endpoint, so
the writer emits the document and leaves forwarding to a caller that can be reviewed
on its own terms.

--------------------------------------------------------------------------------
Fail-soft, like `pr_binding` and `handoff_client`
--------------------------------------------------------------------------------

By the time this runs the review is already posted and the run's real output is
delivered. So :func:`review_result_note` never raises and returns a string the caller
appends to its closing comment. A missing fence produces a stated reason rather than
an exception, because destroying a delivered review over bookkeeping is worse than
recording that the bookkeeping could not be done.

What it must never do is emit something that *reads* as a completed review when it is
not. An unpublished verdict, a skipped functional stage and a head that moved are each
representable and each non-approving; none is smoothed into silence, and the note this
module returns never claims approval — only that an artifact describing one exact
commit now exists.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_OWNER",
    "CONTRACT_VERSION",
    "DEFAULT_RESULT_PATH",
    "HANDOFF_EXPECT_ENV",
    "RESULT_PATH_ENV",
    "REVIEW_EXPECT_ENV",
    "FindingReport",
    "ReviewResultError",
    "StageReport",
    "build_review_result",
    "publication_from_adp_review",
    "review_expected",
    "review_result_note",
    "write_review_result",
]

# Pinned to the shared contract. Spelled here rather than imported because the
# repository's `contracts/` tree is not copied into this image (see the Dockerfile's
# COPY list), so the validator is unavailable at runtime. The CI job in
# `.github/workflows/orchestration-review-contract-tests.yml` validates what this
# module emits against the real models, which is what keeps the two in step; the
# alternative — trusting that two hand-maintained copies agree — is the #4029 defect.
CONTRACT_NAME = "orchestration-review"
CONTRACT_VERSION = 1
CONTRACT_OWNER = "orchestration/review"

#: The dispatch fences this run holds, already published for #5144's handoff. Reused
#: rather than duplicated: two env vars carrying the same tenant/node/cycle is two
#: things that can disagree, and a disagreement between them would be resolved by
#: whichever this module happened to read first.
HANDOFF_EXPECT_ENV = "ADP_HANDOFF_EXPECT"

#: The review-specific expectation the gateway publishes from protected records:
#: which run authored the change under review, and optionally the head this run was
#: dispatched to review and the execution row it belongs to. The worker never writes
#: this and never derives it from agent output.
REVIEW_EXPECT_ENV = "ADP_REVIEW_EXPECT"

#: Where the emitted artifact is written when the caller does not say. A file rather
#: than a log line: the document is JSON with nested structure, and a log pipeline
#: that truncates or reflows it would silently produce something that no longer
#: validates.
RESULT_PATH_ENV = "ADP_REVIEW_RESULT_PATH"
DEFAULT_RESULT_PATH = "/tmp/adp-review-result.json"

#: 40-hex SHA-1 or 64-hex SHA-256, lowercase — identical to the contract's
#: ``SHA_PATTERN``. Kept identical on purpose: a looser pattern on the producer side
#: would emit a head no provider issued and surface downstream instead of here.
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

#: ``owner/name``. Matches the contract's ``REPO_PATTERN``.
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Every fence the artifact's scope and authority need, split by type so a value of
# the wrong type refuses rather than being coerced. Named explicitly rather than
# derived from whichever keys happen to be present, for the reason
# `handoff_client._STRING_FENCES` gives: a missing field must refuse, not be skipped
# by a comparison that never ran.
_STRING_FENCES = ("org_id", "flow_id", "node_id", "claim_id")
_INT_FENCES = ("cycle", "accepted_plan_version", "claim_generation")

#: The stage the contract requires to be described. A result carrying only a security
#: stage is what let a clean scanner report stand in for a functional review.
FUNCTIONAL_STAGE = "functional"
SECURITY_STAGE = "security"

# `adp_review` outcomes that mean the verdict actually reached the provider's review
# list. An accepted set rather than a denial list, so an outcome a future `adp_review`
# adds is non-publishing by default — the fail-closed direction.
_PUBLISHED_OUTCOMES = frozenset({"submitted"})


class ReviewResultError(Exception):
    """The artifact could not be built from what this run actually holds.

    Always a refusal to emit, never a partial artifact. ``reason`` is operator prose
    naming the specific missing fence, because the observed failures were expensive to
    diagnose precisely because "the reviewer ran and nothing happened" said nothing
    about which part was absent.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class StageReport:
    """One review stage, and whether it concluded.

    ``detail`` is required by the contract for any outcome other than ``completed``,
    and :func:`build_review_result` refuses without it. An unexplained ``not-run`` is
    the silent skip the whole contract exists to make noisy.
    """

    name: str
    outcome: str
    detail: str | None = None


@dataclass(frozen=True)
class FindingReport:
    """One finding, its severity, and where it stands at the reviewed commit.

    ``evidence_refs`` are references — a check-run id, an artifact key — never a
    payload, matching the ledger's ``*_ref`` convention. A blocking finding claimed
    ``resolved`` must carry at least one, which the contract enforces: "fixed, trust
    me" is the observed false-negative shape, where a run asserted six blockers fixed
    having re-tested one.
    """

    finding_id: str
    stage: str
    severity: str
    disposition: str
    summary: str
    evidence_refs: tuple[dict[str, object], ...] = field(default_factory=tuple)


def _expect(env_var: str) -> dict:
    """Parse a server-published expectation, or ``{}`` when unusable.

    Defensive like ``handoff_client.expected_identity``: an unparseable expectation
    becomes a refusal at the point a fence is needed, not an exception escaping into a
    run that has already delivered its review.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("review result: %s is not valid JSON; no artifact can be built", env_var)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("review result: %s is not an object; no artifact can be built", env_var)
        return {}
    return parsed


def review_expected() -> dict:
    """The server-published review expectation for this run, or ``{}``.

    Empty means this is not an engine review dispatch, which every entry point treats
    as "do nothing" rather than as a failure: the ad-hoc review paths that predate
    this contract must keep working byte for byte.
    """
    return _expect(REVIEW_EXPECT_ENV)


def _require_fences(expect: dict) -> tuple[dict[str, str], dict[str, int]]:
    """The scope and authority fences, or a refusal naming the first missing one."""
    strings: dict[str, str] = {}
    for key in _STRING_FENCES:
        value = expect.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ReviewResultError(
                f"this dispatch published no {key}, so the review result cannot state "
                "which work it belongs to"
            )
        strings[key] = value
    integers: dict[str, int] = {}
    for key in _INT_FENCES:
        value = expect.get(key)
        # `type(...) is not int` rather than isinstance: `True` is an int to
        # isinstance, and a boolean cycle is not a cycle.
        if type(value) is not int:
            raise ReviewResultError(
                f"this dispatch published no integer {key}, so the review result cannot "
                "state the authority it was produced under"
            )
        integers[key] = value
    return strings, integers


def publication_from_adp_review(
    result: dict | None, *, reviewed_head_sha: str
) -> dict[str, object]:
    """Translate an ``adp_review submit`` result into the contract's publication block.

    This function is the whole reason publication is a separate object from the
    verdict. ``adp_review`` already distinguishes a recorded verdict from prose that
    merely reached the pull request (``verdict_recorded``), and that distinction was
    being discarded at the boundary: a run whose verdict GitHub refused still exited
    with an approving summary. Here a non-published outcome keeps its reason and stays
    *representable*, which is what stops it reading as a completed review.

    ``None`` means no publication was attempted — recorded as ``not-attempted`` with
    that stated, never as an absence a reader could take for success.
    """
    if result is None:
        return {
            "outcome": "not-attempted",
            "published_head_sha": None,
            "reference": None,
            "detail": "this run submitted no formal verdict",
        }

    outcome = str(result.get("outcome") or "").strip()
    recorded = result.get("verdict_recorded") is True
    raw_reference = result.get("url") or result.get("review_id")
    reference = str(raw_reference)[:500] if raw_reference else None

    if recorded and outcome in _PUBLISHED_OUTCOMES:
        return {
            "outcome": "published",
            # The commit the verdict is recorded against. `adp_review submit` is
            # called with `--commit` pinned to the reviewed head, and the contract
            # refuses these two differing: a verdict on a commit the reviewer did not
            # read is not evidence about it.
            "published_head_sha": reviewed_head_sha,
            "reference": reference,
            "detail": None,
        }

    if result.get("pending_human_approval") is True or outcome == "pending_approval":
        return {
            "outcome": "refused",
            "published_head_sha": None,
            "reference": reference,
            "detail": (
                "the provider refused to record the verdict because no distinct reviewer "
                "identity is configured; the analysis was published as a comment, which "
                "sets no review decision"
            ),
        }

    return {
        "outcome": "failed",
        "published_head_sha": None,
        "reference": reference,
        # Outcome only, and bounded. `adp_review` error text can quote a provider
        # response body, and this field is read by operators and stored.
        "detail": f"the verdict was not recorded (adp-review outcome {outcome or 'unknown'!r})",
    }


def build_review_result(
    *,
    result_id: str,
    repo: str,
    provider_repository_id: int,
    pr_number: int,
    provider_pr_node_id: str,
    reviewed_head_sha: str,
    verdict: str,
    stages: list[StageReport],
    publication: dict[str, object],
    findings: list[FindingReport] | None = None,
    evidence_refs: list[dict[str, object]] | None = None,
    reviewer_identity: str | None = None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Assemble this run's review result from protected fences plus what it observed.

    Scope, authority and the authoring run are read from the environment the gateway
    published and cannot be passed in: those are exactly the fields a confused or
    compromised agent would want to choose, and a caller inside this process is not a
    trustworthy source for them. What the caller supplies is what only this run knows
    — the repository and pull request it was pointed at, the commit it checked out, its
    stage outcomes, its findings, and what publication returned.

    ``reviewed_head_sha`` must be the commit **actually inspected**. The entrypoint
    already verifies the checked-out head against the dispatch's expected sha before
    exec; passing anything else would recreate the wrong-revision failure this artifact
    exists to detect.

    Returns:
        A JSON-serialisable document matching ``contracts/orchestration-review/v1``.

    Raises:
        ReviewResultError: when a fence is missing, or when what was passed cannot be
            described honestly. Never returns a partial artifact.
    """
    review_expect = review_expected()
    if not review_expect:
        raise ReviewResultError(
            f"this dispatch published no review expectation ({REVIEW_EXPECT_ENV}), so this "
            "run cannot state whose change it reviewed"
        )
    # Review-specific values win on a key collision: it is the more specific record,
    # and the gateway compares the result against its own rows regardless.
    strings, integers = _require_fences({**_expect(HANDOFF_EXPECT_ENV), **review_expect})

    reviewer_run_id = os.environ.get("ADP_MESSAGE_ID", "").strip()
    if not reviewer_run_id:
        raise ReviewResultError(
            "this run has no run identifier, so its review cannot be attributed to a reviewer"
        )

    author_run_id = review_expect.get("author_run_id")
    if not isinstance(author_run_id, str) or not author_run_id.strip():
        # Deliberately not substituted with anything, and deliberately not accepted as
        # an argument: a reviewer that could name the author could defeat the
        # self-review check by naming a run it is not.
        raise ReviewResultError(
            "this dispatch did not publish which run authored the change, so self-review "
            "cannot be ruled out and no review evidence can be produced"
        )
    if author_run_id == reviewer_run_id:
        raise ReviewResultError(
            "this run authored the change it was asked to review; a run cannot review its "
            "own output"
        )

    _check_subject(
        repo=repo,
        provider_repository_id=provider_repository_id,
        pr_number=pr_number,
        provider_pr_node_id=provider_pr_node_id,
        reviewed_head_sha=reviewed_head_sha,
    )

    expected_head = review_expect.get("expected_head_sha")
    if isinstance(expected_head, str) and expected_head and expected_head != reviewed_head_sha:
        # The dispatch named a head and this run read a different one. Refused rather
        # than recorded: the artifact would be perfectly true and the review would
        # still be about code nobody asked about, which is the defect, not a detail.
        raise ReviewResultError(
            f"this run inspected {reviewed_head_sha[:12]} but was dispatched to review "
            f"{expected_head[:12]}; the head moved and a fresh review is required"
        )

    execution_id = review_expect.get("execution_id")
    usable_execution_id = execution_id if isinstance(execution_id, str) and execution_id else None
    return {
        "name": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "owner": CONTRACT_OWNER,
        "result_id": result_id,
        "scope": {
            "org_id": strings["org_id"],
            "flow_id": strings["flow_id"],
            "node_id": strings["node_id"],
            "cycle": integers["cycle"],
            # Optional in the contract: the runtime may not know it, and inventing a
            # value would be worse than omitting one. The gateway resolves the
            # execution from its own row.
            "execution_id": usable_execution_id,
        },
        "authority": {
            "accepted_plan_version": integers["accepted_plan_version"],
            "claim_id": strings["claim_id"],
            "claim_generation": integers["claim_generation"],
        },
        "repository": {"provider_repository_id": provider_repository_id, "repo": repo},
        "subject": {
            "pr_number": pr_number,
            "provider_pr_node_id": provider_pr_node_id,
            "reviewed_head_sha": reviewed_head_sha,
        },
        "lineage": {
            "author_run_id": author_run_id,
            "reviewer_run_id": reviewer_run_id,
            # Advisory only. A shared bot login is precisely why provider-side
            # independent approval remains a separate requirement.
            "reviewer_identity": reviewer_identity,
        },
        "verdict": verdict,
        "stages": _stage_bodies(stages),
        "findings": _finding_bodies(findings or []),
        "evidence_refs": [dict(ref) for ref in (evidence_refs or [])],
        "publication": dict(publication),
        "observed_at": _timestamp(observed_at),
    }


def _check_subject(
    *,
    repo: str,
    provider_repository_id: int,
    pr_number: int,
    provider_pr_node_id: str,
    reviewed_head_sha: str,
) -> None:
    """Refuse an unusable subject here, where the caller that built it is on the stack.

    Every one of these is also enforced by the contract validator. Checked twice on
    purpose: the validator is not importable inside this image, so without these the
    first sign of a producer bug would be a validation error in a gateway log long
    after the run that caused it exited.
    """
    # `.` and `..` are excluded as segments as well as matched against the pattern,
    # because `-` and `.` are legal in a repository name and `../etc` therefore
    # satisfies the pattern alone. The contract's `_check_repo` rejects exactly the
    # same two, and this check exists because the producer's own suite caught the
    # divergence when it did not.
    if (
        not isinstance(repo, str)
        or not _REPO_PATTERN.match(repo or "")
        or any(part in {".", ".."} for part in repo.split("/"))
    ):
        raise ReviewResultError(
            f"the repository {repo!r} is not an 'owner/name' path, so the review cannot "
            "name what it reviewed"
        )
    # `type(...) is not int` for the bool reason again: `True` would otherwise pass as
    # repository id 1.
    if type(provider_repository_id) is not int or provider_repository_id < 1:
        raise ReviewResultError(
            "this review has no immutable provider repository id; a rename or transfer "
            "re-points the display name, which is how findings land on the wrong repository"
        )
    if type(pr_number) is not int or pr_number < 1:
        raise ReviewResultError(
            f"the pull-request number {pr_number!r} is not a positive integer, so the review "
            "cannot be bound to a pull request"
        )
    if not isinstance(provider_pr_node_id, str) or not provider_pr_node_id.strip():
        raise ReviewResultError(
            "this review has no immutable pull-request node id; a number alone is not "
            "identity, matching the contract's refusal to bind on it"
        )
    if not isinstance(reviewed_head_sha, str) or not _SHA_PATTERN.match(reviewed_head_sha or ""):
        raise ReviewResultError(
            f"the reviewed commit {reviewed_head_sha!r} is not a full lowercase commit sha, so "
            "this review cannot be bound to a revision"
        )


def _stage_bodies(stages: list[StageReport]) -> list[dict[str, object]]:
    """Stage bodies, refusing the shapes the contract would reject anyway."""
    if not stages:
        raise ReviewResultError("a review result that describes no stage is not review evidence")
    names = [stage.name for stage in stages]
    if FUNCTIONAL_STAGE not in names:
        raise ReviewResultError(
            "the functional stage must be described even when it did not run; a result "
            "carrying only a security stage reads as 'nothing blocking found'"
        )
    if len(set(names)) != len(names):
        raise ReviewResultError(
            "a stage must not be described twice; two outcomes for one stage leaves 'did "
            "functional review conclude?' unanswerable"
        )
    bodies: list[dict[str, object]] = []
    for stage in stages:
        if stage.outcome != "completed" and not (stage.detail or "").strip():
            raise ReviewResultError(
                f"stage {stage.name!r} is {stage.outcome!r} and must say why; an unexplained "
                "skip is the silent failure this contract exists to expose"
            )
        bodies.append({"name": stage.name, "outcome": stage.outcome, "detail": stage.detail})
    return bodies


def _finding_bodies(findings: list[FindingReport]) -> list[dict[str, object]]:
    """Finding bodies, refusing a repeated id before it reaches the wire."""
    ids = [finding.finding_id for finding in findings]
    if len(set(ids)) != len(ids):
        raise ReviewResultError(
            "a finding id must not repeat; a duplicate makes 'is this blocker resolved?' "
            "unanswerable because one entry can say open and the other resolved"
        )
    return [
        {
            "finding_id": finding.finding_id,
            "stage": finding.stage,
            "severity": finding.severity,
            "disposition": finding.disposition,
            "summary": finding.summary,
            "evidence_refs": [dict(ref) for ref in finding.evidence_refs],
        }
        for finding in findings
    ]


def _timestamp(observed_at: datetime | None) -> str:
    """A timezone-aware UTC instant. The contract refuses a naive one.

    Naive means "whatever this pod's clock said", and this artifact is read by a
    different machine than the one that wrote it.
    """
    moment = observed_at or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ReviewResultError("observed_at must be timezone-aware")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def write_review_result(document: dict[str, object], *, path: str | None = None) -> str:
    """Write the artifact where a collector can pick it up. Returns the path written.

    Raises:
        OSError: the file could not be written. Callers should use
            :func:`review_result_note`, which reports that instead of raising.
    """
    target = path or os.environ.get(RESULT_PATH_ENV, "").strip() or DEFAULT_RESULT_PATH
    # sort_keys so two runs producing the same review produce byte-identical files; a
    # diff between them then means the review differed, not that dict ordering did.
    body = json.dumps(document, indent=2, sort_keys=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(body + "\n")
    return target


def review_result_note(
    *,
    repo: str,
    provider_repository_id: int,
    pr_number: int,
    provider_pr_node_id: str,
    reviewed_head_sha: str,
    verdict: str,
    stages: list[StageReport],
    publication: dict[str, object],
    result_id: str | None = None,
    findings: list[FindingReport] | None = None,
    evidence_refs: list[dict[str, object]] | None = None,
    reviewer_identity: str | None = None,
    path: str | None = None,
) -> str:
    """Emit this run's review result and return a section for the closing comment.

    Never raises. Returns "" when the artifact does not apply — no review expectation
    was published, so this is not an engine review dispatch and behaviour is exactly
    what it was before this module existed.

    Every other path returns prose. A refusal to build says which fence was missing; a
    write failure says so. Silence is the one thing this must not produce, because a
    review whose evidence was never produced looking identical to one whose evidence
    was is the original defect.

    The note never asserts approval. It reports that an artifact describing one exact
    commit exists and, when the result cannot support approval, why. Whether the
    evidence suffices is the gateway's question
    (`review_evidence.validate_review_result`), and the repository's own
    independent-approval and check requirements apply regardless.
    """
    if not review_expected():
        return ""

    try:
        document = build_review_result(
            result_id=result_id or _default_result_id(reviewed_head_sha),
            repo=repo,
            provider_repository_id=provider_repository_id,
            pr_number=pr_number,
            provider_pr_node_id=provider_pr_node_id,
            reviewed_head_sha=reviewed_head_sha,
            verdict=verdict,
            stages=stages,
            publication=publication,
            findings=findings,
            evidence_refs=evidence_refs,
            reviewer_identity=reviewer_identity,
        )
    except ReviewResultError as exc:
        logger.warning("review result not produced: %s", exc.reason)
        return (
            f"> **Review evidence not produced.** {exc.reason}. This review is prose only: the "
            "engine cannot treat it as evidence about the current revision, and it grants no "
            "approval."
        )

    try:
        written = write_review_result(document, path=path)
    except OSError as exc:
        # The document was valid; only storing it failed. Reported with the reason,
        # because "the review ran and no artifact appeared" is the state that took the
        # longest to diagnose on the observed runs.
        logger.warning("review result could not be written: %s", exc)
        return (
            "> **Review evidence not produced:** the structured review result could not be "
            "written. This review is prose only and grants no approval."
        )
    logger.info("review result written path=%s", written)

    blockers = _local_blockers(document)
    if blockers:
        # Reported, not smoothed. A produced-but-incomplete review is the single most
        # important thing for a reader to see, because the observed runs all looked
        # complete at exactly this point.
        listed = "; ".join(blockers[:5])
        return (
            f"> Review evidence produced for `{reviewed_head_sha[:12]}`, and it does **not** "
            f"support approval: {listed}."
        )
    return (
        f"> Review evidence produced for `{reviewed_head_sha[:12]}`. It is evidence about that "
        "exact commit and nothing else; the repository's own independent-approval and check "
        "requirements still apply."
    )


def _local_blockers(document: dict[str, object]) -> list[str]:
    """The reasons this result cannot support approval, as the producer can see them.

    A deliberately small, local restatement of the contract's ``approval_blockers``
    for the note only — the validator is not importable here. It carries no authority:
    the gateway recomputes the answer from the real models, and the producer contract
    test asserts that a document this function calls clean has no blockers there
    either, so the two cannot drift into disagreeing unnoticed.
    """
    reasons: list[str] = []

    stages = document.get("stages") or []
    functional = next(
        (
            stage
            for stage in stages
            if isinstance(stage, dict) and stage.get("name") == FUNCTIONAL_STAGE
        ),
        None,
    )
    if functional is None or functional.get("outcome") != "completed":
        outcome = functional.get("outcome") if functional else "absent"
        reasons.append(
            f"the functional review stage is {outcome}, so no functional verdict was reached"
        )

    for finding in document.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        # `!= "resolved"` rather than a list of non-clearing dispositions, so a
        # disposition added to the contract later is non-permissive here by default.
        if finding.get("severity") == "blocking" and finding.get("disposition") != "resolved":
            reasons.append(
                f"blocking finding {finding.get('finding_id')!r} is {finding.get('disposition')}"
            )

    publication = document.get("publication") or {}
    if isinstance(publication, dict) and publication.get("outcome") != "published":
        detail = str(publication.get("detail") or "").strip() or "no detail recorded"
        reasons.append(
            f"the formal verdict was not published ({publication.get('outcome')}: {detail}), so "
            "repository rules cannot read it"
        )

    verdict = document.get("verdict")
    if verdict == "request-changes":
        reasons.append("the reviewer's verdict is 'request-changes'")
    elif verdict == "incomplete":
        reasons.append("the reviewer's verdict is 'incomplete'")

    return reasons


def _default_result_id(reviewed_head_sha: str) -> str:
    """A result id derived from the run and the commit reviewed.

    Derived rather than random so a retry of the same review at the same head
    converges on one id instead of accumulating apparent reviews — the reasoning
    `ActionIntent.operation_key` documents on the consumer side.
    """
    run = os.environ.get("ADP_MESSAGE_ID", "").strip() or "unknown-run"
    return f"review-{run}-{reviewed_head_sha[:12]}"
