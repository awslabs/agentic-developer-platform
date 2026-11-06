"""Offline doubles prove that a live handoff driver cannot infer a callback from UI state."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift.product_handoffs import (
    finish_admin_handoff,
    start_admin_handoff,
)


@pytest.fixture
def fixture():
    return {
        "run_id": "run-owned",
        "tenant_id": "tenant-owned",
        "actor_id": "actor-owned",
        "product_id": "product-owned",
        "project_id": "project-owned",
        "owner": {"access_token": "synthetic-owner-token"},
    }


class FakeRequest:
    def __init__(self, fixture):
        self.fixture = fixture
        self.finished = False
        self.requests = []
        self.session = {
            "id": "session-owned",
            "tenant_id": fixture["tenant_id"],
            "product_id": fixture["product_id"],
            "project_id": fixture["project_id"],
            "state": "open",
            "revision": 1,
            "operation_ids": [],
            "steps": {"financials": {"status": "in_progress", "name": "saved work"}},
            "resume_path": "/products/product-owned/projects/project-owned/onboarding-sessions/session-owned",
        }
        self.operation = {
            "id": "operation-owned",
            "state": "waiting_external",
            "tenant_id": fixture["tenant_id"],
            "product_id": fixture["product_id"],
            "project_id": fixture["project_id"],
            "resume_path": self.session["resume_path"],
        }

    def get(self, url, *, headers, timeout):
        assert (
            headers == {"Authorization": "Bearer synthetic-owner-token"}
            and timeout == 30_000
        )
        self.requests.append(("get", url))
        if url.endswith("/operations/operation-owned"):
            return SimpleNamespace(
                status=200,
                json=lambda: deepcopy(
                    {
                        **self.operation,
                        "state": "succeeded" if self.finished else "waiting_external",
                    }
                ),
            )
        if url.endswith("/onboarding-sessions/session-owned"):
            session = deepcopy(self.session)
            session["operation_ids"] = [] if self.finished else ["operation-owned"]
            return SimpleNamespace(status=200, json=lambda: session)
        raise AssertionError(f"Unexpected read: {url}")

    def post(self, url, *, data, headers, timeout):
        assert url.endswith("/onboarding-sessions/session-owned/handoffs")
        assert data == {"target": "administrator", "action": "assign_team"}
        assert (
            headers
            == {
                "Authorization": "Bearer synthetic-owner-token",
                "Idempotency-Key": "owned-handoff-key-123",
                "If-Profile-Revision": "1",
            }
            and timeout == 30_000
        )
        self.requests.append(("post", url))
        return SimpleNamespace(status=202, json=lambda: deepcopy(self.operation))


def receipt(fixture):
    return {
        "operation_id": "operation-owned",
        "tenant_id": fixture["tenant_id"],
        "status": "succeeded",
        "provider_operation_id": "owned-membership",
        "sequence": 1,
    }


def test_api_handoff_preserves_session_and_requires_observed_callback(fixture):
    request = FakeRequest(fixture)
    started = start_admin_handoff(
        request,
        "https://example.invalid",
        fixture,
        "session-owned",
        "owned-handoff-key-123",
    )
    assert (
        request.requests[-1][0] == "post"
        and started["operation_id"] == "operation-owned"
    )
    request.finished = True
    observed = finish_admin_handoff(
        request, fixture, started, lambda operation_id: receipt(fixture)
    )
    assert observed["session"]["steps"]["financials"]["name"] == "saved work"
    assert observed["callback_receipt"]["provider_operation_id"] == "owned-membership"
    assert request.requests[-1][1] == started["session_url"]


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "different-operation"},
        {"tenant_id": "other-tenant"},
        {"status": "failed"},
        {"provider_operation_id": ""},
        {"sequence": 0},
        {"nested": {"access_token": "synthetic-secret"}},
    ],
)
def test_api_handoff_rejects_missing_or_wrong_callback(fixture, change):
    request = FakeRequest(fixture)
    started = start_admin_handoff(
        request,
        "https://example.invalid",
        fixture,
        "session-owned",
        "owned-handoff-key-123",
    )
    request.finished = True
    with pytest.raises(AssertionError, match="receipt|credentials"):
        finish_admin_handoff(
            request,
            fixture,
            started,
            lambda operation_id: {**receipt(fixture), **change},
        )


def test_api_handoff_rejects_wrong_scope_before_write(fixture):
    request = FakeRequest(fixture)
    request.session["tenant_id"] = "other-tenant"
    with pytest.raises(AssertionError, match="owned"):
        start_admin_handoff(
            request,
            "https://example.invalid",
            fixture,
            "session-owned",
            "owned-handoff-key-123",
        )
    assert all(method != "post" for method, _url in request.requests)


def test_api_handoff_rejects_uncertain_completion(fixture):
    request = FakeRequest(fixture)
    started = start_admin_handoff(
        request,
        "https://example.invalid",
        fixture,
        "session-owned",
        "owned-handoff-key-123",
    )
    with pytest.raises(AssertionError, match="polling bound"):
        finish_admin_handoff(
            request,
            fixture,
            started,
            lambda _: receipt(fixture),
            timeout=0.001,
            interval=0.001,
        )


class RepositoryRequest(FakeRequest):
    def __init__(self, fixture, plan):
        super().__init__(fixture)
        self.plan = plan
        self.lost_ack = True
        self.create_calls = 0
        self.reconcile_calls = 0
        self.operation = {
            **self.operation,
            "id": "repo-operation",
            "state": "outcome_unknown",
            "repository": None,
        }

    def get(self, url, *, headers, timeout):
        if url.endswith("/operations/repo-operation"):
            assert (
                headers == {"Authorization": "Bearer synthetic-owner-token"}
                and timeout == 30_000
            )
            return SimpleNamespace(status=200, json=lambda: deepcopy(self.operation))
        return super().get(url, headers=headers, timeout=timeout)

    def post(self, url, *, data, headers, timeout):
        assert (
            headers["Authorization"] == "Bearer synthetic-owner-token"
            and timeout == 30_000
        )
        assert headers["If-Profile-Revision"] == "1"
        if url.endswith("/repository-plans"):
            assert data == {"repository_plan": self.plan}
            assert headers["Idempotency-Key"] == "owned-preview-key-123"
            return SimpleNamespace(
                status=201,
                json=lambda: {
                    "id": "preview-owned",
                    "revision": 1,
                    "repository_plan": deepcopy(self.plan),
                },
            )
        if url.endswith("/handoffs"):
            assert (
                data["preview_id"] == "preview-owned"
                and data["repository_plan"]["confirmed"] is True
            )
            assert headers["Idempotency-Key"] == "owned-confirm-key-123"
            self.create_calls += self.lost_ack
            if self.lost_ack:
                self.lost_ack = False
                raise TimeoutError("synthetic lost response")
            return SimpleNamespace(status=200, json=lambda: deepcopy(self.operation))
        assert url.endswith("/operations/repo-operation/reconcile")
        assert headers["Idempotency-Key"] == "owned-reconcile-key-123"
        self.reconcile_calls += 1
        self.operation.update(
            state="succeeded",
            repository={
                "native_id": "91",
                "coverage": "complete",
                "protections_applied": True,
                "applied_plan": {**self.plan, "confirmed": True},
            },
        )
        return SimpleNamespace(status=202, json=lambda: deepcopy(self.operation))


def repository_case(fixture):
    reference = {
        "tenant_id": fixture["tenant_id"],
        "project_id": fixture["project_id"],
        "source_connection_id": "owned-connection",
        "record_type": "repository",
        "record_id": "template-owned",
        "record_revision": "v1",
    }
    plan = {
        "connection_ref": {"kind": "github_installation", "id": "owned-connection"},
        "owner_native_id": "27",
        "name": "run-owned-project",
        "visibility": "private",
        "template_ref": reference,
        "protections_ref": {
            **reference,
            "record_id": "policy-owned",
            "record_type": "repository_protections",
        },
        "confirmed": False,
    }
    fixture.update(
        owned_repository_name=plan["name"],
        owner_native_id="27",
        expected_protections={"required_approving_review_count": 2},
    )
    return plan


def test_repository_recovery_reuses_confirmation_key_and_observes_one_effect(fixture):
    from tests.e2e.cli_uplift.product_handoffs import recover_repository

    plan = repository_case(fixture)
    request = RepositoryRequest(fixture, plan)
    observations = []

    def observe():
        observed = {
            "repositories": (
                [
                    {
                        "native_id": "91",
                        "owner_native_id": "27",
                        "name": plan["name"],
                        "visibility": "private",
                        "protections": fixture["expected_protections"],
                    }
                ]
                if request.create_calls
                else []
            ),
            "observation_ref": "private:provider-observation",
        }
        observations.append(deepcopy(observed))
        return observed

    keys = {
        "preview": "owned-preview-key-123",
        "confirm": "owned-confirm-key-123",
        "reconcile": "owned-reconcile-key-123",
    }
    result = recover_repository(
        request,
        "https://example.invalid",
        fixture,
        "session-owned",
        plan,
        keys,
        observe,
    )
    assert request.create_calls == request.reconcile_calls == 1
    assert (
        observations[0]["repositories"] == []
        and len(observations[-1]["repositories"]) == 1
    )
    assert result["operation"]["repository"]["native_id"] == "91"


@pytest.mark.parametrize(
    "change",
    [
        {"name": "other-repository"},
        {"visibility": "unknown"},
        {"template_ref": {"tenant_id": "other-tenant"}},
        {"confirmed": True},
    ],
)
def test_repository_recovery_rejects_unowned_or_unconfirmed_plan(fixture, change):
    from tests.e2e.cli_uplift.product_handoffs import recover_repository

    plan = repository_case(fixture)
    plan.update(change)
    request = RepositoryRequest(fixture, plan)
    with pytest.raises(ValueError, match="owned|references"):
        recover_repository(
            request,
            "https://example.invalid",
            fixture,
            "session-owned",
            plan,
            {
                "preview": "owned-preview-key-123",
                "confirm": "owned-confirm-key-123",
                "reconcile": "owned-reconcile-key-123",
            },
            lambda: pytest.fail("Unsafe provider request"),
        )
    assert request.create_calls == 0


@pytest.mark.parametrize(
    "change",
    [
        {"visibility": "public"},
        {"owner_native_id": "other-owner"},
        {"protections": {}},
        {"native_id": "different-id"},
    ],
)
def test_repository_recovery_rejects_unverified_provider_effect(fixture, change):
    from tests.e2e.cli_uplift.product_handoffs import recover_repository

    plan = repository_case(fixture)
    request = RepositoryRequest(fixture, plan)

    def observe():
        return {
            "repositories": (
                [
                    {
                        **{
                            "native_id": "91",
                            "owner_native_id": "27",
                            "name": plan["name"],
                            "visibility": "private",
                            "protections": fixture["expected_protections"],
                        },
                        **change,
                    }
                ]
                if request.create_calls
                else []
            ),
            "observation_ref": "private:provider-observation",
        }

    with pytest.raises(AssertionError, match="Independent provider"):
        recover_repository(
            request,
            "https://example.invalid",
            fixture,
            "session-owned",
            plan,
            {
                "preview": "owned-preview-key-123",
                "confirm": "owned-confirm-key-123",
                "reconcile": "owned-reconcile-key-123",
            },
            observe,
        )
    assert request.create_calls == 1


def test_repository_recovery_does_not_replay_a_rejected_confirmation(fixture):
    from tests.e2e.cli_uplift.product_handoffs import recover_repository

    plan = repository_case(fixture)
    request = RepositoryRequest(fixture, plan)
    original_post = request.post
    attempts = []

    def rejected(url, *, data, headers, timeout):
        if url.endswith("/handoffs"):
            attempts.append(headers["Idempotency-Key"])
            return SimpleNamespace(
                status=403, json=lambda: {"detail": "permission_denied"}
            )
        return original_post(url, data=data, headers=headers, timeout=timeout)

    request.post = rejected
    with pytest.raises(AssertionError, match="403"):
        recover_repository(
            request,
            "https://example.invalid",
            fixture,
            "session-owned",
            plan,
            {
                "preview": "owned-preview-key-123",
                "confirm": "owned-confirm-key-123",
                "reconcile": "owned-reconcile-key-123",
            },
            lambda: {
                "repositories": [],
                "observation_ref": "private:provider-observation",
            },
        )
    assert attempts == ["owned-confirm-key-123"]
    assert request.create_calls == 0
