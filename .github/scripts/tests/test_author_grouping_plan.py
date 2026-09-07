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
import threading
import time
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
    build_authoring_prompt,
    build_cluster_prompt,
    empty_plan,
    gate_plan,
    main,
    parse_clusters,
    parse_object,
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


# The two-phase split, mirrored in the fakes: phase 1 returns ONLY the clustering
# keys, phase 2 returns ONLY the authored ones. A fixture group is a whole work
# item, so these two projections are how one is turned back into the pair of
# responses the producer actually asks for.
def _cluster_response(groups) -> str:
    return json.dumps(
        {"clusters": [{k: g[k] for k in agp._CLUSTER_FIELDS} for g in groups]}
    )


def _item_response(group) -> str:
    return json.dumps({k: group[k] for k in agp._AUTHORED_FIELDS})


class TwoPhaseBedrock:
    """Answers both phases from a plan fixture, by reading the prompt.

    Ordering-independent by construction, which matters because phase 2 runs its
    per-work-item calls in PARALLEL: a canned list popped in order would be
    answering whichever thread happened to arrive first. A phase-2 prompt names
    its work item, so the response is looked up by title instead.

    `cluster_prelude` / `item_prelude` inject leading BAD responses to drive the
    correction loops of either phase.
    """

    _TITLE_RE = re.compile(r"- Fix: \*\*(.+?)\*\*")

    def __init__(self, groups, *, cluster_prelude=(), item_prelude=(), fence=False):
        self.groups = json.loads(json.dumps(list(groups)))
        self.by_title = {g["title"]: g for g in self.groups}
        self.cluster_prelude = list(cluster_prelude)
        self.item_prelude = list(item_prelude)
        self.fence = fence
        self.cluster_calls = 0
        self.item_calls = 0
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def _answer(self, prompt: str) -> str:
        if "## The work item" not in prompt:  # phase 1
            self.cluster_calls += 1
            if self.cluster_prelude:
                return self.cluster_prelude.pop(0)
            return _cluster_response(self.groups)
        self.item_calls += 1
        if self.item_prelude:
            return self.item_prelude.pop(0)
        title = self._TITLE_RE.search(prompt).group(1)
        return _item_response(self.by_title[title])

    def invoke_model(self, *, modelId, contentType, accept, body):  # noqa: N803
        prompt = json.loads(body)["messages"][0]["content"]
        with self._lock:
            self.prompts.append(prompt)
            text = self._answer(prompt)
        if self.fence:
            text = "```json\n" + text + "\n```"
        return {"body": _Body(json.dumps({"content": [{"text": text}]}))}


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
    fake = TwoPhaseBedrock(_fixture_groups())
    _patch_bedrock(monkeypatch, fake)

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    assert output.exists(), "a passing run must write the plan"
    assert fake.cluster_calls == 1, "one clustering call sees the whole night"
    assert fake.item_calls == len(_fixture_groups()), "one authoring call per work item"

    assert tg.main(_validate_argv(plan=output, new_findings=FINDINGS_FIXTURE)) == 0
    assert not no_github.calls


def test_authored_plan_carries_all_fifteen_fields_non_empty(tmp_path, monkeypatch, no_github):
    """Stated separately from the CLI test because it is the reason a mechanical
    rule was rejected: the fifteen fields are prose that reaches a permanent
    issue verbatim, so "present" is not enough -- each must be non-empty."""
    output = tmp_path / "plan.json"
    _patch_bedrock(monkeypatch, TwoPhaseBedrock(_fixture_groups()))

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))

    assert plan["groups"], "a productive night must propose work items"
    for group in plan["groups"]:
        assert set(group) == set(tg._REQUIRED_GROUP_FIELDS)
        for field in tg._REQUIRED_GROUP_FIELDS:
            assert group[field], f"{group['slug']}.{field} is empty"


def test_author_step_emits_the_grouping_stage_traceability_ledger(
    tmp_path, monkeypatch, no_github
):
    """With --traceability, a productive night writes the grouping stage of the
    ledger alongside the plan: finding -> cluster with a computed severity, and no
    issue numbers yet (the filing step folds those in)."""
    output = tmp_path / "plan.json"
    trace = tmp_path / "traceability.json"
    _patch_bedrock(monkeypatch, TwoPhaseBedrock(_fixture_groups()))

    assert (
        main(
            _author_argv(
                new_findings=FINDINGS_FIXTURE,
                output=output,
                extra=("--traceability", str(trace)),
            )
        )
        == 0
    )
    doc = json.loads(trace.read_text(encoding="utf-8"))
    assert doc["stage"] == "grouping"
    assert doc["findings_total"] == 12
    assert doc["groups_total"] == len(json.loads(output.read_text())["groups"])
    assert all(g["issue_number"] is None for g in doc["groups"])
    assert all(g["fix_status"] == "PLANNED" for g in doc["groups"])
    # Severity came from the findings' risk levels, and the CRITICAL finding lands
    # in a CRITICAL cluster.
    assert doc["findings_index"]["f-42dca300"]["severity"] == "CRITICAL"


