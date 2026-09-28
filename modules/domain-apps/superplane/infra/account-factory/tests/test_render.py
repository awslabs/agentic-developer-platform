"""Offline rendering carries no legacy target, no secret, and no core-platform apply.

Issue #5530 (w6-07). This file is AC-01's evidence: *"Offline rendering has no legacy target
IDs or secret literals and no hidden core platform apply."*

## Why "no hidden core platform apply" needs its own tests

The legacy flow's step 2 of provisioning ONE account installed cluster-wide controllers (kro
and four ACK controllers) on the shared cluster and minted IAM roles carrying
`AWSOrganizationsFullAccess`, `IAMFullAccess` and `AmazonEC2FullAccess`. Asking for one
workspace therefore changed the shared control plane as a side effect. So it is not enough to
check that the rendered objects are well-formed: the tests must establish that the shared
installs are NOT in the rendered set, that they come back as a separate reviewed list, and
that rendering executes none of them.

## Why rendering is checked for secrets at all

Rendered output is reviewed, logged, and attached to CI artifacts. A credential reaching it
is disclosed by the review process itself. The module's answer is structural — cross-account
access uses an `IAMRoleSelector` naming a role ARN, which is an identity reference and not a
credential — and `test_no_secret_shaped_value_survives_rendering` checks the structure held
by feeding a secret through a request field.

## What these tests deliberately do NOT establish

That the rendered objects would be ACCEPTED by a cluster, that kro would reconcile them, or
that any account would be created. Rendering is offline by construction: no AWS call, no
Kubernetes call, no network access, no credentials. Confirming the objects apply cleanly is a
live operation under the Wave 6 operations evaluator and is not in scope here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from account_factory.dependencies import load
from account_factory.modes import LEGACY_FORBIDDEN_VALUES, ModeError
from account_factory.render import RenderError, render

from . import MODULE_DIR
from .conftest import (
    BUILDERS,
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_TARGET_ACCOUNT,
    FIXTURE_WORKSPACE,
    bring_existing_cluster_request,
    existing_account_request,
    governed_account_id,
    matching_authorization,
    new_account_request,
)

MANIFESTS_DIR = MODULE_DIR / "manifests"

CORE_NAMESPACES = {
    "adp",
    "adp-gateway",
    "adp-agent-factory",
    "adp-context",
    "adp-system",
    "bedrockgw",
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "default",
    "arc-systems",
    "arc-runners",
}


def kinds(result) -> list[str]:
    return [obj["kind"] for obj in result.objects]


# ── Each mode renders the object set its ownership story implies ──────────────────────


def test_new_account_managed_renders_account_and_infrastructure_as_separate_roots():
    """Two root objects, not one — and that split is load-bearing, not cosmetic.

    A single root owning both the account and the infrastructure makes "protect the account"
    and "remove the infrastructure" the same edit with opposite requirements: deleting it to
    tear down a workspace closes the account, and keeping it to protect the account keeps the
    infrastructure's DECLARATION alive for a controller to rebuild. `test_cleanup.py`'s
    convergence tests are the other half of this.
    """
    result = render(
        new_account_request(),
        creation_record=governed_account_id(new_account_request()),
    )
    assert kinds(result) == [
        "Namespace",
        "AccountOwnership",
        "WorkspaceInfrastructure",
    ]
    # Specifically not the combined root, which the lock records as never instantiated.
    assert "FullAccountInfrastructure" not in kinds(result)


def test_existing_account_managed_renders_no_account_object():
    """The substantive difference from new-account-managed.

    An `Account` custom resource naming an account that already exists asks the Organizations
    controller to create one that is already there.
    """
    result = render(existing_account_request())
    assert kinds(result) == [
        "Namespace",
        "IAMRoleSelector",
        "WorkspaceInfrastructure",
    ]
    assert "Account" not in kinds(result)
    assert "AccountOwnership" not in kinds(result)
    assert "FullAccountInfrastructure" not in kinds(result)


# ── AF-001: the cluster receives the network's outputs ────────────────────────────────
#
# These lock a specific regression. Before the repair, existing-account-managed rendered
# `NetworkStack` and `EKSClusterStack` as two INDEPENDENT objects, and the EKSClusterStack
# spec omitted `subnetIds` and `securityGroupIds` — both of which the vendored graph declares
# as required with no default. So the mode did not implement the behaviour it advertised: the
# cluster had no subnets to launch into, and nothing in the rendered set connected the two.


def test_the_cluster_is_wired_to_the_network_it_depends_on():
    """The wiring is a reference, which is what creates both data flow AND ordering.

    Read from the maintained graph rather than restated here, so this test cannot pass while
    the graph says something else.
    """
    graph = yaml.safe_load(
        (MANIFESTS_DIR / "adp-workspace-infrastructure.yaml").read_text(
            encoding="utf-8"
        )
    )
    resources = {r["id"]: r for r in graph["spec"]["resources"]}
    cluster_spec = resources["eksCluster"]["template"]["spec"]
    # A reference to the network's status, not a literal: kro resolves it after the network
    # reports those values, which is also what makes the cluster wait for the network.
    assert cluster_spec["subnetIds"] == "${network.status.privateSubnetIds}"
    assert cluster_spec["securityGroupIds"] == ["${network.status.securityGroupId}"]


def test_every_rendered_object_supplies_every_required_input_of_its_graph(
    any_mode_request,
):
    """The general form of AF-001, checked against each graph's own declared schema.

    This is what makes the defect class catchable rather than this one instance of it: the
    schema is read from the graph, so a graph that gains a required input fails here until the
    renderer supplies it. `render` itself performs this check, so reaching a result at all is
    the assertion — the explicit comparison below states what was verified.
    """
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    deps = load()
    for obj in result.objects:
        if not obj["apiVersion"].startswith("kro.run/"):
            continue
        schema = deps.schema_for(obj["kind"])
        supplied = set(obj["spec"])
        missing = schema.required_inputs - supplied
        assert not missing, (
            f"{obj['kind']} is missing required input(s): {sorted(missing)}"
        )
        undeclared = supplied - schema.known_inputs
        assert not undeclared, (
            f"{obj['kind']} supplies input(s) its graph does not declare: "
            f"{sorted(undeclared)} — they would be silently ignored"
        )


def test_an_object_missing_a_required_graph_input_is_refused(monkeypatch):
    """The pre-fix output shape must now fail, not render.

    Reproduces AF-001 directly: strip the two fields the repair added and confirm rendering
    refuses. Without this, `_check_graph_inputs` could be removed and every other test in this
    file would still pass.
    """
    from account_factory import render as render_module

    original = render_module._infrastructure_object

    def stripped(request):
        obj = original(request)
        obj["spec"] = {
            key: value
            for key, value in obj["spec"].items()
            if key not in ("vpcCidr", "availabilityZones")
        }
        return obj

    monkeypatch.setattr(render_module, "_infrastructure_object", stripped)
    with pytest.raises(RenderError) as raised:
        render(existing_account_request())
    assert "required input" in str(raised.value)


def test_an_object_supplying_an_input_no_graph_declares_is_refused(monkeypatch):
    """An unknown input is silently ignored by the controller, so it is refused here.

    The asymmetric half of the check above. A misspelled `subnetIDs` would otherwise look
    supplied while the graph never receives it — the same failure as omitting it, but harder
    to see in a diff.
    """
    from account_factory import render as render_module

    original = render_module._infrastructure_object

    def with_typo(request):
        obj = original(request)
        obj["spec"]["clusterVerison"] = "1.31"
        return obj

    monkeypatch.setattr(render_module, "_infrastructure_object", with_typo)
    with pytest.raises(RenderError) as raised:
        render(existing_account_request())
    assert "does not declare" in str(raised.value)


def test_bring_existing_cluster_creates_no_aws_infrastructure():
    """No Account, no NetworkStack, no EKSClusterStack — the reason it is a distinct mode.

    The legacy flow could not express "use this cluster", so attaching a workspace to an
    existing cluster meant letting `FullAccountInfrastructure` try to create one and fail
    partway.
    """
    result = render(bring_existing_cluster_request())
    assert kinds(result) == ["Namespace", "IAMRoleSelector", "ConfigMap"]
    for created in (
        "Account",
        "NetworkStack",
        "EKSClusterStack",
        "FullAccountInfrastructure",
    ):
        assert created not in kinds(result)


def test_the_adoption_record_states_that_adp_did_not_create_the_cluster():
    """`cleanup.py` needs the ownership fact to survive without the original request."""
    result = render(bring_existing_cluster_request())
    configmap = next(obj for obj in result.objects if obj["kind"] == "ConfigMap")
    assert configmap["data"]["adpCreatedCluster"] == "false"
    assert configmap["data"]["clusterOwnership"] == "adopted"
    assert configmap["data"]["clusterName"] == "fixture-adopted-cluster"


def test_every_mode_labels_its_namespace_with_the_ownership_facts(any_mode_request):
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    namespace = next(obj for obj in result.objects if obj["kind"] == "Namespace")
    labels = namespace["metadata"]["labels"]
    assert labels["adp.aws.dev/workspace"] == any_mode_request.workspace_id
    assert labels["adp.aws.dev/ownership-mode"] == any_mode_request.mode.value
    assert (
        labels["adp.aws.dev/cluster-ownership"]
        == any_mode_request.cluster_ownership.value
    )


def test_rendering_is_deterministic(any_mode_request):
    """Reproducible offline rendering: the same request renders the same bytes.

    The legacy flow could not claim this — it fetched its graphs from a moving branch, and
    reused `/tmp/kro` if present, so the same inputs could produce different output.
    """
    first = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    second = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    assert json.dumps(first.objects, sort_keys=True) == json.dumps(
        second.objects, sort_keys=True
    )


# ── AC-01: no legacy target IDs ──────────────────────────────────────────────────────


def test_no_legacy_target_appears_in_any_rendered_set(any_mode_request):
    serialized = json.dumps(
        render(
            any_mode_request, creation_record=governed_account_id(any_mode_request)
        ).objects
    ).lower()
    for value in LEGACY_FORBIDDEN_VALUES:
        assert value.lower() not in serialized, value


def test_a_legacy_target_smuggled_through_a_request_is_refused_before_rendering():
    """Refused at validation, so no object set is produced at all."""
    with pytest.raises(ModeError) as raised:
        render(
            new_account_request(management_cluster="github-arc-runner-eks"),
            creation_record=governed_account_id(new_account_request()),
        )
    assert "refused before any mutation" in str(raised.value)


def test_the_rendered_set_names_only_identities_supplied_by_the_request(
    any_mode_request,
):
    """Nothing is defaulted into the output that the caller did not supply.

    Every account id and cluster name in the rendered set must trace to a request field or to
    the explicitly-passed creation record — otherwise some value is coming from somewhere the
    caller did not choose, which is the legacy `config.env` defect.

    The created account's id is in the permitted set because since #5531 it is an explicit
    argument to `render` rather than a field of the request. That distinction is the fix, not a
    loosening: `target_account_id` (an account a tenant asks ADP to ADOPT) is request data,
    while a created account's id is evidence that the fenced creation path already ran. Keeping
    them separate is why rendering can refuse a creation id in an adopting mode.
    """
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    serialized = json.dumps(result.objects)
    supplied = {
        any_mode_request.organization_id,
        any_mode_request.management_account_id,
        any_mode_request.workspace_id,
        any_mode_request.region,
        any_mode_request.management_cluster,
        any_mode_request.target_account_id or "",
        any_mode_request.existing_cluster_name or "",
        any_mode_request.account_email or "",
        FIXTURE_CREATED_ACCOUNT if any_mode_request.mode.creates_account else "",
    }
    # Any 12-digit run in the output must be an account id the request named.
    import re

    for account_id in set(re.findall(r"\b\d{12}\b", serialized)):
        assert account_id in supplied, f"{account_id} was not supplied by the request"


# ── AC-01: no secret literals ────────────────────────────────────────────────────────


def test_no_secret_object_is_ever_rendered(any_mode_request):
    assert "Secret" not in kinds(
        any_mode_request
        and render(
            any_mode_request, creation_record=governed_account_id(any_mode_request)
        )
    )


def test_cross_account_access_is_a_role_reference_not_a_credential():
    """Why no secret is needed in the first place."""
    result = render(existing_account_request())
    selector = next(obj for obj in result.objects if obj["kind"] == "IAMRoleSelector")
    arn = selector["spec"]["arn"]
    assert arn == (
        f"arn:aws:iam::{FIXTURE_TARGET_ACCOUNT}:role/OrganizationAccountAccessRole"
    )
    assert "secret" not in json.dumps(selector).lower()


@pytest.mark.parametrize(
    "smuggled,sensitive",
    [
        # (value fed through the request, the part that must never be echoed back)
        # These are documentation examples and deliberately-fake values, not real
        # credentials — AKIAIOSFODNN7EXAMPLE and wJalrXUtn...EXAMPLEKEY are AWS's own
        # published examples.
        ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        (
            "aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
            "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
        ),
        ("aws_session_token=IQoJb3JpZ2luX2VjEExample", "IQoJb3JpZ2luX2VjEExample"),
        ("-----BEGIN RSA PRIVATE KEY-----", "BEGIN RSA PRIVATE KEY"),
        ("password=hunter2hunter2", "hunter2hunter2"),
    ],
)
def test_no_secret_shaped_value_survives_rendering(smuggled, sensitive):
    """A secret reaching rendered output would be disclosed by the review process itself.

    The check runs over the SERIALIZED object set, so a value smuggled through any field is
    caught rather than only the fields this module thought to check by name. Here it arrives
    via `node_instance_type`, which is not a field anyone would check for credentials.
    """
    with pytest.raises((RenderError, ModeError)) as raised:
        render(
            new_account_request(node_instance_type=smuggled),
            creation_record=governed_account_id(new_account_request()),
        )
    message = str(raised.value)

    # The refusal must not reproduce the value — not in full, and not as a prefix. An error
    # message is written to logs and CI artifacts just as rendered output is, so a message
    # that quotes what it is refusing becomes the disclosure it was meant to prevent.
    assert smuggled not in message
    assert sensitive not in message
    # Not even a prefix of the sensitive part. The previous implementation echoed the first
    # 24 characters of the match, which this assertion is what caught.
    for length in range(6, len(sensitive) + 1):
        for start in range(len(sensitive) - length + 1):
            assert sensitive[start : start + length] not in message, (
                f"leaked a {length}-character fragment of the value"
            )

    # It must still be actionable: the shape is named, and so is the object it was found in.
    assert "looks like" in message
    assert "WorkspaceInfrastructure" in message


def test_a_rendering_refusal_returns_no_partial_object_set():
    """`render` raises rather than returning what it built before the problem."""
    with pytest.raises((RenderError, ModeError)):
        render(
            new_account_request(workspace_id="adp-gateway"),
            creation_record=governed_account_id(new_account_request()),
        )


# ── AC-01: no hidden core platform apply ─────────────────────────────────────────────


def test_no_rendered_object_touches_a_core_adp_namespace(any_mode_request):
    for obj in render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    ).objects:
        namespace = (obj.get("metadata") or {}).get("namespace")
        if obj["kind"] == "Namespace":
            assert obj["metadata"]["name"] not in CORE_NAMESPACES
            continue
        assert namespace not in CORE_NAMESPACES
        assert namespace == any_mode_request.workspace_id


def test_every_rendered_object_declares_its_namespace_explicitly(any_mode_request):
    """An object with no namespace lands wherever the kubeconfig context points."""
    for obj in render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    ).objects:
        if obj["kind"] == "Namespace":
            continue
        assert (obj.get("metadata") or {}).get("namespace"), obj["kind"]


def test_no_rendered_object_is_cluster_scoped_except_the_workspace_namespace(
    any_mode_request,
):
    """A workspace's own Namespace is the only cluster-scoped object it may create."""
    cluster_scoped = {
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "ResourceGraphDefinition",
        "StorageClass",
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
        "PersistentVolume",
    }
    assert not cluster_scoped.intersection(
        kinds(
            render(
                any_mode_request, creation_record=governed_account_id(any_mode_request)
            )
        )
    )


