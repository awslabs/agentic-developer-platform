from dataclasses import replace
from datetime import UTC, datetime
import json
from uuid import uuid4

import pytest

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.eks_grants import EksGrants
from superplane_bootstrap.kube_grants import KubeGrants
from workspace_provisioning.retirement_inventory import (
    load_bootstrap_retirement_inventory,
    retained_cleanup_capability,
    require_dormant_cleanup_group,
)


def load(runtime, **kwargs):
    return load_bootstrap_retirement_inventory(
        registration_store=runtime.store,
        binding=kwargs.get("binding", replace(runtime.binding, action="teardown")),
    )


def test_actual_bootstrap_journal_supplies_retirement_ownership(runtime):
    result = runtime.run()
    assert result.ready
    inventory = load(runtime)
    assert inventory.remove_namespace
    assert len(inventory.grants) == 7
    assert len(inventory.prerequisites) == 3
    assert not inventory.preserve_cluster
    assert all(grant.spec["actor"] == "supervisor" for grant in inventory.grants)
    assert all(
        grant.identity.get("uid") or grant.identity.get("arn")
        for grant in inventory.grants
    )


def test_dedicated_owner_cannot_retire_after_cluster_opens_for_sharing(runtime):
    assert runtime.run().ready
    db = runtime.store.store
    with db.transaction():
        db.execute("UPDATE clusters SET sharing_enabled=true", {})
    with pytest.raises(BootstrapRefused, match="membership-scoped retirement"):
        load(runtime)
    # Withdrawal of sharing eligibility restores the unchanged dedicated path;
    # no cluster/namespace/credential object was removed by the refused review.
    with db.transaction():
        db.execute("UPDATE clusters SET sharing_enabled=false", {})
    assert load(runtime).remove_namespace


def test_live_peer_prevents_owner_retirement_even_after_sharing_is_disabled(runtime):
    assert runtime.run().ready
    db = runtime.store.store
    peer, member = str(uuid4()), str(uuid4())
    with db.transaction():
        db.execute(
            "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) "
            "SELECT CAST(:peer AS uuid),org_id,'peer','namespace','Provisioning',false "
            "FROM workspaces WHERE id=CAST(:owner AS uuid)",
            {"peer": peer, "owner": runtime.target.workspace_id},
        )
        db.execute(
            "INSERT INTO cluster_memberships(id,org_id,workspace_id,cluster_id,generation,namespace,state) "
            "SELECT CAST(:member AS uuid),org_id,CAST(:peer AS uuid),id,:generation,'peer','reserved' "
            "FROM clusters WHERE workspace_id=CAST(:owner AS uuid)",
            {
                "member": member,
                "peer": peer,
                "owner": runtime.target.workspace_id,
                "generation": "a" * 64,
            },
        )
    with pytest.raises(BootstrapRefused, match="membership-scoped retirement"):
        load(runtime)


@pytest.mark.parametrize("change", ["action", "expiry", "org"])
def test_invalid_authority_cannot_load_retirement(runtime, change):
    assert runtime.run().ready
    binding = replace(runtime.binding, action="teardown")
    if change == "action":
        binding = runtime.binding
    elif change == "expiry":
        binding = replace(binding, expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    else:
        binding = replace(
            binding, principal=replace(binding.principal, org_id="foreign")
        )
    with pytest.raises(BootstrapRefused):
        load(runtime, binding=binding)


@pytest.mark.parametrize(
    "change",
    [
        "missing-inventory",
        "partial-inventory",
        "pending",
        "foreign-rule",
        "missing-uid",
    ],
)
def test_incomplete_ownership_does_not_become_permission_to_delete(runtime, change):
    assert runtime.run().ready
    db = runtime.store.store
    rows = db.execute(
        "SELECT generation, progress_json FROM workspace_bootstrap_authority", {}
    )
    progress = json.loads(rows[0]["progress_json"])
    if change == "missing-inventory":
        progress.pop("prerequisite_inventory")
    elif change == "partial-inventory":
        progress["prerequisite_inventory"]["prerequisites"].pop()
    elif change == "pending":
        progress["phase"] = "revoking"
    elif change == "foreign-rule":
        progress["prerequisite_inventory"]["workspace_id"] = "another-workspace"
    else:
        progress["supervisor-cluster-role"]["identity"].pop("uid")
    with db.transaction():
        db.execute(
            "UPDATE workspace_bootstrap_authority SET progress_json=:progress WHERE generation=:generation",
            {"progress": json.dumps(progress), "generation": rows[0]["generation"]},
        )
    with pytest.raises(BootstrapRefused):
        load(runtime)


def test_byoc_and_preexisting_namespace_are_preserved_even_with_owner_label(runtime):
    from superplane_bootstrap.components import _namespace_labels
    from superplane_bootstrap.access import ObservedNamespace

    target = replace(runtime.target, cluster_ownership="adopted")
    clients = replace(runtime.clients, target=target)
    runtime.factory.resolve_clients = lambda *_: clients
    runtime.cloud.target = target
    namespace = runtime.factory.release.namespace
    labels = _namespace_labels(target, runtime.factory.release.enforce_version)
    runtime.cluster.namespaces[namespace] = ObservedNamespace(
        namespace, "preexisting", labels
    )
    runtime.cloud.objects[("Namespace", None, namespace)] = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "uid": "preexisting",
            "resourceVersion": "1",
            "labels": labels,
        },
    }
    result = runtime.run(cluster_ownership="adopted")
    assert result.ready, repr(result.refusal)
    inventory = load(runtime)
    assert inventory.preserve_cluster
    assert not inventory.remove_namespace
    assert inventory.namespace_uid == "preexisting"


