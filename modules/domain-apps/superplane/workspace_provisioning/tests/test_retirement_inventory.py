from dataclasses import replace
from datetime import UTC, datetime
import json

import pytest

from superplane_bootstrap.errors import BootstrapRefused
from workspace_provisioning.retirement_inventory import (
    load_bootstrap_retirement_inventory,
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
