"""Tests for the nightly grouping-plan producer (#4618, Ruling B).

Gate coverage, taken from the issue's `## Validation`:

* A seeded multi-finding night produces a plan that passes the REAL
  `triage_group_findings.py validate` CLI -- band satisfied, all fifteen group
  fields non-empty, no banned patterns, no `@agent-` mention. The assertion is
  made by running the downstream gate rather than by re-checking its rules here,
  because the claim being tested is "what this writes is fileable", and only that
  CLI decides that.
* A quiet night produces a valid EMPTY plan (`groups: []`) that also passes
  `validate` -- the path where `--plan` is still required and the easiest one to
  get wrong.
* No fallback: an absent input, a malformed model response, and every class of
  invalid plan (band violation, uncovered finding, undeclared field, `@agent-`
  mention, banned-pattern hit) exit non-zero and leave NO plan file behind.
* Zero GitHub calls on every path, asserted through the one seam every `gh`
  invocation in this pipeline goes through (`ensure_umbrella_epic._gh`), which is
  replaced by a fake that fails the test if it is called at all.

The model is injected everywhere below. No test in this file reaches AWS, and
`test_boto3_is_not_imported_at_module_scope` is what keeps that true for the
module itself.
"""

import ast
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import author_grouping_plan as agp
import ensure_umbrella_epic as ue
import triage_group_findings as tg
from author_grouping_plan import (
    DEFAULT_MAX_ATTEMPTS,
    PlanAuthoringError,
    author_plan,
    build_prompt,
    empty_plan,
    gate_plan,
    main,
    parse_groups,
    prompt_findings,
)

RUN_DATE = "2026-08-30"
RUN_ID = "99830451698"
FINDINGS_URI = "s3://adp-dev-security-scans-000000000000/security-agent/runs/2026-08-30/"
SOURCE = "code-review"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / ".github/scripts/author_grouping_plan.py"
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/script-tests.yml"
NIGHTLY_WORKFLOW = REPO_ROOT / ".github/workflows/security-agent-nightly.yml"

