"""Tests for the triage/grouping unit (#4448, U9).

Gate coverage, taken from the issue's `## Validation` and its impact table:

* The grouping ratio falls in the #3984-calibrated band, checked against #3984
  as a committed regression fixture -- so the band is validated against real
  data rather than against a number this test invented.
* One dated parent per date; a retry creates no second one.
* Section-presence lint on every generated body: all five headers present, the
  plain-terms opening first.
* No reproduction detail: bodies reference `f-<hex>` identifiers and match none
  of the committed banned-pattern list.
* No `@agent-` token and no `agent-*` label on any generated work item.
* Zero new findings => no parent, no work items, no dispatch.
* Persona asserts, mechanical rather than by judgment: `architect.md` carries an
  explicit issue-authoring authorization for this flow; it carries no surviving
  comment-only contradiction (the reconciling text is in the SAME section); and
  the mention/label dispatch prohibition is still present.

Every `gh` call goes through the one seam `ensure_umbrella_epic._gh`, which the
fake below replaces. That is what lets these tests assert a write was *not*
issued -- the strongest form of the idempotency and zero-findings claims.
"""

import fnmatch
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ensure_umbrella_epic as ue
import triage_group_findings as tg
from ensure_umbrella_epic import UMBRELLA_TITLE
from join_barrier import load_markers
from security_agent_ledger import (
    SHARD_NAME_TEMPLATE,
    build_shard,
    load_schema,
    validate_shard,
)
from triage_group_findings import (
    DAILY_EPIC_TITLE_TEMPLATE,
    MAX_FINDINGS_PER_GROUP,
    MIN_FINDINGS_PER_GROUP,
    REQUIRED_SECTIONS,
    TriageError,
    banned_pattern_hits,
    ensure_daily_epic,
    group_count_band,
    ledger_fields,
    lint_body,
    load_banned_patterns,
    load_new_findings,
    main,
    render_body,
    run_triage,
    story_labels,
    validate_plan,
)

REPO = "aws-e/adp"
UMBRELLA_NUM = 9001
RUN_DATE = "2026-08-30"
RUN_ID = "99830451698"
GENERATED_AT = "2026-08-30T03:10:00Z"
FINDINGS_URI = "s3://adp-dev-security-scans-000000000000/security-agent/runs/2026-08-30/"

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "triage-3984"
PLAN_FIXTURE = FIXTURES / "grouping-plan.json"
FINDINGS_FIXTURE = FIXTURES / "new-findings.json"
ARCHITECT_PERSONA = REPO_ROOT / "modules/agent-factory/rules/personas/architect.md"
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/script-tests.yml"
WORKER_DOCKERFILE = REPO_ROOT / "modules/agent-factory/agent-worker-image/Dockerfile"


# --------------------------------------------------------------------------
# the gh fake
# --------------------------------------------------------------------------


class FakeGh:
    """Records every `gh` invocation and replays canned responses.

    Modelled on `test_umbrella_epic_idempotency.FakeGh` rather than a second
    fake shape: this module reuses U3's `find_issue_by_exact_title` and
    `link_sub_issue`, so the responses those functions need are the same.
    """

    def __init__(self, *, issues=(), parent_of=None, next_number=9100, labels=()):
        self.issues = list(issues)
        self.parent_of = dict(parent_of or {})
        self.calls: list[list[str]] = []
        self.writes: list[list[str]] = []
        self.created: list[dict] = []
        # Labels that already exist in the repo. Empty by default, which is the
        # state a fresh org is in -- the case that makes ensuring them necessary.
        self.labels = set(labels)
        self.created_labels: list[str] = []
        self._next_number = next_number

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        joined = " ".join(args)

        # Label create, matched before the lookup below: the POST targets
        # `/labels` (no trailing name) and carries --method POST.
        if "--method" in args and "POST" in args and "/labels" in joined:
            self.writes.append(args)
            name = next(
                a.split("=", 1)[1] for a in args if a.startswith("name=")
            )
            self.labels.add(name)
            self.created_labels.append(name)
            return 0, "{}", ""

        # Label lookup: rc 0 when it exists, non-zero (404) when it does not.
        if "/labels/" in joined:
            name = joined.split("/labels/", 1)[1].split()[0]
            return (0, "{}", "") if name in self.labels else (1, "", "Not Found")

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
            # Mimic the search API's relevance matching: it returns anything
            # sharing words with the query, not just exact hits, which is why
            # the code filters to exact equality.
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


@pytest.fixture
def fake_gh(monkeypatch):
    def _install(**kwargs):
        issues = list(kwargs.pop("issues", []))
        issues.append({"number": UMBRELLA_NUM, "title": UMBRELLA_TITLE})
        gh = FakeGh(issues=issues, **kwargs)
        monkeypatch.setattr(ue, "_gh", gh)
        return gh

    return _install


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def load_fixture_plan() -> dict:
    return json.loads(PLAN_FIXTURE.read_text(encoding="utf-8"))


def load_fixture_findings(source="code-review") -> dict:
    return load_new_findings(FINDINGS_FIXTURE, source)


def a_group(**overrides) -> dict:
    """One minimal valid group. Every declared field populated, because every
    declared field is required -- an empty section is the code smell CLAUDE.md's
    enforcement note names."""
    group = {
        "slug": "example-surface",
        "title": "Example fix surface",
        "finding_ids": ["f-aaaa1111"],
        "problem": "Callers reach records they are not entitled to.",
        "fix_in_one_line": "Add the scope check.",
        "goal": "Scope the endpoint to its owning tenant.",
        "motivation": "The check is absent today.",
        "who_benefits": "every tenant",
        "who_is_impacted": "security, support",
        "risks": [{"bug_class": "check missed", "blast_radius": "path stays open"}],
        "cost_footprint": "code only",
        "approach": "Apply the existing scope-check pattern.",
        "fix_surface": ["modules/gateway/src/admin/routes.py"],
        "deployment": "Ships on merge; no apply.",
        "validation": "Unit test per route.",
    }
    group.update(overrides)
    return group


def a_plan(groups, source="code-review") -> dict:
    return {"schema_version": "1", "source": source, "groups": groups}


def render(group, **overrides) -> str:
    kwargs = {
        "run_date": RUN_DATE,
        "source": "code-review",
        "findings_uri": FINDINGS_URI,
        "run_id": RUN_ID,
    }
    kwargs.update(overrides)
    return render_body(group, **kwargs)


def file_all(gh_factory, *, plan=None, findings=None, source="code-review", **kwargs):
    gh = gh_factory(**kwargs)
    result = run_triage(
        REPO,
        plan=plan if plan is not None else load_fixture_plan(),
        new_findings=findings if findings is not None else load_fixture_findings(source),
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        run_date=RUN_DATE,
    )
    return gh, result


# --------------------------------------------------------------------------
# The calibrated grouping band, checked against #3984 as a regression fixture.
# --------------------------------------------------------------------------


def test_the_3984_fixture_is_the_real_shape_it_calibrates_against():
    """12 new findings in 5 groups, sizes 4/2/1/3/2 — #3984's actual answer. If
    this fixture drifts, every band assert below is calibrated against fiction."""
    findings = load_fixture_findings()
    plan = load_fixture_plan()
    assert len(findings["finding_ids"]) == 12
    assert len(plan["groups"]) == 5
    assert sorted(len(g["finding_ids"]) for g in plan["groups"]) == [1, 2, 2, 3, 4]