def test_a_traceability_failure_leaves_no_plan_behind(tmp_path, monkeypatch, no_github):
    """The same fail-closed invariant the stale-plan unlink holds, extended to the
    ledger: anything that can fail while building it must fail before the plan is
    written, or the filing step picks up a plan from a step that exited non-zero.
    Driven with a risk level outside the severity vocabulary."""
    document = json.loads(FINDINGS_FIXTURE.read_text(encoding="utf-8"))
    document["new_findings"][0]["risk_level"] = "INFORMATIONAL"
    findings = tmp_path / "new-findings.json"
    findings.write_text(json.dumps(document), encoding="utf-8")

    output = tmp_path / "plan.json"
    trace = tmp_path / "traceability.json"
    _patch_bedrock(monkeypatch, TwoPhaseBedrock(_fixture_groups()))

    argv = _author_argv(
        new_findings=findings, output=output, extra=("--traceability", str(trace))
    )
    assert main(argv) == 1
    assert not output.exists(), "a failed author step left a plan for the filing step"
    assert not trace.exists()


def test_plan_identity_fields_are_stamped_not_taken_from_the_model(
    tmp_path, monkeypatch, no_github
):
    """`schema_version`, `source` and `run_date` come from this script.

    A model that mislabelled `source` would produce a plan `validate_plan`
    rejects for crossing two scanners -- so the model is never given the chance:
    only the clustering, and each work item's prose, are read out of its
    responses. Here it volunteers all three identity fields and they are ignored.
    """
    output = tmp_path / "plan.json"
    groups = _fixture_groups()
    volunteered = json.dumps(
        {
            "schema_version": "999",
            "source": "pentest",
            "run_date": "1999-01-01",
            "clusters": [{k: g[k] for k in agp._CLUSTER_FIELDS} for g in groups],
        }
    )
    _patch_bedrock(monkeypatch, TwoPhaseBedrock(groups, cluster_prelude=[volunteered]))

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["schema_version"] == tg.PLAN_SCHEMA_VERSION
    assert plan["source"] == SOURCE
    assert plan["run_date"] == RUN_DATE


def test_a_fenced_json_response_is_accepted(tmp_path, monkeypatch, no_github):
    """Models routinely wrap JSON in a fence. Rejecting that would fail nights
    over formatting rather than over grouping quality. Asserted on BOTH phases,
    since either response can arrive fenced."""
    output = tmp_path / "plan.json"
    _patch_bedrock(monkeypatch, TwoPhaseBedrock(_fixture_groups(), fence=True))
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


def _clusters_from(groups) -> list[dict]:
    return [{k: g[k] for k in agp._CLUSTER_FIELDS} for g in groups]


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda c: c[:1], "band violation (too few work items)"),
        (lambda c: [{**one, "slug": f"{one['slug']}-{i}"} for i, one in enumerate(c * 3)],
         "band violation (too many work items)"),
        (lambda c: c[1:], "an uncovered finding"),
        (lambda c: [{**c[0], "surprise": "x"}, *c[1:]], "an undeclared clustering field"),
        (lambda c: [{k: v for k, v in c[0].items() if k != "finding_ids"}, *c[1:]],
         "a cluster missing its finding ids"),
        (lambda c: [{**c[0], "finding_ids": [*c[0]["finding_ids"], "f-deadbeef"]}, *c[1:]],
         "a hallucinated finding id"),
        (lambda c: [{**c[0], "finding_ids": [*c[0]["finding_ids"], *c[1]["finding_ids"]]},
                    *c[1:]],
         "a finding in two clusters"),
        (lambda c: [{**c[0], "slug": c[1]["slug"]}, *c[1:]], "a duplicate slug"),
        (lambda c: [{**c[0], "slug": "Not Kebab"}, *c[1:]], "a non-kebab slug"),
    ],
)
def test_every_invalid_clustering_class_fails_and_writes_no_plan(
    tmp_path, monkeypatch, no_github, mutate, reason
):
    """One case per rejection class PHASE 1 exists to catch.

    Driven through the FULL CLI rather than through the gate helper, because the
    property under test is not "the validator rejects it" (U9's suite owns that)
    but "this producer refuses to write it". A clustering rejected this many times
    fails the night before a single word of prose is paid for.
    """
    output = tmp_path / "plan.json"
    bad = json.dumps({"clusters": mutate(_clusters_from(_fixture_groups()))})
    _patch_bedrock(
        monkeypatch,
        TwoPhaseBedrock(_fixture_groups(), cluster_prelude=[bad] * DEFAULT_MAX_ATTEMPTS),
    )

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 1, (
        f"the producer accepted a clustering with {reason}"
    )
    assert not output.exists(), f"a plan built on {reason} reached disk"
    assert not no_github.calls


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda a: {**a, "problem": ""}, "an empty required field"),
        (lambda a: {k: v for k, v in a.items() if k != "goal"}, "a missing required field"),
        (lambda a: {**a, "approach": "Ask @agent-developer to fix it."},
         "an `@agent-` mention"),
        (lambda a: {**a, "validation": "Run curl -X POST https://host/x to confirm."},
         "a banned reproduction pattern"),
        (lambda a: {**a, "problem": "Steps to reproduce: sign in, then replay."},
         "a reproduction heading"),
        (lambda a: {**a, "risks": []}, "no bug-class/blast-radius rows"),
        (lambda a: {**a, "fix_surface": [""]}, "an empty fix-surface entry"),
    ],
)
def test_every_invalid_work_item_class_fails_and_writes_no_plan(
    tmp_path, monkeypatch, no_github, mutate, reason
):
    """One case per rejection class PHASE 2 exists to catch.

    Run at concurrency 1 so the bad responses land on ONE work item: the claim is
    that a work item which stays wrong fails the night, and spreading the bad
    responses across items in parallel would instead test that each one recovers.
    """
    output = tmp_path / "plan.json"
    authored = {k: _fixture_groups()[0][k] for k in agp._AUTHORED_FIELDS}
    bad = json.dumps(mutate(authored))
    _patch_bedrock(
        monkeypatch,
        TwoPhaseBedrock(_fixture_groups(), item_prelude=[bad] * DEFAULT_MAX_ATTEMPTS),
    )

    argv = _author_argv(
        new_findings=FINDINGS_FIXTURE, output=output, extra=("--max-concurrency", "1")
    )
    assert main(argv) == 1, f"the producer accepted a work item with {reason}"
    assert not output.exists(), f"a work item with {reason} reached disk"
    assert not no_github.calls


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
    one_cluster = json.dumps({"clusters": _clusters_from(_fixture_groups()[:1])})
    fake = TwoPhaseBedrock(_fixture_groups(), cluster_prelude=[one_cluster])
    _patch_bedrock(monkeypatch, fake)

    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    assert fake.cluster_calls == 2, "the rejected clustering was re-prompted once"
    assert "Your previous attempt was REJECTED" in fake.prompts[1]
    # The ACTUAL rejection, quoted back verbatim: a one-cluster answer leaves eight
    # findings uncovered, and naming which ones is what makes the retry a
    # correction rather than a reroll.
    assert "are in no cluster" in fake.prompts[1]
    assert "f-23536a25" in fake.prompts[1]
    assert "REJECTED" not in fake.prompts[0]


