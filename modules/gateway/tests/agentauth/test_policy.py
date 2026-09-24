"""The shared policy both agent adapters call (#5028 AC1, AC2, AC3, AC6, AC7).

The two cases that matter most are adjacent on purpose:

- ``TestTwoWorkersSharingOneRole`` is AC2 as a runnable test. Two callers with
  genuinely-issued credentials, same IAM role, cannot reach each other.
- ``TestLegitimateFlowStillWorks`` is AC1. Refusal tests that broke the flow they
  protect would be worse than no tests, so the allowed path is asserted too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.grants import (
    AgentAction,
    AuthorityReference,
    DelegatedGrant,
    TargetFacts,
    TargetRelationship,
)
from src.agentauth.policy import (
    REFUSED_STATUS,
    THROTTLED_STATUS,
    UNSUPPORTED_STATUS,
    AgentAuthorizationService,
    PolicyError,
    is_live_control,
)
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV, mint_credential

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
TENANT = "org-tenant-001"
ENV = {CREDENTIAL_KEY_ENV: "policy-test-key-not-a-real-secret"}

# The verb set this deployment implements, written out rather than imported.
# Importing it would make the pin below tautological; spelling it here means a
# change to the module attribute has to be made deliberately in two places. The
# tests that need a verb the deployment does *not* implement subtract from this
# set instead of naming one, so implementing another verb cannot invalidate them
# the way #3965 invalidated every assertion that had borrowed ``steer``.
SHIPPED_SUPPORTED_ACTIONS = frozenset({AgentAction.MONITOR, AgentAction.PAUSE, AgentAction.RESUME, AgentAction.STEER, AgentAction.ABORT})

AUTHORITY = AuthorityReference(
    kind="gate_decision",
    reference_id="decision-abc",
    human_id="human-operator-1",
    org_id=TENANT,
)


class FakeGrantStore:
    """Stands in for the protected authority store.

    A dict, deliberately: the point of the store protocol is that the policy does
    not care what backs it, and a fake makes the policy's own logic testable
    without asserting anything about storage.
    """

    def __init__(self, grants: dict[str, DelegatedGrant] | None = None, in_flight: int = 0):
        self.grants = grants or {}
        self.in_flight = in_flight
        self.loads: list[tuple[str, str]] = []
        self.count_calls: list[tuple[str, str]] = []

    def load_grant(self, *, principal: str, tenant_id: str) -> DelegatedGrant | None:
        self.loads.append((principal, tenant_id))
        grant = self.grants.get(principal)
        if grant is None or grant.tenant_id != tenant_id:
            return None
        return grant

    def active_dispatch_count(self, *, grant_id: str, tenant_id: str) -> int:
        self.count_calls.append((grant_id, tenant_id))
        return self.in_flight


class FakeTargetResolver:
    """Resolves target facts from a fixture map, as authoritative state would."""

    def __init__(self, targets: dict[str, TargetFacts] | None = None):
        self.targets = targets or {}
        self.calls: list[tuple[str, str]] = []

    def resolve(self, *, run_id, caller):
        self.calls.append((run_id, caller.principal))
        return self.targets.get(run_id)


class FakeExecutionStore:
    """Stands in for the protected store's execution records.

    Defaults to *auto-vivifying* an ACTIVE record matching whatever credential is
    presented, so the many pre-existing tests that predate live-state checking
    keep exercising the authorization logic they were written for rather than all
    collapsing onto the new refusal.

    That default is a test-only convenience and the opposite of production
    behaviour, where an absent record fails closed. The tests that care about
    that difference construct this with ``auto=False`` — see
    ``TestLiveExecutionState``.
    """

    def __init__(self, records: dict[str, ExecutionRecord] | None = None, auto: bool = True):
        self.records = records or {}
        self.auto = auto
        self.loads: list[tuple[str, str]] = []

    def load_execution(self, *, invocation_id: str, tenant_id: str) -> ExecutionRecord | None:
        self.loads.append((invocation_id, tenant_id))
        record = self.records.get(invocation_id)
        if record is not None:
            return record
        if not self.auto:
            return None
        return ExecutionRecord(
            invocation_id=invocation_id,
            tenant_id=tenant_id,
            current_attempt=1,
            status=ExecutionStatus.ACTIVE,
            current_credential_epoch=1,
            min_acceptable_credential_epoch=1,
            flow_id="flow-42",
        )


def execution(invocation_id="inv-coordinator", **overrides) -> ExecutionRecord:
    kwargs = {
        "invocation_id": invocation_id,
        "tenant_id": TENANT,
        "current_attempt": 1,
        "status": ExecutionStatus.ACTIVE,
        "current_credential_epoch": 1,
        "min_acceptable_credential_epoch": 1,
        "flow_id": "flow-42",
    }
    kwargs.update(overrides)
    return ExecutionRecord(**kwargs)


def credential(invocation_id="inv-coordinator", attempt=1, tenant_id=TENANT, **kw) -> str:
    return mint_credential(
        invocation_id=invocation_id,
        attempt=attempt,
        tenant_id=tenant_id,
        flow_id=kw.pop("flow_id", "flow-42"),
        now=NOW,
        env=ENV,
        **kw,
    )


def coordinator_grant(principal="inv-coordinator#1", **overrides) -> DelegatedGrant:
    kwargs = {
        "grant_id": "grant-coordinator-1",
        "tenant_id": TENANT,
        "principal": principal,
        "authority": AUTHORITY,
        "allowed_actions": frozenset({AgentAction.DISPATCH, AgentAction.MONITOR}),
        "target_relationships": frozenset({TargetRelationship.FLOW_NODE}),
        "flow_id": "flow-42",
        "max_dispatch_concurrency": 3,
        "delegable_actions": frozenset({AgentAction.MONITOR}),
    }
    kwargs.update(overrides)
    return DelegatedGrant(**kwargs)


def developer_target(run_id="run-developer-7", **overrides) -> TargetFacts:
    kwargs = {
        "run_id": run_id,
        "tenant_id": TENANT,
        "flow_id": "flow-42",
        "relationships": frozenset({TargetRelationship.FLOW_NODE}),
        "generation": 3,
    }
    kwargs.update(overrides)
    return TargetFacts(**kwargs)


def service(*, grants=None, targets=None, in_flight=0, executions=None, auto_execution=True) -> AgentAuthorizationService:
    return AgentAuthorizationService(
        grant_store=FakeGrantStore(grants, in_flight=in_flight),
        target_resolver=FakeTargetResolver(targets),
        execution_store=FakeExecutionStore(executions, auto=auto_execution),
        now=lambda: NOW,
        env=ENV,
    )


class TestLegitimateFlowStillWorks:
    """AC1: an authorized coordinator monitors and dispatches, no human prompt."""

    def test_coordinator_may_monitor_a_developer_run_in_its_flow(self):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
        )
        result = svc.authorize(
            credential_token=credential(),
            action=AgentAction.MONITOR,
            target_run_id="run-developer-7",
        )

        assert result.decision.allowed
        assert result.credential.principal == "inv-coordinator#1"
        assert result.target.generation == 3

    def test_coordinator_may_dispatch_within_concurrency(self):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
            in_flight=2,
        )
        assert svc.authorize(
            credential_token=credential(),
            action=AgentAction.DISPATCH,
            target_run_id="run-developer-7",
        ).decision.allowed

    @pytest.mark.parametrize("unimplemented", [AgentAction.MONITOR, AgentAction.PAUSE, AgentAction.RESUME, AgentAction.STEER, AgentAction.ABORT])
    def test_authorization_and_implementation_are_separate_ladders(self, unimplemented, monkeypatch):
        """Authorization succeeds for a verb with no behaviour; 501 comes after.

        STEER carried this case until #3965 implemented it. That is exactly why
        the verb is now supplied by the test rather than borrowed from the shipped
        set: the property is the *order*, not which verb happens to be missing,
        and a test that reads the order off one unimplemented verb dies the day
        that verb ships. Withholding each verb in turn also proves the ladder
        holds for whichever verb is added next.

        Collapsing the two rungs would make an unauthorized caller's 404 and an
        authorized caller's 501 indistinguishable, which is how a caller
        enumerates the deployment's verbs by probing.
        """
        monkeypatch.setattr(
            "src.agentauth.policy.SUPPORTED_AGENT_ACTIONS",
            frozenset(SHIPPED_SUPPORTED_ACTIONS - {unimplemented}),
        )
        svc = service(
            grants={
                "inv-coordinator#1": coordinator_grant(
                    allowed_actions=frozenset({AgentAction.MONITOR, unimplemented}),
                    target_run_ids=frozenset({"run-developer-7"}),
                )
            },
            targets={"run-developer-7": developer_target()},
        )
        authorized = svc.authorize(
            credential_token=credential(),
            action=unimplemented,
            target_run_id="run-developer-7",
        )
        assert authorized.decision.allowed

        with pytest.raises(PolicyError) as exc:
            svc.require_supported(unimplemented)
        assert exc.value.status_code == UNSUPPORTED_STATUS

    @pytest.mark.parametrize(
        "action",
        [AgentAction.MONITOR, AgentAction.PAUSE, AgentAction.RESUME, AgentAction.STEER, AgentAction.ABORT],
    )
    def test_the_implemented_verbs_pass_the_supported_check(self, action):
        """The shipped value of ``SUPPORTED_AGENT_ACTIONS``, with no patching.

        ABORT is here because of #3963, STEER because of #3965. Several test files
        patch this set to cover signing, receipt and ordering paths, and a patched
        set proves nothing about what the deployment actually offers — so this is
        the one place that reads the real module attribute. If a verb were dropped
        from it, that verb's whole path would start returning 501 at
        ``require_supported`` and every test that patches the set would keep
        passing.
        """
        service().require_supported(action)

    def test_the_shipped_set_is_pinned_exactly(self):
        """Pins the shipped set exactly, so a verb cannot join it silently.

        A verb belongs in this set only once it has a worker-side implementation:
        adding one here without that makes the gateway mint an envelope for a
        command the listener will refuse, which surfaces as an opaque delivery
        failure rather than an honest 501. STEER satisfied that in #3965 by
        landing the queue and the handoff boundary that hold an instruction until
        the runtime can take it.

        DISPATCH stays out and is not an oversight: it is arbitrated by this same
        policy but is not a live-control verb, so it never reaches
        ``require_supported``.
        """
        from src.agentauth.policy import SUPPORTED_AGENT_ACTIONS

        assert SUPPORTED_AGENT_ACTIONS == SHIPPED_SUPPORTED_ACTIONS
        assert AgentAction.DISPATCH not in SUPPORTED_AGENT_ACTIONS


class TestTwoWorkersSharingOneRole:
    """AC2: environment rewriting buys nothing when identity is in the credential."""

    def test_a_worker_cannot_act_as_another_run_by_claiming_its_id(self):
        """The attacker holds a *genuine* credential — for its own run."""
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
        )
        attacker_token = credential(invocation_id="inv-attacker")

        with pytest.raises(PolicyError) as exc:
            svc.authorize(
                credential_token=attacker_token,
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
        assert exc.value.status_code == REFUSED_STATUS

    def test_the_policy_looks_up_the_grant_by_credential_principal_not_by_request(self):
        """Proves the identity used for authority came from the credential."""
        store = FakeGrantStore({"inv-coordinator#1": coordinator_grant()})
        svc = AgentAuthorizationService(
            grant_store=store,
            target_resolver=FakeTargetResolver({"run-developer-7": developer_target()}),
            execution_store=FakeExecutionStore(),
            now=lambda: NOW,
            env=ENV,
        )
        svc.authorize(
            credential_token=credential(invocation_id="inv-coordinator", attempt=1),
            action=AgentAction.MONITOR,
            target_run_id="run-developer-7",
        )
        assert store.loads == [("inv-coordinator#1", TENANT)]

    def test_a_different_attempt_of_the_same_invocation_is_a_different_principal(self):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
        )
        with pytest.raises(PolicyError):
            svc.authorize(
                credential_token=credential(attempt=2),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )

    def test_a_forged_credential_is_refused_identically_to_an_unauthorized_target(self):
        """Same status for both, so refusals are not an existence oracle."""
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
        )
        with pytest.raises(PolicyError) as forged:
            svc.authorize(
                credential_token="adpr1.forged.nope",
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
        with pytest.raises(PolicyError) as unknown:
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="run-does-not-exist",
            )
        assert forged.value.status_code == unknown.value.status_code == REFUSED_STATUS
        assert forged.value.detail == unknown.value.detail

    def test_a_credential_for_another_tenant_cannot_reach_this_tenants_run(self):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
        )
        with pytest.raises(PolicyError):
            svc.authorize(
                credential_token=credential(tenant_id="org-other"),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )


class TestUnauthorizedTargets:
    """AC3, at the policy layer rather than the pure-function layer."""

    def test_a_caller_with_no_grant_is_refused(self):
        svc = service(targets={"run-developer-7": developer_target()})
        with pytest.raises(PolicyError) as exc:
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
        assert exc.value.status_code == REFUSED_STATUS

    def test_an_unknown_target_is_refused_before_any_grant_check_can_admit_it(self):
        svc = service(grants={"inv-coordinator#1": coordinator_grant()})
        with pytest.raises(PolicyError):
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="run-nonexistent",
            )

    def test_an_empty_target_id_is_refused(self):
        svc = service(grants={"inv-coordinator#1": coordinator_grant()})
        with pytest.raises(PolicyError):
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="",
            )

    def test_a_cross_flow_target_is_refused_at_the_policy_layer(self):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-other-flow": developer_target(run_id="run-other-flow", flow_id="flow-99")},
        )
        with pytest.raises(PolicyError):
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="run-other-flow",
            )


class TestLimits:
    def test_dispatch_beyond_concurrency_is_throttled_not_silently_dropped(self):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
            in_flight=3,
        )
        with pytest.raises(PolicyError) as exc:
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.DISPATCH,
                target_run_id="run-developer-7",
            )
        assert exc.value.status_code == THROTTLED_STATUS

    def test_a_grant_with_no_dispatch_budget_cannot_dispatch(self):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant(max_dispatch_concurrency=0)},
            targets={"run-developer-7": developer_target()},
        )
        with pytest.raises(PolicyError) as exc:
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.DISPATCH,
                target_run_id="run-developer-7",
            )
        assert exc.value.status_code == REFUSED_STATUS

    def test_an_unauthorized_caller_cannot_probe_concurrency(self):
        """A 429 for an unauthorized caller would leak another flow's load."""
        svc = service(targets={"run-developer-7": developer_target()}, in_flight=99)
        with pytest.raises(PolicyError) as exc:
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.DISPATCH,
                target_run_id="run-developer-7",
            )
        assert exc.value.status_code == REFUSED_STATUS


