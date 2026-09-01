"""Tests for the join barrier and the lean orchestration issue (#4449, U10).

Gate coverage, taken from the issue's `## Validation` and its impact table:

* Ordering: the plan is written only after BOTH markers exist.
* The MARKER gates the barrier -- the presence of work items does not. Asserted
  negatively (the barrier does not fire on items alone), which is the only form
  of that claim worth anything: an implementation keying on issues passes every
  positive test and fails only in production, on the night one pass is slow.
* One source never signals => after the timeout the plan ships, is marked
  partial, and the gap is recorded (in the body AND in the ledger).
* Barrier fired twice for one date => exactly ONE orchestration issue.
* Body <= 3 KB as a BYTE assertion, and no work branch is cut for its number.
* A dependency/ordering column is present.
* NT-5: a night with nothing new files no plan at all.

Every `gh` call goes through the one seam `ensure_umbrella_epic._gh`, which the
fake below replaces. That is what lets these tests assert a write was *not*
issued -- the strongest form of the idempotency, no-dispatch and no-branch
claims. Timeouts are exercised through an injected clock, so the suite proves
the timeout branch without ever sleeping.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ensure_umbrella_epic as ue
import join_barrier as jb
import triage_group_findings as tg
from join_barrier import (
    DEFAULT_TIMEOUT_SECONDS,
    MARKER_STAGE_RE,
    MAX_BODY_BYTES,
    ORCHESTRATION_STAGE,
    PLAN_LABEL,
    PLAN_TITLE_TEMPLATE,
    SOURCE_PRECEDENCE,
    STATE_PARTIAL,
    STATE_READY,
    STATE_STALLED,
    STATE_WAITING,
    BarrierError,
    collect_work_items,
    daily_epic,
    ensure_plan_issue,
    evaluate_barrier,
    ledger_fields,
    lint_body,
    load_markers,
    main,
    marker_stage,
    parse_sources,
    plan_labels,
    planned_sequence,
    render_body,
    run_barrier,
    wait_for_markers,
)
from security_agent_ledger import (
    build_shard,
    load_schema,
    merge_shards,
    serialize_shard,
    validate_shard,
)
from triage_group_findings import SOURCES
from triage_group_findings import main as triage_main

REPO = "aws-e/adp"
RUN_DATE = "2026-08-30"
EPIC_NUM = 5001
GENERATED_AT = "2026-08-30T03:25:00Z"
LEDGER_URI = "s3://adp-dev-security-scans-000000000000/security-agent/runs/2026-08-30/"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/script-tests.yml"
SCHEMA = load_schema()


# --------------------------------------------------------------------------
# the gh fake
# --------------------------------------------------------------------------


class FakeGh:
    """Records every `gh` invocation and replays canned responses.

    Modelled on `test_triage_grouping.FakeGh` rather than a third fake shape:
    this module reuses U3's `find_issue_by_exact_title` and `link_sub_issue`, so
    the responses those functions need are identical.
    """

    def __init__(self, *, issues=(), parent_of=None, next_number=9200):
        self.issues = list(issues)
        self.parent_of = dict(parent_of or {})
        self.calls: list[list[str]] = []
        self.writes: list[list[str]] = []
        self.created: list[dict] = []
        self._next_number = next_number

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        joined = " ".join(args)

        if args[:2] == ["issue", "create"]:
            self.writes.append(args)
            number = self._next_number
            self._next_number += 1
            title = args[args.index("--title") + 1]
            body = args[args.index("--body") + 1]
            labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
            self.issues.append({"number": number, "title": title})
            self.created.append(
                {"number": number, "title": title, "body": body, "labels": labels}
            )
            return 0, f"https://github.com/{REPO}/issues/{number}\n", ""

        if args[:2] == ["api", "graphql"]:
            self.writes.append(args)
            nodes = {
                a.split("=", 1)[0]: int(a.split("NODE_", 1)[1])
                for a in args
                if a.startswith(("p=NODE_", "c=NODE_"))
            }
            self.parent_of[nodes["c"]] = nodes["p"]
            return 0, '{"data":{}}', ""

        if args[:2] == ["issue", "list"]:
            needle = args[args.index("--search") + 1].split('"')[1]
            first_word = needle.split()[0].lower()
            hits = [i for i in self.issues if first_word in i["title"].lower()]
            return 0, json.dumps(hits), ""

        if "/parent" in joined:
            child = int(args[1].split("/")[-2])
            parent = self.parent_of.get(child)
            return (0, f"{parent}\n", "") if parent else (1, "", "Not Found")

        if ".node_id" in joined:
            return 0, f"NODE_{args[1].split('/')[-1]}\n", ""

        raise AssertionError(f"unexpected gh call: {args}")

    # -- assertions the suite leans on ------------------------------------

    def creates(self) -> list[list[str]]:
        return [c for c in self.writes if c[:2] == ["issue", "create"]]

    def branch_calls(self) -> list[list[str]]:
        """Any call that could cut or push a work branch. Must always be empty:
        the plan is a coordination artifact, and a branch on its number is what
        an agent opens a pull request from.

        Matched on the ref/branch ENDPOINTS specifically, not on a bare "git"
        substring -- every rendered body cites `.github/scripts/...`, so a
        substring matcher flags the create call itself and the assert passes for
        the wrong reason forever after.
        """
        endpoints = ("/branches", "/git/refs", "/git/ref/", "/pulls")
        return [c for c in self.calls if any(e in a for a in c for e in endpoints)]


@pytest.fixture
def fake_gh(monkeypatch):
    def _install(**kwargs):
        gh = FakeGh(**kwargs)
        monkeypatch.setattr(ue, "_gh", gh)
        return gh

    return _install


# --------------------------------------------------------------------------
# marker helpers
# --------------------------------------------------------------------------


def a_marker(source="code-review", *, story_ids=(5002, 5003), epic=EPIC_NUM, **fields):
    """One grouping pass's completion marker, built through U2's builder.

    Built rather than hand-written so the marker's shape is U2's schema by
    construction -- the integration point this unit is required to reuse rather
    than restate.
    """
    payload = {
        "daily_epic": epic,
        "stories_created": len(story_ids),
        "story_ids": list(story_ids),
        "findings_covered": [f"f-{1000 + i:04x}" for i in range(len(story_ids))],
    }
    payload.update(fields)
    return build_shard(RUN_DATE, marker_stage(source), GENERATED_AT, payload)


def write_markers(directory: Path, *markers) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for marker in markers:
        (directory / f"shard-{marker['stage']}.json").write_bytes(serialize_shard(marker))
    return directory


def both_markers():
    return {
        "code-review": a_marker("code-review", story_ids=(5002, 5003)),
        "pentest": a_marker("pentest", story_ids=(5004,)),
    }


def _never_sleeping(real_wait):
    """``wait_for_markers`` with sleeping made an error.

    For CLI tests asserting a night joins on the FIRST poll while leaving the
    real default timeout in force. A regression then fails here in milliseconds
    instead of hanging CI for 1800 real seconds -- see the caller's docstring.
    """

    def _wait(*args, **kwargs):
        def explode(_seconds):
            raise AssertionError(
                "the barrier polled a second time: it is waiting on a scanner "
                "this run does not join on, which on a real clock is the full "
                "timeout followed by a false PARTIAL"
            )

        return real_wait(*args, **{**kwargs, "sleep": explode})

    return _wait


def ship(gh_factory, markers=None, *, state=None, **kwargs):
    gh = gh_factory(**kwargs)
    markers = both_markers() if markers is None else markers
    result = run_barrier(
        REPO,
        markers=markers,
        run_date=RUN_DATE,
        ledger_uri=LEDGER_URI,
        state=state or evaluate_barrier(markers, elapsed_seconds=1),
    )
    return gh, result


# ==========================================================================
# Ordering: the plan is written only after BOTH markers exist.
# ==========================================================================


def test_one_marker_alone_keeps_the_barrier_waiting():
    state = evaluate_barrier({"code-review": a_marker()}, elapsed_seconds=1)
    assert state["state"] == STATE_WAITING
    assert state["unsignalled"] == ["pentest"]
    assert state["partial"] is False


def test_both_markers_make_the_barrier_ready():
    state = evaluate_barrier(both_markers(), elapsed_seconds=1)
    assert state["state"] == STATE_READY
    assert state["unsignalled"] == []
    assert state["partial"] is False


def test_no_markers_at_all_keeps_the_barrier_waiting():
    assert evaluate_barrier({}, elapsed_seconds=1)["state"] == STATE_WAITING


def test_a_waiting_barrier_refuses_to_write_a_plan(fake_gh):
    """The ordering guarantee at the point it matters: even asked directly, the
    barrier will not write a plan from a half-signalled night."""
    gh = fake_gh()
    with pytest.raises(BarrierError, match="still waiting"):
        run_barrier(
            REPO,
            markers={"code-review": a_marker()},
            run_date=RUN_DATE,
            ledger_uri=LEDGER_URI,
            state=evaluate_barrier({"code-review": a_marker()}, elapsed_seconds=1),
        )
    assert not gh.creates(), "a plan was filed before both passes signalled"


def test_the_polling_loop_returns_only_once_the_second_marker_lands(tmp_path):
    """The loop's ordering property, with the second marker arriving mid-wait.

    An injected clock and sleep: the timeout branch has to be reachable in a
    test, and a suite that really slept for it would be skipped or its threshold
    quietly lowered.
    """
    ledger = write_markers(tmp_path / "run", a_marker("code-review"))
    ticks = iter([0, 10, 20, 30, 40])
    polls = []

    def fake_sleep(_seconds):
        polls.append(len(polls))
        if len(polls) == 2:  # the pentest half finishes filing on the 2nd poll
            write_markers(ledger, a_marker("pentest", story_ids=(5004,)))

    markers, state = wait_for_markers(
        ledger,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        poll_seconds=1,
        monotonic=lambda: next(ticks),
        sleep=fake_sleep,
    )
    assert state["state"] == STATE_READY
    assert sorted(markers) == ["code-review", "pentest"]
    assert len(polls) == 2, "the barrier stopped waiting before the second marker"


# ==========================================================================
# The MARKER gates the barrier -- work items existing does NOT.
# ==========================================================================


def test_work_items_existing_does_not_fire_the_barrier(fake_gh):
    """The central negative claim of this unit.

    Both scanners' work items are already filed and visible on GitHub, but the
    pentest pass has not written its marker -- it is still mid-filing as far as
    anything durable can tell. An implementation keying on "do work items
    exist" would write a plan here, from a night whose second pass has not
    finished.
    """
    gh = fake_gh(
        issues=[
            {"number": 5002, "title": "[Security 2026-08-30] Tenant scope check"},
            {"number": 5004, "title": "[Security 2026-08-30] Pentest surface"},
        ]
    )
    markers = {"code-review": a_marker("code-review")}
    state = evaluate_barrier(markers, elapsed_seconds=1)
    assert state["state"] == STATE_WAITING, "issues on GitHub must not satisfy the join"
    assert not gh.creates()


def test_the_barrier_reads_markers_from_disk_not_issue_state(tmp_path):
    """`load_markers` consults only the ledger. No `gh` seam is patched here: a
    call to GitHub would raise `FileNotFoundError`-free but unmocked, and the
    absence of one is the point."""
    ledger = write_markers(tmp_path / "run", *both_markers().values())
    assert sorted(load_markers(ledger)) == ["code-review", "pentest"]


def test_an_empty_run_directory_yields_no_markers(tmp_path):
    (tmp_path / "run").mkdir()
    assert load_markers(tmp_path / "run") == {}


def test_a_marker_must_satisfy_the_u2_schema(tmp_path):
    """A malformed marker is an error, not a marker quietly skipped: skipping it
    is indistinguishable from the pass never signalling."""
    ledger = tmp_path / "run"
    ledger.mkdir()
    (ledger / "shard-triage.code-review.json").write_text(
        json.dumps({"schema_version": "1", "stage": "triage.code-review"}), encoding="utf-8"
    )
    with pytest.raises(BarrierError, match="not a valid ledger shard"):
        load_markers(ledger)


def test_an_unreadable_marker_is_an_error(tmp_path):
    ledger = tmp_path / "run"
    ledger.mkdir()
    (ledger / "shard-triage.pentest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BarrierError, match="cannot read the marker"):
        load_markers(ledger)


def test_a_bare_triage_marker_is_rejected_as_unattributable(tmp_path):
    """A `triage` shard with no scanner suffix cannot satisfy a per-scanner join,
    and two passes sharing that stage id would overwrite each other's key."""
    ledger = tmp_path / "run"
    ledger.mkdir()
    shard = build_shard(RUN_DATE, "triage", GENERATED_AT, {"stories_created": 2})
    (ledger / "shard-triage.json").write_bytes(serialize_shard(shard))
    with pytest.raises(BarrierError, match="must signal under its own"):
        load_markers(ledger)


