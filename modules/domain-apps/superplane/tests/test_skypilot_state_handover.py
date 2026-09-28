"""Inventory, per-resource decisions, one owning controller, and rollback.

Issue #5061 (U19), EPIC #4910. One of the three suites the story names.

The property under test throughout is that this code **refuses** rather than defaults.
U12 recorded all five `EXISTING_STATE_CLASSES` with `default_decision="undecided"`, and
the tests below assert that an undecided resource blocks a cutover, that an empty
inventory is not a clean deployment, and that two controllers naming one resource is an
error at construction rather than a field somebody might not read.

No AWS, no cluster, no API server. Inventories are constructed values, which is the
point: this module enumerates nothing, so it cannot claim to have looked.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import _migration_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from migration.handover import (
    REQUIRED_STATE_KINDS,
    HandoverDecision,
    HandoverPlan,
    InventoriedResource,
    OwnershipConflictError,
    RollbackRecord,
    build_plan,
    rollback,
)
from spike.baseline_inventory import EXISTING_STATE_CLASSES, HANDOVER_DECISIONS
from superplane_contracts import (
    ContractViolation,
    CostExposure,
    HandleRecord,
    OperationKind,
    ProviderHandle,
    RecreationDriver,
    ReleaseIntent,
)

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
ALL_KINDS = frozenset(REQUIRED_STATE_KINDS)


def make_handle(
    resource_name: str = "cluster-1", *, durable: bool = True
) -> HandleRecord:
    """A handle record in U11's shape.

    `durable=True` requires `confirmed_at` in that contract — the timestamp *is* the
    persistence acknowledgement — so the two move together here.
    """
    handle = ProviderHandle(
        operation=OperationKind.PROVISION,
        provider="skypilot",
        resource_name=resource_name,
        idempotency_key=f"idem-{resource_name}",
        allocation_id=f"alloc-{resource_name}",
        workspace="ws-w1",
    )
    return HandleRecord(
        handle=handle,
        durable=durable,
        confirmed_at=NOW if durable else None,
    )


def deliberate_release() -> ReleaseIntent:
    """A release somebody decided on, with every recreation driver stopped."""
    return ReleaseIntent(
        deliberate=True,
        stopped_drivers=frozenset(RecreationDriver),
        requested_by="operator-1",
    )


def accidental_release() -> ReleaseIntent:
    """A deletion nobody asked for. Must remain repairable."""
    return ReleaseIntent(deliberate=False, stopped_drivers=frozenset())


def adopted(
    kind: str = "skypilot_clusters",
    resource_id: str = "cluster-1",
    *,
    owning_controller: str = "adp-superplane-controller",
    previous_controller: str = "superplane-controller",
    exposure: CostExposure = CostExposure.ACTIVE,
) -> InventoriedResource:
    return InventoriedResource(
        kind=kind,
        resource_id=resource_id,
        decision=HandoverDecision.ADOPT,
        enumerated_by="POST /status with no cluster_names filter",
        owning_controller=owning_controller,
        previous_controller=previous_controller,
        handle=make_handle(resource_id),
        exposure=exposure,
    )


def nothing_found(kind: str) -> InventoriedResource:
    """A recorded 'we looked and there was nothing' finding for one state class."""
    return InventoriedResource(
        kind=kind,
        resource_id=f"{kind}:none",
        decision=HandoverDecision.NO_EXISTING_STATE,
        enumerated_by=f"enumerated {kind}; zero entries returned",
    )


class TestU12Agreement:
    """This module's vocabulary must stay the vocabulary U12's inventory recognizes."""

    def test_decision_values_match_u12s_tuple_exactly(self) -> None:
        """A drifting duplicate would record decisions against the wrong vocabulary.

        The module checks this at import time as well; this test is what makes the
        import-time check visible as a requirement rather than an implementation
        detail somebody could delete.
        """
        assert {member.value for member in HandoverDecision} == set(HANDOVER_DECISIONS)

    def test_required_kinds_are_derived_from_u12_not_retyped(self) -> None:
        """A state class added upstream must become one a plan needs a finding for.

        That direction matters: a forgotten state class is an orphaned resource that
        keeps billing.
        """
        assert REQUIRED_STATE_KINDS == frozenset(
            entry.kind for entry in EXISTING_STATE_CLASSES
        )

    def test_all_five_u12_state_classes_are_covered(self) -> None:
        assert len(REQUIRED_STATE_KINDS) == 5

    def test_undecided_is_a_member_rather_than_absent(self) -> None:
        """U12 recorded every class as `undecided`, so the value has to be nameable.

        Modelling "no decision" as an absence would make it indistinguishable from
        "not yet inventoried", and those need different follow-ups.
        """
        assert HandoverDecision.UNDECIDED.value == "undecided"


class TestInventoriedResourceValidation:
    """A finding must carry the evidence that makes it a finding."""

    def test_a_well_formed_adoption_is_accepted(self) -> None:
        resource = adopted()
        assert resource.is_decided is True
        assert resource.owning_controller == "adp-superplane-controller"

    @pytest.mark.parametrize("field_name", ["kind", "resource_id", "enumerated_by"])
    def test_blank_required_field_is_refused(self, field_name: str) -> None:
        kwargs: dict[str, object] = {
            "kind": "skypilot_clusters",
            "resource_id": "cluster-1",
            "decision": HandoverDecision.UNDECIDED,
            "enumerated_by": "POST /status",
        }
        kwargs[field_name] = "   "
        with pytest.raises(ContractViolation, match=field_name):
            InventoriedResource(**kwargs)  # type: ignore[arg-type]

    def test_enumerated_by_is_required_so_a_finding_carries_its_method(self) -> None:
        """A finding without a method is an assertion without evidence.

        This is the whole basis of the clean-deployment rule: "we looked and found
        nothing" must be distinguishable from "we did not look".
        """
        with pytest.raises(ContractViolation, match="enumerated_by"):
            InventoriedResource(
                kind="skypilot_clusters",
                resource_id="cluster-1",
                decision=HandoverDecision.NO_EXISTING_STATE,
                enumerated_by="",
            )

    def test_an_unknown_state_class_is_refused(self) -> None:
        """A finding about a class U12 does not record is a finding about nothing."""
        with pytest.raises(ContractViolation, match="unknown state class"):
            InventoriedResource(
                kind="invented_state_class",
                resource_id="x-1",
                decision=HandoverDecision.NO_EXISTING_STATE,
                enumerated_by="guessed",
            )

    def test_a_non_enum_decision_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="HandoverDecision"):
            InventoriedResource(
                kind="skypilot_clusters",
                resource_id="cluster-1",
                decision="adopt",  # type: ignore[arg-type]
                enumerated_by="POST /status",
            )

    @pytest.mark.parametrize(
        "decision", [HandoverDecision.ADOPT, HandoverDecision.DRAIN_RELAUNCH]
    )
    def test_an_actioned_resource_must_name_its_owning_controller(
        self, decision: HandoverDecision
    ) -> None:
        """Without a name, "exactly one owning controller" is unverifiable.

        A resource nobody claimed is the one reconciled by two controllers or by none,
        and both of those cost money.
        """
        with pytest.raises(ContractViolation, match="owning controller"):
            InventoriedResource(
                kind="skypilot_clusters",
                resource_id="cluster-1",
                decision=decision,
                enumerated_by="POST /status",
                owning_controller="",
                handle=make_handle(),
            )

    def test_an_adoption_must_carry_a_handle(self) -> None:
        """U12: adoption "requires the new controller to accept a handle it did not
        create". With no handle there is nothing to accept."""
        with pytest.raises(ContractViolation, match="handle"):
            InventoriedResource(
                kind="skypilot_clusters",
                resource_id="cluster-1",
                decision=HandoverDecision.ADOPT,
                enumerated_by="POST /status",
                owning_controller="adp-superplane-controller",
                handle=None,
            )

    def test_an_adoption_requires_a_durable_handle(self) -> None:
        """A non-durable handle is one a crash loses.

        That leaves an adopted cluster with no reference to reconcile against, which is
        precisely the orphan adoption is supposed to prevent.
        """
        with pytest.raises(ContractViolation, match="DURABLE"):
            InventoriedResource(
                kind="skypilot_clusters",
                resource_id="cluster-1",
                decision=HandoverDecision.ADOPT,
                enumerated_by="POST /status",
                owning_controller="adp-superplane-controller",
                handle=make_handle(durable=False),
            )

    def test_a_drain_relaunch_does_not_require_a_handle(self) -> None:
        """Draining ends the resource, so no handle is carried forward.

        Requiring one would force a caller to fabricate a reference to something it is
        about to destroy.
        """
        resource = InventoriedResource(
            kind="skyserve_services",
            resource_id="svc-1",
            decision=HandoverDecision.DRAIN_RELAUNCH,
            enumerated_by="listed SkyServe services",
            owning_controller="adp-superplane-controller",
        )
        assert resource.handle is None

    def test_a_no_existing_state_finding_cannot_carry_a_handle(self) -> None:
        """A handle is a reference to something that exists, which contradicts the
        finding."""
        with pytest.raises(ContractViolation, match="no_existing_state"):
            InventoriedResource(
                kind="skypilot_clusters",
                resource_id="cluster-1",
                decision=HandoverDecision.NO_EXISTING_STATE,
                enumerated_by="POST /status returned zero clusters",
                handle=make_handle(),
            )

    def test_a_no_existing_state_finding_cannot_name_an_owner(self) -> None:
        with pytest.raises(ContractViolation, match="no_existing_state"):
            InventoriedResource(
                kind="skypilot_clusters",
                resource_id="cluster-1",
                decision=HandoverDecision.NO_EXISTING_STATE,
                enumerated_by="POST /status returned zero clusters",
                owning_controller="adp-superplane-controller",
            )

    def test_exposure_defaults_to_unresolved_not_none(self) -> None:
        """Zero is the expensive claim to get wrong, so it is never the default.

        A resource nobody costed is unknown exposure, not free.
        """
        resource = InventoriedResource(
            kind="skypilot_clusters",
            resource_id="cluster-1",
            decision=HandoverDecision.UNDECIDED,
            enumerated_by="POST /status",
        )
        assert resource.exposure is CostExposure.UNRESOLVED

    def test_deliberately_released_reads_u11s_release_intent(self) -> None:
        """Reusing the contract that already carries the distinction rollback needs."""
        resource = InventoriedResource(
            kind="skypilot_clusters",
            resource_id="cluster-1",
            decision=HandoverDecision.DRAIN_RELAUNCH,
            enumerated_by="POST /status",
            owning_controller="adp-superplane-controller",
            release_intent=deliberate_release(),
        )
        assert resource.deliberately_released is True

    def test_an_accidental_deletion_is_not_a_deliberate_release(self) -> None:
        """It must remain repairable, so rollback treats it differently."""
        resource = InventoriedResource(
            kind="skypilot_clusters",
            resource_id="cluster-1",
            decision=HandoverDecision.DRAIN_RELAUNCH,
            enumerated_by="POST /status",
            owning_controller="adp-superplane-controller",
            release_intent=accidental_release(),
        )
        assert resource.deliberately_released is False

    def test_no_release_intent_is_not_a_deliberate_release(self) -> None:
        assert adopted().deliberately_released is False

    def test_is_frozen(self) -> None:
        """So a validated finding cannot be edited into an unvalidated one."""
        with pytest.raises(FrozenInstanceError):
            adopted().decision = HandoverDecision.NO_EXISTING_STATE  # type: ignore[misc]