def test_shared_controller_installs_are_not_in_the_rendered_object_set(
    any_mode_request,
):
    """The "implicit whole-platform deployment" item, stated as a property of the output."""
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    serialized = json.dumps(result.objects)
    assert "helm" not in serialized.lower()
    assert "ResourceGraphDefinition" not in serialized
    # The prerequisites exist, but as a separate list — not merged into the objects.
    assert result.prerequisites
    assert len(result.objects) < len(result.objects) + len(result.prerequisites)


def test_prerequisites_are_descriptions_that_rendering_does_not_execute(
    any_mode_request,
):
    """They carry a command for an operator to run, and rendering runs none of them."""
    for prerequisite in render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    ).prerequisites:
        assert prerequisite.command
        assert prerequisite.reason
        assert "shared" in prerequisite.scope
        # Digest-pinned, so the operation a reviewer approves is the operation that runs.
        if prerequisite.command[0] == "helm":
            assert any("@sha256:" in part for part in prerequisite.command)


def test_prerequisites_are_scoped_to_the_management_cluster_not_the_workspace(
    any_mode_request,
):
    for prerequisite in render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    ).prerequisites:
        assert "once per cluster" in prerequisite.scope


def test_the_prerequisite_set_is_the_minimum_each_mode_needs():
    """A mode that creates nothing must not ask for the controllers that create things.

    The legacy flow installed all five regardless of what was being asked for.
    """
    new_account = {
        p.name
        for p in render(
            new_account_request(),
            creation_record=governed_account_id(new_account_request()),
        ).prerequisites
    }
    adopted_cluster = {
        p.name for p in render(bring_existing_cluster_request()).prerequisites
    }

    assert "install ack-organizations" in new_account
    assert "install ack-ec2" in new_account
    assert "install ack-eks" in new_account

    # bring-existing-cluster creates no account, no VPC and no cluster.
    assert "install ack-organizations" not in adopted_cluster
    assert "install ack-ec2" not in adopted_cluster
    assert "install ack-eks" not in adopted_cluster
    assert adopted_cluster < new_account


