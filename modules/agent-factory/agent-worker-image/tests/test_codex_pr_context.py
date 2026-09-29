import copy
import json
from types import SimpleNamespace

import pytest
from lib.codex_pr_context import hydrate_pr_comment


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
