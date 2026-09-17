"""Durable handles, reconciliation of ambiguity, and provider-truth reporting.

Issue #5049 (U11), EPIC #4910. R15 acceptances 2, 3, 5, 6 and 7 — A's half.

## The four things this suite proves

1. A durable handle is recorded **before** the provider call counts as made, and a
   crash between the two is reconcilable (acceptance 5).
2. A timed-out call reconciles against the recorded handle and does **not** launch
   a replacement (acceptance 6).
3. While resources or cost exposure are unresolved, the allocation is not marked
   released and the reservation is not returned as unused (acceptance 7).
4. Cleanup failure is reported as failure with a non-zero result, matching
   `deprovision-gpu-node-aws.sh`'s re-check/count/exit standard (acceptance 3).

## What is mocked, and recorded here as a mock

**B's operation authority is a mock** (`MockOperationAuthority` below). B owns the
operation lifecycle, cancellation ordering, leases/fencing and the recovery
worker, and **no lease, fencing or `attempt_id` implementation exists in ADP
today**. So the tests supply the shape A calls across, and this docstring is the
record that they do. A mock authority verifies A's refusal to act without
authority; it verifies nothing about B's authority itself.

`FakeHandleStore` stands in for U11c's upstream handle persistence, for the same
reason: A does not write domain records.

The provider is **not** mocked in the sense that matters. Its responses come from
`fixtures/provider-responses.json`, generated from the SkyPilot client's own
`json:` struct tags and botocore's `DescribeInstances` output shape — never from
what this adapter expects to see. Writing them the other way is how an adapter and
its fixtures end up self-consistently wrong about a timeout.

## What this suite does not claim

R15 acceptances 1, 5, 6 and 8 also have **live** criteria — a real deletion in a
named account, a real crash mid-provision, a real lost response, and stop/cleanup
with the agent process gone. Those need a named account and environment, spend
authorization, a deadline and a named cleanup owner, all of which are unresolved.
Nothing below is evidence for them. These are offline tests against recorded
responses, and that is the whole extent of the claim.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from superplane_contracts import (
    MAX_EXIT_CODE,
    AllocationResources,
    CallOutcome,
    ContractViolation,
    CostExposure,
    Finding,
    HandleRecord,
    OperationKind,
    ProviderAdapter,
    ProviderHandle,
    ProviderObservation,
    ProviderPresence,
    ReconcileRequest,
    ReconcileResult,
    RecreationDriver,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseState,
    TeardownReport,
    assess_release,
    authorize_provider_call,
    reconcile,
)

# A fixed, timezone-aware instant. The contract requires aware datetimes and the
# tests assert on branches rather than on "now", so a constant is both legal and
# deterministic.
CONFIRMED_AT = datetime(2026, 9, 17, 9, 30, 0, tzinfo=UTC)

PROVIDER = "skypilot"
CLUSTER = "sky-node-a1b2c3"
ALLOCATION = "alloc-7f3c"
WORKSPACE = "ws-w1"
AUTHORITY = "op-authority-mocked-b-lifecycle"

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "provider-responses.json"


def load_fixtures() -> dict[str, Any]:
    """Load the generated provider responses.

    Read from disk rather than inlined so the fixture stays the single artifact
    whose provenance `provider-responses.md` documents. A test that inlined the
    same JSON would be a second copy with no provenance at all.
    """
    return json.loads(FIXTURES.read_text())


RESPONSES = load_fixtures()


# --------------------------------------------------------------------------- #
# Translating recorded provider responses into observations
# --------------------------------------------------------------------------- #


def observe_skypilot_status(
    response: list[dict[str, Any]], name: str
) -> ProviderObservation:
    """Read a recorded `POST /status` response the way the Go client reads it.

    Absence is an empty list — that is what the SkyPilot client gets for an
    unknown cluster name, and `ClusterInfo.status` is the provider's own state
    string, so `PRESENT` carries evidence rather than an assertion.
    """
    for cluster in response:
        if cluster["name"] == name:
            return ProviderObservation(
                presence=ProviderPresence.PRESENT,
                queried_by=name,
                provider_state=cluster["status"],
                detail=f"autostop={cluster['autostop']}m",
            )
    return ProviderObservation(
        presence=ProviderPresence.ABSENT,
        queried_by=name,
        detail="POST /status returned no cluster with this name",
    )


def observe_ec2(response: dict[str, Any], resource: str) -> ProviderObservation:
    """Read a recorded `DescribeInstances` response.

    `shutting-down` is deliberately `PRESENT`. It is the state that makes
    "terminate returned, therefore terminated" wrong, and an instance still
    shutting down is still an instance.
    """
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            state = instance["State"]["Name"]
            if state == "terminated":
                continue
            return ProviderObservation(
                presence=ProviderPresence.PRESENT,
                queried_by=resource,
                provider_state=state,
                detail=f"instance {instance['InstanceId']} is {state}",
            )
    return ProviderObservation(
        presence=ProviderPresence.ABSENT,
        queried_by=resource,
        detail="DescribeInstances reports no live instance",
    )


# --------------------------------------------------------------------------- #
# Mocked boundaries — B's authority and U11c's persistence
# --------------------------------------------------------------------------- #


class MockOperationAuthority:
    """MOCK of B's operation authority. B owns the real one; it does not exist yet.

    Recorded as a mock in this module's docstring. It answers "is there an active
    operation I may act under", which is the only question A asks of B.
    """

    def __init__(self, token: str | None = AUTHORITY) -> None:
        self.token = token
        self.asked: list[str] = []

    def authority_for(self, allocation_id: str) -> str | None:
        self.asked.append(allocation_id)
        return self.token


class FakeHandleStore:
    """Stands in for U11c's upstream handle persistence.

    `records` is the durable side, and it is what makes the crash test possible:
    a test can write a handle, stop, and then read it back the way B's recovery
    worker would. `fail` and `acknowledge` cover the two ways a store declines to
    make the durability claim — it raises, or it returns no confirmation instant.
    """

    def __init__(self, *, fail: bool = False, acknowledge: bool = True) -> None:
        self.fail = fail
        self.acknowledge = acknowledge
        self.records: dict[str, ProviderHandle] = {}
        self.references: dict[str, str] = {}
        self.reference_failures = 0

    def record(self, handle: ProviderHandle) -> datetime | None:
        if self.fail:
            raise RuntimeError("handle store unavailable")
        self.records[handle.idempotency_key] = handle
        return CONFIRMED_AT if self.acknowledge else None

    def attach_provider_reference(self, handle: ProviderHandle, reference: str) -> None:
        if self.reference_failures:
            self.reference_failures -= 1
            raise RuntimeError("reference write failed")
        self.references[handle.idempotency_key] = reference


class RecordedProviderClient:
    """A provider whose answers are the recorded responses, not this test's wishes.

    `invoke_raises` is how a lost response is modelled: a lost response is the
    absence of a response, so there is no fixture for it — the fixtures supply
    what the *re-check afterwards* returns.
    """

    def __init__(
        self,
        *,
        status_response: list[dict[str, Any]] | None = None,
        invoke_raises: BaseException | None = None,
        invoke_outcome: CallOutcome = CallOutcome.SUCCEEDED,
        observe_raises: BaseException | None = None,
        allocation_response: dict[str, ProviderObservation] | None = None,
        allocation_raises: BaseException | None = None,
    ) -> None:
        self.status_response = (
            status_response
            if status_response is not None
            else RESPONSES["skypilot"]["status_present_up"]
        )
        self.invoke_raises = invoke_raises
        self.invoke_outcome = invoke_outcome
        self.observe_raises = observe_raises
        self.allocation_response = allocation_response or {}
        self.allocation_raises = allocation_raises
        self.invocations: list[ProviderHandle] = []
        self.observations: list[str] = []

    def invoke(self, handle: ProviderHandle) -> tuple[CallOutcome, str | None]:
        self.invocations.append(handle)
        if self.invoke_raises is not None:
            raise self.invoke_raises
        reference = (
            RESPONSES["skypilot"]["launch_accepted"]["request_id"]
            if self.invoke_outcome is CallOutcome.SUCCEEDED
            else None
        )
        return self.invoke_outcome, reference

    def observe(self, handle: ProviderHandle) -> ProviderObservation:
        self.observations.append(handle.resource_name)
        if self.observe_raises is not None:
            raise self.observe_raises
        return observe_skypilot_status(self.status_response, handle.resource_name)

    def observe_allocation(self, allocation_id: str) -> dict[str, ProviderObservation]:
        if self.allocation_raises is not None:
            raise self.allocation_raises
        return self.allocation_response


def make_handle(
    operation: OperationKind = OperationKind.PROVISION,
    *,
    reference: str | None = None,
) -> ProviderHandle:
    return ProviderHandle(
        operation=operation,
        provider=PROVIDER,
        resource_name=CLUSTER,
        idempotency_key="idem-0001",
        allocation_id=ALLOCATION,
        workspace=WORKSPACE,
        provider_reference=reference,
    )


def durable_record(handle: ProviderHandle | None = None) -> HandleRecord:
    return HandleRecord(
        handle=handle or make_handle(), durable=True, confirmed_at=CONFIRMED_AT
    )


def deliberate_intent() -> ReleaseIntent:
    return ReleaseIntent(
        deliberate=True,
        stopped_drivers=frozenset(RecreationDriver),
        requested_by="operator-1",
    )


def build_adapter(
    provider: RecordedProviderClient,
    *,
    store: FakeHandleStore | None = None,
    authority: MockOperationAuthority | None = None,
) -> tuple[ProviderAdapter, FakeHandleStore, MockOperationAuthority]:
    store = store or FakeHandleStore()
    authority = authority or MockOperationAuthority()
    adapter = ProviderAdapter(
        store=store,
        provider=provider,
        authority=authority,
        provider_name=PROVIDER,
    )
    return adapter, store, authority


# --------------------------------------------------------------------------- #
# Fixture provenance
# --------------------------------------------------------------------------- #


class TestFixtureProvenance:
    """The fixtures must come from the providers' models, not from the adapter."""

    def test_provenance_names_both_producing_models(self) -> None:
        """A fixture with no recorded origin cannot be audited for the rule."""
        provenance = RESPONSES["_provenance"]
        assert provenance["skypilot"]["path"].endswith("skypilot/types.go")
        assert provenance["skypilot"]["blob_sha1"]
        assert provenance["skypilot"]["upstream_revision"]
        assert "DescribeInstances" in provenance["ec2"]["source"]

    def test_skypilot_states_are_the_client_s_declared_constants(self) -> None:
        """A test must not assert on a state the Go client would never produce."""
        statuses = RESPONSES["skypilot"]["cluster_statuses"]
        assert statuses == ["INIT", "UP", "STOPPED"]
        for entry in ("status_present_up", "status_present_init"):
            for cluster in RESPONSES["skypilot"][entry]:
                assert cluster["status"] in statuses

    def test_ec2_states_are_botocore_s_declared_enum(self) -> None:
        names = RESPONSES["ec2"]["instance_state_names"]
        for entry in (
            "describe_instances_running",
            "describe_instances_shutting_down",
            "describe_instances_terminated",
        ):
            for reservation in RESPONSES["ec2"][entry]["Reservations"]:
                for instance in reservation["Instances"]:
                    assert instance["State"]["Name"] in names

    def test_fixtures_carry_no_credential_material(self) -> None:
        """Recorded responses are synthetic; a real capture could carry secrets."""
        raw = FIXTURES.read_text().lower()
        for marker in (
            "aws_secret",
            "akia",
            "private key",
            "password",
            "session_token",
        ):
            assert marker not in raw