def test_a_marker_for_an_unknown_scanner_is_rejected(tmp_path):
    ledger = tmp_path / "run"
    ledger.mkdir()
    shard = build_shard(RUN_DATE, "triage.fuzzer", GENERATED_AT, {"stories_created": 1})
    (ledger / "shard-triage.fuzzer.json").write_bytes(serialize_shard(shard))
    with pytest.raises(BarrierError, match="unknown scanner"):
        load_markers(ledger)


def test_marker_stage_ids_are_distinct_per_scanner():
    """U2's stage id is the concurrency boundary; equal ids would mean one
    grouping pass silently overwrote the other's marker."""
    stages = {marker_stage(s) for s in SOURCES}
    assert len(stages) == len(SOURCES) == 2


def test_marker_stage_rejects_an_undeclared_source():
    with pytest.raises(BarrierError, match="is not one of"):
        marker_stage("nmap")


def test_the_writer_and_the_reader_agree_on_the_marker_stage_id():
    """U9 WRITES the marker stage id and U10 READS it, and each declares the
    template. Two literals is the arrangement this defect class comes from, so
    they are pinned together here: a change to one that the other does not
    follow fails on this line rather than on a night at the join.

    Asserted through the two `marker_stage` functions, not by comparing the
    template strings -- what has to agree is the id each side actually produces.
    """
    for source in SOURCES:
        assert tg.marker_stage(source) == marker_stage(source)
        # And the id the writer emits must satisfy the pattern the reader
        # attributes it by, which is the check `load_markers` performs.
        assert MARKER_STAGE_RE.match(tg.marker_stage(source)) is not None


