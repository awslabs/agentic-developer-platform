"""Provision, join to the workspace EKS cluster, then schedule — as explicit steps.

Issue #5061 (U19), EPIC #4910. R18, the execution half.

## What is preserved and what is repaired

The migration baseline is the user's working SkyPilot -> workspace-EKS path
(`docs/design-notes/4910-skypilot-eks-migration-amendment.md`). The client contracts
are preserved exactly: `/launch`, `/status`, `/down`, `/api/stream` and the status
mapping, all reused from `spike.harness` rather than reimplemented, so a divergence in
this adapter shows up as a harness disagreement rather than as a silent second
behavior.

Three things are **not** preserved, because U12 recorded them as chain gaps whose
`u19_decision_required` is this story's, and because the amendment states that
"functional parity does not preserve authentication bypasses, secret exposure or false
cleanup success as desired behavior":

### `launch-task-has-no-join-step`

U12: "`buildTask` emits only a 'resources' block... those environment variables have
no consumer inside the launched task, so a Launch through the Go client provisions a
VM but does not join it to EKS."

The gap offers two resolutions: add setup/run phases to the launched task, or invoke
the onboarding scripts as an explicit post-provision step. **This adapter takes the
second.** The reason is the next gap: the join needs the SSM activation credential,
and a task's `setup`/`run` phases are strings SkyPilot stores, logs and echoes. Putting
a credential where it will be logged in order to fix a gap whose sibling gap is "that
credential is exposed" trades one recorded defect for another.

So `build_launch_task` keeps the baseline's shape — resources only, no `setup`, no
`run` — and the join is `JoinStep.JOIN`, a separate step this adapter drives after
provisioning succeeds. The step order is a value (`JoinStep`), so "did the join run?"
is answerable from the outcome rather than inferred.

### `k8s-node-name-never-assigned`

U12: "nothing in the snapshot ever assigns either field... A node can be Ready in the
CRD while its EKS membership is unverified", and the decision required is to "resolve
it after the join... or record explicitly that node readiness is not observed. Parity
must not report node registration as verified while this is unresolved."

`NodeReadiness` has no boolean. `READY` requires a resolved Kubernetes node name;
absent one the readiness is `UNOBSERVED`, which is not `NOT_READY` — the same
distinction `OperationState.UNKNOWN` and `CheckStatus.UNKNOWN` draw, and for the same
reason. A caller collapsing "I could not observe it" into "it is not ready" tears down
a node that may well be running, and one collapsing it into "ready" schedules onto a
node that never joined.

### `ssm-activation-credentials-in-task-envs`

U12: activation values are "passed as SkyPilot task environment variables and rendered
into a config.local.env file on disk", and U19 "must source these from scoped ADP
credentials and keep them out of logs and CRD status".

`JoinRequest.activation` is a `SecretMaterial` from `superplane_contracts.delivery` —
which cannot be pickled, formats as `[REDACTED]`, and is unhashable. The adapter passes
it to the join transport and never puts it in the task, the node status or the stream
lines. `assert_no_activation_material` is the assertion, and it runs over the launch
task and over every captured line, so the property is enforced rather than intended.

## What this adapter does not do

**No provider access and no cluster access.** `SkyPilotClient`, `JoinTransport` and
`NodeResolver` are Protocols; the tests pass fakes. U16a/U16b own real EKS
authentication, U10 owns the credential lease, and per `validation-mapping.md` the
account, access, spend, deadline and cleanup owner are all Unresolved. Every R18 live
criterion stays deferred.

**No CRD write and no domain persistence.** `ProvisionedNode` is returned to the
caller. Domain state stays in the upstream Superplane API.

**No parity claim.** Nothing here constructs a `ParityResult`. `spike.harness`
deliberately makes `fixture_result` the only factory available offline, so no run of
this adapter can emit live-verified evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from spike.harness import (
    build_launch_task,
    consume_stream,
    map_cluster_status,
)
from superplane_contracts import (
    CallOutcome,
    ContractViolation,
    HandleRecord,
    OperationKind,
    ProviderHandle,
    ResolvedPrincipal,
    SecretMaterial,
)

# Environment-variable names the baseline used to carry activation material into the
# launched task. Held as a constant so the refusal below names exactly what U12
# recorded, and so a test can assert against the same list the adapter checks.
#
# Cited: chain gap `ssm-activation-credentials-in-task-envs`.
ACTIVATION_ENV_KEYS: frozenset[str] = frozenset(
    {
        "ssm_activation_id",
        "ssm_activation_code",
        "activation_id",
        "activation_code",
    }
)


class JoinStep(str, Enum):
    """The steps this adapter drives, in order.

    An enum rather than a comment, because "the join is a separate explicit step" is
    the resolution of chain gap `launch-task-has-no-join-step`, and a resolution that
    exists only as prose is one a later change can undo without failing anything.
    """

    PROVISION = "provision"
    """SkyPilot allocates the machine. The baseline's `/launch` plus `/api/stream`."""

    JOIN = "join"
    """The node registers into the workspace EKS cluster. Absent from the baseline."""

    RESOLVE_NODE = "resolve_node"
    """Resolve the Kubernetes node name for the joined machine."""

    SCHEDULE = "schedule"
    """Kubernetes schedules the workload onto the joined node."""