# --------------------------------------------------------------------------- #
# Acceptance 5 — recorded before the call counts as made
# --------------------------------------------------------------------------- #


class TestHandleRecordedBeforeCall:
    """The ordering rule: findable first, called second."""

    def test_handle_identity_exists_before_any_provider_call(self) -> None:
        """Every field is locally chosen, so nothing waits on a response.

        This is what makes the rule implementable at all: upstream already picks
        `clusterName` at `onboarder.go:170`, before building the request.
        """
        handle = make_handle()
        assert handle.provider_reference is None
        assert handle.resource_name == CLUSTER
        assert handle.idempotency_key

    def test_non_durable_record_refuses_the_provider_call(self) -> None:
        record = HandleRecord(handle=make_handle(), durable=False)
        decision = authorize_provider_call(record)
        assert decision.permitted is False
        assert "not durably recorded" in decision.reason

    def test_durable_record_permits_the_provider_call(self) -> None:
        decision = authorize_provider_call(durable_record())
        assert decision.permitted is True
        assert decision.reason == ""

    def test_durable_cannot_be_claimed_by_setting_a_boolean(self) -> None:
        """`durable=True` needs persistence's acknowledgement instant, not a flag."""
        with pytest.raises(ContractViolation, match="confirmed_at"):
            HandleRecord(handle=make_handle(), durable=True)

    def test_confirmation_instant_must_be_unambiguous(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            HandleRecord(
                handle=make_handle(),
                durable=True,
                confirmed_at=datetime(2026, 9, 17, 9, 30, 0),  # noqa: DTZ001
            )

    def test_confirmation_instant_without_durability_is_contradictory(self) -> None:
        with pytest.raises(ContractViolation, match="absent"):
            HandleRecord(handle=make_handle(), durable=False, confirmed_at=CONFIRMED_AT)

    def test_adapter_records_the_handle_before_invoking_the_provider(self) -> None:
        """The store holds the handle by the time the provider is called."""
        provider = RecordedProviderClient()
        adapter, store, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert result.record.durable is True
        assert result.call.permitted is True
        assert "idem-0001" in store.records
        # The handle the provider was called with is the handle that was stored.
        assert provider.invocations[0].idempotency_key == "idem-0001"

    def test_unrecordable_handle_means_the_provider_is_never_called(self) -> None:
        """The gate is real: a failing store stops the call, it does not warn."""
        provider = RecordedProviderClient()
        adapter, _, _ = build_adapter(provider, store=FakeHandleStore(fail=True))

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert provider.invocations == []
        assert result.record.durable is False
        assert result.call.permitted is False
        assert result.outcome is None
        assert result.may_repeat_operation is False
        assert result.resources_possibly_created is False

    def test_store_that_does_not_acknowledge_also_stops_the_call(self) -> None:
        """A store returning nothing has not made the durability claim."""
        provider = RecordedProviderClient()
        adapter, _, _ = build_adapter(
            provider, store=FakeHandleStore(acknowledge=False)
        )

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert provider.invocations == []
        assert result.call.permitted is False

    def test_operation_without_b_s_authority_is_refused_before_recording(self) -> None:
        """A never records a handle for an operation B did not authorize."""
        provider = RecordedProviderClient()
        adapter, store, authority = build_adapter(
            provider, authority=MockOperationAuthority(token=None)
        )

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert authority.asked == [ALLOCATION]
        assert store.records == {}
        assert provider.invocations == []
        assert "authority" in result.call.reason

    def test_crash_between_record_and_response_is_reconcilable(self) -> None:
        """The acceptance in one test: the record survives, so the truth is findable.

        The crash is modelled as what it leaves behind — a durable handle in the
        store whose operation never reported an outcome. B's recovery worker (B's,
        not built here) reads it back and asks A to resolve it.
        """
        store = FakeHandleStore()
        handle = make_handle()
        confirmed_at = store.record(handle)  # the write that landed
        # ... process dies here. Nothing reported an outcome.

        recovered = HandleRecord(
            handle=store.records[handle.idempotency_key],
            durable=True,
            confirmed_at=confirmed_at,
        )
        provider = RecordedProviderClient(
            status_response=RESPONSES["skypilot"]["status_present_up"]
        )
        adapter, _, _ = build_adapter(provider, store=store)

        decision = adapter.reconcile_recorded_handle(recovered, ALLOCATION)

        assert provider.observations == [CLUSTER]
        assert decision.result is ReconcileResult.RECONCILED_EXISTS
        assert decision.observation is not None
        assert decision.observation.provider_state == "UP"
        # The resource is adopted, not duplicated.
        assert decision.may_repeat_operation is False

    def test_recovery_requires_b_s_authority(self) -> None:
        provider = RecordedProviderClient()
        adapter, _, _ = build_adapter(
            provider, authority=MockOperationAuthority(token=None)
        )
        with pytest.raises(ContractViolation, match="authority"):
            adapter.reconcile_recorded_handle(durable_record(), ALLOCATION)

    def test_a_handle_that_was_never_recorded_cannot_be_recovered(self) -> None:
        """A non-durable record cannot have been read back out of storage."""
        provider = RecordedProviderClient()
        adapter, _, _ = build_adapter(provider)
        record = HandleRecord(handle=make_handle(), durable=False)
        with pytest.raises(ContractViolation, match="durably recorded"):
            adapter.reconcile_recorded_handle(record, ALLOCATION)

    def test_provider_reference_is_attached_without_editing_the_recorded_form(
        self,
    ) -> None:
        """The pre-call handle is what a crash recovery finds; it stays as written."""
        handle = make_handle()
        enriched = handle.with_provider_reference("req-1")
        assert handle.provider_reference is None
        assert enriched.provider_reference == "req-1"

    def test_a_conflicting_provider_reference_is_refused(self) -> None:
        handle = make_handle(reference="req-1")
        with pytest.raises(ContractViolation, match="already recorded"):
            handle.with_provider_reference("req-2")
        # Re-attaching the same reference is idempotent, which a retry needs.
        assert handle.with_provider_reference("req-1").provider_reference == "req-1"

    def test_blank_identity_fields_are_refused(self) -> None:
        for field in (
            "provider",
            "resource_name",
            "idempotency_key",
            "allocation_id",
            "workspace",
        ):
            kwargs: dict[str, Any] = {
                "operation": OperationKind.PROVISION,
                "provider": PROVIDER,
                "resource_name": CLUSTER,
                "idempotency_key": "idem-0001",
                "allocation_id": ALLOCATION,
                "workspace": WORKSPACE,
                field: "   ",
            }
            with pytest.raises(ContractViolation, match=field):
                ProviderHandle(**kwargs)

    def test_blank_provider_reference_is_worse_than_absent(self) -> None:
        with pytest.raises(ContractViolation, match="provider_reference"):
            make_handle(reference="  ")

    def test_adapter_requires_a_provider_name(self) -> None:
        """An unnamed provider makes every handle it records unattributable."""
        with pytest.raises(ContractViolation, match="provider_name"):
            ProviderAdapter(
                store=FakeHandleStore(),
                provider=RecordedProviderClient(),
                authority=MockOperationAuthority(),
                provider_name="  ",
            )

    def test_reference_write_failure_does_not_lose_the_durable_record(self) -> None:
        """Losing the provider's own id degrades reconciliation; it does not break it.

        The resource stays findable by the name and idempotency key recorded
        before the call, which is the point of recording those first.
        """
        provider = RecordedProviderClient()
        store = FakeHandleStore()
        store.reference_failures = 1
        adapter, _, _ = build_adapter(provider, store=store)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert store.references == {}
        assert "idem-0001" in store.records
        assert result.outcome is CallOutcome.SUCCEEDED


# --------------------------------------------------------------------------- #
# Acceptance 6 — a lost response is an unknown, never a blind retry
# --------------------------------------------------------------------------- #


class TestTimeoutIsReconciledNotRetried:
    """The duplicate-spend bug, closed at the classification step."""

    def test_a_timeout_is_ambiguous_rather_than_a_failure(self) -> None:
        """Upstream's `Success: false` on a timeout is what this refuses to repeat."""
        provider = RecordedProviderClient(invoke_raises=TimeoutError("no response"))
        adapter, _, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert result.outcome is CallOutcome.AMBIGUOUS

    def test_timed_out_launch_reconciles_against_the_recorded_handle(self) -> None:
        """The named acceptance: re-check by the recorded identity, adopt what exists."""
        provider = RecordedProviderClient(
            invoke_raises=TimeoutError("no response"),
            status_response=RESPONSES["skypilot"]["status_present_up"],
        )
        adapter, _, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        # The re-check was made, and it was made by the recorded resource name.
        assert provider.observations == [CLUSTER]
        assert result.decision is not None
        assert result.decision.result is ReconcileResult.RECONCILED_EXISTS
        assert result.decision.observation is not None
        assert result.decision.observation.queried_by == CLUSTER

    def test_no_replacement_is_launched_after_a_timeout(self) -> None:
        """`provisionNode`'s next-cloud launch, refused.

        Two things are asserted, because either alone is insufficient: the adapter
        made exactly one provider call (it did not retry internally), and it did
        not tell its caller a repeat was permitted (so a loop above it stops too).
        """
        provider = RecordedProviderClient(
            invoke_raises=TimeoutError("no response"),
            status_response=RESPONSES["skypilot"]["status_present_up"],
        )
        adapter, _, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert len(provider.invocations) == 1
        assert result.may_repeat_operation is False
        assert result.resources_possibly_created is True

    def test_a_cluster_still_coming_up_is_present_not_absent(self) -> None:
        """`INIT` bills. Treating it as absent is how a second launch happens."""
        provider = RecordedProviderClient(
            invoke_raises=TimeoutError("no response"),
            status_response=RESPONSES["skypilot"]["status_present_init"],
        )
        adapter, _, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert result.decision is not None
        assert result.decision.observation is not None
        assert result.decision.observation.provider_state == "INIT"
        assert result.decision.result is ReconcileResult.RECONCILED_EXISTS
        assert result.may_repeat_operation is False

    def test_provider_established_absence_is_the_only_route_to_a_repeat(self) -> None:
        provider = RecordedProviderClient(
            invoke_raises=TimeoutError("no response"),
            status_response=RESPONSES["skypilot"]["status_absent"],
        )
        adapter, _, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert result.decision is not None
        assert result.decision.result is ReconcileResult.RETRY_PERMITTED
        assert result.may_repeat_operation is True
        assert result.resources_possibly_created is False
        # Even when a repeat is permitted, the adapter does not make it. Retry
        # ordering is B's.
        assert len(provider.invocations) == 1

    def test_a_failed_re_check_authorizes_nothing(self) -> None:
        """A re-check that failed must not resolve to "there is nothing there"."""
        provider = RecordedProviderClient(
            invoke_raises=TimeoutError("no response"),
            observe_raises=ConnectionError("provider API unreachable"),
        )
        adapter, _, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert result.decision is not None
        assert result.decision.result is ReconcileResult.UNRESOLVED
        assert result.may_repeat_operation is False
        assert result.resources_possibly_created is True

    def test_any_lost_response_is_ambiguous_not_only_a_timeout(self) -> None:
        """A dropped connection loses a response exactly as a timeout does."""
        provider = RecordedProviderClient(
            invoke_raises=ConnectionResetError("connection reset"),
            status_response=RESPONSES["skypilot"]["status_absent"],
        )
        adapter, _, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert result.outcome is CallOutcome.AMBIGUOUS
        assert provider.observations == [CLUSTER]

    def test_a_providers_own_refusal_needs_no_re_check(self) -> None:
        """The one case upstream classifies correctly — and it stays correct."""
        provider = RecordedProviderClient(invoke_outcome=CallOutcome.FAILED)
        adapter, _, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert provider.observations == []
        assert result.decision is not None
        assert result.decision.result is ReconcileResult.RETRY_PERMITTED

    def test_a_successful_call_needs_no_re_check(self) -> None:
        provider = RecordedProviderClient()
        adapter, store, _ = build_adapter(provider)

        result = adapter.perform_operation(
            operation=OperationKind.PROVISION,
            resource_name=CLUSTER,
            idempotency_key="idem-0001",
            allocation_id=ALLOCATION,
            workspace=WORKSPACE,
        )

        assert provider.observations == []
        assert result.decision is not None
        assert result.decision.result is ReconcileResult.RECONCILED_EXISTS
        assert (
            result.record.handle.provider_reference
            == (RESPONSES["skypilot"]["launch_accepted"]["request_id"])
        )
        assert (
            store.references["idem-0001"]
            == (RESPONSES["skypilot"]["launch_accepted"]["request_id"])
        )


class TestReconcileContract:
    """`reconcile` on its own, including the paths the adapter cannot reach."""

    def test_ambiguity_with_no_observation_is_unresolved(self) -> None:
        """No provider answer means no permission — not a default to "retry"."""
        request = ReconcileRequest(
            handle=make_handle(),
            outcome=CallOutcome.AMBIGUOUS,
            operation_authority=AUTHORITY,
        )
        decision = reconcile(request, None)
        assert decision.result is ReconcileResult.UNRESOLVED
        assert decision.may_repeat_operation is False
        assert decision.resources_unresolved is True

    def test_reconciliation_refuses_to_act_without_b_s_authority(self) -> None:
        for authority in ("", "   "):
            with pytest.raises(ContractViolation, match="operation_authority"):
                ReconcileRequest(
                    handle=make_handle(),
                    outcome=CallOutcome.AMBIGUOUS,
                    operation_authority=authority,
                )

    def test_an_unknown_observation_leaves_the_allocation_unresolved(self) -> None:
        request = ReconcileRequest(
            handle=make_handle(),
            outcome=CallOutcome.AMBIGUOUS,
            operation_authority=AUTHORITY,
        )
        observation = ProviderObservation(
            presence=ProviderPresence.UNKNOWN,
            queried_by=CLUSTER,
            detail="expired credential",
        )
        decision = reconcile(request, observation)
        assert decision.result is ReconcileResult.UNRESOLVED
        assert "not erased" in decision.reason

    def test_release_ambiguity_reconciles_from_ec2_s_own_states(self) -> None:
        """A lost `down` response resolves against DescribeInstances, both ways."""
        request = ReconcileRequest(
            handle=make_handle(OperationKind.RELEASE),
            outcome=CallOutcome.AMBIGUOUS,
            operation_authority=AUTHORITY,
        )
        shutting_down = observe_ec2(
            RESPONSES["ec2"]["describe_instances_shutting_down"], CLUSTER
        )
        assert reconcile(request, shutting_down).resources_unresolved is True

        terminated = observe_ec2(
            RESPONSES["ec2"]["describe_instances_terminated"], CLUSTER
        )
        assert reconcile(request, terminated).result is ReconcileResult.RETRY_PERMITTED

        empty = observe_ec2(RESPONSES["ec2"]["describe_instances_empty"], CLUSTER)
        assert empty.presence is ProviderPresence.ABSENT


class TestProviderObservation:
    """An observation must be an observation, not an assertion."""

    def test_present_requires_the_provider_s_reported_state(self) -> None:
        with pytest.raises(ContractViolation, match="reported state"):
            ProviderObservation(presence=ProviderPresence.PRESENT, queried_by=CLUSTER)

    def test_unknown_cannot_carry_a_provider_state(self) -> None:
        with pytest.raises(ContractViolation, match="not successfully consulted"):
            ProviderObservation(
                presence=ProviderPresence.UNKNOWN,
                queried_by=CLUSTER,
                provider_state="UP",
                detail="timeout",
            )

    def test_unknown_must_say_why(self) -> None:
        with pytest.raises(ContractViolation, match="why"):
            ProviderObservation(presence=ProviderPresence.UNKNOWN, queried_by=CLUSTER)

    def test_the_query_identity_is_recorded(self) -> None:
        """A confident answer about the wrong resource is still the wrong answer."""
        with pytest.raises(ContractViolation, match="queried_by"):
            ProviderObservation(presence=ProviderPresence.ABSENT, queried_by="  ")


# --------------------------------------------------------------------------- #
# Acceptance 7 — no release or accounting clearance while exposure is unresolved
# --------------------------------------------------------------------------- #


class TestUnresolvedExposureBlocksRelease:
    """`consolidator.go`'s advance-past-a-failed-delete, refused."""

    def test_an_unknown_resource_blocks_both_claims(self) -> None:
        """The named acceptance: not released, and not returned as unused."""
        assessment = assess_release(
            {
                "node": ProviderObservation(
                    presence=ProviderPresence.UNKNOWN,
                    queried_by="node",
                    detail="DescribeInstances threw ThrottlingException",
                )
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
        )
        assert assessment.state is ReleaseState.UNRESOLVED
        assert assessment.may_mark_released is False
        assert assessment.may_return_reservation_unused is False

    def test_incurred_cost_is_accrued_as_unresolved_never_zero(self) -> None:
        assessment = assess_release(
            {
                "volume": ProviderObservation(
                    presence=ProviderPresence.UNKNOWN,
                    queried_by="volume",
                    detail="credential expired mid-teardown",
                )
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"volume"})),
        )
        assert assessment.exposure is CostExposure.UNRESOLVED
        assert assessment.exposure is not CostExposure.NONE
        assert "zero" in assessment.reason

    def test_an_unresolved_allocation_names_what_is_outstanding(self) -> None:
        """Retained and reported, not erased — an operator can go and look."""
        assessment = assess_release(
            {
                "node": ProviderObservation(
                    presence=ProviderPresence.UNKNOWN,
                    queried_by="node",
                    detail="API error",
                ),
                "volume": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_running"], "volume"
                ),
                "subnet": ProviderObservation(
                    presence=ProviderPresence.ABSENT, queried_by="subnet"
                ),
            },
            allocation=AllocationResources(
                ALLOCATION, frozenset({"node", "volume", "subnet"})
            ),
        )
        # Both the unconsultable and the confirmed-present resource are named; the
        # confirmed-absent one is not outstanding.
        assert assessment.unresolved_resources == ("node", "volume")

    def test_unknown_outranks_present(self) -> None:
        """The unconsultable case dominates: a wrong reading there is unrecoverable."""
        assessment = assess_release(
            {
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_running"], "node"
                ),
                "volume": ProviderObservation(
                    presence=ProviderPresence.UNKNOWN,
                    queried_by="volume",
                    detail="API error",
                ),
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node", "volume"})),
        )
        assert assessment.state is ReleaseState.UNRESOLVED
        assert assessment.exposure is CostExposure.UNRESOLVED

    def test_a_confirmed_present_resource_is_retained_and_still_costing(self) -> None:
        assessment = assess_release(
            {
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_running"], "node"
                )
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
        )
        assert assessment.state is ReleaseState.RETAINED
        assert assessment.exposure is CostExposure.ACTIVE
        assert assessment.may_mark_released is False
        assert assessment.may_return_reservation_unused is False

    def test_a_mid_teardown_instance_is_not_released(self) -> None:
        """Blocks the claim "terminate returned, therefore terminated"."""
        assessment = assess_release(
            {
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_shutting_down"], "node"
                )
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
        )
        assert assessment.may_mark_released is False

    def test_release_is_permitted_only_after_established_absence(self) -> None:
        assessment = assess_release(
            {
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_terminated"], "node"
                ),
                CLUSTER: observe_skypilot_status(
                    RESPONSES["skypilot"]["status_absent"], CLUSTER
                ),
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node", CLUSTER})),
        )
        assert assessment.state is ReleaseState.RELEASED
        assert assessment.exposure is CostExposure.NONE
        assert assessment.may_mark_released is True
        assert assessment.may_return_reservation_unused is True
        assert assessment.unresolved_resources == ()

    def test_checking_nothing_is_not_a_clean_release(self) -> None:
        """Checking nothing and finding nothing must not be the same value."""
        assessment = assess_release(
            {}, allocation=AllocationResources(ALLOCATION, frozenset({"node"}))
        )
        assert assessment.state is ReleaseState.UNRESOLVED
        assert assessment.unresolved_resources == ("node",)

    def test_the_two_claims_cannot_be_assembled_by_hand(self) -> None:
        """The guards close the constructor, not merely the factory."""
        with pytest.raises(ContractViolation, match="continuing cost"):
            ReleaseAssessment(
                state=ReleaseState.RELEASED,
                exposure=CostExposure.ACTIVE,
                allocation_id=ALLOCATION,
            )
        with pytest.raises(ContractViolation, match="unresolved resources"):
            ReleaseAssessment(
                state=ReleaseState.RELEASED,
                exposure=CostExposure.NONE,
                unresolved_resources=("node",),
                allocation_id=ALLOCATION,
            )
        with pytest.raises(ContractViolation, match="must name"):
            ReleaseAssessment(
                state=ReleaseState.UNRESOLVED,
                exposure=CostExposure.UNRESOLVED,
                allocation_id=ALLOCATION,
            )
        with pytest.raises(ContractViolation, match="zero cost exposure"):
            ReleaseAssessment(
                state=ReleaseState.RETAINED,
                exposure=CostExposure.NONE,
                unresolved_resources=("node",),
                allocation_id=ALLOCATION,
            )


