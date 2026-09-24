"""Real bootstrap/registration/journal consumers; only cloud transports are doubled."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from superplane_bootstrap.component_journal import ANNOTATION
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.registry import SqlRegistrationStore
from workspace_bootstrap.tests.test_integration import (
    NAMESPACE,
    _FakeCluster,
    _binding,
    _run,
)
from workspace_provisioning.retirement_inventory import (
    load_bootstrap_retirement_inventory,
)


def load(db):
    return load_bootstrap_retirement_inventory(
        registration_store=SqlRegistrationStore(db),
        binding=replace(_binding(), action="teardown"),
    )


def rows(db):
    return [
        json.loads(row["progress_json"])
        for row in db.execute(
            "SELECT progress_json FROM workspace_bootstrap_authority ORDER BY generation",
            {},
        )
    ]


def test_native_bootstrap_publishes_six_exact_component_identities(database, tmp_path):
    cloud, db = _FakeCluster(), database()
    result, _ = _run(cloud, tmp_path, sql_store=db)
    assert result.ready, repr(result.refusal)
    assert (
        len(
            [
                obj
                for obj in result.installation.objects
                if obj.kind != "Namespace" and obj.uid
            ]
        )
        == 6
    )
    inventory = load(database())
    assert inventory.components_complete
    assert len(inventory.components) == 6
    assert all(component.owned for component in inventory.components)
    assert len({c.identity["uid"] for c in inventory.components}) == 6
    assert all(c.identity["creation"] for c in inventory.components)
    assert {c.desired["kind"] for c in inventory.components} == {
        "Deployment",
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "ClusterRole",
        "ClusterRoleBinding",
    }


def test_management_bootstrap_records_five_observer_objects_and_no_deployment(
    database, tmp_path
):
    cloud, db = _FakeCluster(), database()
    result, _ = _run(cloud, tmp_path, sql_store=db, management=True)
    assert result.ready, repr(result.refusal)
    assert len(result.installation.objects) > 0
    assert all(obj.kind != "Deployment" for obj in result.installation.objects)
    inventory = load(database())
    assert inventory.components_complete
    assert len(inventory.components) == 5
    progress = next(
        row for row in rows(database()) if row.get("component_inventory_complete")
    )
    assert progress["component_inventory_mode"] == "management"
    observation = progress["management_observation"]
    assert observation["workspace_id"] == result.target.workspace_id
    assert observation["cluster_arn"] == result.target.cluster_arn
    assert observation["endpoint"] == result.target.endpoint
    assert len(observation["registration_claim"]) == 64
    assert "credential" not in json.dumps(observation)
    plans = database().execute(
        "SELECT plan_json FROM workspace_bootstrap_authority", {}
    )
    rules = [
        rule
        for row in plans
        for grant in json.loads(row["plan_json"])["grants"]
        if grant["key"] in {"installer-cluster-role", "installer-namespace-role"}
        for rule in grant["body"]["rules"]
        if "apps" in rule["apiGroups"] and "deployments" in rule["resources"]
    ]
    assert rules and all(set(rule["verbs"]) <= {"get", "list"} for rule in rules)


@pytest.mark.parametrize("verb", ["create", "patch"])
def test_management_bootstrap_refuses_additive_workspace_deployment_authority(
    database, tmp_path, monkeypatch, verb
):
    from workspace_bootstrap.tests.test_authority_runtime_postgres import Cloud

    allowed = Cloud.allowed

    def additive(self, principal, **attrs):
        if (
            principal.endswith("/installer")
            and attrs.get("verb") == verb
            and attrs.get("resource") == "deployments.apps"
            and attrs.get("namespace") == NAMESPACE
        ):
            return True
        return allowed(self, principal, **attrs)

    monkeypatch.setattr(Cloud, "allowed", additive)
    cloud = _FakeCluster()
    result, _ = _run(cloud, tmp_path, sql_store=database(), management=True)
    assert not result.ready
    assert "write workspace deployments" in str(result.refusal)
    assert not cloud.components


@pytest.mark.parametrize("lost_at", range(1, 7))
def test_lost_component_reply_preserves_committed_intent_without_claiming_success(
    database, tmp_path, lost_at
):
    cloud, db, reader = _FakeCluster(), database(), database()
    original = cloud.run
    creates = []

    def run(argv, **kwargs):
        body = (
            json.loads(kwargs.get("data") or "{}")
            if (kwargs.get("data") or "").startswith("{")
            else {}
        )
        if body.get("metadata", {}).get("annotations", {}).get(ANNOTATION):
            # This independent connection cannot see an uncommitted outer write.
            records = [
                c for row in rows(reader) for c in row.get("components", {}).values()
            ]
            record = next(
                c
                for c in records
                if c.get("creation") == body["metadata"]["annotations"][ANNOTATION]
            )
            assert record["phase"] == "intended"
            creates.append(body["kind"])
            result = original(argv, **kwargs)
            if len(creates) == lost_at:
                raise ConnectionError("lost provider reply")
            return result
        return original(argv, **kwargs)

    cloud.run = run
    result, _ = _run(cloud, tmp_path, sql_store=db)
    assert not result.ready
    records = [c for row in rows(reader) for c in row.get("components", {}).values()]
    assert len(records) == lost_at
    assert sum(c["phase"] == "intended" for c in records) == 1
    assert len(cloud.components) == lost_at
    assert SqlRegistrationStore(reader).read(_binding().principal.workspace_id) is None
    cloud.run = original
    # Namespace/RBAC retries reuse the original immutable object rather than
    # issuing another create. Deployment response recovery is also required.
    retried, _ = _run(cloud, tmp_path, sql_store=database())
    assert retried.ready, repr(retried.refusal)
    inventory = load(database())
    assert inventory.components_complete and len(inventory.components) == 6
    assert len(cloud.components) == 6
    assert cloud.uid_counter == 6


def test_matching_preexisting_service_account_is_adopted_without_deletion_rights(
    database, tmp_path
):
    cloud = _FakeCluster()
    cloud.components[("ServiceAccount", NAMESPACE, "superplane-controller")] = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "namespace": NAMESPACE,
            "name": "superplane-controller",
            "uid": "foreign-uid",
            "resourceVersion": "1",
            "annotations": {ANNOTATION: "forged-owner-marker"},
        },
    }
    result, _ = _run(cloud, tmp_path, sql_store=database())
    assert result.ready, repr(result.refusal)
    components = load(database()).components
    adopted = [c for c in components if not c.owned]
    assert len(adopted) == 1 and adopted[0].identity["uid"] == "foreign-uid"
    assert not next(
        obj for obj in result.installation.objects if obj.kind == "ServiceAccount"
    ).owned
    assert cloud.uid_counter == 5


@pytest.mark.parametrize(
    "change", ["uid", "rules", "aggregation", "missing", "release"]
)
def test_retry_refuses_changed_owned_component(database, tmp_path, change):
    cloud = _FakeCluster(coredns_available=0)
    first, _ = _run(cloud, tmp_path, sql_store=database())
    assert not first.ready and len(cloud.components) == 6
    key = ("Role", NAMESPACE, "superplane-controller-workspace")
    component = cloud.components[key]
    if change == "uid":
        component["metadata"]["uid"] = "replacement"
    elif change == "rules":
        component["rules"].append(
            {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}
        )
    elif change == "aggregation":
        component["metadata"].setdefault("labels", {})[
            "rbac.authorization.k8s.io/aggregate-to-admin"
        ] = "true"
    elif change == "missing":
        del cloud.components[key]
    else:
        key = ("Deployment", NAMESPACE, "superplane-controller")
        cloud.components[key]["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "foreign/image"
        )
    expected = deepcopy(cloud.components)
    retried, _ = _run(cloud, tmp_path, sql_store=database())
    assert not retried.ready
    assert cloud.components == expected


def test_reader_refuses_claim_of_complete_component_inventory_with_an_omission(
    database, tmp_path
):
    db = database()
    assert _run(_FakeCluster(), tmp_path, sql_store=db)[0].ready
    progress = rows(db)[0]
    progress["components"].pop(next(iter(progress["components"])))
    with db.transaction():
        db.execute(
            "UPDATE workspace_bootstrap_authority SET progress_json=:progress",
            {"progress": json.dumps(progress)},
        )
    with pytest.raises(BootstrapRefused, match="component inventory"):
        load(database())


@pytest.mark.parametrize("lost_at", range(1, 7))
def test_process_crash_requires_recovery_then_reuses_owned_objects(
    database, tmp_path, monkeypatch, lost_at
):
    from superplane_bootstrap.authority_runtime import BootstrapAuthorityFactory
    from superplane_bootstrap.state import FileStateStore
    from superplane_bootstrap.workspace import recover_interrupted_bootstrap

    class Crash(BaseException):
        pass

    cloud, captured, count = _FakeCluster(), {}, 0
    create = BootstrapAuthorityFactory.create

    def capture(self, **kwargs):
        captured.update(factory=self, **kwargs)
        return create(self, **kwargs)

    monkeypatch.setattr(BootstrapAuthorityFactory, "create", capture)
    run = cloud.run

    def crash(argv, **kwargs):
        nonlocal count
        result = run(argv, **kwargs)
        body = (
            json.loads(kwargs["data"])
            if (kwargs.get("data") or "").startswith("{")
            else {}
        )
        if body.get("metadata", {}).get("annotations", {}).get(ANNOTATION):
            count += 1
            if count == lost_at:
                raise Crash()
        return result

    cloud.run = crash
    with pytest.raises(Crash):
        _run(cloud, tmp_path, sql_store=database())
    cloud.run = run
    # Fresh public invocations cannot bypass the outstanding reservation.
    state_before = (tmp_path / "state.json").read_bytes()
    blocked, _ = _run(cloud, tmp_path, sql_store=database())
    assert not blocked.ready and len(cloud.components) == lost_at
    assert (tmp_path / "state.json").read_bytes() == state_before
    target = captured["target"]
    recovered = recover_interrupted_bootstrap(
        access=cloud,
        store=SqlRegistrationStore(database()),
        state_store=FileStateStore(tmp_path / "state.json"),
        workspace_id=target.workspace_id,
        cluster_arn=target.cluster_arn,
        target=target,
        binding=captured["binding"],
        authority_factory=captured["factory"],
    )
    assert recovered.reservation_released, repr(recovered.refusal)
    result, _ = _run(cloud, tmp_path, sql_store=database())
    assert result.ready, repr(result.refusal)
    assert cloud.uid_counter == 6
    assert all(c.owned for c in load(database()).components)


def test_completed_or_revoked_component_authority_cannot_create(
    database, tmp_path, monkeypatch
):
    from superplane_bootstrap.component_journal import ComponentJournal

    captured, ensure = [], ComponentJournal.ensure

    def capture(self, body):
        captured.append((self, deepcopy(body)))
        return ensure(self, body)

    monkeypatch.setattr(ComponentJournal, "ensure", capture)
    cloud = _FakeCluster()
    assert _run(cloud, tmp_path, sql_store=database())[0].ready
    journal, body = captured[0]
    before = deepcopy(cloud.components)
    with pytest.raises(BootstrapRefused, match="stale"):
        journal.ensure(body)
    assert cloud.components == before


def test_provider_defaulting_and_status_updates_preserve_owned_identity(
    database, tmp_path
):
    cloud = _FakeCluster(coredns_available=0)
    run = cloud.run

    def defaults(argv, **kwargs):
        result = run(argv, **kwargs)
        if "create" in argv and kwargs.get("data"):
            body = json.loads(kwargs["data"])
            if body.get("kind") == "Deployment":
                key = ("Deployment", NAMESPACE, "superplane-controller")
                live = cloud.components[key]
                live["spec"]["revisionHistoryLimit"] = 10
                return cloud._ok(argv, live)
        return result

    cloud.run = defaults
    assert not _run(cloud, tmp_path, sql_store=database())[0].ready
    live = cloud.components[("Deployment", NAMESPACE, "superplane-controller")]
    live["metadata"]["resourceVersion"] = "200"
    live["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "3"
    live["status"] = {"availableReplicas": 1}
    cloud.coredns_available = 2
    result, _ = _run(cloud, tmp_path, sql_store=database())
    assert result.ready, repr(result.refusal)
    assert cloud.uid_counter == 6