class TestQueuedActionRevalidation:
    """AC6: queued work re-asks, because a signature cannot express revocation."""

    def test_future_epoch_does_not_authorize_current_grant(self):
        svc = service(grants={"inv-coordinator#1": coordinator_grant(revocation_epoch=2)})
        assert not svc.revalidate_epoch(grant_id="grant-coordinator-1", tenant_id=TENANT, principal="inv-coordinator#1", envelope_epoch=3)

    def test_an_unrevoked_grant_revalidates(self):
        svc = service(grants={"inv-coordinator#1": coordinator_grant(revocation_epoch=2)})
        assert svc.revalidate_epoch(
            grant_id="grant-coordinator-1",
            tenant_id=TENANT,
            principal="inv-coordinator#1",
            envelope_epoch=2,
        )

    def test_a_revoked_grant_fails_revalidation(self):
        svc = service(grants={"inv-coordinator#1": coordinator_grant(revoked=True)})
        assert not svc.revalidate_epoch(
            grant_id="grant-coordinator-1",
            tenant_id=TENANT,
            principal="inv-coordinator#1",
            envelope_epoch=1,
        )

    def test_a_grant_that_moved_to_a_later_epoch_fails_revalidation(self):
        """The narrowing case: the grant changed after the envelope was signed."""
        svc = service(grants={"inv-coordinator#1": coordinator_grant(revocation_epoch=5)})
        assert not svc.revalidate_epoch(
            grant_id="grant-coordinator-1",
            tenant_id=TENANT,
            principal="inv-coordinator#1",
            envelope_epoch=3,
        )

    def test_a_vanished_grant_fails_revalidation(self):
        assert not service().revalidate_epoch(
            grant_id="grant-coordinator-1",
            tenant_id=TENANT,
            principal="inv-coordinator#1",
            envelope_epoch=1,
        )

    def test_a_grant_id_mismatch_fails_revalidation(self):
        """The envelope's grant must be the grant that is still live."""
        svc = service(grants={"inv-coordinator#1": coordinator_grant(grant_id="grant-new")})
        assert not svc.revalidate_epoch(
            grant_id="grant-old",
            tenant_id=TENANT,
            principal="inv-coordinator#1",
            envelope_epoch=1,
        )