# ==========================================================================
# Timeout: ship, stamp partial, record the gap.
# ==========================================================================


def test_the_timeout_ships_a_partial_rather_than_stalling():
    state = evaluate_barrier(
        {"code-review": a_marker()}, elapsed_seconds=1801, timeout_seconds=1800
    )
    assert state["state"] == STATE_PARTIAL
    assert state["partial"] is True
    assert state["unsignalled"] == ["pentest"]


def test_the_timeout_is_not_reached_one_second_early():
    state = evaluate_barrier(
        {"code-review": a_marker()}, elapsed_seconds=1799, timeout_seconds=1800
    )
    assert state["state"] == STATE_WAITING


def test_a_partial_plan_says_so_in_its_body(fake_gh):
    markers = {"code-review": a_marker("code-review", story_ids=(5002, 5003))}
    state = evaluate_barrier(markers, elapsed_seconds=9999, timeout_seconds=1800)
    gh, result = ship(fake_gh, markers, state=state)
    body = gh.created[0]["body"]
    assert "PARTIAL PLAN" in body
    assert "pentest" in body, "the body must name the scanner that never signalled"
    assert result["partial"] is True


def test_a_complete_plan_is_not_stamped_partial(fake_gh):
    gh, result = ship(fake_gh)
    assert "PARTIAL" not in gh.created[0]["body"]
    assert result["partial"] is False
    assert result["unsignalled"] == []


def test_the_timeout_gap_is_recorded_in_the_ledger_too(fake_gh):
    """The body is for a human; the ledger is what anything downstream reads. A
    partial only visible by reading an issue is one nothing can detect."""
    markers = {"pentest": a_marker("pentest", story_ids=(5004,))}
    state = evaluate_barrier(markers, elapsed_seconds=9999, timeout_seconds=1800)
    _, result = ship(fake_gh, markers, state=state)
    assert result["ledger_fields"]["unsignalled_sources"] == ["code-review"]


def test_a_complete_plan_records_no_gap(fake_gh):
    _, result = ship(fake_gh)
    assert "unsignalled_sources" not in result["ledger_fields"]


def test_neither_pass_signalling_is_a_hard_failure_not_a_quiet_night(fake_gh):
    """No marker at all inside the window: nothing to plan, but unlike a quiet
    night this must be loud. A silent skip here is a morning with no plan and no
    failure to investigate -- the exact outcome the timeout escape exists to
    prevent."""
    gh = fake_gh()
    state = evaluate_barrier({}, elapsed_seconds=9999, timeout_seconds=1800)
    assert state["state"] == STATE_STALLED
    with pytest.raises(BarrierError, match="no grouping pass signalled"):
        run_barrier(
            REPO, markers={}, run_date=RUN_DATE, ledger_uri=LEDGER_URI, state=state
        )
    assert not gh.creates()


def test_the_polling_loop_gives_up_at_the_timeout(tmp_path):
    ledger = write_markers(tmp_path / "run", a_marker("code-review"))
    ticks = iter([0, 100, 2000])
    _, state = wait_for_markers(
        ledger,
        timeout_seconds=1800,
        poll_seconds=1,
        monotonic=lambda: next(ticks),
        sleep=lambda _s: None,
    )
    assert state["state"] == STATE_PARTIAL


# ==========================================================================
# Idempotency: fired twice for one date => exactly one orchestration issue.
# ==========================================================================


def test_firing_the_barrier_twice_yields_exactly_one_plan(fake_gh):
    gh, first = ship(fake_gh)
    assert first["plan_created"] is True
    assert len(gh.creates()) == 1

    second = run_barrier(
        REPO,
        markers=both_markers(),
        run_date=RUN_DATE,
        ledger_uri=LEDGER_URI,
        state=evaluate_barrier(both_markers(), elapsed_seconds=1),
    )
    assert second["plan_issue"] == first["plan_issue"]
    assert second["plan_created"] is False
    assert len(gh.creates()) == 1, "the retry filed a second plan"
    assert second["ledger_fields"] == first["ledger_fields"]


def test_an_existing_plan_is_adopted_with_no_create_issued(fake_gh):
    title = PLAN_TITLE_TEMPLATE.format(run_date=RUN_DATE)
    gh = fake_gh(issues=[{"number": 7777, "title": title}])
    result = ensure_plan_issue(
        REPO,
        run_date=RUN_DATE,
        body=render_body(
            run_date=RUN_DATE,
            work_items=collect_work_items(both_markers()),
            epic=EPIC_NUM,
            ledger_uri=LEDGER_URI,
            unsignalled=[],
        ),
        epic=EPIC_NUM,
    )
    assert result["number"] == 7777
    assert result["created"] is False
    assert not gh.creates()


def test_a_partial_plan_is_not_duplicated_by_the_completing_retry(fake_gh):
    """The retry that finally has both markers adopts the partial plan rather
    than filing a second, complete one. Two plans for one night is the
    duplicate-delivery failure regardless of which is more accurate."""
    partial_markers = {"code-review": a_marker("code-review")}
    gh, first = ship(
        fake_gh,
        partial_markers,
        state=evaluate_barrier(partial_markers, elapsed_seconds=9999, timeout_seconds=1800),
    )
    second = run_barrier(
        REPO,
        markers=both_markers(),
        run_date=RUN_DATE,
        ledger_uri=LEDGER_URI,
        state=evaluate_barrier(both_markers(), elapsed_seconds=1),
    )
    assert second["plan_issue"] == first["plan_issue"]
    assert len(gh.creates()) == 1


