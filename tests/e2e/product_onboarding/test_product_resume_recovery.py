"""Synthetic recovery checks; deployed mutation and cleanup remain unverified."""

from copy import deepcopy

import pytest

from tests.e2e.new_ui.product_recovery import ProductRun
from tests.e2e.new_ui.product_session_recovery import SessionRecovery, assert_cleanup_receipt

RUN_ID = "product-123-1"
SOURCE = "a" * 40


class Store:
    def __init__(self, document, manifest):
        self.document, self.manifest = document, manifest
        self.fail_save = False

    def load(self, run_id):
        assert run_id == RUN_ID
        return deepcopy(self.document), deepcopy(self.manifest)

    def save(self, run_id, document, manifest=None):
        assert run_id == RUN_ID
        if self.fail_save:
            raise RuntimeError("durable store unavailable")
        self.document, self.manifest = deepcopy(document), deepcopy(manifest)


@pytest.fixture
def recovery_fixture():
    fixture = {"run_id": RUN_ID, "tenant_id": "tenant", "actor_id": "owner", "source_revision": SOURCE,
               "product_id": "product", "project_id": "project"}
    document = {**{key: fixture[key] for key in ("run_id", "tenant_id", "actor_id", "source_revision")},
                "receipt": {"product_id": "product", "project_id": "project"}}
    fixture["state_store"] = Store(document, {**fixture, "state_store": None})
    return fixture


@pytest.fixture
def recovery_state(recovery_fixture):
    state = {"id": "session", "revision": 2,
             "steps": {"financials": {"status": "in_progress"}}, "changes": {"created", "selected"}}
    recovery = SessionRecovery(recovery_fixture,
                               lambda session_id: {key: deepcopy(value) for key, value in state.items() if key != "changes"},
                               lambda: set(state["changes"]))
    return state, recovery


def commit_once(state):
    state["revision"] = 3
    state["steps"]["financials"] = {"status": "later", "reason": "Deferred"}
    state["changes"].add("later-change")


@pytest.mark.parametrize("committed_before_restart", (False, True))
def test_recovery_replays_only_an_uncommitted_intent(recovery_state, committed_before_restart):
    state, recovery = recovery_state
    pending = recovery.record("session", 2, "original_later_key_123", {"action": "later", "reason": "Deferred"})
    assert pending == recovery.owned()[0]["pending_onboarding"]
    replayed = []
    if committed_before_restart:
        commit_once(state)

    def replay(original):
        replayed.append(original)
        commit_once(state)

    result = recovery.reconcile(replay)
    assert result["session_id"] == "session" and result["revision"] == 3
    assert result["change_id"] == "later-change" and len(result["attempts"]) == 2
    assert len(replayed) == (0 if committed_before_restart else 1)
    if replayed:
        assert replayed[0]["key"] == "original_later_key_123"
        assert replayed[0]["revision"] == 2 and replayed[0]["body"] == {"action": "later", "reason": "Deferred"}
    assert recovery.owned()[0]["onboarding_result"] == result
    with pytest.raises(ValueError, match="already reconciled"):
        recovery.reconcile(replay)


def test_recovery_refuses_lost_pending_reference_or_missing_store(recovery_state):
    _, recovery = recovery_state
    with pytest.raises(ValueError, match="pending"):
        recovery.reconcile(lambda _: pytest.fail("no intent to replay"))
    recovery.store.fail_save = True
    with pytest.raises(RuntimeError, match="durable store"):
        recovery.record("session", 2, "original_later_key_123", {"action": "later", "reason": "Deferred"})


def test_recovery_refuses_duplicate_mutations_and_wrong_ownership(recovery_state):
    state, recovery = recovery_state
    recovery.record("session", 2, "original_later_key_123", {"action": "later", "reason": "Deferred"})
    state["changes"].update({"first-change", "second-change"})
    state["revision"] = 3
    state["steps"]["financials"] = {"status": "later", "reason": "Deferred"}
    with pytest.raises(AssertionError, match="exactly once"):
        recovery.reconcile(lambda _: pytest.fail("committed write must never replay"))
    recovery.fixture["project_id"] = "wrong-project"
    with pytest.raises(ValueError, match="original owned"):
        recovery.reconcile(lambda _: pytest.fail("foreign state must never replay"))


def test_recovery_cleanup_requires_owned_absence_and_shared_preservation(recovery_fixture):
    receipt = {"run_id": RUN_ID, "product_id": "product", "project_id": "project",
               "fixture_removed": True, "owned_absent": True, "shared_unchanged": True}
    assert_cleanup_receipt(receipt, recovery_fixture)
    for key in ("run_id", "product_id", "project_id", "fixture_removed", "owned_absent", "shared_unchanged"):
        invalid = {**receipt, key: None}
        with pytest.raises(AssertionError, match="cleanup"):
            assert_cleanup_receipt(invalid, recovery_fixture)


@pytest.mark.parametrize("case", ("resume_ui", "resume_cli", "progress", "recovery"))
def test_v05_owned_product_names_preserve_v01_guard(recovery_fixture, case):
    body = {"name": f"eval-product-{RUN_ID}-{case}", "purpose": "Recovery",
            "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}}
    args = {"run_id": RUN_ID, "tenant_id": "tenant", "actor_id": "owner",
            "source_revision": SOURCE, "owner_token": "test-only-token", "request_body": body}
    ProductRun(recovery_fixture["state_store"], lambda _: None, **args, name_suffix="-" + case)
    with pytest.raises(ValueError, match="name differs"):
        ProductRun(recovery_fixture["state_store"], lambda _: None, **args)


@pytest.mark.parametrize("case", ("v11_idea", "v11_existing_assets", "v11_running_product", "v11_resume", "v11_release"))
def test_v11_owned_product_names_preserve_original_guard(recovery_fixture, case):
    body = {"name": f"eval-product-{RUN_ID}-{case}", "purpose": "Recovery",
            "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}}
    args = {"run_id": RUN_ID, "tenant_id": "tenant", "actor_id": "owner",
            "source_revision": SOURCE, "owner_token": "test-only-token", "request_body": body}
    ProductRun(recovery_fixture["state_store"], lambda _: None, **args, name_suffix="-" + case)
    with pytest.raises(ValueError, match="name differs"):
        ProductRun(recovery_fixture["state_store"], lambda _: None, **args)
