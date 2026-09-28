"""SkyPilot provisioning → workspace EKS node join → Kubernetes scheduling.

Issue #5061 (U19), EPIC #4910. One of the three suites the story names.

The chain the story requires preserved is "SkyPilot provisioning, intended workspace EKS
node join, and Kubernetes workload scheduling". These tests exercise all three links and,
more importantly, exercise the three places U12 recorded the baseline chain as broken:

* the join is a real step, and a launch task that carried it would be refused;
* a node with no resolved Kubernetes name is never READY;
* activation material never appears in the launch task or the captured output.

No AWS, no cluster, no network. `SkyPilotClient`, `JoinTransport` and `NodeResolver` are
Protocols and the fakes below implement them, which is what makes the whole chain
testable offline. Nothing here constructs a `ParityResult`, so no run of this suite can
emit live-verified evidence — R18's parity criteria stay deferred.

The activation string used throughout is test-only material. It authenticates nothing:
the adapter takes activation as an argument precisely so no real credential is needed to
exercise the rules, and the value exists so the by-value leak check has something to
detect.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime

import _migration_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from migration.eks_join import (
    ACTIVATION_ENV_KEYS,
    JoinRequest,
    JoinStep,
    NodeReadiness,
    ProvisionedNode,
    SkyPilotClient,
    SkyPilotEksAdapter,
    WorkspaceClusterResolver,
    WorkloadPlacement,
    assert_no_activation_material,
)
from spike.baseline_inventory import CHAIN_GAPS, SKYPILOT_CLIENT_ENDPOINTS
from spike.fixtures import (
    SSE_LAUNCH_ERROR,
    SSE_LAUNCH_SUCCESS,
    STATUS_RESPONSE_EMPTY,
    STATUS_RESPONSE_STOPPED,
    STATUS_RESPONSE_UP,
)
from spike.harness import teardown as harness_teardown
from superplane_contracts import (
    CallOutcome,
    ContractViolation,
    ResolvedPrincipal,
    SecretMaterial,
)

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

# Test-only activation material. Not a credential: it grants nothing anywhere, and it is
# a distinctive string so the by-value leak check has something unambiguous to find.
ACTIVATION_PLAINTEXT = "test-only-activation-not-a-credential"

WORKSPACE_CLUSTER = "adp-dev-workspace-w1"


def principal(workspace_id: str = "ws-w1") -> ResolvedPrincipal:
    """A principal the *authority* resolved, not one a request body supplied."""
    return ResolvedPrincipal(
        subject="user-1", org_id="org-1", workspace_id=workspace_id
    )


def make_request(**overrides: object) -> JoinRequest:
    kwargs: dict[str, object] = {
        "cluster_name": "gpu-cluster-1",
        "principal": principal(),
        "activation": SecretMaterial(ACTIVATION_PLAINTEXT),
        "cloud": "aws",
        "gpu_type": "H100",
        "gpu_count": 8,
    }
    kwargs.update(overrides)
    return JoinRequest(**kwargs)  # type: ignore[arg-type]


@dataclass
class FakeClient:
    """A `SkyPilotClient` over canned baseline fixtures.

    Mutable and recording, unlike the frozen production types, because the assertions
    below are about *what the adapter called* — and a fake that cannot be interrogated
    turns "the join ran" into an inference.
    """

    stream_body: str = SSE_LAUNCH_SUCCESS
    down_stream_body: str | None = None
    status_records: list[dict[str, object]] = field(
        default_factory=lambda: list(STATUS_RESPONSE_UP)
    )
    launched: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    downed: list[tuple[str, bool]] = field(default_factory=list)

    def launch(self, cluster_name: str, task: dict[str, object]) -> str:
        self.launched.append((cluster_name, task))
        return "req-1"

    def stream(self, request_id: str) -> str:
        if request_id.startswith("req-down") and self.down_stream_body is not None:
            return self.down_stream_body
        return self.stream_body

    def status(self, cluster_name: str) -> list[dict[str, object]]:
        return self.status_records

    def down(self, cluster_name: str, purge: bool = False) -> str:
        self.downed.append((cluster_name, purge))
        return f"req-down-{len(self.downed)}"


@dataclass
class FakeTransport:
    """A `JoinTransport` that records what it was handed."""

    accept: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)
    revealed: list[str] = field(default_factory=list)

    def join(
        self,
        cluster_name: str,
        activation: SecretMaterial,
        *,
        workspace_cluster: str,
    ) -> bool:
        self.calls.append((cluster_name, workspace_cluster))
        # A real transport reveals the material to hand it to the node. Recorded here
        # so a test can assert the adapter passed the material through rather than a
        # placeholder — the join has to actually receive it.
        self.revealed.append(activation.reveal())
        return self.accept


@dataclass
class FakeResolver:
    """A `NodeResolver` whose `None` return is a first-class "not resolvable"."""

    node_name: str | None = "ip-10-0-1-23.ec2.internal"
    schedulable: bool = True
    resolved: list[str] = field(default_factory=list)

    def resolve_node_name(
        self, cluster_name: str, *, workspace_cluster: str
    ) -> str | None:
        self.resolved.append(cluster_name)
        return self.node_name

    def is_schedulable(self, node_name: str, *, workspace_cluster: str) -> bool:
        return self.schedulable


@dataclass
class FakeWorkspaceClusters:
    """Authority-owned workspace-to-cluster bindings, not request input."""

    bindings: dict[str, str] = field(
        default_factory=lambda: {
            "ws-w1": WORKSPACE_CLUSTER,
            "ws-w2": "adp-dev-workspace-w2",
        }
    )

    def for_workspace(self, workspace_id: str) -> str | None:
        return self.bindings.get(workspace_id)


def make_adapter(
    client: FakeClient | None = None,
    transport: FakeTransport | None = None,
    resolver: FakeResolver | None = None,
    workspace_clusters: FakeWorkspaceClusters | None = None,
    *,
    with_clock: bool = False,
) -> SkyPilotEksAdapter:
    return SkyPilotEksAdapter(
        client=client or FakeClient(),
        transport=transport or FakeTransport(),
        resolver=resolver or FakeResolver(),
        workspace_clusters=workspace_clusters or FakeWorkspaceClusters(),
        clock=(lambda: NOW) if with_clock else None,
    )


class TestBaselineSurfacePreserved:
    """The preserved half: the client contract stays the baseline's."""

    def test_the_fake_satisfies_the_client_protocol(self) -> None:
        """`SkyPilotClient` is runtime-checkable, so the fake's shape is checkable.

        If the protocol grew a method the baseline never had, this fails rather than
        the divergence being discovered at integration time.
        """
        assert isinstance(FakeClient(), SkyPilotClient)
        assert isinstance(FakeWorkspaceClusters(), WorkspaceClusterResolver)

    def test_the_client_surface_has_no_serve_method(self) -> None:
        """U12's `SKYPILOT_CLIENT_ENDPOINTS` records no serving endpoint.

        Adding one here would imply a serving path the baseline never had, and R18's
        serving-parity criterion is one of the three that stays deferred.
        """
        methods = {name for name in dir(SkyPilotClient) if not name.startswith("_")}
        assert not any("serve" in name.lower() for name in methods)

    def test_no_baseline_endpoint_is_a_serving_endpoint(self) -> None:
        """Asserted against U12's recorded endpoint list, not against this module.

        The list is `(method, path, go_method)` triples, and none of the three fields
        may mention serving — a `/serve` path and a `Serve` Go method are the same
        capability claim under two names.
        """
        recorded = " ".join(
            field.lower() for entry in SKYPILOT_CLIENT_ENDPOINTS for field in entry
        )
        assert "serve" not in recorded

    def test_the_launch_task_keeps_the_baselines_resources_only_shape(self) -> None:
        """`build_launch_task` is reused unchanged, so the task is resources only.

        The absence of `setup`/`run` *is* the baseline's behavior. This adapter keeps
        it and drives the join separately rather than smuggling the join in here.
        """
        task = make_request().launch_task()
        assert set(task) == {"resources"}

    def test_the_status_mapping_is_the_baselines(self) -> None:
        """Only UP is ready. INIT and STOPPED must not be reported as provisioned."""
        node = make_adapter(
            FakeClient(status_records=list(STATUS_RESPONSE_STOPPED))
        ).provision_and_join(make_request(), "alloc-1")
        assert node.cluster_status == "stopped"
        assert node.readiness is NodeReadiness.UNOBSERVED


