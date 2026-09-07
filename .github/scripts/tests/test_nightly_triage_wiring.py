"""Gate for the nightly's triage stage (#4613).

Every script this stage calls is separately unit-tested and green. None of those
suites fails when nothing CALLS them, so until this wiring the whole middle of
the pipeline could be dead code and every other gate would stay green: the night
would scan, publish to a private prefix nothing reads, find no marker to join,
and exit having created nothing. These gates bind the scripts to their caller.

Gate coverage, from the issue's corrected `## Validation`:

* The `triage` job exists BETWEEN `code-review` and `deliver`, and carries the
  `issues: write` the filing needs -- which is also why it is not the tail of the
  metered 5.5-hour scanner job.
* The invocation set and its order. Asserted as a set as well as an order,
  because the two failures it prevents are different: a missing call breaks the
  night, and an EXTRA call is how this stage nearly shipped a
  `normalize_security_findings.py` invocation that would have exited 0 having
  done nothing (the script has no CLI) and a second orchestration-shard writer
  that would have contradicted `deliver`'s.
* `validate` is a hard gate: it precedes `file`, it is unconditional, and it is
  handed the SAME rendered-body inputs -- gating one body and filing another
  would make the gate decorative.
* The marker seam, executed rather than asserted from a flag string. The real
  writer's derived filename must match the glob `deliver` joins on, and the shard
  it writes must pass the validator `deliver`'s barrier runs it through. That
  second assertion is the one that would have caught the bare-marker defect
  (#4616): the writer exited 0 and the reader rejected the file one job later.
* The quiet night (NT-5) still leaves that marker, so the barrier can tell a
  scanner that found nothing from one that never signalled.
* `RUN_DATE` is ONE job output consumed by both downstream jobs. A per-job
  `date -u` is a per-job UTC-midnight straddle surface (#4609).
* `deliver` names the scanners the night actually runs (`--sources code-review`)
  and no longer carries the temporary `joinable` guard (#4603).

Deliberately NOT here: a `schedule:`-key guard. That gate already exists twice
over in `test_securityagent_preflight.py` (`:143`, `:162`), and a third copy
would be a second place to update and a false sense of a check that is already
made. The dormancy properties this file DOES assert are the ones adding a job
could plausibly break.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ensure_umbrella_epic as ue
import triage_group_findings as tg
from join_barrier import load_markers
from security_agent_ledger import SHARD_NAME_TEMPLATE, load_schema, validate_shard

REPO_ROOT = Path(__file__).resolve().parents[3]
NIGHTLY_WORKFLOW = REPO_ROOT / ".github/workflows/security-agent-nightly.yml"
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/script-tests.yml"
TFVARS = REPO_ROOT / "modules/agent-factory/webhook-ingress/infra/terraform.tfvars"

# Reused rather than re-invented: this stage's job is to make U9's writer run in
# CI, so the seam is asserted against the same corpus U9's own suite uses.
FIXTURES = Path(__file__).parent / "fixtures" / "triage-3984"
PLAN_FIXTURE = FIXTURES / "grouping-plan.json"
FINDINGS_FIXTURE = FIXTURES / "new-findings.json"

RUN_DATE = "2026-08-30"
RUN_ID = "99830451698"
GENERATED_AT = "2026-08-30T03:10:00Z"
FINDINGS_URI = "s3://adp-dev-security-scans-000000000000/security-agent/runs/2026-08-30"
REPO = "aws-e/adp"
UMBRELLA_NUM = 9001

TRIAGE_JOB = "triage"

yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the workflow")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict:
    """The nightly, parsed as YAML.

    Parsed, never grepped: a `grep` for a step name passes on a commented-out
    line and a `grep` for a job key passes on a mention of it in a comment --
    and this file is mostly comments by line count.
    """
    return yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow) -> dict:
    return workflow["jobs"]


@pytest.fixture(scope="module")
def triage_job(jobs) -> dict:
    assert TRIAGE_JOB in jobs, (
        f"the nightly declares no `{TRIAGE_JOB}` job; found {sorted(jobs)}. Without "
        "it the night scans, publishes findings nothing reads, and hands off nothing"
    )
    return jobs[TRIAGE_JOB]


@pytest.fixture(scope="module")
def triage_steps(triage_job) -> list[dict]:
    """The triage job's shell steps, in declaration order."""
    return [step for step in triage_job["steps"] if step.get("run")]