def test_a_key_the_model_invented_is_dropped_not_a_rejection(tmp_path, monkeypatch, no_github):
    """A model that adds a key has still written the twelve fields correctly.

    Rejecting for it bought nothing -- the group is built from named fields, so an
    invented key could never reach a rendered body -- and it was the single largest
    rejection class on the first real whole-repo run (#4290 replay 34108572024).
    The key is projected away, the work item authors on the FIRST attempt, and the
    plan carries exactly the declared field set.
    """
    output = tmp_path / "plan.json"
    authored = {k: _fixture_groups()[0][k] for k in agp._AUTHORED_FIELDS}
    invented = json.dumps(
        {**authored, "motivation_note_placeholder_guard": "x", "confirmation": "ok"}
    )
    fake = TwoPhaseBedrock(_fixture_groups(), item_prelude=[invented])
    _patch_bedrock(monkeypatch, fake)

    argv = _author_argv(
        new_findings=FINDINGS_FIXTURE, output=output, extra=("--max-concurrency", "1")
    )
    assert main(argv) == 0
    assert fake.item_calls == len(_fixture_groups()), "an invented key cost a retry"
    for group in json.loads(output.read_text(encoding="utf-8"))["groups"]:
        assert set(group) == set(tg._REQUIRED_GROUP_FIELDS)
        assert "confirmation" not in group


def test_finding_ids_volunteered_by_the_authoring_call_are_ignored_not_honoured(
    tmp_path, monkeypatch, no_github
):
    """The identity keys are stamped from phase 1. A prose call that volunteers its
    own `finding_ids` must not be able to change what the work item covers."""
    output = tmp_path / "plan.json"
    authored = {k: _fixture_groups()[0][k] for k in agp._AUTHORED_FIELDS}
    lying = json.dumps({**authored, "finding_ids": ["f-deadbeef"], "slug": "hijacked"})
    _patch_bedrock(
        monkeypatch, TwoPhaseBedrock(_fixture_groups(), item_prelude=[lying])
    )

    argv = _author_argv(
        new_findings=FINDINGS_FIXTURE, output=output, extra=("--max-concurrency", "1")
    )
    assert main(argv) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert "f-deadbeef" not in output.read_text(encoding="utf-8")
    assert "hijacked" not in [g["slug"] for g in plan["groups"]]
    covered = sorted(f for g in plan["groups"] for f in g["finding_ids"])
    assert covered == sorted(_findings()["finding_ids"])


@pytest.mark.parametrize(
    "trailer",
    [
        "\n\nI have written all twelve fields as requested.",
        "\nNote: the validation section avoids reproduction detail.",
    ],
    ids=["closing-sentence", "note"],
)
def test_a_response_that_keeps_talking_after_the_object_is_still_read(
    tmp_path, monkeypatch, no_github, trailer
):
    """"Extra data: line 1 column 3069" cost three work items on the first real
    whole-repo run. A model that answers correctly and then adds a courtesy
    sentence has not failed; failing the night on that is failing on manners."""
    output = tmp_path / "plan.json"
    authored = {k: _fixture_groups()[0][k] for k in agp._AUTHORED_FIELDS}
    fake = TwoPhaseBedrock(
        _fixture_groups(), item_prelude=[json.dumps(authored) + trailer]
    )
    _patch_bedrock(monkeypatch, fake)

    argv = _author_argv(
        new_findings=FINDINGS_FIXTURE, output=output, extra=("--max-concurrency", "1")
    )
    assert main(argv) == 0
    assert fake.item_calls == len(_fixture_groups()), "a trailing sentence cost a retry"


def test_a_clustering_response_that_keeps_talking_is_still_read(
    tmp_path, monkeypatch, no_github
):
    chatty = _cluster_response(_fixture_groups()) + "\n\nThat is the grouping."
    fake = TwoPhaseBedrock(_fixture_groups(), cluster_prelude=[chatty])
    _patch_bedrock(monkeypatch, fake)
    output = tmp_path / "plan.json"
    assert main(_author_argv(new_findings=FINDINGS_FIXTURE, output=output)) == 0
    assert fake.cluster_calls == 1


