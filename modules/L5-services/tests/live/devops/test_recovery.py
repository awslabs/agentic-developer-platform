"""Synthetic driver/oracle tests; only a deployed reader can close AC-02."""

from copy import deepcopy

import pytest

from recovery import FAULTS, verify_recovery


SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}


@pytest.fixture
def recovery_case():
    return {"recovery": {"authorized_faults": True, "exclusive_agent_space": True,
                         "exclusive_worker_pool": True, "scope": SCOPE,
                         "foreign_scope": {**SCOPE, "product_id": "foreign-product-example",
                                           "project_id": "foreign-project-example"}, "binding_id": "binding-example",
                         "users": [{"id": "user-one", "credential_ref": "SECRET_USER_ONE"},
                                   {"id": "user-two", "credential_ref": "SECRET_USER_TWO"}],
                         "owned_routes": ["poc/owned-example"], "retained_routes": ["gateway/retained-example"],
                         "bounds": {"deadline_seconds": 120, "max_requests": 60, "cleanup_grace_seconds": 30},
                         "receipts": {fault: f"receipt-{fault}-example" for fault in FAULTS}}}


class FakeTarget:
    def __init__(self, *, fault=None):
        self.fault = fault
        self.sessions = []
        self.provider_calls = {}
        self.jobs = {}
        self.routes = {"poc/owned-example": "owned", "gateway/retained-example": "keep"}
        self.actions = []
        self.logged_out = False
        self.revoked = False
        self.owners = {"poc/owned-example": SCOPE}
        self.routes["gateway/other-example"] = "another-product"
        self.spaces = {"space-example": "retained"}

    def launch(self, credential, scope, kind, attempt_ref):
        self.actions.append(("launch", kind))
        if kind == "allowed":
            user_id = "user-one" if credential == "SECRET_USER_ONE" else "user-two"
            self.sessions.append({"user_id": user_id, "scope": scope,
                                  "provider_identity": user_id if self.fault != "session_alias" else "shared"})
            return {"status": 200, "scope": scope}
        if kind in {"foreign", "throttled"} and self.revoked:
            return {"status": 403}
        if kind == "logged_out" and not self.logged_out:
            return {"status": 200, "scope": scope}
        if kind == "revoked" and not self.revoked:
            return {"status": 200, "scope": scope}
        if kind == "invalid" and self.fault == "denial_provider":
            self.provider_calls[attempt_ref] = ["unexpected-provider-session"]
        if kind == "foreign" and self.fault == "foreign_leak":
            return {"status": 404, "native_session": "other-project-session"}
        return {"status": {"missing": 401, "invalid": 401, "foreign": 404, "throttled": 429,
                           "logged_out": 401, "revoked": 403}[kind]}

    def logout(self, credential):
        self.actions.append(("logout", credential))
        self.logged_out = True

    def revoke(self, credential):
        self.actions.append(("revoke", credential))
        self.revoked = True

    def inject_fault(self, fault, scope, receipt):
        self.actions.append(("inject", fault))
        self.jobs[receipt] = {"scope": scope, "receipt": receipt, "status": "completed",
                              "work_id": f"work-{fault}", "native_job_id": f"job-{fault}",
                              "native_submit_count": 2 if self.fault == "duplicate" else 1}

    def submit(self, scope, fault, receipt):
        self.actions.append(("submit", fault))
        raise TimeoutError("ack uncertain; read durable work before resubmitting")

    def restart_worker(self, fault):
        self.actions.append(("restart", fault))

    def cleanup_routes(self, scope, routes):
        self.actions.append(("cleanup", tuple(routes)))
        if self.fault != "cleanup_missing":
            for route in routes:
                self.routes.pop(route)
        if self.fault == "delete_unrelated":
            self.routes.pop("gateway/retained-example")
        if self.fault == "delete_other":
            self.routes.pop("gateway/other-example")
        if self.fault == "delete_space":
            self.spaces.pop("space-example")