# --------------------------------------------------------------------------- #
# Acceptance 3 — cleanup failure is reported as failure, with a non-zero result
# --------------------------------------------------------------------------- #


class TestCleanupFailureIsReportedAsFailure:
    """The `deprovision-gpu-node-aws.sh` standard: re-check, count, exit non-zero."""

    def test_a_failed_teardown_exits_non_zero_with_the_error_count(self) -> None:
        """`ERRORS=$((ERRORS + 1))` … `exit ${ERRORS}`, in Python."""
        report = TeardownReport(
            allocation_id=ALLOCATION,
            assessment=assess_release(
                {
                    "node": observe_ec2(
                        RESPONSES["ec2"]["describe_instances_running"], "node"
                    ),
                    "volume": ProviderObservation(
                        presence=ProviderPresence.UNKNOWN,
                        queried_by="volume",
                        detail="API error",
                    ),
                },
                allocation=AllocationResources(
                    ALLOCATION, frozenset({"node", "volume"})
                ),
            ),
            intent=deliberate_intent(),
            findings=(
                Finding(resource="node", detail="instance still running"),
                Finding(resource="volume", detail="could not be consulted"),
            ),
        )
        assert report.succeeded is False
        assert report.failure_count == 2
        assert report.exit_code == 2

    def test_a_confirmed_teardown_exits_zero(self) -> None:
        report = TeardownReport(
            allocation_id=ALLOCATION,
            assessment=assess_release(
                {
                    "node": observe_ec2(
                        RESPONSES["ec2"]["describe_instances_terminated"], "node"
                    )
                },
                allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
            ),
            intent=deliberate_intent(),
        )
        assert report.succeeded is True
        assert report.exit_code == 0

    def test_an_assessment_that_established_nothing_still_exits_non_zero(self) -> None:
        """Zero findings and a non-RELEASED state must not read as success."""
        report = TeardownReport(
            allocation_id=ALLOCATION,
            assessment=assess_release(
                {
                    "node": ProviderObservation(
                        presence=ProviderPresence.UNKNOWN,
                        queried_by="node",
                        detail="API error",
                    )
                },
                allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
            ),
            intent=deliberate_intent(),
        )
        assert report.findings == ()
        assert report.exit_code == 1

    def test_the_exit_code_stays_below_the_shell_s_reserved_range(self) -> None:
        """130 unresolved resources must not be reported as "killed by SIGINT"."""
        findings = tuple(
            Finding(resource=f"node-{index}", detail="still present")
            for index in range(200)
        )
        report = TeardownReport(
            allocation_id=ALLOCATION,
            assessment=assess_release(
                {
                    "node": observe_ec2(
                        RESPONSES["ec2"]["describe_instances_running"], "node"
                    )
                },
                allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
            ),
            intent=deliberate_intent(),
            findings=findings,
        )
        assert report.failure_count == 200
        assert report.exit_code == MAX_EXIT_CODE
        assert report.exit_code < 126

    def test_a_credential_failure_is_its_own_counted_error(self) -> None:
        report = TeardownReport(
            allocation_id=ALLOCATION,
            assessment=assess_release(
                {
                    "node": ProviderObservation(
                        presence=ProviderPresence.UNKNOWN,
                        queried_by="node",
                        detail="credential expired",
                    )
                },
                allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
            ),
            intent=deliberate_intent(),
            credential_failure=True,
        )
        assert report.failure_count == 1
        assert report.exit_code == 1

    def test_cleanup_cannot_be_claimed_after_losing_credentials(self) -> None:
        """Design note §8, refused at construction so no caller can assemble it."""
        released = assess_release(
            {
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_terminated"], "node"
                )
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
        )
        with pytest.raises(ContractViolation, match="credential"):
            TeardownReport(
                allocation_id=ALLOCATION,
                assessment=released,
                intent=deliberate_intent(),
                credential_failure=True,
            )

    def test_a_released_allocation_cannot_carry_findings(self) -> None:
        released = assess_release(
            {
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_terminated"], "node"
                )
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
        )
        with pytest.raises(ContractViolation, match="outstanding findings"):
            TeardownReport(
                allocation_id=ALLOCATION,
                assessment=released,
                intent=deliberate_intent(),
                findings=(Finding(resource="node", detail="still there"),),
            )

    def test_a_report_must_name_its_allocation_and_time_it_unambiguously(self) -> None:
        released = assess_release(
            {
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_terminated"], "node"
                )
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
        )
        with pytest.raises(ContractViolation, match="allocation_id"):
            TeardownReport(
                allocation_id="  ", assessment=released, intent=deliberate_intent()
            )
        with pytest.raises(ContractViolation, match="timezone-aware"):
            TeardownReport(
                allocation_id=ALLOCATION,
                assessment=released,
                intent=deliberate_intent(),
                reported_at=datetime(2026, 9, 17, 9, 30, 0),  # noqa: DTZ001
            )

    def test_a_finding_must_be_actionable(self) -> None:
        """Naming the orphaned ids is actionable, the way a bare count is not."""
        with pytest.raises(ContractViolation, match="name the resource"):
            Finding(resource=" ", detail="still present")
        with pytest.raises(ContractViolation, match="what is outstanding"):
            Finding(resource="node", detail="  ")


