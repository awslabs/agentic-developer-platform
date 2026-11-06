import json

import pytest

from lib.codex_read_only_delivery import publish
from lib.codex_review_delivery import ReviewDeliveryBlocked, require_delivery_success


def test_publication_uses_scoped_reviewer_and_preserves_provider_response():
    value = {
        "repo": "aws-e/adp",
        "pr_number": 8172,
        "event": "APPROVE",
        "head": "a" * 40,
        "body": "actual review",
    }
    calls = []

    def mint(**kwargs):
        calls.append(kwargs)
        return "private-test-token", "reviewer"

    def submit(**kwargs):
        assert kwargs["token"] == "private-test-token"
        assert kwargs["commit_id"] == "a" * 40
        return {
            "verdict_recorded": False,
            "state": "COMMENTED",
            "commit_id": "b" * 40,
            "review_id": 3,
        }

    result = publish(value, mint=mint, submit=submit)
    assert calls == [{"repo": "aws-e/adp"}]
    assert result["commit_id"] == "b" * 40 and result["verdict_recorded"] is False
    assert "token" not in result


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"verdict_recorded": False},
        {"state": "COMMENTED"},
        {"commit_id": "b" * 40},
        {"review_id": True},
    ],
)
def test_source_review_requires_actual_provider_verdict_without_merge(changes):
    receipt = {
        "verdict_recorded": True,
        "state": "APPROVED",
        "commit_id": "a" * 40,
        "review_id": 3,
        **changes,
    }
    envelope = {"source_ref": {"pr": 8172, "sha": "a" * 40}, "payload": {"review_only": True}}
    result = {
        "status": "read_only_reviewed",
        "sha": "a" * 40,
        "verdict": "approve",
        "submission": receipt,
    }
    if changes:
        with pytest.raises(ReviewDeliveryBlocked):
            require_delivery_success(json.dumps(result), envelope)
    else:
        require_delivery_success(json.dumps(result), envelope)