def test_two_dates_get_two_plans(fake_gh):
    fake_gh()
    body = render_body(
        run_date=RUN_DATE,
        work_items=collect_work_items(both_markers()),
        epic=EPIC_NUM,
        ledger_uri=LEDGER_URI,
        unsignalled=[],
    )
    first = ensure_plan_issue(REPO, run_date="2026-08-30", body=body, epic=EPIC_NUM)
    second = ensure_plan_issue(REPO, run_date="2026-08-31", body=body, epic=EPIC_NUM)
    assert first["number"] != second["number"]


def test_the_plan_title_is_dated_so_the_date_is_the_idempotency_key():
    assert PLAN_TITLE_TEMPLATE.format(run_date=RUN_DATE) != PLAN_TITLE_TEMPLATE.format(
        run_date="2026-08-31"
    )
    assert RUN_DATE in PLAN_TITLE_TEMPLATE.format(run_date=RUN_DATE)


# ==========================================================================
# Body: <= 3 KB (bytes), link-only, ordered, no work branch.
# ==========================================================================


def a_body(count=6, **overrides):
    items = [
        {"number": 5000 + i, "source": SOURCE_PRECEDENCE[i % len(SOURCE_PRECEDENCE)]}
        for i in range(count)
    ]
    kwargs = {
        "run_date": RUN_DATE,
        "work_items": items,
        "epic": EPIC_NUM,
        "ledger_uri": LEDGER_URI,
        "unsignalled": [],
    }
    kwargs.update(overrides)
    return render_body(**kwargs)


def test_the_body_is_under_three_kilobytes_in_bytes():
    """A BYTE assertion, not a character one: the em dashes and arrows in this
    body are multi-byte, and the ceiling is about what the reading agent's
    context actually consumes."""
    assert len(a_body().encode("utf-8")) <= MAX_BODY_BYTES
    assert MAX_BODY_BYTES == 3072


def test_a_partial_body_is_also_under_the_ceiling():
    """The partial banner is the largest thing this renderer adds."""
    body = a_body(unsignalled=["pentest"])
    assert len(body.encode("utf-8")) <= MAX_BODY_BYTES


def test_a_pathological_night_truncates_visibly_instead_of_bursting_the_ceiling():
    body = a_body(count=500)
    assert len(body.encode("utf-8")) <= MAX_BODY_BYTES
    assert "more, in order, on the dated" in body, "truncation must be visible"


def test_an_oversized_body_is_refused(fake_gh):
    gh = fake_gh()
    with pytest.raises(BarrierError, match="over the 3072-byte ceiling"):
        ensure_plan_issue(
            REPO, run_date=RUN_DATE, body="x" * (MAX_BODY_BYTES + 1), epic=EPIC_NUM
        )
    assert not gh.creates(), "an oversized plan was filed anyway"


def test_the_body_has_a_dependency_ordering_column():
    body = a_body()
    assert "| Order | Work item | Scanner | Depends on |" in body
    assert "| 1 | #5000 | code-review | — |" in body
    assert "| 2 | #5001 | pentest | #5000 |" in body


def test_the_body_is_link_only_and_carries_no_finding_detail():
    """Work-item numbers and one ledger pointer. No finding ids, titles or
    severities: the items hold that, and restating it here would make the index
    the de-facto source of truth while the items drift."""
    body = a_body()
    assert LEDGER_URI in body
    assert f"#{EPIC_NUM}" in body
    assert "f-" not in body
    assert "findings_covered" not in body


def test_the_body_never_mentions_an_agent(fake_gh):
    assert "@agent-" not in a_body()
    gh = fake_gh()
    with pytest.raises(BarrierError, match="`@agent-` mention"):
        ensure_plan_issue(
            REPO, run_date=RUN_DATE, body="Plan\n\n@agent-developer take this", epic=EPIC_NUM
        )
    assert not gh.creates()


def test_the_body_is_scanned_against_the_banned_pattern_list(fake_gh):
    gh = fake_gh()
    with pytest.raises(BarrierError, match="banned pattern"):
        ensure_plan_issue(
            REPO,
            run_date=RUN_DATE,
            body="Plan\n\nSteps to reproduce: see below.",
            epic=EPIC_NUM,
        )
    assert not gh.creates()


def test_a_clean_body_passes_the_lint():
    lint_body(a_body())


def test_the_plan_carries_no_dispatching_label(fake_gh):
    """`agent-*` is the dispatching namespace. A label from it would start a run
    on a coordination issue at creation."""
    assert plan_labels() == [PLAN_LABEL]
    assert not any(lab.startswith("agent-") for lab in plan_labels())
    assert "story" not in plan_labels(), "the plan is an index, not an implementable item"
    gh, _ = ship(fake_gh)
    assert gh.created[0]["labels"] == [PLAN_LABEL]


def test_no_work_branch_is_ever_cut_for_the_plan(fake_gh):
    """The plan takes no `agent/issue-N` branch: it is a coordination artifact,
    and an agent that treated it as implementable would open a pull request
    against an index."""
    gh, result = ship(fake_gh)
    assert not gh.branch_calls(), "the barrier touched a branch/ref endpoint"
    assert f"agent/issue-{result['plan_issue']}" not in json.dumps(gh.calls)
    assert "no pull request" in gh.created[0]["body"].replace("\n", " ")


def test_a_failed_create_is_raised_not_swallowed(monkeypatch):
    """A create that fails must not leave the run believing a plan exists: the
    next stage would dispatch against nothing."""
    monkeypatch.setattr(ue, "_gh", lambda args: (0, "[]", "") if args[:2] == ["issue", "list"]
                        else (1, "", "rate limited"))
    with pytest.raises(BarrierError, match="failed to create issue"):
        ensure_plan_issue(REPO, run_date=RUN_DATE, body=a_body(), epic=EPIC_NUM)


def test_an_unparseable_create_response_is_raised(monkeypatch):
    monkeypatch.setattr(ue, "_gh", lambda args: (0, "[]", "") if args[:2] == ["issue", "list"]
                        else (0, "created something\n", ""))
    with pytest.raises(BarrierError, match="could not parse issue number"):
        ensure_plan_issue(REPO, run_date=RUN_DATE, body=a_body(), epic=EPIC_NUM)


def test_the_plan_body_states_it_is_a_coordination_artifact():
    assert "coordination artifact" in a_body().replace("\n", " ")


def test_a_plan_with_no_dated_epic_still_renders(fake_gh):
    """Defensive: a marker set missing `daily_epic` still yields a usable plan
    rather than a crash, and issues no link mutation it cannot target."""
    markers = {
        "code-review": build_shard(
            RUN_DATE, marker_stage("code-review"), GENERATED_AT, {"story_ids": [5002]}
        )
    }
    state = evaluate_barrier(markers, elapsed_seconds=9999, timeout_seconds=1800)
    gh, result = ship(fake_gh, markers, state=state)
    assert result["daily_epic"] is None
    assert not [c for c in gh.writes if c[:2] == ["api", "graphql"]]