def test_retried_bootstrap_adopts_one_supervisor_without_duplicate_retirement(
    runtime, monkeypatch
):
    from superplane_bootstrap.registry import SqlRegistrationStore

    original = SqlRegistrationStore.finalize
    monkeypatch.setattr(
        SqlRegistrationStore,
        "finalize",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("unavailable")),
    )
    result = runtime.run()
    assert result.refusal and result.reservation_released
    monkeypatch.setattr(SqlRegistrationStore, "finalize", original)
    assert runtime.run().ready
    inventory = load(runtime)
    assert len(inventory.grants) == 7
    assert len(inventory.prerequisites) == 3
    assert inventory.remove_namespace


@pytest.mark.parametrize("change", ["namespace", "endpoint", "metadata"])
def test_canonical_drift_cannot_delete_the_previous_registered_target(runtime, change):
    assert runtime.run().ready
    db = runtime.store.store
    query = {
        "namespace": "UPDATE workspaces SET namespace_name='successor'",
        "endpoint": "UPDATE clusters SET endpoint='https://successor.invalid'",
        "metadata": "UPDATE clusters SET actual_state_json='{}'::jsonb",
    }[change]
    with db.transaction():
        db.execute(query, {})
    with pytest.raises(BootstrapRefused, match="canonical"):
        load(runtime)


def test_new_dedicated_cleanup_capability_binds_original_allocation(runtime):
    runtime.factory.original_allocation_id = "original-allocation"
    assert runtime.run().ready
    inventory = load(runtime)
    capability = retained_cleanup_capability(
        inventory,
        original_allocation_id="original-allocation",
        release=runtime.factory.release,
        principals=runtime.clients.principals,
        controller_mode="legacy",
        kubernetes=KubeGrants(runtime.clients.supervisor_kubernetes, runtime.target),
    )
    assert len(capability.grants) == 6
    assert capability.group.endswith(":cleanup")
    assert capability.original_allocation_id == "original-allocation"
    assert all(
        capability.group not in entry["kubernetesGroups"]
        for entry in runtime.cloud.entries.values()
    )