class TestChainGapsAreTheWorkList:
    """The four gaps U12 recorded are the four this story had to decide."""

    def test_u12_recorded_exactly_four_gaps(self) -> None:
        """Pinned so a fifth gap added upstream fails here rather than going unanswered."""
        assert len(CHAIN_GAPS) == 4

    def test_every_gap_carries_a_decision_this_story_owed(self) -> None:
        """`u19_decision_required` is literally this story's work list."""
        assert all(gap.u19_decision_required.strip() for gap in CHAIN_GAPS)

    def test_the_three_execution_gaps_are_the_ones_this_module_answers(self) -> None:
        """The fourth, serving ownership, is answered in `handover.py`.

        Named explicitly so the split between the two modules is asserted rather than
        left as a reader's assumption.
        """
        ids = {gap.gap_id for gap in CHAIN_GAPS}
        assert {
            "launch-task-has-no-join-step",
            "k8s-node-name-never-assigned",
            "ssm-activation-credentials-in-task-envs",
        } <= ids


class TestActivationMaterialNeverLeaks:
    """`ssm-activation-credentials-in-task-envs`: refused by key and by value."""

    def test_activation_must_be_secret_material_not_a_string(self) -> None:
        """A bare string is a credential this adapter cannot keep out of logs."""
        with pytest.raises(ContractViolation, match="SecretMaterial"):
            make_request(activation=ACTIVATION_PLAINTEXT)

    def test_secret_material_redacts_itself_in_a_repr(self) -> None:
        """The property that makes an accidental log line harmless."""
        assert ACTIVATION_PLAINTEXT not in repr(SecretMaterial(ACTIVATION_PLAINTEXT))

    def test_the_launch_task_carries_no_activation_material(self) -> None:
        adapter = make_adapter()
        request = make_request()
        adapter.provision_and_join(request, "alloc-1")
        _, task = adapter.client.launched[0]  # type: ignore[union-attr]
        assert ACTIVATION_PLAINTEXT not in repr(task)

    @pytest.mark.parametrize("key", sorted(ACTIVATION_ENV_KEYS))
    def test_a_payload_is_refused_by_activation_key_name(self, key: str) -> None:
        """The baseline's actual shape: activation values as task env vars.

        Checked by key as well as by value, because a placeholder under the real key
        name is still the defect — the next deploy fills it in.
        """
        with pytest.raises(ContractViolation, match="activation material"):
            assert_no_activation_material({"envs": {key: "placeholder"}})

    def test_key_matching_is_case_and_separator_insensitive(self) -> None:
        """`SSM_ACTIVATION_CODE` and `ssm-activation-code` are the same leak.

        Matching only the exact lowercase spelling would miss the form the baseline
        actually used, which is upper-case.
        """
        with pytest.raises(ContractViolation, match="activation material"):
            assert_no_activation_material({"envs": {"SSM-Activation-Code": "x"}})

    def test_a_payload_is_refused_by_activation_value(self) -> None:
        """Catches the real code hidden under an innocuous key.

        Key-name matching cannot see this, which is why both checks exist.
        """
        secret = SecretMaterial(ACTIVATION_PLAINTEXT)
        with pytest.raises(ContractViolation, match="activation secret's value"):
            assert_no_activation_material(
                {"envs": {"harmless_name": ACTIVATION_PLAINTEXT}}, secret
            )

    def test_a_value_embedded_in_a_longer_string_is_still_refused(self) -> None:
        """A leak inside a rendered config line is the `config.local.env` case U12
        recorded, and a whole-string equality check would miss it."""
        secret = SecretMaterial(ACTIVATION_PLAINTEXT)
        with pytest.raises(ContractViolation, match="activation secret's value"):
            assert_no_activation_material(
                [f"[sky] writing code={ACTIVATION_PLAINTEXT} to config"], secret
            )

    def test_nested_structures_are_walked(self) -> None:
        secret = SecretMaterial(ACTIVATION_PLAINTEXT)
        with pytest.raises(ContractViolation, match="activation secret's value"):
            assert_no_activation_material(
                {"a": [{"b": ({"c": ACTIVATION_PLAINTEXT},)}]}, secret
            )

    def test_the_refusal_names_the_path_and_not_the_content(self) -> None:
        """The rule `secrets.py` follows: a message that quotes the secret leaks it.

        An error raised to prevent a credential ending up in a log must not itself put
        the credential in the log.
        """
        secret = SecretMaterial(ACTIVATION_PLAINTEXT)
        with pytest.raises(ContractViolation) as excinfo:
            assert_no_activation_material(
                {"envs": {"inner": {"code": ACTIVATION_PLAINTEXT}}}, secret
            )
        message = str(excinfo.value)
        assert ACTIVATION_PLAINTEXT not in message
        assert "envs.inner.code" in message

    def test_no_material_means_no_value_check(self) -> None:
        """Without the plaintext there is nothing to compare against.

        The key check still applies, so the guard is never a no-op.
        """
        assert_no_activation_material({"envs": {"harmless": "value"}})

    def test_captured_progress_output_is_checked_on_every_outcome(self) -> None:
        """Checked in `__post_init__`, so every constructed outcome is checked.

        Checking only where a test remembers to look would leave the property
        intended rather than enforced.
        """
        request = make_request()
        with pytest.raises(ContractViolation, match="progress output"):
            ProvisionedNode(
                request=request,
                steps_completed=(),
                cluster_status="ready",
                readiness=NodeReadiness.UNOBSERVED,
                handle=make_adapter()._handle(request, "alloc-1"),
                progress_lines=(f"[sky] code={ACTIVATION_PLAINTEXT}",),
            )

    def test_the_join_transport_does_receive_the_real_material(self) -> None:
        """The material has to reach the node, or the join could not work.

        Keeping it out of logs is not the same as never delivering it; asserting both
        directions stops a "fix" that simply drops the credential.
        """
        transport = FakeTransport()
        make_adapter(transport=transport).provision_and_join(make_request(), "alloc-1")
        assert transport.revealed == [ACTIVATION_PLAINTEXT]