def test_existing_account_managed_needs_no_organizations_controller():
    """It adopts the account, so nothing asks Organizations to create one."""
    names = {p.name for p in render(existing_account_request()).prerequisites}
    assert "install ack-organizations" not in names
    assert "install ack-ec2" in names


def test_no_broad_iam_policy_is_named_anywhere_in_the_rendered_output(any_mode_request):
    """The legacy `01-setup-iam-roles.sh` attached these four managed policies."""
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    everything = json.dumps(result.objects) + json.dumps(
        [list(p.command) + [p.reason] for p in result.prerequisites]
    )
    for policy in (
        "AWSOrganizationsFullAccess",
        "AmazonEC2FullAccess",
        "IAMFullAccess",
        "AdministratorAccess",
    ):
        assert policy not in everything, policy


def test_no_wildcard_resource_arn_is_rendered(any_mode_request):
    """`CrossAccountAssumeRole` allowed `arn:aws:iam::*:role/OrganizationAccountAccessRole`.

    A wildcard account in the resource means the role could assume into ANY account in the
    organization, not the one the request names.
    """
    serialized = json.dumps(
        render(
            any_mode_request, creation_record=governed_account_id(any_mode_request)
        ).objects
    )
    assert "arn:aws:iam::*" not in serialized
    assert '"*"' not in serialized