@pytest.fixture(scope="module")
def triage_bodies(triage_steps) -> list[str]:
    return [step["run"] for step in triage_steps]


class FakeGh:
    """Records every `gh` call and replays the responses the filing path needs.

    A thin restatement of U9's fake, kept local because what this suite needs
    from it is narrow: the umbrella must resolve so the productive path reaches
    the marker write, and nothing must reach the network.
    """

    def __init__(self, *, issues=(), next_number=9100):
        self.issues = list(issues)
        self.calls: list[list[str]] = []
        self._next = next_number

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        if args[:2] == ["issue", "create"]:
            number = self._next
            self._next += 1
            self.issues.append(
                {"number": number, "title": args[args.index("--title") + 1]}
            )
            return 0, f"https://github.com/{REPO}/issues/{number}\n", ""
        if args[:2] == ["api", "graphql"]:
            return 0, json.dumps({"data": {}}), ""
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(self.issues), ""
        if args[:2] == ["label", "list"]:
            return 0, json.dumps([{"name": "story"}]), ""
        return 0, "[]", ""


@pytest.fixture
def fake_gh(monkeypatch):
    def _install():
        gh = FakeGh(issues=[{"number": UMBRELLA_NUM, "title": ue.UMBRELLA_TITLE}])
        monkeypatch.setattr(ue, "_gh", gh)
        return gh

    return _install


def _steps_invoking(bodies: list[str], *fragments: str) -> list[int]:
    """Indices of the steps whose body contains every fragment."""
    return [
        i for i, body in enumerate(bodies) if all(f in body for f in fragments)
    ]


def _one_step(bodies: list[str], *fragments: str) -> str:
    matches = _steps_invoking(bodies, *fragments)
    assert len(matches) == 1, (
        f"expected exactly one triage step invoking {fragments}; found {len(matches)}"
    )
    return bodies[matches[0]]


# --------------------------------------------------------------------------
# the stage exists, and it is its own job
# --------------------------------------------------------------------------


def test_triage_runs_between_the_scan_and_the_handoff(jobs, triage_job):
    """The hole this issue closes, as a dependency assertion.

    `deliver` must depend on `triage`, not merely follow it in the file: without
    the edge it races the stage that writes the markers it joins on, syncs an
    empty prefix, and -- with the temporary guard now gone -- hard-fails the
    barrier as STALLED, blaming a triage pass that had not yet run.
    """
    # `needs` may be a string or a list, and triage legitimately gained a second
    # dependency (#4792's `scan_gate`, which decides whether this scan file has
    # already been turned into issues). The invariant is that the EDGE to the scan
    # exists, not that it is the only one -- a stricter assertion would fail any
    # future gate for doing exactly what it was asked to do.
    triage_needs = triage_job["needs"]
    triage_needs = [triage_needs] if isinstance(triage_needs, str) else triage_needs
    assert "code-review" in triage_needs, (
        "triage consumes the findings the scan published, so it must depend on it. "
        f"Found {triage_job['needs']!r}"
    )
    assert jobs["deliver"]["needs"] == ["code-review", "triage"], (
        "deliver must depend on BOTH: `code-review` for the run date it consumes "
        "and `triage` for the markers its barrier joins on. Found "
        f"{jobs['deliver']['needs']!r}"
    )