class TestBuildPlanOwnership:
    """Exactly one owning controller per resource, enforced at construction."""

    def test_two_controllers_for_one_resource_is_an_error_not_a_field(self) -> None:
        """A conflict reported inside an executable plan is a conflict that executes.

        Two controllers reconciling one GPU cluster both act on it — one scaling it up
        while the other tears it down.
        """
        with pytest.raises(OwnershipConflictError) as excinfo:
            build_plan(
                (
                    adopted(resource_id="cluster-1", owning_controller="controller-a"),
                    adopted(resource_id="cluster-1", owning_controller="controller-b"),
                ),
                frozenset({"skypilot_clusters"}),
            )
        assert "cluster-1" in str(excinfo.value)
        assert "controller-a" in str(excinfo.value)
        assert "controller-b" in str(excinfo.value)

    def test_the_conflict_error_names_never_restarting_the_old_controller(self) -> None:
        """The story's rule, stated where an operator reading the failure will see it."""
        with pytest.raises(OwnershipConflictError, match="Never restart an old"):
            build_plan(
                (
                    adopted(resource_id="c-1", owning_controller="controller-a"),
                    adopted(resource_id="c-1", owning_controller="controller-b"),
                ),
                frozenset({"skypilot_clusters"}),
            )

    def test_the_conflict_error_is_a_contract_violation(self) -> None:
        """So a caller catching contract violations at its boundary catches this too."""
        with pytest.raises(ContractViolation):
            build_plan(
                (
                    adopted(resource_id="c-1", owning_controller="controller-a"),
                    adopted(resource_id="c-1", owning_controller="controller-b"),
                ),
                frozenset({"skypilot_clusters"}),
            )

    def test_the_error_carries_the_structured_conflicts(self) -> None:
        with pytest.raises(OwnershipConflictError) as excinfo:
            build_plan(
                (
                    adopted(resource_id="c-1", owning_controller="controller-b"),
                    adopted(resource_id="c-1", owning_controller="controller-a"),
                ),
                frozenset({"skypilot_clusters"}),
            )
        conflicts = excinfo.value.conflicts
        assert len(conflicts) == 1
        assert conflicts[0].resource_id == "c-1"
        assert conflicts[0].controllers == ("controller-a", "controller-b")

    def test_the_same_controller_named_twice_is_not_a_conflict(self) -> None:
        """Re-inventorying a resource is normal; the check counts distinct controllers.

        If a repeat listing were an error, running the inventory twice would fail the
        cutover for no reason.
        """
        plan = build_plan(
            (
                adopted(resource_id="c-1", owning_controller="controller-a"),
                adopted(resource_id="c-1", owning_controller="controller-a"),
            ),
            frozenset({"skypilot_clusters"}),
        )
        assert plan.owners == {"c-1": "controller-a"}

    def test_one_resource_cannot_have_conflicting_decisions(self) -> None:
        with pytest.raises(ContractViolation, match="conflicting handover decisions"):
            build_plan(
                (
                    adopted(resource_id="c-1", owning_controller="controller-a"),
                    InventoriedResource(
                        kind="skypilot_clusters",
                        resource_id="c-1",
                        decision=HandoverDecision.NO_EXISTING_STATE,
                        enumerated_by="POST /status returned no matching entries",
                    ),
                ),
                frozenset({"skypilot_clusters"}),
            )

    def test_different_resources_may_have_different_owners(self) -> None:
        plan = build_plan(
            (
                adopted(resource_id="c-1", owning_controller="controller-a"),
                adopted(resource_id="c-2", owning_controller="controller-b"),
            ),
            frozenset({"skypilot_clusters"}),
        )
        assert plan.owners == {"c-1": "controller-a", "c-2": "controller-b"}

    def test_serving_resources_go_through_the_same_ownership_check(self) -> None:
        """U12's fourth chain gap, `no-owning-controller-for-serving`.

        Answered by naming an owner rather than by declaring serving out of scope: a
        serving resource with no owner cannot be constructed at all.
        """
        with pytest.raises(ContractViolation, match="owning controller"):
            InventoriedResource(
                kind="skyserve_services",
                resource_id="svc-1",
                decision=HandoverDecision.DRAIN_RELAUNCH,
                enumerated_by="listed SkyServe services",
                owning_controller="",
            )

    def test_a_serving_resource_with_an_owner_is_accepted(self) -> None:
        plan = build_plan(
            (
                InventoriedResource(
                    kind="skyserve_services",
                    resource_id="svc-1",
                    decision=HandoverDecision.DRAIN_RELAUNCH,
                    enumerated_by="listed SkyServe services",
                    owning_controller="adp-superplane-controller",
                ),
            ),
            frozenset({"skyserve_services"}),
        )
        assert plan.owners == {"svc-1": "adp-superplane-controller"}