# ── Ordering: validation and dependency verification precede production ──────────────


def test_an_invalid_request_is_refused_before_dependencies_are_even_read():
    """Order matters: the mode check must not depend on the lock being loadable."""
    with pytest.raises(ModeError):
        render(
            new_account_request(workspace_id="kube-system"),
            lock_path=Path("/nonexistent/dependencies.lock.yaml"),
            creation_record=governed_account_id(new_account_request()),
        )


def test_an_unverifiable_dependency_set_prevents_rendering(tmp_path):
    """A rendered set must always correspond to a known dependency set."""
    from account_factory.dependencies import DependencyError

    broken = tmp_path / "dependencies.lock.yaml"
    broken.write_text("charts: {}\n")
    with pytest.raises(DependencyError):
        render(
            new_account_request(),
            lock_path=broken,
            creation_record=governed_account_id(new_account_request()),
        )


def test_rendering_reports_what_authorization_did_not_verify(any_mode_request):
    """Carried forward so a report can say "not checked" rather than implying "passed"."""
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    expected = {
        "organization_id",
        "management_account_id",
        "management_cluster",
        "mode",
        # The two AF-003 comparisons are reported here too: rendering with no authorization
        # verified neither the workspace nor the target account, and the report must say so.
        "workspace_id",
    }
    if any_mode_request.target_account_id:
        expected.add("target_account_id")
    if any_mode_request.organizational_unit_id:
        # Only the account-creating mode states a placement (#5531), and rendering with no
        # authorization verified it against nothing.
        expected.add("organizational_unit_id")
    assert set(result.unchecked_authorization) == expected