class TestJoinRequestAuthority:
    """Body-supplied identity never grants authority."""

    def test_the_principal_must_be_a_resolved_principal(self) -> None:
        """The caller's half cannot supply the authority's half.

        There is no `user_id`/`org_id` string on this type for a requester to set, so
        a fabricated identity cannot be constructed at all.
        """
        with pytest.raises(ContractViolation, match="ResolvedPrincipal"):
            make_request(principal={"subject": "attacker", "workspace_id": "ws-w2"})

    def test_a_workspace_scoped_join_needs_a_principal_with_a_workspace(self) -> None:
        """Enforced by U9's contract, which is why the adapter does not re-check it.

        A principal without a workspace cannot be constructed at all, so a request
        carrying one cannot exist and the adapter needs no second copy of the rule —
        two copies of a tenant-scoping check can only drift apart.
        """
        with pytest.raises(ContractViolation, match="must carry a workspace"):
            principal(workspace_id="")

    def test_no_launch_happens_when_authority_is_missing(self) -> None:
        """The refusal is worth nothing if the provider was already called.

        A request whose principal is not a `ResolvedPrincipal` fails at construction,
        so there is no path from an unauthorized request to a launch.
        """
        client = FakeClient()
        with pytest.raises(ContractViolation):
            make_adapter(client).provision_and_join(
                make_request(principal={"workspace_id": "ws-w2"}), "alloc-1"
            )
        assert client.launched == []

    def test_the_request_cannot_supply_a_cluster_for_another_workspace(self) -> None:
        """The join target is derived from authority, so no body field can override it."""
        client = FakeClient()
        with pytest.raises(TypeError, match="workspace_cluster"):
            make_adapter(client).provision_and_join(
                make_request(workspace_cluster="adp-prod-other-tenant"), "alloc-1"
            )
        assert client.launched == []

    def test_missing_authoritative_cluster_refuses_before_launch(self) -> None:
        client = FakeClient()
        adapter = make_adapter(
            client,
            workspace_clusters=FakeWorkspaceClusters(bindings={}),
        )
        with pytest.raises(
            ContractViolation, match="no authoritative workspace cluster"
        ):
            adapter.provision_and_join(make_request(), "alloc-1")
        assert client.launched == []

    def test_the_authority_selects_the_principals_workspace_cluster(self) -> None:
        transport = FakeTransport()
        make_adapter(transport=transport).provision_and_join(
            make_request(principal=principal("ws-w2")), "alloc-1"
        )
        assert transport.calls == [("gpu-cluster-1", "adp-dev-workspace-w2")]

    @pytest.mark.parametrize("field_name", ["cluster_name", "cloud", "gpu_type"])
    def test_blank_required_field_is_refused(self, field_name: str) -> None:
        with pytest.raises(ContractViolation, match=field_name):
            make_request(**{field_name: "  "})

    def test_non_positive_gpu_count_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="gpu_count"):
            make_request(gpu_count=0)

    def test_the_handle_carries_the_principals_workspace(self) -> None:
        """Tenant scoping on the durable reference, not just on the request.

        A handle recorded under the wrong workspace is one another tenant's
        reconciliation could act on.
        """
        node = make_adapter().provision_and_join(make_request(), "alloc-1")
        assert node.handle.workspace == "ws-w1"

    def test_the_idempotency_key_is_the_cluster_name(self) -> None:
        """A repeated launch for the same cluster is the same operation.

        That is the property that makes a retry after an ambiguous response safe
        rather than a second machine.
        """
        node = make_adapter().provision_and_join(make_request(), "alloc-1")
        assert node.handle.idempotency_key == "gpu-cluster-1"