def test_the_3984_grouping_falls_inside_the_calibrated_band():
    findings = load_fixture_findings()
    low, high = group_count_band(len(findings["finding_ids"]))
    assert (low, high) == (4, 6), "the band for 12 findings is the issue's smoke criterion"
    assert low <= len(load_fixture_plan()["groups"]) <= high


def test_the_3984_plan_validates_end_to_end():
    validated = validate_plan(load_fixture_plan(), load_fixture_findings()["finding_ids"],
                              source="code-review")
    assert len(validated["covered"]) == 12


def test_one_work_item_per_finding_is_rejected():
    """The degenerate grouping: the flood U8 exists to prevent, arriving one
    layer later. Named explicitly in the issue's impact table."""
    ids = [f"f-{i:04x}" for i in range(12)]
    plan = a_plan([a_group(slug=f"g{i}", finding_ids=[fid]) for i, fid in enumerate(ids)])
    with pytest.raises(TriageError, match="outside the calibrated band"):
        validate_plan(plan, ids, source="code-review")


def test_lumping_a_whole_night_into_one_item_is_rejected():
    ids = [f"f-{i:04x}" for i in range(12)]
    with pytest.raises(TriageError, match="outside the calibrated band"):
        validate_plan(a_plan([a_group(finding_ids=ids)]), ids, source="code-review")


def test_the_band_is_derived_from_the_group_sizes_not_hardcoded():
    """The band and the two group-size constants must not be able to drift."""
    import math

    for n in (1, 2, 3, 7, 12, 40):
        assert group_count_band(n) == (
            math.ceil(n / MAX_FINDINGS_PER_GROUP),
            math.ceil(n / MIN_FINDINGS_PER_GROUP),
        )


def test_the_band_permits_the_3984_group_sizes_without_a_per_group_cap():
    """#3984's largest real group holds 4 findings, above MAX_FINDINGS_PER_GROUP.
    A per-group cap would reject the very fixture the band calibrates against —
    the band bounds the AVERAGE deliberately."""
    assert max(len(g["finding_ids"]) for g in load_fixture_plan()["groups"]) > MAX_FINDINGS_PER_GROUP


def test_the_band_for_no_findings_is_no_work_items():
    assert group_count_band(0) == (0, 0)


def test_a_single_finding_night_files_one_item():
    low, high = group_count_band(1)
    assert (low, high) == (1, 1)
    validate_plan(a_plan([a_group()]), ["f-aaaa1111"], source="code-review")


# --------------------------------------------------------------------------
# Coverage: no orphan, no duplicate.
# --------------------------------------------------------------------------


def test_a_finding_in_no_group_is_an_error_not_a_silent_drop():
    with pytest.raises(TriageError, match="in no group"):
        validate_plan(
            a_plan([a_group(finding_ids=["f-aaaa1111"])]),
            ["f-aaaa1111", "f-bbbb2222"],
            source="code-review",
        )


def test_a_finding_in_two_groups_is_an_error():
    with pytest.raises(TriageError, match="more than one group"):
        validate_plan(
            a_plan(
                [
                    a_group(slug="one", finding_ids=["f-aaaa1111", "f-bbbb2222"]),
                    a_group(slug="two", finding_ids=["f-aaaa1111", "f-cccc3333"]),
                ]
            ),
            ["f-aaaa1111", "f-bbbb2222", "f-cccc3333"],
            source="code-review",
        )


def test_a_group_referencing_a_finding_that_is_not_new_is_rejected():
    with pytest.raises(TriageError, match="not a new finding"):
        validate_plan(
            a_plan([a_group(finding_ids=["f-aaaa1111", "f-9999dead"])]),
            ["f-aaaa1111"],
            source="code-review",
        )


def test_a_group_covering_no_findings_is_rejected():
    """An empty list is caught by the required-field check; a bare string is the
    shape that reaches this branch — and it must not be iterated per character."""
    with pytest.raises(TriageError, match="covers no findings"):
        validate_plan(
            a_plan([a_group(finding_ids="f-aaaa1111")]), ["f-aaaa1111"], source="code-review"
        )


def test_a_non_service_finding_id_is_rejected():
    with pytest.raises(TriageError, match="service finding id"):
        validate_plan(
            a_plan([a_group(finding_ids=["CVE-2026-1"])]), ["f-aaaa1111"], source="code-review"
        )


def test_duplicate_slugs_are_rejected():
    with pytest.raises(TriageError, match="duplicate group slug"):
        validate_plan(
            a_plan([a_group(finding_ids=["f-aaaa1111"]), a_group(finding_ids=["f-bbbb2222"])]),
            ["f-aaaa1111", "f-bbbb2222"],
            source="code-review",
        )


def test_an_undeclared_plan_field_cannot_reach_a_body():
    """Allow-list, not deny-list: same reasoning as normalize's `_FIELD_MAP`."""
    group = a_group()
    group["exploit_steps"] = "..."
    with pytest.raises(TriageError, match="outside the schema"):
        validate_plan(a_plan([group]), ["f-aaaa1111"], source="code-review")


@pytest.mark.parametrize("field", sorted(tg._GROUP_FIELDS))
def test_every_declared_field_is_required(field):
    """An absent field would render an empty section, which the repo's
    enforcement note calls a code smell."""
    group = a_group()
    group[field] = "" if isinstance(group[field], str) else []
    with pytest.raises(TriageError):
        validate_plan(a_plan([group]), ["f-aaaa1111"], source="code-review")


def test_an_empty_fix_surface_entry_is_rejected():
    """A blank entry renders a bullet pointing at nothing — the one section a
    downstream implementer navigates by."""
    with pytest.raises(TriageError, match="empty fix_surface entry"):
        validate_plan(
            a_plan([a_group(fix_surface=["modules/gateway/src/x.py", "  "])]),
            ["f-aaaa1111"],
            source="code-review",
        )


def test_a_group_whose_risks_are_not_rows_is_rejected():
    with pytest.raises(TriageError, match="records no bug-class/blast-radius rows"):
        validate_plan(a_plan([a_group(risks="high")]), ["f-aaaa1111"], source="code-review")


def test_a_risk_row_missing_its_blast_radius_is_rejected():
    with pytest.raises(TriageError, match="risk row missing"):
        validate_plan(
            a_plan([a_group(risks=[{"bug_class": "x"}])]), ["f-aaaa1111"], source="code-review"
        )


def test_a_risk_row_with_undeclared_fields_is_rejected():
    with pytest.raises(TriageError, match="undeclared fields"):
        validate_plan(
            a_plan([a_group(risks=[{"bug_class": "x", "blast_radius": "y", "poc": "z"}])]),
            ["f-aaaa1111"],
            source="code-review",
        )


def test_a_non_kebab_slug_is_rejected():
    with pytest.raises(TriageError, match="kebab-case"):
        validate_plan(a_plan([a_group(slug="Not Kebab")]), ["f-aaaa1111"], source="code-review")


@pytest.mark.parametrize(
    "plan,match",
    [
        ("not an object", "must be an object"),
        ({"schema_version": "9", "source": "code-review", "groups": []}, "schema_version"),
        ({"schema_version": "1", "source": "pentest", "groups": []}, "must not be crossed"),
        ({"schema_version": "1", "source": "code-review", "groups": {}}, "must be an array"),
        ({"schema_version": "1", "source": "code-review", "groups": ["x"]}, "must be an object"),
    ],
)
def test_malformed_plans_are_rejected(plan, match):
    with pytest.raises(TriageError, match=match):
        validate_plan(plan, ["f-aaaa1111"], source="code-review")