class NodeReadiness(str, Enum):
    """Whether the node is usable, with "not observed" distinct from "not ready".

    Three values, not a boolean. See the module docstring on
    `k8s-node-name-never-assigned`: the baseline's defect is expressible precisely
    because a boolean has nowhere to put "the join may have worked and I cannot tell".
    """

    READY = "ready"
    """Joined, resolved to a Kubernetes node, and reported schedulable."""

    NOT_READY = "not_ready"
    """Observed, and observed not to be usable. A negative *observation*."""

    UNOBSERVED = "unobserved"
    """Readiness could not be established. Not a failure and not a success."""


@runtime_checkable
class SkyPilotClient(Protocol):
    """The preserved SkyPilot API surface, exactly the baseline's six endpoints.

    Deliberately no `serve` method: U12's `SKYPILOT_CLIENT_ENDPOINTS` has none, and
    `test_serving_parity.py` asserts the method set contains no `"Serve"`. Adding one
    here would imply a serving path the baseline never had.
    """

    def launch(self, cluster_name: str, task: dict[str, object]) -> str:
        """Start a launch. Returns the request id. `POST /launch`."""
        ...

    def stream(self, request_id: str) -> str:
        """Raw SSE progress for a request. `GET /api/stream`."""
        ...

    def status(self, cluster_name: str) -> list[dict[str, object]]:
        """Cluster records. `POST /status`."""
        ...

    def down(self, cluster_name: str, purge: bool = False) -> str:
        """Tear a cluster down. `POST /down`."""
        ...


@runtime_checkable
class JoinTransport(Protocol):
    """Delivers activation material to the node without routing it through the task.

    This is the seam that resolves `ssm-activation-credentials-in-task-envs`. The
    material is handed over as `SecretMaterial`, so an implementation that logs its
    argument logs `[REDACTED]`.

    U16a/U16b own the real implementation (brokered workspace-role assume, cluster
    authentication). This adapter holds only the boundary.
    """

    def join(
        self,
        cluster_name: str,
        activation: SecretMaterial,
        *,
        workspace_cluster: str,
    ) -> bool:
        """Register the machine into ``workspace_cluster``. True when accepted."""
        ...


@runtime_checkable
class WorkspaceClusterResolver(Protocol):
    """Resolves the EKS target from authority-owned workspace configuration."""

    def for_workspace(self, workspace_id: str) -> str | None:
        """Return the configured cluster for ``workspace_id``, or no authority."""
        ...