class TestProvisionJoinScheduleChain:
    """The whole chain, and every step recorded as a value rather than inferred."""

    def test_the_happy_path_completes_all_four_recorded_steps(self) -> None:
        node = make_adapter(with_clock=True).provision_and_join(
            make_request(), "alloc-1"
        )
        assert node.steps_completed == (
            JoinStep.PROVISION,
            JoinStep.JOIN,
            JoinStep.RESOLVE_NODE,
        )
        assert node.readiness is NodeReadiness.READY
        assert node.k8s_node_name == "ip-10-0-1-23.ec2.internal"
        assert node.observed_at == NOW

    def test_the_join_is_a_real_step_that_actually_runs(self) -> None:
        """`launch-task-has-no-join-step`: the baseline provisioned but never joined.

        Asserted against the transport's recorded call, so "the join ran" is an
        observation rather than an inference from the step list.
        """
        transport = FakeTransport()
        make_adapter(transport=transport).provision_and_join(make_request(), "alloc-1")
        assert transport.calls == [("gpu-cluster-1", WORKSPACE_CLUSTER)]

    def test_the_join_step_is_recorded_as_a_value(self) -> None:
        """A resolution that exists only as prose is one a later change can undo."""
        node = make_adapter().provision_and_join(make_request(), "alloc-1")
        assert JoinStep.JOIN in node.steps_completed

    def test_the_join_runs_only_after_the_cluster_reports_up(self) -> None:
        """Joining a machine still in INIT registers a node that cannot accept pods."""
        transport = FakeTransport()
        make_adapter(
            FakeClient(status_records=[{"name": "c", "status": "INIT"}]),
            transport=transport,
        ).provision_and_join(make_request(), "alloc-1")
        assert transport.calls == []

    def test_a_workload_schedules_onto_a_verified_joined_node(self) -> None:
        adapter = make_adapter()
        node = adapter.provision_and_join(make_request(), "alloc-1")
        placement = adapter.schedule(node, "job-1")
        assert placement.scheduled is True
        assert "ip-10-0-1-23.ec2.internal" in placement.reason

    def test_the_cancel_after_window_is_passed_through_unchanged(self) -> None:
        """Preserving the baseline's cancellation semantics rather than adding a
        second timeout model."""
        node = make_adapter().provision_and_join(
            make_request(), "alloc-1", cancel_after=0
        )
        assert node.progress_lines == ()
        assert node.readiness is NodeReadiness.UNOBSERVED