@pytest.mark.parametrize(
    "response",
    ["no object here at all", "{ unterminated", "prose { not: json } more"],
    ids=["no-braces", "unterminated", "malformed-object"],
)
def test_a_response_with_no_readable_object_still_fails(response):
    """The tolerance is for trailing chatter, not for unreadable answers."""
    with pytest.raises(PlanAuthoringError, match="did not return parseable JSON"):
        parse_clusters(response)


def test_the_object_extractor_survives_braces_and_quotes_inside_prose():
    """Every field is prose that can contain braces and escaped quotes, so the
    extractor is brace-counted with string awareness rather than a regex."""
    tricky = {"problem": 'A body with {braces} and a \\"quoted\\" phrase.', "goal": "}"}
    text = json.dumps(tricky) + "\n\nDone."
    assert parse_object(text) == tricky


def test_a_rejected_work_item_is_retried_with_its_own_rejection_reason(
    tmp_path, monkeypatch, no_github
):
    """Phase 2 corrects per work item, and the correction names that item's own
    violation -- so one difficult item is re-prompted rather than the whole night
    being re-authored."""
    output = tmp_path / "plan.json"
    authored = {k: _fixture_groups()[0][k] for k in agp._AUTHORED_FIELDS}
    bad = json.dumps({**authored, "goal": ""})
    fake = TwoPhaseBedrock(_fixture_groups(), item_prelude=[bad])
    _patch_bedrock(monkeypatch, fake)

    argv = _author_argv(
        new_findings=FINDINGS_FIXTURE, output=output, extra=("--max-concurrency", "1")
    )
    assert main(argv) == 0
    assert fake.item_calls == len(_fixture_groups()) + 1, "exactly one item was re-prompted"
    retried = [p for p in fake.prompts if "Your previous attempt was REJECTED" in p]
    assert len(retried) == 1
    assert "goal" in retried[0]
    assert "## The work item" in retried[0], "the correction stayed a phase-2 prompt"


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


def _cluster_prompt():
    projected = prompt_findings(FINDINGS_FIXTURE, _findings()["finding_ids"], SOURCE)
    return projected, build_cluster_prompt(projected, source=SOURCE, run_date=RUN_DATE)


def _authoring_prompt():
    groups = _fixture_groups()
    cluster = {k: groups[0][k] for k in agp._CLUSTER_FIELDS}
    findings = prompt_findings(FINDINGS_FIXTURE, cluster["finding_ids"], SOURCE)
    return cluster, build_authoring_prompt(
        cluster, findings, source=SOURCE, run_date=RUN_DATE
    )


def test_the_clustering_prompt_states_the_band_and_sees_every_finding():
    """Phase 1's whole advantage is seeing the entire night at once, so the
    findings must all be in it -- and the band it will be judged against has to be
    stated, or the first attempt is a guess."""
    projected, prompt = _cluster_prompt()
    low, high = tg.group_count_band(len(projected))
    assert f"between {low} and {high} clusters" in prompt
    for finding in projected:
        assert finding["finding_id"] in prompt
    for field in agp._CLUSTER_FIELDS:
        assert f"`{field}`" in prompt
    assert "No reproduction detail" in prompt
    assert "`@agent-` mention" in prompt


def test_the_clustering_prompt_asks_for_no_prose():
    """If phase 1 asked for prose it would be the single overflowing call again.
    Asserted by the absence of the authored field names."""
    _, prompt = _cluster_prompt()
    for field in ("problem", "motivation", "who_benefits", "deployment", "validation"):
        assert f"`{field}`" not in prompt


def test_the_authoring_prompt_states_every_authored_field_and_no_identity_field():
    """Phase 2 is asked for exactly the fields it owns. The three phase-1 keys are
    explicitly NOT requested -- that is what makes dropping `finding_ids`
    impossible rather than merely unlikely."""
    cluster, prompt = _authoring_prompt()
    for field in agp._AUTHORED_FIELDS:
        assert f"`{field}`" in prompt
    for field in agp._CLUSTER_FIELDS:
        assert f"`{field}`" not in prompt
    # It does carry the decided work item, so the model knows what it is writing.
    assert cluster["title"] in prompt
    for finding_id in cluster["finding_ids"]:
        assert finding_id in prompt


def test_the_authoring_prompt_demands_plain_language_over_code_coordinates():
    """The fifteen sections land verbatim in an issue that non-engineers read
    first, so the template has to say so -- CLAUDE.md's plain-terms convention."""
    _, prompt = _authoring_prompt()
    assert "never seen this codebase" in prompt
    assert "No file paths, no function names." in prompt
    assert "this is the template, follow it" in prompt


def test_the_split_between_the_phases_covers_every_required_field():
    """What phase 1 decides plus what phase 2 writes IS U9's required field set.
    A field added downstream must land in one of the two prompts, never in
    neither -- which would make every night fail on a missing field."""
    assert set(agp._CLUSTER_FIELDS) | set(agp._AUTHORED_FIELDS) == set(
        tg._REQUIRED_GROUP_FIELDS
    )
    assert not set(agp._CLUSTER_FIELDS) & set(agp._AUTHORED_FIELDS)