class TestBuildPlanEnumerationBookkeeping:
    """What was enumerated is the caller's statement, not derivable from what was found."""

    def test_an_unknown_checked_kind_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="unknown state class"):
            build_plan((), frozenset({"made_up_kind"}))

    def test_a_finding_for_an_unenumerated_class_is_refused(self) -> None:
        """Otherwise `unchecked_kinds` would under-report.

        A finding recorded for a class the plan does not claim to have checked makes
        the plan's own coverage statement wrong.
        """
        with pytest.raises(ContractViolation, match="unenumerated"):
            build_plan((adopted(),), frozenset({"skyserve_services"}))

    def test_kinds_checked_is_separate_from_findings(self) -> None:
        """A class checked and found empty differs from a class nobody enumerated.

        Both produce no live resources; only one is evidence.
        """
        plan = build_plan(
            (nothing_found("skypilot_clusters"),), frozenset({"skypilot_clusters"})
        )
        assert plan.kinds_checked == frozenset({"skypilot_clusters"})
        assert "skypilot_clusters" not in plan.unchecked_kinds

    def test_unchecked_kinds_names_every_class_without_a_finding(self) -> None:
        plan = build_plan(
            (nothing_found("skypilot_clusters"),), frozenset({"skypilot_clusters"})
        )
        assert set(plan.unchecked_kinds) == ALL_KINDS - {"skypilot_clusters"}

    def test_unchecked_kinds_is_sorted_for_a_stable_message(self) -> None:
        plan = build_plan((), frozenset())
        assert list(plan.unchecked_kinds) == sorted(plan.unchecked_kinds)

    def test_naive_planned_at_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            HandoverPlan(
                resources=(),
                kinds_checked=ALL_KINDS,
                planned_at=datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            )

    def test_planned_at_is_carried_through(self) -> None:
        plan = build_plan(
            (nothing_found(kind) for kind in ()),  # type: ignore[arg-type]
            frozenset(),
            planned_at=NOW,
        )
        assert plan.planned_at == NOW