class FakeReader:
    def __init__(self, target):
        self.target = target

    def read_sessions(self, scope):
        return deepcopy(self.target.sessions)

    def read_provider_calls(self, attempt_ref):
        return deepcopy(self.target.provider_calls.get(attempt_ref, []))

    def read_work(self, scope, receipt):
        return deepcopy(self.target.jobs.get(receipt))

    def read_route_inventory(self, scope):
        return deepcopy(self.target.routes)

    def read_spaces(self, scope):
        return deepcopy(self.target.spaces)

    def read_route_owners(self, scope, routes):
        return {route: self.target.owners[route] for route in routes if route in self.target.owners}

    def read_routes(self, scope, routes):
        return {route: self.target.routes[route] for route in routes if route in self.target.routes}


def test_recovery_exercises_denial_reconciliation_and_owned_cleanup(recovery_case):
    target = FakeTarget()
    proof = verify_recovery(recovery_case, target, FakeReader(target), clock=lambda: 10)
    assert {session["provider_identity"] for session in proof["sessions"]} == {"user-one", "user-two"}
    assert len(proof["denials"]) == 6
    steps = target.actions
    assert max(steps.index(("launch", kind)) for kind in ("foreign", "throttled")) < steps.index(
        ("revoke", "SECRET_USER_TWO"))
    assert steps.index(("logout", "SECRET_USER_ONE")) < steps.index(("launch", "logged_out"))
    assert steps.index(("revoke", "SECRET_USER_TWO")) < steps.index(("launch", "revoked"))
    assert set(proof["recovery"]) == set(FAULTS)
    assert [action for action in target.actions if action[0] == "submit"] == [
        ("submit", fault) for fault in FAULTS]
    assert target.routes == {"gateway/retained-example": "keep",
                             "gateway/other-example": "another-product"}
    assert target.actions[-1] == ("cleanup", ("poc/owned-example",))


@pytest.mark.parametrize("fault", ["denial_provider", "foreign_leak", "duplicate", "session_alias",
                                    "cleanup_missing", "delete_unrelated", "delete_other", "delete_space"])
def test_recovery_rejects_wrong_scope_duplicate_work_and_cleanup_failures(recovery_case, fault):
    target = FakeTarget(fault=fault)
    with pytest.raises(ValueError):
        verify_recovery(recovery_case, target, FakeReader(target), clock=lambda: 10)
    assert any(action[0] == "cleanup" for action in target.actions)


def test_recovery_rejects_shared_receipt_before_actions(recovery_case):
    target = FakeTarget()
    recovery_case["recovery"]["receipts"]["worker"] = recovery_case["recovery"]["receipts"]["event"]
    with pytest.raises(ValueError, match="receipts"):
        verify_recovery(recovery_case, target, FakeReader(target))
    assert target.actions == []


def test_recovery_cleans_owned_routes_on_deadline_failure(recovery_case):
    target = FakeTarget()
    with pytest.raises(ValueError, match="deadline"):
        verify_recovery(recovery_case, target, FakeReader(target), clock=iter([0, 121]).__next__)
    assert target.actions == [("cleanup", ("poc/owned-example",))]


def test_recovery_rejects_foreign_poc_route_without_deleting_it(recovery_case):
    target = FakeTarget()
    target.owners["poc/owned-example"] = {**SCOPE, "project_id": "foreign-example"}
    with pytest.raises(ValueError, match="ownership"):
        verify_recovery(recovery_case, target, FakeReader(target))
    assert target.actions == []
    assert target.routes["poc/owned-example"] == "owned"


def test_recovery_rejects_missing_owned_route_readback(recovery_case):
    target = FakeTarget()
    target.routes.pop("poc/owned-example")
    with pytest.raises(ValueError, match="ownership"):
        verify_recovery(recovery_case, target, FakeReader(target))
    assert target.actions == []