# The #3984 corpus U9's own suite is calibrated against: twelve real findings and
# the five-work-item plan a real triage produced from them. Reused rather than
# re-invented so this producer is measured against the same data the validator
# it feeds was calibrated on.
FIXTURES = Path(__file__).parent / "fixtures" / "triage-3984"
FINDINGS_FIXTURE = FIXTURES / "new-findings.json"
PLAN_FIXTURE = FIXTURES / "grouping-plan.json"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeBedrock:
    """Replays canned Bedrock `invoke_model` responses and records the prompts.

    `responses` are texts to return in order; a `BaseException` instance is
    raised instead of returned, which is how the "the call itself failed" path is
    driven. Running out of responses is a test bug, not a production path, so it
    fails loudly.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.model_ids: list[str] = []

    def invoke_model(self, *, modelId, contentType, accept, body):  # noqa: N803
        payload = json.loads(body)
        self.prompts.append(payload["messages"][0]["content"])
        self.model_ids.append(modelId)
        assert self.responses, "FakeBedrock ran out of canned responses"
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return {"body": _Body(json.dumps({"content": [{"text": response}]}))}


class _Body:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


class ForbiddenGh:
    """Stands in for `ensure_umbrella_epic._gh` and fails if anything calls it.

    The strongest available form of "this step touches no GitHub state": the
    claim is asserted by the absence of a call, not by inspecting arguments.
    """

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        raise AssertionError(f"a GitHub call was issued by the plan producer: {args}")


@pytest.fixture
def no_github(monkeypatch):
    forbidden = ForbiddenGh()
    monkeypatch.setattr(ue, "_gh", forbidden)
    return forbidden


@pytest.fixture
def patterns():
    return tg.load_banned_patterns()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _findings(source: str = SOURCE):
    return tg.load_new_findings(FINDINGS_FIXTURE, source)


def _fixture_groups():
    return json.loads(PLAN_FIXTURE.read_text(encoding="utf-8"))["groups"]


def _model_response(groups) -> str:
    return json.dumps({"groups": groups})


def _quiet_input(tmp_path: Path, run_date: str = RUN_DATE) -> Path:
    """A dedup result from a night where nothing survived the baseline.

    The explicit `nothing_to_file` flag with an empty `new_findings` list is the
    real shape `dedup_security_findings.build_result` emits on that night.
    """
    path = tmp_path / "new-findings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_date": run_date,
                "nothing_to_file": True,
                "identified_raw": 4,
                "identified_new_after_dedup": 0,
                "dropped_by_status": 1,
                "suppressed_by_baseline": 3,
                "unmatched_baseline_fingerprints": [],
                "new_findings": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _author_argv(*, new_findings: Path, output: Path, extra=()) -> list[str]:
    return [
        "--new-findings",
        str(new_findings),
        "--source",
        SOURCE,
        "--output",
        str(output),
        "--findings-uri",
        FINDINGS_URI,
        "--run-id",
        RUN_ID,
        *extra,
    ]


def _validate_argv(*, plan: Path, new_findings: Path) -> list[str]:
    return [
        "validate",
        "--plan",
        str(plan),
        "--new-findings",
        str(new_findings),
        "--source",
        SOURCE,
        "--findings-uri",
        FINDINGS_URI,
        "--run-id",
        RUN_ID,
    ]


# ==========================================================================
# 1. the productive night -- the plan this writes passes the downstream gate
# ==========================================================================


def test_authored_plan_passes_the_real_validate_cli(tmp_path, monkeypatch, no_github):
    """The headline claim, asserted end to end.

    The producer runs through its own CLI, then the file it wrote is handed to
    `triage_group_findings.py validate` -- the same command the workflow runs as
    its hard gate. Anything the gate would reject fails here.
    """
    output = tmp_path / "plan.json"
    fake = FakeBedrock([_model_response(_fixture_groups())])
    _patch_bedrock(monkeypatch, fake)

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    assert output.exists(), "a passing run must write the plan"

    assert tg.main(_validate_argv(plan=output, new_findings=FINDINGS_FIXTURE)) == 0
    assert not no_github.calls


def test_authored_plan_carries_all_fifteen_fields_non_empty(tmp_path, monkeypatch, no_github):
    """Stated separately from the CLI test because it is the reason a mechanical
    rule was rejected: the fifteen fields are prose that reaches a permanent
    issue verbatim, so "present" is not enough -- each must be non-empty."""
    output = tmp_path / "plan.json"
    _patch_bedrock(monkeypatch, FakeBedrock([_model_response(_fixture_groups())]))

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))

    assert plan["groups"], "a productive night must propose work items"
    for group in plan["groups"]:
        assert set(group) == set(tg._REQUIRED_GROUP_FIELDS)
        for field in tg._REQUIRED_GROUP_FIELDS:
            assert group[field], f"{group['slug']}.{field} is empty"


def test_plan_identity_fields_are_stamped_not_taken_from_the_model(
    tmp_path, monkeypatch, no_github
):
    """`schema_version`, `source` and `run_date` come from this script.

    A model that mislabelled `source` would produce a plan `validate_plan`
    rejects for crossing two scanners -- so the model is never given the chance:
    only `groups` is read out of its response.
    """
    output = tmp_path / "plan.json"
    groups = _fixture_groups()
    response = json.dumps(
        {
            "schema_version": "999",
            "source": "pentest",
            "run_date": "1999-01-01",
            "groups": groups,
        }
    )
    _patch_bedrock(monkeypatch, FakeBedrock([response]))

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["schema_version"] == tg.PLAN_SCHEMA_VERSION
    assert plan["source"] == SOURCE
    assert plan["run_date"] == RUN_DATE


def test_a_fenced_json_response_is_accepted(tmp_path, monkeypatch, no_github):
    """Models routinely wrap JSON in a fence. Rejecting that would fail nights
    over formatting rather than over grouping quality."""
    output = tmp_path / "plan.json"
    fenced = "```json\n" + _model_response(_fixture_groups()) + "\n```"
    _patch_bedrock(monkeypatch, FakeBedrock([fenced]))
    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0


# ==========================================================================
# 2. the quiet night -- a valid EMPTY plan, and no model call
# ==========================================================================


def test_quiet_night_writes_an_empty_plan_that_passes_validate(tmp_path, no_github):
    """`--plan` is required on the quiet path and `validate_plan` demands
    `groups: []` there, so the producer must emit a valid empty plan rather than
    skip. No Bedrock client is patched in: this path must not make a model call,
    and an attempt to would fail on the unprovisioned import."""
    quiet = _quiet_input(tmp_path)
    output = tmp_path / "plan.json"

    assert main(_author_argv(new_findings=quiet, output=output)) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["groups"] == []
    assert plan["source"] == SOURCE
    assert plan["run_date"] == RUN_DATE

    assert tg.main(_validate_argv(plan=output, new_findings=quiet)) == 0
    assert not no_github.calls


def test_quiet_night_makes_no_model_call(tmp_path, monkeypatch, no_github):
    """Asserted by the absence of an invocation: there is nothing to group on a
    quiet night, so an empty plan is not a judgment and must not be metered."""
    quiet = _quiet_input(tmp_path)
    fake = FakeBedrock([])
    _patch_bedrock(monkeypatch, fake)

    assert main(_author_argv(new_findings=quiet, output=tmp_path / "plan.json")) == 0
    assert fake.prompts == []


def test_a_night_whose_findings_are_all_another_scanners_is_quiet(tmp_path, no_github):
    """`load_new_findings` selects by source, so a code-review pass over a night
    that only produced pentest findings is quiet for this pass. It must still
    emit a valid empty plan rather than treat the input as broken."""
    path = tmp_path / "new-findings.json"
    document = json.loads(FINDINGS_FIXTURE.read_text(encoding="utf-8"))
    for finding in document["new_findings"]:
        finding["source"] = "pentest"
    path.write_text(json.dumps(document), encoding="utf-8")

    output = tmp_path / "plan.json"
    assert main(_author_argv(new_findings=path, output=output)) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["groups"] == []


# ==========================================================================
# 3. no fallback -- every failure is loud, writes no plan, and calls no GitHub
# ==========================================================================


def test_absent_input_fails_and_writes_no_plan(tmp_path, no_github, capsys):
    output = tmp_path / "plan.json"
    assert main(_author_argv(new_findings=tmp_path / "missing.json", output=output)) == 1
    assert not output.exists()
    assert "::error title=Security grouping plan::" in capsys.readouterr().err


def test_a_stale_plan_from_an_earlier_attempt_is_removed_before_failing(
    tmp_path, monkeypatch, no_github
):
    """The sharpest form of "no fallback".

    A plan file left by an earlier attempt (or written by hand) must not survive
    a failed run: the filing step consumes `--plan` by path, so a stale file that
    outlives the failure is filed as though it had passed the gate.
    """
    output = tmp_path / "plan.json"
    output.write_text(
        json.dumps(
            {
                "schema_version": tg.PLAN_SCHEMA_VERSION,
                "source": SOURCE,
                "run_date": RUN_DATE,
                "groups": _fixture_groups(),
            }
        ),
        encoding="utf-8",
    )
    _patch_bedrock(monkeypatch, FakeBedrock(["not json at all"] * DEFAULT_MAX_ATTEMPTS))

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 1
    assert not output.exists(), "a failed run left a plan behind for `file` to pick up"


def test_malformed_model_output_fails_loudly(tmp_path, monkeypatch, no_github):
    output = tmp_path / "plan.json"
    _patch_bedrock(
        monkeypatch, FakeBedrock(["I cannot help with that."] * DEFAULT_MAX_ATTEMPTS)
    )
    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 1
    assert not output.exists()


def test_a_failed_model_call_fails_loudly(tmp_path, monkeypatch, no_github):
    output = tmp_path / "plan.json"
    _patch_bedrock(
        monkeypatch,
        FakeBedrock([RuntimeError("ThrottlingException")] * DEFAULT_MAX_ATTEMPTS),
    )
    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda g: g[:1], "band violation (too few work items)"),
        (lambda g: [{**one, "slug": f"{one['slug']}-{i}"} for i, one in enumerate(g * 3)],
         "band violation (too many work items)"),
        (lambda g: g[1:], "an uncovered finding"),
        (lambda g: [{**g[0], "surprise": "x"}, *g[1:]], "an undeclared field"),
        (lambda g: [{**g[0], "problem": ""}, *g[1:]], "an empty required field"),
        (lambda g: [{k: v for k, v in g[0].items() if k != "goal"}, *g[1:]],
         "a missing required field"),
        (lambda g: [{**g[0], "approach": "Ask @agent-developer to fix it."}, *g[1:]],
         "an `@agent-` mention"),
        (lambda g: [{**g[0], "validation": "Run curl -X POST https://host/x to confirm."},
                    *g[1:]],
         "a banned reproduction pattern"),
        (lambda g: [{**g[0], "problem": "Steps to reproduce: sign in, then replay."},
                    *g[1:]],
         "a reproduction heading"),
    ],
)
def test_every_invalid_plan_class_fails_and_writes_no_plan(
    tmp_path, monkeypatch, no_github, mutate, reason
):
    """One case per rejection class the gate exists to catch.

    Each is driven through the FULL CLI rather than through `gate_plan`, because
    the property under test is not "the validator rejects it" (U9's suite owns
    that) but "this producer refuses to write it".
    """
    output = tmp_path / "plan.json"
    bad = _model_response(mutate(_fixture_groups()))
    _patch_bedrock(monkeypatch, FakeBedrock([bad] * DEFAULT_MAX_ATTEMPTS))

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 1, (
        f"the producer accepted a plan with {reason}"
    )
    assert not output.exists(), f"a plan with {reason} reached disk"
    assert not no_github.calls


def test_a_group_covering_a_finding_from_another_night_is_rejected(
    tmp_path, monkeypatch, no_github
):
    """A hallucinated id is the model failure mode with the worst downstream
    shape: it looks like coverage and is not."""
    groups = _fixture_groups()
    groups[0] = {**groups[0], "finding_ids": [*groups[0]["finding_ids"], "f-deadbeef"]}
    _patch_bedrock(monkeypatch, FakeBedrock([_model_response(groups)] * DEFAULT_MAX_ATTEMPTS))
    output = tmp_path / "plan.json"
    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 1
    assert not output.exists()


def test_an_unresolvable_run_date_fails_rather_than_guessing(tmp_path, no_github):
    """The rendered bodies and the dated parent are keyed by the run date, so a
    plan gated against a guessed date is gated against the wrong bodies."""
    path = tmp_path / "new-findings.json"
    document = json.loads(FINDINGS_FIXTURE.read_text(encoding="utf-8"))
    document["run_date"] = None
    path.write_text(json.dumps(document), encoding="utf-8")

    output = tmp_path / "plan.json"
    assert main(_author_argv(new_findings=path, output=output)) == 1
    assert not output.exists()


# ==========================================================================
# 4. the bounded retry -- corrections, not rerolls, and exhaustion fails
# ==========================================================================


def test_a_rejected_first_attempt_is_retried_with_the_rejection_reason(
    tmp_path, monkeypatch, no_github
):
    """The retry exists to correct a near-miss, so the rejection has to reach the
    model. A silent reroll would burn the same budget with no new information."""
    output = tmp_path / "plan.json"
    fake = FakeBedrock(
        [_model_response(_fixture_groups()[:1]), _model_response(_fixture_groups())]
    )
    _patch_bedrock(monkeypatch, fake)

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    assert len(fake.prompts) == 2
    assert "Your previous attempt was REJECTED" in fake.prompts[1]
    # The ACTUAL rejection, quoted back verbatim: a one-group plan leaves eight
    # findings uncovered, and naming which ones is what makes the retry a
    # correction rather than a reroll.
    assert "are in no group" in fake.prompts[1]
    assert "f-23536a25" in fake.prompts[1]
    assert "REJECTED" not in fake.prompts[0]


def test_attempts_are_bounded(tmp_path, monkeypatch, no_github):
    """Bounded rather than best-effort: an unbounded correction loop against a
    metered model is the unbounded-cost failure mode, and the night has to end."""
    output = tmp_path / "plan.json"
    fake = FakeBedrock(["nonsense"] * 5)
    _patch_bedrock(monkeypatch, fake)

    assert (
        main(
            _author_argv(
                new_findings=FINDINGS_FIXTURE,
                output=output,
                extra=("--max-attempts", "3"),
            )
        )
        == 1
    )
    assert len(fake.prompts) == 3
    assert not output.exists()


def test_exhaustion_is_a_hard_failure_not_a_default_plan(patterns, no_github):
    """Asserted at the function boundary too: `author_plan` has exactly two
    outcomes, a gated plan or an exception. There is no third return."""
    fake = FakeBedrock(["nonsense"] * DEFAULT_MAX_ATTEMPTS)
    with pytest.raises(PlanAuthoringError, match="no valid grouping plan after"):
        author_plan(
            fake,
            _findings(),
            prompt_findings(FINDINGS_FIXTURE, _findings()["finding_ids"], SOURCE),
            run_date=RUN_DATE,
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
            patterns=patterns,
        )


def test_zero_attempts_is_rejected(patterns):
    """A configuration that would author nothing and validate nothing must fail
    rather than quietly behave like a skipped step."""
    with pytest.raises(PlanAuthoringError, match="at least 1"):
        author_plan(
            FakeBedrock([]),
            _findings(),
            [],
            run_date=RUN_DATE,
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
            max_attempts=0,
            patterns=patterns,
        )


# ==========================================================================
# 5. the prompt -- bounded, and carrying no reproduction detail
# ==========================================================================


def test_the_prompt_carries_only_allow_listed_finding_fields():
    """An allow-list for the same reason `normalize_security_findings._FIELD_MAP`
    is one: the findings document comes from an open-set service schema, so a
    deny-list would pass through whatever field the service adds next."""
    projected = prompt_findings(FINDINGS_FIXTURE, _findings()["finding_ids"], SOURCE)
    assert projected
    for finding in projected:
        assert set(finding) <= set(agp._PROMPT_FINDING_FIELDS)


def test_the_prompt_states_the_band_and_the_closed_field_set():
    """The gate decides whether the night proceeds; telling the model the rules
    first is what keeps a productive night to one model call."""
    projected = prompt_findings(FINDINGS_FIXTURE, _findings()["finding_ids"], SOURCE)
    prompt = build_prompt(projected, source=SOURCE, run_date=RUN_DATE)

    low, high = tg.group_count_band(len(projected))
    assert f"between {low} and {high} groups" in prompt
    for field in tg._REQUIRED_GROUP_FIELDS:
        assert f"`{field}`" in prompt
    assert "No reproduction detail" in prompt
    assert "`@agent-` mention" in prompt


def test_the_prompt_matches_no_banned_pattern(patterns):
    """The prompt names the prohibitions, and naming them must not itself trip
    them -- otherwise the instruction that prevents a violation could not be
    written down."""
    projected = prompt_findings(FINDINGS_FIXTURE, _findings()["finding_ids"], SOURCE)
    prompt = build_prompt(projected, source=SOURCE, run_date=RUN_DATE)
    assert tg.banned_pattern_hits(prompt, patterns) == []


@pytest.mark.parametrize(
    "response",
    [
        "[]",
        '{"work_items": []}',
        '"a string"',
        "42",
    ],
    ids=["top-level-array", "wrong-key", "string", "number"],
)
def test_a_response_that_is_not_a_groups_object_is_rejected(response):
    """Parseable JSON is not a plan. Each of these would otherwise reach
    `validate_plan` as a `groups` of `None` and be rejected with a message about
    the plan's shape rather than about the model's response."""
    with pytest.raises(PlanAuthoringError):
        parse_groups(response)


def test_a_finding_the_input_cannot_describe_is_an_error_not_a_silent_drop():
    """A dropped finding would leave the plan structurally unable to cover it,
    which `validate_plan` would then report as an uncovered finding with no clue
    why. Failing at the boundary names the real cause."""
    with pytest.raises(PlanAuthoringError, match="not readable back out of it"):
        prompt_findings(FINDINGS_FIXTURE, ["f-42dca300", "f-notpresent"], SOURCE)


# ==========================================================================
# 6. the gate is U9's, not a second opinion
# ==========================================================================


def test_gate_runs_lint_body_over_every_rendered_group(patterns):
    """`validate_plan` alone is not the gate. It checks the plan; `lint_body`
    checks the BODY, which is what becomes permanent -- so a violation that only
    appears after rendering must still be caught."""
    groups = _fixture_groups()
    groups[0] = {**groups[0], "motivation": "Escalate to @agent-architect for review."}
    plan = {
        "schema_version": tg.PLAN_SCHEMA_VERSION,
        "source": SOURCE,
        "run_date": RUN_DATE,
        "groups": groups,
    }
    # The plan itself is structurally fine: it is the rendered body that is not.
    tg.validate_plan(plan, _findings()["finding_ids"], source=SOURCE)
    with pytest.raises(tg.TriageError, match="@agent-"):
        gate_plan(
            plan,
            _findings(),
            run_date=RUN_DATE,
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
            patterns=patterns,
        )


def test_the_empty_plan_is_gated_like_any_other(patterns):
    """The quiet path is the common path, so it must not be the one path with no
    check on it."""
    plan = empty_plan(source=SOURCE, run_date=RUN_DATE)
    quiet = {
        "run_date": RUN_DATE,
        "source": SOURCE,
        "nothing_to_file": True,
        "finding_ids": [],
        "unreferenceable": 0,
    }
    gate_plan(
        plan,
        quiet,
        run_date=RUN_DATE,
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        patterns=patterns,
    )
    # And a non-empty plan on a quiet night is rejected by that same gate.
    with pytest.raises(tg.TriageError, match="no new findings survived dedup"):
        gate_plan(
            {**plan, "groups": _fixture_groups()},
            quiet,
            run_date=RUN_DATE,
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
            patterns=patterns,
        )


def test_the_producer_does_not_restate_the_validation_rules():
    """Asserted by absence. A second copy of the band, the field list or the
    banned-pattern check here is a second answer to "is this plan fileable",
    which can disagree with the one that actually guards the filing step."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for borrowed in (
        "validate_plan",
        "lint_body",
        "render_body",
        "load_new_findings",
        "group_count_band",
        "load_banned_patterns",
        "_REQUIRED_GROUP_FIELDS",
    ):
        assert borrowed in source, f"{borrowed} should be reused from U9, not reimplemented"
    for reimplemented in ("MIN_FINDINGS_PER_GROUP =", "MAX_FINDINGS_PER_GROUP =", '"slug":'):
        assert reimplemented not in source, (
            f"{reimplemented} looks like a second copy of a U9 rule"
        )