class TestNodeNameGatesReadiness:
    """`k8s-node-name-never-assigned`: READY is unconstructible without a node name."""

    def test_an_unresolvable_node_name_yields_unobserved_not_ready(self) -> None:
        """The baseline's exact defect, now explicit.

        The join was accepted but the Kubernetes node cannot be identified, so EKS
        membership is unverified and readiness must not be reported as ready.
        """
        node = make_adapter(resolver=FakeResolver(node_name=None)).provision_and_join(
            make_request(), "alloc-1"
        )
        assert node.readiness is NodeReadiness.UNOBSERVED
        assert node.membership_unverified is True
        assert node.k8s_node_name is None
        assert "membership is unverified" in (node.failure or "")

    def test_ready_without_a_node_name_cannot_be_constructed(self) -> None:
        """Enforced on the type, not only on the adapter path.

        Another unit building this outcome directly must hit the same refusal, or the
        invariant would hold only for callers who happen to go through the adapter.
        """
        request = make_request()
        with pytest.raises(ContractViolation, match="resolved Kubernetes node name"):
            ProvisionedNode(
                request=request,
                steps_completed=(JoinStep.PROVISION, JoinStep.JOIN),
                cluster_status="ready",
                readiness=NodeReadiness.READY,
                handle=make_adapter()._handle(request, "alloc-1"),
                k8s_node_name=None,
            )

    def test_ready_without_a_completed_join_cannot_be_constructed(self) -> None:
        """A node cannot be ready in the workspace cluster it never joined."""
        request = make_request()
        with pytest.raises(ContractViolation, match="completed the join step"):
            ProvisionedNode(
                request=request,
                steps_completed=(JoinStep.PROVISION,),
                cluster_status="ready",
                readiness=NodeReadiness.READY,
                handle=make_adapter()._handle(request, "alloc-1"),
                k8s_node_name="ip-10-0-1-23.ec2.internal",
            )

    def test_a_found_but_unschedulable_node_is_not_ready_not_unobserved(self) -> None:
        """Here a negative reading IS an observation.

        The node was found and Kubernetes reported it unschedulable — a real negative,
        distinct from every "could not tell" path.
        """
        node = make_adapter(
            resolver=FakeResolver(schedulable=False)
        ).provision_and_join(make_request(), "alloc-1")
        assert node.readiness is NodeReadiness.NOT_READY
        assert node.membership_unverified is False
        assert node.k8s_node_name == "ip-10-0-1-23.ec2.internal"

    def test_schedulable_is_derived_not_settable(self) -> None:
        """It gates placing a workload, so it is derived from the observation."""
        node = make_adapter().provision_and_join(make_request(), "alloc-1")
        with pytest.raises(AttributeError):
            node.schedulable = False  # type: ignore[misc]

    def test_the_outcome_is_frozen(self) -> None:
        node = make_adapter().provision_and_join(make_request(), "alloc-1")
        with pytest.raises(FrozenInstanceError):
            node.readiness = NodeReadiness.NOT_READY  # type: ignore[misc]

    def test_naive_observed_at_is_refused(self) -> None:
        request = make_request()
        with pytest.raises(ContractViolation, match="timezone-aware"):
            ProvisionedNode(
                request=request,
                steps_completed=(),
                cluster_status="unknown",
                readiness=NodeReadiness.UNOBSERVED,
                handle=make_adapter()._handle(request, "alloc-1"),
                observed_at=datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            )

    def test_a_clock_returning_a_naive_datetime_is_refused(self) -> None:
        """The adapter will not launder a naive clock into a recorded observation."""
        adapter = SkyPilotEksAdapter(
            client=FakeClient(),
            transport=FakeTransport(),
            resolver=FakeResolver(),
            workspace_clusters=FakeWorkspaceClusters(),
            clock=lambda: datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
        )
        with pytest.raises(ContractViolation, match="timezone-aware"):
            adapter.provision_and_join(make_request(), "alloc-1")

    def test_no_clock_means_no_invented_timestamp(self) -> None:
        """The adapter should not manufacture a time it has no source for."""
        node = make_adapter().provision_and_join(make_request(), "alloc-1")
        assert node.observed_at is None


