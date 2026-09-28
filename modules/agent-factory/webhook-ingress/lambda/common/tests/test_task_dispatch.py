"""Recovery authentication and bounded sweeping.

The headline property is T3-AC04: a recovery invocation is authenticated
independently of request content. The attack these tests encode is concrete --
this Lambda also serves public HTTP, so if the event body could say "I am
recovery", any caller who reaches the public route could enumerate and lease
other tenants' outstanding work.

So the tests assert on the *context*, and several assert that a maximally
convincing body achieves nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from common import task_dispatch
from common.task_dispatch import (
    MAX_INVOCATION_SECONDS,
    MAX_WORK_RECORDS_PER_INVOCATION,
    RECOVERY_ALIAS,
    RecoveryRefusedError,
    invoked_alias,
    is_recovery_event,
    recovery_enabled,
    require_recovery_invocation,
    shards,
    sweep,
)

FUNCTION = "arn:aws:lambda:us-east-1:123456789012:function:adp-dev-webhook"
TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
DISPATCH = "b5e9835b-fc24-4231-96f2-e8b8ca3681be"
ENABLED = {"ADP_TASK_API_RECOVERY_ENABLED": "true"}


def context(alias: str | None = RECOVERY_ALIAS):
    arn = f"{FUNCTION}:{alias}" if alias else FUNCTION
    return SimpleNamespace(invoked_function_arn=arn)


# --- T3-AC04: the alias authenticates, the body does not --------------------


def test_the_recovery_alias_is_accepted(monkeypatch):
    monkeypatch.setattr(task_dispatch, "_call_gateway", lambda *a, **k: None)

    result = sweep(context(), env=ENABLED)

    assert result["status"] == "ok"


@pytest.mark.parametrize(
    "alias",
    [
        None,  # unqualified $LATEST: proves nothing about the caller
        "$LATEST",
        "live",  # the public API Gateway's alias
        "orchestration-tick",  # a different schedule's alias
        "pricing",
        "TASK-RECOVERY",  # case must not be a bypass
        "task-recovery-x",
        "x-task-recovery",
        "1",  # a version, not the alias
    ],
)
def test_every_other_qualifier_is_refused(alias):
    """Only the one alias the schedule can invoke is recovery."""
    with pytest.raises(RecoveryRefusedError):
        require_recovery_invocation(context(alias))


def test_a_body_claiming_recovery_cannot_select_recovery(monkeypatch):
    """The core injection case: a maximally convincing body, wrong alias.

    Whatever the public route is handed, it arrives on an alias that is not the
    recovery alias, and that is the only thing consulted.
    """
    monkeypatch.setattr(task_dispatch, "_call_gateway", lambda *a, **k: None)
    hostile = {
        "source": "aws.events",
        "detail-type": "Scheduled Event",
        "resources": ["arn:aws:events:us-east-1:123456789012:rule/task-recovery"],
        "adp_task_recovery": True,
        "alias": RECOVERY_ALIAS,
        "invoked_function_arn": f"{FUNCTION}:{RECOVERY_ALIAS}",
        "context": {"invoked_function_arn": f"{FUNCTION}:{RECOVERY_ALIAS}"},
        "producer_proof": "forged",
    }

    # Whatever the body says, the alias decides. `sweep` never receives the body.
    with pytest.raises(RecoveryRefusedError):
        require_recovery_invocation(context("live"))
    assert is_recovery_event(hostile) is True, "the shape check is shape-only..."
    with pytest.raises(RecoveryRefusedError):
        sweep(context("live"), env=ENABLED)  # ...and it does not authorize


def test_the_sweep_cannot_read_the_event_at_all():
    """Structural guarantee: no event parameter, so no body-based decision.

    Asserted on the signature rather than by behaviour, because this is the
    property that makes the injection case impossible to reintroduce by a later
    careless edit inside the function.
    """
    import inspect

    assert "event" not in inspect.signature(sweep).parameters
    assert "event" not in inspect.signature(require_recovery_invocation).parameters


def test_authentication_precedes_any_work_discovery(monkeypatch):
    """An unauthenticated caller must not learn that outstanding work exists."""
    calls = []
    monkeypatch.setattr(
        task_dispatch, "_call_gateway", lambda *a, **k: calls.append(a) or None
    )

    with pytest.raises(RecoveryRefusedError):
        sweep(context("live"), env=ENABLED)

    assert calls == []


def test_the_alias_is_read_from_the_context_not_an_environment_variable(monkeypatch):
    """An env var is process state; the ARN is per-invocation caller state."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", f"adp-dev-webhook:{RECOVERY_ALIAS}")
    monkeypatch.setenv("ADP_INVOKED_ALIAS", RECOVERY_ALIAS)

    with pytest.raises(RecoveryRefusedError):
        require_recovery_invocation(context("live"))