def test_triage_may_file_issues_and_the_scanner_still_may_not(jobs, triage_job):
    """Why this is a separate job rather than the scanner's tail.

    Filing the EPIC and its stories needs `issues: write`. Granting that to the
    metered, 5.5-hour, service-role-adjacent `code-review` job would widen the
    blast radius of the one job in this file whose narrow blast radius is the
    unit's whole safety story.
    """
    assert triage_job["permissions"]["issues"] == "write", (
        "triage files the dated EPIC and its stories; read-only cannot create them"
    )
    assert "issues" not in (jobs["code-review"].get("permissions") or {}), (
        "the metered scanner job must not gain `issues: write` as a side effect of "
        "wiring triage -- that is the reason triage is its own job"
    )


def test_triage_does_not_repeat_the_securityagent_provisioning(triage_bodies):
    """`code-review` repeats the provision/preflight pair because it constructs a
    `securityagent` client. No step in triage touches that service, so
    provisioning here would install a package for nothing and add minutes to
    every night for no property gained."""
    assert _steps_invoking(triage_bodies, "securityagent_preflight.py") == [], (
        "triage provisions the Security Agent client it never constructs"
    )


# --------------------------------------------------------------------------
# the invocation set and its order
# --------------------------------------------------------------------------


def test_the_stage_calls_the_four_scripts_the_night_needs(triage_bodies):
    """Each one exactly once. Twice would dedup, file or author twice, and the
    filing step is the one that creates permanent GitHub state."""
    for script, subcommand in (
        ("dedup_security_findings.py", "diff"),
        ("ensure_umbrella_epic.py", "--repo"),
        ("author_grouping_plan.py", "--new-findings"),
        ("triage_group_findings.py", "validate"),
        ("triage_group_findings.py", "file"),
    ):
        matches = _steps_invoking(triage_bodies, script, subcommand)
        assert len(matches) == 1, (
            f"expected exactly one triage step invoking `{script} {subcommand}`; "
            f"found {len(matches)}"
        )


def test_the_stage_runs_in_the_only_order_that_works(triage_bodies):
    """Order, not just presence, and each edge has a distinct failure:

    * dedup before authoring -- the plan is authored FROM the dedup result
    * umbrella before filing -- `run_triage` hard-raises when it is absent, and
      only on a productive night, so the first night that finds something is
      where a missing umbrella would otherwise be discovered
    * validate before file -- a gate after the issues exist gates nothing
    """
    dedup = _steps_invoking(triage_bodies, "dedup_security_findings.py", "diff")[0]
    umbrella = _steps_invoking(triage_bodies, "ensure_umbrella_epic.py")[0]
    author = _steps_invoking(triage_bodies, "author_grouping_plan.py")[0]
    validate = _steps_invoking(triage_bodies, "triage_group_findings.py", "validate")[0]
    filing = _steps_invoking(triage_bodies, "triage_group_findings.py", "file")[0]

    assert dedup < author, "the plan is authored from the dedup result"
    assert umbrella < filing, (
        "`file` hard-raises on an absent umbrella, and only on a night that found "
        "something -- so the umbrella must be ensured before filing, not after"
    )
    assert author < validate < filing, (
        "the plan must be authored, then gated, then filed. A gate that runs after "
        "the issues exist cannot prevent anything"
    )


def test_the_stage_does_not_invoke_the_normalize_library(triage_bodies):
    """`normalize_security_findings.py` has no argparse, no main and no
    `__main__`: it is a library that `dedup ... diff` already calls internally.
    A step invoking it would exit 0 having done nothing -- a call that looks like
    a pipeline stage and is not one, which is the shape of defect this whole
    workflow's comments exist to prevent."""
    assert _steps_invoking(triage_bodies, "normalize_security_findings.py") == [], (
        "triage invokes the normalize LIBRARY as if it had a CLI; normalization "
        "happens inside `dedup_security_findings.py diff`"
    )