class TestFailurePathsReportUnobservedNotFailure:
    """The machine exists and is billing, so "could not tell" is never "not ready"."""

    def test_a_failed_launch_reports_unobserved_and_preserves_the_error(self) -> None:
        node = make_adapter(
            FakeClient(stream_body=SSE_LAUNCH_ERROR)
        ).provision_and_join(make_request(), "alloc-1")
        assert node.readiness is NodeReadiness.UNOBSERVED
        assert node.failure == "no capacity in region"

    def test_a_cancelled_launch_reports_unobserved_and_keeps_the_handle(self) -> None:
        """A cancelled launch may still have created provider resources.

        That is why the handle is built before the call and returned here — there has
        to be a reference to reconcile against. `cancel.cancelled-launch-releases-
        capacity` is a NOT_RUN baseline check, so nothing offline may claim the
        capacity was released.
        """
        node = make_adapter().provision_and_join(
            make_request(), "alloc-1", cancel_after=1
        )
        assert node.readiness is NodeReadiness.UNOBSERVED
        assert node.handle.resource_name == "gpu-cluster-1"
        assert "cancelled" in (node.failure or "")

    def test_an_empty_status_response_is_unobserved_not_ready(self) -> None:
        """No record is not evidence of a healthy cluster."""
        node = make_adapter(
            FakeClient(status_records=list(STATUS_RESPONSE_EMPTY))
        ).provision_and_join(make_request(), "alloc-1")
        assert node.cluster_status == "unknown"
        assert node.readiness is NodeReadiness.UNOBSERVED

    def test_a_rejected_join_is_unobserved_not_a_negative_observation(self) -> None:
        """Reporting NOT_READY here would invite a teardown of a running machine.

        The node may simply be unregistered, so the honest value is "not observed".
        """
        node = make_adapter(transport=FakeTransport(accept=False)).provision_and_join(
            make_request(), "alloc-1"
        )
        assert node.readiness is NodeReadiness.UNOBSERVED
        assert node.steps_completed == (JoinStep.PROVISION,)
        assert JoinStep.JOIN not in node.steps_completed

    def test_progress_lines_are_preserved_on_a_failure(self) -> None:
        """The captured output is the only diagnostic an operator has."""
        node = make_adapter(
            FakeClient(stream_body=SSE_LAUNCH_ERROR)
        ).provision_and_join(make_request(), "alloc-1")
        assert node.progress_lines == (
            "[sky] Launching cluster...",
            "[sky] no capacity in region",
        )