class TestReleasePathReportsProviderTruth:
    """`release_allocation` end to end — the path B's driver invokes."""

    def test_a_confirmed_release_reports_success(self) -> None:
        provider = RecordedProviderClient(
            allocation_response={
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_terminated"], "node"
                ),
                CLUSTER: observe_skypilot_status(
                    RESPONSES["skypilot"]["status_absent"], CLUSTER
                ),
            }
        )
        adapter, _, _ = build_adapter(provider)

        report = adapter.release_allocation(
            ALLOCATION,
            deliberate_intent(),
            allocation_resources=AllocationResources(
                ALLOCATION, frozenset({"node", CLUSTER})
            ),
        )

        assert report.succeeded is True
        assert report.exit_code == 0
        assert report.findings == ()

    def test_a_partial_release_reports_each_outstanding_resource(self) -> None:
        provider = RecordedProviderClient(
            allocation_response={
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_running"], "node"
                ),
                "volume": ProviderObservation(
                    presence=ProviderPresence.UNKNOWN,
                    queried_by="volume",
                    detail="DescribeVolumes threw ThrottlingException",
                ),
            }
        )
        adapter, _, _ = build_adapter(provider)

        report = adapter.release_allocation(
            ALLOCATION,
            deliberate_intent(),
            allocation_resources=AllocationResources(
                ALLOCATION, frozenset({"node", "volume"})
            ),
        )

        assert report.succeeded is False
        assert report.exit_code == 2
        assert {finding.resource for finding in report.findings} == {"node", "volume"}
        assert report.assessment.may_return_reservation_unused is False

    def test_a_credential_failure_never_reports_a_clean_release(self) -> None:
        provider = RecordedProviderClient(
            allocation_raises=PermissionError("assume-role denied")
        )
        adapter, _, _ = build_adapter(provider)

        report = adapter.release_allocation(
            ALLOCATION,
            deliberate_intent(),
            allocation_resources=AllocationResources(ALLOCATION, frozenset({"node"})),
        )

        assert report.credential_failure is True
        assert report.succeeded is False
        assert report.exit_code >= 1
        assert report.assessment.state is ReleaseState.UNRESOLVED

    def test_a_failed_re_check_leaves_the_allocation_on_the_books(self) -> None:
        provider = RecordedProviderClient(
            allocation_raises=ConnectionError("provider API unreachable")
        )
        adapter, _, _ = build_adapter(provider)

        report = adapter.release_allocation(
            ALLOCATION,
            deliberate_intent(),
            allocation_resources=AllocationResources(ALLOCATION, frozenset({"node"})),
        )

        assert report.credential_failure is False
        assert report.assessment.state is ReleaseState.UNRESOLVED
        assert report.assessment.exposure is CostExposure.UNRESOLVED
        assert report.exit_code >= 1


