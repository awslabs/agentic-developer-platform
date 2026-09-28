"""Tests for the autonomous ops handoff and the stuck rule (#4450, U11).

Gate coverage, taken from the issue's `## Validation` and its impact table. Each
bullet below is a row in that table -- a way this unit can consume the platform's
runner capacity, dispatch outside its sequencing, or never finish a night:

* **Lineage**, asserted in BOTH directions: the night's root is exactly one
  `aws events put-events`, and it is never an `adp-trigger`; every per-item hop
  is an `adp-trigger`, and never a `put-events`. One direction alone is not the
  claim. A per-item `put-events` mints a fresh root at `chain_depth=0`, which
  disables the depth cap and the cross-persona loop guard and leaves the night's
  runs unlinked -- and it would pass any test that only checked "a dispatch
  happened".
* **No mention-based and no label-based dispatch path exists**, asserted
  structurally over the module source as well as behaviourally. This is the
  assertion worth the most in the file: a mention or a label dispatches at
  *write* time, so an implementation that used one would satisfy every positive
  test here and fail only in production, on a night when two agents pick up the
  same item (#3626).
* **The plan issue and the dated parent are rejected** as dispatch targets by
  number. They are an index and a container; an agent pointed at one opens a pull
  request against a coordination artifact.
* **The bound is a count** -- per-item runs `<=` items created that night, on the
  fresh path and on the resume/retry path, because "one run per item per retry"
  is the unbounded case that starves every other pipeline.
* **Both stuck paths reach stuck with a reason stamped** -- three failed runs,
  and 24h without a transition -- plus 23h59m staying `in_progress`, which is
  what pins the boundary rather than merely observing that a big number trips it.
  The thresholds are read from `x-stuck-rule` in the ledger schema and never
  restated here: U11 delegates to U2's `evaluate_story_status`, and a second copy
  of "3" and "24" in the tests would let the two drift apart silently.
* **Status comments carry no `@agent-` token**, checked on the exact bytes handed
  to the `gh` seam rather than on the render's return value -- posting is the
  step that would make a status update a dispatch.
* **H1 regression**: an injected `reason` cannot override the pinned `persona` or
  `service_identity`. EventBridge substitutes into its input template without
  escaping, so this is a privilege-escalation guard, not input tidiness.

Every `gh` call goes through U3's one seam (`ensure_umbrella_epic._gh`), which
the fake below replaces -- that is what lets these tests assert a write was
*never* issued. The AWS and `adp-trigger` seams are separate and injected, so
"exactly one root" and "no per-item put-events" are assertions about counted
calls. `now` is a parameter everywhere, so the 24-hour path is proven without
sleeping.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ensure_umbrella_epic as ue
import ops_dispatch as od
import ops_stuck_tracker as ost
from security_agent_ledger import load_schema, serialize_shard

REPO = "aws-e/adp"
RUN_DATE = "2026-08-30"
NOW = "2026-08-30T03:30:00Z"
PLAN_ISSUE = 5100
DAILY_EPIC = 5000
ITEMS = [5101, 5102, 5103]

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/script-tests.yml"
NIGHTLY_WORKFLOW = REPO_ROOT / ".github/workflows/security-agent-nightly.yml"
EVENTBRIDGE_TF = (
    REPO_ROOT / "modules/agent-factory/webhook-ingress/infra/eventbridge.tf"
)
OPS_DISPATCH_SRC = Path(od.__file__).read_text(encoding="utf-8")
OPS_TRACKER_SRC = Path(ost.__file__).read_text(encoding="utf-8")

SCHEMA = load_schema()

# The stuck rule's numbers come from the schema, never from this file. U11 applies
# U2's `evaluate_story_status`; restating 3 and 24 here would create a second
# source of truth that can silently disagree with the one that ends runs.
STUCK_RULE = SCHEMA["x-stuck-rule"]
FAILED_RUNS_THRESHOLD = STUCK_RULE["failed_runs_threshold"]
NO_TRANSITION_HOURS = STUCK_RULE["no_transition_hours"]


# --------------------------------------------------------------------------
# fakes -- one per seam, so each transport is counted separately
# --------------------------------------------------------------------------


class FakeGh:
    """Records every `gh` call. Records, not asserts: several tests below need to
    prove a call was never made, which needs the full list."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        return 0, "", ""

    @property
    def comment_bodies(self) -> list[str]:
        """The exact `--body` bytes of every comment posted."""
        return [
            args[args.index("--body") + 1]
            for args in self.calls
            if args[:2] == ["issue", "comment"] and "--body" in args
        ]


class FakeRunner:
    """A subprocess seam. `calls` is the argv of each invocation, in order."""

    def __init__(self, *, rc=0, stdout='{"FailedEntryCount": 0}', stderr=""):
        self.calls: list[list[str]] = []
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        return self.rc, self.stdout, self.stderr


@pytest.fixture
def gh(monkeypatch):
    """Patch the ONE gh seam. Patching `ensure_umbrella_epic._gh` (not
    `ops_dispatch._gh`) is deliberate: `ops_dispatch._gh` resolves it at call
    time, so this intercepts every GitHub write the module can make."""
    fake = FakeGh()
    monkeypatch.setattr(ue, "_gh", fake)
    return fake


def _write_shard(directory: Path, stage: str, fields: dict):
    """Write one shard for `stage`, validated the way the pipeline writes it."""
    shard = {
        "schema_version": SCHEMA["properties"]["schema_version"]["const"],
        "run_date": RUN_DATE,
        "stage": stage,
        "stage_type": stage.split(".")[0],
        "generated_at": NOW,
        "fields": fields,
    }
    (directory / f"shard-{stage}.json").write_bytes(serialize_shard(shard))


def write_orchestration_shard(directory: Path, *, sequence=None, epic=DAILY_EPIC):
    """The night's plan as the ledger actually holds it.

    Two shards, not one, because field ownership is per stage: `planned_sequence`
    belongs to `orchestration` (U10's barrier) and `daily_epic` to `triage` (U9).
    Writing both here means `load_plan` is exercised against the real merge rather
    than a shape the schema would reject in production.
    """
    directory.mkdir(parents=True, exist_ok=True)
    _write_shard(
        directory,
        "orchestration",
        {"planned_sequence": list(sequence if sequence is not None else ITEMS)},
    )
    if epic is not None:
        _write_shard(directory, "triage", {"daily_epic": epic})
    return directory


def _strip_comments_and_docstrings(source: str) -> str:
    """Source with comments and docstrings removed, via tokenize+AST rather than a
    regex, so "is this in code or in prose?" is answered exactly.

    Needed because these modules document the policies they implement: the
    docstrings discuss vault credentials and mention prefixes in order to explain
    why neither is used, and a naive substring scan cannot tell that explanation
    apart from a violation."""
    import ast
    import io
    import tokenize

    kept: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            continue
        kept.append(tok.string if tok.type != tokenize.STRING else '""')
    stripped = " ".join(kept)
    # Parse to prove the strip did not produce something unparseable, which would
    # make an assertion over it meaningless.
    ast.parse(source)
    return stripped


