"""Native owner publications reconcile exact lost acknowledgements without replay."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from installation import bootstrap_recovery as owner


@pytest.fixture
def publication():
    schema = owner.evidence_contract()
    now = datetime.now(timezone.utc)
    base = dict(
        org_id="org",
        workspace_id="workspace",
        operation_id="operation",
        plan_digest="a" * 64,
        source_key="source",
        source_fence=3,
        source_audit={
            "log_group": "logs",
            "creation_event_id": "create",
            "terminal_event_ids": ["done"],
        },
        checked_at=(now - timedelta(hours=2)).isoformat(),
        expires_at=(now - timedelta(hours=1)).isoformat(),
    )
    base["record_id"] = schema.base_record_id(base)
    fresh = {
        **copy.deepcopy(base),
        "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }
    base_item = {
        "record_id": {"S": base["record_id"]},
        "document": {"S": schema.canonical(base)},
        "revision": {"S": schema.digest(base)},
    }
    state = SimpleNamespace(
        items={base["record_id"]: base_item},
        puts=0,
        collects=0,
        saved=[],
        fault=None,
        locks=0,
    )
    installer = SimpleNamespace(
        owner="installation", receipt={"remote_lock": {"etag": "original"}}
    )

    def save():
        state.saved.append(copy.deepcopy(installer.receipt))

    def reconcile():
        state.locks += 1
        if state.fault == "lost-lock" and state.locks > 1:
            return None
        return installer.receipt["remote_lock"]

    installer.save, installer.reconcile_lock_attempt = save, reconcile

    class Client:
        def get_item(self, **kwargs):
            assert kwargs["ConsistentRead"] is True
            value = state.items.get(kwargs["Key"]["record_id"]["S"])
            return {} if value is None else {"Item": copy.deepcopy(value)}

        def put_item(self, **kwargs):
            assert kwargs["ConditionExpression"] == "attribute_not_exists(record_id)"
            state.puts += 1
            value = copy.deepcopy(kwargs["Item"])
            assert (
                state.saved[-1]["bootstrap_recovery_publication_intent"]["item"]
                == value
            )
            key = value["record_id"]["S"]
            if state.fault == "absent-ack":
                raise OSError("unknown result")
            if state.fault == "race":
                competitor = schema.decode_item(value, key)
                competitor["source_audit"]["historical_environment"] = {
                    "different": True
                }
                value["document"]["S"] = schema.canonical(competitor)
                value["revision"]["S"] = schema.digest(competitor)
            assert key not in state.items
            state.items[key] = value
            if state.fault in {"lost-ack", "race"}:
                raise OSError("unknown result")

    client = Client()
    session = SimpleNamespace(client=lambda *args, **kwargs: client)
    enrollment = {
        "environment": "test",
        "authority": {"installation_id": "installation", "region": "us-east-1"},
    }

    def collect(*args):
        state.collects += 1
        if state.fault == "changed-source":
            return {**fresh, "source_principal": "changed"}
        if state.fault == "append-during-collect":
            child = schema.renew(fresh, base)
            state.items[child["record_id"]] = {
                "record_id": {"S": child["record_id"]},
                "document": {"S": schema.canonical(child)},
                "revision": {"S": schema.digest(child)},
            }
        return copy.deepcopy(fresh)

    def run(**kwargs):
        return owner.record(
            installer,
            enrollment,
            {},
            base,
            session,
            evidence_collector=collect,
            **kwargs,
        )

    return SimpleNamespace(
        run=run,
        state=state,
        installer=installer,
        base=base,
        fresh=fresh,
        original=copy.deepcopy(base_item),
        schema=schema,
    )


@pytest.mark.parametrize("fault", [None, "lost-ack"])
def test_append_and_lost_ack_preserve_original_and_replay_only_exact_intent(
    publication, fault
):
    p = publication
    p.state.fault = fault
    result = p.run(check_only=False)
    assert result["state"] == "bootstrap-boundary-recorded"
    assert result["record_id"] != p.base["record_id"]
    assert p.state.puts == p.state.collects == 1
    assert p.state.items[p.base["record_id"]] == p.original
    before = copy.deepcopy(p.installer.receipt["bootstrap_recovery_publication_intent"])
    assert p.run(check_only=False) == result
    assert p.state.puts == p.state.collects == 1
    assert p.installer.receipt["bootstrap_recovery_publication_intent"] == before
    assert p.state.locks >= 5


@pytest.mark.parametrize("fault", ["race", "absent-ack"])
def test_uncertain_intent_is_observe_only_and_cannot_adopt_a_different_winner(
    publication, fault
):
    p = publication
    p.state.fault = fault
    with pytest.raises(ValueError):
        p.run(check_only=False)
    before = copy.deepcopy(p.installer.receipt["bootstrap_recovery_publication_intent"])
    assert "bootstrap_recovery_evidence" not in p.installer.receipt
    with pytest.raises(ValueError):
        p.run(check_only=False)
    assert p.state.puts == p.state.collects == 1
    assert p.installer.receipt["bootstrap_recovery_publication_intent"] == before
    assert p.state.items[p.base["record_id"]] == p.original


@pytest.mark.parametrize(
    "fault",
    [
        "lost-lock",
        "changed-source",
        "append-during-collect",
        "legacy-receipt",
        "fresh-predecessor",
    ],
)
def test_changed_lock_source_chain_or_completed_run_refuses_before_publication(
    publication, fault
):
    p = publication
    p.state.fault = fault
    if fault == "legacy-receipt":
        p.installer.receipt["bootstrap_recovery_evidence"] = copy.deepcopy(p.base)
    elif fault == "fresh-predecessor":
        now = datetime.now(timezone.utc)
        p.base.update(
            checked_at=now.isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        value = p.state.items[p.base["record_id"]]
        value.update(
            document={"S": p.schema.canonical(p.base)},
            revision={"S": p.schema.digest(p.base)},
        )
    with pytest.raises(ValueError):
        p.run(check_only=False)
    assert p.state.puts == 0
    assert "bootstrap_recovery_publication_intent" not in p.installer.receipt
    if fault in {"legacy-receipt", "fresh-predecessor"}:
        assert p.state.collects == 0


def test_preview_requalifies_without_publishing_intent_or_records(publication):
    p = publication
    result = p.run(check_only=True)
    assert result["state"] == "bootstrap-boundary-reviewed"
    assert result["evidence"]["renewal"]["generation"] == 1
    assert p.state.collects == 1 and p.state.puts == 0
    assert "bootstrap_recovery_publication_intent" not in p.installer.receipt
    assert len(p.state.items) == 1


def test_first_publication_keeps_legacy_record_identity(publication):
    p = publication
    p.state.items.clear()
    result = p.run(check_only=False)
    assert result["record_id"] == p.base["record_id"]
    assert "renewal" not in p.installer.receipt["bootstrap_recovery_evidence"]
    assert p.state.puts == p.state.collects == 1


@pytest.mark.parametrize("fault", [None, "lost-ack"])
def test_large_first_publication_is_lossless_and_uncertain_write_is_not_replayed(
    publication, fault
):
    p = publication
    p.state.items.clear()
    p.state.fault = fault
    p.fresh["retained_history_example"] = (
        "original immutable journal; original approval; " * 35000
    )
    result = p.run(check_only=False)
    item = p.state.items[result["record_id"]]
    assert len(item["document"]["S"].encode()) < p.schema.MAX_STORED_BYTES
    assert p.schema.decode_item(item, result["record_id"]) == p.fresh
    assert item["revision"]["S"] == p.schema.digest(p.fresh)
    assert p.run(check_only=False) == result
    assert p.state.puts == p.state.collects == 1