# ==========================================================================
# 7. structural gates
# ==========================================================================


def test_boto3_is_not_imported_at_module_scope():
    """Same gate `assert_findings_bucket_private.py` carries: a module-scope
    import makes `--help` and every unit test fail on an unprovisioned machine,
    which is where these tests run."""
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name != "boto3" for alias in node.names)
        assert not (isinstance(node, ast.ImportFrom) and node.module == "boto3")


def test_the_producer_issues_no_github_call_at_all():
    """Asserted structurally as well as behaviourally: this step is deliberately
    NOT issue-bound, which is why it is not the architect workflow.

    The `gh` seam is reached only through `ensure_umbrella_epic._gh`, so the
    absence of that name -- plus the absence of any subprocess primitive to
    bypass it with -- is the whole claim.
    """
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for forbidden in ("_gh", "ensure_umbrella_epic", "subprocess", "issue create",
                      "adp-trigger"):
        assert forbidden not in source, f"{forbidden!r} appears in a step that files nothing"


def test_no_agent_mention_literal_in_the_producer_source():
    """Same absolute gate `ops_dispatch` carries: an `@agent-<name>` literal is a
    dispatch primitive, and the prompt that PROHIBITS mentions must state the
    prohibition without containing one."""
    offenders = [
        line
        for line in SCRIPT_PATH.read_text(encoding="utf-8").splitlines()
        if re.search(r"@agent-[a-z]", line, re.IGNORECASE)
    ]
    assert offenders == []


