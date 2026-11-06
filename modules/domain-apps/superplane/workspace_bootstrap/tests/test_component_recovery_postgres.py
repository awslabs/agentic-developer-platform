"""Multi-delete revocation uses native SQL locks and preserves component objects."""

import copy
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from harness_jobs import settled_bootstrap_repeat
from superplane_bootstrap.component_journal import (
    ANNOTATION,
    component_identity,
    component_key,
)
from superplane_bootstrap.component_recovery import KIND, recover_components
from superplane_bootstrap.denied_grant import digest
from superplane_bootstrap.errors import BootstrapRefused

from installation.bootstrap_components_repeat import cleanup_revalidator

from .test_acquisition_recovery_postgres import acquisition  # noqa: F401
from .test_authority_journal_postgres import authority  # noqa: F401
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401


@pytest.fixture
def component_source(acquisition, monkeypatch):  # noqa: F811 -- Imported PostgreSQL fixture.
    journal, registry, backend, resources, _, _ = acquisition
    plan, progress = journal.read()
    progress["phase"] = "revoking"
    # Mirror a native cleanup interrupted midway: some grants are already gone.
    for grant in plan["grants"][-14:]:
        progress[grant["key"]]["phase"] = "revoked"
        resources.pop(grant["key"], None)
    ns = plan["grants"][2]["body"]["metadata"]["name"]
    objects, components = {}, {}
    for kind, namespace, name in (
        ("ServiceAccount", ns, "superplane-controller"),
        ("Role", ns, "superplane-controller-workspace"),
        ("RoleBinding", ns, "superplane-controller-workspace"),
        ("ClusterRole", "", "superplane-controller-" + ns + "-cluster"),
        ("ClusterRoleBinding", "", "superplane-controller-" + ns + "-cluster"),
    ):
        desired = {
            "kind": kind,
            "apiVersion": "v1"
            if kind == "ServiceAccount"
            else "rbac.authorization.k8s.io/v1",
            "metadata": {
                "name": name,
                **({"namespace": namespace} if namespace else {}),
            },
        }
        body = copy.deepcopy(desired)
        body["metadata"].update(uid=kind + "-uid", annotations={ANNOTATION: "a" * 32})
        key = component_key(body)
        objects[key] = body
        components[key] = {
            "desired": desired,
            "phase": "owned",
            "creation": "a" * 32,
            "identity": component_identity(body),
        }
    progress.update(
        components=components,
        component_inventory_complete=True,
        component_inventory_mode="management",
        management_observation={},
        prerequisite_inventory={},
        system_workload_baseline={},
    )
    with journal.fenced(recovery=True):
        journal.write_locked(progress)
    prior = {
        "retained_progress": {
            "components": copy.deepcopy(components),
            "workspace-namespace": copy.deepcopy(progress["workspace-namespace"]),
        }
    }
    # The companion Harness suite validates two real immutable closure chains.
    # This suite isolates native SQL-fenced cleanup and its maintained revalidator.
    closure = {"kind": "tested-by-harness-companion"}
    monkeypatch.setattr(settled_bootstrap_repeat, "validate", lambda value: value)
    monkeypatch.setattr(
        settled_bootstrap_repeat, "component_anchor", lambda value: prior
    )
    monkeypatch.setattr(
        settled_bootstrap_repeat, "journals_bound", lambda value, rows: rows[0]
    )
    qualification = {
        "evidence_kind": KIND,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "operation_id": journal.binding.operation_id,
        "org_id": journal.target.org_id,
        "workspace_id": journal.target.workspace_id,
        "cluster_arn": journal.target.cluster_arn,
        "generation": journal.generation,
        "claim": journal.claim,
        "plan_sha256": digest(plan),
        "revoking_progress_sha256": digest(progress),
        "retained_plan": plan,
        "retained_progress": progress,
        "settled_bootstrap": closure,
    }
    descriptor = {
        "operation_id": journal.binding.operation_id,
        "source_principal": "source#1",
        "source_key": "source-key",
        "source_fence": 6,
        "settled_bootstrap": closure,
    }
    expiry = {
        **descriptor,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "earliest_owner_evidence_at": (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat(),
    }

    def read_snapshot():
        return {
            "journals": [
                dict(
                    journal.store.execute(
                        "SELECT * FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id AND generation=:generation",
                        journal.key,
                    )[0]
                )
            ]
        }

    return (
        journal,
        registry,
        backend,
        resources,
        objects,
        qualification,
        descriptor,
        expiry,
        read_snapshot,
    )


@pytest.mark.parametrize("acquisition", [(29, "revoke_intent", True)], indirect=True)
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "lost-delete-ack",
        "completed-before-release",
        "component-uid",
        "unrecorded-component-uid",
        "namespace-uid",
        "live-session",
        "live-grant",
        "wrong-generation",
        "retain-marker",
        "extra-component",
        "revoked-regression",
        "changed-progress-identity",
        "residual",
        "batch-revoked",
    ],
)
def test_partial_component_cleanup_retains_native_claim_on_any_uncertainty(
    component_source, fault, partial, batched
):
    if fault == "batch-revoked" and not batched:
        pytest.skip("batch-specific refusal")
    (
        journal,
        registry,
        backend,
        resources,
        objects,
        qualification,
        descriptor,
        expiry,
        read_snapshot,
    ) = component_source
    if partial:
        _, progress = journal.read()
        qualification["inherited_components"] = copy.deepcopy(progress["components"])
        progress["components"] = dict(list(progress["components"].items())[:2])
        for key in (
            "component_inventory_complete",
            "component_inventory_mode",
            "management_observation",
            "system_workload_baseline",
        ):
            progress.pop(key)
        with journal.fenced(recovery=True):
            journal.write_locked(progress)
        qualification.update(
            retained_progress=progress, revoking_progress_sha256=digest(progress)
        )
    before_objects = copy.deepcopy(objects)
    original_namespace = copy.deepcopy(resources["workspace-namespace"])
    before_plan, before_progress = journal.read()
    if fault == "component-uid":
        next(iter(objects.values()))["metadata"]["uid"] = "foreign"
    elif fault == "unrecorded-component-uid":
        list(objects.values())[-1]["metadata"]["uid"] = "foreign-unrecorded"
    elif fault == "namespace-uid":
        resources["workspace-namespace"]["uid"] = "foreign"
    elif fault == "live-session":
        expiry["issued_sessions_expired"] = False
    elif fault == "live-grant":
        expiry["original_authority_expired"] = False
    elif fault == "completed-before-release":
        with journal.fenced(recovery=True):
            _, progress = journal.read_locked()
            for grant in before_plan["grants"]:
                if grant["key"] != "workspace-namespace":
                    progress[grant["key"]]["phase"] = "revoked"
                    resources.pop(grant["key"], None)
            progress.update(phase="revoked", complete=True)
            journal.write_locked(progress)
        qualification.pop("revoking_progress_sha256")
        qualification.update(
            retained_progress=progress, revoked_progress_sha256=digest(progress)
        )
    elif fault == "wrong-generation":
        qualification["generation"] = "foreign"
    elif fault in {
        "retain-marker",
        "extra-component",
        "revoked-regression",
        "changed-progress-identity",
    }:
        with journal.fenced(recovery=True):
            _, progress = journal.read_locked()
            if fault == "retain-marker":
                progress["retain_workspace"] = False
            elif fault == "extra-component":
                progress["components"]["foreign"] = {}
            elif fault == "revoked-regression":
                progress[before_plan["grants"][-1]["key"]]["phase"] = "revoke_intent"
            else:
                progress[before_plan["grants"][-1]["key"]]["identity"]["uid"] = (
                    "foreign"
                )
            journal.write_locked(progress)
    elif fault == "residual":
        backend.residual = True
    elif fault == "lost-delete-ack":
        # The recorded revoke intent outlived the provider's successful delete.
        key = before_plan["grants"][4]["key"]
        resources.pop(key)

    batches, active = [], []
    if batched:
        observe, delete = backend.observe, backend.delete

        @contextmanager
        def read_batch(bodies):
            assert not active
            active.extend(bodies)
            batches.append(copy.deepcopy(bodies))
            try:
                yield
            finally:
                active.clear()
                if fault == "batch-revoked" and len(batches) == 2:
                    raise BootstrapRefused("read batch authority revoked")

        def scoped_observe(spec):
            if active:
                assert spec["kind"] == "kubernetes" and spec["body"] in active
            return observe(spec)

        def fenced_delete(spec, identity):
            assert not active, "a read batch must never authorize deletion"
            return delete(spec, identity)

        backend.read_batch = read_batch
        backend.observe = scoped_observe
        backend.delete = fenced_delete

    def read_component(desired):
        if batched:
            assert desired in active
        return copy.deepcopy(objects.get(component_key(desired)))

    def run():
        revalidate = cleanup_revalidator(
            qualification,
            descriptor,
            read_snapshot=read_snapshot,
            read_expiry=lambda: copy.deepcopy(expiry),
        )
        return recover_components(
            journal,
            registry,
            backend,
            backend,
            revalidate=revalidate,
            verify_absence=lambda _: set(resources) == {"workspace-namespace"},
            read_component=read_component,
        )

    if fault not in {None, "lost-delete-ack", "completed-before-release"}:
        with pytest.raises((BootstrapRefused, ValueError)):
            run()
        assert journal.store.execute(
            "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
            journal.key,
        )
        return
    result = run()
    assert (
        result["components_retained"]
        and result["namespace_retained"]
        and result["claim_released"]
    )
    assert (
        not backend.deleted
        if fault == "completed-before-release"
        else len(backend.deleted) >= 12
    )
    assert objects == before_objects
    assert resources == {"workspace-namespace": original_namespace}
    stored = read_snapshot()["journals"][0]
    plan, progress = (
        json.loads(stored["plan_json"]),
        json.loads(stored["progress_json"]),
    )
    assert (
        plan == before_plan and progress["components"] == before_progress["components"]
    )
    assert progress["phase"] == "revoked" and progress["complete"] is True
    assert "retain_workspace" not in progress
    if batched:
        # Pre-cleanup retained identity check, then two independent complete
        # absence/retained-identity passes before the claim is released.
        assert [len(batch) for batch in batches] == [6, 24, 6, 24, 6]
        assert batches[1] == batches[3] and batches[0] == batches[2] == batches[4]
        assert not active
    if partial:
        assert len(progress["components"]) == 2
        assert "component_inventory_complete" not in progress
        assert "management_observation" not in progress
        assert "system_workload_baseline" not in progress
    deleted = backend.deleted[:]
    # Replaying a consumed cleanup cannot reacquire its released reservation.
    with pytest.raises((BootstrapRefused, ValueError)):
        run()
    assert backend.deleted == deleted


@pytest.mark.parametrize("acquisition", [(29, "revoke_intent", True)], indirect=True)
def test_partial_permit_keeps_all_original_reads_and_no_component_deletes(
    component_source,
):
    from superplane_bootstrap.closed_cleanup_permit import object_scope

    qualification = copy.deepcopy(component_source[5])
    original = qualification["retained_progress"]["components"]
    qualification["inherited_components"] = copy.deepcopy(original)
    qualification["retained_progress"]["components"] = dict(list(original.items())[:2])
    reads, deletes = object_scope({"qualification": qualification})
    for record in original.values():
        body = record["desired"]
        key = (
            body["apiVersion"],
            body["kind"],
            body["metadata"].get("namespace", ""),
            body["metadata"]["name"],
        )
        assert reads[key] == body and key not in deletes