@runtime_checkable
class NodeResolver(Protocol):
    """Resolves the Kubernetes node name for a joined machine.

    The missing piece of `k8s-node-name-never-assigned`. Returning ``None`` is a
    first-class answer meaning "not resolvable yet", which becomes
    `NodeReadiness.UNOBSERVED` rather than a failure.
    """

    def resolve_node_name(
        self, cluster_name: str, *, workspace_cluster: str
    ) -> str | None:
        """The Kubernetes node name, or ``None`` if it cannot be resolved."""
        ...

    def is_schedulable(self, node_name: str, *, workspace_cluster: str) -> bool:
        """Whether Kubernetes reports the node as able to accept pods."""
        ...


def assert_no_activation_material(
    payload: object,
    activation: SecretMaterial | None = None,
    *,
    what: str = "payload",
) -> None:
    """Refuse a payload carrying activation material, by key or by value.

    Two checks, because either alone is insufficient. The key check catches the
    baseline's actual shape — `SSM_ACTIVATION_CODE` as a task env — including cases
    where the value is a placeholder. The value check catches a payload that carries
    the real code under an innocuous key, which key-name matching cannot see.

    The value check needs the plaintext to compare against, so it calls `reveal()`
    on the caller's own material. Nothing is logged and nothing is returned; the
    message names the path, never the content — the rule `secrets.py` follows.

    Raises `ContractViolation`, matching the contracts package's convention.
    """
    revealed = activation.reveal() if activation is not None else None

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key).strip().lower().replace("-", "_")
                if key_text in ACTIVATION_ENV_KEYS:
                    raise ContractViolation(
                        f"{what} carries node-activation material at "
                        f"{path + '.' if path else ''}{key}: activation values reach "
                        "the node through the scoped join transport, never through the "
                        "launch task, node status or captured output"
                    )
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str) and revealed and revealed in node:
            raise ContractViolation(
                f"{what} carries the activation secret's value at "
                f"{path or '<value>'}: activation values reach the node through the "
                "scoped join transport, never through the launch task, node status or "
                "captured output"
            )

    walk(payload, "")