def test_the_stage_writes_no_orchestration_shard(triage_bodies):
    """`deliver`'s barrier writes `shard-orchestration.json` itself. A second
    writer here would produce a contradicting shard, and `planned_sequence` merges
    `single` in the ledger schema's `x-fields`, so a disagreement is an error
    rather than something reconciled."""
    for body in triage_bodies:
        assert "shard-orchestration" not in body, (
            "triage writes an orchestration shard; that shard is `deliver`'s and "
            "two writers for one `single`-merge stage is an error, not a merge"
        )
        assert "security_agent_ledger.py write" not in body, (
            "triage writes a shard through the generic ledger CLI, which puts the "
            "stage id in a workflow string -- the seam the derived filenames close"
        )


def test_the_stage_dispatches_nothing(triage_bodies):
    """Filing must not dispatch. An `@agent-` mention or an `agent-*` label
    dispatches at write time and bypasses the sequencing the root event exists to
    drive -- and the root is `deliver`'s single `put-events`, not a per-item hop
    from this job."""
    for body in triage_bodies:
        assert not re.search(r"@agent-", body), (
            "a triage step contains an `@agent-` mention, which dispatches an agent "
            "at creation time"
        )
        assert not re.search(r"--add-label\s+[\"']?agent-", body)
        assert "ops_dispatch.py" not in body, (
            "the night's one root dispatch belongs to `deliver`, after the plan is "
            "filed -- a second emit here would mint a second chain"
        )


# --------------------------------------------------------------------------
# validate is a HARD gate
# --------------------------------------------------------------------------


def test_the_gate_is_unconditional_and_has_no_fallback(triage_steps, triage_bodies):
    """The asymmetry that decides this: a failed night is recoverable by
    re-dispatch and costs one wasted scan, whereas issues filed under a dated EPIC
    with placeholder sections cannot be recalled. So the gate must not be
    conditional, and its failure must not be swallowed."""
    index = _steps_invoking(triage_bodies, "triage_group_findings.py", "validate")[0]
    assert "if" not in triage_steps[index], (
        "the plan gate is conditional; a gate that can be skipped is not a gate"
    )
    body = triage_bodies[index]
    assert "|| true" not in body and "continue-on-error" not in str(
        triage_steps[index]
    ), "the plan gate swallows its own failure"


def test_the_gate_lints_the_body_that_will_actually_be_filed(triage_bodies):
    """`--findings-uri`, `--source`, `--run-id`, `--run-date` and the plan are all
    rendered into each body, and `validate` lints the RENDERED body. Gating one
    body and filing a different one would make the gate decorative -- so the two
    invocations must agree argument for argument."""
    validate = _one_step(triage_bodies, "triage_group_findings.py", "validate")
    filing = _one_step(triage_bodies, "triage_group_findings.py", "file")

    for flag in (
        "--plan plan.json",
        "--new-findings new-findings.json",
        '--source "$SOURCE"',
        '--run-date "$RUN_DATE"',
        '--run-id "${{ github.run_id }}"',
    ):
        assert flag in validate, f"the gate is missing {flag!r}"
        assert flag in filing, f"the filing step is missing {flag!r}"

    def findings_uri(body: str) -> str:
        match = re.search(r"--findings-uri\s+\"([^\"]+)\"", body)
        assert match, f"no quoted --findings-uri in:\n{body}"
        return match.group(1)

    assert findings_uri(validate) == findings_uri(filing), (
        "the gate and the filing step render different findings URIs into the "
        "bodies, so the gate lints a body that is never filed"
    )
    assert findings_uri(filing).startswith("s3://"), (
        "the findings URI must be the private run prefix; it is rendered into "
        "every filed body as the pointer to the detail"
    )