# --------------------------------------------------------------------------
# Section-presence lint on every generated body.
# --------------------------------------------------------------------------


def test_every_generated_body_carries_all_five_sections_in_order():
    for group in load_fixture_plan()["groups"]:
        body = render(group)
        positions = [body.index(header) for header in REQUIRED_SECTIONS]
        assert positions == sorted(positions), f"{group['slug']} has sections out of order"
        lint_body(body)


def test_the_plain_terms_opening_comes_first():
    body = render(a_group())
    assert body.lstrip().startswith("## The problem in plain terms")
    assert "**The fix in one line:**" in body


def test_the_lint_rejects_a_body_missing_a_section():
    body = render(a_group()).replace("## Validation", "## Verification")
    with pytest.raises(TriageError, match="missing the '## Validation'"):
        lint_body(body)


def test_the_lint_rejects_a_body_whose_opening_is_not_first():
    body = "## Description\n\nx\n\n" + render(a_group())
    with pytest.raises(TriageError):
        lint_body(body)


def test_the_lint_rejects_sections_out_of_order():
    body = render(a_group())
    design = body.index("## Design")
    deployment = body.index("## Deployment")
    shuffled = (
        body[:design]
        + body[deployment : body.index("## Validation")]
        + body[design:deployment]
        + body[body.index("## Validation") :]
    )
    with pytest.raises(TriageError, match="out of order"):
        lint_body(shuffled)


def test_the_body_carries_the_ledger_pointer_not_inlined_detail():
    """Issue bodies are the context-passing channel for the downstream ops
    agent: a stable S3 reference plus a run id, not the scanner's own detail."""
    body = render(a_group())
    assert FINDINGS_URI in body
    assert RUN_ID in body


# --------------------------------------------------------------------------
# No reproduction detail.
# --------------------------------------------------------------------------


def test_generated_bodies_reference_findings_by_identifier_only():
    for group in load_fixture_plan()["groups"]:
        body = render(group)
        for finding_id in group["finding_ids"]:
            assert f"`{finding_id}`" in body


def test_no_generated_body_matches_the_banned_pattern_list():
    patterns = load_banned_patterns()
    for group in load_fixture_plan()["groups"]:
        assert banned_pattern_hits(render(group), patterns) == []


@pytest.mark.parametrize(
    "detail",
    [
        "curl -X POST https://api.example.com/v1/admin",
        "POST /admin/users with an empty body",
        "Authorization: Bearer abcdefghijklmnop",
        "Steps to reproduce: send the request twice",
        "Proof of concept: see below",
        "../../../../etc/passwd",
        "<script>alert(1)</script>",
        "' OR '1'='1",
        "UNION SELECT password FROM users",
        "; cat /etc/shadow",
        "sqlmap -u https://target/",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN RSA PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",
        "ghp_0123456789abcdefghij",
        "password = hunter2secret",
    ],
)
def test_reproduction_detail_in_a_body_is_a_hard_failure(detail):
    with pytest.raises(TriageError, match="banned pattern"):
        lint_body(render(a_group(approach=f"Fix it. {detail}")))


def test_the_banned_pattern_scan_reports_ids_never_the_matched_text():
    """The matched text must not travel: a CI log is a retained artifact."""
    hits = banned_pattern_hits("curl -X POST https://x/y")
    assert hits == ["curl-invocation"]
    assert all(isinstance(h, str) and " " not in h for h in hits)


def test_the_banned_pattern_list_covers_every_declared_class():
    """Each committed pattern must actually be a valid regex with a reason —
    a decorative row is a pattern that silently matches nothing."""
    document = json.loads(
        (REPO_ROOT / ".github/security/triage-banned-patterns.json").read_text(encoding="utf-8")
    )
    for entry in document["patterns"]:
        assert entry["reason"].strip(), f"{entry['id']} records no reason"
        re.compile(entry["regex"])
    assert len({e["id"] for e in document["patterns"]}) == len(document["patterns"])


def test_a_missing_banned_pattern_list_is_an_error_not_an_empty_scan():
    """Degrading to "no patterns" would turn the strongest control in this unit
    into a check that passes having asserted nothing."""
    with pytest.raises(TriageError, match="cannot read the banned-pattern list"):
        load_banned_patterns(Path("/nonexistent/patterns.json"))


def test_an_empty_banned_pattern_list_is_rejected(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"patterns": []}), encoding="utf-8")
    with pytest.raises(TriageError, match="refusing to scan nothing"):
        load_banned_patterns(path)


def test_an_invalid_regex_in_the_list_is_rejected(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"patterns": [{"id": "bad", "regex": "("}]}), encoding="utf-8")
    with pytest.raises(TriageError, match="not a valid regex"):
        load_banned_patterns(path)


def test_a_pattern_without_an_id_or_regex_is_rejected(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"patterns": [{"id": "x"}]}), encoding="utf-8")
    with pytest.raises(TriageError, match="needs an `id` and a `regex`"):
        load_banned_patterns(path)


# --------------------------------------------------------------------------
# Nothing self-dispatches.
# --------------------------------------------------------------------------


def test_no_generated_body_carries_an_agent_mention():
    for group in load_fixture_plan()["groups"]:
        assert "@agent-" not in render(group)


def test_an_agent_mention_in_a_body_is_a_hard_failure():
    with pytest.raises(TriageError, match="@agent-"):
        lint_body(render(a_group(motivation="Handing to @agent-developer.")))


def test_an_agent_mention_in_a_title_is_a_hard_failure(fake_gh):
    gh = fake_gh()
    with pytest.raises(TriageError, match="mention"):
        tg.file_group(
            REPO,
            a_group(title="ask @agent-developer to fix"),
            run_date=RUN_DATE,
            source="code-review",
            daily_epic=9100,
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
        )
    assert gh.writes == [], "a rejected work item must not have been created"


def test_filed_work_items_carry_the_story_label_and_nothing_that_dispatches(fake_gh):
    gh, _ = file_all(fake_gh)
    stories = [c for c in gh.created if "epic" not in c["labels"]]
    assert stories, "no work items were filed"
    for created in stories:
        assert created["labels"] == ["story"]
        assert not any(lab.startswith("agent-") for lab in created["labels"])


def test_story_labels_is_a_closed_list_of_exactly_the_story_label():
    assert story_labels() == ["story"]


# --------------------------------------------------------------------------
# Computed severity: a criticality on every filed issue, derived from the
# findings, never authored by the model.
# --------------------------------------------------------------------------


def _fixture_severities() -> dict:
    import security_traceability as st

    return st.severity_by_finding(FINDINGS_FIXTURE, "code-review")


def test_a_filed_issue_carries_a_severity_label_and_states_it_in_the_body(fake_gh):
    """The cluster covering the one CRITICAL finding is filed CRITICAL; an
    all-HIGH cluster is filed HIGH. Both the label and the body line come from the
    findings' risk levels, so a reader can triage by criticality without opening
    the issue and cannot be misled by a model that guessed."""
    gh = fake_gh()
    run_triage(
        REPO,
        plan=load_fixture_plan(),
        new_findings=load_fixture_findings(),
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        run_date=RUN_DATE,
        severities=_fixture_severities(),
    )
    stories = [c for c in gh.created if "epic" not in c["labels"]]
    assert stories, "no work items were filed"
    for created in stories:
        sev_labels = [lab for lab in created["labels"] if lab.startswith("severity:")]
        assert len(sev_labels) == 1, "every filed issue carries exactly one severity label"
        level = sev_labels[0].split(":", 1)[1].upper()
        assert f"**Severity** — {level}" in created["body"]
        # The story label is still present and nothing dispatches.
        assert "story" in created["labels"]
        assert not any(lab.startswith("agent-") for lab in created["labels"])
    # The fixture's one CRITICAL finding produces exactly one critical issue.
    assert sum("severity:critical" in c["labels"] for c in stories) == 1


