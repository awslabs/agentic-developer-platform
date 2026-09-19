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

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_REPORT_PATH_ENV",
    "CONTRACT_NAME",
    "CONTRACT_OWNER",
    "CONTRACT_VERSION",
    "DEFAULT_AGENT_REPORT_PATH",
    "DEFAULT_RESULT_PATH",
    "HANDOFF_EXPECT_ENV",
    "RESULT_PATH_ENV",
    "REVIEW_EXPECT_ENV",
    "FindingReport",
    "ReviewResultError",
    "StageReport",
    "build_review_result",
    "findings_from_agent_report",
    "publication_from_adp_review",
    "read_agent_report",
    "review_expected",
    "review_result_note",
    "reviewer_evidence_note",
    "stages_from_agent_report",
    "verdict_from_agent_report",
    "write_review_result",
]

# Pinned to the shared contract. Still spelled here — these three values are what a
# produced document must *carry*, so they have to exist before the validator is
# consulted, and they must be readable in an artifact where the contract is somehow
# absent. What changed (#5146) is that they are no longer the only thing keeping the
# producer honest: the image now carries `contracts/` (see the Dockerfile), so
# `contract_models()` below returns the real validator and
# `lib/contract_selfcheck.py` fails the build if it is missing. The old arrangement —
# constants duplicated here, agreement guaranteed only by a CI job — is the #4029
# drift the shared contract exists to prevent.
CONTRACT_NAME = "orchestration-review"
CONTRACT_VERSION = 1
CONTRACT_OWNER = "orchestration/review"

#: Where the contract lands in the image (`/app/contracts/...`, beside `lib/`) and in
#: a repository checkout (four parents up). Both are checked, in that order, so the
#: same code path works in the image, in CI and in a developer's tree.
_CONTRACT_RELATIVE = os.path.join("contracts", "orchestration-review", "v1")


def _contract_candidates() -> tuple[str, ...]:
    """Every directory the shared contract may legitimately live in, in priority order."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # /app or the image dir
    repo_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return (
        os.path.join(here, _CONTRACT_RELATIVE),
        os.path.join(repo_root, _CONTRACT_RELATIVE),
    )


def contract_models():
    """The normative validator, or ``None`` when this artifact does not carry it.

    ``None`` rather than an exception, and that is deliberate. This module is
    fail-soft by contract: it runs after the review is already posted, from a call
    site in ``entrypoint.py`` with no error handling, and losing the evidence artifact
    because a build dropped a directory would be a worse outcome than emitting an
    unvalidated one. So the caller degrades rather than dies — and
    ``lib/contract_selfcheck.py`` makes the missing directory a failed *build*, which
    is where that problem belongs.

    Cached in ``sys.modules`` under a private name, registered before execution
    because ``models.py`` defers its annotations and pydantic resolves them by module
    lookup; executing before registering leaves ``ReviewResult`` unable to validate.
    """
    cached = sys.modules.get("_adp_review_contract_v1")
    if cached is not None:
        return cached
    for directory in _contract_candidates():
        path = os.path.join(directory, "models.py")
        if not os.path.isfile(path):
            continue
        import importlib.util

        spec = importlib.util.spec_from_file_location("_adp_review_contract_v1", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules["_adp_review_contract_v1"] = module
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - fail-soft: see the module docstring
            # A present-but-unimportable validator must not stay cached under the
            # shared name for the next caller to mistake for a working one.
            sys.modules.pop("_adp_review_contract_v1", None)
            return None
        return module
    return None

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

#: Where the reviewer agent writes its own account of stages and findings. Read as
#: untrusted model output — see :func:`read_agent_report`.
AGENT_REPORT_PATH_ENV = "ADP_REVIEW_REPORT_PATH"
DEFAULT_AGENT_REPORT_PATH = "/tmp/adp-review-report.json"

# Bounds on that untrusted report. Generous enough for a real review and finite, since
# the alternative is letting model output decide how much memory this pod uses.
_MAX_REPORT_BYTES = 256 * 1024
_MAX_DETAIL_CHARS = 1000
_MAX_FINDINGS = 100
_MAX_REFS_PER_FINDING = 20

# The contract's closed vocabularies, mapped so an unrecognised value lands on the
# NON-permissive member rather than falling through. A typo must never be the reason a
# blocker stops blocking.
_SEVERITIES = {
    "blocking": "blocking",
    "major": "major",
    "minor": "minor",
    "informational": "informational",
}
_DISPOSITIONS = {
    "open": "open",
    "resolved": "resolved",
    "acknowledged": "acknowledged",
    "stale-head": "stale-head",
}


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


#: Operator-facing prose for each machine-readable ``refusal_reason`` `adp_review`
#: reports. Spelled here rather than imported from `adp_review.client` for the reason
#: the contract constants above give — but unlike those, a drift here is caught by the
#: producer suite, which asserts this mapping covers every constant that module
#: declares. The three causes are genuinely different actions for an operator, which
#: is why one shared sentence for all of them was a defect and not a simplification.
_REFUSAL_DETAILS: dict[str, str] = {
    "self_review": (
        "the provider refused to record the verdict because the author and the reviewer "
        "are the same identity; the analysis was published as a comment, which sets no "
        "review decision. A distinct reviewer identity is required"
    ),
    "state_downgraded": (
        "the provider accepted the submission but recorded a state carrying no verdict, "
        "so the analysis is present as a comment and no review decision was set. This is "
        "not an identity problem — the submission itself did not take"
    ),
    "unreadable_response": (
        "the provider answered successfully with a response this run could not read, so "
        "whether a verdict was recorded is unknown; it is reported as not recorded"
    ),
}

#: Used when the provider reported a refusal without saying why — for example a result
#: produced by an `adp_review` predating `refusal_reason`. Naming the absence is the
#: point: inventing the most familiar cause is what this whole change removes.
_REFUSAL_UNSTATED = (
    "the provider did not record the verdict and reported no reason, so the analysis is "
    "present as a comment and no review decision was set; the cause is unknown"
)


def _refusal_detail(reason: object) -> str:
    """Prose for the provider's actual refusal cause, or an honest 'unknown'."""
    if isinstance(reason, str):
        known = _REFUSAL_DETAILS.get(reason.strip())
        if known:
            return known
        if reason.strip():
            # A cause this producer does not recognise is reported verbatim and
            # bounded, rather than being flattened into a cause it is not.
            return (
                "the provider did not record the verdict; it reported reason "
                f"{reason.strip()[:120]!r}, which this run does not recognise"
            )
    return _REFUSAL_UNSTATED