def test_the_plan_s_finding_set_comes_from_the_dedup_output_not_the_raw_findings(
    triage_bodies,
):
    """The producer's COVERAGE input is the deduped document. Driving coverage from
    the raw findings would re-file every already-accepted finding as new -- nightly
    issue spam against the baseline the dedup step exists to honour.

    The raw document IS handed to the producer, deliberately, but only under
    `--raw-findings`: it is the sole artifact still holding the scanner's own
    account of each finding, which the per-work-item authoring call needs so its
    `validation` section can assert the defect is CLOSED rather than merely that
    new code runs. That cannot widen the night, because the detail projection is
    keyed by the deduped id list and returns nothing outside it
    (`test_the_detail_projection_takes_only_the_findings_of_this_night`, and
    end-to-end in `test_a_raw_document_holding_suppressed_findings_does_not_widen_the_night`).

    So the gate is on WHICH FLAG the raw document may arrive under, not on whether
    it appears at all.
    """
    author = _one_step(triage_bodies, "author_grouping_plan.py")
    assert "--new-findings new-findings.json" in author
    flags = re.findall(r"(--[\w-]+)\s+\S*code-review-findings\.json", author)
    assert set(flags) <= {"--raw-findings"}, (
        f"the raw findings document reaches the producer under {sorted(set(flags))}; "
        "it may only arrive as `--raw-findings` (detail), never as its findings input"
    )


# --------------------------------------------------------------------------
# the shard seams: derived names, never typed ones
# --------------------------------------------------------------------------


def test_the_stage_names_no_shard_filename(triage_bodies):
    """Both writers take a DIRECTORY and derive the filename from the stage id.
    A hand-typed path is free to disagree with the stage inside the file and to
    stop matching `deliver`'s glob, at which point the join silently has nothing
    to join -- and nothing errors."""
    for body in triage_bodies:
        assert "--ledger-fields" not in body, (
            "a triage step passes `--ledger-fields` (a file path). Both writers "
            "take `--ledger-dir` and derive the name from the stage id"
        )
        assert not re.search(r"shard-(triage|workflow)[.\w]*\.json", body), (
            "a triage step names a shard filename; the name is the writer's to "
            "derive from its stage id"
        )
    for script, subcommand in (
        ("dedup_security_findings.py", "diff"),
        ("triage_group_findings.py", "file"),
    ):
        body = _one_step(triage_bodies, script, subcommand)
        assert '--ledger-dir "$LEDGER_DIR"' in body, (
            f"`{script} {subcommand}` writes no shard, so the night records nothing"
        )
        assert "--generated-at" in body, (
            "the shard's timestamp is the caller's, so a re-run of a night rewrites "
            "a byte-identical shard rather than a differing one"
        )


def test_the_marker_the_writer_derives_is_the_one_deliver_joins_on(fake_gh, tmp_path):
    """The seam, EXECUTED. Both halves are asserted against the real writer's real
    output rather than against a flag string in the YAML:

    * the derived filename matches the glob `deliver` hands to `load_markers`
    * the shard passes `validate_shard`, which is what `load_markers` runs it
      through -- the assertion that would have caught #4616, where the writer
      exited 0 and the reader rejected the file one job later
    """
    fake_gh()
    ledger = tmp_path / "ledger"
    assert (
        tg.main(
            [
                "file",
                "--repo",
                REPO,
                "--plan",
                str(PLAN_FIXTURE),
                "--new-findings",
                str(FINDINGS_FIXTURE),
                "--source",
                "code-review",
                "--findings-uri",
                FINDINGS_URI,
                "--run-id",
                RUN_ID,
                "--run-date",
                RUN_DATE,
                "--ledger-dir",
                str(ledger),
                "--generated-at",
                GENERATED_AT,
            ]
        )
        == 0
    )

    written = sorted(path.name for path in ledger.iterdir())
    assert written == ["shard-triage.code-review.json"]
    # The glob is read out of the workflow rather than retyped, so a change to
    # either side fails here instead of silently diverging.
    glob = _deliver_marker_glob()
    assert Path(written[0]).match(glob), (
        f"the marker {written[0]!r} does not match `deliver`'s {glob!r}, so "
        "`joinable` never sees it and the night hands off nothing, silently"
    )

    shard = json.loads((ledger / written[0]).read_text(encoding="utf-8"))
    validate_shard(shard, load_schema())
    assert written[0] == SHARD_NAME_TEMPLATE.format(stage=shard["stage"])
    # And through the barrier's own reader, which is the code that actually
    # rejected the pre-#4616 bare marker.
    assert sorted(load_markers(ledger)) == ["code-review"]