class TestSchedulingRefusals:
    """A workload never reaches a node whose membership was not verified."""

    @pytest.mark.parametrize(
        "resolver",
        [FakeResolver(node_name=None), FakeResolver(schedulable=False)],
        ids=["unobserved", "not_ready"],
    )
    def test_scheduling_is_refused_with_a_reason(self, resolver: FakeResolver) -> None:
        """A refusal, not an exception: "not usable yet" is a normal outcome the caller
        handles by waiting or relocating."""
        adapter = make_adapter(resolver=resolver)
        node = adapter.provision_and_join(make_request(), "alloc-1")
        placement = adapter.schedule(node, "job-1")
        assert placement.scheduled is False
        assert node.readiness.value in placement.reason

    def test_a_scheduled_placement_onto_a_non_ready_node_cannot_be_constructed(
        self,
    ) -> None:
        """The invariant on the type, so a caller bypassing `schedule` still hits it."""
        adapter = make_adapter(resolver=FakeResolver(node_name=None))
        node = adapter.provision_and_join(make_request(), "alloc-1")
        with pytest.raises(ContractViolation, match="not READY"):
            WorkloadPlacement(node=node, workload_id="job-1", scheduled=True)

    def test_a_blank_workload_id_is_refused(self) -> None:
        node = make_adapter().provision_and_join(make_request(), "alloc-1")
        with pytest.raises(ContractViolation, match="workload_id"):
            WorkloadPlacement(node=node, workload_id="  ", scheduled=False)


