"""Compile the `wave4_evidence_index` artifact from evidence that already exists.

W4-10's row asks for an index of "exactly all 37 acceptance IDs, each with an owner
and passing compatible source/deployment evidence". That is the single most tempting
artifact in the whole evaluation to hand-write: 37 rows of `{"status": "passed"}` is
twenty minutes of typing and it satisfies every structural check the evaluator makes
about SHAPE.

So this module refuses to be able to type one. Every row it emits is DERIVED, and
there are exactly two places a row may come from:

1. **A consolidated evidence artifact's own `criteria` map** — the steering, abort,
   security and runtime artifacts each carry per-AC entries recorded by the wave that
   made those observations. The row's status, evidence and liveness are copied from
   there; its revision comes from that artifact's `evidenced_revision`.
2. **A previous evaluator report's `checks` map** — for the criteria whose evidence is
   the browser capture (AC-F3 and the pause family), where the thing that establishes
   "this passed" is the EVALUATOR's own verdict on the capture, not a field anyone
   wrote. `result.json` is written by `agent-control-eval.py`; reading it is reading a
   machine's conclusion rather than an operator's claim.

A criterion with no source in either place is REFUSED, so it is absent from the
emitted index — and the evaluator then reports "the evidence index has no entry for
[AC-x]", which is the true state. The failure mode this closes is the one that matters
most: an index that is complete because it was authored to be complete.

**Why reading a prior report is not circular.** The index is compiled BETWEEN two
evaluator runs: run the wave, compile from what that run observed, re-run so W4-10 can
reconcile the index against the run in front of it. `check_w4_10` compares the index's
coverage against THIS run's `emitted_ids` and skips wave 4 in its own `evaluations`
loop, so a stale or flattering index cannot certify the run that reads it. What the
prior report contributes is verdicts on individual captures; what it cannot contribute
is wave 4's acceptance.

**Statuses are never upgraded.** A source entry that says `failed` or `not_run` is
emitted with that status, not omitted. Omitting it would read as "no entry" — true but
less useful — whereas emitting it makes the evaluator name the criterion AND its real
state. The one thing this module will not do is write `passed` where its source did
not.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .collector import Artifact, Measured, Refused, is_refused
from .preflight import measure_prior_wave

# Where each family of acceptance IDs gets its row, and who owns it.
#
# `artifact` names the consolidated evidence file whose `criteria` map carries the
# per-AC entries. Declared per-family rather than searched for across all artifacts:
# if AC-A4 could be satisfied by an entry in whichever file happened to mention it,
# then filing steering evidence under the abort criteria would silently work, and the
# index's job is precisely to keep each criterion attached to the evidence that
# actually covers it.


@dataclass(frozen=True)
class CriteriaSource:
    """One consolidated artifact, and the criteria it is allowed to evidence."""

    artifact: str
    owner: str
    acceptance_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReportSource:
    """One evaluator-verdict-backed family: a check ID and the criteria it carries.

    Used where the evidence is a browser capture. There is no per-AC record in the
    capture — a Playwright run reports what the DOM did, not which acceptance IDs it
    satisfied — so the thing that maps observations onto criteria is the evaluator's
    check, and its verdict is what this reads.
    """

    check_id: str
    owner: str
    acceptance_ids: tuple[str, ...]


def build_sources(
    *,
    consolidated: Mapping[str, Sequence[str]],
    report_backed: Mapping[str, Sequence[str]],
    owners: Mapping[str, str],
) -> tuple[tuple[CriteriaSource, ...], tuple[ReportSource, ...]]:
    """Turn the caller's declarations into source records, or raise.

    The mappings come from the harness's own manifests (`CHECK_ACCEPTANCE_IDS` keyed by
    the check that carries each family) rather than being written out here. That is
    deliberate: a list of families duplicated in this module would be a second place
    the 37 could drift, and the whole reliability argument for W4-10 is that the number
    is computed from one source.
    """
    criteria_sources = tuple(
        CriteriaSource(
            artifact=artifact,
            owner=owners.get(artifact, "unattributed"),
            acceptance_ids=tuple(ids),
        )
        for artifact, ids in sorted(consolidated.items())
    )
    report_sources = tuple(
        ReportSource(
            check_id=check_id,
            owner=owners.get(check_id, "unattributed"),
            acceptance_ids=tuple(ids),
        )
        for check_id, ids in sorted(report_backed.items())
    )
    return criteria_sources, report_sources


def _row_from_criteria(
    source: CriteriaSource,
    acceptance_id: str,
    payload: Mapping[str, Any] | None,
) -> Measured:
    """One index row, copied out of a consolidated artifact's own criteria entry."""
    if payload is None:
        return Refused(
            f"{acceptance_id}: {source.artifact} was not collected, so the criterion it evidences has "
            "no source. An index row written without one would be the authored-completeness failure "
            "this module exists to prevent"
        )
    criteria = payload.get("criteria")
    if not isinstance(criteria, Mapping):
        return Refused(
            f"{acceptance_id}: {source.artifact} carries no 'criteria' map ({type(criteria).__name__}), "
            "so there is nothing to derive this row from"
        )
    entry = criteria.get(acceptance_id)
    if not isinstance(entry, Mapping):
        return Refused(
            f"{acceptance_id}: {source.artifact}'s criteria map has no entry for it, so the criterion is "
            "unevidenced. That is the state the index must report, not one to fill in"
        )
    status = entry.get("status")
    if not status:
        return Refused(
            f"{acceptance_id}: its entry in {source.artifact} records no status, and a row with a blank "
            "status would be indistinguishable from an unevidenced one once a reader skims it"
        )
    evidence = entry.get("evidence")
    if not evidence:
        return Refused(
            f"{acceptance_id}: its entry in {source.artifact} records status {status!r} with no evidence "
            "reference. A status with nothing behind it is the substitute for evidence both this "
            "collector and the evaluator refuse"
        )
    revision = payload.get("evidenced_revision")
    if not isinstance(revision, str) or len(revision) != 40:
        return Refused(
            f"{acceptance_id}: {source.artifact} records evidenced_revision {revision!r}, which is not a "
            "full 40-character SHA, so this row's currency could not be checked by anything downstream"
        )
    # `live` is read, never inferred. An artifact that did not record whether an
    # observation was live has not answered the question W4-10's row turns on, and
    # defaulting it either way would answer it on the artifact's behalf.
    live = entry.get("live")
    if not isinstance(live, bool):
        return Refused(
            f"{acceptance_id}: its entry in {source.artifact} records live={live!r}, which is not a "
            "boolean. Whether a named live SDK/API/browser check was really live is the distinction the "
            "row turns on, so an unrecorded value cannot be supplied here"
        )
    return {
        "owner": source.owner,
        "evaluation": str(payload.get("evaluation") or ""),
        "revision": revision,
        "evidence": evidence,
        "live": live,
        "status": str(status),
    }