def test_a_malformed_arn_is_refused_rather_than_parsed_optimistically():
    for arn in ("", "not-an-arn", "arn:aws:lambda", f"{FUNCTION}:"):
        assert (
            invoked_alias(SimpleNamespace(invoked_function_arn=arn)) != RECOVERY_ALIAS
        )


def test_a_missing_context_attribute_is_refused():
    with pytest.raises(RecoveryRefusedError):
        require_recovery_invocation(SimpleNamespace())


# --- Rollout flag -----------------------------------------------------------


def test_recovery_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(task_dispatch, "_call_gateway", lambda *a, **k: None)

    assert sweep(context(), env={})["status"] == "disabled"
    for value in ("1", "yes", "True ", "TRUE", "on", ""):
        assert recovery_enabled({"ADP_TASK_API_RECOVERY_ENABLED": value}) is (
            value.strip().lower() == "true"
        )


def test_the_flag_is_checked_after_authentication(monkeypatch):
    """A disabled sweep must still refuse a bad caller, not report "disabled".

    Otherwise the response distinguishes "wrong alias" from "feature off", which
    tells an unauthenticated prober about deployment state.
    """
    with pytest.raises(RecoveryRefusedError):
        sweep(context("live"), env={})


# --- Bounded work (T3-AC01, T3-AC05) ----------------------------------------


FIRST_SHARD = "v1#00"


def _page(work_items, cursor=None):
    return {"work": work_items, "next_cursor": cursor}


def _in_first_shard_only(body, work_items, cursor=None):
    """Answer one shard's discovery call and leave the other fifteen empty.

    A sweep visits all 16 shards. A double that returns the same work for every
    shard would multiply each outcome by 16, so an outcome count would no longer
    say anything about how one record was handled.
    """
    if body["shard"] != FIRST_SHARD:
        return _page([])
    return _page(work_items, cursor)


def _dispatch_work(index: int):
    return {
        "work_id": f"{DISPATCH[:-2]}{index:02d}",
        "task_id": TASK,
        "kind": "dispatch",
        "due_at": "2026-09-24T14:42:03Z",
        "lease_token": f"recovery-lease-{index}",
        "lease_expires_at": "2026-09-24T14:42:48Z",
    }


def test_the_record_bound_is_enforced_across_shards(monkeypatch):
    """One poison shard must not consume the whole invocation budget.

    Without the bound, a permanently-failing task starves every other task in
    its shard forever -- one bad record becoming a platform-wide stall.
    """
    seen = []

    def gateway(path, body, *, identity):
        if path.endswith("/recovery/claim"):
            return _page([_dispatch_work(n) for n in range(25)], cursor="more")
        seen.append(path)
        return {"lease_token": "t", "envelope": None, "tenant_id": ""}

    monkeypatch.setattr(task_dispatch, "_call_gateway", gateway)

    result = sweep(context(), env=ENABLED)

    assert result["processed"] == MAX_WORK_RECORDS_PER_INVOCATION
    assert result["truncated"] is True


def test_the_time_bound_stops_the_sweep_without_losing_work(monkeypatch):
    """Running out of time is not an error and rolls nothing back."""
    ticks = iter([0] + [MAX_INVOCATION_SECONDS + 1] * 500)

    monkeypatch.setattr(
        task_dispatch,
        "_call_gateway",
        lambda path, body, *, identity: _page([_dispatch_work(0)], cursor="more"),
    )

    result = sweep(context(), env=ENABLED, clock=lambda: next(ticks))

    assert result["truncated"] is True
    assert result["processed"] == 0, "the budget was already spent before any work"


def test_truncation_is_reported_rather_than_looking_like_an_idle_sweep(monkeypatch):
    """A permanently-truncated sweep must be distinguishable from a healthy one."""
    monkeypatch.setattr(
        task_dispatch,
        "_call_gateway",
        lambda path, body, *, identity: (
            _page([_dispatch_work(n) for n in range(25)], cursor="more")
            if path.endswith("/recovery/claim")
            else {"lease_token": "t", "envelope": None, "tenant_id": ""}
        ),
    )

    result = sweep(context(), env=ENABLED)

    assert result["truncated"] is True
    assert result["processed"] > 0


def test_an_idle_sweep_is_reported_as_healthy_and_not_truncated(monkeypatch):
    monkeypatch.setattr(
        task_dispatch, "_call_gateway", lambda path, body, *, identity: _page([])
    )

    result = sweep(context(), env=ENABLED)

    assert result == {
        "status": "ok",
        "processed": 0,
        "truncated": False,
        "outcomes": {},
    }