# ==========================================================================
# Ordering of the work items themselves.
# ==========================================================================


def test_work_items_are_sequenced_by_scanner_then_number():
    items = collect_work_items(both_markers())
    assert planned_sequence(items) == [5002, 5003, 5004]
    assert [i["source"] for i in items] == ["code-review", "code-review", "pentest"]


def test_the_sequence_is_independent_of_marker_dict_order():
    """Order must come from the declared precedence, not from whatever order the
    run happened to produce."""
    forward = both_markers()
    reversed_markers = dict(reversed(list(forward.items())))
    assert collect_work_items(forward) == collect_work_items(reversed_markers)


def test_an_item_claimed_by_both_scanners_is_listed_once():
    markers = {
        "code-review": a_marker("code-review", story_ids=(5002, 5004)),
        "pentest": a_marker("pentest", story_ids=(5004, 5005)),
    }
    sequence = planned_sequence(collect_work_items(markers))
    assert sequence == [5002, 5004, 5005]
    assert len(sequence) == len(set(sequence))


def test_a_scanner_with_no_declared_precedence_sorts_last_deterministically():
    """Defensive: a scanner added later without declaring its precedence must
    still produce a stable order rather than an arbitrary one."""
    markers = {
        "code-review": a_marker("code-review", story_ids=(5002,)),
        "pentest": a_marker("pentest", story_ids=(5004,)),
    }
    markers["fuzzer"] = build_shard(
        RUN_DATE, "triage.pentest", GENERATED_AT, {"story_ids": [5009]}
    )
    assert planned_sequence(collect_work_items(markers)) == [5002, 5004, 5009]


def test_source_precedence_covers_every_scanner():
    """A scanner absent from the precedence list would be ordered by fallback,
    which is an ordering nobody declared."""
    assert set(SOURCE_PRECEDENCE) == set(SOURCES)


def test_markers_disagreeing_on_the_dated_epic_is_raised_not_resolved():
    markers = {
        "code-review": a_marker("code-review", epic=5001),
        "pentest": a_marker("pentest", epic=6001),
    }
    with pytest.raises(BarrierError, match="disagree on the night's dated EPIC"):
        daily_epic(markers)


# ==========================================================================
# NT-5: a night with nothing new files nothing, including no plan.
# ==========================================================================


def test_a_quiet_night_files_no_plan_at_all(fake_gh):
    gh = fake_gh()
    markers = {
        "code-review": a_marker("code-review", story_ids=()),
        "pentest": a_marker("pentest", story_ids=()),
    }
    result = run_barrier(
        REPO,
        markers=markers,
        run_date=RUN_DATE,
        ledger_uri=LEDGER_URI,
        state=evaluate_barrier(markers, elapsed_seconds=1),
    )
    assert result["nothing_to_plan"] is True
    assert not gh.creates()
    assert "ledger_fields" not in result


def test_a_quiet_night_writes_no_orchestration_shard(fake_gh, tmp_path, capsys):
    ledger = write_markers(
        tmp_path / "run",
        a_marker("code-review", story_ids=()),
        a_marker("pentest", story_ids=()),
    )
    fake_gh()
    out_file = tmp_path / "shard-orchestration.json"
    rc = main(
        [
            "wait",
            "--repo",
            REPO,
            "--run-date",
            RUN_DATE,
            "--ledger-dir",
            str(ledger),
            "--ledger-uri",
            LEDGER_URI,
            "--generated-at",
            GENERATED_AT,
            "--ledger-fields",
            str(out_file),
        ]
    )
    assert rc == 0
    assert "nothing_to_plan=true" in capsys.readouterr().out
    assert not out_file.exists(), "a quiet night wrote an orchestration shard anyway"


# ==========================================================================
# The ledger shard is U2's, written to U2's schema.
# ==========================================================================


def test_the_orchestration_fields_are_accepted_by_the_u2_schema(fake_gh):
    _, result = ship(fake_gh)
    shard = build_shard(RUN_DATE, ORCHESTRATION_STAGE, GENERATED_AT, result["ledger_fields"])
    assert validate_shard(shard, SCHEMA)["fields"]["planned_sequence"] == [5002, 5003, 5004]


def test_every_field_written_is_owned_by_the_orchestration_stage(fake_gh):
    """This unit writes its OWN shard and reshapes no other stage's."""
    markers = {"code-review": a_marker("code-review")}
    _, result = ship(
        fake_gh,
        markers,
        state=evaluate_barrier(markers, elapsed_seconds=9999, timeout_seconds=1800),
    )
    for name in result["ledger_fields"]:
        assert "orchestration" in SCHEMA["x-fields"][name]["stages"], name


def test_the_partial_gap_field_is_declared_in_the_schema():
    spec = SCHEMA["x-fields"]["unsignalled_sources"]
    assert spec["stages"] == ["orchestration"]
    assert spec["merge"] in {"sum", "max", "unique-list", "single", "map-union"}


def test_the_orchestration_shard_merges_with_the_night_s_other_shards():
    """The barrier's shard has to sit alongside the markers it read, on one run
    prefix, without a merge conflict."""
    shard = build_shard(
        RUN_DATE, ORCHESTRATION_STAGE, GENERATED_AT, {"planned_sequence": [5002, 5004]}
    )
    merged = merge_shards([*both_markers().values(), shard], SCHEMA)
    assert merged["fields"]["planned_sequence"] == [5002, 5004]
    assert ORCHESTRATION_STAGE in merged["stages"]


def test_the_orchestration_stage_id_needs_no_suffix():
    """One barrier per night, so unlike the two grouping halves it cannot
    collide with a concurrent writer."""
    assert ORCHESTRATION_STAGE == "orchestration"
    assert ORCHESTRATION_STAGE not in {marker_stage(s) for s in SOURCES}


def test_ledger_fields_shape():
    items = [{"number": 5004, "source": "pentest"}, {"number": 5002, "source": "code-review"}]
    assert ledger_fields(items, []) == {"planned_sequence": [5004, 5002]}
    assert ledger_fields(items, ["code-review"])["unsignalled_sources"] == ["code-review"]


# ==========================================================================
# CLI
# ==========================================================================


