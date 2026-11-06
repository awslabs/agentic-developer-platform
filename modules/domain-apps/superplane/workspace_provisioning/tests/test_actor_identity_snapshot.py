"""Independent SDK identity reads overlap, without caching operation authority."""

from threading import Barrier
from types import SimpleNamespace

import pytest

from workspace_provisioning.credentials import canonical_role_identity_snapshot
from workspace_provisioning.runtime_config import LifecycleRefused


@pytest.mark.parametrize("fault", [None, "recreated", "denied", "withdrawn"])
def test_fresh_actor_reads_are_bracketed_and_complete(fault):
    actors = ("registrar", "installer", "supervisor")
    roles = {a: "arn:aws:iam::123456789012:role/" + a for a in actors}
    barrier = Barrier(6)
    calls, checks = [], []

    def identity(actor):
        calls.append(("sts", actor))
        barrier.wait(timeout=10)
        return {"Account": "123456789012", "UserId": "immutable-" + actor + ":session"}

    def role(*, RoleName):
        calls.append(("iam", RoleName))
        barrier.wait(timeout=10)
        if fault == "denied" and RoleName == "installer":
            raise PermissionError("synthetic denial")
        return {
            "Role": {
                "Arn": roles[RoleName],
                "RoleId": "replacement"
                if fault == "recreated" and RoleName == "installer"
                else "immutable-" + RoleName,
            }
        }

    def verify():
        checks.append(True)
        if fault == "withdrawn" and len(checks) == 2:
            raise LifecycleRefused("synthetic withdrawal during reads")

    sessions = {
        a: SimpleNamespace(
            client=lambda service, actor=a: SimpleNamespace(
                get_caller_identity=lambda: identity(actor)
            )
        )
        for a in actors
    }
    source = SimpleNamespace(client=lambda service: SimpleNamespace(get_role=role))
    reader = canonical_role_identity_snapshot(sessions, source, roles, verify=verify)
    if fault:
        with pytest.raises(PermissionError if fault == "denied" else LifecycleRefused):
            reader()
        assert len(calls) == 6
    else:
        for _ in range(2):
            values = reader()
            assert {a: v.principal_arn for a, v in values.items()} == roles
        assert len(calls) == 12 and len(checks) == 4