class TestPlanExecutability:
    """A plan refuses to be executable while anything is undecided or unchecked."""

    def test_an_undecided_resource_blocks_the_plan(self) -> None:
        """U12 recorded all five classes as undecided, and that is the finding.

        Draining is workload-affecting, so the decision needs the resource owner's
        agreement rather than a default.
        """
        plan = build_plan(
            tuple(
                nothing_found(kind)
                for kind in sorted(ALL_KINDS - {"skypilot_clusters"})
            )
            + (
                InventoriedResource(
                    kind="skypilot_clusters",
                    resource_id="cluster-1",
                    decision=HandoverDecision.UNDECIDED,
                    enumerated_by="POST /status",
                ),
            ),
            ALL_KINDS,
        )
        assert plan.executable is False
        assert plan.undecided[0].resource_id == "cluster-1"

    def test_the_blocking_reason_names_the_undecided_resources(self) -> None:
        """An operator needs to know which resource is waiting on whom."""
        plan = build_plan(
            tuple(
                nothing_found(kind)
                for kind in sorted(ALL_KINDS - {"skypilot_clusters"})
            )
            + (
                InventoriedResource(
                    kind="skypilot_clusters",
                    resource_id="cluster-1",
                    decision=HandoverDecision.UNDECIDED,
                    enumerated_by="POST /status",
                ),
            ),
            ALL_KINDS,
        )
        assert "cluster-1" in plan.blocking_reason
        assert "workload-affecting" in plan.blocking_reason

    def test_an_unchecked_state_class_blocks_the_plan(self) -> None:
        """The state nobody looked for is the state that orphans running clusters."""
        plan = build_plan(
            (nothing_found("skypilot_clusters"),), frozenset({"skypilot_clusters"})
        )
        assert plan.executable is False
        assert "no finding recorded" in plan.blocking_reason

    def test_the_blocking_reason_names_the_unchecked_classes(self) -> None:
        plan = build_plan(
            (nothing_found("skypilot_clusters"),), frozenset({"skypilot_clusters"})
        )
        assert "skyserve_services" in plan.blocking_reason

    def test_a_fully_decided_fully_checked_plan_is_executable(self) -> None:
        resources = tuple(
            nothing_found(kind) for kind in sorted(ALL_KINDS - {"skypilot_clusters"})
        ) + (adopted(),)
        plan = build_plan(resources, ALL_KINDS, planned_at=NOW)
        assert plan.executable is True
        assert plan.blocking_reason == ""

    def test_both_blocking_reasons_are_reported_together(self) -> None:
        """An operator fixing one and finding another is a second round-trip."""
        plan = build_plan(
            (
                InventoriedResource(
                    kind="skypilot_clusters",
                    resource_id="cluster-1",
                    decision=HandoverDecision.UNDECIDED,
                    enumerated_by="POST /status",
                ),
            ),
            frozenset({"skypilot_clusters"}),
        )
        assert "no finding recorded" in plan.blocking_reason
        assert "awaiting the resource owner's decision" in plan.blocking_reason

    def test_executable_is_derived_not_settable(self) -> None:
        """It gates a cutover, so it cannot be something a caller asserts."""
        plan = build_plan((), frozenset())
        with pytest.raises(AttributeError):
            plan.executable = True  # type: ignore[misc]