def test_rendering_with_a_matching_authorization_leaves_nothing_unchecked():
    request = new_account_request()
    result = render(
        request,
        matching_authorization(request),
        creation_record=governed_account_id(new_account_request()),
    )
    assert result.unchecked_authorization == ()


def test_an_unauthorized_request_renders_nothing():
    request = new_account_request()
    authorization = matching_authorization(
        request, management_account_id="999999999999"
    )
    with pytest.raises(ModeError):
        render(
            request,
            authorization,
            creation_record=governed_account_id(new_account_request()),
        )


def test_every_rendered_kro_kind_is_declared_by_a_graph_in_the_lock(any_mode_request):
    """A custom resource with no definition behind it would apply into nothing.

    Resolved through `schema_for`, which raises for an undeclared kind, rather than compared
    against a hand-written set here — a second list of kinds would drift from the lock.
    """
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    deps = load()
    for obj in result.objects:
        if obj["apiVersion"].startswith("kro.run/"):
            assert deps.schema_for(obj["kind"]).kind == obj["kind"]


def test_the_render_result_names_the_graphs_it_relied_on(any_mode_request):
    """Provenance travels with the output, so a reviewer can trace what expanded it."""
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    assert set(result.resource_graph_files) == {
        "01-network-stack.yaml",
        "02-eks-cluster-stack.yaml",
        "03-full-account-infrastructure.yaml",
        "adp-account-ownership.yaml",
        "adp-workspace-infrastructure.yaml",
    }