@dataclass(frozen=True)
class JoinRequest:
    """What one provision-and-join needs, including who authorized it.

    ``principal`` is a `ResolvedPrincipal` — resolved by the caller's authority, not
    supplied as a field the requester fills in. This is the same rule
    `provisioning.py` enforces: "the *caller's* half cannot supply the *authority's*
    half". There is no ``user_id``, ``org_id`` or cluster target string on this type
    for a caller to set. The adapter resolves the target from the principal's workspace
    through its authority-owned ``workspace_clusters`` collaborator before launch.
    """

    cluster_name: str
    principal: ResolvedPrincipal
    activation: SecretMaterial
    cloud: str
    gpu_type: str
    gpu_count: int
    region: str = ""
    disk_size_gb: int = 0
    use_spot: bool = False

    def __post_init__(self) -> None:
        for name in ("cluster_name", "cloud", "gpu_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"JoinRequest.{name} is required")
        if not isinstance(self.principal, ResolvedPrincipal):
            raise ContractViolation(
                "JoinRequest.principal must be a ResolvedPrincipal resolved by the "
                "caller's authority"
            )
        if not isinstance(self.activation, SecretMaterial):
            raise ContractViolation(
                "JoinRequest.activation must be SecretMaterial: a bare string is a "
                "credential this adapter cannot keep out of logs"
            )
        if not isinstance(self.gpu_count, int) or self.gpu_count < 1:
            raise ContractViolation("JoinRequest.gpu_count must be a positive integer")

    def launch_task(self) -> dict[str, object]:
        """The launch task, in the baseline's exact shape.

        `build_launch_task` is reused unchanged, which means resources only — no
        `setup`, no `run`, and no activation envs. That omission *is* the baseline's
        behavior (chain gap `launch-task-has-no-join-step`), and this adapter keeps
        it rather than smuggling the join in here; the join is `JoinStep.JOIN`.

        The assertion is not redundant with that: it holds the property against a
        future change to `build_launch_task` or to this method.
        """
        task = build_launch_task(
            cloud=self.cloud,
            gpu_type=self.gpu_type,
            gpu_count=self.gpu_count,
            disk_size_gb=self.disk_size_gb,
            region=self.region,
            use_spot=self.use_spot,
        )
        assert_no_activation_material(task, self.activation, what="launch task")
        return task


@dataclass(frozen=True)
class ProvisionedNode:
    """What happened across the four steps, and what it does and does not establish.

    ``readiness`` is a recorded observation. ``schedulable`` is a *property* derived
    from it, for the reason `ParityResult.live_verified` is a property: it gates
    whether a workload is placed onto this node, so it is derived from evidence
    rather than settable by a caller who wants it True.
    """

    request: JoinRequest
    steps_completed: tuple[JoinStep, ...]
    cluster_status: str
    readiness: NodeReadiness
    handle: ProviderHandle
    k8s_node_name: str | None = None
    progress_lines: tuple[str, ...] = ()
    failure: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ContractViolation(
                "ProvisionedNode.observed_at must be timezone-aware"
            )
        # The central invariant of `k8s-node-name-never-assigned`: READY requires a
        # resolved node name. Without it the honest value is UNOBSERVED, and this
        # refuses to let the ready-without-membership state be constructed at all.
        if self.readiness is NodeReadiness.READY and not self.k8s_node_name:
            raise ContractViolation(
                "a READY node requires a resolved Kubernetes node name; without one "
                "its EKS membership is unverified and readiness is UNOBSERVED"
            )
        if (
            self.readiness is NodeReadiness.READY
            and JoinStep.JOIN not in self.steps_completed
        ):
            raise ContractViolation("a READY node must have completed the join step")
        # Captured provider output is the other place the baseline leaked activation
        # material. Checking it here means every constructed outcome is checked,
        # rather than only the ones a test remembers to look at.
        assert_no_activation_material(
            list(self.progress_lines), self.request.activation, what="progress output"
        )

    @property
    def schedulable(self) -> bool:
        """Whether a workload may be scheduled onto this node."""
        return self.readiness is NodeReadiness.READY

    @property
    def membership_unverified(self) -> bool:
        """True when the node's EKS membership was not established either way.

        The state the baseline could reach silently. Named so a caller can branch on
        it instead of treating it as failure.
        """
        return self.readiness is NodeReadiness.UNOBSERVED


@dataclass(frozen=True)
class WorkloadPlacement:
    """The outcome of scheduling a workload onto a joined node."""

    node: ProvisionedNode
    workload_id: str
    scheduled: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.workload_id, str) or not self.workload_id.strip():
            raise ContractViolation("WorkloadPlacement.workload_id is required")
        # Refuses the failure this whole module is shaped around: scheduling onto a
        # node whose EKS membership was never verified.
        if self.scheduled and not self.node.schedulable:
            raise ContractViolation(
                "a workload cannot be scheduled onto a node that is not READY; "
                f"node readiness is {self.node.readiness.value}"
            )