def test_the_cli_writes_the_plan_and_the_shard(fake_gh, tmp_path, capsys):
    ledger = write_markers(tmp_path / "run", *both_markers().values())
    gh = fake_gh()
    out_file = tmp_path / "out" / "shard-orchestration.json"
    rc = main(
        [
            "wait",
            "--repo",
            REPO,
            "--run-date",
            RUN_DATE,
            "--ledger-dir",
            str(ledger),
            "--ledger-uri",
            LEDGER_URI,
            "--generated-at",
            GENERATED_AT,
            "--ledger-fields",
            str(out_file),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing_to_plan=false" in out
    assert "partial=false" in out
    assert len(gh.creates()) == 1
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert validate_shard(written, SCHEMA)["stage"] == ORCHESTRATION_STAGE
    assert written["fields"]["planned_sequence"] == [5002, 5003, 5004]
    assert written["generated_at"] == GENERATED_AT, "the caller supplies the timestamp"


def test_the_cli_logs_no_finding_detail(fake_gh, tmp_path, capsys):
    """A CI log is readable by anyone who can see the run."""
    ledger = write_markers(tmp_path / "run", *both_markers().values())
    fake_gh()
    main(
        [
            "wait", "--repo", REPO, "--run-date", RUN_DATE,
            "--ledger-dir", str(ledger), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT,
        ]
    )
    assert "f-" not in capsys.readouterr().out


def test_the_cli_warns_when_it_ships_a_partial(fake_gh, tmp_path, capsys):
    ledger = write_markers(tmp_path / "run", a_marker("code-review"))
    fake_gh()
    rc = main(
        [
            "wait", "--repo", REPO, "--run-date", RUN_DATE,
            "--ledger-dir", str(ledger), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT, "--timeout-seconds", "0",
            "--poll-seconds", "0",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning title=Partial security plan::" in out
    assert "partial=true" in out


def test_the_cli_fails_loudly_when_nothing_signalled(fake_gh, tmp_path, capsys):
    ledger = tmp_path / "run"
    ledger.mkdir()
    fake_gh()
    rc = main(
        [
            "wait", "--repo", REPO, "--run-date", RUN_DATE,
            "--ledger-dir", str(ledger), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT, "--timeout-seconds", "0",
            "--poll-seconds", "0",
        ]
    )
    assert rc == 1
    assert "::error title=Security plan barrier::" in capsys.readouterr().err


def test_the_cli_rejects_a_malformed_run_date(tmp_path, capsys):
    rc = main(
        [
            "wait", "--repo", REPO, "--run-date", "30-08-2026",
            "--ledger-dir", str(tmp_path), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT,
        ]
    )
    assert rc == 1
    assert "is not YYYY-MM-DD" in capsys.readouterr().err


def test_the_cli_surfaces_a_malformed_marker_as_an_error(fake_gh, tmp_path, capsys):
    ledger = tmp_path / "run"
    ledger.mkdir()
    (ledger / "shard-triage.pentest.json").write_text("{}", encoding="utf-8")
    fake_gh()
    assert main(
        [
            "wait", "--repo", REPO, "--run-date", RUN_DATE,
            "--ledger-dir", str(ledger), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT,
        ]
    ) == 1
    assert "::error" in capsys.readouterr().err


# ==========================================================================
# The joined set is an INPUT, not the constant (defect 🔴-2).
#
# The barrier joins on `SOURCES` by default, which includes `pentest` -- a
# scanner (U6/U7) that is not built and does not run. A night running only
# code-review therefore could never reach `ready`: it waited out the full 1800s
# and then shipped a plan stamped PARTIAL blaming a scanner that was never
# missing, and wrote that false claim into the run ledger every single night.
#
# The two halves of the fix, and why both are needed:
#   * `--sources` exists at all, so a caller can say what ran tonight;
#   * `wait_for_markers` PASSES IT DOWN. It previously accepted no `sources` and
#     called `evaluate_barrier` without one, so even a correct flag would have
#     been silently discarded -- `evaluate_barrier`'s parameter was reachable
#     only from a direct unit-test call, never from the CLI.
#
# The set the barrier JOINS on is per-run. The set of VALID sources is the
# constant, and it stays whole: it is also U9's `--source` validator and dedup's
# source vocabulary, and pentest is a planned scanner, not a deleted one.
# ==========================================================================


def test_a_code_review_only_night_is_ready_at_once_not_partial_at_the_timeout():
    """The defect, at the decision function. One marker, one joined scanner."""
    state = evaluate_barrier(
        {"code-review": a_marker()}, elapsed_seconds=0, sources=("code-review",)
    )
    assert state["state"] == STATE_READY
    assert state["unsignalled"] == [], "pentest is not running, so it is not a gap"
    assert state["partial"] is False


def test_the_polling_loop_threads_sources_instead_of_dropping_them(tmp_path):
    """The bug itself: `wait_for_markers` never passed `sources` down.

    The clock is injected and yields exactly the two ticks ONE pass needs (the
    start reading, then the first poll's). A loop that still joined on pentest
    would sleep and ask for a third, so it raises StopIteration here rather than
    quietly passing -- and zero sleeps is the claim that matters: the night is
    joined on the first poll, not after 1800s.
    """
    ledger = write_markers(tmp_path / "run", a_marker("code-review"))
    polls = []

    markers, state = wait_for_markers(
        ledger,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        poll_seconds=1,
        monotonic=iter([0, 0]).__next__,
        sleep=lambda _s: polls.append(1),
        sources=("code-review",),
    )
    assert state["state"] == STATE_READY
    assert polls == [], "the barrier slept waiting for a scanner that never runs"
    assert sorted(markers) == ["code-review"]


def test_the_cli_joins_only_the_named_scanner_and_ships_a_clean_plan(
    fake_gh, tmp_path, capsys, monkeypatch
):
    """End to end through the real CLI, on the REAL default 1800s timeout.

    `--timeout-seconds` is deliberately not passed: the claim is about the night
    the nightly actually runs, and a test that shrank the window to 0 would pass
    just as happily against the broken code.

    Sleeping is made an error instead, which is the property under test stated
    directly: a correctly-joined night never polls twice. It also makes a
    regression fail in milliseconds with a readable message -- on a real clock
    this test would hang CI for the full 30 minutes and then report PARTIAL, and
    a 30-minute hang is not something anyone reads as a test failure.
    """
    ledger = write_markers(tmp_path / "run", a_marker("code-review"))
    gh = fake_gh()
    out_file = tmp_path / "out" / "shard-orchestration.json"
    monkeypatch.setattr(jb, "wait_for_markers", _never_sleeping(jb.wait_for_markers))
    rc = main(
        [
            "wait", "--repo", REPO, "--run-date", RUN_DATE,
            "--ledger-dir", str(ledger), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT, "--sources", "code-review",
            "--ledger-fields", str(out_file),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "partial=false" in out
    assert "::warning" not in out, "a plan for the scanners that ran is not partial"
    assert "pentest" not in out

    body = gh.created[0]["body"]
    assert "PARTIAL" not in body
    assert "pentest" not in body, "the plan blamed a scanner that was never running"

    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert validate_shard(written, SCHEMA)["stage"] == ORCHESTRATION_STAGE
    assert "unsignalled_sources" not in written["fields"], (
        "the false 'pentest went silent' claim was recorded in the run ledger"
    )


def test_a_genuine_multi_source_night_still_ships_partial_after_the_timeout(
    fake_gh, tmp_path, capsys
):
    """The regression guard. `--sources` must not become a way to lose the real
    signal: when a scanner that IS running goes silent, that is still a PARTIAL
    plan and still a recorded gap -- the hung-scanner investigation signal."""
    ledger = write_markers(tmp_path / "run", a_marker("code-review"))
    gh = fake_gh()
    out_file = tmp_path / "out" / "shard-orchestration.json"
    rc = main(
        [
            "wait", "--repo", REPO, "--run-date", RUN_DATE,
            "--ledger-dir", str(ledger), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT, "--sources", "code-review,pentest",
            "--timeout-seconds", "0", "--poll-seconds", "0",
            "--ledger-fields", str(out_file),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning title=Partial security plan::" in out
    assert "partial=true" in out
    assert "PARTIAL PLAN" in gh.created[0]["body"]
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert written["fields"]["unsignalled_sources"] == ["pentest"]


def test_omitting_sources_joins_on_every_source():
    """The default is unchanged, so no existing caller changes behaviour. The
    nightly becomes the explicit caller of `--sources code-review` (#4613)."""
    assert parse_sources(None) == SOURCES
    assert parse_sources([]) == SOURCES


def test_sources_accepts_comma_separated_and_repeated_flags_alike():
    """A workflow writing one comma-separated flag and one writing two flags mean
    the same thing; neither spelling may be the one that silently misjoins."""
    assert parse_sources(["code-review,pentest"]) == ("code-review", "pentest")
    assert parse_sources(["code-review", "pentest"]) == ("code-review", "pentest")
    assert parse_sources(["code-review", "pentest,code-review"]) == (
        "code-review",
        "pentest",
    )
    assert parse_sources([" code-review , pentest "]) == ("code-review", "pentest")


def test_an_unknown_scanner_is_rejected_rather_than_joined_on_forever():
    """A typo'd source can never write a marker under a stage id this module
    reads, so joining on it is an unconditional 1800s timeout. Reject at the
    boundary -- the same membership check `marker_stage` applies."""
    with pytest.raises(BarrierError, match="not one of"):
        parse_sources(["nmap"])
    with pytest.raises(BarrierError, match="not one of"):
        parse_sources(["code-review,nmap"])


def test_sources_naming_no_scanner_is_rejected():
    """A barrier joining on the empty set is `ready` before reading a marker --
    it would ship a plan from a night that had not run."""
    with pytest.raises(BarrierError, match="named no scanner"):
        parse_sources([""])
    with pytest.raises(BarrierError, match="named no scanner"):
        parse_sources([","])


def test_the_cli_surfaces_an_unknown_source_as_an_error_not_a_traceback(
    fake_gh, tmp_path, capsys, monkeypatch
):
    """Rejection must happen BEFORE the wait, so sleeping is again an error: a
    build that let an unknown source through would otherwise sit on the real
    1800s clock here rather than failing."""
    ledger = write_markers(tmp_path / "run", a_marker("code-review"))
    gh = fake_gh()
    monkeypatch.setattr(jb, "wait_for_markers", _never_sleeping(jb.wait_for_markers))
    rc = main(
        [
            "wait", "--repo", REPO, "--run-date", RUN_DATE,
            "--ledger-dir", str(ledger), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT, "--sources", "nmap",
        ]
    )
    assert rc == 1
    assert "::error title=Security plan barrier::" in capsys.readouterr().err
    assert not gh.creates(), "a plan was filed from an unvalidated join set"


def test_the_source_vocabulary_constant_is_not_shrunk():
    """Pins the 'do not shrink SOURCES' rule the design calls out.

    Making the joined set an input is the fix; deleting `pentest` from the
    constant is the tempting wrong one. `SOURCES` is also U9's `--source` choice
    validator and dedup's source vocabulary, and U6/U7 are planned -- so pentest
    must stay a VALID source that tonight simply does not run.
    """
    assert "pentest" in SOURCES
    assert set(SOURCES) == {"code-review", "pentest"}
    assert marker_stage("pentest") == "triage.pentest"
    assert parse_sources(["pentest"]) == ("pentest",)


# ==========================================================================
# The U9 -> U10 seam, driven through U9's REAL CLI.
#
# Everything above builds markers with `a_marker`. That is the right shape by
# construction, but it cannot catch a DISAGREEMENT between what U9 actually
# emits and what the barrier expects -- and that disagreement is exactly what
# shipped: U9's quiet path wrote no marker at all, so the empty markers the NT-5
# tests synthesize were markers production never produced. Those tests passed
# while the common night hard-failed.
#
# So these drive `triage_group_findings.main` for real and feed its ACTUAL output
# into the barrier. No hand-written marker anywhere below.
# ==========================================================================


def u9_marker_from_a_real_quiet_run(tmp_path, source, monkeypatch):
    """Run U9's `file` command on a genuine zero-findings input; return the
    marker it really wrote, exactly as written.

    This helper used to take U9's bare field output and wrap it in `build_shard`
    ITSELF before handing it to the barrier -- which is how the shard-wrapper
    defect (#4616) survived a suite that drove U9's real CLI. The test supplied
    the envelope production did not, so every assertion below passed against an
    artifact no U9 run ever produced. Nothing is wrapped here now: what the
    barrier gets is the bytes U9 wrote, and both the name of the file and the
    stage inside it come from U9.
    """
    gh_calls: list[list[str]] = []

    def record(args):
        gh_calls.append(args)
        raise AssertionError(f"a quiet night must touch no GitHub state: {args}")

    monkeypatch.setattr(ue, "_gh", record)

    findings = tmp_path / f"dedup.{source}.json"
    findings.write_text(
        json.dumps(
            {
                "run_date": RUN_DATE,
                "source": source,
                "nothing_to_file": True,
                "new_findings": [],
                "finding_ids": [],
                "unreferenceable": 0,
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / f"plan.{source}.json"
    plan.write_text(
        json.dumps(
            {"schema_version": "1", "source": source, "run_date": RUN_DATE, "groups": []}
        ),
        encoding="utf-8",
    )
    # A directory, not a filename: U9 derives the marker's name from its stage
    # id, so this test cannot name the file and therefore cannot paper over a
    # disagreement between that name and the barrier's glob.
    ledger_out = tmp_path / f"ledger.{source}"

    rc = triage_main(
        [
            "file",
            "--repo", REPO,
            "--plan", str(plan),
            "--new-findings", str(findings),
            "--source", source,
            "--findings-uri", LEDGER_URI,
            "--run-id", "12345",
            "--ledger-dir", str(ledger_out),
            "--generated-at", GENERATED_AT,
        ]
    )
    assert rc == 0, "U9's quiet path must exit cleanly"
    assert gh_calls == []
    written = sorted(ledger_out.glob("shard-triage*.json"))
    assert len(written) == 1, (
        "U9 wrote no completion marker matching the delivery job's "
        "`shard-triage*.json` glob on a quiet night, so the barrier cannot tell "
        f"this healthy scanner from one that hung (found: {written})"
    )
    return json.loads(written[0].read_text(encoding="utf-8"))


def test_a_real_u9_quiet_run_produces_a_marker_the_barrier_accepts(tmp_path, monkeypatch):
    """The seam, asserted on U9's real output rather than on a synthetic marker."""
    marker = u9_marker_from_a_real_quiet_run(tmp_path, "code-review", monkeypatch)
    validate_shard(marker, SCHEMA)
    assert marker["fields"]["story_ids"] == []
    assert "daily_epic" not in marker["fields"], "there is no dated EPIC on a quiet night"

    # Present-but-empty counts as SIGNALLED. This is the distinction the barrier
    # exists to make, checked against the real artifact.
    state = evaluate_barrier({"code-review": marker}, elapsed_seconds=1)
    assert state["signalled"] == ["code-review"]
    assert state["unsignalled"] == ["pentest"]


def test_a_fully_quiet_night_end_to_end_is_a_clean_no_op(tmp_path, monkeypatch, capsys):
    """NT-5 on the COMMON night, from U9's real output through to the barrier.

    Before the U9 fix this timed out, reported STATE_STALLED and exited 1 -- a
    quiet night failing CI, the exact inverse of NT-5.
    """
    markers = [
        u9_marker_from_a_real_quiet_run(tmp_path, source, monkeypatch)
        for source in SOURCES
    ]
    ledger = write_markers(tmp_path / "run", *markers)

    gh = FakeGh(issues=[])
    monkeypatch.setattr(ue, "_gh", gh)
    out_file = tmp_path / "shard-orchestration.json"
    rc = main(
        [
            "wait", "--repo", REPO, "--run-date", RUN_DATE,
            "--ledger-dir", str(ledger), "--ledger-uri", LEDGER_URI,
            "--generated-at", GENERATED_AT,
            # A zero timeout: were these markers not recognised as completion
            # signals, the barrier would stall immediately and exit 1.
            "--timeout-seconds", "0", "--poll-seconds", "0",
            "--ledger-fields", str(out_file),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0, "a quiet night must close cleanly, not fail"
    assert "nothing_to_plan=true" in out
    assert not gh.creates(), "a quiet night filed a plan"
    assert not out_file.exists(), "a quiet night wrote an orchestration shard"
    assert "PARTIAL" not in out and "never signalled" not in out, (
        "a quiet night must not be reported as a scanner failure"
    )


def test_one_scanner_quiet_and_one_with_findings_does_not_blame_the_quiet_one(
    tmp_path, monkeypatch, fake_gh
):
    """The false-blame case: pentest ran and found nothing, code-review filed work.

    The plan must ship COMPLETE. Reporting it PARTIAL would accuse a working
    scanner every quiet night, and a warning that cries wolf nightly is a warning
    nobody reads on the night it is real.
    """
    quiet = u9_marker_from_a_real_quiet_run(tmp_path, "pentest", monkeypatch)
    markers = {
        "code-review": a_marker("code-review", story_ids=(5002, 5003)),
        "pentest": quiet,
    }
    state = evaluate_barrier(markers, elapsed_seconds=1)
    assert state["state"] == STATE_READY
    assert state["unsignalled"] == []

    gh = fake_gh()
    result = run_barrier(
        REPO, markers=markers, run_date=RUN_DATE, ledger_uri=LEDGER_URI, state=state
    )
    assert result["partial"] is False
    assert result["work_items"] == [
        {"number": 5002, "source": "code-review"},
        {"number": 5003, "source": "code-review"},
    ], "the quiet scanner contributes zero items, not a missing-scanner gap"
    body = gh.created[0]["body"]
    assert "PARTIAL" not in body
    assert "never signalled" not in body
    assert "unsignalled_sources" not in result["ledger_fields"]


def test_a_genuinely_absent_scanner_is_still_reported_as_never_signalled(
    tmp_path, monkeypatch, fake_gh
):
    """The other side of the distinction: absence must still drive PARTIAL.

    Paired deliberately with the test above -- together they pin BOTH directions,
    so a change that makes one pass by collapsing the two fails the other.
    """
    monkeypatch.setattr(ue, "_gh", lambda args: (0, "[]", ""))
    # code-review signalled with work; pentest left no marker at all.
    markers = {"code-review": a_marker("code-review", story_ids=(5002, 5003))}
    state = evaluate_barrier(
        markers, elapsed_seconds=DEFAULT_TIMEOUT_SECONDS + 1
    )
    assert state["state"] == STATE_PARTIAL
    assert state["unsignalled"] == ["pentest"]

    gh = fake_gh()
    result = run_barrier(
        REPO, markers=markers, run_date=RUN_DATE, ledger_uri=LEDGER_URI, state=state
    )
    assert result["partial"] is True
    body = gh.created[0]["body"]
    assert "PARTIAL PLAN" in body and "pentest" in body
    assert result["ledger_fields"]["unsignalled_sources"] == ["pentest"]


def test_the_barrier_distinguishes_a_quiet_scanner_from_an_absent_one(
    tmp_path, monkeypatch
):
    """The two cases are different STATES, not different shades of the same one.

    Stated as one assertion so the property survives edits to either test above:
    same run date, same absent-vs-empty difference, opposite verdicts.
    """
    quiet = u9_marker_from_a_real_quiet_run(tmp_path, "pentest", monkeypatch)
    with_findings = a_marker("code-review", story_ids=(5002,))
    at_timeout = DEFAULT_TIMEOUT_SECONDS + 1

    ran_and_found_nothing = evaluate_barrier(
        {"code-review": with_findings, "pentest": quiet}, elapsed_seconds=at_timeout
    )
    never_ran = evaluate_barrier(
        {"code-review": with_findings}, elapsed_seconds=at_timeout
    )

    assert ran_and_found_nothing["state"] == STATE_READY
    assert never_ran["state"] == STATE_PARTIAL
    assert ran_and_found_nothing["unsignalled"] == []
    assert never_ran["unsignalled"] == ["pentest"]


# ==========================================================================
# The gate itself: Script Tests must actually run this suite.
# ==========================================================================


def test_script_tests_runs_this_suite_and_watches_its_subject():
    """Without these bindings this file can rot silently and the issue's CI-check
    gate is vacuous -- the same reason the workflow pins files explicitly."""
    workflow = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_join_barrier.py" in workflow
    assert workflow.count(".github/scripts/join_barrier.py") == 2, "push + pull_request"
