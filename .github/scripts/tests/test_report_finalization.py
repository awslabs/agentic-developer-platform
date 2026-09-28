"""Tests for finalize_security_report.py (issue #4451, unit U12).

Gate coverage: FR-C35 the report re-renders as each item transitions; FR-C36
`final` withheld while any item is in progress and applied when all are terminal,
exactly once; NFR-3 every item reaches merged or stuck, so a run always
finalizes; FR-C38 the report matches no banned pattern and is not written to a
public path; FR-C32 reconciliation enforced rather than displayed.

**Driven through U11, not through synthesized markers.** Every item state in this
suite is produced by `ops_stuck_tracker.apply_transition` and written by its
`write_ops_shard`, and every ledger is read back through U2's `load_and_merge`.
Hand-written status dicts would let this suite pass against a state vocabulary
U11 does not actually emit -- and the seam between the two units is the thing
under test, so faking either side would test nothing that ships.
"""

import hashlib
import json
import re
import subprocess  # nosec B404 - fixed argv, no shell, test-only
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ops_stuck_tracker as u11
from finalize_security_report import (
    FINAL_STAMP_NAME,
    REPORT_NAME,
    STATUS_FINAL,
    STATUS_IN_PROGRESS,
    FinalizationError,
    all_items_terminal,
    assert_no_banned_patterns,
    assert_renderer_agrees,
    build_parser,
    claim_final,
    finalize,
    main,
    next_status,
    read_previous_status,
    report_key,
    report_path,
    stamp_path,
)
from render_security_report import render
from security_agent_ledger import (
    LedgerError,
    build_shard,
    load_and_merge,
    load_schema,
    serialize_shard,
)
from triage_group_findings import load_banned_patterns

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).parent.parent / "finalize_security_report.py"
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/script-tests.yml"

SCHEMA = load_schema()
PATTERNS = load_banned_patterns()

RUN_DATE = "2026-08-30"
TS = "2026-08-30T02:41:00Z"
# Past the 24h no-transition threshold from TS. Used to drive the sweep path
# without sleeping; the threshold itself is x-stuck-rule's, never restated here.
MUCH_LATER = "2026-09-01T02:41:00Z"


# --------------------------------------------------------------------------
# fixture builders -- shards written the way the pipeline writes them
# --------------------------------------------------------------------------


def _write(run_dir: Path, shard: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"shard-{shard['stage']}.json").write_bytes(serialize_shard(shard))


def _run_dir(tmp_path: Path, *, items=(5002, 5003), findings=("f-a1c2", "f-b3d4")):
    """A run mid-flight: scanners done, items filed and dispatched.

    Dispatch goes through U11's `dispatched` event, so the in-progress records
    here are the records U11 actually creates.
    """
    run_dir = tmp_path / RUN_DATE
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "workflow.code-review",
            TS,
            {
                "identified_raw": 7,
                "identified_new_after_dedup": len(findings),
                "new_finding_ids": list(findings),
                "run_duration_seconds": 1800,
                "pentest_cost_usd": 12.5,
            },
            SCHEMA,
        ),
    )
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {
                "daily_epic": 5001,
                "stories_created": len(items),
                "story_ids": list(items),
                "findings_covered": list(findings),
            },
            SCHEMA,
        ),
    )
    for item in items:
        _transition(run_dir, item, "dispatched", now=TS)
    return run_dir


def _transition(run_dir: Path, item: int, event: str, *, now=TS):
    """Apply one U11 event to one item and write its shard. Returns the record.

    Reads the item's current record back off disk rather than threading it
    through the test, so each step operates on the state the previous step
    actually persisted.
    """
    current = u11.read_ops_record(run_dir, item)
    updated = u11.apply_transition(current, event=event, now=now, schema=SCHEMA)
    u11.write_ops_shard(
        run_dir,
        item=item,
        record=updated,
        run_date=RUN_DATE,
        generated_at=now,
        schema=SCHEMA,
    )
    return updated


def _merged(run_dir: Path) -> dict:
    return load_and_merge(run_dir, SCHEMA)


def _status_line(document: str) -> str:
    """The status the document itself carries, read out of the rendered page."""
    return document.split("Status:", 1)[1].split("</p>", 1)[0]