def test_the_severity_labels_are_created_before_an_issue_is_filed_with_one(fake_gh):
    """`gh issue create --label` does NOT create a missing label, so filing with a
    `severity:<level>` that does not exist fails -- and it fails AFTER the dated
    parent was created, leaving a half-finished night. A fresh org has none of
    these labels (`aws-e/adp` had none when this was written), so they are ensured
    the same way `ensure_umbrella_epic.main` ensures `story`.

    Order is the claim: every label a filed issue carries must already have been
    created by an earlier call.
    """
    gh = fake_gh()  # no labels exist, which is the fresh-org state
    run_triage(
        REPO,
        plan=load_fixture_plan(),
        new_findings=load_fixture_findings(),
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        run_date=RUN_DATE,
        severities=_fixture_severities(),
    )
    # The night's two distinct severities were created, and nothing else was.
    assert sorted(gh.created_labels) == ["severity:critical", "severity:high"]

    creates = [i for i, call in enumerate(gh.calls) if call[:2] == ["issue", "create"]]
    for index, call in enumerate(gh.calls):
        if call[:2] != ["issue", "create"]:
            continue
        for label in (call[i + 1] for i, a in enumerate(call) if a == "--label"):
            if not label.startswith("severity:"):
                continue
            made_at = next(
                i for i, c in enumerate(gh.calls)
                if "--method" in c and f"name={label}" in c
            )
            assert made_at < index, f"{label} was used before it was created"
    assert creates, "no issues were filed"


def test_an_existing_severity_label_is_not_recreated(fake_gh):
    """Idempotent, so a second night cannot clobber a hand-tuned colour."""
    gh = fake_gh(labels={"severity:critical", "severity:high"})
    run_triage(
        REPO,
        plan=load_fixture_plan(),
        new_findings=load_fixture_findings(),
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        run_date=RUN_DATE,
        severities=_fixture_severities(),
    )
    assert gh.created_labels == []


def test_a_colour_is_declared_for_every_severity_in_the_vocabulary():
    """A level with no colour would raise a KeyError mid-filing, after the parent
    exists. Asserted against the traceability module's vocabulary so the two
    cannot drift."""
    import security_traceability as st

    assert set(tg.SEVERITY_LABEL_COLORS) == set(st.SEVERITY_ORDER)


def test_severity_in_the_body_is_the_first_impact_bullet_not_before_the_opening():
    """The severity line must not break the 'plain-terms opening comes first'
    lint — it lives inside Impact analysis, so the body still lints."""
    body = render(a_group(), severity="HIGH")
    assert "**Severity** — HIGH" in body
    lint_body(body, load_banned_patterns())  # does not raise
    assert body.lstrip().startswith(REQUIRED_SECTIONS[0])
    # Severity sits under Impact analysis, above the "Who benefits" bullet.
    assert body.index("**Severity**") > body.index("## Impact analysis")
    assert body.index("**Severity**") < body.index("Who benefits")


def test_a_body_without_a_computed_severity_omits_the_line_and_still_lints():
    """Severity is optional so the plan-authoring gate and `validate` can render a
    body with no findings document to derive it from."""
    body = render(a_group())
    assert "**Severity**" not in body
    lint_body(body, load_banned_patterns())  # does not raise


def test_a_group_covering_a_finding_with_no_severity_fails_the_filing(fake_gh):
    """A plan/findings mismatch is a hard error on the filing path, not a silently
    unlabelled issue on the run that files un-recallable documents."""
    fake_gh()
    severities = _fixture_severities()
    severities.pop("f-42dca300")
    with pytest.raises(TriageError, match="no severity"):
        run_triage(
            REPO,
            plan=load_fixture_plan(),
            new_findings=load_fixture_findings(),
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
            run_date=RUN_DATE,
            severities=severities,
        )


def test_the_file_command_enriches_the_traceability_ledger_grouping_to_filed(fake_gh, tmp_path):
    """The one file the whole feature exists for: written at grouping (finding ->
    cluster, with severity), then enriched IN PLACE at filing (cluster -> issue),
    and asserted to account for every finding exactly once before it persists."""
    import security_traceability as st

    # Stand up the grouping-stage ledger the author step would have written.
    trace_path = tmp_path / "traceability.json"
    severities = st.severity_by_finding(FINDINGS_FIXTURE, "code-review")
    st.write_ledger(trace_path, st.build_grouping(load_fixture_plan(), severities, run_id=RUN_ID))

    fake_gh()
    rc = tg.main(
        [
            "file",
            "--plan", str(PLAN_FIXTURE),
            "--new-findings", str(FINDINGS_FIXTURE),
            "--source", "code-review",
            "--repo", REPO,
            "--findings-uri", FINDINGS_URI,
            "--run-id", RUN_ID,
            "--run-date", RUN_DATE,
            "--traceability", str(trace_path),
        ]
    )
    assert rc == 0

    filed = json.loads(trace_path.read_text())
    assert filed["stage"] == "filed"
    st.assert_fully_traced(filed)  # every one of the 12 findings reached a real issue
    # The CRITICAL finding's row now points at a concrete issue number.
    assert isinstance(filed["findings_index"]["f-42dca300"]["issue_number"], int)
    assert filed["findings_index"]["f-42dca300"]["fix_status"] == "FILED"


def test_nothing_in_this_module_dispatches(fake_gh):
    """The whole flow must issue no `adp-trigger` call and no dispatching label.
    Asserted over every recorded gh call, not by reading the source."""
    gh, _ = file_all(fake_gh)
    for call in gh.calls:
        joined = " ".join(call)
        assert "adp-trigger" not in joined
        assert "@agent-" not in joined
    source = Path(tg.__file__).read_text(encoding="utf-8")
    assert "adp-trigger --persona" not in source


# --------------------------------------------------------------------------
# The dated parent: one per date, retry-safe.
# --------------------------------------------------------------------------


def test_the_dated_parent_is_created_under_the_runtime_umbrella(fake_gh):
    gh, result = file_all(fake_gh)
    assert result["umbrella"] == UMBRELLA_NUM
    assert result["daily_epic_created"] is True
    assert gh.parent_of[result["daily_epic"]] == UMBRELLA_NUM


def test_a_retry_creates_no_second_dated_parent(fake_gh):
    """Two partially-populated parents for one night is a state the run
    report's reconciliation can never balance."""
    gh = fake_gh()
    first = ensure_daily_epic(REPO, RUN_DATE, UMBRELLA_NUM)
    creates_after_first = len([c for c in gh.writes if c[:2] == ["issue", "create"]])
    second = ensure_daily_epic(REPO, RUN_DATE, UMBRELLA_NUM)
    assert second["number"] == first["number"]
    assert second["created"] is False
    assert len([c for c in gh.writes if c[:2] == ["issue", "create"]]) == creates_after_first