# --------------------------------------------------------------------------- #
# Acceptance 2 — accidental deletion is not deliberate retirement
# --------------------------------------------------------------------------- #


class TestDeliberateRetirementVersusAccident:
    """Stated expected behaviour, "or operators will read auto-repair as a bug"."""

    def test_an_accidental_deletion_is_expected_to_be_recreated(self) -> None:
        """Auto-repair restoring capacity nobody meant to lose is correct behaviour."""
        intent = ReleaseIntent(deliberate=False)
        assert intent.recreation_expected is True

    def test_a_deliberate_retirement_expects_no_recreation(self) -> None:
        assert deliberate_intent().recreation_expected is False

    def test_a_deliberate_release_must_stop_every_recreation_driver(self) -> None:
        """Withdrawing owner intent alone leaves the pod watcher to rebuild it."""
        with pytest.raises(ContractViolation, match="pending_workload"):
            ReleaseIntent(
                deliberate=True,
                stopped_drivers=frozenset({RecreationDriver.OWNER_INTENT}),
                requested_by="operator-1",
            )
        with pytest.raises(ContractViolation, match="owner_intent"):
            ReleaseIntent(
                deliberate=True,
                stopped_drivers=frozenset({RecreationDriver.PENDING_WORKLOAD}),
                requested_by="operator-1",
            )

    def test_a_deliberate_release_records_who_asked(self) -> None:
        """Without a requester there is no way to tell retirement from accident."""
        with pytest.raises(ContractViolation, match="who requested"):
            ReleaseIntent(deliberate=True, stopped_drivers=frozenset(RecreationDriver))

    def test_an_accident_cannot_claim_to_have_stopped_drivers(self) -> None:
        """An accidental deletion must stay repairable."""
        with pytest.raises(ContractViolation, match="accidental"):
            ReleaseIntent(
                deliberate=False,
                stopped_drivers=frozenset({RecreationDriver.OWNER_INTENT}),
            )

    def test_both_verified_recreation_drivers_are_enumerated(self) -> None:
        """Auto-repair and the unschedulable-pod path, both registered in main.go."""
        assert {driver.value for driver in RecreationDriver} == {
            "owner_intent",
            "pending_workload",
        }

    def test_an_accidental_release_still_reports_provider_truth(self) -> None:
        """Intent does not change what the provider said; it changes what to expect."""
        provider = RecordedProviderClient(
            allocation_response={
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_running"], "node"
                )
            }
        )
        adapter, _, _ = build_adapter(provider)

        report = adapter.release_allocation(
            ALLOCATION,
            ReleaseIntent(deliberate=False),
            allocation_resources=AllocationResources(ALLOCATION, frozenset({"node"})),
        )

        assert report.intent.recreation_expected is True
        assert report.succeeded is False
        assert report.assessment.state is ReleaseState.RETAINED