class TestCleanDeploymentEvidence:
    """ "No existing state" is a positive finding per class, never an empty list."""

    def test_all_five_classes_found_empty_is_a_clean_deployment(self) -> None:
        plan = build_plan(
            tuple(nothing_found(kind) for kind in sorted(ALL_KINDS)),
            ALL_KINDS,
            planned_at=NOW,
        )
        assert plan.clean_deployment is True
        assert plan.executable is True

    def test_an_empty_inventory_is_not_a_clean_deployment(self) -> None:
        """The silent assumption this property exists to refuse.

        An empty inventory is ambiguous between "we looked and there is nothing" and
        "we did not look", and a redeploy onto fresh storage under the second reading
        orphans every running cluster.
        """
        plan = build_plan((), frozenset())
        assert plan.clean_deployment is False

    def test_claiming_every_class_was_checked_with_no_findings_is_still_not_clean(
        self,
    ) -> None:
        """Because a checked class with nothing found must say so as a finding.

        Otherwise "checked" degenerates into a flag a caller can set without having
        enumerated anything.
        """
        plan = HandoverPlan(resources=(), kinds_checked=ALL_KINDS)
        assert plan.clean_deployment is False
        assert plan.executable is False
        assert "lack an enumerated_by finding" in plan.blocking_reason

    def test_one_live_resource_means_it_is_not_a_clean_deployment(self) -> None:
        resources = tuple(
            nothing_found(kind) for kind in sorted(ALL_KINDS - {"skypilot_clusters"})
        ) + (adopted(),)
        plan = build_plan(resources, ALL_KINDS)
        assert plan.clean_deployment is False

    def test_a_partially_checked_environment_is_not_a_clean_deployment(self) -> None:
        plan = build_plan(
            (nothing_found("skypilot_clusters"),), frozenset({"skypilot_clusters"})
        )
        assert plan.clean_deployment is False

    def test_each_empty_finding_records_how_it_was_enumerated(self) -> None:
        """The evidence, not the conclusion. Without it the claim is unverifiable."""
        plan = build_plan(
            tuple(nothing_found(kind) for kind in sorted(ALL_KINDS)), ALL_KINDS
        )
        assert all(item.enumerated_by for item in plan.resources)