class TestTeardownAndHandleDurability:
    """Cleanup claims nothing it cannot observe; durability is the store's word."""

    def test_a_successful_teardown_reports_succeeded(self) -> None:
        adapter = make_adapter()
        node = adapter.provision_and_join(make_request(), "alloc-1")
        released, outcome = adapter.cancel(node)
        assert released is True
        assert outcome is CallOutcome.SUCCEEDED
        assert adapter.client.downed == [("gpu-cluster-1", False)]  # type: ignore[union-attr]

    def test_a_failed_teardown_is_ambiguous_not_failed(self) -> None:
        """A failed Down has not established that the provider holds nothing.

        `reconcile` needs to be told AMBIGUOUS so it asks the provider, rather than
        FAILED which would permit a retry against a resource that may exist.

        The failure is injected through the harness's own `first_attempt_fails` /
        `purge_fails` parameters rather than a hand-rolled stub, so what is exercised
        is the baseline's real two-attempt Down-then-purge sequence.
        """
        client = FakeClient(down_stream_body=SSE_LAUNCH_ERROR)
        adapter = make_adapter(client)
        node = adapter.provision_and_join(make_request(), "alloc-1")
        released, outcome = adapter.cancel(node)
        assert released is False
        assert outcome is CallOutcome.AMBIGUOUS
        assert outcome is not CallOutcome.FAILED
        assert client.downed == [
            ("gpu-cluster-1", False),
            ("gpu-cluster-1", True),
        ]

    def test_teardown_never_claims_provider_side_absence(self) -> None:
        """`cleanup.provider-side-absence-verified` is a NOT_RUN baseline check.

        Nothing offline establishes that the provider holds nothing, so a successful
        Down is a successful call and not evidence of absence.
        """
        outcome = harness_teardown("gpu-cluster-1")
        assert outcome.provider_absence_confirmed is False

    def test_a_handle_record_requires_the_stores_confirmation(self) -> None:
        """`durable=True` needs the instant persistence acknowledged it.

        A caller cannot claim durability without a confirmation timestamp, which is
        what stops "we wrote it" from standing in for "it is stored".
        """
        adapter = make_adapter()
        node = adapter.provision_and_join(make_request(), "alloc-1")
        record = adapter.record_handle(node, NOW)
        assert record.durable is True
        assert record.confirmed_at == NOW

    def test_the_handle_exists_before_the_provider_call(self) -> None:
        """Record-then-call: a lost response still leaves a reference to reconcile.

        Demonstrated on the cancelled path, where the response never arrived and the
        handle is nonetheless present.
        """
        node = make_adapter().provision_and_join(
            make_request(), "alloc-1", cancel_after=0
        )
        assert node.handle.allocation_id == "alloc-1"
        assert node.handle.resource_name == "gpu-cluster-1"