def _provider_commit(value: object) -> str | None:
    """The provider's returned commit id, or ``None`` if it did not return a usable one.

    Shape-checked here so a provider echoing something that is not a commit sha cannot
    satisfy the published-head comparison by accident.
    """
    if isinstance(value, str) and _SHA_PATTERN.match(value.strip()):
        return value.strip()
    return None


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

    Two values are taken from what the provider *returned*, never from what was asked
    for, because both were previously asserted rather than observed:

    * ``published_head_sha`` is the provider's own ``commit_id``. It used to be set to
      ``reviewed_head_sha``, which made the contract's "the published commit must be
      the reviewed commit" check compare a value with itself — it could not fail, so
      it evidenced nothing. Now a provider that recorded the verdict against a
      different commit, or that returned no commit at all, does not reach
      ``published``.
    * the refusal ``detail`` is derived from ``refusal_reason``. ``pending_approval``
      alone is not proof of a same-identity refusal, and stating that cause for a
      downgraded state or an unreadable response would send an operator to configure
      a reviewer App that would not have changed the outcome.
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
    provider_commit = _provider_commit(result.get("commit_id"))

    if recorded and outcome in _PUBLISHED_OUTCOMES:
        if provider_commit is None:
            # The verdict may well exist, but nothing here establishes which revision
            # it is attached to, and that binding is the artifact's entire purpose.
            # Unknown is reported as not-published, in the fail-closed direction.
            return {
                "outcome": "failed",
                "published_head_sha": None,
                "reference": reference,
                "detail": (
                    "the provider reported a recorded verdict but returned no commit id, "
                    "so the revision it is attached to cannot be established"
                ),
            }
        if provider_commit != reviewed_head_sha:
            # The wrong-revision failure, caught at the boundary that can see it.
            return {
                "outcome": "failed",
                "published_head_sha": None,
                "reference": reference,
                "detail": (
                    f"the provider recorded the verdict against commit {provider_commit} "
                    f"but this run reviewed {reviewed_head_sha}, so the verdict is not "
                    "evidence about the revision that was read"
                ),
            }
        return {
            "outcome": "published",
            # The provider's value, having matched the commit actually inspected.
            "published_head_sha": provider_commit,
            "reference": reference,
            "detail": None,
        }

    if result.get("pending_human_approval") is True or outcome == "pending_approval":
        return {
            "outcome": "refused",
            "published_head_sha": None,
            "reference": reference,
            "detail": _refusal_detail(result.get("refusal_reason")),
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


def read_agent_report(path: str) -> dict:
    """Read the reviewer agent's own account of its stages and findings.

    The agent process knows things the entrypoint cannot: whether it actually ran a
    functional pass, what it found, and how each finding stands. So it writes them to
    a file and this reads them back.

    **Everything in that file is untrusted.** It is model output, and the issue's rule
    is that arbitrary prose must not manufacture approval. Two consequences:

    * Nothing about scope, authority, lineage, the repository, the pull request or the
      reviewed commit is read from here — not even as a hint. Those come from the
      server-published dispatch and from what the entrypoint inspected, and this
      function's return value has no way to influence them.
    * An unreadable, absent or malformed report yields ``{}``, which
      :func:`stages_from_agent_report` turns into a ``failed`` functional stage. The
      absent answer is never a pass: a reviewer that wrote nothing is reported as a
      review that did not conclude, which is precisely the state the observed runs
      were in while reporting success.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            # Bounded: this is model output, and an unbounded read is a memory
            # exposure in a pod that has already finished its real work.
            raw = handle.read(_MAX_REPORT_BYTES + 1)
    except OSError:
        logger.info("review result: no agent report at %s", path)
        return {}
    if len(raw) > _MAX_REPORT_BYTES:
        logger.warning(
            "review result: agent report at %s exceeds %s bytes", path, _MAX_REPORT_BYTES
        )
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("review result: agent report at %s is not valid JSON", path)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("review result: agent report at %s is not an object", path)
        return {}
    return parsed


def stages_from_agent_report(report: dict) -> list[StageReport]:
    """The stages the agent reported, with an absent or unusable claim failing closed.

    A stage is only recorded as ``completed`` when the report says exactly that. Every
    other value — missing, misspelled, a non-string, a claim about a stage this
    contract does not model — becomes an inconclusive stage carrying the reason.

    That direction is the whole point. The observed security-only runs would have
    produced no functional stage at all; here that yields ``functional: failed`` with
    "the reviewer did not report" as the detail, which
    :func:`_local_blockers` and the contract both read as non-approving.
    """
    claimed = report.get("stages")
    claims: dict[str, object] = {}
    if isinstance(claimed, dict):
        claims = claimed

    stages: list[StageReport] = []
    for name in (FUNCTIONAL_STAGE, SECURITY_STAGE):
        claim = claims.get(name)
        if claim == "completed":
            stages.append(StageReport(name, "completed"))
            continue
        if name == SECURITY_STAGE and claim is None:
            # Only `functional` is mandatory. A functional-only review is legitimate
            # and inventing a `not-run` security stage would report a skip that was
            # never owed.
            continue
        detail = _stage_detail(report, name, claim)
        stages.append(StageReport(name, "failed" if claim is not None else "not-run", detail))
    return stages


def _stage_detail(report: dict, name: str, claim: object) -> str:
    """Why a stage is not being recorded as completed. Never blank, and bounded."""
    supplied = report.get("stage_details")
    if isinstance(supplied, dict):
        text = supplied.get(name)
        if isinstance(text, str) and text.strip():
            # The agent's own explanation, truncated. Useful to an operator and
            # load-bearing for nothing: the outcome above was decided before this was
            # read, so no wording here can turn an unfinished stage into a finished one.
            return text.strip()[:_MAX_DETAIL_CHARS]
    if claim is None:
        return f"the reviewer did not report a {name} stage, so it cannot be treated as concluded"
    return (
        f"the reviewer reported {name} stage outcome {str(claim)[:60]!r}, which is not a "
        "completed stage"
    )


def findings_from_agent_report(report: dict) -> list[FindingReport]:
    """The findings the agent reported, normalised so an unknown value cannot clear one.

    Severity and disposition are mapped through the contract's closed vocabularies and
    anything unrecognised becomes the *non-permissive* member: an unknown severity is
    ``blocking`` and an unknown disposition is ``open``. A typo must not be the reason
    a defect stops blocking, which is the direction every other closed vocabulary in
    this codebase chooses.

    A finding claiming ``resolved`` with no evidence reference is downgraded to
    ``acknowledged`` rather than dropped or passed through: the contract would reject
    the document outright ("fixed, trust me"), and silently discarding the finding
    would hide a defect the reviewer actually saw. Neither disposition clears it.
    """
    claimed = report.get("findings")
    if not isinstance(claimed, list):
        return []

    findings: list[FindingReport] = []
    seen: set[str] = set()
    for index, entry in enumerate(claimed[:_MAX_FINDINGS]):
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("finding_id")
        finding_id = raw_id.strip()[:120] if isinstance(raw_id, str) and raw_id.strip() else ""
        if not finding_id:
            # Positional rather than dropped: an unlabelled finding is still a finding,
            # and dropping it would lose a defect over a formatting fault.
            finding_id = f"unlabelled-finding-{index + 1}"
        if finding_id in seen:
            finding_id = f"{finding_id}-{index + 1}"
        seen.add(finding_id)

        stage = entry.get("stage")
        summary = entry.get("summary")
        refs = _evidence_refs(entry.get("evidence_refs"))
        severity = _SEVERITIES.get(str(entry.get("severity", "")).strip().lower(), "blocking")
        disposition = _DISPOSITIONS.get(str(entry.get("disposition", "")).strip().lower(), "open")
        if severity == "blocking" and disposition == "resolved" and not refs:
            disposition = "acknowledged"

        findings.append(
            FindingReport(
                finding_id=finding_id,
                stage=stage if stage in {FUNCTIONAL_STAGE, SECURITY_STAGE} else FUNCTIONAL_STAGE,
                severity=severity,
                disposition=disposition,
                summary=(
                    summary.strip()[:_MAX_DETAIL_CHARS]
                    if isinstance(summary, str) and summary.strip()
                    else "the reviewer recorded no description for this finding"
                ),
                evidence_refs=refs,
            )
        )
    return findings


def _evidence_refs(claimed: object) -> tuple[dict[str, object], ...]:
    """Normalise reported evidence into references. Anything else is discarded.

    ``head_bound`` defaults to **True** for an unspecified value: the consequence of a
    wrong guess is that a head change marks the reference stale and the reviewer is
    asked again, which is the recoverable direction. Defaulting to False would let a
    test result from an old commit survive a head change as if it were current.
    """
    if not isinstance(claimed, list):
        return ()
    refs: list[dict[str, object]] = []
    for entry in claimed[:_MAX_REFS_PER_FINDING]:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        ref = entry.get("ref")
        if not isinstance(kind, str) or not kind.strip():
            continue
        if not isinstance(ref, str) or not ref.strip():
            continue
        body: dict[str, object] = {
            "kind": kind.strip()[:60],
            # Bounded and copied: a reference is a pointer the owning store resolves,
            # so an over-long one is a producer fault rather than data to preserve.
            "ref": ref.strip()[:500],
            "head_bound": entry.get("head_bound") is not False,
        }
        summary = entry.get("summary")
        if isinstance(summary, str) and summary.strip():
            body["summary"] = summary.strip()[:_MAX_DETAIL_CHARS]
        refs.append(body)
    return tuple(refs)


def verdict_from_agent_report(report: dict, *, stages: list[StageReport]) -> str:
    """The verdict, which the agent may only ever *narrow*.

    A report may say ``request-changes`` and be believed. It may say ``approve`` and be
    believed only when the stages it produced actually concluded — and even then the
    publication block decides whether that approval was recorded anywhere, and the
    contract refuses an ``approve`` the evidence does not support.

    Anything unrecognised is ``incomplete``. An agent whose verdict field is a typo has
    not approved anything.
    """
    claimed = str(report.get("verdict", "")).strip().lower()
    if claimed == "approve" and all(stage.outcome == "completed" for stage in stages):
        return "approve"
    if claimed in {"request-changes", "request_changes", "changes-requested"}:
        return "request-changes"
    if claimed == "approve":
        # An approval whose own stages did not conclude. Reported as the incomplete
        # review it is rather than refused, so the artifact still records what ran.
        logger.warning("review result: 'approve' claimed with an inconclusive stage")
        return "incomplete"
    return "incomplete"


def _narrowed_verdict(verdict: str, publication: dict[str, object]) -> str:
    """Narrow ``approve`` to ``incomplete`` when no verdict was ever attempted.

    ``verdict_from_agent_report`` decides from the stages, which is all it can see;
    whether the verdict was *attempted* is only known once the publication block
    exists. The contract refuses `approve` over `not-attempted` — deliberately, since
    that pairing is the silent-skip failure it was written to expose — so composing the
    two independently produced a document that could never validate. Before the image
    carried the validator that document was simply written and refused later by the
    gateway; now it would be refused here, which is better but still means an artifact
    for a concluded review is never produced.

    So the narrowing happens where both facts are in hand. `failed` and `refused` are
    NOT narrowed: those are honest reports of an attempt the provider rejected, the
    contract permits `approve` over them, and downgrading them would erase the
    distinction between "the provider said no" and "we never asked".
    """
    if verdict == "approve" and publication.get("outcome") == "not-attempted":
        logger.warning(
            "review result: 'approve' claimed with no publication attempted; recording "
            "'incomplete' — a verdict that was never submitted cannot approve anything"
        )
        return "incomplete"
    return verdict


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


def _contract_rejection(document: dict[str, object]) -> str | None:
    """Why the shared validator rejects this document, or ``None`` if it accepts it.

    ``None`` is also returned when the artifact does not carry the validator, because
    there is nothing to disagree with — an absent contract is a build problem
    (``lib/contract_selfcheck.py``), not a reason to discard a delivered review's
    evidence. The distinction matters: this function answers "does the contract refuse
    this?", and "not checked" is deliberately not reported as "refused".
    """
    models = contract_models()
    if models is None:
        return None
    try:
        models.ReviewResult.model_validate(document)
    except Exception as exc:  # noqa: BLE001 - any rejection must be reported, not raised
        # Bounded: a pydantic error quotes the offending values, and this string goes
        # into a public pull-request comment.
        return str(exc).replace("\n", " ")[:400]
    return None


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
            # Provisional when the caller named no id: the derived id has to see the
            # assembled content to distinguish a changed result from a replay, and
            # `_content_fingerprint` reads none of the fields that depend on it, so
            # stamping the real id after assembly below is not circular.
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

    if not result_id:
        # Now that the content exists, bind the identity to it. Two results from one
        # reviewer invocation at one head — the failed publication and its authorized
        # retry — differ here, so they no longer fold onto a single immutable ledger
        # observation. A byte-identical redelivery still lands on the same id.
        document["result_id"] = _default_result_id(reviewed_head_sha, document)

    # Validate against the normative validator before writing, now that the image
    # carries it. The producer's own rules cannot catch a disagreement with the
    # contract — that is what being a second implementation means — so previously a
    # malformed artifact travelled all the way to the gateway to be refused
    # `malformed_result`, where the reviewer run was long gone and the reason reached
    # nobody who could act on it. Refused here, the reason is in the run's own closing
    # comment. Skipped, with that stated, when the artifact does not carry the
    # validator; `lib/contract_selfcheck.py` makes that a failed build.
    invalid = _contract_rejection(document)
    if invalid:
        logger.warning("review result rejected by the shared contract: %s", invalid)
        return (
            "> **Review evidence not produced.** The structured result did not satisfy the "
            f"shared review contract: {invalid}. This review is prose only: it grants no "
            "approval and the engine cannot treat it as evidence about this revision."
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


def reviewer_evidence_note(
    *,
    repo: str,
    pr_number: int,
    provider_repository_id: int,
    provider_pr_node_id: str,
    reviewed_head_sha: str,
    report_path: str | None = None,
    result_path: str | None = None,
) -> str:
    """The single call site: produce this reviewer run's evidence. Never raises.

    Composes the pieces above into the one function ``entrypoint.py`` invokes, so the
    call site stays a single line. That matters beyond tidiness — the entrypoint hook
    is deliberately minimal because #4529 holds a separate reservation in the same
    file, and both contracts should survive each other.

    The division of trust is the whole design:

    * ``repo``, ``pr_number``, the immutable identities and ``reviewed_head_sha`` come
      from the caller, which read them from the dispatch envelope and from the commit
      it actually checked out and verified before exec.
    * Scope, authority and the authoring run come from the server-published dispatch,
      inside :func:`build_review_result`, where no caller can reach them.
    * Stages, findings and the claimed verdict come from the agent's report and are
      normalised so that every unrecognised or absent value fails closed.
    * Publication comes from the agent's report too, but only as an
      ``adp_review``-shaped result whose ``verdict_recorded`` flag decides — the one
      field that cannot be talked around.

    Returns "" when this is not an engine review dispatch.
    """
    if not review_expected():
        return ""

    report = read_agent_report(
        report_path
        or os.environ.get(AGENT_REPORT_PATH_ENV, "").strip()
        or DEFAULT_AGENT_REPORT_PATH
    )
    stages = stages_from_agent_report(report)
    submission = report.get("submission")
    publication = publication_from_adp_review(
        submission if isinstance(submission, dict) else None,
        reviewed_head_sha=reviewed_head_sha,
    )
    return review_result_note(
        repo=repo,
        provider_repository_id=provider_repository_id,
        pr_number=pr_number,
        provider_pr_node_id=provider_pr_node_id,
        reviewed_head_sha=reviewed_head_sha,
        verdict=_narrowed_verdict(
            verdict_from_agent_report(report, stages=stages), publication
        ),
        stages=stages,
        publication=publication,
        findings=findings_from_agent_report(report),
        evidence_refs=list(_evidence_refs(report.get("evidence_refs"))),
        reviewer_identity=_reported_identity(report),
        path=result_path,
    )


def _reported_identity(report: dict) -> str | None:
    """The provider login the verdict was published under, if the report names one.

    Advisory and bounded. Nothing is decided by it: a shared bot identity is exactly
    why provider-side independent approval is a separate requirement, so this is for a
    human reading the record.
    """
    submission = report.get("submission")
    if isinstance(submission, dict):
        identity = submission.get("identity")
        if isinstance(identity, str) and identity.strip():
            return identity.strip()[:200]
    return None


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


#: The parts of a review result whose change makes it a *different* result. Named
#: explicitly rather than "everything except a deny-list" so a field added to the
#: contract later cannot silently start or stop distinguishing two results: a new
#: field is non-identifying until someone adds it here on purpose.
#:
#: ``observed_at`` is deliberately excluded. It moves on every call, and including it
#: would give a byte-identical redelivery of one review a fresh identity — turning
#: every SQS retry into another recorded review, which is the failure the derived id
#: exists to prevent.
_IDENTIFYING_KEYS = ("verdict", "stages", "findings", "evidence_refs", "publication")


def _content_fingerprint(document: dict[str, object]) -> str:
    """A short digest of what this result actually says.

    Why the result id cannot just be run + head: a publication retry inside the SAME
    reviewer invocation keeps both. The reproduced sequence is one run whose verdict
    failed to publish with HTTP 401 and then published successfully on the unchanged
    head — two genuinely different results, one identity. Downstream those collapse
    onto a single immutable ledger observation, and the second is refused as
    ``action_already_settled`` while the first, incomplete, record is what survives.

    So identity follows content: the verdict, what ran, what was found, what it
    relies on, and what publication returned. The publication block is included
    precisely because that is the field that differs across the retry — a fingerprint
    covering only the review body would reproduce the collision exactly.

    ``sort_keys`` so two assemblies of the same result agree regardless of dict
    ordering, and the components are length-prefixed for the reason
    ``_result_identity`` gives on the consumer side: a bare join is not injective.
    """
    material = "".join(
        f"{key}={len(part)}:{part}"
        for key in _IDENTIFYING_KEYS
        for part in (json.dumps(document.get(key), sort_keys=True, separators=(",", ":")),)
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _default_result_id(reviewed_head_sha: str, document: dict[str, object] | None = None) -> str:
    """A result id derived from the run, the commit reviewed and what the result says.

    Derived rather than random so a retry of the *same* review converges on one id
    instead of accumulating apparent reviews — the reasoning
    ``ActionIntent.operation_key`` documents on the consumer side. Derived from the
    content as well as the run and head so a *changed* result within one invocation
    is a different id, which is what keeps a publication recovery from colliding with
    the failed attempt it is recovering from.

    ``document`` is optional only so the id can be computed before assembly for a
    caller that has nothing to fingerprint yet; omitting it restores the old
    run-and-head-only behaviour and should not be relied on for anything persisted.
    """
    run = os.environ.get("ADP_MESSAGE_ID", "").strip() or "unknown-run"
    stem = f"review-{run}-{reviewed_head_sha[:12]}"
    return stem if document is None else f"{stem}-{_content_fingerprint(document)}"