def test_a_full_retry_of_the_night_files_no_duplicate_anything(fake_gh):
    gh, first = file_all(fake_gh)
    creates = len(gh.created)
    second = run_triage(
        REPO,
        plan=load_fixture_plan(),
        new_findings=load_fixture_findings(),
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        run_date=RUN_DATE,
    )
    assert len(gh.created) == creates, "the retry created new issues"
    assert second["daily_epic"] == first["daily_epic"]
    assert second["ledger_fields"] == first["ledger_fields"]
    assert all(item["created"] is False for item in second["work_items"])


def test_a_half_finished_earlier_run_gets_its_parent_linked(fake_gh):
    """Created but never linked: the link is repaired, not re-created."""
    title = DAILY_EPIC_TITLE_TEMPLATE.format(run_date=RUN_DATE)
    gh = fake_gh(issues=[{"number": 8888, "title": title}])
    result = ensure_daily_epic(REPO, RUN_DATE, UMBRELLA_NUM)
    assert result == {"number": 8888, "created": False, "linked": True}
    assert not [c for c in gh.writes if c[:2] == ["issue", "create"]]


def test_two_dates_get_two_parents(fake_gh):
    fake_gh()
    first = ensure_daily_epic(REPO, "2026-08-30", UMBRELLA_NUM)
    second = ensure_daily_epic(REPO, "2026-08-31", UMBRELLA_NUM)
    assert first["number"] != second["number"]


def test_the_dated_parent_is_not_the_runtime_umbrella():
    """Two distinct umbrellas; conflating them breaks this unit."""
    assert DAILY_EPIC_TITLE_TEMPLATE.format(run_date=RUN_DATE) != UMBRELLA_TITLE


def test_a_missing_runtime_umbrella_is_an_error_not_an_orphan_parent(monkeypatch):
    """Filing under nothing would put the night's findings on an intake path
    nobody watches."""
    monkeypatch.setattr(ue, "_gh", FakeGh(issues=[]))
    with pytest.raises(TriageError, match="does not exist"):
        run_triage(
            REPO,
            plan=load_fixture_plan(),
            new_findings=load_fixture_findings(),
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
            run_date=RUN_DATE,
        )


def test_work_items_are_native_children_of_the_dated_parent(fake_gh):
    gh, result = file_all(fake_gh)
    assert len(result["work_items"]) == 5
    for item in result["work_items"]:
        assert gh.parent_of[item["number"]] == result["daily_epic"]
        assert item["linked"] is True


# --------------------------------------------------------------------------
# Zero new findings.
# --------------------------------------------------------------------------


def test_zero_new_findings_creates_no_parent_no_work_items_and_dispatches_nothing(fake_gh):
    gh = fake_gh()
    result = run_triage(
        REPO,
        plan=a_plan([]),
        new_findings={
            "run_date": RUN_DATE,
            "source": "code-review",
            "nothing_to_file": True,
            "finding_ids": [],
            "unreferenceable": 0,
        },
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        run_date=RUN_DATE,
    )
    assert result["nothing_to_file"] is True
    assert result["source"] == "code-review"
    assert result["run_date"] == RUN_DATE
    assert gh.calls == [], "a zero-findings night must touch no GitHub state at all"

    # NT-5 is about GitHub state -- no parent, no work item, no dispatch, all
    # asserted above. It is NOT about the ledger: this pass DID run, and it says
    # so with a completion marker carrying no stories. The join barrier (U10)
    # reads exactly this to tell "ran, found nothing" from "never ran"; without
    # it every quiet night looks like a hung scanner.
    assert result["ledger_fields"] == {
        "stories_created": 0,
        "story_ids": [],
        "findings_covered": [],
    }
    # No dated EPIC exists on a quiet night, so the field is absent rather than
    # zero -- the schema declares it `minimum: 1`, and a placeholder would be
    # recorded as fact.
    assert "daily_epic" not in result["ledger_fields"]


def test_a_plan_proposing_work_on_a_zero_findings_night_is_rejected():
    with pytest.raises(TriageError, match="files nothing"):
        validate_plan(a_plan([a_group()]), [], source="code-review")


def test_the_dedup_result_nothing_to_file_flag_is_read_not_inferred():
    """U8 built that flag so "no work" is distinguishable from "did not run"."""
    with pytest.raises(TriageError, match="not a dedup result"):
        _write_and_load({"new_findings": []})


def test_a_source_with_no_findings_of_its_own_files_nothing():
    """The fixture is a code-review night; the pentest pass over it has nothing
    to do and must not file an empty parent."""
    assert load_fixture_findings("pentest")["nothing_to_file"] is True


# --------------------------------------------------------------------------
# Two grouping passes, one per scanner.
# --------------------------------------------------------------------------


def test_both_scanners_land_under_the_same_dated_parent(fake_gh):
    fake_gh()
    code_review = run_triage(
        REPO,
        plan=load_fixture_plan(),
        new_findings=load_fixture_findings(),
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        run_date=RUN_DATE,
    )
    pentest_findings = load_fixture_findings()
    pentest_findings["source"] = "pentest"
    pentest_findings["finding_ids"] = ["f-eeee5555", "f-ffff6666"]
    pentest = run_triage(
        REPO,
        plan=a_plan(
            [a_group(slug="pentest-surface", finding_ids=["f-eeee5555", "f-ffff6666"])],
            source="pentest",
        ),
        new_findings=pentest_findings,
        findings_uri=FINDINGS_URI,
        run_id=RUN_ID,
        run_date=RUN_DATE,
    )
    assert pentest["daily_epic"] == code_review["daily_epic"]
    assert pentest["daily_epic_created"] is False


def test_a_plan_for_the_other_scanner_is_rejected():
    with pytest.raises(TriageError, match="must not be crossed"):
        validate_plan(a_plan([a_group()], source="pentest"), ["f-aaaa1111"], source="code-review")


def test_load_new_findings_selects_only_this_scanners_findings(tmp_path):
    document = {
        "run_date": RUN_DATE,
        "nothing_to_file": False,
        "new_findings": [
            {"finding_id": "f-aaaa1111", "source": "code-review"},
            {"finding_id": "f-bbbb2222", "source": "pentest"},
        ],
    }
    path = tmp_path / "dedup.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_new_findings(path, "code-review")["finding_ids"] == ["f-aaaa1111"]
    assert load_new_findings(path, "pentest")["finding_ids"] == ["f-bbbb2222"]


def test_an_unknown_source_is_rejected():
    with pytest.raises(TriageError, match="not one of"):
        load_new_findings(FINDINGS_FIXTURE, "sast")


def test_a_finding_with_an_unreferenceable_id_is_counted_not_silently_dropped():
    result = _write_and_load(
        {
            "run_date": RUN_DATE,
            "nothing_to_file": False,
            "new_findings": [
                {"finding_id": "f-aaaa1111", "source": "code-review"},
                {"finding_id": "not-an-id", "source": "code-review"},
            ],
        }
    )
    assert result["finding_ids"] == ["f-aaaa1111"]


@pytest.mark.parametrize(
    "document,match",
    [
        ("[]", "must hold an object"),
        ('{"nothing_to_file": false}', "no `new_findings` array"),
        ("not json", "cannot read the dedup result"),
    ],
)
def test_malformed_dedup_results_are_rejected(document, match, tmp_path):
    path = tmp_path / "dedup.json"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(TriageError, match=match):
        load_new_findings(path, "code-review")


def test_an_unreadable_dedup_result_is_an_error():
    with pytest.raises(TriageError, match="cannot read the dedup result"):
        load_new_findings(Path("/nonexistent/dedup.json"), "code-review")


