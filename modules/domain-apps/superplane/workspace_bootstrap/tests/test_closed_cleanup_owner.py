"""Owner publication reconciles exact reviewed bytes; it never extends enrollment."""

import copy

import pytest
from installation import closed_cleanup as owner
from superplane_bootstrap.closed_cleanup_permit import binding_digest

from .test_closed_cleanup_access import permit


class DDB:
    def __init__(self):
        self.row, self.writes, self.lose = None, 0, False

    def get_item(self, **kw):
        assert kw["ConsistentRead"]
        return {"Item": copy.deepcopy(self.row)} if self.row else {}

    def put_item(self, **kw):
        assert kw["TableName"].endswith("provider-authorities")
        assert kw["Item"]["record_id"]["S"].startswith("sp-cleanup:")
        self.writes += 1
        self.row = copy.deepcopy(kw["Item"])
        if self.lose:
            raise TimeoutError("lost permit acknowledgement")


@pytest.mark.parametrize("lost", [False, True])
def test_publish_uses_reviewed_bytes_and_reconciles_lost_write(monkeypatch, lost):
    from types import SimpleNamespace

    p = permit()
    p["authority"]["region"] = "us-east-1"
    p["permit_id"] = "sp-cleanup:" + binding_digest(p)
    monkeypatch.setattr(owner, "owner", lambda *args: None)
    ddb = DDB()
    ddb.lose = lost
    session = SimpleNamespace(client=lambda *args, **kw: ddb)
    args = ({"environment": "test"}, p, session)
    before = copy.deepcopy(p)
    result = owner.publish(*args, requalify=lambda: binding_digest(p))
    assert (
        p == before
        and not result["provider_enrollment_renewed"]
        and not result["worker_resumed"]
    )
    owner.publish(*args, requalify=lambda: binding_digest(p))
    assert ddb.writes == 1
    with pytest.raises(ValueError):
        owner.publish(*args, requalify=lambda: "drift")
    assert ddb.writes == 1