def _quiet_night(tmp_path: Path) -> Path:
    """FR-C2's common night: findings all deduped away, no items filed."""
    run_dir = tmp_path / "quiet"
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "workflow.pentest",
            TS,
            {"identified_raw": 4, "identified_new_after_dedup": 0, "new_finding_ids": []},
            SCHEMA,
        ),
    )
    return run_dir


# --------------------------------------------------------------------------
# FR-C35 -- the report is living
# --------------------------------------------------------------------------


def test_fr_c35_report_is_rerendered_on_every_item_state_transition(tmp_path):
    """The whole point of the unit: the page tracks the night as it resolves.

    Asserted on the report's BYTES, not on the return value -- a state machine
    that computed the right status and left a stale document on disk would pass
    a return-value-only test and fail the operator reading the page.
    """
    run_dir = _run_dir(tmp_path)
    seen = []

    finalize(run_dir, SCHEMA, PATTERNS)
    seen.append(report_path(run_dir).read_text())

    _transition(run_dir, 5002, "merged", now=TS)
    finalize(run_dir, SCHEMA, PATTERNS)
    seen.append(report_path(run_dir).read_text())

    _transition(run_dir, 5003, "merged", now=TS)
    finalize(run_dir, SCHEMA, PATTERNS)
    seen.append(report_path(run_dir).read_text())

    # Each state transition moved the document.
    assert len(set(seen)) == len(seen)
    # ...and the counts it shows are the counts the ledger holds at each point.
    assert "<td>Fixed autonomously</td><td>0</td>" in seen[0]
    assert "<td>Fixed autonomously</td><td>1</td>" in seen[1]
    assert "<td>Fixed autonomously</td><td>2</td>" in seen[2]


def test_a_failed_run_that_changes_no_state_re_renders_identical_bytes(tmp_path):
    """`failed_runs` is tracked in the ledger and deliberately NOT a report
    field: the morning acts on states and reasons, not on retry counts. So a
    failed run below the ceiling leaves the document byte-identical.

    Worth pinning rather than leaving implicit -- it is the observable half of
    U2's purity guarantee (same ledger view in => same bytes out) reaching the
    file on disk, and it is what makes re-rendering on every event cheap and
    safe rather than a source of churn.
    """
    run_dir = _run_dir(tmp_path)
    finalize(run_dir, SCHEMA, PATTERNS)
    before = report_path(run_dir).read_bytes()

    record = _transition(run_dir, 5002, "failed", now=TS)
    assert record["failed_runs"] == 1 and record["status"] == "in_progress"
    finalize(run_dir, SCHEMA, PATTERNS)

    assert report_path(run_dir).read_bytes() == before


def test_fr_c35_every_render_states_which_of_the_two_statuses_it_is(tmp_path):
    """The page always says whether it is still moving or is the final picture.

    A page that is silent on this is worse than either label: the reader cannot
    tell yesterday's record from today's live run.
    """
    run_dir = _run_dir(tmp_path)
    finalize(run_dir, SCHEMA, PATTERNS)
    assert STATUS_IN_PROGRESS in _status_line(report_path(run_dir).read_text())

    _transition(run_dir, 5002, "merged", now=TS)
    _transition(run_dir, 5003, "merged", now=TS)
    finalize(run_dir, SCHEMA, PATTERNS)
    assert STATUS_FINAL in _status_line(report_path(run_dir).read_text())


def test_the_document_is_u2s_renderer_output_byte_for_byte(tmp_path):
    """This unit adds a state machine over U2's renderer; it is not a second
    renderer. If the bytes on disk were ever assembled here instead, they would
    stop matching `render()` and this assert is what notices."""
    run_dir = _run_dir(tmp_path)
    result = finalize(run_dir, SCHEMA, PATTERNS)
    expected = render(_merged(run_dir), SCHEMA)
    assert report_path(run_dir).read_text() == expected
    assert result["report_sha256"] == hashlib.sha256(expected.encode()).hexdigest()


def test_no_report_markup_is_authored_in_this_module():
    """FR-C33's structural half: there is no write path to the document's text
    in this file. A behavioural test cannot distinguish "renders via U2" from
    "renders via U2 today", so the absence of markup is asserted on the source.
    """
    source = SCRIPT.read_text()
    body = "".join(
        line for line in source.splitlines(keepends=True) if not line.lstrip().startswith("#")
    )
    # Strip docstrings, which legitimately discuss the document.
    body = re.sub(r'""".*?"""', "", body, flags=re.DOTALL)
    for markup in ("<html", "<table", "<tr", "<td", "<p>", "<h1", "<h2", "DOCTYPE"):
        assert markup not in body, f"report markup {markup!r} authored in {SCRIPT.name}"


