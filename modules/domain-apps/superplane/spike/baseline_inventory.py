"""Inventory of the existing Superplane SkyPilot-to-EKS baseline.

Issue #5040 (U12), EPIC #4910.

The user reported that the existing Superplane EKS integration works well for
them, and the scope amendment makes preserving that behavior the migration
outcome. This module records what the pinned upstream source actually
implements, so that "works well" becomes a set of checkable statements instead
of a recollection.

Two things this module is careful NOT to do:

1. It does not claim live verification. Nothing here was observed running. The
   user's report is recorded as ``USER_REPORTED`` and the specific revision,
   configuration and logs behind it are still uncaptured — that is the deferred
   R17 live-baseline criterion, which does not close with this story.

2. It does not treat the AWS support limitation as a requirement to replace the
   architecture. The limitation is recorded verbatim as a deployment
   constraint (see ``SUPPORT_LIMITATION``). The amendment supersedes the
   earlier no-EKS-join spike gate.

The most consequential finding is ``CHAIN_GAPS``: the provisioning half of the
join path is implemented in Go, but the half that actually registers the node
with EKS lives in shell scripts shipped through a ConfigMap, and the field that
would link the two (``OnboardResult.K8sNodeName``) is never assigned. U19 must
resolve that explicitly rather than assume the Go path completes a join.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .provenance import Citation, EvidenceStatus

# Recorded verbatim from the upstream PoC README, per the amendment's
# instruction to record the limitation accurately and not convert it into a
# no-join requirement.
SUPPORT_LIMITATION = (
    "AWS does NOT officially support running EKS Hybrid Nodes on other cloud "
    "providers. This PoC demonstrates technical feasibility but uses an "
    "unsupported configuration. For production, evaluate the risk of no AWS "
    "support for this topology."
)

SUPPORT_LIMITATION_CITATION = Citation(
    path="poc/eks-hybrid-skypilot/README.md",
    detail=(
        "Line 30 states the support limitation; the 'Known Limitations' "
        "section adds Cilium-only networking (VPC CNI incompatible), IPv4 "
        "only, and no EBS/EFS storage classes on hybrid nodes."
    ),
)


@dataclass(frozen=True)
class BaselineScenario:
    """One observable behavior of the baseline.

    Every scenario must state its inputs, its expected outcome and how well it
    is attested, and must cite the upstream files that justify the claim. The
    inventory test enforces all four so a scenario cannot be added as a bare
    assertion.
    """

    scenario_id: str
    summary: str
    inputs: tuple[str, ...]
    expected_outcome: str
    evidence_status: EvidenceStatus
    citations: tuple[Citation, ...]
    # Set when the scenario's stated outcome is not fully reachable from the
    # cited source. Recording this is the point of the story: an unqualified
    # "provisioning works" would hide the join gap from U19.
    caveat: str | None = None

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ValueError(f"{self.scenario_id}: no inputs recorded")
        if not self.citations:
            raise ValueError(f"{self.scenario_id}: no citations recorded")
        if not self.expected_outcome.strip():
            raise ValueError(f"{self.scenario_id}: no expected outcome")


@dataclass(frozen=True)
class ChainGap:
    """A break between what the Go controller does and what the join needs.

    These are not bugs to fix in this story. They are the migration decisions
    U19 has to make explicitly, recorded here with the evidence that they are
    real so that a green adapter test cannot paper over them.
    """

    gap_id: str
    description: str
    consequence: str
    citations: tuple[Citation, ...]
    u19_decision_required: str


# --------------------------------------------------------------------------
# The SkyPilot REST surface the controller actually speaks.
#
# Derived from skypilot/client.go. This matters for parity because it bounds
# what the baseline can possibly do: there is no serve or jobs endpoint here,
# so serving cannot be attested by this client at all.
# --------------------------------------------------------------------------
SKYPILOT_CLIENT_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", "/api/health", "Health"),
    ("POST", "/launch", "Launch"),
    ("POST", "/status", "Status"),
    ("POST", "/down", "Down"),
    ("GET", "/enabled_clouds", "EnabledClouds"),
    ("GET", "/api/stream", "StreamProgress"),
)

SKYPILOT_DEFAULT_BASE_URL = "http://skypilot-api.skypilot.svc.cluster.local:46580"


SCENARIOS: tuple[BaselineScenario, ...] = (
    BaselineScenario(
        scenario_id="provider-selection-cheapest-first",
        summary=(
            "The provisioner sorts every available cloud/region option from the "
            "pool's configured adapters by price and tries them cheapest-first, "
            "falling back on failure."
        ),
        inputs=(
            "NodePool with a GPUType (e.g. H100) and optional PreferSpot",
            (
                "The pool's configured cloud list (pool.Spec.Clouds), which "
                "filterAdapters uses to restrict adapters; empty means all of "
                "aws, nebius and lambda"
            ),
        ),
        expected_outcome=(
            "SelectAllAvailable returns every option whose Available is true, "
            "ordered by effective hourly cost with near-ties broken by cloud "
            "name; provisionAsync tries each in order and marks the node Failed "
            "only after all options are exhausted."
        ),
        evidence_status=EvidenceStatus.SOURCE_ONLY,
        citations=(
            Citation(
                path="src/superplane-controller/controllers/provisioner.go",
                detail=(
                    "handlePending calls adapters.SelectAllAvailable; "
                    "provisionAsync loops options cheapest-first and, on "
                    "onboarding failure, calls TerminateNode before the next "
                    "option; exhaustion sets phase Failed."
                ),
            ),
            Citation(
                path="src/superplane-controller/adapters/aws.go",
                detail=(
                    "ListGPUPricing reads the static awsGPUPricing map "
                    "(H100 p5.48xlarge at 98.32/hr, A100, A10G, L4), falling "
                    "back to dynamicGPULookup for unknown GPU types."
                ),
            ),
            Citation(
                path="src/superplane-controller/adapters/adapter.go",
                detail=(
                    "SelectAllAvailable skips rows whose Available is false and "
                    "sorts the rest by cost, breaking differences under 0.001 "
                    "by cloud name. It never consults enabled clouds."
                ),
            ),
            Citation(
                path="src/superplane-controller/adapters/helpers.go",
                detail=(
                    "isCloudEnabled queries /enabled_clouds and is documented "
                    "in-source as 'used as a fallback when a GPU type is not in "
                    "the static pricing map'; its only callers are "
                    "dynamicGPULookup and CheckAvailability."
                ),
            ),
        ),
        caveat=(
            "Pricing is a static in-source table, not a live pricing API, so "
            "selection is only as correct as that table. Parity must compare "
            "the ordering the table produces, not real-time market prices. "
            "Two exclusion mechanisms must not be conflated: the pool's "
            "configured cloud list restricts which adapters are consulted, "
            "whereas /enabled_clouds is only a fallback for GPU types missing "
            "from the static map — so a cloud reported disabled there is still "
            "selected for a statically priced type such as H100."
        ),
    ),
    BaselineScenario(
        scenario_id="skypilot-launch-and-stream",
        summary=(
            "Onboarding health-checks the SkyPilot API, launches a cluster and "
            "streams launch progress over SSE until a terminal event."
        ),
        inputs=(
            "NodeSpec with Cloud, GPUType, GPUCount, DiskSizeGB, Region, UseSpot",
            "A SkyPilot cluster name assigned by the controller",
            f"SkyPilot API reachable at {SKYPILOT_DEFAULT_BASE_URL}",
        ),
        expected_outcome=(
            "Health() succeeds, Launch() returns a request id, StreamProgress "
            "emits '[sky] ' prefixed lines until an event with Event=complete "
            "or IsTerminal, then Status() yields the head IP of an UP cluster."
        ),
        evidence_status=EvidenceStatus.SOURCE_ONLY,
        citations=(
            Citation(
                path="src/superplane-controller/provisioner/onboarder.go",
                detail=(
                    "Onboard() runs the documented six steps: health, "
                    "buildTask, Launch, streamLaunchProgress, getClusterIP, "
                    "result. A failed health check returns Success=false with "
                    "a nil error rather than propagating an error."
                ),
            ),
            Citation(
                path="src/superplane-controller/skypilot/client.go",
                detail=(
                    "Launch POSTs /launch and reads request_id; "
                    "StreamProgress GETs /api/stream?request_id=... and "
                    "parses SSE id/event/data frames."
                ),
            ),
        ),
    ),
    BaselineScenario(
        scenario_id="autostop-and-spot-defaults",
        summary=(
            "Launches carry an idle autostop and a default disk size, so an "
            "idle cluster stops without operator action."
        ),
        inputs=("OnboarderConfig with IdleMinutesToAutostop unset or explicit",),
        expected_outcome=(
            "IdleMinutesToAutostop defaults to 120 minutes and disk_size to "
            "256 GB; use_spot is set in the task only when NodeSpec.UseSpot."
        ),
        evidence_status=EvidenceStatus.SOURCE_ONLY,
        citations=(
            Citation(
                path="src/superplane-controller/provisioner/onboarder.go",
                detail=(
                    "DefaultIdleMinutesToAutostop = 120, DefaultDiskSizeGB = "
                    "256, DefaultTimeout = 30m; buildTask sets use_spot only "
                    "when spec.UseSpot is true."
                ),
            ),
        ),
        caveat=(
            "Autostop is cost protection, not cleanup: a STOPPED cluster can "
            "still hold provider resources, so cleanup parity must assert Down "
            "rather than autostop."
        ),
    ),
    BaselineScenario(
        scenario_id="eks-join-via-onboarding-scripts",
        summary=(
            "The EKS join is performed by shell scripts shipped in a "
            "ConfigMap, which SSH to the SkyPilot node and run nodeadm."
        ),
        inputs=(
            (
                "config.local.env rendered with CLUSTER_NAME, AWS_REGION, "
                "SSM_ACTIVATION_ID, SSM_ACTIVATION_CODE"
            ),
            "A running SkyPilot cluster reachable over SSH",
        ),
        expected_outcome=(
            "04-join-node.sh installs nodeadm and joins the node to EKS; "
            "05-install-cilium.sh installs Cilium plus the NVIDIA device "
            "plugin, after which the hybrid node moves NotReady -> Ready."
        ),
        evidence_status=EvidenceStatus.USER_REPORTED,
        citations=(
            Citation(
                path="poc/eks-hybrid-skypilot/04-join-node.sh",
                detail=(
                    "Requires CLUSTER_NAME, AWS_REGION, SSM_ACTIVATION_ID and "
                    "SSM_ACTIVATION_CODE; runs 'sky status', then 'sky exec' "
                    "to install nodeadm and join. Header calls itself an "
                    "alternative to the SkyPilot YAML run phase."
                ),
            ),
            Citation(
                path="src/superplane-controller/deploy/configmap.yaml",
                detail=(
                    "superplane-onboard-scripts carries onboard-node.sh as a "
                    "5-step sequence (IAM/SSM, sky launch, join, Cilium, "
                    "WireGuard) and a config.env template the controller "
                    "renders into config.local.env."
                ),
            ),
            Citation(
                path="poc/eks-hybrid-skypilot/README.md",
                detail=(
                    "States hybrid nodes transition NotReady -> Ready after "
                    "the Cilium step, and that Cilium is mandatory because "
                    "VPC CNI is incompatible."
                ),
            ),
        ),
        caveat=(
            "This is the step the user's 'works well' report covers, and it is "
            "the least captured: the scripts run via 'sky launch' and 'sky "
            "exec' shell calls, not through the Go SkyPilot client, so no "
            "recorded request/response evidence exists in the snapshot. "
            "Capturing the actual revision, config and logs is the deferred "
            "R17 live-baseline criterion."
        ),
    ),
    BaselineScenario(
        scenario_id="node-health-monitoring",
        summary=(
            "A health monitor watches the Kubernetes Node behind each "
            "SuperplaneNode and degrades the node when NodeReady is lost."
        ),
        inputs=("A SuperplaneNode whose status.k8sNodeName is populated",),
        expected_outcome=(
            "checkNodeHealth reads the Node's NodeReady condition; sustained "
            "unhealthiness sets phase Degraded, and recovery restores Ready."
        ),
        evidence_status=EvidenceStatus.SOURCE_ONLY,
        citations=(
            Citation(
                path="src/superplane-controller/controllers/health_monitor.go",
                detail=(
                    "Reconcile reads spNode.Status.K8sNodeName and explicitly "
                    "SKIPS the health check when it is empty; checkNodeHealth "
                    "inspects the corev1.NodeReady condition."
                ),
            ),
        ),
        caveat=(
            "Reachable only when status.k8sNodeName is set. See chain gap "
            "'k8s-node-name-never-assigned' — nothing in the snapshot assigns "
            "it, so on the Go path this monitor is inert."
        ),
    ),
    BaselineScenario(
        scenario_id="cost-aggregation-per-nodepool",
        summary=(
            "A cost reconciler sums hourly cost across a pool's nodes and "
            "projects a daily estimate."
        ),
        inputs=("SuperplaneNodes with status.hourlyCost set at provision time",),
        expected_outcome=(
            "Every 60s the pool's HourlyCostUSD is the sum of active nodes' "
            "hourly costs and DailyCostEstimateUSD is that times 24."
        ),
        evidence_status=EvidenceStatus.SOURCE_ONLY,
        citations=(
            Citation(
                path="src/superplane-controller/controllers/cost_reconciler.go",
                detail=(
                    "CostReconcileInterval = 60s; aggregateCosts sums "
                    "per-node hourly cost into HourlyCostUSD and a 24h "
                    "DailyCostEstimateUSD projection."
                ),
            ),
        ),
        caveat=(
            "This is cost OBSERVATION derived from the static pricing table at "
            "provision time, not billed spend from a provider invoice. It is "
            "not a spend control and must not be reported as one."
        ),
    ),
    BaselineScenario(
        scenario_id="teardown-via-down-then-purge",
        summary=(
            "Releasing a node calls SkyPilot Down, retrying with purge if the "
            "first attempt fails."
        ),
        inputs=("A SuperplaneNode with status.skypilotCluster set",),
        expected_outcome=(
            "Down(cluster, purge=false) is issued; on error the consolidator "
            "retries Down(cluster, purge=true) and surfaces an error only if "
            "that also fails."
        ),
        evidence_status=EvidenceStatus.SOURCE_ONLY,
        citations=(
            Citation(
                path="src/superplane-controller/controllers/consolidator.go",
                detail=(
                    "Calls skypilot.Down with purge=false, logs 'sky down "
                    "failed, retrying with purge' and retries with purge=true."
                ),
            ),
            Citation(
                path="src/superplane-controller/adapters/aws.go",
                detail=(
                    "TerminateNode delegates to client.Down(cluster, false) "
                    "and returns the SkyPilot request id; nebius.go and "
                    "lambda.go are identical in shape."
                ),
            ),
        ),
        caveat=(
            "purge=true makes SkyPilot forget local cluster state whether or "
            "not the provider actually released the resources, so a successful "
            "purge is NOT evidence of cleanup. Cleanup parity must verify "
            "provider-side absence independently."
        ),
    ),
    BaselineScenario(
        scenario_id="serving-via-sky-serve-yaml",
        summary=(
            "Model serving exists as SkyPilot SkyServe YAML specs with ordered "
            "multi-region fallback, invoked by the sky CLI."
        ),
        inputs=(
            "A SkyServe spec such as qwen35-35b-a3b-serve-eu.yaml",
            "'sky serve up <spec> -n <name>' run by an operator",
        ),
        expected_outcome=(
            "SkyServe brings up replicas on the first available infra in the "
            "ordered list (nebius/eu-north1 -> lambda/europe-central-1 -> "
            "lambda -> aws) exposing port 8000."
        ),
        evidence_status=EvidenceStatus.ABSENT,
        citations=(
            Citation(
                path="infra/skypilot-models/qwen35-35b-a3b-serve-eu.yaml",
                detail=(
                    "Ordered resources across nebius/lambda/aws with H100:1 "
                    "and ports 8000; header documents 'sky serve up ... -n "
                    "qwen35-eu' as the invocation and 2 replicas autoscaling "
                    "to 3."
                ),
            ),
            Citation(
                path="src/superplane-controller/skypilot/client.go",
                detail=(
                    "Implements only /api/health, /launch, /status, /down, "
                    "/enabled_clouds and /api/stream. There is no serve or "
                    "jobs endpoint, so the controller cannot drive SkyServe."
                ),
            ),
        ),
        caveat=(
            "ABSENT from the controller, not from the project: the specs are "
            "real but are operator-run CLI artifacts. No owning controller "
            "reconciles a SkyServe service, so there is no baseline evidence "
            "of service reachability, authentication or controller-driven "
            "teardown. Batch success cannot substitute for any of these."
        ),
    ),
)


CHAIN_GAPS: tuple[ChainGap, ...] = (
    ChainGap(
        gap_id="k8s-node-name-never-assigned",
        description=(
            "OnboardResult declares K8sNodeName and SSMInstanceID, and "
            "updateNodeSuccess copies them into status when non-empty, but "
            "nothing in the snapshot ever assigns either field."
        ),
        consequence=(
            "On the Go controller path status.k8sNodeName stays empty, so the "
            "health monitor skips every check and no SuperplaneNode is ever "
            "linked to its Kubernetes Node. A node can be Ready in the CRD "
            "while its EKS membership is unverified."
        ),
        citations=(
            Citation(
                path="src/superplane-controller/provisioner/onboarder.go",
                detail=(
                    "K8sNodeName and SSMInstanceID appear only as struct "
                    "field declarations; the success return sets Success, "
                    "PublicIP, ClusterName, RequestID and Output only."
                ),
            ),
            Citation(
                path="src/superplane-controller/controllers/health_monitor.go",
                detail=(
                    "Reconcile: 'SuperplaneNode has no k8sNodeName, skipping "
                    "health check' when the field is empty."
                ),
            ),
        ),
        u19_decision_required=(
            "Decide where the joined node name comes from — resolve it after "
            "the join (e.g. by provider instance id or SSM managed instance "
            "id) and populate status.k8sNodeName — or record explicitly that "
            "node readiness is not observed. Parity must not report node "
            "registration as verified while this is unresolved."
        ),
    ),
    ChainGap(
        gap_id="launch-task-has-no-join-step",
        description=(
            "buildTask emits only a 'resources' block. It contains no setup or "
            "run phase, yet buildEnvs passes CLUSTER_NAME, K8S_VERSION and "
            "SSM_ACTIVATION_ID/CODE into the task."
        ),
        consequence=(
            "Those environment variables have no consumer inside the launched "
            "task, so a Launch through the Go client provisions a VM but does "
            "not join it to EKS. The join only happens if the ConfigMap "
            "scripts are run separately against the node."
        ),
        citations=(
            Citation(
                path="src/superplane-controller/provisioner/onboarder.go",
                detail=(
                    "buildTask returns {'resources': {...}} with cloud, "
                    "accelerators, disk_size and optional region/use_spot; "
                    "buildEnvs populates AWS_REGION, CLUSTER_NAME, "
                    "K8S_VERSION, SSM_ACTIVATION_* and SKYPILOT_* metadata."
                ),
            ),
            Citation(
                path="poc/eks-hybrid-skypilot/04-join-node.sh",
                detail=(
                    "Header describes itself as the alternative to the "
                    "SkyPilot YAML run phase, confirming the join is expected "
                    "to come from either a task run phase or this script — "
                    "and the Go-built task has no run phase."
                ),
            ),
        ),
        u19_decision_required=(
            "Choose the join mechanism deliberately: add setup/run phases to "
            "the launched task, or invoke the onboarding scripts as an "
            "explicit post-provision step. Either way the SSM activation "
            "values must reach the node without being logged."
        ),
    ),
    ChainGap(
        gap_id="ssm-activation-credentials-in-task-envs",
        description=(
            "SSM_ACTIVATION_ID and SSM_ACTIVATION_CODE are passed as SkyPilot "
            "task environment variables and rendered into a config.local.env "
            "file on disk."
        ),
        consequence=(
            "An activation code is a credential that lets a machine register "
            "into the cluster. Carrying it through task envs and an on-disk "
            "env file widens where it can be logged or read."
        ),
        citations=(
            Citation(
                path="src/superplane-controller/provisioner/onboarder.go",
                detail=(
                    "buildEnvs places SSM_ACTIVATION_ID and "
                    "SSM_ACTIVATION_CODE into the launch request Envs map."
                ),
            ),
            Citation(
                path="src/superplane-controller/deploy/configmap.yaml",
                detail=(
                    "The config.env template exports SSM_ACTIVATION_ID and "
                    "SSM_ACTIVATION_CODE into config.local.env on the node."
                ),
            ),
        ),
        u19_decision_required=(
            "Per the amendment, functional parity does not preserve secret "
            "exposure as desired behavior. U19 must source these from scoped "
            "ADP credentials and keep them out of logs and CRD status; parity "
            "asserts the values never appear in captured output."
        ),
    ),
    ChainGap(
        gap_id="no-owning-controller-for-serving",
        description=(
            "SkyServe specs are operator-run CLI artifacts. No controller "
            "reconciles a serving deployment, and the Go client has no serve "
            "endpoint."
        ),
        consequence=(
            "Nothing owns a served endpoint's lifecycle, so there is no "
            "baseline evidence for reachability, authentication or teardown, "
            "and a leaked replica would not be reconciled away."
        ),
        citations=(
            Citation(
                path="src/superplane-controller/skypilot/client.go",
                detail=(
                    "Client methods are Health, Launch, Status, Down, "
                    "EnabledClouds and StreamProgress only."
                ),
            ),
            Citation(
                path="infra/skypilot-models/qwen35-35b-a3b-serve-eu.yaml",
                detail=(
                    "Documents 'sky serve up' as the invocation path, i.e. "
                    "outside the controller."
                ),
            ),
        ),
        u19_decision_required=(
            "Either declare serving out of scope for migration parity, or "
            "name the owning controller and capture separate reachability, "
            "authentication and teardown evidence. Exactly one controller must "
            "own each serving resource."
        ),
    ),
)


# --------------------------------------------------------------------------
# Existing-state handover (U19's adopt / drain-relaunch / no-existing-state
# decision). The amendment is explicit that where no existing state is being
# migrated, that fact must be recorded and verified rather than assumed.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ExistingStateClass:
    """A class of live resource that a cutover has to account for."""

    kind: str
    how_to_enumerate: str
    default_decision: str
    rationale: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)


EXISTING_STATE_CLASSES: tuple[ExistingStateClass, ...] = (
    ExistingStateClass(
        kind="skypilot_clusters",
        how_to_enumerate=(
            "POST /status with no cluster_names filter returns every cluster "
            "the API server knows, including STOPPED ones."
        ),
        default_decision="undecided",
        rationale=(
            "A STOPPED cluster still has a handle and may hold provider "
            "resources, so it cannot be ignored during cutover. Adoption "
            "requires the new controller to accept a handle it did not create."
        ),
        citations=(
            Citation(
                path="src/superplane-controller/skypilot/client.go",
                detail=(
                    "Status sends an empty body when no names are given, and "
                    "the client test asserts the no-filter call sends an empty "
                    "body and can return an empty cluster list."
                ),
            ),
        ),
    ),
    ExistingStateClass(
        kind="superplane_node_crs",
        how_to_enumerate=(
            "List SuperplaneNode and NodePool custom resources in the "
            "workspace cluster."
        ),
        default_decision="undecided",
        rationale=(
            "CR status carries skypilotCluster, hourlyCost and provisionedAt. "
            "Recreating CRs without those values loses the only link between a "
            "live cluster and its owner."
        ),
        citations=(
            Citation(
                path="src/superplane-controller/api/v1/superplanenode_types.go",
                detail="Defines the SuperplaneNode spec/status contract.",
            ),
        ),
    ),
    ExistingStateClass(
        kind="skypilot_api_server_state",
        how_to_enumerate=(
            "Inspect the SkyPilot API server's backing store: the SQLite "
            "deployment, the Postgres StatefulSet or the RDS option."
        ),
        default_decision="undecided",
        rationale=(
            "Cluster handles live in the API server's database. A redeploy "
            "onto fresh storage orphans every running cluster, which is the "
            "concrete way a 'clean deployment' assumption leaks money."
        ),
        citations=(
            Citation(
                path="infra/skypilot-api/02-skypilot-api-sqlite.yaml",
                detail="SQLite-backed API server variant.",
            ),
            Citation(
                path="infra/skypilot-api/05-postgres-statefulset.yaml",
                detail=(
                    "Postgres StatefulSet variant; 06-rds-terraform/main.tf "
                    "is the managed alternative."
                ),
            ),
        ),
    ),
    ExistingStateClass(
        kind="joined_eks_hybrid_nodes",
        how_to_enumerate=(
            "List Kubernetes Nodes in the workspace EKS cluster and correlate "
            "with SSM managed instances."
        ),
        default_decision="undecided",
        rationale=(
            "A joined node outlives the controller that created it. Draining "
            "it is a workload-affecting action, so the decision needs the "
            "resource owner's agreement, not a default."
        ),
        citations=(
            Citation(
                path="poc/eks-hybrid-skypilot/07-teardown.sh",
                detail="Teardown path for PoC-created hybrid nodes.",
            ),
        ),
    ),
    ExistingStateClass(
        kind="skyserve_services",
        how_to_enumerate=(
            "'sky serve status' from an authorized client; there is no "
            "controller-side inventory."
        ),
        default_decision="undecided",
        rationale=(
            "No owning controller exists (see chain gap "
            "'no-owning-controller-for-serving'), so a running service would "
            "not appear in any CR listing and could be missed entirely."
        ),
    ),
)

# Permitted values for a per-class handover decision. "no_existing_state" is a
# claim that must be VERIFIED by enumeration, not assumed by silence.
HANDOVER_DECISIONS = ("adopt", "drain_relaunch", "no_existing_state", "undecided")


def scenario_by_id(scenario_id: str) -> BaselineScenario:
    """Look up a scenario, raising if it is unknown."""
    for scenario in SCENARIOS:
        if scenario.scenario_id == scenario_id:
            return scenario
    raise KeyError(scenario_id)


def scenarios_with_status(status: EvidenceStatus) -> tuple[BaselineScenario, ...]:
    """All scenarios at a given evidence level."""
    return tuple(s for s in SCENARIOS if s.evidence_status is status)


def unresolved_gaps() -> tuple[ChainGap, ...]:
    """Chain gaps U19 must decide before claiming migration parity."""
    return CHAIN_GAPS