def test_this_suite_and_its_subject_are_pinned_into_script_tests():
    """An unpinned suite never runs in CI, which makes every gate above
    decorative."""
    text = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_author_grouping_plan.py" in text, (
        "the suite is not in the Script Tests pytest list, so it never runs"
    )
    assert text.count(".github/scripts/author_grouping_plan.py") == 2, (
        "the subject script must be in BOTH the push and pull_request path filters"
    )


def test_this_change_arms_nothing():
    """This unit is consumed by #4613's wiring and must not do the wiring. A
    nightly that calls this producer before the wiring PR has reviewed the whole
    stage is an un-reviewed change to the pipeline's shape."""
    assert "author_grouping_plan" not in NIGHTLY_WORKFLOW.read_text(encoding="utf-8"), (
        "wiring the producer into the nightly is #4613, not this issue"
    )


# --------------------------------------------------------------------------
# tiny local helper (kept at the bottom: plumbing, not a gate)
# --------------------------------------------------------------------------


def _patch_bedrock(monkeypatch, fake):
    """Intercept the late `boto3.client("bedrock-runtime")` the CLI performs.

    A stub module is installed when boto3 is genuinely absent, so this suite
    runs identically on a provisioned runner and an unprovisioned laptop.
    """
    import types

    module = sys.modules.get("boto3")
    if module is None:
        module = types.ModuleType("boto3")
        monkeypatch.setitem(sys.modules, "boto3", module)
    monkeypatch.setattr(module, "client", lambda *a, **k: fake, raising=False)