def hours_later(base: str, hours: float) -> str:
    """A timestamp `hours` after `base`, for the no-transition path."""
    from datetime import datetime, timedelta, timezone

    parsed = datetime.fromisoformat(base.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    return (parsed + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 1. lineage -- the root is one put-events, the hops are adp-trigger
# --------------------------------------------------------------------------


class TestRootDispatchLineage:
    def test_root_emits_exactly_one_put_events(self):
        """One night, one root. A second root would mint a second chain."""
        runner = FakeRunner()
        entry = od.build_root_event(run_date=RUN_DATE, issue_number=PLAN_ISSUE)
        od.emit_root_event(entry, runner=runner)

        assert len(runner.calls) == 1
        assert runner.calls[0][:2] == ["events", "put-events"]

    def test_root_is_not_an_adp_trigger(self):
        """The negative half of the lineage claim. `adp-trigger` cannot mint a
        root -- a fresh call has no lineage and a fabricated one is rejected
        422 unknown_chain -- so a root built as a trigger would never start."""
        runner = FakeRunner()
        od.emit_root_event(
            od.build_root_event(run_date=RUN_DATE, issue_number=PLAN_ISSUE),
            runner=runner,
        )
        flattened = " ".join(" ".join(c) for c in runner.calls)
        assert "adp-trigger" not in flattened

    def test_root_event_matches_the_rule_pattern(self):
        """Source and detail-type are what the EventBridge rule keys on. A drift
        here means the event is accepted and routed nowhere -- the night reports
        nothing, silently."""
        entry = od.build_root_event(run_date=RUN_DATE, issue_number=PLAN_ISSUE)
        assert entry["Source"] == "adp.security-agent"
        assert entry["DetailType"] == "ADP Agent Dispatch"

        rule = _terraform_block("aws_cloudwatch_event_rule", "security_agent_dispatch")
        assert entry["Source"] in rule
        assert entry["DetailType"] in rule

    def test_root_detail_carries_no_persona_identity_or_repo(self):
        """These three are Terraform literals and must not travel in the event:
        `events:PutEvents` cannot be scoped to an event source, so the
        transformer's literals are the primary control (#4559 §5)."""
        entry = od.build_root_event(run_date=RUN_DATE, issue_number=PLAN_ISSUE)
        detail = json.loads(entry["Detail"])
        assert set(detail) == {"reason", "run_date", "issue_number"}

    def test_root_requires_an_existing_issue_number(self):
        """`create_issue` is inert on this path and the worker runs
        `gh issue view $ISSUE_NUMBER` at startup, so an empty number does not
        degrade -- it kills the run before it does anything (#4559 §7.2)."""
        for bad in (0, -1, None, True, "5100"):
            with pytest.raises(od.DispatchError):
                od.build_root_event(run_date=RUN_DATE, issue_number=bad)

    def test_put_events_entry_rejection_is_loud(self):
        """put-events returns HTTP 200 with a per-entry error list, so a rejected
        entry looks like success to anything checking only the exit code -- and
        the night would then wait forever for a run that never started."""
        runner = FakeRunner(
            stdout='{"FailedEntryCount": 1, "Entries": [{"ErrorCode": "x"}]}'
        )
        with pytest.raises(od.DispatchError, match="rejected the entry"):
            od.emit_root_event(
                od.build_root_event(run_date=RUN_DATE, issue_number=PLAN_ISSUE),
                runner=runner,
            )

    def test_second_root_for_the_same_date_is_skipped(self):
        """Idempotency lives in the CI job: the envelope's `dedup_key` names a
        channel and deduplicates nothing (#4559 §7.3)."""
        marker = od.build_root_marker(
            run_date=RUN_DATE, issue_number=PLAN_ISSUE, emitted_at=NOW
        )
        decision = od.decide_root_dispatch(
            run_date=RUN_DATE, issue_number=PLAN_ISSUE, existing_marker=marker
        )
        assert decision["emit"] is False

    def test_first_root_for_a_date_emits(self):
        decision = od.decide_root_dispatch(
            run_date=RUN_DATE, issue_number=PLAN_ISSUE, existing_marker=None
        )
        assert decision["emit"] is True

    def test_two_plans_for_one_night_is_raised(self):
        """Not reconciled: whichever plan this call picked, the other night's
        items would be driven by nobody."""
        marker = od.build_root_marker(
            run_date=RUN_DATE, issue_number=PLAN_ISSUE, emitted_at=NOW
        )
        with pytest.raises(od.DispatchError, match="two plans for one night"):
            od.decide_root_dispatch(
                run_date=RUN_DATE, issue_number=PLAN_ISSUE + 7, existing_marker=marker
            )

    def test_root_marker_is_not_a_ledger_shard(self):
        """The marker must not be picked up by the ledger's `shard-*.json` glob:
        it is an idempotency record and must not join the merge the morning's
        report reconciles against."""
        assert not od.ROOT_MARKER_NAME.startswith("shard-")
        assert od.root_marker_key(RUN_DATE).endswith(od.ROOT_MARKER_NAME)

    def test_run_date_must_be_iso(self):
        for bad in ("30-08-2026", "tonight", "", None):
            with pytest.raises(od.DispatchError):
                od.build_root_event(run_date=bad, issue_number=PLAN_ISSUE)


class TestPerItemHopsUseAdpTrigger:
    def test_each_hop_is_an_adp_trigger_and_not_put_events(self):
        """The other half of the lineage claim. A per-item `put-events` would
        mint a fresh root per item at `chain_depth=0`: the depth cap and the
        cross-persona loop guard would never engage and the night's runs would
        not be linked -- while every positive test still passed."""
        plan = od.bounded_dispatch_plan(
            planned_sequence=ITEMS,
            plan_issue=PLAN_ISSUE,
            daily_epic=DAILY_EPIC,
            repo=REPO,
            reason="nightly security remediation",
        )
        runner = FakeRunner(stdout="")
        results = od.dispatch_items(plan, runner=runner)

        assert len(runner.calls) == len(ITEMS)
        assert all(r["dispatched"] for r in results)
        for argv in runner.calls:
            joined = " ".join(argv)
            assert "put-events" not in joined
            assert "events" not in argv
            assert argv[:2] == ["--persona", od.DELIVERY_PERSONA]

    def test_hop_argv_targets_the_item_issue(self):
        argv = od.build_item_dispatch(ITEMS[0], repo=REPO, reason="remediation")
        assert argv[argv.index("--issue") + 1] == str(ITEMS[0])
        assert argv[argv.index("--repo") + 1] == REPO

    def test_failed_hop_is_reported_not_swallowed(self):
        plan = od.bounded_dispatch_plan(
            planned_sequence=[ITEMS[0]],
            plan_issue=PLAN_ISSUE,
            daily_epic=DAILY_EPIC,
            repo=REPO,
            reason="remediation",
        )
        runner = FakeRunner(rc=1, stdout="", stderr="boom")
        results = od.dispatch_items(plan, runner=runner)
        assert results[0]["dispatched"] is False
        assert results[0]["error"] == "boom"


# --------------------------------------------------------------------------
# 2. no mention-based and no label-based dispatch path exists
# --------------------------------------------------------------------------


class TestNoProhibitedDispatchPath:
    """The highest-value assertions here. Both prohibited paths dispatch at
    *write* time, so an implementation using one passes every positive test and
    fails only in production -- which is why these are asserted structurally over
    the module source, not merely behaviourally."""

    @pytest.mark.parametrize(
        "source", [OPS_DISPATCH_SRC, OPS_TRACKER_SRC], ids=["dispatch", "tracker"]
    )
    def test_no_agent_mention_string_in_module_source(self, source):
        """Any `@agent-` literal in this unit's source is a dispatch primitive.
        Prose in docstrings names roles without the prefix ("the delivery role")
        precisely so this assertion can be absolute."""
        offenders = [
            line
            for line in source.splitlines()
            if re.search(r"@agent-[a-z]", line, re.IGNORECASE)
        ]
        assert offenders == []

    @pytest.mark.parametrize(
        "source", [OPS_DISPATCH_SRC, OPS_TRACKER_SRC], ids=["dispatch", "tracker"]
    )
    def test_module_never_applies_a_label(self, source):
        """Label-based dispatch is the second prohibited path. Nothing in this
        unit may add a label at all -- `--add-label`, `--label`, or an edit."""
        for token in ("--add-label", "--label", "issue edit"):
            assert token not in source

    def test_dispatching_labels_recognises_the_prohibited_prefix(self):
        """The helper exists so the prohibition is checkable, not merely
        promised."""
        assert od.dispatching_labels(["agent-developer", "bug"]) == ["agent-developer"]
        assert od.dispatching_labels(["bug", "security"]) == []

    def test_no_label_is_applied_across_a_full_dispatch_run(self, gh, tmp_path):
        """Behavioural counterpart: drive the real fan-out and assert the gh seam
        saw no label write and no mention."""
        ledger = write_orchestration_shard(tmp_path / "run")
        _run_dispatch_items(ledger, gh)

        for args in gh.calls:
            joined = " ".join(args)
            assert "--label" not in joined
            assert "@agent-" not in joined

    def test_a_reason_carrying_a_mention_is_rejected(self):
        """A mention smuggled through the reason would reach a comment body and
        dispatch from there."""
        with pytest.raises(od.DispatchError, match="mention"):
            od.build_item_dispatch(ITEMS[0], repo=REPO, reason="ask @agent-developer")

    @pytest.mark.parametrize(
        "source", [OPS_DISPATCH_SRC, OPS_TRACKER_SRC], ids=["dispatch", "tracker"]
    )
    def test_no_vault_or_tenant_credential_is_reached(self, source):
        """The night runs `is_human_rooted=false`, so every run gets
        `authorized_user_id=""` and no vault credential (#3174 policy, #4559
        §7.1). Everything here must be a GitHub App installation-token
        operation; a reach for a stored credential would fail closed at runtime
        on a path nobody watches.

        Checked over CODE only -- comments are stripped first, because the module
        docstring legitimately explains the no-credential policy and would
        otherwise trip its own assertion."""
        code = _strip_comments_and_docstrings(source).lower()
        for token in ("adp-cred", "vault", "secretsmanager", "get-secret-value"):
            assert token not in code


# --------------------------------------------------------------------------
# 3. the plan and the dated parent are never dispatch targets
# --------------------------------------------------------------------------


class TestTargetRejection:
    def test_plan_issue_is_rejected(self):
        """An index. An agent dispatched onto it opens a pull request against a
        coordination artifact."""
        with pytest.raises(od.DispatchError, match="plan"):
            od.validate_item_target(
                PLAN_ISSUE,
                planned_sequence=ITEMS + [PLAN_ISSUE],
                plan_issue=PLAN_ISSUE,
                daily_epic=DAILY_EPIC,
            )

    def test_dated_parent_is_rejected(self):
        """A container for the work items, not one of them."""
        with pytest.raises(od.DispatchError, match="dated parent"):
            od.validate_item_target(
                DAILY_EPIC,
                planned_sequence=ITEMS + [DAILY_EPIC],
                plan_issue=PLAN_ISSUE,
                daily_epic=DAILY_EPIC,
            )

    def test_item_off_the_planned_sequence_is_rejected(self):
        """This is also where the bound comes from: dispatch is closed over the
        plan, so it cannot exceed it."""
        with pytest.raises(od.DispatchError, match="not one of this night"):
            od.validate_item_target(
                999999,
                planned_sequence=ITEMS,
                plan_issue=PLAN_ISSUE,
                daily_epic=DAILY_EPIC,
            )

    def test_non_integer_target_is_rejected(self):
        for bad in ("5101", None, True, 5101.0):
            with pytest.raises(od.DispatchError):
                od.validate_item_target(
                    bad,
                    planned_sequence=ITEMS,
                    plan_issue=PLAN_ISSUE,
                    daily_epic=DAILY_EPIC,
                )

    def test_a_planned_item_is_accepted(self):
        for item in ITEMS:
            od.validate_item_target(
                item,
                planned_sequence=ITEMS,
                plan_issue=PLAN_ISSUE,
                daily_epic=DAILY_EPIC,
            )

    def test_plan_issue_inside_the_sequence_never_gets_dispatched(self, gh, tmp_path):
        """Defence in depth: even if U10 wrote the plan's own number into the
        sequence, the fan-out must refuse rather than dispatch onto it."""
        ledger = write_orchestration_shard(
            tmp_path / "run", sequence=ITEMS + [PLAN_ISSUE]
        )
        with pytest.raises(od.DispatchError):
            od.bounded_dispatch_plan(
                planned_sequence=od.load_plan(ledger)["planned_sequence"],
                plan_issue=PLAN_ISSUE,
                daily_epic=DAILY_EPIC,
                repo=REPO,
                reason="remediation",
            )


# --------------------------------------------------------------------------
# 4. the bound is a count
# --------------------------------------------------------------------------


class TestFanOutBound:
    def test_fresh_night_dispatches_exactly_one_run_per_item(self):
        plan = od.bounded_dispatch_plan(
            planned_sequence=ITEMS,
            plan_issue=PLAN_ISSUE,
            daily_epic=DAILY_EPIC,
            repo=REPO,
            reason="remediation",
        )
        assert plan["count"] == len(ITEMS)
        assert plan["bound"] == len(ITEMS)
        assert len(plan["dispatches"]) == len(ITEMS)

    def test_resume_does_not_redispatch_started_items(self):
        """The retry path is the unbounded case: one run per item per retry is
        what turns a findings night into a capacity incident."""
        plan = od.bounded_dispatch_plan(
            planned_sequence=ITEMS,
            plan_issue=PLAN_ISSUE,
            daily_epic=DAILY_EPIC,
            repo=REPO,
            reason="remediation",
            dispatched=[ITEMS[0], ITEMS[1]],
        )
        assert [d["number"] for d in plan["dispatches"]] == [ITEMS[2]]
        assert plan["count"] == len(ITEMS)
        assert plan["count"] <= plan["bound"]

    def test_fully_dispatched_night_dispatches_nothing(self):
        plan = od.bounded_dispatch_plan(
            planned_sequence=ITEMS,
            plan_issue=PLAN_ISSUE,
            daily_epic=DAILY_EPIC,
            repo=REPO,
            reason="remediation",
            dispatched=list(ITEMS),
        )
        assert plan["dispatches"] == []
        assert plan["count"] <= plan["bound"]

    def test_duplicate_items_in_the_sequence_dispatch_once(self):
        plan = od.bounded_dispatch_plan(
            planned_sequence=[ITEMS[0], ITEMS[0], ITEMS[1]],
            plan_issue=PLAN_ISSUE,
            daily_epic=DAILY_EPIC,
            repo=REPO,
            reason="remediation",
        )
        assert [d["number"] for d in plan["dispatches"]] == [ITEMS[0], ITEMS[1]]

    def test_dispatch_follows_the_plans_order(self):
        """Order is the barrier's, not re-derived: a second ordering can disagree
        with the plan the morning reads."""
        sequence = [ITEMS[2], ITEMS[0], ITEMS[1]]
        plan = od.bounded_dispatch_plan(
            planned_sequence=sequence,
            plan_issue=PLAN_ISSUE,
            daily_epic=DAILY_EPIC,
            repo=REPO,
            reason="remediation",
        )
        assert [d["number"] for d in plan["dispatches"]] == sequence

    def test_over_bound_count_is_raised(self):
        """The assertion is on a number, not a loop, so it cannot be satisfied by
        a convention that a later edit breaks."""
        with pytest.raises(od.DispatchError, match="exceeds"):
            od.assert_within_bound({"count": len(ITEMS) + 1, "bound": len(ITEMS)})

    def test_bound_holds_when_the_night_created_nothing(self):
        plan = od.bounded_dispatch_plan(
            planned_sequence=[],
            plan_issue=PLAN_ISSUE,
            daily_epic=DAILY_EPIC,
            repo=REPO,
            reason="remediation",
        )
        assert plan["dispatches"] == []
        assert plan["bound"] == 0

    def test_already_dispatched_reads_the_ops_shards(self, tmp_path):
        """The ops shard is the durable "already started" record, which is what
        makes a retried run adopt the night instead of re-dispatching it."""
        ledger = tmp_path / "run"
        ledger.mkdir(parents=True)
        for item in (ITEMS[0], ITEMS[1]):
            ost.write_ops_shard(
                ledger,
                item=item,
                record=ost.initial_record(NOW),
                run_date=RUN_DATE,
                generated_at=NOW,
            )
        assert od.already_dispatched(ledger) == [ITEMS[0], ITEMS[1]]

    def test_end_to_end_run_stays_within_bound(self, gh, tmp_path):
        ledger = write_orchestration_shard(tmp_path / "run")
        runner = _run_dispatch_items(ledger, gh)
        assert len(runner.calls) <= len(ITEMS)

    def test_a_rerun_after_a_full_night_dispatches_nothing(self, gh, tmp_path):
        """The bound across the whole retry, end to end: the second pass must add
        no runs at all."""
        ledger = write_orchestration_shard(tmp_path / "run")
        first = _run_dispatch_items(ledger, gh)
        second = _run_dispatch_items(ledger, gh)

        assert len(first.calls) == len(ITEMS)
        assert second.calls == []

    def test_plan_without_a_sequence_is_refused(self, tmp_path):
        ledger = write_orchestration_shard(tmp_path / "run", sequence=[])
        with pytest.raises(od.DispatchError, match="no planned_sequence"):
            od.load_plan(ledger)


def _run_dispatch_items(ledger: Path, gh: FakeGh) -> FakeRunner:
    """Drive the real fan-out against a ledger directory, returning the trigger
    seam so a test can count the hops."""
    plan_doc = od.load_plan(ledger)
    plan = od.bounded_dispatch_plan(
        planned_sequence=plan_doc["planned_sequence"],
        plan_issue=PLAN_ISSUE,
        daily_epic=plan_doc["daily_epic"],
        repo=REPO,
        reason="nightly security remediation",
        dispatched=od.already_dispatched(ledger),
    )
    runner = FakeRunner(stdout="")
    results = od.dispatch_items(plan, runner=runner)
    for result in results:
        if not result["dispatched"]:
            continue
        ost.write_ops_shard(
            ledger,
            item=result["number"],
            record=ost.initial_record(NOW),
            run_date=RUN_DATE,
            generated_at=NOW,
        )
        od.post_status_comment(
            REPO,
            PLAN_ISSUE,
            od.render_status_comment(number=result["number"], status="in_progress"),
        )
    return runner


# --------------------------------------------------------------------------
# 5. stuck, by each of the two paths, with the reason stamped
# --------------------------------------------------------------------------


class TestStuckByFailedRuns:
    def test_threshold_failed_runs_reaches_stuck_with_a_reason(self):
        """Path one. Without a definition an item stays in progress forever, the
        night never finalizes, and the morning report never reaches a final
        state -- a run with no result and no failure is the one outcome nobody
        investigates."""
        record = ost.initial_record(NOW)
        for _ in range(FAILED_RUNS_THRESHOLD):
            record = ost.record_failed_run(record, NOW, SCHEMA)

        assert record["status"] == ost.STATUS_STUCK
        assert record["reason"] == "failed_run_limit"
        assert record["failed_runs"] == FAILED_RUNS_THRESHOLD
        assert ost.is_terminal(record)

    def test_one_below_the_threshold_stays_in_progress(self):
        """Pins the boundary. Without this, a rule that tripped on the first
        failure would pass the test above."""
        record = ost.initial_record(NOW)
        for _ in range(FAILED_RUNS_THRESHOLD - 1):
            record = ost.record_failed_run(record, NOW, SCHEMA)

        assert record["status"] == ost.STATUS_IN_PROGRESS
        assert record.get("reason") in (None, "")
        assert not ost.is_terminal(record)

    def test_a_failed_run_restamps_the_transition_clock(self):
        """A failure IS a transition. Otherwise the 24-hour path fires on an item
        that is actively being retried and records staleness as the reason for
        what is really a failure loop -- the wrong instruction for the morning."""
        record = ost.initial_record(NOW)
        later = hours_later(NOW, 5)
        record = ost.record_failed_run(record, later, SCHEMA)
        assert record["last_transition_at"] == later

    def test_a_retried_dispatch_does_not_reset_failures(self):
        """Otherwise an item could fail, be retried, and never reach the ceiling
        -- the "never ends" bug wearing a different hat."""
        record = ost.initial_record(NOW)
        record = ost.record_failed_run(record, NOW, SCHEMA)
        readopted = ost.apply_transition(record, event="dispatched", now=NOW)
        assert readopted["failed_runs"] == 1

    def test_failures_after_stuck_do_not_change_the_record(self):
        record = ost.initial_record(NOW)
        for _ in range(FAILED_RUNS_THRESHOLD):
            record = ost.record_failed_run(record, NOW, SCHEMA)
        assert ost.record_failed_run(record, hours_later(NOW, 1), SCHEMA) == record


class TestStuckByNoTransition:
    def test_no_transition_for_the_threshold_window_reaches_stuck(self):
        """Path two, and genuinely independent of path one: this item never
        failed a run, its run simply never reported back. Proven with an injected
        clock rather than by sleeping."""
        record = ost.initial_record(NOW)
        stale = hours_later(NOW, NO_TRANSITION_HOURS)
        refreshed = ost.refresh(record, stale, SCHEMA)

        assert refreshed["status"] == ost.STATUS_STUCK
        assert refreshed["reason"] == "no_transition_timeout"
        assert refreshed["failed_runs"] == 0
        assert ost.is_terminal(refreshed)

    def test_one_minute_before_the_window_stays_in_progress(self):
        """23h59m. The boundary assertion -- a rule that tripped immediately
        would satisfy the test above."""
        record = ost.initial_record(NOW)
        nearly = hours_later(NOW, NO_TRANSITION_HOURS - (1 / 60))
        refreshed = ost.refresh(record, nearly, SCHEMA)

        assert refreshed["status"] == ost.STATUS_IN_PROGRESS
        assert not ost.is_terminal(refreshed)

    def test_a_fixed_item_is_not_revisited_because_time_passed(self):
        record = ost.mark_fixed(ost.initial_record(NOW), NOW)
        assert ost.refresh(record, hours_later(NOW, 72), SCHEMA) == record

    def test_dispatch_stamps_a_transition_so_an_item_can_age_out(self):
        """An item with no stamped transition can never age out, so it would hang
        forever -- the exact failure this unit closes."""
        assert ost.initial_record(NOW)["last_transition_at"] == NOW


class TestTerminalStatesAndReasons:
    def test_merged_item_is_fixed_and_carries_no_reason(self):
        record = ost.mark_fixed(ost.initial_record(NOW), NOW)
        assert record["status"] == ost.STATUS_FIXED
        assert "reason" not in record

    def test_stuck_is_never_overwritten_by_fixed(self):
        """The stuck reason is the only explanation the morning has."""
        record = ost.initial_record(NOW)
        for _ in range(FAILED_RUNS_THRESHOLD):
            record = ost.record_failed_run(record, NOW, SCHEMA)
        with pytest.raises(ost.TrackerError, match="stuck reason"):
            ost.mark_fixed(record, NOW)

    def test_a_stuck_shard_without_a_reason_is_rejected_at_write_time(self, tmp_path):
        """Schema validation is what enforces "the reason is stamped" -- a blank
        cell must not be able to reach the report."""
        with pytest.raises(Exception):
            ost.build_ops_shard(
                item=ITEMS[0],
                record={
                    "status": ost.STATUS_STUCK,
                    "failed_runs": FAILED_RUNS_THRESHOLD,
                    "last_transition_at": NOW,
                },
                run_date=RUN_DATE,
                generated_at=NOW,
            )

    def test_both_stuck_reasons_render_as_prose(self):
        """Both paths must be explicable to the morning. A stuck item whose
        reason cannot be rendered is a report cell nobody can act on."""
        for reason in ("failed_run_limit", "no_transition_timeout"):
            body = od.render_status_comment(
                number=ITEMS[0], status="stuck", reason=reason
            )
            assert "stuck" in body
            assert body.strip()

    def test_stuck_without_a_recorded_reason_cannot_be_rendered(self):
        with pytest.raises(od.DispatchError, match="recorded reason"):
            od.render_status_comment(number=ITEMS[0], status="stuck", reason=None)

    def test_each_item_owns_its_own_shard_key(self, tmp_path):
        """One shard per item is what stops concurrent delivery runs losing each
        other's updates (FR-C29)."""
        ledger = tmp_path / "run"
        paths = {
            ost.write_ops_shard(
                ledger,
                item=item,
                record=ost.initial_record(NOW),
                run_date=RUN_DATE,
                generated_at=NOW,
            )
            for item in ITEMS
        }
        assert len(paths) == len(ITEMS)

    def test_round_trips_through_the_ledger(self, tmp_path):
        ledger = tmp_path / "run"
        record = ost.initial_record(NOW)
        ost.write_ops_shard(
            ledger, item=ITEMS[0], record=record, run_date=RUN_DATE, generated_at=NOW
        )
        assert ost.read_ops_record(ledger, ITEMS[0]) == record
        assert ost.read_ops_record(ledger, 999999) is None

    def test_a_corrupt_ops_shard_is_raised_not_read_as_absent(self, tmp_path):
        """Absent means "never dispatched", which re-dispatches the item. A
        corrupt shard must therefore fail loudly rather than read as absent and
        silently double a run."""
        ledger = tmp_path / "run"
        ledger.mkdir(parents=True)
        (ledger / f"shard-{ost.ops_stage(ITEMS[0])}.json").write_text(
            "{not json", encoding="utf-8"
        )
        with pytest.raises(ost.TrackerError, match="cannot read"):
            ost.read_ops_record(ledger, ITEMS[0])

    def test_a_shard_missing_this_items_status_is_raised(self, tmp_path):
        ledger = tmp_path / "run"
        ledger.mkdir(parents=True)
        (ledger / f"shard-{ost.ops_stage(ITEMS[0])}.json").write_text(
            json.dumps({"fields": {"story_status": {}}}), encoding="utf-8"
        )
        with pytest.raises(ost.TrackerError, match="records no status"):
            ost.read_ops_record(ledger, ITEMS[0])

    def test_progress_cannot_be_tracked_before_dispatch(self):
        with pytest.raises(ost.TrackerError, match="dispatched"):
            ost.apply_transition(None, event="failed", now=NOW)

    def test_unknown_event_is_refused(self):
        with pytest.raises(ost.TrackerError, match="unknown event"):
            ost.apply_transition(
                ost.initial_record(NOW), event="cancelled", now=NOW
            )

    def test_apply_transition_routes_each_event(self):
        record = ost.apply_transition(None, event="dispatched", now=NOW)
        assert record["status"] == ost.STATUS_IN_PROGRESS
        assert (
            ost.apply_transition(record, event="failed", now=NOW, schema=SCHEMA)[
                "failed_runs"
            ]
            == 1
        )
        assert (
            ost.apply_transition(record, event="merged", now=NOW)["status"]
            == ost.STATUS_FIXED
        )
        stale = hours_later(NOW, NO_TRANSITION_HOURS)
        assert (
            ost.apply_transition(record, event="sweep", now=stale, schema=SCHEMA)[
                "status"
            ]
            == ost.STATUS_STUCK
        )

    def test_ops_stage_rejects_a_non_item(self):
        for bad in (0, -1, "5101", True):
            with pytest.raises(ost.TrackerError):
                ost.ops_stage(bad)

    def test_sweep_moves_a_stale_item_and_leaves_a_fresh_one(self, tmp_path):
        """The sweep is what makes the 24-hour path fire on an item whose run
        vanished: nothing else would ever look at it again."""
        ledger = tmp_path / "run"
        ost.write_ops_shard(
            ledger,
            item=ITEMS[0],
            record=ost.initial_record(NOW),
            run_date=RUN_DATE,
            generated_at=NOW,
        )
        ost.write_ops_shard(
            ledger,
            item=ITEMS[1],
            record=ost.initial_record(hours_later(NOW, NO_TRANSITION_HOURS)),
            run_date=RUN_DATE,
            generated_at=NOW,
        )
        rc = ost.main(
            [
                "sweep",
                "--ledger-dir",
                str(ledger),
                "--run-date",
                RUN_DATE,
                "--now",
                hours_later(NOW, NO_TRANSITION_HOURS),
            ]
        )
        assert rc == 0
        assert ost.read_ops_record(ledger, ITEMS[0])["status"] == ost.STATUS_STUCK
        assert ost.read_ops_record(ledger, ITEMS[1])["status"] == (
            ost.STATUS_IN_PROGRESS
        )

    def test_transition_cli_drives_an_item_to_stuck(self, tmp_path):
        """The CLI is the only clock read, so this covers the wiring the workflow
        actually calls: dispatch, then failures up to the ceiling."""
        ledger = tmp_path / "run"
        ledger.mkdir(parents=True)

        def transition(event: str) -> int:
            return ost.main(
                [
                    "transition",
                    "--ledger-dir",
                    str(ledger),
                    "--run-date",
                    RUN_DATE,
                    "--item",
                    str(ITEMS[0]),
                    "--event",
                    event,
                    "--now",
                    NOW,
                ]
            )

        assert transition("dispatched") == 0
        assert ost.read_ops_record(ledger, ITEMS[0])["status"] == (
            ost.STATUS_IN_PROGRESS
        )
        for _ in range(FAILED_RUNS_THRESHOLD):
            assert transition("failed") == 0

        record = ost.read_ops_record(ledger, ITEMS[0])
        assert record["status"] == ost.STATUS_STUCK
        assert record["reason"] == "failed_run_limit"

    def test_tracker_cli_reports_an_error_without_traceback(self, tmp_path):
        rc = ost.main(
            [
                "transition",
                "--ledger-dir",
                str(tmp_path),
                "--run-date",
                RUN_DATE,
                "--item",
                str(ITEMS[0]),
                "--event",
                "failed",
                "--now",
                NOW,
            ]
        )
        assert rc == 1


# --------------------------------------------------------------------------
# 6. status comments carry no dispatch token
# --------------------------------------------------------------------------


class TestStatusProse:
    @pytest.mark.parametrize(
        "status,reason",
        [
            ("in_progress", None),
            ("fixed", None),
            ("stuck", "failed_run_limit"),
            ("stuck", "no_transition_timeout"),
        ],
    )
    def test_no_rendered_status_carries_a_mention(self, status, reason):
        body = od.render_status_comment(
            number=ITEMS[0], status=status, reason=reason
        )
        assert "@agent-" not in body
        assert str(ITEMS[0]) in body

    def test_posted_bytes_carry_no_mention(self, gh):
        """Asserted on the exact bytes handed to the gh seam, not on the render's
        return value: posting is the step that would turn a status update into a
        dispatch, and every state change posts one."""
        for status, reason in (
            ("in_progress", None),
            ("fixed", None),
            ("stuck", "failed_run_limit"),
        ):
            od.post_status_comment(
                REPO,
                PLAN_ISSUE,
                od.render_status_comment(
                    number=ITEMS[0], status=status, reason=reason
                ),
            )

        assert len(gh.comment_bodies) == 3
        for body in gh.comment_bodies:
            assert "@agent-" not in body

    def test_status_is_posted_on_the_plan_issue(self, gh):
        od.post_status_comment(
            REPO,
            PLAN_ISSUE,
            od.render_status_comment(number=ITEMS[0], status="fixed"),
        )
        args = gh.calls[0]
        assert args[:2] == ["issue", "comment"]
        assert args[2] == str(PLAN_ISSUE)

    def test_a_body_carrying_a_mention_is_refused_at_post_time(self, gh):
        """Belt and braces: the lint runs again on the exact bytes, so a body
        assembled by some future caller cannot bypass the render's guarantees."""
        with pytest.raises(od.DispatchError, match="mention"):
            od.post_status_comment(REPO, PLAN_ISSUE, "ping @agent-developer please")
        assert gh.calls == []

    def test_unknown_status_is_refused(self):
        with pytest.raises(od.DispatchError, match="unknown status"):
            od.render_status_comment(number=ITEMS[0], status="cancelled")

    def test_empty_prose_is_refused(self):
        for bad in ("", "   ", None):
            with pytest.raises(od.DispatchError):
                od.lint_prose(bad, what="status comment")

    def test_scanner_detail_is_refused_in_prose(self):
        """NEV-2 / NT-11: the comment lands on a permanently retained issue, so
        reproduction detail must not reach it."""
        patterns = [{"id": "poc", "regex": re.compile(r"curl\s+-X\s+POST")}]
        with pytest.raises(od.DispatchError, match="banned pattern"):
            od.lint_prose(
                "reproduce with curl -X POST /admin", patterns, what="status comment"
            )

    def test_failed_dispatch_of_a_status_comment_is_raised(self, monkeypatch):
        monkeypatch.setattr(ue, "_gh", FakeRunner(rc=1, stdout="", stderr="nope"))
        with pytest.raises(od.DispatchError, match="failed to comment"):
            od.post_status_comment(
                REPO,
                PLAN_ISSUE,
                od.render_status_comment(number=ITEMS[0], status="fixed"),
            )


# --------------------------------------------------------------------------
# 7. H1 -- an injected reason cannot override the pinned literals
# --------------------------------------------------------------------------


def _terraform_block(kind: str, name: str, *, strip_comments: bool = False) -> str:
    """The source text of one Terraform resource block.

    `strip_comments` drops `#` lines. These blocks document the controls they
    implement -- one comment explains that `create_issue` is deliberately absent,
    naming it -- so a scan for an absent token has to look at configuration only,
    or the explanation trips the assertion it exists to support.
    """
    source = EVENTBRIDGE_TF.read_text(encoding="utf-8")
    start = source.index(f'resource "{kind}" "{name}"')
    nxt = source.find("\nresource ", start + 1)
    block = source[start : nxt if nxt != -1 else len(source)]
    if strip_comments:
        block = "\n".join(
            line for line in block.splitlines() if not line.lstrip().startswith("#")
        )
    return block


class TestReasonInjectionCannotOverridePinnedIdentity:
    """EventBridge splices InputTransformer substitutions in WITHOUT escaping, so
    a `reason` carrying a quote could close its own string and append a duplicate
    `"persona"` key. JSON parsers keep the LAST occurrence of a repeated key, so
    the injected one would win over the Terraform literal that #4559 §5 calls the
    primary control -- `events:PutEvents` cannot be scoped to an event source, so
    that literal is the only thing pinning persona, identity and target repo.
    This is privilege escalation, not input tidiness."""

    @pytest.mark.parametrize(
        "payload",
        [
            'x", "persona": "architect',
            'x", "service_identity": "eventbridge:adp-dev-anything',
            'x", "target": {"repo": "attacker/evil"}, "z": "',
            'x"}, "detail": {"adp_trigger": {"persona": "operations',
            "brace{injection}",
            "back\\slash",
            "new\nline",
            "quote'single",
        ],
    )
    def test_injection_payloads_are_rejected(self, payload):
        with pytest.raises(od.DispatchError):
            od.build_root_event(
                run_date=RUN_DATE, issue_number=PLAN_ISSUE, reason=payload
            )

    def test_a_benign_reason_is_still_accepted(self):
        """The allowlist must not be so tight that the real reasons stop working
        -- a guard that blocks the happy path gets reverted."""
        for good in (
            od.DEFAULT_ROOT_REASON,
            "nightly security remediation plan ready",
            "stage 2: dispatch, bounded",
            "run_date 2026-08-30 - plan ready",
        ):
            entry = od.build_root_event(
                run_date=RUN_DATE, issue_number=PLAN_ISSUE, reason=good
            )
            assert json.loads(entry["Detail"])["reason"] == good

    def test_the_detail_json_survives_a_round_trip(self):
        """Whatever passes the allowlist must still produce parseable JSON with
        exactly one value per key."""
        entry = od.build_root_event(
            run_date=RUN_DATE, issue_number=PLAN_ISSUE, reason="plan ready"
        )
        detail = json.loads(entry["Detail"])
        assert detail["reason"] == "plan ready"
        assert detail["issue_number"] == PLAN_ISSUE

    def test_per_item_reasons_are_linted_too(self):
        with pytest.raises(od.DispatchError):
            od.build_item_dispatch(
                ITEMS[0], repo=REPO, reason="ask @agent-developer to fix"
            )

    def test_event_field_lint_still_applies_the_prose_checks(self):
        """Layered, not replaced: the allowlist runs after the mention and
        banned-pattern checks, so neither is lost."""
        with pytest.raises(od.DispatchError, match="mention"):
            od.lint_event_field("see @agent-developer", what="root dispatch reason")

    def test_status_comments_keep_their_hash_references(self):
        """The allowlist is deliberately NOT applied to comment bodies: every
        status comment carries `#<number>`, so one allowlist over both surfaces
        would reject them all. This pins the two linters apart."""
        body = od.render_status_comment(number=ITEMS[0], status="in_progress")
        assert f"#{ITEMS[0]}" in body
        with pytest.raises(od.DispatchError):
            od.lint_event_field(body, what="root dispatch reason")

    def test_caller_supplied_values_precede_the_pinned_literals(self):
        """The second, independent defence. Even if a quote reached the template,
        an injected duplicate placed BEFORE the literal is overridden by the
        literal rather than the other way round. Order here is a security
        property, so it is asserted rather than left to a comment."""
        target = _terraform_block(
            "aws_cloudwatch_event_target",
            "security_agent_to_lambda",
            strip_comments=True,
        )
        template = target[target.index("input_template") :]

        # Only the STRING-substituted values can inject a key: a value spliced
        # inside quotes can close them and open a new pair. `<issue_number>`
        # interpolates unquoted as a JSON number and is int-validated before it is
        # ever emitted, so it is not part of this claim -- it is asserted numeric
        # by `test_root_requires_an_existing_issue_number` instead.
        last_injectable = max(
            template.index("<reason>"), template.index("<run_date>")
        )
        assert template.index('"persona"') > last_injectable
        assert template.index('"service_identity"') > last_injectable

        # `repo` is pinned inside `target`, so it must outrank the injectable
        # values too -- a redirected target repo is a redirected pull request.
        assert template.index('"repo"') > last_injectable

    def test_persona_identity_and_repo_are_terraform_literals(self):
        """None of the three may be sourced from `$.detail`: that would hand the
        emitter the choice of persona, identity or target repo."""
        target = _terraform_block(
            "aws_cloudwatch_event_target",
            "security_agent_to_lambda",
            strip_comments=True,
        )
        assert '"persona": "operations"' in target
        assert "service_identity" in target
        for forbidden in (
            "$.detail.persona",
            "$.detail.service_identity",
            "$.detail.repo",
            "$.detail.target",
        ):
            assert forbidden not in target

    def test_the_rule_and_its_identity_row_are_gated_together(self):
        """A rule without its identity row fails closed at 403
        unknown_service_identity -- safe, but silent, retried by nothing, and the
        night then reports nothing."""
        source = EVENTBRIDGE_TF.read_text(encoding="utf-8")
        gate = "var.enable_eventbridge_security_agent_rule ? 1 : 0"
        for block in (
            _terraform_block("aws_cloudwatch_event_rule", "security_agent_dispatch"),
            _terraform_block(
                "aws_cloudwatch_event_target", "security_agent_to_lambda"
            ),
            _terraform_block(
                "aws_dynamodb_table_item", "security_agent_service_identity"
            ),
        ):
            assert gate in block
        assert source.count(gate) >= 3

    def test_create_issue_is_absent_from_the_security_target(self):
        """Inert on this path (#4559 §7.2). `issue_number` is required instead."""
        target = _terraform_block(
            "aws_cloudwatch_event_target",
            "security_agent_to_lambda",
            strip_comments=True,
        )
        assert "create_issue" not in target
        assert "<issue_number>" in target


# --------------------------------------------------------------------------
# 8. the gate that runs this file must actually cover these subjects
# --------------------------------------------------------------------------


class TestTransportSeams:
    """The two seams must launch the two different binaries. This is the lineage
    claim at its lowest level: everything above is argv construction, and these
    are what actually decide whether a dispatch mints a root or extends the
    chain."""

    def test_aws_seam_invokes_the_aws_cli(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return type("P", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

        monkeypatch.setattr(od.subprocess, "run", fake_run)
        od._aws(["events", "put-events"])
        assert seen["argv"][0] == "aws"

    def test_trigger_seam_invokes_adp_trigger(self, monkeypatch):
        """`adp-trigger` and nothing else: it reads lineage from the pod
        environment and SigV4-signs, so the spawned run stays under the one
        root."""
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(od.subprocess, "run", fake_run)
        od._adp_trigger(["--persona", "developer"])
        assert seen["argv"][0] == "adp-trigger"

    def test_gh_seam_delegates_to_the_shared_one(self, gh):
        """One seam, so a test that patches it intercepts every GitHub write."""
        od._gh(["issue", "view", "1"])
        assert gh.calls == [["issue", "view", "1"]]


class TestCli:
    """The CLI is what a workflow step invokes, and it holds the only clock read
    and the only process launches. Covering it here means the wiring is asserted,
    not just the pure decision functions underneath it."""

    def test_root_dispatch_emits_once_then_is_idempotent(self, tmp_path, monkeypatch):
        """A retried nightly is normal, so the second call must not emit: a second
        `put-events` mints a second root and doubles the fan-out."""
        aws = FakeRunner()
        monkeypatch.setattr(od, "_aws", aws)
        marker_dir = tmp_path / "marker"

        argv = [
            "root-dispatch",
            "--run-date",
            RUN_DATE,
            "--issue-number",
            str(PLAN_ISSUE),
            "--marker-dir",
            str(marker_dir),
        ]
        assert od.main(argv) == 0
        assert len(aws.calls) == 1
        assert (marker_dir / od.ROOT_MARKER_NAME).exists()

        assert od.main(argv) == 0
        assert len(aws.calls) == 1

    def test_root_dispatch_refuses_an_injected_reason(self, tmp_path, monkeypatch):
        """End to end at the CLI boundary: the H1 payload must not reach the
        emitter at all."""
        aws = FakeRunner()
        monkeypatch.setattr(od, "_aws", aws)
        rc = od.main(
            [
                "root-dispatch",
                "--run-date",
                RUN_DATE,
                "--issue-number",
                str(PLAN_ISSUE),
                "--marker-dir",
                str(tmp_path),
                "--reason",
                'x", "persona": "architect',
            ]
        )
        assert rc == 1
        assert aws.calls == []

    def test_dispatch_items_cli_fans_out_within_the_bound(
        self, tmp_path, monkeypatch, gh
    ):
        ledger = write_orchestration_shard(tmp_path / "run")
        trigger = FakeRunner(stdout="")
        monkeypatch.setattr(od, "_adp_trigger", trigger)

        rc = od.main(
            [
                "dispatch-items",
                "--repo",
                REPO,
                "--plan-issue",
                str(PLAN_ISSUE),
                "--ledger-dir",
                str(ledger),
            ]
        )
        assert rc == 0
        assert len(trigger.calls) == len(ITEMS)
        # One shard and one status comment per dispatched item.
        assert od.already_dispatched(ledger) == sorted(ITEMS)
        assert len(gh.comment_bodies) == len(ITEMS)
        for body in gh.comment_bodies:
            assert "@agent-" not in body

    def test_dispatch_items_cli_rerun_dispatches_nothing(
        self, tmp_path, monkeypatch, gh
    ):
        ledger = write_orchestration_shard(tmp_path / "run")
        trigger = FakeRunner(stdout="")
        monkeypatch.setattr(od, "_adp_trigger", trigger)
        argv = [
            "dispatch-items",
            "--repo",
            REPO,
            "--plan-issue",
            str(PLAN_ISSUE),
            "--ledger-dir",
            str(ledger),
        ]
        assert od.main(argv) == 0
        assert od.main(argv) == 0
        assert len(trigger.calls) == len(ITEMS)

    def test_dispatch_items_cli_reports_a_failed_hop(self, tmp_path, monkeypatch, gh):
        """A hop that fails must surface as a non-zero exit, not a silent gap: the
        item would otherwise be neither driven nor reported."""
        ledger = write_orchestration_shard(tmp_path / "run")
        monkeypatch.setattr(od, "_adp_trigger", FakeRunner(rc=1, stdout="", stderr="x"))
        rc = od.main(
            [
                "dispatch-items",
                "--repo",
                REPO,
                "--plan-issue",
                str(PLAN_ISSUE),
                "--ledger-dir",
                str(ledger),
            ]
        )
        assert rc == 1
        assert od.already_dispatched(ledger) == []

    def test_status_cli_posts_prose(self, gh):
        rc = od.main(
            [
                "status",
                "--repo",
                REPO,
                "--plan-issue",
                str(PLAN_ISSUE),
                "--item",
                str(ITEMS[0]),
                "--status",
                "stuck",
                "--reason",
                "failed_run_limit",
            ]
        )
        assert rc == 0
        assert len(gh.comment_bodies) == 1
        assert "@agent-" not in gh.comment_bodies[0]

    def test_cli_reports_errors_without_a_traceback(self, tmp_path):
        """An operator reads the annotation, not a stack trace."""
        rc = od.main(
            [
                "dispatch-items",
                "--repo",
                REPO,
                "--plan-issue",
                str(PLAN_ISSUE),
                "--ledger-dir",
                str(tmp_path / "empty"),
            ]
        )
        assert rc == 1

    def test_unreadable_marker_is_raised(self, tmp_path, monkeypatch):
        """A corrupt marker must not be treated as absent -- that would re-emit
        the root and mint a second chain."""
        monkeypatch.setattr(od, "_aws", FakeRunner())
        marker_dir = tmp_path / "marker"
        marker_dir.mkdir(parents=True)
        (marker_dir / od.ROOT_MARKER_NAME).write_text("{not json", encoding="utf-8")
        rc = od.main(
            [
                "root-dispatch",
                "--run-date",
                RUN_DATE,
                "--issue-number",
                str(PLAN_ISSUE),
                "--marker-dir",
                str(marker_dir),
            ]
        )
        assert rc == 1

    def test_put_events_failure_is_surfaced(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            od, "_aws", FakeRunner(rc=1, stdout="", stderr="AccessDenied")
        )
        rc = od.main(
            [
                "root-dispatch",
                "--run-date",
                RUN_DATE,
                "--issue-number",
                str(PLAN_ISSUE),
                "--marker-dir",
                str(tmp_path),
            ]
        )
        assert rc == 1
        assert not (Path(tmp_path) / od.ROOT_MARKER_NAME).exists()

    def test_unparseable_put_events_response_is_raised(self):
        with pytest.raises(od.DispatchError, match="unparseable"):
            od.emit_root_event(
                od.build_root_event(run_date=RUN_DATE, issue_number=PLAN_ISSUE),
                runner=FakeRunner(stdout="not json"),
            )


class TestScriptTestsPin:
    """A gate that does not trigger is indistinguishable from one that passes.
    These assertions are why the pin cannot silently rot: if a later edit drops a
    subject from the workflow, this suite fails rather than quietly stopping to
    cover it."""

    @pytest.fixture
    def workflow(self) -> str:
        return SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "subject",
        [".github/scripts/ops_dispatch.py", ".github/scripts/ops_stuck_tracker.py"],
    )
    def test_subject_is_in_both_paths_filters(self, workflow, subject):
        """Both triggers: a pull_request-only workflow never attaches a check-run
        to a main commit, so "does this gate exist on main?" is unanswerable."""
        assert workflow.count(f"- '{subject}'") == 2

    def test_this_test_file_is_pinned_by_name(self, workflow):
        """By name, not by glob: a glob silently stops covering a file that is
        moved or renamed, which is the loss this workflow exists to prevent."""
        assert "tests/test_ops_dispatch_bounds.py" in workflow

    def test_the_eventbridge_terraform_is_a_watched_artifact(self, workflow):
        """This suite asserts against the input template's key order, which is a
        security property. If that file can change without re-running this gate,
        the assertion is decorative."""
        assert (
            "modules/agent-factory/webhook-ingress/infra/eventbridge.tf" in workflow
        )


# --------------------------------------------------------------------------
# the caller -- #4598, §8 items 6-9
# --------------------------------------------------------------------------


class TestNightlyWiring:
    """The scripts above are only reachable if something CALLS them.

    U11 shipped the dispatch machinery and its tests; every gate before this
    class exercises the module in isolation. None of them fails when nothing
    invokes it -- so until #4598 the whole unit could be dead code and this suite
    would stay green. These gates bind the module to its caller.

    The asymmetry below is the substance, not an omission: `root-dispatch` MUST be
    a step in the nightly, and `dispatch-items` / `transition` MUST NOT be. The
    two transports are not interchangeable (#4559 §1). A GitHub Actions runner
    cannot originate a chain -- `adp-trigger` and `/agent/trigger` are both closed
    to root-minting -- so the root has to be `put-events`. And the per-item hops
    cannot run here: `adp-trigger` is installed only in the agent-worker image,
    and its client hard-requires ADP_CORRELATION_ID / ADP_MESSAGE_ID /
    ADP_CHAIN_DEPTH / ADP_TRIGGER_ENDPOINT from the pod environment, which a
    runner does not have. They belong to the `operations` run the root starts.

    So a workflow step calling `dispatch-items` is not a harmless extra: it fails
    every night, and the obvious "fix" -- switching the per-item hops to
    `put-events` -- silently mints a fresh root per item at `chain_depth=0`,
    disabling the depth cap and the loop guard and unlinking the night in lineage.
    That is the regression this class exists to catch, and it is invisible to
    every other test in the file.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def workflow() -> dict:
        yaml = pytest.importorskip("yaml", reason="PyYAML required to parse workflows")
        return yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))

    @staticmethod
    @pytest.fixture(scope="class")
    def run_steps(workflow) -> list[dict]:
        """Every shell step in the nightly, across all jobs.

        Collected workflow-wide rather than from one job by name: the property is
        "the nightly does/does not invoke this command anywhere", and a job-scoped
        search would pass if a later unit moved the call into a job this fixture
        did not know to look at.
        """
        return [
            step
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if step.get("run")
        ]

    def _steps_invoking(self, run_steps, script: str, subcommand: str) -> list[dict]:
        return [
            step
            for step in run_steps
            if script in step["run"] and subcommand in step["run"]
        ]

    # -- the root dispatch IS wired (item 7) -------------------------------

    def test_the_nightly_emits_the_root_dispatch(self, run_steps):
        """Item 7. Without this the pipeline U8-U11 built stops at "filed a plan"
        and the drive-to-merge step never runs -- the gap #4598 closes."""
        steps = self._steps_invoking(run_steps, "ops_dispatch.py", "root-dispatch")
        assert len(steps) == 1, (
            "expected exactly one step invoking `ops_dispatch.py root-dispatch`; "
            f"found {len(steps)}. Two would mint two roots for one night"
        )

    def test_the_root_dispatch_passes_a_real_issue_number(self, run_steps):
        """#4559 §7.2: this path attaches an agent to an EXISTING issue and cannot
        create one. The worker runs `gh issue view $ISSUE_NUMBER` at startup, so an
        empty number does not degrade -- it kills the run before it does anything."""
        body = self._steps_invoking(run_steps, "ops_dispatch.py", "root-dispatch")[0][
            "run"
        ]
        assert "--issue-number" in body
        assert "--run-date" in body, "the night's identity is its date"
        assert "--marker-dir" in body, (
            "without the per-date marker the emit is not idempotent, and a retried "
            "night mints a second root (the envelope's dedup_key deduplicates "
            "nothing -- §7.3)"
        )

    def test_the_plan_is_filed_before_it_is_dispatched(self, run_steps):
        """Item 6, as an ORDER assertion. The number passed above has to come from
        somewhere; a dispatch that precedes the filing names an issue that does not
        exist yet."""
        names = [step["run"] for step in run_steps]
        plan = next(i for i, r in enumerate(names) if "join_barrier.py" in r)
        dispatch = next(i for i, r in enumerate(names) if "root-dispatch" in r)
        assert plan < dispatch, (
            "the night's plan must be filed before the root event carrying its "
            "number is emitted (#4559 §8 items 6 then 7)"
        )

    def test_the_root_dispatch_does_not_pass_the_pinned_identity_fields(
        self, run_steps
    ):
        """#4559 §5: persona, service_identity and the target repo are Terraform
        literals in the rule's InputTransformer, and that transformer is the
        primary control on this path because `events:PutEvents` cannot be scoped to
        an event source. A workflow that supplied any of them would hand a
        compromised runner the choice -- a privilege escalation, not a style slip.
        """
        body = self._steps_invoking(run_steps, "ops_dispatch.py", "root-dispatch")[0][
            "run"
        ]
        for forbidden in ("--persona", "--service-identity", "--repo"):
            assert forbidden not in body, (
                f"the root dispatch step passes {forbidden}, which belongs only in "
                "the rule's InputTransformer as a Terraform literal (§5)"
            )

    # -- the in-night hops are NOT wired here (item 9) ---------------------

    @pytest.mark.parametrize("subcommand", ["dispatch-items", "status"])
    def test_the_nightly_does_not_run_the_in_night_hops(self, run_steps, subcommand):
        """Item 9: every hop inside the night is an `adp-trigger` from the
        operations run, not a CI step. `adp-trigger` is absent from the ARC runner
        image and its client exits 2 without the pod's lineage env, so a step here
        fails on every run."""
        steps = self._steps_invoking(run_steps, "ops_dispatch.py", subcommand)
        assert steps == [], (
            f"the nightly invokes `ops_dispatch.py {subcommand}`, which cannot work "
            "on a GitHub Actions runner: it dispatches via `adp-trigger`, which is "
            "installed only in the agent-worker image and requires the pod's "
            "ADP_CORRELATION_ID / ADP_MESSAGE_ID / ADP_CHAIN_DEPTH / "
            "ADP_TRIGGER_ENDPOINT. This runs inside the dispatched operations run"
        )

    def test_the_nightly_does_not_run_the_stuck_tracker(self, run_steps):
        """Same boundary, other script. The tracker applies transitions for items
        the delivery role is driving; the nightly job has exited long before any
        item reaches a terminal state, so a `transition` call here could only ever
        record a state nothing had reached."""
        for subcommand in ("transition", "sweep"):
            steps = self._steps_invoking(
                run_steps, "ops_stuck_tracker.py", subcommand
            )
            assert steps == [], (
                f"the nightly invokes `ops_stuck_tracker.py {subcommand}`; item "
                "state is tracked by the delivery role during the night, not by "
                "the CI job that handed the plan off"
            )

    def test_no_workflow_step_dispatches_by_mention_or_label(self, run_steps):
        """The two prohibited paths, asserted at the CALLER as well as in the
        module. The module-level gates above prove `ops_dispatch.py` contains no
        such path; they say nothing about a workflow step doing it directly with
        `gh issue comment` or `gh issue edit --add-label agent-*`, which dispatches
        at write time and bypasses the sequencing entirely (#3626)."""
        for step in run_steps:
            assert not re.search(r"@agent-", step["run"]), (
                f"step {step.get('name')!r} contains an `@agent-` mention, which "
                "dispatches an agent at write time"
            )
            assert not re.search(r"--add-label\s+[\"']?agent-", step["run"]), (
                f"step {step.get('name')!r} adds an `agent-*` label, which is the "
                "other prohibited dispatch path"
            )

    # -- the wiring lands INERT (item 8) ----------------------------------

    def test_the_nightly_still_has_no_schedule_key(self, workflow):
        """#4598 is explicit that arming the cron is a separate, deliberate step:
        it needs a measured run duration and a window that does not collide with
        the three other nightly suites on the same shared dev environment."""
        # PyYAML follows YAML 1.1, where the bare key `on` is boolean True.
        triggers = workflow.get("on", workflow.get(True))
        assert triggers is not None, "the workflow declares no triggers at all"
        assert "schedule" not in triggers, (
            "wiring the dispatch must not also arm the cron -- see the header of "
            "security-agent-nightly.yml"
        )

    def test_the_eventbridge_rule_is_still_disabled(self):
        """The other half of "lands inert". With the rule off, the emit above
        matches nothing and starts no run, so this wiring is unreachable until
        someone enables it deliberately -- after the `events:PutEvents` grant is
        applied (§8 items 8 and 10)."""
        tfvars = (
            REPO_ROOT
            / "modules/agent-factory/webhook-ingress/infra/terraform.tfvars"
        ).read_text(encoding="utf-8")
        assert re.search(
            r"^enable_eventbridge_security_agent_rule\s*=\s*false\s*$",
            tfvars,
            re.MULTILINE,
        ), (
            "enable_eventbridge_security_agent_rule is no longer false; #4598 wires "
            "the caller and must leave the rule disabled. Arming it is a separate "
            "step, and the IAM events:PutEvents grant must be applied first"
        )

    def test_the_nightly_is_a_watched_artifact_of_this_suite(self):
        """This class asserts against the nightly's steps. If that file can change
        without re-running this gate, every assertion above is decorative -- the
        same reason the EventBridge Terraform is pinned."""
        workflow = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
        assert (
            workflow.count("- '.github/workflows/security-agent-nightly.yml'") == 2
        ), (
            "the nightly must be in BOTH paths filters: a pull_request-only "
            "trigger never attaches a check-run to a main commit"
        )