@pytest.mark.parametrize(
    "changed",
    [
        "allocation",
        "role",
        "uid",
        "digest",
        "rules",
        "generation",
        "adopted",
        "duplicate",
    ],
)
def test_cleanup_capability_refuses_missing_or_changed_bootstrap_evidence(
    runtime, changed
):
    runtime.factory.original_allocation_id = "original-allocation"
    assert runtime.run().ready
    inventory = load(runtime)
    if changed == "adopted":
        inventory = replace(inventory, cluster_ownership="adopted")
    elif changed != "allocation":
        grant = next(
            item
            for item in inventory.grants
            if item.spec.get("key") == "cleanup-cluster-role"
        )
        spec, identity = dict(grant.spec), dict(grant.identity)
        if changed == "role":
            grants = tuple(item for item in inventory.grants if item is not grant)
        elif changed == "duplicate":
            grants = (*inventory.grants, grant)
        else:
            if changed in {"uid", "digest", "generation"}:
                identity[changed] = "substituted"
            else:
                from copy import deepcopy

                spec["body"] = deepcopy(spec["body"])
                spec["body"]["rules"][0]["verbs"].append("create")
            grants = tuple(
                replace(grant, spec=spec, identity=identity) if item is grant else item
                for item in inventory.grants
            )
        inventory = replace(inventory, grants=grants)
    with pytest.raises(BootstrapRefused, match="original|cleanup"):
        retained_cleanup_capability(
            inventory,
            original_allocation_id=(
                "another-allocation"
                if changed == "allocation"
                else "original-allocation"
            ),
            release=runtime.factory.release,
            principals=runtime.clients.principals,
            controller_mode="legacy",
            kubernetes=KubeGrants(
                runtime.clients.supervisor_kubernetes, runtime.target
            ),
        )


def test_adopted_cleanup_grant_phase_cannot_authorize_removal(runtime):
    runtime.factory.original_allocation_id = "original-allocation"
    assert runtime.run().ready
    db = runtime.store.store
    rows = db.execute(
        "SELECT generation,progress_json FROM workspace_bootstrap_authority", {}
    )
    progress = json.loads(rows[0]["progress_json"])
    progress["cleanup-cluster-role"]["phase"] = "adopted"
    with db.transaction():
        db.execute(
            "UPDATE workspace_bootstrap_authority SET progress_json=:progress WHERE generation=:generation",
            {"progress": json.dumps(progress), "generation": rows[0]["generation"]},
        )
    with pytest.raises(BootstrapRefused, match="not created by this bootstrap"):
        load(runtime)


@pytest.mark.parametrize("changed", ["uid", "rules", "subjects"])
def test_live_cleanup_grant_drift_refuses_activation(runtime, changed):
    runtime.factory.original_allocation_id = "original-allocation"
    assert runtime.run().ready
    inventory = load(runtime)
    kind = "ClusterRoleBinding" if changed == "subjects" else "ClusterRole"
    body = next(
        resource
        for (resource_kind, _, name), resource in runtime.cloud.objects.items()
        if resource_kind == kind and name.endswith("cleanup-cluster")
    )
    if changed == "uid":
        body["metadata"]["uid"] = "replacement-uid"
    elif changed == "rules":
        body["rules"][0]["verbs"].append("create")
    else:
        body["subjects"][0]["name"] = "another-group"
    with pytest.raises(BootstrapRefused, match="live UID or body"):
        retained_cleanup_capability(
            inventory,
            original_allocation_id="original-allocation",
            release=runtime.factory.release,
            principals=runtime.clients.principals,
            controller_mode="legacy",
            kubernetes=KubeGrants(
                runtime.clients.supervisor_kubernetes, runtime.target
            ),
        )


def test_control_activation_refuses_existing_cleanup_mapping(runtime):
    runtime.factory.original_allocation_id = "original-allocation"
    assert runtime.run().ready
    capability = retained_cleanup_capability(
        load(runtime),
        original_allocation_id="original-allocation",
        release=runtime.factory.release,
        principals=runtime.clients.principals,
        controller_mode="legacy",
        kubernetes=KubeGrants(runtime.clients.supervisor_kubernetes, runtime.target),
    )
    eks = EksGrants(
        runtime.cloud, runtime.target, entry_client=runtime.cloud.entry_client
    )
    require_dormant_cleanup_group(capability, eks)
    with pytest.raises(BootstrapRefused, match="outside the original cluster"):
        require_dormant_cleanup_group(replace(capability, grants=()), eks)
    with pytest.raises(BootstrapRefused, match="outside the original cluster"):
        require_dormant_cleanup_group(replace(capability, workspace_id="other"), eks)
    principal = f"arn:aws:iam::{runtime.target.account_id}:role/unattributed"
    runtime.cloud.entries[principal] = {
        "principalArn": principal,
        "kubernetesGroups": [capability.group],
    }
    before = list(runtime.cloud.events)
    with pytest.raises(BootstrapRefused, match="unapproved EKS mapping"):
        require_dormant_cleanup_group(capability, eks)
    assert runtime.cloud.events == before