# --------------------------------------------------------------------------
# The ledger shard: U2's schema, not a reshaped one.
# --------------------------------------------------------------------------


def test_the_triage_ledger_fields_are_accepted_by_the_U2_schema(fake_gh):
    _, result = file_all(fake_gh)
    shard = build_shard(
        RUN_DATE, "triage.code-review", "2026-08-30T03:10:00Z",
        result["ledger_fields"], load_schema(),
    )
    assert shard["fields"]["stories_created"] == 5
    assert len(shard["fields"]["findings_covered"]) == 12


def test_this_unit_writes_only_fields_the_triage_stage_owns(fake_gh):
    _, result = file_all(fake_gh)
    schema = load_schema()
    for name in result["ledger_fields"]:
        assert "triage" in schema["x-fields"][name]["stages"], f"{name} is not a triage field"


def test_the_ledger_records_identities_not_only_counts(fake_gh):
    """Reconciliation must be able to name WHICH story or finding is
    unaccounted for, not only report a delta."""
    _, result = file_all(fake_gh)
    fields = result["ledger_fields"]
    assert len(fields["story_ids"]) == fields["stories_created"]
    assert fields["story_ids"] == sorted(i["number"] for i in result["work_items"])


def test_findings_covered_matches_what_the_plan_grouped(fake_gh):
    _, result = file_all(fake_gh)
    assert result["ledger_fields"]["findings_covered"] == load_fixture_findings()["finding_ids"]


def test_ledger_fields_dedupes_findings_across_work_items():
    fields = ledger_fields(
        5001,
        [
            {"number": 5003, "finding_ids": ["f-aaaa1111", "f-bbbb2222"]},
            {"number": 5002, "finding_ids": ["f-bbbb2222"]},
        ],
    )
    assert fields == {
        "daily_epic": 5001,
        "stories_created": 2,
        "story_ids": [5002, 5003],
        "findings_covered": ["f-aaaa1111", "f-bbbb2222"],
    }


# --------------------------------------------------------------------------
# The marker WRAPPER and the marker NAME (#4616).
#
# `ledger_fields` above returns the right field SET, and the tests above proved
# it -- by wrapping it in `build_shard` themselves. That is exactly what shipped
# the defect: the marker `_cmd_file` wrote was the bare field set, and the join
# barrier validates every marker through U2's envelope and RAISES on a missing
# field. The stage exited 0, landed an unreadable marker, and failed one job
# later with "invalid shard" -- indistinguishable from a genuinely broken run.
#
# So these assert on the WRITTEN ARTIFACT, and nothing here supplies an envelope
# on the writer's behalf.
# --------------------------------------------------------------------------


def test_the_written_marker_is_accepted_by_the_barrier_as_written(fake_gh, tmp_path):
    """The acceptance test: U9's real output goes straight into U2's validator
    and U10's loader, with no test-side wrapping in between."""
    fake_gh()
    out = tmp_path / "ledger"
    assert main(_cli_args("file", ["--repo", REPO, *_marker_args(out)])) == 0

    marker = out / "shard-triage.code-review.json"
    shard = json.loads(marker.read_text(encoding="utf-8"))
    validate_shard(shard, load_schema())

    # And through the barrier's own reader, which is the code that actually
    # rejected the old bare marker.
    assert sorted(load_markers(out)) == ["code-review"]
    assert load_markers(out)["code-review"]["fields"]["stories_created"] == 5


def test_the_marker_carries_the_full_u2_envelope(fake_gh, tmp_path):
    """Named field by field: `validate_shard` raises on the FIRST missing one, so
    a single assertion cannot show the envelope is complete."""
    fake_gh()
    out = tmp_path / "ledger"
    assert main(_cli_args("file", ["--repo", REPO, *_marker_args(out)])) == 0
    shard = json.loads((out / "shard-triage.code-review.json").read_text(encoding="utf-8"))

    assert shard["schema_version"] == "1"
    assert shard["run_date"] == RUN_DATE
    assert shard["stage"] == "triage.code-review"
    assert shard["stage_type"] == "triage"
    assert shard["generated_at"] == GENERATED_AT
    assert isinstance(shard["fields"], dict)


def test_the_marker_name_matches_the_delivery_jobs_glob(fake_gh, tmp_path):
    """`security-agent-nightly.yml`'s deliver job decides whether there is
    anything to join with `find -name 'shard-triage*.json'`. A marker outside that
    glob leaves `joinable` false forever: the night hands off nothing, silently,
    and no error is raised anywhere."""
    fake_gh()
    out = tmp_path / "ledger"
    assert main(_cli_args("file", ["--repo", REPO, *_marker_args(out)])) == 0

    written = sorted(p.name for p in out.iterdir())
    assert written == ["shard-triage.code-review.json"]
    assert fnmatch.fnmatch(written[0], "shard-triage*.json")
    # The name is DERIVED from the stage id, not typed alongside it -- so the two
    # cannot drift apart.
    shard = json.loads((out / written[0]).read_text(encoding="utf-8"))
    assert written[0] == SHARD_NAME_TEMPLATE.format(stage=shard["stage"])


def test_each_scanner_writes_its_own_marker_under_its_own_name(fake_gh, tmp_path):
    """U2's stage id is the concurrency boundary. The two grouping halves run
    concurrently into the SAME ledger directory, so if they derived one name the
    second to finish would silently overwrite the first's completion signal and
    the barrier would wait forever on a pass that had already reported."""
    # ONE directory for both passes, which is the arrangement that matters: the
    # delivery job syncs every shard of the night into a single prefix, so two
    # passes deriving one name is a lost completion signal, not a collision two
    # separate temp dirs would have hidden.
    out = tmp_path / "ledger"
    for source in tg.SOURCES:
        fake_gh()
        args = _cli_args("file", ["--repo", REPO, *_marker_args(out)])
        args[args.index("--source") + 1] = source
        # The pentest plan/findings fixtures are the code-review ones relabelled:
        # what is under test is the marker's identity, not the grouping.
        args[args.index("--plan") + 1] = str(_relabelled(tmp_path, PLAN_FIXTURE, source))
        args[args.index("--new-findings") + 1] = str(
            _relabelled(tmp_path, FINDINGS_FIXTURE, source)
        )
        assert main(args) == 0

    assert sorted(p.name for p in out.iterdir()) == [
        "shard-triage.code-review.json",
        "shard-triage.pentest.json",
    ], "the second pass overwrote the first's completion marker"
    # Both are attributable, so the barrier sees a complete join rather than
    # waiting forever on a pass that already reported.
    assert sorted(load_markers(out)) == ["code-review", "pentest"]


def test_a_marker_cannot_be_written_under_an_undeclared_scanner():
    with pytest.raises(TriageError, match="is not one of"):
        tg.marker_stage("nmap")


def test_the_marker_stage_is_never_a_bare_triage():
    """A bare `triage` id is the shape the barrier rejects as unattributable, and
    two passes sharing it would overwrite each other's key. It must be
    unreachable from a declared source, not merely absent today."""
    for source in tg.SOURCES:
        assert tg.marker_stage(source) != "triage"
        assert tg.marker_stage(source).startswith("triage.")