# --------------------------------------------------------------------------
# FR-C36 -- final is withheld, then applied, exactly once
# --------------------------------------------------------------------------


def test_fr_c36_final_is_withheld_while_any_item_is_in_progress(tmp_path):
    """The top blast-radius row: a report claiming a complete picture that is
    not one, with the still-open items invisible to whoever trusted it."""
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)  # one done, one still moving

    result = finalize(run_dir, SCHEMA, PATTERNS)

    assert result["status"] == STATUS_IN_PROGRESS
    assert result["stamped"] is False
    assert not stamp_path(run_dir).exists()
    assert STATUS_FINAL not in _status_line(report_path(run_dir).read_text())


@pytest.mark.parametrize("last_event,expected_status", [("merged", "fixed"), ("failed", "stuck")])
def test_fr_c36_final_is_applied_when_all_items_are_terminal(
    tmp_path, last_event, expected_status
):
    """Both terminal states finalize a night. `stuck` is not a lesser ending:
    an item that cannot go green has still finished, and a run that only
    finalized on all-merged would hang on every genuinely-blocked night."""
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    for _ in range(3):  # three failures is x-stuck-rule's ceiling
        record = _transition(run_dir, 5003, last_event, now=TS)

    assert record["status"] == expected_status
    result = finalize(run_dir, SCHEMA, PATTERNS)
    assert result["status"] == STATUS_FINAL
    assert result["stamped"] is True
    assert STATUS_FINAL in _status_line(report_path(run_dir).read_text())


def test_fr_c36_final_is_stamped_exactly_once(tmp_path):
    """The smoke criterion: the report flips in-progress -> final ONCE.

    The second attempt raises rather than producing a second final report --
    overlapping final reports for one night leave no way to tell which is the
    record.
    """
    run_dir = _run_dir(tmp_path)
    finalize(run_dir, SCHEMA, PATTERNS)
    _transition(run_dir, 5002, "merged", now=TS)
    _transition(run_dir, 5003, "merged", now=TS)

    first = finalize(run_dir, SCHEMA, PATTERNS)
    assert first["stamped"] is True

    with pytest.raises(FinalizationError, match="already stamped final"):
        finalize(run_dir, SCHEMA, PATTERNS)

    # One stamp, and it still describes the report that was stamped.
    stamp = json.loads(stamp_path(run_dir).read_text())
    assert stamp["status"] == STATUS_FINAL
    assert stamp["report_sha256"] == first["report_sha256"]
    assert stamp["report_sha256"] == hashlib.sha256(
        report_path(run_dir).read_bytes()
    ).hexdigest()


def test_exactly_once_is_the_filesystems_guarantee_not_a_prior_check(tmp_path):
    """`claim_final` must fail on an existing stamp even when nothing looked
    first. A check-then-write races two concurrent finalizers into two stamps,
    so the exclusivity has to live at the write itself."""
    run_dir = _run_dir(tmp_path)
    merged = _merged(run_dir)
    claim_final(run_dir, merged, "deadbeef")
    with pytest.raises(FinalizationError, match="already exists"):
        claim_final(run_dir, merged, "deadbeef")


def test_a_stamped_night_is_not_re_stamped_by_a_fresh_process(tmp_path):
    """The stamp is durable state, not in-memory state: a second invocation --
    a retried workflow step -- must refuse, and refuse loudly."""
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    _transition(run_dir, 5003, "merged", now=TS)
    assert main(["--ledger-dir", str(run_dir)]) == 0
    assert main(["--ledger-dir", str(run_dir)]) == 1