def test_a_quiet_night_still_leaves_the_marker(fake_gh, tmp_path):
    """NT-5. "This scanner ran and found nothing" and "this scanner never
    signalled" are different facts, and the barrier tells them apart only by the
    marker's presence. Without it every common night would time out and hard-fail
    as a hung pass -- and with `--sources` now naming one scanner, a quiet night
    with no marker is a STALLED night."""
    fake_gh()
    ledger = tmp_path / "ledger"
    quiet_findings = tmp_path / "quiet-new-findings.json"
    quiet_findings.write_text(
        json.dumps({"run_date": RUN_DATE, "nothing_to_file": True, "new_findings": []}),
        encoding="utf-8",
    )
    quiet_plan = tmp_path / "quiet-plan.json"
    quiet_plan.write_text(
        json.dumps({"schema_version": "1", "source": "code-review", "groups": []}),
        encoding="utf-8",
    )

    assert (
        tg.main(
            [
                "file",
                "--repo",
                REPO,
                "--plan",
                str(quiet_plan),
                "--new-findings",
                str(quiet_findings),
                "--source",
                "code-review",
                "--findings-uri",
                FINDINGS_URI,
                "--run-id",
                RUN_ID,
                "--run-date",
                RUN_DATE,
                "--ledger-dir",
                str(ledger),
                "--generated-at",
                GENERATED_AT,
            ]
        )
        == 0
    )
    marker = ledger / "shard-triage.code-review.json"
    assert marker.is_file(), "a quiet night left no marker: the barrier will stall"
    shard = json.loads(marker.read_text(encoding="utf-8"))
    validate_shard(shard, load_schema())
    assert shard["fields"]["story_ids"] == [], "a quiet night filed something"
    assert "daily_epic" not in shard["fields"], (
        "a quiet night names a dated EPIC it never created"
    )


def test_the_filing_step_is_not_conditional(triage_steps, triage_bodies):
    """The marker is what the barrier joins on, and the filing step is what writes
    it -- on both the productive and the quiet path. A condition here is how a
    quiet night stops signalling."""
    index = _steps_invoking(triage_bodies, "triage_group_findings.py", "file")[0]
    assert "if" not in triage_steps[index], (
        "the filing step is conditional, so some nights write no marker at all"
    )


def test_the_shards_are_persisted_even_when_the_stage_fails(triage_steps):
    """A night that filed some issues and then failed must still leave its shards
    behind: they are what make the retry adopt the work already done rather than
    re-file it."""
    # The upload is the sync whose SOURCE is the ledger directory. Matching on the
    # directory alone would also match the download, whose destination it is.
    sync_up = [
        step
        for step in triage_steps
        if re.search(r'aws s3 sync\s+\\?\s*"\$LEDGER_DIR/"', step["run"])
    ]
    assert len(sync_up) == 1, "expected exactly one step syncing the shards up"
    assert sync_up[0].get("if") == "always()", (
        "the shard upload is not `if: always()`, so a later failure loses the "
        "record the retry's idempotency depends on"
    )


# --------------------------------------------------------------------------
# one clock read for the whole night
# --------------------------------------------------------------------------