def test_writing_a_marker_needs_a_timestamp_and_says_so_before_filing(
    monkeypatch, tmp_path, capsys
):
    """`--generated-at` is required with `--ledger-dir`, and the refusal lands
    BEFORE anything is filed: a marker this pass cannot write is a wiring bug,
    and discovering it after the issues exist means the fixing retry runs against
    GitHub state the first attempt created."""
    gh = FakeGh(issues=[])
    monkeypatch.setattr(ue, "_gh", gh)
    out = tmp_path / "ledger"
    assert main(_cli_args("file", ["--repo", REPO, "--ledger-dir", str(out)])) == 1
    assert "--ledger-dir needs --generated-at" in capsys.readouterr().err
    assert gh.calls == [], "the refusal must precede every GitHub write"
    assert not out.exists()


def test_a_malformed_timestamp_fails_in_this_stage_not_in_the_barrier(fake_gh, tmp_path, capsys):
    """`build_shard` validates before writing, so an unusable timestamp is this
    stage's named error rather than an unreadable marker the barrier is blamed
    for one job later."""
    fake_gh()
    out = tmp_path / "ledger"
    assert main(_cli_args("file", ["--repo", REPO, *_marker_args(out, "last tuesday")])) == 1
    assert "::error title=Security findings triage::" in capsys.readouterr().err
    assert not out.exists(), "an invalid marker must not be left on disk"


def test_the_marker_is_byte_reproducible_across_a_rerun(fake_gh, tmp_path):
    """FR-C30: the same night re-run with the same caller-supplied timestamp
    rewrites the same bytes, so a retry is a no-op rather than a spurious diff.
    This is why `--generated-at` is the caller's and not a clock read here."""
    first, second = tmp_path / "a", tmp_path / "b"
    for out in (first, second):
        fake_gh()
        assert main(_cli_args("file", ["--repo", REPO, *_marker_args(out)])) == 0
    name = "shard-triage.code-review.json"
    assert (first / name).read_bytes() == (second / name).read_bytes()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cli_args(command, extra=()):
    return [
        command,
        "--plan", str(PLAN_FIXTURE),
        "--new-findings", str(FINDINGS_FIXTURE),
        "--source", "code-review",
        "--findings-uri", FINDINGS_URI,
        "--run-id", RUN_ID,
        *extra,
    ]


def _marker_args(ledger_dir, generated_at=GENERATED_AT):
    """The marker-writing flags. A DIRECTORY plus a caller-supplied timestamp --
    the filename is U9's to derive, so no test names it on the command line."""
    return ["--ledger-dir", str(ledger_dir), "--generated-at", generated_at]


def _relabelled(tmp_path, fixture, source):
    """Copy a `code-review` fixture across to another scanner.

    `source` is stamped on the document AND on every finding, because
    `load_new_findings` selects per-finding: relabelling only the envelope yields
    a document that parses and matches nothing, which reads as a quiet night.
    """
    document = json.loads(fixture.read_text(encoding="utf-8"))
    document["source"] = source
    for finding in document.get("new_findings", []):
        finding["source"] = source
    out = tmp_path / f"{fixture.stem}.{source}.json"
    out.write_text(json.dumps(document), encoding="utf-8")
    return out


def test_the_validate_subcommand_touches_no_github_state(monkeypatch, capsys):
    gh = FakeGh(issues=[])
    monkeypatch.setattr(ue, "_gh", gh)
    assert main(_cli_args("validate")) == 0
    assert gh.calls == []
    assert "work_items=5 band=4..6" in capsys.readouterr().out


def test_the_validate_subcommand_fails_on_a_bad_plan(tmp_path, capsys):
    bad = tmp_path / "plan.json"
    bad.write_text(json.dumps(a_plan([a_group()])), encoding="utf-8")
    args = _cli_args("validate")
    args[args.index("--plan") + 1] = str(bad)
    assert main(args) == 1
    assert "::error title=Security findings triage::" in capsys.readouterr().err


def test_the_file_subcommand_writes_the_ledger_shard(fake_gh, tmp_path, capsys):
    """The marker is a full shard under a DERIVED name, into a directory that
    need not already exist."""
    fake_gh()
    out = tmp_path / "nested" / "ledger"
    assert main(_cli_args("file", ["--repo", REPO, *_marker_args(out)])) == 0
    shard = json.loads((out / "shard-triage.code-review.json").read_text(encoding="utf-8"))
    assert shard["fields"]["stories_created"] == 5
    assert shard["stage"] == "triage.code-review"
    assert "nothing_to_file=false" in capsys.readouterr().out


def test_the_ci_log_carries_counts_not_titles_or_paths(fake_gh, capsys):
    """A CI log is readable by anyone who can see the run (NEV-2)."""
    fake_gh()
    main(_cli_args("file", ["--repo", REPO]))
    out = capsys.readouterr().out
    for group in load_fixture_plan()["groups"]:
        assert group["title"] not in out
        for surface in group["fix_surface"]:
            assert surface not in out


def test_the_file_subcommand_on_a_zero_findings_night(monkeypatch, tmp_path, capsys):
    gh = FakeGh(issues=[])
    monkeypatch.setattr(ue, "_gh", gh)
    findings = tmp_path / "dedup.json"
    findings.write_text(
        json.dumps({"run_date": RUN_DATE, "nothing_to_file": True, "new_findings": []}),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(a_plan([])), encoding="utf-8")
    args = _cli_args("file", ["--repo", REPO])
    args[args.index("--plan") + 1] = str(plan)
    args[args.index("--new-findings") + 1] = str(findings)
    assert main(args) == 0
    assert gh.calls == []
    assert "nothing_to_file=true" in capsys.readouterr().out


def test_the_file_subcommand_writes_a_completion_marker_on_a_quiet_night(
    monkeypatch, tmp_path, capsys
):
    """The quiet path must still write its completion marker.

    This is the assertion whose absence let the barrier bug ship: `_cmd_file`
    returned before the write, so a healthy scanner that found nothing left no
    trace, and the join barrier could not distinguish it from one that hung. The
    marker is the ONLY thing a quiet night produces -- GitHub is untouched.
    """
    gh = FakeGh(issues=[])
    monkeypatch.setattr(ue, "_gh", gh)
    findings = tmp_path / "dedup.json"
    findings.write_text(
        json.dumps({"run_date": RUN_DATE, "nothing_to_file": True, "new_findings": []}),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(a_plan([])), encoding="utf-8")
    out = tmp_path / "nested" / "ledger"
    args = _cli_args("file", ["--repo", REPO, *_marker_args(out)])
    args[args.index("--plan") + 1] = str(plan)
    args[args.index("--new-findings") + 1] = str(findings)

    assert main(args) == 0
    assert gh.calls == [], "a quiet night must still touch no GitHub state"
    marker = out / "shard-triage.code-review.json"
    assert marker.exists(), "a quiet night left no completion marker"

    # The marker U9 writes must be a shard the barrier ACCEPTS as written -- the
    # whole envelope, read back off disk, not a payload a test wraps for it.
    shard = json.loads(marker.read_text(encoding="utf-8"))
    validate_shard(shard, load_schema())
    assert shard["fields"] == {"stories_created": 0, "story_ids": [], "findings_covered": []}
    assert "daily_epic" not in shard["fields"], "there is no dated EPIC on a quiet night"


def test_an_unresolvable_run_date_is_an_error(tmp_path):
    findings = tmp_path / "dedup.json"
    findings.write_text(
        json.dumps({"nothing_to_file": True, "new_findings": []}), encoding="utf-8"
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"schema_version": "1", "source": "code-review", "groups": []}),
                    encoding="utf-8")
    args = _cli_args("validate")
    args[args.index("--plan") + 1] = str(plan)
    args[args.index("--new-findings") + 1] = str(findings)
    assert main(args) == 1