# --------------------------------------------------------------------------- #
# Boundaries A must not cross
# --------------------------------------------------------------------------- #


class TestScopeBoundaries:
    """The things this unit deliberately does not contain."""

    def test_the_package_owns_no_lifecycle_machinery(self) -> None:
        """No scheduler, timer, queue or thread anywhere in the contracts package.

        A structural check rather than a documented promise: acceptance 8 is met by
        B's independent-lifetime driver calling `release_allocation`, and a second
        lifecycle owner in the adapter is what the ownership split forbids.
        """
        package = Path(_contracts_path.PACKAGE_PARENT) / "superplane_contracts"
        forbidden = ("import threading", "import asyncio", "import sched", "time.sleep")
        for source in sorted(package.glob("*.py")):
            text = source.read_text()
            for marker in forbidden:
                assert marker not in text, f"{source.name} contains {marker}"

    def test_the_adapter_holds_no_operation_state_between_calls(self) -> None:
        """Two operations through one adapter do not see each other."""
        provider = RecordedProviderClient()
        adapter, store, _ = build_adapter(provider)

        for key in ("idem-0001", "idem-0002"):
            result = adapter.perform_operation(
                operation=OperationKind.SUBMIT,
                resource_name=CLUSTER,
                idempotency_key=key,
                allocation_id=ALLOCATION,
                workspace=WORKSPACE,
            )
            assert result.record.durable is True

        assert set(store.records) == {"idem-0001", "idem-0002"}

    def test_no_budget_or_reservation_arithmetic_lives_here(self) -> None:
        """C owns the ledger. A reports whether a clearance is permitted."""
        assessment = assess_release(
            {
                "node": observe_ec2(
                    RESPONSES["ec2"]["describe_instances_running"], "node"
                )
            },
            allocation=AllocationResources(ALLOCATION, frozenset({"node"})),
        )
        assert not hasattr(assessment, "balance")
        assert not hasattr(assessment, "spend_usd")
        assert isinstance(assessment.exposure, CostExposure)

    def test_a_handle_carries_no_retry_or_attempt_state(self) -> None:
        """`attempt_id` and its fencing are B's; a handle that counted would fork it."""
        handle = make_handle()
        for absent in ("attempt", "attempt_id", "retries", "backoff"):
            assert not hasattr(handle, absent)