@pytest.mark.parametrize("prompt_of", [_cluster_prompt, _authoring_prompt])
def test_neither_prompt_s_own_instructions_match_a_banned_pattern(patterns, prompt_of):
    """Each prompt names the prohibitions, and naming them must not itself trip
    them -- otherwise the instruction that prevents a violation could not be
    written down.

    Scoped to the prompts' own instructions: with `--raw-findings` the authoring
    prompt deliberately CARRIES reproduction detail (that is the point of
    `_DETAIL_FINDING_FIELDS`), so the invariant that holds is about what this
    module writes, not about what the scanner said. The banned-pattern list guards
    the rendered BODY, which is the artifact that becomes permanent.
    """
    _, prompt = prompt_of()
    assert tg.banned_pattern_hits(prompt, patterns) == []


# --------------------------------------------------------------------------
# the scanner's own account: read by phase 2, never by phase 1
# --------------------------------------------------------------------------


def _raw_findings(tmp_path: Path, finding_ids, *, attack="Attack: replay the signed body.") -> Path:
    """A raw scanner document -- the shape `code_review_request.write_findings`
    writes and the private bucket holds, with the two detail fields present."""
    path = tmp_path / "code-review-findings.json"
    path.write_text(
        json.dumps(
            {
                "runDate": RUN_DATE,
                "codeReviewJobId": "cj-1",
                "findingCount": len(finding_ids),
                "findings": [
                    {
                        "findingId": fid,
                        "name": f"Raw title {i}",
                        "riskLevel": "HIGH",
                        "riskType": "AUTHORIZATION",
                        "description": f"The gate for {fid} is enforced on one path only.",
                        "attackScript": attack,
                        # An open-set field that must NOT be projected.
                        "reasoning": "SECRET-REASONING-DO-NOT-LEAK",
                    }
                    for i, fid in enumerate(finding_ids)
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_detail_projection_is_an_allow_list_of_exactly_two_fields(tmp_path):
    """The raw document is the one artifact still holding everything the service
    said, so a deny-list here would hand the model whatever field it adds next."""
    ids = _findings()["finding_ids"]
    details = agp.detail_by_finding(_raw_findings(tmp_path, ids), ids)
    assert set(details) == set(ids)
    for detail in details.values():
        assert set(detail) == set(agp._DETAIL_FINDING_FIELDS)
    assert "SECRET-REASONING-DO-NOT-LEAK" not in json.dumps(details)


def test_the_detail_projection_takes_only_the_findings_of_this_night(tmp_path):
    ids = _findings()["finding_ids"]
    raw = _raw_findings(tmp_path, [*ids, "f-notmine"])
    assert "f-notmine" not in agp.detail_by_finding(raw, ids)


def test_a_finding_with_no_detail_is_simply_absent_not_an_error(tmp_path):
    """The service does not always populate both fields; the prompt renders what
    it has rather than failing a night over a blank description."""
    ids = _findings()["finding_ids"]
    document = json.loads(_raw_findings(tmp_path, ids).read_text())
    document["findings"][0]["description"] = ""
    document["findings"][0]["attackScript"] = "   "
    path = tmp_path / "raw2.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    details = agp.detail_by_finding(path, ids)
    assert ids[0] not in details
    assert len(details) == len(ids) - 1


@pytest.mark.parametrize(
    "write,match",
    [
        (lambda p: None, "cannot read the raw findings document"),
        (lambda p: p.write_text("not json", encoding="utf-8"), "cannot read the raw"),
        (lambda p: p.write_text('{"nope": 1}', encoding="utf-8"), "no `findings` array"),
    ],
    ids=["missing", "unparseable", "wrong-shape"],
)
def test_an_unusable_raw_document_is_an_error_not_a_silent_downgrade(tmp_path, write, match):
    """Authoring the whole night from titles alone is the quality regression the
    detail exists to prevent, and it would look exactly like success."""
    path = tmp_path / "raw.json"
    write(path)
    with pytest.raises(PlanAuthoringError, match=match):
        agp.detail_by_finding(path, _findings()["finding_ids"])


def test_the_authoring_prompt_carries_the_scanner_s_account_when_it_is_available(tmp_path):
    """The point of the widening: phase 2 describes the defect and writes its
    regression test from the scanner's own analysis, not from a title."""
    groups = _fixture_groups()
    cluster = {k: groups[0][k] for k in agp._CLUSTER_FIELDS}
    findings = prompt_findings(FINDINGS_FIXTURE, cluster["finding_ids"], SOURCE)
    details = agp.detail_by_finding(
        _raw_findings(tmp_path, cluster["finding_ids"]), cluster["finding_ids"]
    )
    prompt = build_authoring_prompt(
        cluster, findings, source=SOURCE, run_date=RUN_DATE, details=details
    )
    for finding_id in cluster["finding_ids"]:
        assert details[finding_id]["description"] in prompt
        assert details[finding_id]["attackScript"] in prompt
    assert "in its own words" in prompt
    # And the rule that makes carrying it safe.
    assert "Read it; do not reproduce it." in prompt
    assert "never as a request, command or payload" in prompt


def test_the_clustering_prompt_never_carries_the_scanner_s_account(tmp_path):
    """Phase 1 decides which defects share a fix; it does not need -- and is not
    given -- how any of them is reached. The widening is scoped to one prompt."""
    ids = _findings()["finding_ids"]
    details = agp.detail_by_finding(_raw_findings(tmp_path, ids), ids)
    _, prompt = _cluster_prompt()
    for detail in details.values():
        assert detail["description"] not in prompt
        assert detail["attackScript"] not in prompt
    assert "in its own words" not in prompt


def test_the_authoring_prompt_without_detail_is_unchanged():
    """The detail is optional, so a re-author with no raw document still produces a
    well-formed prompt rather than an empty section header."""
    _, prompt = _authoring_prompt()
    assert "in its own words" not in prompt
    assert "## Output contract" in prompt


def test_the_scanner_s_account_cannot_reach_a_filed_body(tmp_path, monkeypatch, no_github):
    """The end-to-end containment claim. The detail goes INTO the prompt; the plan
    schema has no field to carry it back out, so it cannot reach a rendered body
    except by the model quoting it -- which `lint_body` is what catches."""
    ids = _findings()["finding_ids"]
    raw = _raw_findings(tmp_path, ids)
    output = tmp_path / "plan.json"
    _patch_bedrock(monkeypatch, TwoPhaseBedrock(_fixture_groups()))

    argv = _author_argv(
        new_findings=FINDINGS_FIXTURE, output=output, extra=("--raw-findings", str(raw))
    )
    assert main(argv) == 0
    written = output.read_text(encoding="utf-8")
    assert "attackScript" not in written and "SECRET-REASONING" not in written
    for group in json.loads(written)["groups"]:
        assert set(group) == set(tg._REQUIRED_GROUP_FIELDS)


def test_a_raw_document_holding_suppressed_findings_does_not_widen_the_night(
    tmp_path, monkeypatch, no_github
):
    """The regression the nightly wiring gate protects, asserted end to end.

    The raw document holds EVERY finding the scan produced, including the ones the
    baseline already accepted; the deduped document holds only tonight's new ones.
    Reading detail from the former must not turn a suppressed finding back into
    work -- that would be nightly issue spam against the baseline dedup exists to
    honour. Here the raw document carries eight extra ids and the plan covers
    exactly the twelve the dedup output named.
    """
    ids = _findings()["finding_ids"]
    suppressed = [f"f-{i:08x}" for i in range(8)]
    raw = _raw_findings(tmp_path, [*ids, *suppressed])
    output = tmp_path / "plan.json"
    _patch_bedrock(monkeypatch, TwoPhaseBedrock(_fixture_groups()))

    argv = _author_argv(
        new_findings=FINDINGS_FIXTURE, output=output, extra=("--raw-findings", str(raw))
    )
    assert main(argv) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    covered = sorted(f for g in plan["groups"] for f in g["finding_ids"])
    assert covered == sorted(ids), "the plan's coverage came from the dedup output"
    for gone in suppressed:
        assert gone not in output.read_text(encoding="utf-8")


def test_a_body_that_echoes_the_attack_fails_the_night(tmp_path, monkeypatch, no_github):
    """The backstop, asserted rather than assumed: now that the model READS the
    attack, the thing standing between it and a permanent issue is the
    banned-pattern lint on the rendered body. A work item whose validation quotes a
    request must fail the night, not file."""
    ids = _findings()["finding_ids"]
    raw = _raw_findings(tmp_path, ids)
    output = tmp_path / "plan.json"
    authored = {k: _fixture_groups()[0][k] for k in agp._AUTHORED_FIELDS}
    echoed = json.dumps(
        {**authored, "validation": "Confirm with curl -X POST https://host/hook -d @body.json"}
    )
    _patch_bedrock(
        monkeypatch,
        TwoPhaseBedrock(_fixture_groups(), item_prelude=[echoed] * DEFAULT_MAX_ATTEMPTS),
    )

    argv = _author_argv(
        new_findings=FINDINGS_FIXTURE,
        output=output,
        extra=("--raw-findings", str(raw), "--max-concurrency", "1"),
    )
    assert main(argv) == 1
    assert not output.exists(), "a body echoing the attack reached disk"


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
def test_a_response_that_is_not_a_clusters_object_is_rejected(response):
    """Parseable JSON is not a clustering. Each of these would otherwise reach the
    coverage check as a `clusters` of `None` and be rejected with a message about
    the plan's shape rather than about the model's response."""
    with pytest.raises(PlanAuthoringError):
        parse_clusters(response)


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


def test_the_producer_is_the_nightly_plan_author():
    """This gate was the inverse until #4613: while the wiring was unreviewed, a
    nightly that called this producer was an un-reviewed change to the pipeline's
    shape, so the gate asserted the call was ABSENT. #4613 reviewed and landed
    that wiring, so the same line is now the binding: it asserts the producer is
    the ONLY thing the nightly authors a plan with.

    Kept here rather than deleted because an unreferenced producer is this unit's
    real failure mode -- every gate above passes against a script nothing calls.
    The rest of the stage's shape is asserted by
    `tests/test_nightly_triage_wiring.py`.
    """
    assert "author_grouping_plan.py" in NIGHTLY_WORKFLOW.read_text(encoding="utf-8"), (
        "the nightly authors no plan, so this producer is dead code and every gate "
        "in this suite is decorative (#4613 wired it into the `triage` job)"
    )


# ==========================================================================
# 8. the whole-repo night -- two phases, one call per work item, in parallel
# ==========================================================================
#
# A single call cannot author a correct in-band plan for a whole-repo night: the
# response overflows (#4290 replay runs 34056793941/34059125104) and the fifteen-
# key group schema drifts under repetition (runs 34063284310/34063907234, which
# batching the findings did NOT fix). So the night is one clustering call over
# every finding, then one prose call per work item, in parallel. These tests
# assert the assembled plan is fileable, coverage is exact, the total lands in
# band, the identity keys come from the clustering rather than the prose call, and
# the concurrency cap is honoured.


def _synth_findings(tmp_path: Path, n: int, *, run_date: str = RUN_DATE) -> tuple[Path, list[str]]:
    """A dedup result with ``n`` genuinely-new findings, each a distinct id.

    Cloned from the real fixture finding so every field the producer reads is
    present and well-shaped; only the identity, title and risk type vary.
    """
    base = json.loads(FINDINGS_FIXTURE.read_text(encoding="utf-8"))["new_findings"][0]
    risk_types = ["AUTHORIZATION", "INJECTION", "SECRETS", "SSRF", "CRYPTO"]
    findings = []
    for i in range(n):
        finding = json.loads(json.dumps(base))
        finding["finding_id"] = f"f-{i:08x}"
        finding["fingerprint"] = f"{i:064x}"
        finding["title"] = f"Synthetic finding {i}"
        finding["title_signature"] = f"synthetic finding {i}"
        finding["risk_type"] = risk_types[i % len(risk_types)]
        finding["source"] = SOURCE
        findings.append(finding)
    path = tmp_path / "new-findings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_date": run_date,
                "nothing_to_file": False,
                "identified_raw": n,
                "identified_new_after_dedup": n,
                "dropped_by_status": 0,
                "suppressed_by_baseline": 0,
                "unmatched_baseline_fingerprints": [],
                "new_findings": findings,
            }
        ),
        encoding="utf-8",
    )
    return path, [f["finding_id"] for f in findings]


def _split_evenly(total: int, parts: int) -> list[int]:
    base, rem = divmod(total, parts)
    return [base + (1 if i < rem else 0) for i in range(parts)]


class _AutoTwoPhase:
    """Answers a night of ANY size by reading each prompt, not from a fixture.

    Phase 1: takes the finding ids and the band out of the clustering prompt and
    returns exactly `low` clusters covering them. Phase 2: returns the template's
    authored fields, so every prose field is non-empty and lint-clean.

    Also records how many phase-2 calls were in flight at once, which is how the
    concurrency claims below are asserted rather than assumed.
    """

    _ID_RE = re.compile(r'"finding_id": "(f-[0-9a-f]+)"')
    _BAND_RE = re.compile(r"between (\d+) and (\d+) clusters")

    def __init__(self, template: dict, *, delay: float = 0.0):
        self.authored = {k: template[k] for k in agp._AUTHORED_FIELDS}
        self.delay = delay
        self.cluster_calls = 0
        self.item_calls = 0
        self.max_inflight = 0
        self._inflight = 0
        self._lock = threading.Lock()

    def _clusters(self, prompt: str) -> str:
        ids = self._ID_RE.findall(prompt)
        low = int(self._BAND_RE.search(prompt).group(1))
        clusters, start = [], 0
        for i, size in enumerate(_split_evenly(len(ids), low)):
            clusters.append(
                {
                    "slug": f"grp-{i}",
                    "title": f"Fix cluster {i}",
                    "finding_ids": ids[start : start + size],
                }
            )
            start += size
        return json.dumps({"clusters": clusters})

    def invoke_model(self, *, modelId, contentType, accept, body):  # noqa: N803
        prompt = json.loads(body)["messages"][0]["content"]
        if "## The work item" not in prompt:
            with self._lock:
                self.cluster_calls += 1
            text = self._clusters(prompt)
            return {"body": _Body(json.dumps({"content": [{"text": text}]}))}
        with self._lock:
            self.item_calls += 1
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self.delay:
                time.sleep(self.delay)
            text = json.dumps(self.authored)
        finally:
            with self._lock:
                self._inflight -= 1
        return {"body": _Body(json.dumps({"content": [{"text": text}]}))}


def test_a_whole_repo_night_is_one_clustering_call_then_one_call_per_work_item(
    tmp_path, monkeypatch, no_github
):
    """40 findings: ONE call decides the grouping, then one call writes each work
    item. The assembled plan passes the REAL downstream gate -- coverage exact,
    total in band."""
    path, ids = _synth_findings(tmp_path, 40)
    output = tmp_path / "plan.json"
    fake = _AutoTwoPhase(_fixture_groups()[0])
    _patch_bedrock(monkeypatch, fake)

    assert main(_author_argv(new_findings=path, output=output)) == 0
    assert output.exists()
    plan = json.loads(output.read_text(encoding="utf-8"))

    assert fake.cluster_calls == 1, "the clustering sees the whole night in one call"
    assert fake.item_calls == len(plan["groups"]), "one authoring call per work item"

    # The headline claim: the assembled plan passes the same CLI the workflow gates on.
    assert tg.main(_validate_argv(plan=output, new_findings=path)) == 0

    covered = [fid for group in plan["groups"] for fid in group["finding_ids"]]
    assert sorted(covered) == sorted(ids), "every finding covered exactly once, none invented"
    low, high = tg.group_count_band(len(ids))
    assert low <= len(plan["groups"]) <= high, "the total lands in the calibrated band"
    assert not no_github.calls


def test_the_identity_of_a_work_item_comes_from_the_clustering_not_the_prose_call(
    tmp_path, monkeypatch, no_github
):
    """The regression this whole split exists for. The prose call is never asked
    for `finding_ids`, so a model that omits them -- as Opus 5 repeatedly did in
    runs 34063284310/34063907234 -- can no longer cost the night: the ids are
    stamped from phase 1. Here the prose responses carry NO identity keys at all
    and every group still has its slug, title and ids."""
    path, ids = _synth_findings(tmp_path, 24)
    output = tmp_path / "plan.json"
    _patch_bedrock(monkeypatch, _AutoTwoPhase(_fixture_groups()[0]))

    assert main(_author_argv(new_findings=path, output=output)) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    for group in plan["groups"]:
        assert set(group) == set(tg._REQUIRED_GROUP_FIELDS)
        assert group["slug"] and group["title"] and group["finding_ids"]
    assert sorted(f for g in plan["groups"] for f in g["finding_ids"]) == sorted(ids)


def test_the_work_items_are_written_in_parallel_up_to_the_cap(
    tmp_path, monkeypatch, no_github
):
    """The per-item calls are independent, so a night of dozens must not be dozens
    of serial round trips -- but it must not exceed the cap either, because the far
    side is a throttled service."""
    path, _ = _synth_findings(tmp_path, 40)
    output = tmp_path / "plan.json"
    fake = _AutoTwoPhase(_fixture_groups()[0], delay=0.02)
    _patch_bedrock(monkeypatch, fake)

    argv = _author_argv(
        new_findings=path, output=output, extra=("--max-concurrency", "4")
    )
    assert main(argv) == 0
    assert fake.max_inflight > 1, "the per-item calls ran serially, not in parallel"
    assert fake.max_inflight <= 4, "the concurrency cap was exceeded"


def test_concurrency_one_writes_the_work_items_serially(tmp_path, monkeypatch, no_github):
    """The escape hatch has to actually serialize -- it is what makes a throttled
    night recoverable by re-dispatch with a lower cap."""
    path, _ = _synth_findings(tmp_path, 24)
    output = tmp_path / "plan.json"
    fake = _AutoTwoPhase(_fixture_groups()[0], delay=0.005)
    _patch_bedrock(monkeypatch, fake)

    argv = _author_argv(
        new_findings=path, output=output, extra=("--max-concurrency", "1")
    )
    assert main(argv) == 0
    assert fake.max_inflight == 1


def test_a_clustering_that_stays_wrong_fails_before_any_prose_is_paid_for(
    tmp_path, monkeypatch, no_github
):
    """The cheap-failure claim: a night whose grouping cannot be agreed spends
    nothing on prose, and writes no plan."""
    path, _ = _synth_findings(tmp_path, 40)
    output = tmp_path / "plan.json"
    fake = TwoPhaseBedrock(
        _fixture_groups(), cluster_prelude=["nonsense"] * DEFAULT_MAX_ATTEMPTS
    )
    _patch_bedrock(monkeypatch, fake)

    assert main(_author_argv(new_findings=path, output=output)) == 1
    assert fake.item_calls == 0, "prose was written for a clustering that never passed"
    assert not output.exists(), "a failed night leaves no plan for the filing step"
    assert not no_github.calls


def test_a_work_item_that_stays_wrong_fails_the_whole_night_with_no_plan(
    tmp_path, monkeypatch, no_github
):
    """Fail-closed survives the split: the night is one plan, and a plan missing a
    work item is not fileable -- so a single unwritable item fails the night rather
    than shipping the rest."""
    path, _ = _synth_findings(tmp_path, 24)
    output = tmp_path / "plan.json"
    fake = _AutoTwoPhase(_fixture_groups()[0])
    fake.authored = {k: v for k, v in fake.authored.items() if k != "problem"}
    _patch_bedrock(monkeypatch, fake)

    assert main(_author_argv(new_findings=path, output=output)) == 1
    assert not output.exists()
    assert not no_github.calls


def test_zero_concurrency_is_rejected(patterns):
    """A configuration that would author nothing must fail rather than quietly
    behave like a skipped step -- the same reason zero attempts is rejected."""
    with pytest.raises(PlanAuthoringError, match="at least 1"):
        author_plan(
            TwoPhaseBedrock([]),
            _findings(),
            [],
            run_date=RUN_DATE,
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
            max_concurrency=0,
            patterns=patterns,
        )


# --------------------------------------------------------------------------
# tiny local helper (kept at the bottom: plumbing, not a gate)
# --------------------------------------------------------------------------


class _StubConfig:
    """Stands in for `botocore.config.Config`, recording what it was given."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _patch_bedrock(monkeypatch, fake):
    """Intercept the late `boto3.client("bedrock-runtime")` the CLI performs.

    Both `boto3` AND `botocore` are stubbed, and stubbed UNCONDITIONALLY rather
    than only when the real package is absent. The conditional version of this
    helper is what let a `from botocore.config import Config` in the CLI reach CI
    as a `ModuleNotFoundError` while passing on every laptop that happens to have
    botocore installed: whether this suite exercised the stub or the real package
    depended on the machine. Unconditional stubs make the path identical
    everywhere, which is the only version of "runs without AWS" worth asserting.

    `monkeypatch.setitem` unwinds after each test, so nothing here leaks into the
    suites that legitimately use the real boto3.
    """
    import types

    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = lambda *a, **k: fake
    monkeypatch.setitem(sys.modules, "boto3", boto3_stub)

    botocore_stub = types.ModuleType("botocore")
    config_stub = types.ModuleType("botocore.config")
    config_stub.Config = _StubConfig
    botocore_stub.config = config_stub
    monkeypatch.setitem(sys.modules, "botocore", botocore_stub)
    monkeypatch.setitem(sys.modules, "botocore.config", config_stub)