class TestRollback:
    """Ownership returns; deliberately released capacity does not come back."""

    def test_an_adopted_resource_returns_to_its_previous_controller(self) -> None:
        plan = build_plan((adopted(),), frozenset({"skypilot_clusters"}))
        record = rollback(plan, rolled_back_at=NOW)
        assert [item.resource_id for item in record.returned] == ["cluster-1"]
        assert record.returned[0].previous_controller == "superplane-controller"

    def test_deliberately_released_capacity_is_not_recreated(self) -> None:
        """U11: a deliberate release stopped every recreation driver.

        Recreating it would spend money undoing a decision that was correct.
        """
        released = InventoriedResource(
            kind="skypilot_clusters",
            resource_id="cluster-gone",
            decision=HandoverDecision.DRAIN_RELAUNCH,
            enumerated_by="POST /status",
            owning_controller="adp-superplane-controller",
            previous_controller="superplane-controller",
            release_intent=deliberate_release(),
        )
        record = rollback(build_plan((released,), frozenset({"skypilot_clusters"})))
        assert [item.resource_id for item in record.not_recreated] == ["cluster-gone"]
        assert record.returned == ()

    def test_the_record_answers_the_recreation_question_directly(self) -> None:
        """A reviewer should be able to ask rather than infer from an absence."""
        record = rollback(build_plan((adopted(),), frozenset({"skypilot_clusters"})))
        assert record.recreated_deliberately_released_capacity is False

    def test_an_accidental_deletion_is_returned_and_stays_repairable(self) -> None:
        """The other half of U11's distinction: nobody asked for this one."""
        deleted = InventoriedResource(
            kind="skypilot_clusters",
            resource_id="cluster-oops",
            decision=HandoverDecision.ADOPT,
            enumerated_by="POST /status",
            owning_controller="adp-superplane-controller",
            previous_controller="superplane-controller",
            handle=make_handle("cluster-oops"),
            release_intent=accidental_release(),
        )
        record = rollback(build_plan((deleted,), frozenset({"skypilot_clusters"})))
        assert [item.resource_id for item in record.returned] == ["cluster-oops"]

    def test_an_unresolved_resource_is_retained_not_dropped(self) -> None:
        """R15 acceptance 4's rule: unresolved allocations are reported, not erased."""
        plan = build_plan((adopted(),), frozenset({"skypilot_clusters"}))
        record = rollback(plan, unresolved_ids=frozenset({"cluster-1"}))
        assert [item.resource_id for item in record.unresolved] == ["cluster-1"]
        assert record.returned == ()

    def test_unresolved_wins_over_a_deliberate_release(self) -> None:
        """Intent is not observation.

        A resource whose disposition could not be established is unresolved whatever
        the plan meant to do with it.
        """
        released = InventoriedResource(
            kind="skypilot_clusters",
            resource_id="cluster-gone",
            decision=HandoverDecision.DRAIN_RELAUNCH,
            enumerated_by="POST /status",
            owning_controller="adp-superplane-controller",
            release_intent=deliberate_release(),
        )
        record = rollback(
            build_plan((released,), frozenset({"skypilot_clusters"})),
            unresolved_ids=frozenset({"cluster-gone"}),
        )
        assert [item.resource_id for item in record.unresolved] == ["cluster-gone"]
        assert record.not_recreated == ()

    def test_a_resource_with_no_previous_controller_is_unresolved(self) -> None:
        """Not quietly successful: there is nobody to hand it back to.

        A resource whose old owner is unknown is a state a human needs to see, not one
        a rollback can report as returned.
        """
        orphan = InventoriedResource(
            kind="superplane_node_crs",
            resource_id="node-cr-1",
            decision=HandoverDecision.ADOPT,
            enumerated_by="listed SuperplaneNode CRs",
            owning_controller="adp-superplane-controller",
            previous_controller="",
            handle=make_handle("node-cr-1"),
        )
        record = rollback(build_plan((orphan,), frozenset({"superplane_node_crs"})))
        assert [item.resource_id for item in record.unresolved] == ["node-cr-1"]

    def test_a_no_existing_state_finding_needs_no_hand_back(self) -> None:
        """The one case where an absent previous controller is not a problem.

        Nothing existed, so there is nothing outstanding and nothing to return.
        """
        plan = build_plan(
            tuple(nothing_found(kind) for kind in sorted(ALL_KINDS)), ALL_KINDS
        )
        record = rollback(plan)
        assert len(record.not_recreated) == 5
        assert record.unresolved == ()

    def test_every_planned_resource_appears_in_the_record(self) -> None:
        """Rollback "preserves records", so nothing may fall through the branches."""
        resources = (
            adopted(resource_id="c-1"),
            InventoriedResource(
                kind="skyserve_services",
                resource_id="svc-1",
                decision=HandoverDecision.DRAIN_RELAUNCH,
                enumerated_by="listed SkyServe services",
                owning_controller="adp-superplane-controller",
                release_intent=deliberate_release(),
            ),
            nothing_found("joined_eks_hybrid_nodes"),
        )
        plan = build_plan(
            resources,
            frozenset(
                {"skypilot_clusters", "skyserve_services", "joined_eks_hybrid_nodes"}
            ),
        )
        record = rollback(plan, unresolved_ids=frozenset({"c-1"}))
        assert record.records_preserved is True

    def test_the_plan_itself_is_retained_on_the_record(self) -> None:
        """So the decisions a rollback acted on stay readable alongside its outcome."""
        plan = build_plan((adopted(),), frozenset({"skypilot_clusters"}))
        assert rollback(plan).plan is plan

    def test_naive_rolled_back_at_is_refused(self) -> None:
        plan = build_plan((adopted(),), frozenset({"skypilot_clusters"}))
        with pytest.raises(ContractViolation, match="timezone-aware"):
            RollbackRecord(
                plan=plan,
                returned=(),
                not_recreated=(),
                unresolved=(),
                exposure=CostExposure.UNRESOLVED,
                rolled_back_at=datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            )