def test_an_explicit_run_date_overrides_the_documents(fake_gh, capsys):
    fake_gh()
    assert main(_cli_args("file", ["--repo", REPO, "--run-date", "2026-09-01"])) == 0
    assert "nothing_to_file=false" in capsys.readouterr().out


def test_an_unreadable_plan_is_an_error(tmp_path):
    args = _cli_args("validate")
    args[args.index("--plan") + 1] = "/nonexistent/plan.json"
    assert main(args) == 1


def test_a_create_failure_is_surfaced_not_swallowed(monkeypatch):
    class FailingGh(FakeGh):
        def __call__(self, args):
            if args[:2] == ["issue", "create"]:
                return 1, "", "HTTP 403"
            return super().__call__(args)

    monkeypatch.setattr(ue, "_gh", FailingGh(issues=[{"number": UMBRELLA_NUM, "title": UMBRELLA_TITLE}]))
    with pytest.raises(TriageError, match="failed to create issue"):
        run_triage(
            REPO,
            plan=load_fixture_plan(),
            new_findings=load_fixture_findings(),
            findings_uri=FINDINGS_URI,
            run_id=RUN_ID,
            run_date=RUN_DATE,
        )


def test_an_unparseable_create_response_is_an_error(monkeypatch):
    class OddGh(FakeGh):
        def __call__(self, args):
            if args[:2] == ["issue", "create"]:
                return 0, "created!", ""
            return super().__call__(args)

    monkeypatch.setattr(ue, "_gh", OddGh(issues=[{"number": UMBRELLA_NUM, "title": UMBRELLA_TITLE}]))
    with pytest.raises(TriageError, match="could not parse issue number"):
        ensure_daily_epic(REPO, RUN_DATE, UMBRELLA_NUM)


# --------------------------------------------------------------------------
# Persona asserts.
#
# Mechanical, because "we updated the doc" is a judgment call. Each assert below
# names a property of the FILE, not an opinion about it.
# --------------------------------------------------------------------------


def _persona_text() -> str:
    return ARCHITECT_PERSONA.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of one `###`/`##` section, up to the next heading of the same or
    higher level. Section scoping is what makes "the reconciling text is in the
    SAME section" mechanically checkable rather than a judgment call."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading) :]
    following = [
        m.start()
        for m in re.finditer(r"^#{1,%d} " % level, rest, re.MULTILINE)
    ]
    body = rest[: following[0]] if following else rest
    # Whitespace-flattened: the persona file is hard-wrapped, so a phrase these
    # asserts care about can span a line break. The assert is about the
    # instruction being present, not about where the author broke the line.
    return re.sub(r"\s+", " ", body)


def test_architect_persona_authorizes_issue_authoring_for_this_flow():
    text = _persona_text()
    section = _section(text, "### Authoring authorization")
    assert "authorized" in section.lower()
    assert "triage" in section.lower()
    assert "#4290" in section, "the authorization must name the flow it applies to"


def test_the_authorization_lives_in_the_same_section_as_the_assessment_rule():
    """The reconciliation must be reachable from the instruction it reconciles.
    An authorization filed in a distant section leaves a later reader choosing
    between two rules — which is the state this unit exists to end."""
    text = _persona_text()
    output_section = _section(text, "## Design review output — what to write")
    assert "Deliver one assessment." in output_section
    assert "the runtime publishes that response as the issue outcome" in output_section
    assert "Do not also post a design-review comment with a tool." in output_section
    assert "### Authoring authorization" in output_section, (
        "the authorization is not inside the section carrying the assessment "
        "instruction, so the contradiction survives for a later reader"
    )


def test_no_surviving_assessment_only_contradiction():
    """Every instruction that could read as "an assessment is your ONLY output" must
    be qualified by the authoring exception."""
    text = _persona_text()
    output_section = _section(text, "## Design review output — what to write")
    assessment_index = output_section.index("Deliver one assessment.")
    authorization_index = output_section.index("### Authoring authorization")
    assert authorization_index > assessment_index, "the qualification must follow the rule"
    style = _section(text, "## Interaction style")
    assert "reviewing, not replacing" in style
    assert "Authoring authorization" in style, (
        "the 'reviewing, not replacing' rule is unqualified, so it still reads as "
        "forbidding the authoring this unit authorizes"
    )


def test_the_dispatch_prohibition_is_still_present():
    """Authoring is permitted; dispatching is not. This is the safety rule the
    whole EPIC is built inside, and it must survive the widening above."""
    section = _section(_persona_text(), "### Authoring authorization")
    assert "@agent-<persona>` comment" in section
    assert "label" in section
    assert "never" in section.lower()
    assert "adp-trigger" in section


def test_the_persona_forbids_dispatching_tokens_on_authored_issues():
    section = _section(_persona_text(), "### Authoring authorization")
    assert "`agent-*` label" in section
    assert "no `@agent-` mention" in section


def test_the_persona_requires_the_five_section_convention_and_no_repro_detail():
    section = _section(_persona_text(), "### Authoring authorization")
    assert "five-section" in section
    assert "f-<hex>" in section
    assert "reproduction detail" in section


def test_the_persona_records_that_ci_materializes_the_grouping():
    """Without this the role would try a direct create on a path where it is
    inert, and report success having filed nothing."""
    section = _section(_persona_text(), "### Authoring authorization")
    assert "triage_group_findings.py" in section
    assert "credential" in section


def test_no_other_persona_file_changed():
    """Scope guard: this unit modifies exactly one persona file. Asserted by
    content, since a persona edit only reaches the runtime via an image rebuild
    and a stray edit would ride along unnoticed."""
    personas = ARCHITECT_PERSONA.parent
    for path in sorted(personas.glob("*.md")):
        if path == ARCHITECT_PERSONA:
            continue
        assert "Authoring authorization" not in path.read_text(encoding="utf-8"), (
            f"{path.name} carries this unit's text; only architect.md should"
        )


def test_the_persona_change_reaches_the_runtime_only_through_the_persona_dir():
    """The worker image copies ONLY `rules/personas/`. This assert is why the
    authorization was written into architect.md rather than into the routing or
    phase documents — editing those changes nothing on the hosted path."""
    dockerfile = WORKER_DOCKERFILE.read_text(encoding="utf-8")
    assert "modules/agent-factory/rules/personas/" in dockerfile
    assert str(ARCHITECT_PERSONA.relative_to(REPO_ROOT)).startswith(
        "modules/agent-factory/rules/personas/"
    )


# --------------------------------------------------------------------------
# CI binding: a suite no workflow runs is a gate that does not exist.
# --------------------------------------------------------------------------


def test_this_suite_is_pinned_into_script_tests():
    text = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_triage_grouping.py" in text, (
        "the new suite is not pinned into Script Tests, so it never runs in CI"
    )
    for path in (
        ".github/scripts/triage_group_findings.py",
        ".github/security/triage-banned-patterns.json",
        "modules/agent-factory/rules/personas/architect.md",
    ):
        assert path in text, f"{path} is not in the Script Tests paths filter"


# --------------------------------------------------------------------------
# tiny local helper (kept at the bottom: plumbing, not a gate)
# --------------------------------------------------------------------------


def _write_and_load(document: dict, source: str = "code-review") -> dict:
    import tempfile

    path = Path(tempfile.mkdtemp()) / "dedup.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_new_findings(path, source)