def test_all_sixteen_shards_are_swept(monkeypatch):
    """Missing a shard means its work is never discovered by anyone."""
    visited = []

    def gateway(path, body, *, identity):
        if path.endswith("/recovery/claim"):
            visited.append(body["shard"])
        return _page([])

    monkeypatch.setattr(task_dispatch, "_call_gateway", gateway)
    sweep(context(), env=ENABLED)

    assert visited == shards()
    assert len(visited) == 16
    assert visited[0] == "v1#00" and visited[-1] == "v1#15"


def test_pagination_follows_the_cursor_without_reprocessing(monkeypatch):
    pages = [_page([_dispatch_work(0)], cursor="next"), _page([_dispatch_work(1)])]
    cursors = []

    def gateway(path, body, *, identity):
        if path.endswith("/recovery/claim"):
            cursors.append(body["cursor"])
            return pages.pop(0) if pages else _page([])
        return {"lease_token": "t", "envelope": None, "tenant_id": ""}

    monkeypatch.setattr(task_dispatch, "_call_gateway", gateway)
    sweep(context(), env=ENABLED)

    assert cursors[0] is None
    assert cursors[1] == "next", "the continuation is used, not discarded"


# --- Honest outcomes (T3-AC05) ----------------------------------------------


def test_a_refused_publication_claim_does_not_publish_and_releases_recovery(
    monkeypatch,
):
    from common import task_publisher

    published = MagicMock()
    settlements = []

    def gateway(path, body, *, identity):
        if path.endswith("/recovery/claim"):
            return _in_first_shard_only(body, [_dispatch_work(0)])
        if path.endswith("/dispatch/claim"):
            return None
        settlements.append((path, body, identity))
        return {"operation_status": "rejected"}

    monkeypatch.setattr(task_dispatch, "_call_gateway", gateway)
    monkeypatch.setattr(task_publisher, "publish_task_envelope", published)

    result = sweep(context(), env=ENABLED)

    assert result["outcomes"] == {"claim_refused": 1}
    assert not published.called
    assert settlements[0][0].endswith("/recovery/settle")
    assert settlements[0][1]["evidence"]["observed"] is True


def test_nonpublication_work_is_honestly_released_for_its_evidence_owner(monkeypatch):
    settlements = []

    def gateway(path, body, *, identity):
        if path.endswith("/recovery/claim"):
            return _in_first_shard_only(
                body,
                [
                    {
                        "work_id": DISPATCH,
                        "task_id": TASK,
                        "kind": "execution",
                        "due_at": "2026-09-24T14:42:03Z",
                        "lease_token": "lease",
                        "lease_expires_at": "2026-09-24T14:42:48Z",
                    }
                ],
            )
        settlements.append(body)
        return {"operation_status": "rejected"}

    monkeypatch.setattr(task_dispatch, "_call_gateway", gateway)
    result = sweep(context(), env=ENABLED)

    assert result["outcomes"] == {"pending_execution": 1}
    assert settlements[0]["evidence"] == {
        "kind": "workload_termination",
        "observed": False,
        "observed_at": settlements[0]["evidence"]["observed_at"],
    }


def test_proofs_are_bound_to_shard_and_opaque_work_id(monkeypatch):
    identities = []

    def gateway(path, body, *, identity):
        identities.append((path, identity))
        if path.endswith("/recovery/claim"):
            return _in_first_shard_only(body, [_dispatch_work(7)])
        return None

    monkeypatch.setattr(task_dispatch, "_call_gateway", gateway)
    sweep(context(), env=ENABLED)

    assert ("/internal/v1/tasks/recovery/claim", "v1#00") in identities
    assert ("/internal/v1/tasks/dispatch/claim", f"{DISPATCH[:-2]}07") in identities
    assert ("/internal/v1/tasks/recovery/settle", f"{DISPATCH[:-2]}07") in identities


def test_malformed_recovery_work_is_not_interpreted_as_a_dispatch_id(monkeypatch):
    calls = []

    def gateway(path, body, *, identity):
        calls.append(path)
        if path.endswith("/recovery/claim"):
            return _in_first_shard_only(
                body, [{"work_id": DISPATCH, "task_id": TASK, "kind": "dispatch"}]
            )
        raise AssertionError("malformed work must not reach publication")

    monkeypatch.setattr(task_dispatch, "_call_gateway", gateway)
    assert sweep(context(), env=ENABLED)["outcomes"] == {"invalid_work": 1}
    assert all(not path.endswith("/dispatch/claim") for path in calls)