class TestRollbackExposure:
    """Unknown exposure is UNRESOLVED, never zero."""

    def test_any_unresolved_resource_makes_the_exposure_unresolved(self) -> None:
        plan = build_plan((adopted(),), frozenset({"skypilot_clusters"}))
        record = rollback(plan, unresolved_ids=frozenset({"cluster-1"}))
        assert record.exposure is CostExposure.UNRESOLVED

    def test_a_returned_active_resource_reports_active_exposure(self) -> None:
        """It is confirmed present, so cost continues to accrue."""
        plan = build_plan(
            (adopted(exposure=CostExposure.ACTIVE),), frozenset({"skypilot_clusters"})
        )
        assert rollback(plan).exposure is CostExposure.ACTIVE

    def test_a_returned_unresolved_resource_reports_unresolved_exposure(self) -> None:
        plan = build_plan(
            (adopted(exposure=CostExposure.UNRESOLVED),),
            frozenset({"skypilot_clusters"}),
        )
        assert rollback(plan).exposure is CostExposure.UNRESOLVED

    def test_a_returned_resource_is_never_reported_as_zero_exposure(self) -> None:
        """`NONE` requires provider-established absence, which nothing offline supplies.

        A handed-back cluster is still a cluster; reporting zero would be the claim
        `accounting.py` refuses to let local state make.
        """
        plan = build_plan(
            (adopted(exposure=CostExposure.NONE),), frozenset({"skypilot_clusters"})
        )
        assert rollback(plan).exposure is not CostExposure.NONE

    def test_a_clean_deployment_rollback_reports_no_exposure(self) -> None:
        """Nothing existed and nothing was returned, so there is nothing accruing."""
        plan = build_plan(
            tuple(nothing_found(kind) for kind in sorted(ALL_KINDS)), ALL_KINDS
        )
        assert rollback(plan).exposure is CostExposure.NONE

    def test_the_record_refuses_zero_exposure_alongside_unresolved_resources(
        self,
    ) -> None:
        """Constructed directly, because the invariant must hold for any caller.

        `rollback` never produces this pair, but the record is a value another unit can
        build, and zero-with-unresolved is the expensive mistake.
        """
        plan = build_plan((adopted(),), frozenset({"skypilot_clusters"}))
        with pytest.raises(ContractViolation, match="never as zero"):
            RollbackRecord(
                plan=plan,
                returned=(),
                not_recreated=(),
                unresolved=plan.resources,
                exposure=CostExposure.NONE,
            )