def test_status_transition_sequence_over_a_whole_night(tmp_path):
    """The state machine as a sequence, which is how it is actually used: one
    call per item transition, in-progress until the last one lands."""
    run_dir = _run_dir(tmp_path, items=(5002, 5003, 5004), findings=("f-a1c2",))
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {
                "daily_epic": 5001,
                "stories_created": 3,
                "story_ids": [5002, 5003, 5004],
                "findings_covered": ["f-a1c2"],
            },
            SCHEMA,
        ),
    )

    observed = [finalize(run_dir, SCHEMA, PATTERNS)["status"]]
    for item in (5002, 5003, 5004):
        _transition(run_dir, item, "merged", now=TS)
        observed.append(finalize(run_dir, SCHEMA, PATTERNS)["status"])

    assert observed == [STATUS_IN_PROGRESS] * 3 + [STATUS_FINAL]
    assert observed.count(STATUS_FINAL) == 1


def test_quiet_night_is_final_on_its_first_render(tmp_path):
    """FR-C2's common night files no items, so it is finished the moment the
    scanners are. It must still be stamped -- a quiet night that never
    finalizes reads 'delivery in progress' forever."""
    result = finalize(_quiet_night(tmp_path), SCHEMA, PATTERNS)
    assert result["status"] == STATUS_FINAL
    assert result["stamped"] is True


# --------------------------------------------------------------------------
# NFR-3 -- a run always finalizes: the full transition set
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "steps,expected",
    [
        # Each step is (event, when). The times matter: a failed run IS a state
        # transition and re-stamps the clock, so "failed then swept in the same
        # instant" legitimately stays in progress -- the item is being actively
        # retried, not stale. The stale path needs time to pass after the last
        # transition, which is why these carry per-step times rather than one.
        ((("merged", TS),), "fixed"),
        ((("failed", TS), ("merged", TS)), "fixed"),
        ((("failed", TS), ("failed", TS), ("failed", TS)), "stuck"),
        ((("sweep", MUCH_LATER),), "stuck"),
        ((("failed", TS), ("sweep", MUCH_LATER)), "stuck"),
        ((("failed", MUCH_LATER), ("sweep", MUCH_LATER)), "in_progress"),
    ],
)
def test_nfr3_every_path_through_u11s_events_reaches_a_terminal_state(
    tmp_path, steps, expected
):
    """Over U11's full event set, every path ends. This is what makes "a run
    always finalizes" a property rather than a hope: no sequence leaves an item
    able to move forever, including the one where the delivery run vanishes and
    only the sweep ever looks at it again.

    The one non-terminal row is the actively-retried item, and it is here on
    purpose: the state machine must hold that night open rather than stamp it,
    and the next sweep is what ends it (see the test below).
    """
    run_dir = _run_dir(tmp_path, items=(5002,), findings=("f-a1c2",))
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {"stories_created": 1, "story_ids": [5002], "findings_covered": ["f-a1c2"]},
            SCHEMA,
        ),
    )
    for event, when in steps:
        record = _transition(run_dir, 5002, event, now=when)

    assert record["status"] == expected
    if expected == "in_progress":
        assert finalize(run_dir, SCHEMA, PATTERNS)["status"] == STATUS_IN_PROGRESS
        return
    assert u11.is_terminal(record)
    assert all_items_terminal(_merged(run_dir))
    assert finalize(run_dir, SCHEMA, PATTERNS)["status"] == STATUS_FINAL


def test_nfr3_an_actively_retried_item_still_ends_on_a_later_sweep(tmp_path):
    """Closes the one non-terminal row above. Holding a night open for an item
    under active retry is correct; holding it open forever is the bug. A retried
    item is ended by whichever ceiling it reaches first, and the stale path is
    still reachable once the retries stop -- so there is no sequence of events
    after which the night can never finalize.
    """
    run_dir = _run_dir(tmp_path, items=(5002,), findings=("f-a1c2",))
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {"stories_created": 1, "story_ids": [5002], "findings_covered": ["f-a1c2"]},
            SCHEMA,
        ),
    )
    _transition(run_dir, 5002, "failed", now=MUCH_LATER)
    assert finalize(run_dir, SCHEMA, PATTERNS)["status"] == STATUS_IN_PROGRESS

    # The retries stopped; the next sweep a day later ends it.
    record = _transition(run_dir, 5002, "sweep", now="2026-09-03T02:41:00Z")
    assert record["status"] == "stuck"
    assert finalize(run_dir, SCHEMA, PATTERNS)["status"] == STATUS_FINAL