class TestLiveExecutionState:
    """AC2/AC6: a perfectly valid credential is refused when the execution moved.

    Every credential minted in this class verifies and is unexpired. Only the
    protected store differs — which is the whole claim: signature plus expiry is
    not a liveness check, so the policy must consult live state.

    ``auto_execution=False`` throughout, because the auto-vivifying default exists
    to keep older tests focused on grant logic and would defeat these.
    """

    def _svc(self, executions):
        return service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
            executions=executions,
            auto_execution=False,
        )

    def _refuse(self, executions, *, token=None, action=AgentAction.MONITOR) -> PolicyError:
        with pytest.raises(PolicyError) as excinfo:
            self._svc(executions).authorize(
                credential_token=token or credential(),
                action=action,
                target_run_id="run-developer-7",
            )
        assert excinfo.value.status_code == REFUSED_STATUS
        return excinfo.value

    def test_a_live_active_execution_still_authorizes(self):
        """The baseline: adding the liveness check must not refuse legitimate work."""
        result = self._svc({"inv-coordinator": execution()}).authorize(
            credential_token=credential(),
            action=AgentAction.MONITOR,
            target_run_id="run-developer-7",
        )
        assert result.decision.allowed
        assert result.execution is not None
        assert result.execution.invocation_id == "inv-coordinator"

    def test_a_missing_execution_record_fails_closed(self):
        """No record must not read as "unrestricted".

        Otherwise an outage in the authority store — or a credential naming a run
        that was never dispatched — becomes an authorization bypass.
        """
        assert self._refuse({}).audit_reason == "execution_not_found"

    def test_a_superseded_attempt_is_refused_despite_a_valid_credential(self):
        """The retry case. Attempt 1's pod may still be running and its token is
        still unexpired; only the store knows attempt 2 took over."""
        executions = {"inv-coordinator": execution(current_attempt=2)}
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant(), "inv-coordinator#2": coordinator_grant(principal="inv-coordinator#2")},
            targets={"run-developer-7": developer_target()},
            executions=executions,
            auto_execution=False,
        )
        with pytest.raises(PolicyError) as excinfo:
            svc.authorize(
                credential_token=credential(attempt=1),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
        assert excinfo.value.audit_reason == "execution_attempt_superseded"

    def test_a_cancelled_execution_is_refused(self):
        executions = {"inv-coordinator": execution(status=ExecutionStatus.CANCELLED)}
        assert self._refuse(executions).audit_reason == "execution_not_active:cancelled"

    def test_a_revoked_execution_is_refused(self):
        executions = {"inv-coordinator": execution(status=ExecutionStatus.REVOKED)}
        assert self._refuse(executions).audit_reason == "execution_not_active:revoked"

    def test_a_superseded_credential_epoch_is_refused(self):
        """AC6 renewal: the old credential stays cryptographically valid, so only
        the store's epoch floor can retire it."""
        executions = {"inv-coordinator": execution(current_credential_epoch=2, min_acceptable_credential_epoch=2)}
        error = self._refuse(executions, token=credential(credential_epoch=1))
        assert error.audit_reason == "credential_epoch_superseded"

    def test_the_previous_epoch_still_works_inside_the_rotation_overlap(self):
        """A worker mid-request when its credential rotates must not be refused."""
        executions = {
            "inv-coordinator": execution(
                current_credential_epoch=2,
                min_acceptable_credential_epoch=1,
                epoch_overlap_expires_at=NOW + timedelta(seconds=30),
            )
        }
        assert (
            self._svc(executions)
            .authorize(
                credential_token=credential(credential_epoch=1),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
            .decision.allowed
        )

    def test_a_bound_execution_refuses_a_different_workload(self):
        """The leaked-credential case, through the policy rather than the pure check."""
        executions = {"inv-coordinator": execution(workload_binding="pod-uid-aaa")}
        with pytest.raises(PolicyError) as excinfo:
            self._svc(executions).resolve_live_execution(
                self._svc(executions).resolve_caller(credential()),
                presented_workload_binding="pod-uid-bbb",
            )
        assert excinfo.value.audit_reason == "workload_binding_mismatch"

    def test_an_execution_belonging_to_another_tenant_is_refused(self):
        """The store is keyed by tenant, so this is defence in depth — but the
        policy must not rely on the key alone to establish tenancy."""
        executions = {"inv-coordinator": execution(tenant_id="org-tenant-999")}
        assert self._refuse(executions).audit_reason == "execution_tenant_mismatch"

    def test_a_store_failure_refuses_rather_than_allowing(self):
        """Fail closed on infrastructure failure, and distinguishably in the audit."""

        class ExplodingExecutionStore:
            def load_execution(self, *, invocation_id, tenant_id):
                raise RuntimeError("authority store unavailable")

        svc = AgentAuthorizationService(
            grant_store=FakeGrantStore({"inv-coordinator#1": coordinator_grant()}),
            target_resolver=FakeTargetResolver({"run-developer-7": developer_target()}),
            execution_store=ExplodingExecutionStore(),
            now=lambda: NOW,
            env=ENV,
        )
        with pytest.raises(PolicyError) as excinfo:
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
        assert excinfo.value.status_code == REFUSED_STATUS
        assert excinfo.value.audit_reason == "execution_store_unavailable"

    def test_the_execution_is_looked_up_by_credential_identity_not_by_request(self):
        """A caller must not be able to steer which execution record judges it."""
        store = FakeExecutionStore({"inv-coordinator": execution()}, auto=False)
        svc = AgentAuthorizationService(
            grant_store=FakeGrantStore({"inv-coordinator#1": coordinator_grant()}),
            target_resolver=FakeTargetResolver({"run-developer-7": developer_target()}),
            execution_store=store,
            now=lambda: NOW,
            env=ENV,
        )
        svc.authorize(
            credential_token=credential(),
            action=AgentAction.MONITOR,
            target_run_id="run-developer-7",
        )
        assert store.loads == [("inv-coordinator", TENANT)]

    def test_liveness_is_checked_before_the_target_is_resolved(self):
        """A stale caller must not learn whether a run it named exists.

        Resolving the target first would make the target resolver a probe
        available to superseded and cancelled executions.
        """
        resolver = FakeTargetResolver({"run-developer-7": developer_target()})
        svc = AgentAuthorizationService(
            grant_store=FakeGrantStore({"inv-coordinator#1": coordinator_grant()}),
            target_resolver=resolver,
            execution_store=FakeExecutionStore({}, auto=False),
            now=lambda: NOW,
            env=ENV,
        )
        with pytest.raises(PolicyError):
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
        assert resolver.calls == []

    def test_a_live_state_refusal_is_audited_with_the_caller_and_reason(self, caplog):
        """AC7: refusals on this path must be as auditable as refusals on any other."""
        with caplog.at_level("INFO", logger="bedrockgateway.agentauth.policy"):
            with pytest.raises(PolicyError):
                self._svc({"inv-coordinator": execution(status=ExecutionStatus.CANCELLED)}).authorize(
                    credential_token=credential(),
                    action=AgentAction.MONITOR,
                    target_run_id="run-developer-7",
                )

        refusals = [r for r in caplog.records if hasattr(r, "agent_authorization") and not r.agent_authorization["allowed"]]
        assert refusals
        fields = refusals[-1].agent_authorization
        assert fields["reason"] == "execution_not_active:cancelled"
        assert fields["principal"] == "inv-coordinator#1"

    def test_the_refusal_reason_never_reaches_the_caller(self):
        """Every live-state refusal presents one opaque message. A caller able to
        tell "superseded attempt" from "no such execution" learns whether a run it
        named exists and how many times it has been retried.
        """
        messages = {
            self._refuse({}).detail,
            self._refuse({"inv-coordinator": execution(current_attempt=9)}).detail,
            self._refuse({"inv-coordinator": execution(status=ExecutionStatus.COMPLETED)}).detail,
            self._refuse({"inv-coordinator": execution(tenant_id="org-tenant-999")}).detail,
        }
        assert len(messages) == 1


class TestAuditTrail:
    """AC7: allowed and refused requests both recorded, no secrets."""

    def test_an_allowed_request_is_logged_with_its_authority_reference(self, caplog):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
        )
        with caplog.at_level("INFO", logger="bedrockgateway.agentauth.policy"):
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )

        records = [r for r in caplog.records if hasattr(r, "agent_authorization")]
        assert records
        fields = records[-1].agent_authorization
        assert fields["allowed"] is True
        assert fields["authority_reference_id"] == "decision-abc"
        assert fields["human_authority_id"] == "human-operator-1"

    def test_a_refused_request_is_also_logged(self, caplog):
        svc = service(targets={"run-developer-7": developer_target()})
        with caplog.at_level("INFO", logger="bedrockgateway.agentauth.policy"):
            with pytest.raises(PolicyError):
                svc.authorize(
                    credential_token=credential(),
                    action=AgentAction.MONITOR,
                    target_run_id="run-developer-7",
                )

        refusals = [r for r in caplog.records if hasattr(r, "agent_authorization") and not r.agent_authorization["allowed"]]
        assert refusals
        assert refusals[-1].agent_authorization["reason"] == "no_grant"

    def test_a_missing_target_is_logged_as_a_refusal(self, caplog):
        svc = service(grants={"inv-coordinator#1": coordinator_grant()})
        with caplog.at_level("INFO", logger="bedrockgateway.agentauth.policy"):
            with pytest.raises(PolicyError):
                svc.authorize(
                    credential_token=credential(),
                    action=AgentAction.MONITOR,
                    target_run_id="run-gone",
                )

        records = [r for r in caplog.records if hasattr(r, "agent_authorization")]
        assert records[-1].agent_authorization["reason"] == "target_not_found"

    def test_a_throttled_dispatch_is_audited_as_refused_not_allowed(self, caplog):
        """A 429 must not leave an audit trail whose only entry says allowed.

        Regression. ``_authorize`` recorded the grant check's verdict before
        ``_enforce_limits`` ran, so a dispatch refused for concurrency produced
        exactly one record — ``allowed=true`` — while the caller got 429 and
        nothing was dispatched. An operator reading the audit trail would
        conclude the dispatch was authorized and had happened.

        The grant verdict is a step; the limit refusal is the outcome. This
        asserts no record claims otherwise, rather than merely asserting a
        refusal record exists alongside the misleading one.
        """
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
            in_flight=3,
        )
        with caplog.at_level("INFO", logger="bedrockgateway.agentauth.policy"):
            with pytest.raises(PolicyError) as exc:
                svc.authorize(
                    credential_token=credential(),
                    action=AgentAction.DISPATCH,
                    target_run_id="run-developer-7",
                )
        assert exc.value.status_code == THROTTLED_STATUS

        records = [r.agent_authorization for r in caplog.records if hasattr(r, "agent_authorization")]
        assert records, "a refused dispatch must be audited"
        assert not any(r["allowed"] for r in records), f"no record may claim this dispatch was allowed, got {records}"

        final = records[-1]
        assert final["reason"] == "dispatch_concurrency_exceeded"
        # Attribution survives the refusal: without it the 429 is an anonymous
        # throttle that cannot be traced to a caller or its authority (AC7).
        assert final["principal"] == "inv-coordinator#1"
        assert final["action"] == AgentAction.DISPATCH.value
        assert final["target_run_id"] == "run-developer-7"
        assert final["grant_id"] == "grant-coordinator-1"
        assert final["authority_reference_id"] == "decision-abc"
        assert final["human_authority_id"] == "human-operator-1"

    def test_a_dispatch_with_no_budget_is_audited_with_its_own_reason(self, caplog):
        """The two limit refusals are distinguishable in the audit trail.

        A misprovisioned grant (no budget, permanent) and a congested one
        (temporary) both refuse, but an operator needs to tell them apart: one is
        fixed by editing the grant, the other by waiting.
        """
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant(max_dispatch_concurrency=0)},
            targets={"run-developer-7": developer_target()},
        )
        with caplog.at_level("INFO", logger="bedrockgateway.agentauth.policy"):
            with pytest.raises(PolicyError):
                svc.authorize(
                    credential_token=credential(),
                    action=AgentAction.DISPATCH,
                    target_run_id="run-developer-7",
                )

        records = [r.agent_authorization for r in caplog.records if hasattr(r, "agent_authorization")]
        assert not any(r["allowed"] for r in records)
        assert records[-1]["reason"] == "no_dispatch_budget"

    def test_an_allowed_dispatch_within_budget_is_still_audited_as_allowed(self, caplog):
        """The fix must not invert the normal case.

        Guards against a correction that records every dispatch as refused: the
        allowed path has to survive the reordering, so this asserts the positive
        outcome that the two tests above would not catch.
        """
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
            in_flight=1,
        )
        with caplog.at_level("INFO", logger="bedrockgateway.agentauth.policy"):
            result = svc.authorize(
                credential_token=credential(),
                action=AgentAction.DISPATCH,
                target_run_id="run-developer-7",
            )
        assert result.decision.allowed

        records = [r.agent_authorization for r in caplog.records if hasattr(r, "agent_authorization")]
        assert len(records) == 1, f"exactly one decision per request, got {records}"
        assert records[-1]["allowed"] is True

    def test_the_credential_never_appears_in_a_log_record(self, caplog):
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
        )
        token = credential()
        with caplog.at_level("DEBUG", logger="bedrockgateway.agentauth.policy"):
            svc.authorize(
                credential_token=token,
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
        assert token not in caplog.text


class TestTheGrantedAndReadPathsAreDifferentMethods:
    """Why ``authorize`` and ``authorize_read`` are separate entry points.

    A run reading its own status is authorized without a grant — otherwise every
    ordinary run would need one provisioned before it could see itself. That
    grantless outcome must be reachable from the read path and from nowhere else,
    because a grantless authorization carries no revocation epoch and no
    authority reference: there is nothing to bound it with and nothing to audit
    it against, so it must never reach the signing path.

    The separation is structural rather than a flag. ``authorize_read`` takes no
    action argument, so no argument mistake at a call site can turn it into a
    grantless mutation.
    """

    def _self_service(self) -> AgentAuthorizationService:
        return service(
            targets={
                "inv-coordinator": developer_target(
                    run_id="inv-coordinator",
                    relationships=frozenset({TargetRelationship.SELF}),
                )
            }
        )

    def test_a_run_reads_its_own_status_with_no_grant(self):
        result = self._self_service().authorize_read(
            credential_token=credential(),
            target_run_id="inv-coordinator",
        )

        assert result.decision.allowed
        assert result.decision.reason == "self_monitor"
        assert result.grant is None

    def test_a_grantless_self_read_is_audited_against_the_authenticated_caller(self, caplog):
        """AC7: an allowed request must name its caller, grant or no grant.

        Regression. ``evaluate_grant`` derived the recorded principal from the
        grant, so the one allowed outcome that has no grant — the self-read —
        was audited as ``principal="unknown"`` even though the credential had
        been verified and the caller was known. "Auditable caller" was satisfied
        for every path except the one every ordinary run uses.
        """
        with caplog.at_level("INFO", logger="bedrockgateway.agentauth.policy"):
            result = self._self_service().authorize_read(
                credential_token=credential(),
                target_run_id="inv-coordinator",
            )

        assert result.decision.allowed
        assert result.grant is None
        assert result.credential.principal == "inv-coordinator#1"

        records = [r.agent_authorization for r in caplog.records if hasattr(r, "agent_authorization")]
        assert records[-1]["principal"] == "inv-coordinator#1"
        assert records[-1]["principal"] == result.credential.principal
        # No grant means no authority reference to record. That is honest rather
        # than a gap: the self-read derives from being the run, not from a
        # delegated authority, and inventing a reference would misattribute it.
        assert records[-1]["grant_id"] is None
        assert records[-1]["authority_reference_id"] is None

    def test_the_recorded_principal_is_the_credential_not_the_grant(self):
        """The two inputs are distinct, and the credential is the one that counts.

        A store returning a grant whose principal is not the authenticated caller
        is a provisioning bug or a tampered record. Honouring it would let a
        corrupted authority row rename the caller in the audit trail — so the
        request is refused rather than recorded under either name.
        """
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant(principal="inv-someone-else#1")},
            targets={"run-developer-7": developer_target()},
        )
        with pytest.raises(PolicyError) as exc:
            svc.authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="run-developer-7",
            )
        assert exc.value.status_code == REFUSED_STATUS

    def test_the_granted_path_refuses_the_same_grantless_caller(self):
        """Even for MONITOR, and even on itself.

        Not an inconsistency: ``authorize`` is the path that leads to acting on a
        target, and a caller with no grant has no authority object for the
        adapter to bind an envelope or a budget to.
        """
        with pytest.raises(PolicyError) as exc:
            self._self_service().authorize(
                credential_token=credential(),
                action=AgentAction.MONITOR,
                target_run_id="inv-coordinator",
            )

        assert exc.value.status_code == REFUSED_STATUS

    @pytest.mark.parametrize("action", [AgentAction.PAUSE, AgentAction.RESUME, AgentAction.STEER, AgentAction.ABORT, AgentAction.DISPATCH])
    def test_no_mutating_action_is_reachable_without_a_grant(self, action):
        """The property the split exists to guarantee.

        A caller whose target resolves as SELF must not be able to pause or abort
        itself on the strength of the implicit self-monitor rule.
        """
        with pytest.raises(PolicyError) as exc:
            self._self_service().authorize(
                credential_token=credential(),
                action=action,
                target_run_id="inv-coordinator",
            )

        assert exc.value.status_code == REFUSED_STATUS

    def test_the_read_path_cannot_be_asked_for_any_other_action(self):
        """``authorize_read`` has no action parameter at all."""
        import inspect

        assert "action" not in inspect.signature(AgentAuthorizationService.authorize_read).parameters

    def test_a_grantless_read_of_someone_elses_run_is_still_refused(self):
        """The grantless path is narrow: SELF only, never a sibling."""
        svc = service(targets={"run-developer-7": developer_target()})

        with pytest.raises(PolicyError) as exc:
            svc.authorize_read(credential_token=credential(), target_run_id="run-developer-7")

        assert exc.value.status_code == REFUSED_STATUS

    def test_a_granted_caller_still_reads_through_the_read_path(self):
        """AC1 again: the read path is not a fallback, it is the read path."""
        svc = service(
            grants={"inv-coordinator#1": coordinator_grant()},
            targets={"run-developer-7": developer_target()},
        )

        result = svc.authorize_read(credential_token=credential(), target_run_id="run-developer-7")

        assert result.decision.allowed
        assert result.grant is not None
        assert result.decision.authority_reference_id == "decision-abc"

    def test_a_grantless_read_consults_no_concurrency_ceiling(self):
        """There is no grant to read a ceiling from, and a read spends no budget.

        Asserted by exhausting the fake's in-flight count: a self-read must not
        become throttled because the caller's *flow* is busy dispatching.
        """
        svc = service(
            targets={
                "inv-coordinator": developer_target(
                    run_id="inv-coordinator",
                    relationships=frozenset({TargetRelationship.SELF}),
                )
            },
            in_flight=99,
        )

        assert svc.authorize_read(credential_token=credential(), target_run_id="inv-coordinator").decision.allowed


class TestLiveControlClassification:
    def test_control_verbs_are_classified_as_live_control(self):
        for action in (AgentAction.PAUSE, AgentAction.RESUME, AgentAction.STEER, AgentAction.ABORT):
            assert is_live_control(action)

    def test_monitor_and_dispatch_need_no_worker_side_envelope(self):
        assert not is_live_control(AgentAction.MONITOR)
        assert not is_live_control(AgentAction.DISPATCH)