@pytest.mark.parametrize("presence", [None, "absent", False, "invented"])
def test_unrecognized_presence_cannot_clear_release(presence):
    with pytest.raises(ContractViolation, match="presence"):
        ProviderObservation(presence=presence, queried_by="resource")


def test_untyped_observation_cannot_clear_release():
    from types import SimpleNamespace

    with pytest.raises(ContractViolation, match="typed"):
        assess_release(
            {"resource": SimpleNamespace(presence="absent")},
            allocation=AllocationResources(ALLOCATION, frozenset({"resource"})),
        )


def test_other_resource_absence_cannot_authorize_a_retry():
    handle = ProviderHandle(
        OperationKind.PROVISION,
        "provider",
        "intended-resource",
        "key",
        "allocation",
        "workspace",
    )
    request = ReconcileRequest(handle, CallOutcome.AMBIGUOUS, "mock-authority")
    result = reconcile(
        request, ProviderObservation(ProviderPresence.ABSENT, "different-resource")
    )
    assert result.result is ReconcileResult.UNRESOLVED
    assert not result.may_repeat_operation


def test_other_allocation_cannot_reconcile_a_recorded_handle():
    from unittest.mock import Mock
    from datetime import datetime, timezone

    handle = ProviderHandle(
        OperationKind.PROVISION,
        "provider",
        "resource",
        "key",
        "allocation-one",
        "workspace",
    )
    authority, provider = Mock(), Mock()
    adapter = ProviderAdapter(Mock(), provider, authority, "provider")
    record = HandleRecord(handle, True, datetime.now(timezone.utc))
    with pytest.raises(ContractViolation, match="allocation/provider"):
        adapter.reconcile_recorded_handle(record, "allocation-two")
    authority.authority_for.assert_not_called()
    provider.observe.assert_not_called()


