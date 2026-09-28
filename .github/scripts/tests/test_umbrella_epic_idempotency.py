"""Tests for ensure_umbrella_epic.py.

The idempotency claim is proven against recorded API responses, so no real issues
are created in CI. Every `gh` invocation goes through a single seam (`_gh`), which
lets these tests both stub reads and assert that a *write* was never issued.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ensure_umbrella_epic as ue
from ensure_umbrella_epic import (
    UMBRELLA_TITLE,
    ensure_label,
    ensure_umbrella_epic,
    find_issue_by_exact_title,
    link_sub_issue,
    main,
)

REPO = "aws-e/adp"
PARENT = 615
UMBRELLA_NUM = 9001

# The delivery EPIC for intent #4290. Also a child of #615, also reads as "the
# umbrella EPIC under #615" — and must never be mistaken for the runtime one.
DELIVERY_EPIC = {
    "number": 4438,
    "title": (
        "EPIC: Nightly AWS Security Agent — whole-repo code review + fenced "
        "pentest + autonomous triage→delivery (build plan for #4290)"
    ),
}
RUNTIME_UMBRELLA = {"number": UMBRELLA_NUM, "title": UMBRELLA_TITLE}


class FakeGh:
    """Records every `gh` invocation and replays canned responses.

    Any call classified as a write (issue create, label create, graphql mutation)
    is recorded in `writes`, so a test can assert none happened.
    """

    def __init__(self, *, issues=(), label_exists=True, parent_of=None):
        self.issues = list(issues)
        self.label_exists = label_exists
        # child number -> parent number
        self.parent_of = dict(parent_of or {})
        self.calls: list[list[str]] = []
        self.writes: list[list[str]] = []
        self._next_number = UMBRELLA_NUM

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        joined = " ".join(args)

        # --- writes -------------------------------------------------------
        if args[:2] == ["issue", "create"]:
            self.writes.append(args)
            number = self._next_number
            self._next_number += 1
            title = args[args.index("--title") + 1]
            self.issues.append({"number": number, "title": title})
            return 0, f"https://github.com/{REPO}/issues/{number}\n", ""

        if "--method" in args and "POST" in args and "/labels" in joined:
            self.writes.append(args)
            self.label_exists = True
            return 0, "{}", ""

        if args[:2] == ["api", "graphql"]:
            self.writes.append(args)
            child = int(args[-1].split("=")[1].removeprefix("NODE_"))
            self.parent_of[child] = PARENT
            return 0, '{"data":{}}', ""

        # --- reads --------------------------------------------------------
        if "/labels/" in joined:
            return (0, "{}", "") if self.label_exists else (1, "", "Not Found")

        if args[:2] == ["issue", "list"]:
            # Mimic the search API's relevance matching: it returns anything
            # whose title shares words with the query, not just exact hits.
            search = args[args.index("--search") + 1]
            needle = search.split('"')[1]
            first_word = needle.split()[0].lower()
            hits = [i for i in self.issues if first_word in i["title"].lower()]
            return 0, json.dumps(hits), ""

        if joined.endswith("/parent --jq .number") or "/parent" in joined:
            child = int(args[1].split("/")[-2])
            parent = self.parent_of.get(child)
            return (0, f"{parent}\n", "") if parent else (1, "", "Not Found")

        if ".node_id" in joined:
            number = args[1].split("/")[-1]
            return 0, f"NODE_{number}\n", ""

        raise AssertionError(f"unexpected gh call: {args}")


@pytest.fixture
def fake_gh(monkeypatch):
    def _install(**kwargs):
        gh = FakeGh(**kwargs)
        monkeypatch.setattr(ue, "_gh", gh)
        return gh

    return _install


# --------------------------------------------------------------------------
# Uniqueness: the create path called twice yields exactly one umbrella.
# --------------------------------------------------------------------------


def test_create_path_twice_yields_one_umbrella(fake_gh):
    """Second call returns the existing number and issues no create mutation."""
    gh = fake_gh(issues=[DELIVERY_EPIC])

    first = ensure_umbrella_epic(REPO, PARENT)
    assert first["created"] is True

    creates_after_first = [w for w in gh.writes if w[:2] == ["issue", "create"]]
    assert len(creates_after_first) == 1

    second = ensure_umbrella_epic(REPO, PARENT)

    assert second["number"] == first["number"], "second call must reuse the umbrella"
    assert second["created"] is False
    # The whole point: no second create, ever.
    assert [w for w in gh.writes if w[:2] == ["issue", "create"]] == creates_after_first
    assert len([i for i in gh.issues if i["title"] == UMBRELLA_TITLE]) == 1


def test_existing_umbrella_issues_no_write_at_all(fake_gh):
    """Against a fully-converged repo the helper is a pure read."""
    gh = fake_gh(
        issues=[DELIVERY_EPIC, RUNTIME_UMBRELLA],
        parent_of={UMBRELLA_NUM: PARENT},
    )

    result = ensure_umbrella_epic(REPO, PARENT)

    assert result == {"number": UMBRELLA_NUM, "created": False, "linked": False}
    assert gh.writes == [], f"expected no writes, got {gh.writes}"


# --------------------------------------------------------------------------
# Parentage: the umbrella's native parent resolves to 615.
# --------------------------------------------------------------------------


def test_created_umbrella_parent_resolves_to_615(fake_gh):
    gh = fake_gh(issues=[DELIVERY_EPIC])

    result = ensure_umbrella_epic(REPO, PARENT)

    assert result["linked"] is True
    assert gh.parent_of[result["number"]] == 615
    assert ue.current_parent(REPO, result["number"]) == 615


def test_default_parent_is_615():
    """The remediation tree is the contract, not a caller-supplied default."""
    assert ue.DEFAULT_PARENT == 615


def test_orphaned_umbrella_gets_linked_on_rerun(fake_gh):
    """Repairs a half-finished run that created the umbrella but never linked it."""
    gh = fake_gh(issues=[RUNTIME_UMBRELLA], parent_of={})

    result = ensure_umbrella_epic(REPO, PARENT)

    assert result == {"number": UMBRELLA_NUM, "created": False, "linked": True}
    assert gh.parent_of[UMBRELLA_NUM] == 615
    assert not [w for w in gh.writes if w[:2] == ["issue", "create"]]


def test_link_is_idempotent(fake_gh):
    gh = fake_gh(issues=[RUNTIME_UMBRELLA], parent_of={UMBRELLA_NUM: PARENT})

    assert link_sub_issue(REPO, PARENT, UMBRELLA_NUM) is False
    assert gh.writes == []


def test_refuses_to_reparent_a_foreign_child(fake_gh):
    """Silently re-parenting would move findings out of a tree someone watches."""
    fake_gh(issues=[RUNTIME_UMBRELLA], parent_of={UMBRELLA_NUM: 777})

    with pytest.raises(RuntimeError, match="sub-issue of #777"):
        link_sub_issue(REPO, PARENT, UMBRELLA_NUM)


# --------------------------------------------------------------------------
# Exact-title matching: the delivery EPIC is not the runtime umbrella.
# --------------------------------------------------------------------------


def test_exact_title_match_ignores_delivery_epic(fake_gh):
    """A fixture containing both EPICs returns only the runtime one."""
    fake_gh(issues=[DELIVERY_EPIC, RUNTIME_UMBRELLA])

    assert find_issue_by_exact_title(REPO, UMBRELLA_TITLE) == UMBRELLA_NUM


def test_delivery_epic_alone_is_not_treated_as_the_umbrella(fake_gh):
    """With only the delivery EPIC present the helper must still create."""
    fake_gh(issues=[DELIVERY_EPIC])

    assert find_issue_by_exact_title(REPO, UMBRELLA_TITLE) is None

    result = ensure_umbrella_epic(REPO, PARENT)
    assert result["created"] is True
    assert result["number"] != DELIVERY_EPIC["number"]


def test_near_miss_titles_do_not_match(fake_gh):
    """Trailing whitespace / punctuation drift is not an exact match."""
    fake_gh(
        issues=[
            {"number": 10, "title": UMBRELLA_TITLE + " (2026-08-30)"},
            {"number": 11, "title": UMBRELLA_TITLE.replace("—", "-")},
            {"number": 12, "title": UMBRELLA_TITLE + " "},
        ]
    )

    assert find_issue_by_exact_title(REPO, UMBRELLA_TITLE) is None


def test_duplicate_exact_titles_pick_lowest(fake_gh, capsys):
    fake_gh(
        issues=[
            {"number": 50, "title": UMBRELLA_TITLE},
            {"number": 20, "title": UMBRELLA_TITLE},
        ]
    )

    assert find_issue_by_exact_title(REPO, UMBRELLA_TITLE) == 20
    assert "2 issues share the exact title" in capsys.readouterr().err


def test_search_failure_raises(fake_gh, monkeypatch):
    monkeypatch.setattr(ue, "_gh", lambda args: (1, "", "gh: rate limited"))

    with pytest.raises(RuntimeError, match="failed to search issues"):
        find_issue_by_exact_title(REPO, UMBRELLA_TITLE)


def test_unparseable_search_output_raises(monkeypatch):
    monkeypatch.setattr(ue, "_gh", lambda args: (0, "not json", ""))

    with pytest.raises(RuntimeError, match="unparseable"):
        find_issue_by_exact_title(REPO, UMBRELLA_TITLE)


# --------------------------------------------------------------------------
# Label idempotency.
# --------------------------------------------------------------------------


def test_label_create_is_idempotent(fake_gh):
    """Against an existing label: succeeds, issues no duplicate create."""
    gh = fake_gh(label_exists=True)

    assert ensure_label(REPO, "story", "0e8a16", "desc") is False
    assert gh.writes == [], "existing label must not trigger a create request"


def test_label_created_when_absent(fake_gh):
    gh = fake_gh(label_exists=False)

    assert ensure_label(REPO, "story", "0e8a16", "desc") is True
    assert len(gh.writes) == 1
    # Second pass sees it and stands down.
    assert ensure_label(REPO, "story", "0e8a16", "desc") is False
    assert len(gh.writes) == 1


def test_label_race_already_exists_is_success(monkeypatch):
    """Concurrent nightly runs: 'already_exists' is the desired end state."""
    calls = []

    def gh(args):
        calls.append(args)
        if "/labels/" in " ".join(args):
            return 1, "", "Not Found"
        return 1, "", '{"errors":[{"code":"already_exists"}]}'

    monkeypatch.setattr(ue, "_gh", gh)

    assert ensure_label(REPO, "story", "0e8a16", "desc") is False


def test_label_create_hard_failure_raises(monkeypatch):
    def gh(args):
        if "/labels/" in " ".join(args):
            return 1, "", "Not Found"
        return 1, "", "HTTP 403: Resource not accessible by integration"

    monkeypatch.setattr(ue, "_gh", gh)

    with pytest.raises(RuntimeError, match="failed to create label"):
        ensure_label(REPO, "story", "0e8a16", "desc")


def test_no_type_prefixed_labels():
    """Decision D-20: bare `epic`/`story`, no `type:*` scheme."""
    assert ue.UMBRELLA_LABEL == "epic"
    assert ue.STORY_LABEL == "story"
    assert not ue.UMBRELLA_LABEL.startswith("type:")
    assert not ue.STORY_LABEL.startswith("type:")


# --------------------------------------------------------------------------
# CLI end-to-end (still no real API calls).
# --------------------------------------------------------------------------


def test_main_is_idempotent_end_to_end(fake_gh, capsys):
    gh = fake_gh(issues=[DELIVERY_EPIC], label_exists=False)

    assert main(["--repo", REPO]) == 0
    first_writes = len(gh.writes)
    number = ensure_umbrella_epic(REPO, PARENT)["number"]

    capsys.readouterr()
    assert main(["--repo", REPO]) == 0

    assert f"umbrella_issue={number}" in capsys.readouterr().out
    assert len(gh.writes) == first_writes, "second run must issue no new writes"


def test_main_creates_umbrella_labelled_epic(fake_gh):
    gh = fake_gh(issues=[DELIVERY_EPIC])

    main(["--repo", REPO])

    create = next(w for w in gh.writes if w[:2] == ["issue", "create"])
    assert create[create.index("--label") + 1] == "epic"
    assert create[create.index("--title") + 1] == UMBRELLA_TITLE


def test_create_failure_raises(monkeypatch):
    def gh(args):
        if args[:2] == ["issue", "list"]:
            return 0, "[]", ""
        return 1, "", "HTTP 403"

    monkeypatch.setattr(ue, "_gh", gh)

    with pytest.raises(RuntimeError, match="failed to create umbrella"):
        ensure_umbrella_epic(REPO, PARENT)


def test_unparseable_create_output_raises(monkeypatch):
    def gh(args):
        if args[:2] == ["issue", "list"]:
            return 0, "[]", ""
        return 0, "something unexpected", ""

    monkeypatch.setattr(ue, "_gh", gh)

    with pytest.raises(RuntimeError, match="could not parse issue number"):
        ensure_umbrella_epic(REPO, PARENT)


def test_gh_seam_shells_out_to_gh(monkeypatch):
    """`_gh` prefixes `gh` and captures text output."""
    captured = {}

    class Proc:
        returncode = 0
        stdout = "out"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr(ue.subprocess, "run", fake_run)

    assert ue._gh(["api", "x"]) == (0, "out", "")
    assert captured["cmd"] == ["gh", "api", "x"]
    assert captured["kwargs"]["text"] is True