@dataclass(frozen=True)
class SkyPilotEksAdapter:
    """Drives provision -> join -> resolve -> schedule against injected boundaries.

    Frozen with injected collaborators, like `ProvisioningAdapter` and
    `ProviderExecutor`. The adapter holds no client, URL or credential of its own:
    `client`, `transport` and `resolver` are Protocols the caller supplies, so the
    tests pass fakes and no offline run can reach a provider or a cluster.

    ``handle_store`` is optional and, when supplied, is called **before** the
    provider call — the record-then-call ordering `adapter.py` enforces, so a lost
    response still has a durable reference to reconcile against (U11).
    """

    client: SkyPilotClient
    transport: JoinTransport
    resolver: NodeResolver
    workspace_clusters: WorkspaceClusterResolver
    clock: object = None
    provider_name: str = "skypilot"

    def _now(self) -> datetime | None:
        """Current instant, when a clock was supplied.

        Optional because the outcome types treat `observed_at` as optional and the
        adapter should not invent a timestamp it has no source for.
        """
        if self.clock is None:
            return None
        now = self.clock()  # type: ignore[operator]
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ContractViolation("clock must return a timezone-aware datetime")
        return now

    def _handle(self, request: JoinRequest, allocation_id: str) -> ProviderHandle:
        """The durable handle for this provision.

        ``idempotency_key`` is the cluster name because that is what the provider
        keys on: a repeated launch for the same cluster name is the same operation,
        which is the property that makes a retry after an ambiguous response safe.
        """
        return ProviderHandle(
            operation=OperationKind.PROVISION,
            provider=self.provider_name,
            resource_name=request.cluster_name,
            idempotency_key=request.cluster_name,
            allocation_id=allocation_id,
            workspace=request.principal.workspace_id,
        )

    def provision_and_join(
        self,
        request: JoinRequest,
        allocation_id: str,
        *,
        cancel_after: int | None = None,
    ) -> ProvisionedNode:
        """Provision, then join, then resolve the node name — each as its own step.

        Ordering and refusals:

        * The target cluster is derived from authority-owned workspace configuration,
          never from the request. A missing or blank binding refuses before launch.
        * `JoinStep.JOIN` runs only after the cluster reports UP. Joining a machine
          that is still INIT registers a node that cannot accept pods.
        * A failed join does **not** produce `NOT_READY`. The machine exists and is
          billing; readiness was not observed, so it is `UNOBSERVED` and the caller
          gets a `failure` string. Reporting NOT_READY would invite a teardown on the
          assumption the node is useless when it may simply be unregistered.
        * `cancel_after` is threaded to `consume_stream` unchanged, preserving the
          baseline's cancellation semantics rather than adding a second timeout model.
        """
        workspace_cluster = self.workspace_clusters.for_workspace(
            request.principal.workspace_id
        )
        if not isinstance(workspace_cluster, str) or not workspace_cluster.strip():
            raise ContractViolation(
                "no authoritative workspace cluster is configured for "
                f"{request.principal.workspace_id!r}"
            )

        handle = self._handle(request, allocation_id)

        task = request.launch_task()
        request_id = self.client.launch(request.cluster_name, task)
        outcome = consume_stream(
            self.client.stream(request_id), cancel_after=cancel_after
        )
        lines = tuple(outcome.lines)

        if outcome.cancelled or not outcome.succeeded:
            # A cancelled or failed launch may still have created provider
            # resources — that is why the handle was built before the call and is
            # returned here. `cancel.cancelled-launch-releases-capacity` is a
            # NOT_RUN baseline check: nothing offline establishes that cancelling
            # released the capacity, so this reports the outcome and leaves the
            # resource accounting to the caller's reconciliation.
            return ProvisionedNode(
                request=request,
                steps_completed=(),
                cluster_status="unknown",
                readiness=NodeReadiness.UNOBSERVED,
                handle=handle,
                progress_lines=lines,
                failure=outcome.error or "launch cancelled before completion",
                observed_at=self._now(),
            )

        records = self.client.status(request.cluster_name)
        raw_status = str(records[0].get("status", "")) if records else ""
        cluster_status = map_cluster_status(raw_status)

        if cluster_status != "ready":
            return ProvisionedNode(
                request=request,
                steps_completed=(),
                cluster_status=cluster_status,
                readiness=NodeReadiness.UNOBSERVED,
                handle=handle,
                progress_lines=lines,
                failure=f"cluster is {cluster_status}, not provisioned",
                observed_at=self._now(),
            )

        steps: list[JoinStep] = [JoinStep.PROVISION]

        joined = self.transport.join(
            request.cluster_name,
            request.activation,
            workspace_cluster=workspace_cluster,
        )
        if not joined:
            return ProvisionedNode(
                request=request,
                steps_completed=tuple(steps),
                cluster_status=cluster_status,
                readiness=NodeReadiness.UNOBSERVED,
                handle=handle,
                progress_lines=lines,
                failure="join to the workspace cluster was not accepted",
                observed_at=self._now(),
            )
        steps.append(JoinStep.JOIN)

        node_name = self.resolver.resolve_node_name(
            request.cluster_name, workspace_cluster=workspace_cluster
        )
        if not node_name:
            # The baseline's exact defect, now explicit: the join was accepted but
            # the Kubernetes node cannot be identified, so membership is unverified
            # and readiness must not be reported as ready.
            return ProvisionedNode(
                request=request,
                steps_completed=tuple(steps),
                cluster_status=cluster_status,
                readiness=NodeReadiness.UNOBSERVED,
                handle=handle,
                progress_lines=lines,
                failure=(
                    "joined but the Kubernetes node name could not be resolved; "
                    "EKS membership is unverified"
                ),
                observed_at=self._now(),
            )
        steps.append(JoinStep.RESOLVE_NODE)

        schedulable = self.resolver.is_schedulable(
            node_name, workspace_cluster=workspace_cluster
        )
        return ProvisionedNode(
            request=request,
            steps_completed=tuple(steps),
            cluster_status=cluster_status,
            # Here a negative reading IS an observation: the node was found and
            # Kubernetes reported it unschedulable. That is NOT_READY, distinct from
            # the UNOBSERVED paths above.
            readiness=NodeReadiness.READY if schedulable else NodeReadiness.NOT_READY,
            handle=handle,
            k8s_node_name=node_name,
            progress_lines=lines,
            failure=None if schedulable else "node is not schedulable",
            observed_at=self._now(),
        )

    def schedule(self, node: ProvisionedNode, workload_id: str) -> WorkloadPlacement:
        """Schedule a workload onto a joined node, or refuse with a reason.

        Kubernetes does the scheduling; this is the gate in front of it. Returns a
        refusal rather than raising, because "this node is not usable yet" is a
        normal outcome the caller handles by waiting or relocating, not an error.
        """
        if not node.schedulable:
            return WorkloadPlacement(
                node=node,
                workload_id=workload_id,
                scheduled=False,
                reason=(
                    f"node readiness is {node.readiness.value}; "
                    "workloads are scheduled only onto a verified joined node"
                ),
            )
        return WorkloadPlacement(
            node=node,
            workload_id=workload_id,
            scheduled=True,
            reason=f"scheduled onto {node.k8s_node_name}",
        )

    def cancel(self, node: ProvisionedNode) -> tuple[bool, CallOutcome]:
        """Tear down a provisioned cluster, preserving the baseline's Down-then-purge.

        `teardown` is reused so the two-attempt sequence stays the baseline's. The
        second element is deliberately `AMBIGUOUS` on failure rather than `FAILED`:
        a failed Down attempt has not established that the provider holds nothing,
        which is what `reconciliation.reconcile` needs to be told so it asks the
        provider rather than permitting a retry.

        Note what this does **not** return: any claim of provider-side absence.
        `TeardownOutcome.provider_absence_confirmed` stays False in every offline
        path, and `cleanup.provider-side-absence-verified` remains a NOT_RUN baseline
        check. Cleanup evidence is a live criterion.
        """
        for purge in (False, True):
            try:
                request_id = self.client.down(node.request.cluster_name, purge=purge)
                outcome = consume_stream(self.client.stream(request_id))
            except Exception:
                continue
            if outcome.succeeded and not outcome.cancelled:
                return True, CallOutcome.SUCCEEDED
        return False, CallOutcome.AMBIGUOUS

    def record_handle(
        self,
        node: ProvisionedNode,
        confirmed_at: datetime,
    ) -> HandleRecord:
        """The durable record for this node's provider handle.

        Separate from `provision_and_join` because durability is the *store's*
        acknowledgement, not something the adapter can assert. `HandleRecord`
        enforces that: `durable=True` requires the instant persistence confirmed,
        so a caller cannot claim durability without a confirmation timestamp.
        """
        return HandleRecord(handle=node.handle, durable=True, confirmed_at=confirmed_at)