def test_release_reporting_requires_active_authority():
    from unittest.mock import Mock

    authority, provider = Mock(), Mock()
    authority.authority_for.return_value = None
    adapter = ProviderAdapter(Mock(), provider, authority, "provider")
    with pytest.raises(ContractViolation, match="authority"):
        adapter.release_allocation(
            "allocation",
            None,
            allocation_resources=AllocationResources("allocation", frozenset({"node"})),
        )
    provider.observe_allocation.assert_not_called()


@pytest.mark.parametrize("presence", list(ProviderPresence))
def test_release_never_uses_another_resources_observation(presence):
    observation = ProviderObservation(
        presence=presence,
        queried_by="different-node",
        provider_state="running" if presence is ProviderPresence.PRESENT else None,
        detail="query unavailable" if presence is ProviderPresence.UNKNOWN else "",
    )
    provider = RecordedProviderClient(
        allocation_response={"allocated-node": observation}
    )
    adapter, _, _ = build_adapter(provider)
    report = adapter.release_allocation(
        ALLOCATION,
        deliberate_intent(),
        allocation_resources=AllocationResources(
            ALLOCATION, frozenset({"allocated-node"})
        ),
    )
    assert report.assessment.state is ReleaseState.UNRESOLVED
    assert report.assessment.exposure is CostExposure.UNRESOLVED
    assert not report.assessment.may_mark_released
    assert not report.assessment.may_return_reservation_unused
    assert report.exit_code == 1
    assert report.findings == (
        Finding(
            resource="allocated-node",
            detail="provider observation identified a different resource",
        ),
    )


def test_partial_release_retains_mismatched_and_present_resources_once():
    assessment = assess_release(
        {
            "wrong-query": ProviderObservation(
                presence=ProviderPresence.PRESENT,
                queried_by="other-node",
                provider_state="running",
            ),
            "present": ProviderObservation(
                presence=ProviderPresence.PRESENT,
                queried_by="present",
                provider_state="running",
            ),
            "gone": ProviderObservation(
                presence=ProviderPresence.ABSENT, queried_by="gone"
            ),
        },
        allocation=AllocationResources(
            ALLOCATION, frozenset({"wrong-query", "present", "gone"})
        ),
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert set(assessment.unresolved_resources) == {"wrong-query", "present"}
    assert len(assessment.unresolved_resources) == 2


@pytest.mark.parametrize(
    "returned",
    [
        {"foreign-node": ProviderObservation(ProviderPresence.ABSENT, "foreign-node")},
        {"compute": ProviderObservation(ProviderPresence.ABSENT, "compute")},
        {},
    ],
)
def test_release_requires_the_complete_authoritative_allocation_inventory(returned):
    inventory = AllocationResources(
        ALLOCATION, frozenset({"compute", "volume", "network"})
    )
    provider = RecordedProviderClient(allocation_response=returned)
    adapter, _, _ = build_adapter(provider)
    report = adapter.release_allocation(
        ALLOCATION, deliberate_intent(), allocation_resources=inventory
    )
    assert report.assessment.state is ReleaseState.UNRESOLVED
    assert not report.assessment.may_return_reservation_unused
    assert not report.succeeded and report.exit_code > 0
    assert {finding.resource for finding in report.findings} >= {"volume", "network"}
    assert len(report.findings) == len(report.assessment.unresolved_resources)


def test_complete_allocation_absence_is_the_only_clearance_path():
    inventory = AllocationResources(
        ALLOCATION, frozenset({"compute", "volume", "network"})
    )
    provider = RecordedProviderClient(
        allocation_response={
            "compute": ProviderObservation(ProviderPresence.ABSENT, "compute"),
            "volume": ProviderObservation(ProviderPresence.ABSENT, "volume"),
            "network": ProviderObservation(ProviderPresence.ABSENT, "network"),
        }
    )
    adapter, _, _ = build_adapter(provider)
    report = adapter.release_allocation(
        ALLOCATION, deliberate_intent(), allocation_resources=inventory
    )
    assert report.succeeded and report.exit_code == 0
    assert report.assessment.allocation_id == ALLOCATION


def test_foreign_inventory_is_refused_before_authority_or_provider_access():
    from unittest.mock import Mock

    authority, provider = Mock(), Mock()
    adapter = ProviderAdapter(Mock(), provider, authority, PROVIDER)
    with pytest.raises(ContractViolation, match="inventory"):
        adapter.release_allocation(
            ALLOCATION,
            deliberate_intent(),
            allocation_resources=AllocationResources(
                "other-allocation", frozenset({"node"})
            ),
        )
    authority.authority_for.assert_not_called()
    provider.observe_allocation.assert_not_called()


def test_release_report_cannot_relabel_another_allocations_assessment():
    assessment = assess_release(
        {"node": ProviderObservation(ProviderPresence.ABSENT, "node")},
        allocation=AllocationResources("other-allocation", frozenset({"node"})),
    )
    with pytest.raises(ContractViolation, match="another allocation"):
        TeardownReport(
            allocation_id=ALLOCATION, assessment=assessment, intent=deliberate_intent()
        )


@pytest.mark.parametrize(
    "resources", [None, "node", {"node"}, frozenset(), frozenset({""}), frozenset({1})]
)
def test_inventory_requires_explicit_immutable_resource_identifiers(resources):
    with pytest.raises(ContractViolation, match="resource set"):
        AllocationResources(ALLOCATION, resources)


@pytest.mark.parametrize("allocation_id", [None, " ", 1])
def test_inventory_requires_an_allocation_id(allocation_id):
    with pytest.raises(ContractViolation, match="allocation id"):
        AllocationResources(allocation_id, frozenset({"node"}))


def test_untyped_inventory_and_malformed_observation_maps_are_refused():
    with pytest.raises(ContractViolation, match="inventory"):
        assess_release({}, allocation=None)
    inventory = AllocationResources(ALLOCATION, frozenset({"node"}))
    with pytest.raises(ContractViolation, match="mapping"):
        assess_release([], allocation=inventory)
    with pytest.raises(ContractViolation, match="resource identifiers"):
        assess_release(
            {None: ProviderObservation(ProviderPresence.ABSENT, "node")},
            allocation=inventory,
        )
    with pytest.raises(ContractViolation, match="allocation id"):
        ReleaseAssessment(ReleaseState.RELEASED, CostExposure.NONE, "")