def test_the_run_date_is_resolved_once_and_published(jobs):
    """Each independent `date -u` is another UTC-midnight straddle surface: a job
    crossing midnight addresses a prefix the review never wrote to and reads an
    empty night as a quiet one. One clock read, published as a job output, is the
    only shape where that cannot happen (#4609)."""
    assert jobs["code-review"]["outputs"]["run_date"], (
        "`code-review` publishes no run_date output, so the downstream jobs have "
        "nothing to consume and must call `date` themselves"
    )
    for name in (TRIAGE_JOB, "deliver"):
        run_date = jobs[name]["env"]["RUN_DATE"]
        assert "needs.code-review.outputs.run_date" in run_date, (
            f"job {name!r} derives RUN_DATE from {run_date!r} rather than consuming "
            "the one `code-review` resolved"
        )


def test_only_one_step_in_the_nightly_reads_the_date(workflow):
    """Asserted across the whole file, not just the new job: the property is "the
    night has one date", and a job-scoped check would pass while a later unit
    added a second `date -u +%Y-%m-%d` elsewhere.

    Timestamp reads (`%Y-%m-%dT%H:%M:%SZ`) are a different thing and are
    deliberately not counted -- each shard's `generated_at` is its writer's own,
    which is what makes a re-run rewrite a byte-identical shard.
    """
    date_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("run") and re.search(r"date -u \+%Y-%m-%d(?![T%])", step["run"])
    ]
    assert len(date_steps) == 1, (
        "expected exactly one step resolving the night's date; found "
        f"{[s.get('name') for s in date_steps]}"
    )


# --------------------------------------------------------------------------
# the delivery half, rewired
# --------------------------------------------------------------------------


def _deliver_job() -> dict:
    return yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))["jobs"][
        "deliver"
    ]


def _deliver_marker_glob() -> str:
    """The glob `deliver`'s barrier actually joins on, read out of its reader.

    Not retyped as a literal here: a hardcoded `shard-triage*.json` would keep
    passing after `load_markers` changed to look for something else, which is
    precisely the writer/reader drift this gate exists to catch. Before #4603 the
    workflow spelled the glob out in the `joinable` guard; that guard is gone, so
    the reader is now the only place it exists.
    """
    source = Path(__file__).parent.parent / "join_barrier.py"
    globs = set(re.findall(r"glob\(\"([^\"]+)\"\)", source.read_text(encoding="utf-8")))
    assert len(globs) == 1, (
        f"expected `join_barrier` to glob for markers in exactly one pattern; "
        f"found {sorted(globs)}"
    )
    return globs.pop()


def test_the_barrier_joins_only_on_the_scanners_this_night_runs():
    """`--sources` defaults to every source in the vocabulary, which includes
    `pentest` -- a scanner that is not merely absent tonight but unbuilt (U6/U7).
    Joining on it would wait out the full 1800s timeout and then ship a plan
    stamped PARTIAL blaming a scanner that was never going to signal, and record
    that as a permanent `unsignalled_sources` entry in the durable ledger."""
    bodies = [step["run"] for step in _deliver_job()["steps"] if step.get("run")]
    barrier = [body for body in bodies if "join_barrier.py" in body]
    assert len(barrier) == 1
    match = re.search(r"--sources\s+(\S+)", barrier[0])
    assert match, (
        "the barrier is called without `--sources`, so it joins on every source "
        "including the unbuilt pentest half and stalls out every night"
    )
    named = {source for source in match.group(1).split(",") if source}
    assert named == {"code-review"}, (
        f"the barrier joins on {sorted(named)}; only `code-review` runs tonight"
    )
    assert named <= set(tg.SOURCES), "a source outside the module's vocabulary"