def test_nfr3_a_stalled_item_finalizes_via_the_sweep_not_by_waiting(tmp_path):
    """The failure this unit closes: before the sweep the night is legitimately
    in progress; after it, the same untouched item is terminal and the night
    finalizes. Nothing about the item changed except that time passed."""
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    assert finalize(run_dir, SCHEMA, PATTERNS)["status"] == STATUS_IN_PROGRESS

    _transition(run_dir, 5003, "sweep", now=MUCH_LATER)
    result = finalize(run_dir, SCHEMA, PATTERNS)

    assert result["status"] == STATUS_FINAL
    # And the morning is told WHY it stopped, not just that it did (FR-C37).
    assert "24h with no state transition" in report_path(run_dir).read_text()


def test_a_stuck_item_reports_its_reason_in_the_final_report(tmp_path):
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    for _ in range(3):
        _transition(run_dir, 5003, "failed", now=TS)

    finalize(run_dir, SCHEMA, PATTERNS)
    assert "3 failed developer runs" in report_path(run_dir).read_text()


# --------------------------------------------------------------------------
# FR-C32 -- reconciliation is enforced, not displayed
# --------------------------------------------------------------------------


def test_fr_c32_terminal_but_unreconciled_run_raises_instead_of_hanging(tmp_path):
    """The "never stamped final" row of the impact table.

    U2's `is_final` returns False both when an item is still moving and when the
    totals do not add up. Inheriting that would leave this night reading
    'delivery in progress' forever with nobody told. It must fail loudly.
    """
    run_dir = _run_dir(tmp_path)
    # Three items filed, two tracked: the third silently vanished between triage
    # and delivery, which is exactly what the counts exist to catch.
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {
                "daily_epic": 5001,
                "stories_created": 3,
                "story_ids": [5002, 5003, 5004],
                "findings_covered": ["f-a1c2", "f-b3d4"],
            },
            SCHEMA,
        ),
    )
    _transition(run_dir, 5002, "merged", now=TS)
    _transition(run_dir, 5003, "merged", now=TS)

    with pytest.raises(FinalizationError, match="do not reconcile"):
        finalize(run_dir, SCHEMA, PATTERNS)
    assert not stamp_path(run_dir).exists()


def test_fr_c32_the_error_names_the_unaccounted_item(tmp_path):
    """A count-only failure is unactionable; the item number is the fix."""
    run_dir = _run_dir(tmp_path, items=(5002,), findings=("f-a1c2",))
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {"stories_created": 2, "story_ids": [5002, 5009], "findings_covered": ["f-a1c2"]},
            SCHEMA,
        ),
    )
    _transition(run_dir, 5002, "merged", now=TS)
    with pytest.raises(FinalizationError, match="5009"):
        finalize(run_dir, SCHEMA, PATTERNS)


def test_fr_c32_an_item_tracked_but_never_filed_blocks_finalization(tmp_path):
    """The mirror image of the missing-item case: delivery reported progress on
    an item triage never filed. Both directions are unaccounted work, so both
    block the stamp and both name the number."""
    run_dir = _run_dir(tmp_path, items=(5002, 5003), findings=("f-a1c2",))
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {"stories_created": 2, "story_ids": [5002], "findings_covered": ["f-a1c2"]},
            SCHEMA,
        ),
    )
    _transition(run_dir, 5002, "merged", now=TS)
    _transition(run_dir, 5003, "merged", now=TS)

    with pytest.raises(FinalizationError, match="unfiled.*5003"):
        finalize(run_dir, SCHEMA, PATTERNS)
    assert not stamp_path(run_dir).exists()


def test_fr_c32_uncovered_finding_blocks_finalization(tmp_path):
    """The identity holds and every item is terminal, but a finding got no item
    at all. Findings silently vanishing between dedup and the report is the
    other half of the reconciliation claim."""
    run_dir = _run_dir(tmp_path, items=(5002,), findings=("f-a1c2", "f-b3d4"))
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {"stories_created": 1, "story_ids": [5002], "findings_covered": ["f-a1c2"]},
            SCHEMA,
        ),
    )
    _transition(run_dir, 5002, "merged", now=TS)
    with pytest.raises(FinalizationError, match="covered by no item"):
        finalize(run_dir, SCHEMA, PATTERNS)


