"""Fixtures and fakes for workspace bootstrap — Issue #5533 (w6-10), EPIC #4910.

Every identity here is SYNTHETIC. The account id is in the documentation range, the
cluster names and UUIDs are invented, and the certificate strings are obviously
not certificates. That is deliberate and it follows the precedent
`../../infra/account-factory/tests/conftest.py` sets: a fixture that leaked into a
real invocation must not be able to act on anything, and the defect that module
locks shut was a reference config shipping a real account id as a working default.

In particular: none of these values is the selected first deployment's. Those
identities appear in this story's issue and its handoffs, and copying them into a
test fixture would make the suite look like evidence about a live workspace. It is
not — it is evidence about the decision logic. `#5540 AC-03` keeps the live
criteria.

## The fakes implement the Protocols from `superplane_bootstrap.access`

`FakeClusterAccess` is deliberately configurable per-behavior rather than
"realistic": the negative cases are the substance of AC-01 and AC-02, so being
able to say "this cluster reports IMDS reachable" or "this cluster already runs a
controller" in one line is what lets each refusal get its own test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from superplane_bootstrap.access import (
    ClusterIdentity,
    ObservedNamespace,
    ObservedPod,
    ObservedWorkload,
    ProviderIdentity,
)
from superplane_bootstrap.components import CONTROLLER_IMAGE_MARKER
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.prerequisites import ExpectedPrerequisites
from superplane_bootstrap.readiness import (
    FORBIDDEN_CONTROLLER_PERMISSIONS,
    REQUIRED_CONTROLLER_PERMISSIONS,
    REQUIRED_SYSTEM_WORKLOADS,
    SYSTEM_NAMESPACE,
)
from superplane_bootstrap.state import (
    BootstrapState,
    state_from_mapping,
)
from superplane_bootstrap.state import (
    claim_fingerprint as fingerprint_of,
)
from superplane_contracts.provisioning import (
    PROVISION,
    REQUIRED_PERMISSION,
    OperationBinding,
    ResolvedPrincipal,
)

# --- Synthetic target identities -------------------------------------------------

ACCOUNT_ID = "000000000000"
REGION = "us-east-1"
ORG_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
CLUSTER_ID = "33333333-3333-4333-8333-333333333333"
CLUSTER_NAME = "adp-test-spw-0123456789abcdef0123456789abcdef"
CLUSTER_ARN = f"arn:aws:eks:{REGION}:{ACCOUNT_ID}:cluster/{CLUSTER_NAME}"
ENDPOINT = f"https://EXAMPLE0123456789.gr7.{REGION}.eks.amazonaws.com"
# Not a certificate. Base64-shaped so a reader is not misled into thinking a real
# CA was pasted here, and distinct from STALE_CA so the mismatch test is meaningful.
CA_DATA = "c3ludGhldGljLXRlc3QtY2VydGlmaWNhdGUtYXV0aG9yaXR5"
STALE_CA = "c3ludGhldGljLXN0YWxlLWNlcnRpZmljYXRlLWF1dGhvcml0eQ=="
PRINCIPAL_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/SyntheticTestRole"
NAMESPACE = "superplane-workspace"
OIDC_ISSUER = f"https://oidc.eks.{REGION}.amazonaws.com/id/EXAMPLE0123456789"

WORKSPACE_CRDS = ("nodepools.superplane.ai", "superplanenodes.superplane.ai")
BOOTSTRAP_TAINT_KEY = "superplane.aws-e/bootstrap"
CNI_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/SyntheticVpcCniRole"
ENFORCE_VERSION = "v1.35"
CREDENTIAL_ID = "44444444-4444-4444-8444-444444444444"
CONTROLLER_NAME = "superplane-controller"
CONTROLLER_SERVICE_ACCOUNT = "superplane-controller"

# --- Synthetic access prerequisites (F4) ------------------------------------------
# Shaped like real identifiers so the attribution comparisons are meaningful, and
# obviously synthetic so none of them could act on anything.
CLUSTER_SG_ID = "sg-0000000000cluster"
MANAGEMENT_SG_ID = "sg-0000000000mgmtpl"
VPC_ID = "vpc-00000000000000000"
ENDPOINT_RULE_ID = "sgr-0000000000endpoint"
MANAGEMENT_RULE_ID = "sgr-0000000000mgmtret"
ACCESS_POLICY_ARN = "arn:aws:eks::aws:cluster-access-policy/SyntheticNamespaceAdmin"

# --- Fixtures --------------------------------------------------------------------


@pytest.fixture
def principal() -> ResolvedPrincipal:
    return ResolvedPrincipal(
        subject="synthetic-test-subject", org_id=ORG_ID, workspace_id=WORKSPACE_ID
    )


@pytest.fixture
def binding(principal: ResolvedPrincipal) -> OperationBinding:
    """A real OperationBinding, not a fake.

    The contract's own `__post_init__` enforces the permission and action, so a
    test that constructs one has already shown the values this package reads are
    the values the contract publishes.
    """
    return OperationBinding(
        operation_id="synthetic-operation",
        principal=principal,
        action=PROVISION,
        permission=REQUIRED_PERMISSION,
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def provider_identity() -> ProviderIdentity:
    return ProviderIdentity(account_id=ACCOUNT_ID, principal_arn=PRINCIPAL_ARN)


@pytest.fixture
def observed_cluster() -> ClusterIdentity:
    return ClusterIdentity(
        name=CLUSTER_NAME,
        arn=CLUSTER_ARN,
        region=REGION,
        account_id=ACCOUNT_ID,
        endpoint=ENDPOINT,
        certificate_authority_data=CA_DATA,
        status="ACTIVE",
        oidc_issuer_url=OIDC_ISSUER,
        version="1.35",
    )


@pytest.fixture
def expected_target() -> dict[str, str]:
    """The `expected_*` arguments, as a consumer reads them from Terraform outputs."""
    return {
        "expected_account_id": ACCOUNT_ID,
        "expected_region": REGION,
        "expected_cluster_name": CLUSTER_NAME,
        "expected_cluster_arn": CLUSTER_ARN,
        "expected_certificate_authority_data": CA_DATA,
    }


@pytest.fixture
def expected_prerequisites() -> ExpectedPrerequisites:
    """The F4 expectations, as a consumer reads them from Terraform outputs.

    `management_security_group_id` is not published by `infra/workspaces/outputs.tf`
    (the management surface is outside that module), so in production it is an
    operator-supplied value. It is still an EXPECTATION here: the rule observed on AWS
    is compared against it rather than the reverse.
    """
    return ExpectedPrerequisites(
        account_id=ACCOUNT_ID,
        vpc_id=VPC_ID,
        cluster_security_group_id=CLUSTER_SG_ID,
        management_security_group_id=MANAGEMENT_SG_ID,
        node_security_group_id="sg-synthetic-nodes",
        sts_endpoint_security_group_id="sg-synthetic-sts",
        sts_endpoint_vpc_id=VPC_ID,
    )


# --- Fakes -----------------------------------------------------------------------


@dataclass
class FakeClusterAccess:
    """A configurable stand-in for scoped access to one workspace cluster.

    Defaults describe a cluster that is ready to bootstrap and correctly isolated.
    Each negative test flips exactly one field, so the test name states which
    single condition produced the refusal.
    """

    crds: list[str] = field(default_factory=lambda: list(WORKSPACE_CRDS))
    controller_images: list[str] = field(default_factory=list)
    namespaces: dict[str, ObservedNamespace] = field(default_factory=dict)
    imds: dict[str, bool] = field(
        default_factory=lambda: {"ipv4": False, "ipv6": False}
    )
    tenant_can_change_labels: bool = False
    cni_scope: dict[str, object] = field(
        default_factory=lambda: {
            "aws_node_role_arn": CNI_ROLE_ARN,
            "node_role_has_cni_permissions": False,
            "node_role_has_account_wide_ecr": False,
        }
    )
    taints: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"key": BOOTSTRAP_TAINT_KEY, "value": "pending", "effect": "NoSchedule"}
        ]
    )
    # Pod specs whose keys appear here are admitted; anything requesting a host
    # escape hatch is rejected, matching restricted:v1.35 behaviour.
    admit_unsafe: bool = False
    # Rejects every pod including a conforming one — a broken probe, an absent
    # namespace or a restrictive quota. Distinct from `admit_unsafe` because it is the
    # opposite hazard: without a positive control, "rejects everything" would read as
    # perfect isolation.
    reject_everything: bool = False
    establish_crds_result: list[str] | None = None
    next_uid: str = "namespace-uid-0001"

    # --- F2: runtime readiness ---------------------------------------------------
    # Workloads keyed by (namespace, name). Defaults describe CoreDNS available in
    # kube-system and the workspace controller available in its own namespace, which
    # is the state readiness requires. Each negative test removes or degrades one.
    workloads: dict[tuple[str, str], ObservedWorkload] = field(
        default_factory=lambda: {
            (SYSTEM_NAMESPACE, name): ObservedWorkload(
                name=name,
                namespace=SYSTEM_NAMESPACE,
                desired_replicas=2,
                available_replicas=2,
            )
            for name in REQUIRED_SYSTEM_WORKLOADS
        }
    )
    # A single reconciler, handover complete. `reconcilers` is separate from the
    # workload's replica count because "two replicas of one controller" and "two
    # controllers reconciling the same CRDs" are different facts, and only the second
    # is the split-brain the gate refuses.
    handover: dict[str, object] = field(
        default_factory=lambda: {"complete": True, "reconcilers": 1}
    )
    # Absent keys mean UNKNOWN, not denied — `readiness._rbac_checks` refuses an
    # unanswered permission rather than defaulting it falsy. The default grants every
    # required pair and denies every forbidden one.
    controller_rbac: dict[tuple[str, str], bool] = field(
        default_factory=lambda: {
            **{pair: True for pair in REQUIRED_CONTROLLER_PERMISSIONS},
            **{pair: False for pair in FORBIDDEN_CONTROLLER_PERMISSIONS},
        }
    )
    # Whether a tenant pod is still unschedulable. None models "no nodes, cannot
    # answer", which the gate treats as unverified rather than as denial.
    tenant_denied: bool | None = True
    # Names `place_system_workloads` refuses to place, so the "CoreDNS cannot be
    # prepared" case is expressible.
    unplaceable: tuple[str, ...] = ()
    # Names the cluster ACCEPTS for placement and which then never become available.
    # Distinct from `unplaceable` and the distinction is the substance of F2: a refused
    # placement raises at the placement step, whereas this one reports success and leaves
    # the workload at 0 available — an image it cannot pull, insufficient capacity, a
    # failing probe. That second case is the one the first revision registered as Ready,
    # and it is only caught by the readiness check AFTER placement, so a fake whose
    # placement always healed the workload could not express it at all.
    stays_unavailable: tuple[str, ...] = ()
    # The taint cannot be put back. The worst outcome this package can reach: nodes
    # schedulable, workspace unregistered, and no automatic way to close it.
    restore_fails: bool = False
    # The same failure, but TRANSIENT: the first N restorations fail and later ones
    # succeed. `restore_fails` alone models the permanent case and cannot express a
    # recovery that has to retry — a two-call test needs the first call to leave the
    # interlock off and the second to put it back, which is the F12 direction that
    # mirrors `release_fails_for`. Counts down, like `release_fails_for`, so a test says
    # how many attempts fail rather than flipping a flag between calls.
    restore_fails_for: int = 0

    # --- F2: the controller install seams ----------------------------------------
    # How `install_controller` behaves. "available" is the clean path: the Deployment
    # exists with every replica available, which is the state readiness requires.
    # "unavailable" installs it with 0/2 available — present and not serving, the exact
    # CoreDNS-shaped failure F2 was about, expressed for the controller.
    controller_install: str = "available"
    # `establish_controller_rbac` fails, or reports nothing created. The second is
    # separately expressible because "succeeded but named no objects" is a distinct
    # hazard from "failed": it is the case that used to leave objects cleanup could not
    # name.
    rbac_install_fails: bool = False
    rbac_reports_nothing: bool = False
    installed_rbac: list[tuple[str, str]] = field(default_factory=list)
    installed_controllers: list[tuple[str, str, str]] = field(default_factory=list)

    # Recorded calls, so a test can assert what was NOT done as well as what was.
    created_namespaces: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    established: list[tuple[str, ...]] = field(default_factory=list)
    removed_taints: list[str] = field(default_factory=list)
    restored_taints: list[str] = field(default_factory=list)
    placed_workloads: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    dry_run_calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    # Every seam call in order, so a test can assert the SEQUENCE of gates and not
    # merely that each ran. F1 asks for proof that the real entry point invokes every
    # gate; "in the right order" is the part that matters for the taint interlock.
    calls: list[str] = field(default_factory=list)

    def close(self):
        self.closed = True

    def bind_target(self, target, binding):
        assert (target.org_id, target.workspace_id) == (
            binding.principal.org_id,
            binding.principal.workspace_id,
        )

    def custom_resource_definitions(self) -> Sequence[str]:
        self.calls.append("custom_resource_definitions")
        return list(self.crds)

    def controller_deployments(self) -> Sequence[str]:
        self.calls.append("controller_deployments")
        return list(self.controller_images)

    def bootstrap_permission(self, **attributes):
        self.calls.append(
            "bootstrap_permission:" + attributes["verb"] + ":" + attributes["resource"]
        )
        return True

    def namespace(self, name: str) -> ObservedNamespace | None:
        self.calls.append(f"namespace:{name}")
        return self.namespaces.get(name)

    def create_namespace(
        self, name: str, labels: Mapping[str, str]
    ) -> ObservedNamespace:
        self.calls.append(f"create_namespace:{name}")
        self.created_namespaces.append((name, dict(labels)))
        observed = ObservedNamespace(name=name, uid=self.next_uid, labels=dict(labels))
        self.namespaces[name] = observed
        return observed

    def establish_crds(self, names: Sequence[str]) -> Sequence[str]:
        self.calls.append("establish_crds")
        self.established.append(tuple(names))
        if self.establish_crds_result is not None:
            return list(self.establish_crds_result)
        for name in names:
            if name not in self.crds:
                self.crds.append(name)
        return list(self.crds)

    # --- F2: the install seams ---------------------------------------------------

    def establish_controller_rbac(
        self, namespace: str, service_account: str
    ) -> Mapping[str, str]:
        self.calls.append(f"establish_controller_rbac:{namespace}")
        if self.rbac_install_fails:
            raise RuntimeError("synthetic RBAC apply failure")
        self.installed_rbac.append((namespace, service_account))
        if self.rbac_reports_nothing:
            return {}
        return {
            "ServiceAccount": service_account,
            "Role": f"{service_account}-workspace",
            "RoleBinding": f"{service_account}-workspace",
        }

    def install_controller(
        self, namespace: str, name: str, service_account: str
    ) -> ObservedWorkload:
        """Install the controller, and make it visible to `controller_deployments`.

        Registering the image is what makes the sequence honest: `components.py` refuses
        when a controller already exists and `readiness.py` requires exactly one
        afterwards, so a fake that installed a Deployment without listing its image
        would let the single-reconciler check pass by accident — it would be counting
        zero controllers and calling that "not exactly one"... in the wrong direction.
        """
        self.calls.append(f"install_controller:{namespace}/{name}")
        if self.controller_install == "fails":
            raise RuntimeError("synthetic controller apply failure")
        self.installed_controllers.append((namespace, name, service_account))
        self.controller_images.append(f"registry.example/{CONTROLLER_IMAGE_MARKER}:v1")
        if self.controller_install == "misnamed":
            observed = ObservedWorkload(
                name=f"{name}-typo",
                namespace=namespace,
                desired_replicas=2,
                available_replicas=2,
            )
            return observed
        available = 0 if self.controller_install == "unavailable" else 2
        observed = ObservedWorkload(
            name=name,
            namespace=namespace,
            desired_replicas=2,
            available_replicas=available,
        )
        self.workloads[(namespace, name)] = observed
        return observed

    # --- F2: the readiness seams -------------------------------------------------

    def workload(self, namespace: str, name: str) -> ObservedWorkload | None:
        self.calls.append(f"workload:{namespace}/{name}")
        return self.workloads.get((namespace, name))

    def place_system_workloads(
        self, namespace: str, names: Sequence[str]
    ) -> Mapping[str, bool]:
        """Tolerate the bootstrap taint for named system workloads only.

        Returns per-name success. A name in `unplaceable` fails, which is how the
        "CoreDNS cannot be scheduled behind the pending taint" case is expressed — the
        exact condition F2 found: readiness was declared while CoreDNS sat unschedulable.
        """
        self.calls.append(f"place_system_workloads:{namespace}")
        self.placed_workloads.append((namespace, tuple(names)))
        placed: dict[str, bool] = {}
        for name in names:
            if name in self.unplaceable:
                placed[name] = False
                continue
            existing = self.workloads.get((namespace, name))
            if existing is not None:
                # Placement succeeded, so the workload becomes available — unless this
                # name is one the cluster accepts and then cannot run. Both halves are
                # written here rather than left to the caller's initial `workloads` value,
                # because placement is the step that DECIDES availability: a caller that
                # seeded 0/2 would have it healed away, and a caller that seeded 2/2
                # would never see the degraded case. The knob has to act at this point or
                # it cannot act at all.
                available = (
                    0 if name in self.stays_unavailable else existing.desired_replicas
                )
                self.workloads[(namespace, name)] = ObservedWorkload(
                    name=name,
                    namespace=namespace,
                    desired_replicas=existing.desired_replicas,
                    available_replicas=available,
                )
            placed[name] = True
        return placed

    def tenant_scheduling_denied(self, namespace: str) -> bool | None:
        self.calls.append(f"tenant_scheduling_denied:{namespace}")
        return self.tenant_denied

    def controller_handover(self, namespace: str) -> Mapping[str, object]:
        self.calls.append(f"controller_handover:{namespace}")
        return dict(self.handover)

    def controller_permissions(self, namespace: str) -> Mapping[tuple[str, str], bool]:
        self.calls.append(f"controller_permissions:{namespace}")
        return dict(self.controller_rbac)

    def dry_run_pod(self, namespace: str, spec: Mapping[str, object]) -> ObservedPod:
        self.calls.append(f"dry_run_pod:{namespace}")
        self.dry_run_calls.append((namespace, dict(spec)))
        unsafe_keys = ("hostNetwork", "hostPID", "privileged", "hostPath")
        requested = [key for key in unsafe_keys if key in spec]
        name = str(spec.get("name", "pod"))
        if self.reject_everything:
            return ObservedPod(
                name=name, admitted=False, rejected_reason="exceeded quota: pods"
            )
        if requested and not self.admit_unsafe:
            return ObservedPod(
                name=name,
                admitted=False,
                rejected_reason=(
                    "violates PodSecurity restricted:v1.35: " + ", ".join(requested)
                ),
            )
        return ObservedPod(name=name, admitted=True)

    def imds_reachable_from_tenant_pod(self, namespace: str) -> Mapping[str, bool]:
        self.calls.append(f"imds_reachable_from_tenant_pod:{namespace}")
        return dict(self.imds)

    def can_tenant_change_admission_labels(self, namespace: str) -> bool:
        self.calls.append(f"can_tenant_change_admission_labels:{namespace}")
        return self.tenant_can_change_labels

    def cni_credential_scope(self) -> Mapping[str, object]:
        self.calls.append("cni_credential_scope")
        return dict(self.cni_scope)

    def node_taints(self) -> Sequence[Mapping[str, str]]:
        self.calls.append("node_taints")
        return [dict(taint) for taint in self.taints]

    def remove_bootstrap_taint(self, key: str) -> Sequence[Mapping[str, str]]:
        self.calls.append(f"remove_bootstrap_taint:{key}")
        self.removed_taints.append(key)
        self.taints = [taint for taint in self.taints if taint.get("key") != key]
        return [dict(taint) for taint in self.taints]

    def restore_bootstrap_taint(self, key: str) -> Sequence[Mapping[str, str]]:
        """Put the interlock back. The F5 undo.

        Returns the taints now present, so the caller VERIFIES restoration rather than
        trusting that a call returning without raising restored anything. `restore_fails`
        models the worst case in this package — the taint could not be put back and the
        workspace is unregistered — which `BootstrapOutcome.nodes_left_schedulable`
        must report as an alarm.
        """
        self.calls.append(f"restore_bootstrap_taint:{key}")
        self.restored_taints.append(key)
        transient = self.restore_fails_for > 0
        if transient:
            self.restore_fails_for -= 1
        if (
            not self.restore_fails
            and not transient
            and not any(taint.get("key") == key for taint in self.taints)
        ):
            self.taints.append({"key": key, "value": "pending", "effect": "NoSchedule"})
        return [dict(taint) for taint in self.taints]


@dataclass
class FakePrerequisiteAccess:
    """A configurable stand-in for authoritative AWS reads of the access path (F4).

    Defaults describe a correctly scoped access entry and both security-group rules,
    attributed to the expected account and VPC. Each negative test changes one field,
    so a refusal names one cause.

    Ownership is deliberately NOT settable to "created by bootstrap" by default: a real
    `aws` read cannot observe who created a rule, so `prerequisites._ownership` records
    ADOPTED unless the observation explicitly says otherwise. Tests that need a
    removable prerequisite set `created_by_bootstrap` — and there is a test asserting
    the default direction is the safe one.
    """

    entry_exists: bool = True
    entry_scope: str = "namespace"
    entry_namespaces: tuple[str, ...] | None = None
    entry_policy: str = ACCESS_POLICY_ARN
    entry_created_by_bootstrap: bool | None = None

    rules_exist: bool = True
    rule_account_id: str = ACCOUNT_ID
    rule_vpc_id: str = VPC_ID
    rule_port: int = 443
    rule_protocol: str = "tcp"
    rule_created_by_bootstrap: bool | None = None
    # Field names to omit from the rule observation, so the "incompletely observed"
    # refusal is reachable.
    omit_rule_fields: tuple[str, ...] = ()

    calls: list[str] = field(default_factory=list)

    def access_entry(
        self, cluster_arn: str, principal_arn: str
    ) -> Mapping[str, object]:
        self.calls.append(f"access_entry:{cluster_arn}")
        if not self.entry_exists:
            return {"exists": False}
        namespaces = (
            (NAMESPACE,) if self.entry_namespaces is None else self.entry_namespaces
        )
        observed: dict[str, object] = {
            "exists": True,
            "scope": self.entry_scope,
            "namespaces": namespaces,
            "policy": self.entry_policy,
            "cluster_arn": cluster_arn,
            "principal_arn": principal_arn,
            "access_entry_arn": cluster_arn.replace(":cluster/", ":access-entry/")
            + "/role/000000000000/SyntheticTestRole/entry-one",
        }
        if self.entry_created_by_bootstrap is not None:
            observed["created_by_bootstrap"] = self.entry_created_by_bootstrap
        return observed

    def security_group_rule(
        self, group_id: str, source: str, port: int, protocol: str
    ) -> Mapping[str, object]:
        self.calls.append(f"security_group_rule:{group_id}<-{source}")
        if not self.rules_exist:
            return {"exists": False}
        rule_id = ENDPOINT_RULE_ID if group_id == CLUSTER_SG_ID else MANAGEMENT_RULE_ID
        observed: dict[str, object] = {
            "exists": True,
            "rule_id": rule_id,
            "tags": {"OrgId": ORG_ID, "WorkspaceId": WORKSPACE_ID},
            "group_id": group_id,
            "source": source,
            "vpc_id": self.rule_vpc_id,
            "account_id": self.rule_account_id,
            "port": self.rule_port,
            "protocol": self.rule_protocol,
        }
        if self.rule_created_by_bootstrap is not None:
            observed["created_by_bootstrap"] = self.rule_created_by_bootstrap
        for name in self.omit_rule_fields:
            observed.pop(name, None)
        return observed


@dataclass
class FakeStateStore:
    """Durable bootstrap state, in memory, with the same read/write semantics.

    One store per workspace, matching `StateStore`: `load()` takes no arguments, so the
    store cannot be asked for a workspace it was not opened for. `load_state` supplies
    the workspace and cluster it EXPECTS and refuses a record naming another, which is
    the check a keyed store would have made unreachable.

    `history` keeps every saved value rather than only the latest, because the F6
    question is "was progress recorded BEFORE the next mutation" — a question about the
    sequence of writes, not about the final state. A store keeping only the last value
    could not tell a correct implementation from one that wrote everything at the end.

    `fail_on` makes the Nth save raise, so "the durable write itself failed" is testable.
    """

    current: BootstrapState | None = None
    history: list[Mapping[str, object]] = field(default_factory=list)
    fail_on: int | None = None

    def load(self) -> BootstrapState | None:
        return self.current

    def save(self, state: BootstrapState) -> None:
        if self.fail_on is not None and len(self.history) == self.fail_on:
            raise OSError("synthetic durable-state write failure")
        # Round-tripped through the mapping form so the fake exercises the same
        # serialization the file store does. A fake that stored the object directly
        # would hide a field that cannot survive `to_mapping`/`state_from_mapping`.
        payload = state.to_mapping()
        self.history.append(payload)
        self.current = state_from_mapping(payload)


@dataclass
class FakeRegistrationStore:
    """An in-memory registration store implementing the reserve/finalize/release contract.

    There is no `write` method, matching `RegistrationStore`: a second way to create a
    record would be a way to create one without a reservation, which is the F5 hole.

    `finalized` is a list rather than a dict so a replay that wrongly wrote twice is
    visible as a length, which is what the duplicate-registration test asserts.
    """

    records: dict[str, object] = field(default_factory=dict)
    finalized: list[object] = field(default_factory=list)
    reservations: dict[str, dict[str, str]] = field(default_factory=dict)
    released: list[str] = field(default_factory=list)
    # F10: the token issued per held claim, keyed by workspace. A real column on the
    # reservation row, modelled as a parallel dict so `reservations` keeps holding exactly
    # the identity the store was given and a test can assert over either independently.
    tokens: dict[str, str] = field(default_factory=dict)
    # Fixed rather than random so a failure message is readable and a test can assert the
    # exact value threaded through reserve -> finalize. The production store uses
    # `secrets.token_hex`; what this fake exists to exercise is the plumbing, and
    # `test_registry.py` covers the generation.
    issued_token: str = "attempt-token-issued-by-the-fake-store"
    # Make the reservation refuse, modelling a concurrent bootstrap that got there
    # first. The conflicting identity is what a real store would report.
    conflict: str = ""
    # Make `finalize` raise. The F5 case where the registration write fails AFTER the
    # taint was cleared, so the taint must be restored on the way out.
    finalize_fails: bool = False
    # Omit the `reserved` key entirely — an unanswered reservation, which must refuse
    # rather than read as a held one.
    reserve_answers: bool = True
    # How many of the first `release` calls raise, modelling a store that is unreachable
    # now and reachable later. A COUNT rather than a boolean because F12's second half is
    # about a release that is RETRIED: a permanent failure and a transient one are
    # indistinguishable from a single call, and only the transient case can show that the
    # retry actually happens and then stops happening.
    release_fails_for: int = 0
    recovery_unreachable: bool = False
    calls: list[str] = field(default_factory=list)

    def reserve(
        self, workspace_id: str, identity: Mapping[str, str]
    ) -> Mapping[str, object]:
        self.calls.append(f"reserve:{workspace_id}")
        if not self.reserve_answers:
            return {}
        if self.conflict:
            return {"reserved": False, "conflict": self.conflict}
        existing = self.reservations.get(workspace_id)
        if existing is not None:
            divergent = sorted(
                name
                for name in set(existing) | set(identity)
                if str(existing.get(name, "")) != str(identity.get(name, ""))
            )
            if divergent:
                return {
                    "reserved": False,
                    "conflict": f"already bound with a different {', '.join(divergent)}",
                }
            # F10: a matching row is a REPLAY only when the registration completed. An
            # unfinalized claim with the same identity is a live competitor, and reporting
            # `reserved: True` for it is the defect the finding names — so the fake makes
            # the same distinction the production store does. A fake that kept returning
            # the old answer would let `workspace.py` regress while every test passed.
            if workspace_id in self.records:
                return {"reserved": True, "replayed": True}
            return {
                "reserved": False,
                "conflict": "another attempt already holds an unfinalized reservation",
            }
        self.reservations[workspace_id] = {k: str(v) for k, v in identity.items()}
        self.tokens[workspace_id] = self.issued_token
        return {
            "reserved": True,
            "replayed": False,
            "attempt_token": self.issued_token,
        }

    def read(self, workspace_id: str) -> object | None:
        self.calls.append(f"read:{workspace_id}")
        return self.records.get(workspace_id)

    def finalize(self, target: object, attempt_token: str = "") -> None:
        workspace_id = getattr(target, "workspace_id", "")
        self.calls.append(f"finalize:{workspace_id}")
        if self.finalize_fails:
            raise OSError("synthetic registration write failure")
        # F10: the fence, enforced by the fake too. A fake that accepted any token would
        # make every `workspace.py` test pass whether or not the token was threaded from
        # reserve to finalize at all — which is the whole of the repair at this layer.
        held = self.tokens.get(workspace_id)
        if held is not None and str(attempt_token or "") != held:
            raise BootstrapRefused(
                f"the reservation for workspace {workspace_id!r} is held by a different "
                "bootstrap attempt"
            )
        self.finalized.append(target)
        self.records[target.workspace_id] = target

    def release(self, workspace_id: str, attempt_token: str = "") -> bool:
        self.calls.append(f"release:{workspace_id}")
        if self.release_fails_for > 0:
            self.release_fails_for -= 1
            # Raised rather than returned falsy, because that is the shape a real store
            # failure has: `SqlRegistrationStore.release` propagates whatever the driver
            # raised, and `_release_reservation` is the thing that converts it. A fake
            # that returned False instead would never exercise that conversion.
            raise OSError("synthetic reservation release failure")
        if not str(attempt_token or "").strip():
            raise BootstrapRefused(
                f"no attempt token was supplied to release workspace {workspace_id!r}"
            )
        # A token that does not match the held claim releases NOTHING, and says so. This
        # is F10's loser-release case: the loser is not an error, it simply has no claim.
        if self.tokens.get(workspace_id) != str(attempt_token):
            return False
        self.released.append(workspace_id)
        self.tokens.pop(workspace_id, None)
        return self.reservations.pop(workspace_id, None) is not None

    def recover_claim(self, workspace_id, fingerprint, *, restore):
        if self.recovery_unreachable:
            raise OSError("synthetic unreachable recovery store")
        held = self.tokens.get(workspace_id)
        if (
            workspace_id in self.records
            or held is None
            or fingerprint_of(held) != fingerprint
        ):
            return False, False, False
        if not restore():
            return True, False, False
        return True, True, self.release_claim(workspace_id, fingerprint)

    def release_claim(self, workspace_id: str, claim_fingerprint: str) -> bool:
        """The recovery path's release, fenced on a claim FINGERPRINT (F13).

        Modelled separately from `release` because the authority differs, as in production.

        The fence is enforced here too, and that is the point of touching this fake at all:
        it used to delete whatever reservation it held for the workspace, so every
        `workspace.py` recovery test would have passed whether or not the fingerprint was
        threaded from the state file to the store — which is the entire repair at this
        layer. A fake that answered the old way would let F13 come straight back.
        """
        self.calls.append(f"release_claim:{workspace_id}")
        if self.release_fails_for > 0:
            self.release_fails_for -= 1
            raise OSError("synthetic reservation release failure")
        if not str(claim_fingerprint or "").strip():
            raise BootstrapRefused(
                "no claim fingerprint was supplied to release workspace "
                f"{workspace_id!r}"
            )
        # Never a completed registration, matching `AND state = 'reserved'`.
        if workspace_id in self.records:
            return False
        # The fingerprint must match the claim actually held. A stale fingerprint — the F13
        # case — names a claim that is gone, and deletes nothing even though a DIFFERENT
        # attempt's reservation is sitting right there.
        held = self.tokens.get(workspace_id)
        if held is None or fingerprint_of(held) != str(claim_fingerprint):
            return False
        self.released.append(workspace_id)
        self.tokens.pop(workspace_id, None)
        return self.reservations.pop(workspace_id, None) is not None