def _row_from_report(
    source: ReportSource,
    acceptance_id: str,
    report: Mapping[str, Any] | None,
    *,
    bundle_revision: Measured,
) -> Measured:
    """One index row, derived from a prior evaluator run's verdict on a check."""
    if report is None:
        return Refused(
            f"{acceptance_id}: no prior evaluator report was available, so {source.check_id}'s verdict on "
            "the browser capture is unknown. The capture's own fields cannot supply it — they say what "
            "the DOM did, not whether that satisfied the criterion"
        )
    checks = report.get("checks")
    if not isinstance(checks, Mapping):
        return Refused(
            f"{acceptance_id}: the prior report carries no 'checks' object, so no verdict can be read "
            "from it"
        )
    entry = checks.get(source.check_id)
    if not isinstance(entry, Mapping):
        return Refused(
            f"{acceptance_id}: the prior report has no result for {source.check_id}, which is the check "
            "that maps the capture's observations onto this criterion"
        )
    status = entry.get("status")
    if not status:
        return Refused(
            f"{acceptance_id}: the prior report's {source.check_id} result records no status"
        )
    evidence = entry.get("evidence")
    if not evidence:
        return Refused(
            f"{acceptance_id}: the prior report's {source.check_id} result carries no evidence list, so "
            "there is nothing for a reviewer to retrieve behind this row"
        )
    if is_refused(bundle_revision):
        return Refused(f"{acceptance_id}: {bundle_revision.reason}")
    declared = entry.get("acceptance_ids")
    # The check whose verdict is being borrowed must be the check that carries this
    # criterion. Without this, a passing Gate/regression check could lend its status to
    # any AC the caller pointed at it.
    #
    # Only checked when the report DECLARES its acceptance IDs: an older report that
    # does not carry them cannot be interrogated about coverage, and inventing the
    # answer either way would be worse than the report's silence.
    if (
        isinstance(declared, Sequence)
        and not isinstance(declared, str)
        and acceptance_id not in {str(value) for value in declared}
    ):
        return Refused(
            f"{acceptance_id}: the prior report's {source.check_id} result declares acceptance IDs "
            f"{sorted(str(value) for value in declared)}, which do not include it. A verdict can "
            "only evidence the criteria its own check carries"
        )
    return {
        "owner": source.owner,
        "evaluation": str(report.get("evaluation") or ""),
        "revision": bundle_revision,
        "evidence": list(evidence),
        # A Playwright run driving a deployed bundle is a live observation by
        # construction: there is no mocked variant of it that could reach this branch,
        # because the row is derived from the evaluator's verdict on a capture whose
        # provenance it checked first (`_browser_run`).
        "live": True,
        "status": str(status),
    }