def test_fr_c32_reconciliation_identity_holds_on_the_merged_ledger(tmp_path):
    """fixed + stuck + in_progress == items created, read off the real merged
    ledger of a night driven through U11 -- not off a hand-built dict."""
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    for _ in range(3):
        _transition(run_dir, 5003, "failed", now=TS)

    from render_security_report import build_view

    view = build_view(_merged(run_dir), SCHEMA)
    assert view["fixed"] + view["stuck"] + view["in_progress"] == view["stories_created"]
    assert view["reconciliation"]["ok"] is True
    assert finalize(run_dir, SCHEMA, PATTERNS)["status"] == STATUS_FINAL


def test_an_unreconciled_run_still_in_progress_re_renders_normally(tmp_path):
    """Reconciliation is only decisive at the finalization boundary. Mid-flight
    it is expected to be unsettled -- failing there would break the living
    report on every night, which is the opposite of the intent."""
    run_dir = _run_dir(tmp_path)
    _write(
        run_dir,
        build_shard(
            RUN_DATE,
            "triage",
            TS,
            {
                "daily_epic": 5001,
                "stories_created": 3,
                "story_ids": [5002, 5003, 5004],
                "findings_covered": ["f-a1c2", "f-b3d4"],
            },
            SCHEMA,
        ),
    )
    assert finalize(run_dir, SCHEMA, PATTERNS)["status"] == STATUS_IN_PROGRESS


# --------------------------------------------------------------------------
# FR-C38 -- no exploit detail, no public location
# --------------------------------------------------------------------------


def test_fr_c38_report_matches_no_committed_banned_pattern(tmp_path):
    """Scanned against U9's committed list, not a copy of it: one list means a
    pattern added for the issue path also guards the report path."""
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    for _ in range(3):
        _transition(run_dir, 5003, "failed", now=TS)
    finalize(run_dir, SCHEMA, PATTERNS)

    document = report_path(run_dir).read_text()
    hits = [p["id"] for p in PATTERNS if p["regex"].search(document)]
    assert hits == []


@pytest.mark.parametrize(
    "payload",
    [
        "curl -X POST https://gateway/admin",
        "Steps to reproduce: open the console",
        "Authorization: Bearer abcdefghijklmnop",
        "' OR '1'='1",
    ],
)
def test_fr_c38_a_report_carrying_reproduction_detail_is_rejected(payload):
    """The scan is load-bearing, so it is exercised with text that must fail it.
    U2's allow-list makes such a document very hard to produce; this is the
    layer behind that, on an artifact that cannot be recalled."""
    with pytest.raises(FinalizationError, match="banned pattern"):
        assert_no_banned_patterns(f"<p>{payload}</p>", PATTERNS)


def test_fr_c38_the_scan_runs_before_anything_is_written(tmp_path):
    """A rejected report must leave no bytes and no stamp. A scan that ran after
    the write would be a scan of a file already on disk."""
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    _transition(run_dir, 5003, "merged", now=TS)
    # A pattern matching any document at all stands in for a real hit.
    always = [{"id": "test-catch-all", "regex": re.compile("<html")}]

    with pytest.raises(FinalizationError, match="banned pattern"):
        finalize(run_dir, SCHEMA, always)

    assert not report_path(run_dir).exists()
    assert not stamp_path(run_dir).exists()


def test_the_report_and_stamp_land_beside_the_shards_they_describe(tmp_path):
    """Both artifacts go into the run directory the ledger already owns, so the
    private location the ledger unit defines is the only location involved --
    there is no second path to keep in sync or to get wrong."""
    run_dir = _quiet_night(tmp_path)
    finalize(run_dir, SCHEMA, PATTERNS)
    written = {p.name for p in run_dir.iterdir()} - {
        p.name for p in run_dir.glob("shard-*.json")
    }
    assert written == {REPORT_NAME, FINAL_STAMP_NAME}


def test_fr_c38_the_report_key_is_under_the_private_ledger_prefix():
    """Same private location the ledger unit defines, derived from its
    `run_prefix` -- not a second path this unit invented."""
    key = report_key(RUN_DATE)
    assert key == f"security-agent/runs/{RUN_DATE}/{REPORT_NAME}"
    assert not key.startswith("/")
    assert "public" not in key


def test_fr_c38_no_public_artifact_or_acl_write_path_exists_in_the_module():
    """Structural: the way to not have a world-readable report is to have no
    code that could produce one. Asserted on the source, because a behavioural
    test can only cover the paths that exist today."""
    source = SCRIPT.read_text()
    for forbidden in (
        "public-read",
        "upload-artifact",
        "put_object_acl",
        "ACL=",
        "actions/upload",
        "GITHUB_STEP_SUMMARY",
    ):
        assert forbidden not in source, f"{SCRIPT.name} references {forbidden!r}"