def test_the_temporary_joinable_guard_is_gone():
    """#4603. The guard existed because nothing wrote the markers it looked for:
    the condition was unconditionally true and an unguarded barrier call would
    have failed every run. Triage writes them now and `deliver` needs it, so zero
    markers no longer means "triage is not wired" -- it means triage is wholly
    broken, which is exactly what the barrier's STALLED hard-fail is for. Keeping
    the guard would go on masking it."""
    deliver = _deliver_job()
    steps = deliver["steps"]
    assert not [step for step in steps if step.get("id") == "joinable"], (
        "the temporary `joinable` guard is still present; with triage wired it "
        "masks the all-markers-absent case the barrier exists to fail on"
    )
    for step in steps:
        assert "joinable" not in str(step.get("if") or ""), (
            f"step {step.get('name')!r} is still gated on the removed guard"
        )
    barrier = [
        step
        for step in steps
        if step.get("run") and "join_barrier.py" in step["run"]
    ]
    assert len(barrier) == 1
    assert "if" not in barrier[0], (
        "the barrier call is conditional; a night with no marker must fail loudly"
    )


def test_the_barrier_still_pipefails_and_still_writes_the_orchestration_shard():
    """Regression check on the parts of `deliver` this change did NOT set out to
    touch. Without `pipefail`, `tee` decides the step's exit status and a failed
    barrier reads as success -- after which the dispatch emits a root event naming
    an issue that was never filed."""
    barrier = [
        step["run"]
        for step in _deliver_job()["steps"]
        if step.get("run") and "join_barrier.py" in step["run"]
    ][0]
    assert "set -o pipefail" in barrier
    assert "| tee barrier.out" in barrier
    assert "--ledger-fields" in barrier, (
        "the orchestration shard is `deliver`'s to write, and the retry's "
        "idempotency depends on it"
    )


# --------------------------------------------------------------------------
# dormancy: adding a job must arm nothing
# --------------------------------------------------------------------------


def test_the_eventbridge_rule_is_still_disabled():
    """With the rule off, `deliver`'s emit matches nothing and starts no run. This
    wiring makes the pipeline functional, NOT armed: arming needs the
    `events:PutEvents` grant, a measured run duration and a non-colliding window,
    and each is a separate deliberate decision."""
    assert re.search(
        r"^enable_eventbridge_security_agent_rule\s*=\s*false\s*$",
        TFVARS.read_text(encoding="utf-8"),
        re.MULTILINE,
    ), "enable_eventbridge_security_agent_rule is no longer false"


def test_the_new_job_grants_no_put_events_and_assumes_no_role(triage_job):
    """Triage reaches S3, Bedrock and `gh` -- all on the ARC runner's ambient
    IRSA identity, as every other job in this file does. A
    `configure-aws-credentials` step or an `events:PutEvents` call here would be a
    new identity and a new transport in a job whose only job is to file issues."""
    for step in triage_job["steps"]:
        assert "configure-aws-credentials" not in (step.get("uses") or ""), (
            "triage assumes a role; every job in this file runs as the runner's "
            "ambient IRSA identity and the nightly service role is not assumable"
        )
        assert "put-events" not in (step.get("run") or ""), (
            "the night's ONE root event is `deliver`'s; a second emit here would "
            "mint a second chain at chain_depth=0"
        )


def test_the_new_job_pins_no_version_literal(triage_bodies):
    """The existing gate searches the whole workflow for a version literal: the
    botocore floor lives only in the validated profile, and a second copy here --
    or in a comment -- is how the next reader gets it wrong."""
    for body in triage_bodies:
        assert not re.search(r"\b1\.\d+\.\d+\b", body), (
            f"a triage step names a concrete version:\n{body}"
        )


# --------------------------------------------------------------------------
# this gate must actually run
# --------------------------------------------------------------------------


def test_this_suite_and_its_subject_are_pinned_into_script_tests():
    """A suite CI never runs makes every gate above decorative -- the same reason
    the workflow pins its files explicitly rather than globbing."""
    text = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_nightly_triage_wiring.py" in text, (
        "this suite is not in the Script Tests pytest list, so it never runs"
    )
    assert (
        text.count("- '.github/workflows/security-agent-nightly.yml'") == 2
    ), "the nightly must be in BOTH paths filters (push and pull_request)"