def compile_index(
    *,
    acceptance_ids: Sequence[str],
    criteria_sources: Sequence[CriteriaSource],
    report_sources: Sequence[ReportSource],
    artifacts: Mapping[str, Mapping[str, Any]],
    prior_report: Mapping[str, Any] | None,
    bundle_revision: Measured,
    compiled_revision: Measured,
    compiled_at: str,
    prior_waves: Sequence[int],
    read_result: Callable[[int], Mapping[str, Any]],
    fixture_identity: Mapping[str, Any],
) -> tuple[Artifact, dict[str, str]]:
    """Assemble `wave4_evidence_index`, plus the per-criterion refusals.

    Returns the artifact and a `{acceptance_id: reason}` map of the rows that could not
    be derived. Both halves are the output: the artifact is what the evaluator reads,
    and the refusal map is what tells the operator which criteria are genuinely
    unevidenced — which is the actionable half and the one that must not end up inside
    the artifact looking like a footnote on otherwise-complete evidence.

    `acceptance_ids` is passed in from `all_acceptance_ids()` rather than recomputed
    here. The index must be compiled against the same set the evaluator will compare
    it to, and two independent derivations of "the 37" could disagree — in which case
    the compiler would be quietly authoring the very mismatch W4-10 exists to detect.
    """
    artifact = Artifact("wave4_evidence_index")
    rows: dict[str, Any] = {}
    refusals: dict[str, str] = {}

    owned: dict[str, Measured] = {}
    for source in criteria_sources:
        payload = artifacts.get(source.artifact)
        for acceptance_id in source.acceptance_ids:
            owned[acceptance_id] = _row_from_criteria(source, acceptance_id, payload)
    for source in report_sources:
        for acceptance_id in source.acceptance_ids:
            row = _row_from_report(
                source, acceptance_id, prior_report, bundle_revision=bundle_revision
            )
            # A criterion claimed by both a consolidated artifact and a report-backed
            # check keeps the artifact's row: the artifact is the wave that made the
            # observation, and the report is a verdict on a capture. Recording the
            # conflict rather than silently picking one is what makes an overlap
            # visible if the manifests ever grow one.
            if acceptance_id in owned and not is_refused(owned[acceptance_id]):
                refusals[f"{acceptance_id} (duplicate source)"] = (
                    f"both {source.check_id} and a consolidated artifact claim it; the artifact's row was "
                    "kept"
                )
                continue
            owned[acceptance_id] = row

    for acceptance_id in acceptance_ids:
        row = owned.get(acceptance_id)
        if row is None:
            refusals[acceptance_id] = (
                "no source declares it: it belongs to no consolidated artifact and to no "
                "report-backed check, so nothing in this collection evidences it"
            )
            continue
        if is_refused(row):
            refusals[acceptance_id] = row.reason
            continue
        rows[acceptance_id] = row

    # An index that derived nothing is refused whole rather than emitted empty. An
    # empty `criteria` object would satisfy the key-presence check and then fail the
    # coverage check with 37 missing IDs — a correct outcome reached by a confusing
    # route, and one that reads as "the index is broken" rather than "no evidence was
    # collected".
    artifact.set(
        "criteria",
        rows
        or Refused(
            "criteria: not one of the 37 criteria could be derived from a collected artifact or a prior "
            "evaluator report, so there is no index to publish"
        ),
    )

    # The prior waves' acceptance, read from their own reports by the same measurement
    # the preflight uses. Shared deliberately: two readings of "was wave 2 accepted"
    # in one collection could disagree, and this artifact and the preflight are
    # compared against each other by the evaluator.
    evaluations: dict[str, Any] = {}
    evaluation_refusals: list[str] = []
    for wave in prior_waves:
        record = measure_prior_wave(wave, read_result=read_result)
        if is_refused(record):
            evaluation_refusals.append(record.reason)
            # Keyed by the WAVE, not by a prefix sliced out of the reason text. The
            # reason's shape is a message, and keying on it made the refusal map's keys
            # depend on the wording of a sentence elsewhere.
            refusals[f"evaluations[{wave}]"] = record.reason
            artifact.refuse_entry("evaluations", str(wave), record.reason)
        else:
            evaluations[str(wave)] = record
    if evaluation_refusals and not evaluations:
        artifact.set(
            "evaluations", Refused("evaluations: " + "; ".join(evaluation_refusals))
        )
    else:
        artifact.set("evaluations", evaluations)

    artifact.set("compiled_at", compiled_at)
    artifact.set("compiled_revision", compiled_revision)
    artifact.set("fixture_identity", dict(fixture_identity))
    return artifact, refusals


__all__ = [
    "CriteriaSource",
    "ReportSource",
    "build_sources",
    "compile_index",
]