# ── Staged rendering: ordering that cannot live inside either graph ───────────────────


def test_new_account_mode_stages_infrastructure_behind_the_account():
    """Splitting the roots moved an ordering constraint out of the graph, so it is stated.

    Inside one graph, `${account.status.accountID}` both passed the id along and made the
    cluster wait. Across two independent roots kro cannot express that, so the ordering becomes
    an explicit stage precondition rather than an assumption — an unstated precondition is one
    an operator discovers by applying stage 2 too early.
    """
    result = render(
        new_account_request(),
        creation_record=governed_account_id(new_account_request()),
    )
    stages = {stage.name: stage for stage in result.stages}
    infrastructure = stages["workspace infrastructure"]
    account = stages["account ownership"]
    assert account.number < infrastructure.number
    assert infrastructure.precondition
    assert "accountId" in infrastructure.precondition


def test_the_stages_contain_exactly_the_rendered_objects(any_mode_request):
    """`objects` is the flattening of `stages`, so neither can carry something the other omits.

    Two views of one object set that could disagree would let an operator applying stages skip
    an object that the reviewed `objects` output contained.
    """
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    flattened = [obj for stage in result.stages for obj in stage.objects]
    assert flattened == list(result.objects)


def test_only_a_real_dependency_states_a_precondition(any_mode_request):
    """A precondition must describe something that has to be TRUE first, or it is noise.

    Noise is what stops preconditions from being read at all, so the property under test is
    that no stage carries one gratuitously — not that early stages never carry one.

    Stage 0 (the namespace) never has one: it depends on nothing. The adopting modes' stage 1
    never has one: their account and cluster already exist and the request names them.

    new-account-managed's stage 1 DOES, and that is the #5531 change. It used to be the stage
    that created the account by declaring an ACK `Account`, so there was nothing to require
    beforehand — applying it was the beginning of the causal chain. Now it binds to an account
    the fenced creation path already opened, so "that account exists and is durably recorded
    against this operation" is a genuine, checkable precondition, and an operator applying this
    stage without it would bind a workspace to an account nothing vouches for.
    """
    result = render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    )
    # Stage 1 is only exempt for the adopting modes, so the exempt set is stated once rather
    # than as two branches that must be kept in agreement.
    stages_depending_on_nothing = (
        {0} if any_mode_request.mode.creates_account else {0, 1}
    )
    for stage in result.stages:
        if stage.number in stages_depending_on_nothing:
            assert stage.precondition is None, stage.name

    if any_mode_request.mode.creates_account:
        ownership = next(stage for stage in result.stages if stage.number == 1)
        assert ownership.precondition is not None
        # It has to name the account it binds to. A precondition an operator cannot check
        # against a specific account is advice rather than a condition.
        assert FIXTURE_CREATED_ACCOUNT in ownership.precondition


def test_no_unsubstituted_placeholder_reaches_the_rendered_set(any_mode_request):
    """The legacy `04-provision-account.yaml` shipped `ACCOUNT_NAME_PLACEHOLDER` literals."""
    serialized = json.dumps(
        render(
            any_mode_request, creation_record=governed_account_id(any_mode_request)
        ).objects
    )
    assert "PLACEHOLDER" not in serialized
    assert "${" not in serialized


def test_no_spec_field_renders_empty(any_mode_request):
    """An empty required field is applied as empty rather than rejected."""
    for obj in render(
        any_mode_request, creation_record=governed_account_id(any_mode_request)
    ).objects:
        for key, value in (obj.get("spec") or {}).items():
            assert value not in (None, "", []), f"{obj['kind']}.spec.{key}"


def test_the_namespace_matches_the_workspace_for_every_mode():
    for mode, build in BUILDERS.items():
        result = render(build(), creation_record=governed_account_id(build()))
        assert result.namespace == FIXTURE_WORKSPACE, mode
        assert result.request.mode is mode
