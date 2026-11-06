import copy
import json
from types import SimpleNamespace

import pytest
from lib.codex_pr_context import checkout_review_only_pr, hydrate_pr_comment, record_context_refusal


def test_pr_comment_resolves_current_head():
    envelope = {
        "source_ref": {"repo": "aws-e/adp", "issue": 42},
        "payload": {"issue": {"number": 42, "pull_request": {"url": "ignored"}}},
    }
    pr = {
        "number": 42,
        "state": "open",
        "head": {"repo": {"full_name": "aws-e/adp"}, "ref": "agent/issue-12", "sha": "a" * 40},
        "base": {"repo": {"full_name": "aws-e/adp"}, "ref": "main"},
    }

    def run(args):
        assert args == ["gh", "api", "repos/aws-e/adp/pulls/42"]
        return SimpleNamespace(stdout=json.dumps(pr))

    assert hydrate_pr_comment(envelope, run)
    assert envelope["source_ref"]["sha"] == "a" * 40
    assert envelope["source_ref"]["pr"] == 42
    other = copy.deepcopy(envelope)
    pr["head"]["repo"]["full_name"] = "other/repo"
    with pytest.raises(ValueError):
        hydrate_pr_comment(other, run)


def test_real_issue_does_not_fetch_pr():
    assert not hydrate_pr_comment(
        {"payload": {"issue": {"number": 42}}}, lambda _: pytest.fail("unexpected lookup")
    )


@pytest.fixture
def human_merged_pr():
    envelope = {
        "channel": "github",
        "intent": {"trigger": "mentioned"},
        "correlation": {"is_human_rooted": True, "root_human_id": "owner"},
        "source_ref": {"repo": "aws-e/adp", "issue": 8172},
        "payload": {
            "issue": {"number": 8172, "pull_request": {"url": "ignored"}},
            "sender": {"type": "User", "id": 123},
            "comment": {
                "body": "@agent-codex-reviewer Review this exact merged source only.",
                "user": {"type": "User", "id": 123},
            },
        },
    }
    pr = {
        "number": 8172,
        "state": "closed",
        "merged": True,
        "merge_commit_sha": "c" * 40,
        "head": {"repo": {"full_name": "aws-e/adp"}, "ref": "codex/fix", "sha": "a" * 40},
        "base": {"repo": {"full_name": "aws-e/adp"}, "ref": "main", "sha": "b" * 40},
    }
    return envelope, pr


def test_human_can_review_exact_merged_non_agent_source(human_merged_pr):
    envelope, pr = human_merged_pr
    assert hydrate_pr_comment(envelope, lambda _: SimpleNamespace(stdout=json.dumps(pr)))
    assert envelope["payload"]["review_only"] is True
    assert envelope["source_ref"]["sha"] == pr["head"]["sha"]
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=pr["head"]["sha"])

    assert checkout_review_only_pr(envelope, run, "/work/repo") == "a" * 40
    assert calls == [
        ["git", "fetch", "--no-tags", "origin", "refs/pull/8172/head"],
        ["git", "rev-parse", "FETCH_HEAD"],
        ["git", "checkout", "--detach", "a" * 40],
    ]


@pytest.mark.parametrize(
    "change",
    [
        "bot",
        "wrong_author",
        "no_human",
        "automatic",
        "foreign",
        "unmerged",
        "bad_merge",
        "bad_base",
        "bool_number",
    ],
)
def test_review_only_refuses_untrusted_or_mismatched_context(human_merged_pr, change):
    envelope, pr = human_merged_pr
    if change == "bot":
        envelope["payload"]["sender"]["type"] = "Bot"
    elif change == "wrong_author":
        envelope["payload"]["comment"]["user"]["id"] = 456
    elif change == "no_human":
        envelope["correlation"]["is_human_rooted"] = False
    elif change == "automatic":
        envelope["intent"]["trigger"] = "pr_opened"
    elif change == "foreign":
        pr["head"]["repo"]["full_name"] = "other/repo"
    elif change == "unmerged":
        pr["merged"] = False
    elif change == "bad_merge":
        pr["merge_commit_sha"] = "main"
    elif change == "bad_base":
        pr["base"]["sha"] = "main"
    elif change == "bool_number":
        envelope["source_ref"]["issue"] = True
    before = copy.deepcopy(envelope)
    with pytest.raises(ValueError):
        hydrate_pr_comment(envelope, lambda _: SimpleNamespace(stdout=json.dumps(pr)))
    assert envelope == before


def test_changed_provider_ref_cannot_retarget_review_checkout():
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="b" * 40)

    with pytest.raises(ValueError, match="head changed"):
        checkout_review_only_pr({"source_ref": {"pr": 42, "sha": "a" * 40}}, run, "/work/repo")
    assert not any("checkout" in args for args in calls)


@pytest.mark.parametrize("unknown", [False, True])
def test_startup_failure_acknowledges_only_after_durable_failed_receipt(unknown):
    calls = []

    def report(status, fields):
        calls.append((status, fields))
        if unknown:
            raise RuntimeError("response lost")

    if unknown:
        with pytest.raises(RuntimeError):
            record_context_refusal(report, lambda: calls.append("ack"))
    else:
        record_context_refusal(report, lambda: calls.append("ack"))
    assert calls[0][0] == "failed"
    assert ("ack" in calls) is not unknown
