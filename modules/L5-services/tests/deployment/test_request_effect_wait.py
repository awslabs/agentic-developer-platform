"""Same-run wait has no future fixture IDs and cannot turn partial facts into passes."""

from types import SimpleNamespace

import pytest

from test_shared_release import environment, load, Opener  # noqa: F401

load("qualification_human_observations", "qualification_human_observations.py")
effects = load("qualification_request_effects", "qualification_request_effects.py")
waiter = load("qualification_observation_wait", "qualification_observation_wait.py")


def selected(environment):  # noqa: F811
    selection = {"product_id": "product", "project_id": "project", "service_id": "security", "binding_id": "binding",
                 "configuration_id": "configuration", "configuration_revision": 1, "check_id": "ui-cli-mcp-parity",
                 "subject": "configuration", "result_id": None, "result_revision": None, "result_run_id": None}
    request = {"selection": selection, "preparer_id": "owner", "observer_id": "owner", "evidence": "effect",
               "operation": "check", "request_sha256": "a" * 64}
    records = []
    journal = SimpleNamespace(binding={"context": waiter.release_identity(environment)}, append=lambda kind, value: records.append((kind, value)))
    pending = {"contract": waiter.CONTRACT, "status": "pending", "selector": None, "pending_challenge_ids": [],
               "release_check_completed": False, "native_qualified": False, "next_action": "Complete exact owner preparation"}
    return request, journal, pending, records


def test_preparation_wait_returns_only_unique_current_owner_challenge(environment):  # noqa: F811
    request, journal, pending, records = selected(environment)
    pending["pending_challenge_ids"] = ["00000000-0000-4000-8000-000000000001"]
    value = waiter.wait_for_preparation(journal, request, environment=environment, opener=Opener(pending))
    assert value == pending["pending_challenge_ids"][0]
    assert [kind for kind, _ in records] == ["effect-awaiting-preparation", "effect-preparation-selected"]
    assert all(item[1]["release_check_completed"] is False for item in records)


@pytest.mark.parametrize("boundary", ["already_attempted", "ambiguous", "fixture_ids", "pass"])
def test_preparation_wait_refuses_uncertain_repeats_and_forged_selection(environment, boundary):  # noqa: F811
    request, journal, pending, _ = selected(environment)
    if boundary == "already_attempted":
        pending["status"] = "ready"
    elif boundary == "ambiguous":
        pending["pending_challenge_ids"] = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
    elif boundary == "fixture_ids":
        request["challenge_id"] = "00000000-0000-4000-8000-000000000001"
    else:
        pending["release_check_completed"] = True
    with pytest.raises(ValueError):
        waiter.wait_for_preparation(journal, request, environment=environment, opener=Opener(pending))


def test_collection_timeout_is_explicit_and_never_executes_an_effect(environment):  # noqa: F811
    request, journal, pending, records = selected(environment)
    instants = iter([0, 2])
    with pytest.raises(TimeoutError, match="still pending"):
        waiter.collect_by_scope(journal, [request], environment=environment, opener=Opener(pending),
                                timeout_seconds=1, clock=lambda: next(instants), sleep=lambda _: pytest.fail("no sleep required"))
    assert [kind for kind, _ in records] == ["observation-awaiting-human", "observation-wait-expired"]


def test_effect_values_require_actual_protected_collection():
    with pytest.raises(ValueError, match="not collected"):
        _ = effects.CollectedRequestEffect("{}", object()).facts
