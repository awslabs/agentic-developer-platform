"""Offline checks for the independent Product readback oracle."""

import pytest

from tests.e2e.new_ui.test_product_profile import assert_saved_profile


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def json(self):
        return self.body


PROFILE = {
    "product_id": "owned", "project_id": "project", "revision": 1,
    "product": {"name": "owned-name", "purpose": "reason", "intended_users": ["team"]},
}


def test_readback_requires_independent_committed_values():
    assert assert_saved_profile(Response(200, PROFILE), "owned", "project", "owned-name", "reason", ["team"]) == PROFILE
    for field in ("product_id", "project_id", "name", "purpose", "intended_users"):
        changed = {**PROFILE, "product": {**PROFILE["product"]}}
        parent = changed["product"] if field in changed["product"] else changed
        parent[field] = ["wrong"] if field == "intended_users" else "wrong"
        with pytest.raises(AssertionError):
            assert_saved_profile(Response(200, changed), "owned", "project", "owned-name", "reason", ["team"])
    with pytest.raises(AssertionError, match="HTTP 503"):
        assert_saved_profile(Response(503, {}), "owned", "project", "owned-name", "reason", ["team"])