def test_fr_c38_the_report_destination_cannot_be_overridden_from_the_cli(capsys):
    """No `--out`: an operator-supplied destination is how a private artifact
    ends up somewhere public, and the ledger's prefix is not negotiable."""
    parser = build_parser()
    options = {action.dest for action in parser._actions}
    assert options == {"help", "ledger_dir"}
    with pytest.raises(SystemExit):
        parser.parse_args(["--ledger-dir", "x", "--out", "/tmp/public.html"])
    capsys.readouterr()


def test_the_report_carries_no_finding_ids_or_item_titles(tmp_path):
    """Item numbers and counts are the report's content (FR-C34). Finding ids
    are ledger-internal and must not be rendered."""
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    _transition(run_dir, 5003, "merged", now=TS)
    finalize(run_dir, SCHEMA, PATTERNS)
    document = report_path(run_dir).read_text()
    assert "f-a1c2" not in document
    assert "#5002" in document


# --------------------------------------------------------------------------
# the state machine as a pure function
# --------------------------------------------------------------------------


def test_next_status_is_a_pure_function_of_previous_and_ledger(tmp_path):
    merged = _merged(_run_dir(tmp_path))
    assert next_status(None, merged) == next_status(None, merged) == STATUS_IN_PROGRESS


def test_next_status_rejects_a_status_it_never_emits(tmp_path):
    merged = _merged(_quiet_night(tmp_path))
    with pytest.raises(FinalizationError, match="unknown previous status"):
        next_status("almost final", merged)


def test_next_status_refuses_to_leave_final(tmp_path):
    """`final` is terminal for the report too. A night that could be un-finalized
    is a night with no record."""
    merged = _merged(_quiet_night(tmp_path))
    with pytest.raises(FinalizationError, match="already stamped final"):
        next_status(STATUS_FINAL, merged)


def test_in_progress_is_a_legal_previous_status(tmp_path):
    merged = _merged(_quiet_night(tmp_path))
    assert next_status(STATUS_IN_PROGRESS, merged) == STATUS_FINAL


def test_previous_status_is_read_from_disk_across_the_runs_lifetime(tmp_path):
    run_dir = _run_dir(tmp_path)
    assert read_previous_status(run_dir) is None

    finalize(run_dir, SCHEMA, PATTERNS)
    assert read_previous_status(run_dir) == STATUS_IN_PROGRESS

    _transition(run_dir, 5002, "merged", now=TS)
    _transition(run_dir, 5003, "merged", now=TS)
    finalize(run_dir, SCHEMA, PATTERNS)
    assert read_previous_status(run_dir) == STATUS_FINAL


def test_an_unreadable_stamp_is_not_treated_as_absent(tmp_path):
    """The stamp is the only record that a night was finalized, so a corrupt one
    must halt rather than be read as "never stamped" -- which would re-stamp."""
    run_dir = _run_dir(tmp_path)
    stamp_path(run_dir).write_text("{ truncated")
    with pytest.raises(FinalizationError, match="no readable status"):
        read_previous_status(run_dir)


def test_a_stamp_recording_a_non_final_status_is_rejected(tmp_path):
    run_dir = _run_dir(tmp_path)
    stamp_path(run_dir).write_text(json.dumps({"status": STATUS_IN_PROGRESS}))
    with pytest.raises(FinalizationError, match="written only for"):
        read_previous_status(run_dir)


def test_the_state_machine_and_the_renderer_cannot_disagree(tmp_path):
    """Two definitions of "final" that can drift is how a report claims a
    picture the state machine never agreed to. It cannot fail today; the assert
    exists so that it fails the day it can."""
    merged = _merged(_quiet_night(tmp_path))
    assert_renderer_agrees(STATUS_FINAL, merged, SCHEMA)
    with pytest.raises(FinalizationError, match="drifted"):
        assert_renderer_agrees(STATUS_IN_PROGRESS, merged, SCHEMA)


def test_all_items_terminal_on_an_item_free_night(tmp_path):
    assert all_items_terminal(_merged(_quiet_night(tmp_path))) is True


