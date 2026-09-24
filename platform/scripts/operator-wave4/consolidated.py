"""Produce the four consolidated evidence artifacts by transcribing prior evidence.

W4-03, W4-05, W4-06 and W4-09 each consolidate a family of criteria an earlier wave
evidenced. Wave 4 is not asked to re-run those probes — re-aborting the fixture run
would mutate the very run the other checks describe — so what has to be produced is a
faithful transcription of the owning wave's evidence, carrying the metadata that makes
its CURRENCY checkable: which revision it was taken at, and when.

This module is that transcription, and its whole design is about the two ways a
transcription goes wrong:

**Upgrading.** A source entry that says `failed`, or a proof recorded as `False`, must
arrive at the evaluator saying exactly that. So a recorded `False` is EMITTED, not
refused: refusing it would omit the key, the evaluator would report `not_run`, and
"we did not look" would have replaced "we looked and it was not true" — a downgrade in
severity achieved by a collector being careful. Only a genuinely ABSENT field is
refused.

**Inventing.** A criterion the source document does not carry gets no row. Not a
`not_run` row, not a placeholder — nothing, so the evaluator reports the criterion as
unevidenced and names it. The same for the per-check live observations (the aborted
row's `completed_at`, say): those come from injected lookups that talk to the real
system, and a lookup that could not answer produces a refusal rather than a value.

What this module deliberately does NOT do is decide whether the evidence is still
current. Containment and staleness are computed by the evaluator from the commit graph
(`_assert_contained_in`, `_surface_last_modified`), because a collector that supplied
its own answer would be handing over the conclusion those checks exist to reach. This
module's job is to carry the two INPUTS — `evidenced_revision` and `evidenced_at` —
accurately, and to refuse rather than guess when it cannot.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .collector import Artifact, Measured, Refused, is_refused

_SHA_LENGTH = 40

# What each consolidating check's artifact is called and which wave owns the evidence
# it transcribes. Mirrors the evaluator's `WAVE4_CONSOLIDATED_SOURCES` for the two
# fields a collector needs; the surfaces list is deliberately NOT mirrored, because
# staleness is the evaluator's computation and a copy here would be a second place for
# it to drift.
CONSOLIDATED_ARTIFACTS: dict[str, dict[str, Any]] = {
    "W4-03": {"artifact": "wave4_steering_evidence", "wave": 3},
    "W4-05": {"artifact": "wave4_abort_evidence", "wave": 3},
    "W4-06": {"artifact": "wave4_security_matrix", "wave": 3},
    "W4-09": {"artifact": "wave4_runtime_comparison", "wave": 2},
}


def _criteria_from(
    document: Mapping[str, Any], acceptance_ids: Sequence[str], *, artifact: str
) -> Measured:
    """The per-AC map, transcribed entry by entry from the owning wave's evidence.

    An entry missing its status, its evidence or its liveness flag is DROPPED rather
    than completed. The evaluator then reports that criterion as unevidenced and names
    it, which is the true state — whereas a row assembled around the missing field
    would report the criterion as evidenced by something nobody recorded.
    """
    source = document.get("criteria")
    if not isinstance(source, Mapping):
        return Refused(
            f"{artifact}: the source evidence carries no 'criteria' map "
            f"({type(source).__name__}), so there is nothing to transcribe"
        )
    rows: dict[str, Any] = {}
    for acceptance_id in acceptance_ids:
        entry = source.get(acceptance_id)
        if not isinstance(entry, Mapping):
            continue
        status, evidence = entry.get("status"), entry.get("evidence")
        if not status or not evidence:
            continue
        row = {"status": str(status), "evidence": evidence}
        # `live` is carried through when the source recorded it, and left ABSENT when
        # it did not. W4-10 is the check that requires it, and it requires a boolean —
        # so an unrecorded value must reach it as a gap, not as a default.
        if isinstance(entry.get("live"), bool):
            row["live"] = entry["live"]
        rows[acceptance_id] = row
    if not rows:
        return Refused(
            f"{artifact}: not one of {list(acceptance_ids)} could be transcribed from the source "
            "evidence, so this check has no evidence rather than partial evidence"
        )
    return rows


def _carry_from(
    document: Mapping[str, Any], proofs: Sequence[str], *, artifact: str
) -> dict[str, Measured]:
    """Each named field, as the source recorded it — including recorded falsity.

    Used for two kinds of field, because they need identical treatment: the named
    proofs a wave-4 row lists individually (`fifo_order_proven`, …) and the recorded
    observations a row requires verbatim (`finalized_comment_count`,
    `aborted_renderers`). Both are the owning wave's to state and this module's only to
    carry.

    The asymmetry here is the point. An absent proof is a refusal (the evaluator will
    report `not_run`, naming the missing key); a proof recorded as `False` is emitted
    verbatim (the evaluator will FAIL on it). Collapsing the two would let a collector
    turn a known-failing proof into an unmeasured one, which is a softer report of a
    worse fact.
    """
    measured: dict[str, Measured] = {}
    for proof in proofs:
        if proof not in document:
            measured[proof] = Refused(
                f"{artifact}: the source evidence records no {proof!r}. The wave-4 row names this proof "
                "individually, so an absent one cannot be covered by the others"
            )
        else:
            measured[proof] = document[proof]
    return measured


def collect_consolidated(
    check_id: str,
    *,
    read_evidence: Callable[[int], Mapping[str, Any]],
    acceptance_ids: Sequence[str],
    proofs: Sequence[str] = (),
    extra_fields: Mapping[str, Measured] | None = None,
    evaluation: str | None = None,
    fixture_identity: Mapping[str, Any],
) -> Artifact:
    """Assemble one consolidated artifact from the owning wave's evidence document.

    `read_evidence(wave)` returns that wave's per-criterion evidence as the wave that
    produced it wrote it. Injected, so the tests drive this through a controlled
    transport and so there is exactly one place the real read is configured.

    `extra_fields` carries the per-check observations that are not transcriptions —
    the aborted row read, the browser-derived security fields — already measured by
    their own collectors. They are passed in rather than measured here because each
    talks to a different system, and a module that reached into DynamoDB, the gateway
    and a Playwright report would have four reasons to fail with one error message.
    """
    spec = CONSOLIDATED_ARTIFACTS[check_id]
    artifact = Artifact(spec["artifact"])
    wave = spec["wave"]

    try:
        document = read_evidence(wave)
    except Exception as exc:  # noqa: BLE001 - any read failure is a refusal
        document = None
        failure = Refused(
            f"{spec['artifact']}: cannot read wave {wave}'s evidence document: {exc}"
        )
    else:
        failure = None
        if not isinstance(document, Mapping):
            document = None
            failure = Refused(
                f"{spec['artifact']}: wave {wave}'s evidence document is "
                f"{type(document).__name__}, not an object"
            )

    if document is None:
        # Every field is refused naming the ONE cause. Four unexplained gaps would send
        # an operator looking for four problems.
        for key in ("wave", "evaluation", "criteria", "evidenced_revision", "evidenced_at", *proofs):
            artifact.set(key, failure)
        artifact.set("fixture_identity", dict(fixture_identity))
        artifact.update(dict(extra_fields or {}))
        return artifact

    # Preserve the source identity. The caller's expectation must not relabel a
    # document from another evaluation as the requested wave's evidence.
    recorded_wave = document.get("wave", wave)
    artifact.set("wave", recorded_wave)
    recorded_evaluation = document.get("evaluation")
    if not recorded_evaluation:
        artifact.set("evaluation", Refused(f"{spec['artifact']}: source evidence names no evaluation"))
    elif evaluation is not None and str(recorded_evaluation) != str(evaluation):
        artifact.set("evaluation", Refused(
            f"{spec['artifact']}: source evaluation {recorded_evaluation!r} differs from expected {evaluation!r}"
        ))
    else:
        artifact.set("evaluation", str(recorded_evaluation))
    artifact.set("criteria", _criteria_from(document, acceptance_ids, artifact=spec["artifact"]))

    revision = document.get("evidenced_revision")
    artifact.set(
        "evidenced_revision",
        revision
        if isinstance(revision, str) and len(revision) == _SHA_LENGTH
        else Refused(
            f"{spec['artifact']}: wave {wave}'s evidence records evidenced_revision {revision!r}, which "
            "is not a full 40-character SHA. Which build these observations describe is part of the "
            "observation, and it is what the evaluator's containment check needs"
        ),
    )
    evidenced_at = document.get("evidenced_at")
    artifact.set(
        "evidenced_at",
        evidenced_at
        if isinstance(evidenced_at, str) and evidenced_at.strip()
        else Refused(
            f"{spec['artifact']}: wave {wave}'s evidence records evidenced_at {evidenced_at!r}. Without "
            "an orderable instant the evidence cannot be shown to postdate the code it describes, so "
            "staleness is unanswerable rather than absent"
        ),
    )
    artifact.update(_carry_from(document, proofs, artifact=spec["artifact"]))
    artifact.update(dict(extra_fields or {}))
    artifact.set("fixture_identity", dict(fixture_identity))
    return artifact


def measure_aborted_row(
    run_id: str,
    *,
    row_lookup: Callable[[str], Measured],
) -> Measured:
    """The aborted run's ACTUAL row, read rather than described.

    W4-05's row demands "the actual row has completed_at", which is the difference
    between a UI that renders a terminal state and a record that is one. So this reads
    the row and emits what it found — including a status that is not `aborted`, which
    the evaluator then fails on. A collector that refused a wrong status would convert
    a real defect into a missing measurement.
    """
    answer = row_lookup(run_id)
    if is_refused(answer):
        return Refused(f"completed_at_observed: {answer.reason}")
    if not isinstance(answer, Mapping):
        return Refused(
            f"completed_at_observed: the row lookup returned {type(answer).__name__}, not the row"
        )
    observed = {key: answer.get(key) for key in ("run_id", "status", "completed_at")}
    absent = sorted(key for key, value in observed.items() if not value)
    if absent:
        return Refused(
            f"completed_at_observed: the row lookup answered without {absent}. A read that cannot say "
            "WHICH row it saw is indistinguishable from a read of a different run"
        )
    return observed


def measure_stats_source(
    endpoint: str,
    *,
    get: Callable[[str], tuple[int, Any]],
) -> tuple[Measured, Measured]:
    """The live stats read: its provenance, and the keys the response actually carried.

    Returns the pair because they answer two different questions and the evaluator asks
    them separately — a schema can match perfectly on fabricated data, so the key list
    is not evidence of a live read and the provenance record is not evidence of parity.

    A non-200 is emitted as the status it was, WITH no key list. That combination is
    the honest one: the response did not serve the schema, so there are no served keys
    to report, and reporting the keys of an error body as the stats schema would be the
    invented-fields case the row forbids.
    """
    try:
        status, body = get(endpoint)
    except Exception as exc:  # noqa: BLE001
        refusal = Refused(f"stats_source: GET {endpoint} failed: {exc}")
        return refusal, Refused(f"stats_response_keys: {refusal.reason}")
    source: dict[str, Any] = {"live": True, "endpoint": endpoint, "status": status}
    if status != 200:
        return source, Refused(
            f"stats_response_keys: GET {endpoint} returned {status}, so no stats schema was served and "
            "the keys of an error body are not the response's keys"
        )
    if not isinstance(body, Mapping):
        return source, Refused(
            f"stats_response_keys: GET {endpoint} returned 200 with a "
            f"{type(body).__name__} body, which has no top-level keys to compare"
        )
    return source, sorted(str(key) for key in body)


__all__ = [
    "CONSOLIDATED_ARTIFACTS",
    "collect_consolidated",
    "measure_aborted_row",
    "measure_stats_source",
]
