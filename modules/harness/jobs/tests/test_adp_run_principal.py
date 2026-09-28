"""The shared grant must accept the real, verified ADP invocation/attempt subject."""

from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import ContractViolation, ResolvedPrincipal
from harness_jobs.leases import ExecutionLease


def test_adp_run_principal_can_bind_an_execution_grant():
    holder = "df616178-9e25-4c35-b668-d46a73b418bc#2"
    now = datetime.now(UTC)
    principal = ResolvedPrincipal(
        "org", "workspace", holder, frozenset({"workspace:provision"})
    )
    lease = ExecutionLease(
        operation_id="operation",
        org_id="org",
        workspace_id="workspace",
        holder=holder,
        attempt_id="admitted-attempt",
        fence_token=3,
        acquired_at=now,
        expires_at=now + timedelta(seconds=60),
        runtime_deadline=now + timedelta(seconds=120),
        attempts=2,
    )
    assert ExecutionGrant(principal, lease).lease.holder == holder


@pytest.mark.parametrize(
    "subject", ["run#0", "run#-1", "run#one", "run##1", "run#1#2", "run #1"]
)
def test_malformed_adp_attempt_subject_is_refused(subject):
    with pytest.raises(ContractViolation):
        ResolvedPrincipal("org", "workspace", subject)


@pytest.mark.parametrize("field", ["org_id", "workspace_id"])
def test_attempt_separator_does_not_widen_tenant_identifiers(field):
    values = dict(org_id="org", workspace_id="workspace", subject="run#1")
    values[field] = "foreign#1"
    with pytest.raises(ContractViolation):
        ResolvedPrincipal(**values)