def test_finalize_does_not_mutate_the_ledger_shards(tmp_path):
    """Re-rendering must never edit a shard: the ledger is append-only per stage
    (FR-C29), and a report step that wrote back into it would be the shared-object
    write that design exists to prevent."""
    run_dir = _run_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in sorted(run_dir.glob("shard-*.json"))}
    finalize(run_dir, SCHEMA, PATTERNS)
    after = {p.name: p.read_bytes() for p in sorted(run_dir.glob("shard-*.json"))}
    assert after == before


def test_the_final_stamp_reads_its_timestamp_from_the_ledger(tmp_path):
    """No clock in the stamp either: its `generated_at` is the ledger's, so two
    finalizations of identical shards would record identical metadata."""
    run_dir = _quiet_night(tmp_path)
    finalize(run_dir, SCHEMA, PATTERNS)
    assert json.loads(stamp_path(run_dir).read_text())["generated_at"] == TS


def test_the_stamp_records_the_terminal_counts(tmp_path):
    run_dir = _run_dir(tmp_path)
    _transition(run_dir, 5002, "merged", now=TS)
    for _ in range(3):
        _transition(run_dir, 5003, "failed", now=TS)
    finalize(run_dir, SCHEMA, PATTERNS)

    stamp = json.loads(stamp_path(run_dir).read_text())
    assert (stamp["fixed"], stamp["stuck"], stamp["in_progress"]) == (1, 1, 0)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_reports_status_and_placement_only(tmp_path, capsys):
    run_dir = _run_dir(tmp_path)
    assert main(["--ledger-dir", str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert f"status={STATUS_IN_PROGRESS}" in out
    assert "stamped=false" in out
    assert report_key(RUN_DATE) in out


def test_cli_stamps_and_says_so(tmp_path, capsys):
    run_dir = _quiet_night(tmp_path)
    assert main(["--ledger-dir", str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert f"status={STATUS_FINAL}" in out
    assert "stamped=true" in out


def test_cli_fails_loudly_on_an_invalid_ledger(tmp_path, capsys):
    """A ledger error must not pass as "nothing to report": an unattended run
    with no result and no failure is the outcome nobody investigates."""
    run_dir = tmp_path / RUN_DATE
    run_dir.mkdir()
    (run_dir / "shard-workflow.json").write_text(json.dumps({"nonsense": True}))
    assert main(["--ledger-dir", str(run_dir)]) == 1
    assert "::error title=Security report finalization::" in capsys.readouterr().err


def test_cli_fails_on_an_empty_run_directory(tmp_path, capsys):
    run_dir = tmp_path / RUN_DATE
    run_dir.mkdir()
    assert main(["--ledger-dir", str(run_dir)]) == 1
    capsys.readouterr()


def test_module_runs_as_a_script(tmp_path):
    """The workflow invokes this file with `python3`, so the entry point is
    exercised the way CI exercises it."""
    run_dir = _quiet_night(tmp_path)
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), "--ledger-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"status={STATUS_FINAL}" in proc.stdout


def test_ledger_errors_surface_as_ledger_errors_not_as_finalization_errors(tmp_path):
    """U2 owns shard validity; this module does not re-wrap its failures into a
    type that would suggest a finalization decision was made."""
    run_dir = tmp_path / RUN_DATE
    run_dir.mkdir()
    with pytest.raises(LedgerError):
        finalize(run_dir, SCHEMA, PATTERNS)


# --------------------------------------------------------------------------
# CI binding
# --------------------------------------------------------------------------


def test_this_suite_is_pinned_in_script_tests():
    """script-tests.yml pins its suite list explicitly rather than globbing, so
    an unpinned suite is simply not run and any gate citing it is vacuous."""
    workflow = SCRIPT_TESTS_WORKFLOW.read_text()
    assert f"tests/{Path(__file__).name}" in workflow, (
        "this suite is not pinned in script-tests.yml, so CI does not run it"
    )


def test_the_subject_script_triggers_script_tests():
    """An edit to the module under test must re-run the suite that guards it."""
    workflow = SCRIPT_TESTS_WORKFLOW.read_text()
    assert workflow.count(f"'.github/scripts/{SCRIPT.name}'") == 2, (
        f"{SCRIPT.name} must appear in both the push and pull_request paths lists"
    )
