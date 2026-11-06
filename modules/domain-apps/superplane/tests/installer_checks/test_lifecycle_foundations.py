"""Owned policy/state preparation refuses drift and verifies durable binding."""

import copy
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from installation import lifecycle_foundations as owner
from installation.config import LABEL, Refusal, identity
from .test_paid_worker import lifecycle_config, native
from workspace_provisioning.tests.test_lifecycle_policy import runtime_config

__all__ = ["native"]


@pytest.fixture
def setup(native):
    env, lock = native
    lifecycle_config(env)
    env["api_adapters"]["dispatcher"]["operation_database_secret_ref"] = {
        "name": "superplane-operation-api-db",
        "key": "dsn",
    }
    env["paid_worker"].update(max_replica_count=1, active_deadline_seconds=3600)
    policy = {
        "adp_org_id": env["adp_org_id"],
        "aws_organization_id": "o-fixture1234",
        "management_account_id": env["account_id"],
        "management_cluster": env["cluster"],
        "permitted_modes": ["managed"],
        "permitted_target_accounts": [env["account_id"]],
        "permitted_regions": [env["region"]],
        "isolation_modes": ["dedicated"],
        "workspace_defaults": {},
        "operation_max_runtime_seconds": 3600,
        "runtime": runtime_config(),
        "credential_references": {
            env["account_id"]: {
                "credential_id": "reviewed-connection",
                "credential_service": "aws",
                "credential_label": "reviewed",
            }
        },
    }
    raw = json.dumps({"version": 1, "tenants": {env["org_id"]: policy}})
    env["paid_worker"]["lifecycle_policy_sha256"] = hashlib.sha256(
        raw.encode()
    ).hexdigest()
    env[owner.KEY] = {
        "policy_json": raw,
        "state": {"storage_class": "reviewed-retained-ebs", "capacity": "10Gi"},
    }
    return env, lock


class Cluster:
    def __init__(self, env, lock):
        self.env, self.lock, self.owner = env, lock, identity(env)
        self.run_id = "run-fixture"
        self.receipt = {"objects": []}
        self.objects, self.writes = {}, []
        self.save = Mock()
        self.sc = {
            "metadata": {
                "name": env[owner.KEY]["state"]["storage_class"],
                "uid": "sc-1",
                "resourceVersion": "1",
            },
            "reclaimPolicy": "Retain",
            "volumeBindingMode": "WaitForFirstConsumer",
            "provisioner": "ebs.csi.eks.amazonaws.com",
        }
        self.pv = None

    @staticmethod
    def key(doc):
        return doc["kind"], doc["metadata"]["name"]

    def existing(self, doc):
        return copy.deepcopy(self.objects.get(self.key(doc)))

    @staticmethod
    def json(value):
        return json.loads(value.stdout)

    def put(self, doc):
        current = copy.deepcopy(doc)
        current["metadata"].update(uid="uid-" + doc["kind"], resourceVersion="1")
        self.objects[self.key(doc)] = current
        return current

    def kube(self, *args, data=None):
        if args[:2] == ("get", "storageclass"):
            value = self.sc
        elif args[:2] == ("get", "pv"):
            value = self.pv
        elif args[0] == "create":
            doc = yaml.safe_load(data)
            assert self.key(doc) not in self.objects
            self.writes.append(copy.deepcopy(doc))
            value = self.put(doc)
        else:
            raise AssertionError(args)
        return SimpleNamespace(stdout=json.dumps(value))

    def apply(self, docs):
        for doc in docs:
            self.writes.append(copy.deepcopy(doc))
            current = self.put(doc)
            self.receipt["objects"].append(
                {
                    "kind": doc["kind"],
                    "name": doc["metadata"]["name"],
                    "namespace": doc["metadata"]["namespace"],
                    "uid": current["metadata"]["uid"],
                }
            )

    def wait_job(self, job):
        pvc = next(
            v
            for (kind, _), v in self.objects.items()
            if kind == "PersistentVolumeClaim"
        )
        pvc["spec"]["volumeName"] = "pv-fixture"
        pvc["status"] = {"phase": "Bound"}
        self.pv = {
            "status": {"phase": "Bound"},
            "metadata": {"name": "pv-fixture", "uid": "pv-1", "resourceVersion": "1"},
            "spec": {
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": self.env[owner.KEY]["state"]["storage_class"],
                "volumeMode": "Filesystem",
                "accessModes": ["ReadWriteOnce"],
                "capacity": {"storage": self.env[owner.KEY]["state"]["capacity"]},
                "csi": {
                    "driver": "ebs.csi.eks.amazonaws.com",
                    "volumeHandle": "vol-12345678",
                },
                "claimRef": {
                    "uid": pvc["metadata"]["uid"],
                    "name": pvc["metadata"]["name"],
                    "namespace": self.env["namespace"],
                },
            },
        }


def test_owned_create_and_replay_are_idempotent_and_pin_live_storage(setup):
    cluster = Cluster(*setup)
    owner.prepare(cluster)
    assert [d["kind"] for d in cluster.writes] == [
        "ConfigMap",
        "PersistentVolumeClaim",
        "ServiceAccount",
        "Job",
    ]
    assert cluster.receipt[owner.KEY]["PersistentVolume"]["uid"] == "pv-1"
    assert cluster.receipt[owner.KEY]["retained"] is True
    assert {value["kind"] for value in cluster.receipt["objects"]} == {
        "Job",
        "ServiceAccount",
    }  # state is never cleanup workload inventory
    owner.prepare(cluster)
    assert len(cluster.writes) == 4
    job = cluster.writes[-1]
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["image"].endswith("@sha256:" + "9" * 64)
    assert pod["nodeSelector"] == cluster.env["paid_worker"]["node_selector"]


def test_binding_stage_needs_no_pv_read_but_normal_preparation_still_does(setup):
    cluster = Cluster(*setup)
    original = cluster.kube

    def no_pv_read(*args, **kwargs):
        if args[:2] == ("get", "pv"):
            raise Refusal("PV read has not been provisioned")
        return original(*args, **kwargs)

    cluster.kube = no_pv_read
    owner.prepare(cluster, verify_volume=False)
    evidence = cluster.receipt[owner.KEY]
    assert evidence["volume_read_request"] == {
        "namespace": cluster.env["namespace"],
        "name": cluster.env["paid_worker"]["lifecycle_state_claim"],
        "uid": "uid-PersistentVolumeClaim",
        "volume_name": "pv-fixture",
        "storage_class": "reviewed-retained-ebs",
    }
    assert "retained" not in evidence and "PersistentVolume" not in evidence
    owner.prepare(cluster, verify_volume=False)
    assert len(cluster.writes) == 4
    with pytest.raises(Refusal, match="PV read"):
        owner.prepare(cluster)
    cluster.kube = original
    owner.prepare(cluster)
    assert evidence["retained"] is True
    assert evidence["PersistentVolume"]["uid"] == "pv-1"
    assert len(cluster.writes) == 4


@pytest.mark.parametrize("fault", ["foreign", "replaced", "unbound", "missing-volume"])
def test_binding_stage_does_not_publish_an_untrusted_volume_request(setup, fault):
    cluster = Cluster(*setup)
    owner.prepare(cluster, verify_volume=False)
    pvc = cluster.objects[
        ("PersistentVolumeClaim", cluster.env["paid_worker"]["lifecycle_state_claim"])
    ]
    if fault == "foreign":
        pvc["metadata"]["labels"][LABEL] = "another-installation"
    elif fault == "replaced":
        pvc["metadata"]["uid"] = "replacement"
    elif fault == "unbound":
        pvc["status"]["phase"] = "Pending"
    else:
        del pvc["spec"]["volumeName"]
    with pytest.raises(Refusal):
        owner.volume_read_request(cluster)


def test_state_binding_execution_stops_before_migration_rollout_and_activation(
    monkeypatch,
):
    from installation import lifecycle_worker
    from installation.runner import Installer

    calls = []
    prepare = Mock()
    monkeypatch.setattr(owner, "prepare", prepare)
    monkeypatch.setattr(
        lifecycle_worker, "quiescence_job", lambda _: calls.append("quiescence-probe")
    )
    installer = SimpleNamespace(
        env={
            "lifecycle_foundations": {"state": {}},
            "paid_worker": {"mode": "native-lifecycle"},
            "timeout_seconds": 60,
        },
        receipt={"plan_sha256": "reviewed"},
        directory=Path("/private/plan"),
        preflight=lambda: calls.append("preflight"),
        exclusive=nullcontext,
        disable_route=lambda: calls.append("route-disabled"),
        foundations=lambda: calls.append("namespace-foundations"),
        commands=SimpleNamespace(call=lambda *a, **k: calls.append("terraform")),
        save=Mock(),
    )
    installer.phase = lambda name, action: (calls.append(name), action())[-1]
    Installer.execute(installer, "reviewed", "real-token", prepare_lifecycle_state=True)
    prepare.assert_called_once_with(installer, verify_volume=False)
    assert installer.receipt["status"] == "awaiting-volume-read"
    assert calls.index("quiescence-probe") < calls.index("lifecycle-state-binding")
    assert calls[-1] == "lifecycle-state-binding"
    assert not set(calls) & {
        "migration",
        "rollout",
        "adapter-activation",
        "public-route",
    }


def test_state_binding_flag_cannot_be_used_without_native_foundations():
    from installation.runner import Installer

    installer = SimpleNamespace(env={}, preflight=Mock())
    with pytest.raises(Refusal, match="configured native"):
        Installer.execute(
            installer, "reviewed", "real-token", prepare_lifecycle_state=True
        )
    installer.preflight.assert_not_called()


def test_waiting_for_volume_read_resumes_same_installation(setup, tmp_path):
    from installation.runner import Installer

    installer = Installer(*setup, tmp_path)
    previous = copy.deepcopy(installer.receipt)
    previous["status"] = "awaiting-volume-read"
    installer.resume(previous)
    assert installer.run_id == previous["run_id"]
    changed = copy.deepcopy(previous)
    changed["environment"]["namespace"] = "another-app"
    with pytest.raises(Refusal, match="exact original"):
        installer.resume(changed)


@pytest.mark.parametrize(
    "fault",
    [
        "foreign",
        "policy",
        "mutable",
        "extra-data",
        "pvc-spec",
        "pvc-source",
        "terminating",
    ],
)
def test_all_existing_dependencies_checked_before_first_mutation(setup, fault):
    cluster = Cluster(*setup)
    policy, pvc = owner.documents(cluster.env)
    doc = pvc if fault.startswith("pvc") else policy
    current = cluster.put(doc)
    if fault == "foreign":
        current["metadata"]["labels"][LABEL] = "foreign"
    if fault == "policy":
        current["data"]["lifecycle.json"] += " "
    if fault == "mutable":
        current["immutable"] = False
    if fault == "extra-data":
        current["data"]["unreviewed"] = "unexpected"
    if fault == "pvc-spec":
        current["spec"]["accessModes"] = ["ReadWriteMany"]
    if fault == "pvc-source":
        current["spec"]["dataSource"] = {"name": "foreign"}
    if fault == "terminating":
        current["metadata"]["deletionTimestamp"] = "now"
    with pytest.raises(Refusal):
        owner.prepare(cluster)
    assert cluster.writes == []


@pytest.mark.parametrize(
    "fault",
    [
        "claim-replaced",
        "policy-replaced",
        "volume-replaced",
        "class-replaced",
        "class-drift",
        "claim-unbound",
        "volume-foreign",
        "volume-delete",
        "missing",
    ],
)
def test_resume_and_activation_refuse_replaced_or_unsafe_storage(setup, fault):
    cluster = Cluster(*setup)
    owner.prepare(cluster)
    pvc_key = (
        "PersistentVolumeClaim",
        cluster.env["paid_worker"]["lifecycle_state_claim"],
    )
    policy_key = ("ConfigMap", cluster.env["paid_worker"]["lifecycle_policy_configmap"])
    if fault == "claim-replaced":
        cluster.objects[pvc_key]["metadata"]["uid"] = "replacement"
    if fault == "policy-replaced":
        cluster.objects[policy_key]["metadata"]["uid"] = "replacement"
    if fault == "volume-replaced":
        cluster.pv["metadata"]["uid"] = "replacement"
    if fault == "class-replaced":
        cluster.sc["metadata"]["uid"] = "replacement"
    if fault == "class-drift":
        cluster.sc["metadata"]["resourceVersion"] = "2"
    if fault == "claim-unbound":
        cluster.objects[pvc_key]["status"]["phase"] = "Pending"
    if fault == "volume-foreign":
        cluster.pv["spec"]["claimRef"]["uid"] = "foreign"
    if fault == "volume-delete":
        cluster.pv["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
    if fault == "missing":
        del cluster.objects[pvc_key]
    with pytest.raises(Refusal):
        owner.snapshot(cluster)


def test_default_delete_storageclass_never_creates_resources(setup):
    cluster = Cluster(*setup)
    cluster.sc["reclaimPolicy"] = "Delete"
    with pytest.raises(Refusal):
        owner.prepare(cluster)
    assert cluster.writes == []


@pytest.mark.parametrize(
    "fault",
    ["org", "adp-org", "cluster", "digest", "short", "concurrency", "extra-tenant"],
)
def test_input_policy_cannot_rebind_domain_or_shorten_session_budget(setup, fault):
    env, _ = setup
    document = json.loads(env[owner.KEY]["policy_json"])
    policy = document["tenants"][env["org_id"]]
    if fault == "org":
        document["tenants"] = {"foreign": policy}
    if fault == "adp-org":
        policy["adp_org_id"] = "personal"
    if fault == "cluster":
        policy["management_cluster"] = "foreign"
    if fault == "short":
        policy["operation_max_runtime_seconds"] = 900
    if fault == "extra-tenant":
        document["tenants"]["foreign"] = policy
    if fault == "concurrency":
        env["paid_worker"]["max_replica_count"] = 2
    raw = json.dumps(document)
    env[owner.KEY]["policy_json"] = raw
    env["paid_worker"]["lifecycle_policy_sha256"] = (
        "a" * 64 if fault == "digest" else hashlib.sha256(raw.encode()).hexdigest()
    )
    with pytest.raises(Refusal):
        owner.validate(env)


def test_api_and_worker_share_reviewed_policy_mount(setup):
    from installation.manifests import render

    env, lock = setup
    docs = render(env, lock)
    api = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
    )
    template = api["spec"]["template"]
    pod = template["spec"]
    container = pod["containers"][0]
    assert {
        "name": "SUPERPLANE_LIFECYCLE_CONFIG_FILE",
        "value": "/run/lifecycle-policy/lifecycle.json",
    } in container["env"]
    mount = next(v for v in pod["volumes"] if v["name"] == "lifecycle-policy")
    assert (
        mount["configMap"]["name"] == env["paid_worker"]["lifecycle_policy_configmap"]
    )
    assert (
        template["metadata"]["annotations"]["adp.aws-e.io/lifecycle-policy-sha256"]
        == env["paid_worker"]["lifecycle_policy_sha256"]
    )
    assert len([d for d in docs if owner.managed(env, d)]) == 2


def test_probe_retry_accepts_kubernetes_defaults_but_refuses_injected_authority(setup):
    cluster = Cluster(*setup)
    owner.prepare(cluster)
    job = next(value for (kind, _), value in cluster.objects.items() if kind == "Job")
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["imagePullPolicy"] = "IfNotPresent"
    container["terminationMessagePath"] = "/dev/termination-log"
    owner.prepare(cluster)
    container["env"] = [{"name": "AWS_ROLE_ARN", "value": "unreviewed"}]
    with pytest.raises(Refusal, match="probe differs"):
        owner.prepare(cluster)


def test_probe_service_account_cannot_carry_ambient_aws_identity(setup):
    cluster = Cluster(*setup)
    owner.prepare(cluster)
    account = cluster.objects[("ServiceAccount", "superplane-state-probe")]
    account["metadata"]["annotations"] = {"eks.amazonaws.com/role-arn": "unreviewed"}
    with pytest.raises(Refusal, match="unexpected authority"):
        owner.prepare(cluster)


def test_lock_recovery_waits_for_existing_storage_probe(setup):
    from installation.runner import Installer

    env, lock = setup
    installer = SimpleNamespace(
        env=env,
        lock=lock,
        owner=identity(env),
        run_id="0123456789abcdef",
        bucket="owned-state",
        lock_key="owned-lock",
        target=Mock(),
        save=Mock(),
        release_lock=Mock(),
        terminal=Installer.terminal,
        receipt={
            "remote_lock": {
                "bucket": "owned-state",
                "key": "owned-lock",
                "etag": "exact",
            },
            owner.KEY: {"PersistentVolumeClaim": {"uid": "claim-1"}},
        },
    )
    probe = owner.probe_job(installer, "claim-1")
    installer.existing = (
        lambda doc: probe
        if doc["metadata"]["name"] == probe["metadata"]["name"]
        else None
    )
    with pytest.raises(Refusal, match="nonterminal"):
        Installer.recover_lock(installer, installer.run_id)
    installer.release_lock.assert_not_called()


def test_scoped_policy_retains_default_and_validates_worker_deadline(setup):
    env, _ = setup
    document = json.loads(env[owner.KEY]["policy_json"])
    original = copy.deepcopy(document["tenants"][env["org_id"]])
    profile = copy.deepcopy(original)
    profile["permitted_regions"] = ["us-west-2"]
    document["workspace_policies"] = {
        env["org_id"]: {"00000000-0000-5000-8000-000000000001": profile}
    }

    def save():
        raw = json.dumps(document)
        env[owner.KEY]["policy_json"] = raw
        env["paid_worker"]["lifecycle_policy_sha256"] = hashlib.sha256(
            raw.encode()
        ).hexdigest()

    save()
    owner.validate(env)
    assert document["tenants"][env["org_id"]] == original
    profile["operation_max_runtime_seconds"] = 7200
    save()
    with pytest.raises(Refusal, match="deadline"):
        owner.validate(env)


def test_recovery_observation_projects_only_exact_api_role(setup):
    from installation.lifecycle_observation import validate, role_arn
    from installation.lifecycle_worker import project_api

    env, lock = setup
    env["api_producer_role"] = {"api_id": "abcdefghij", "stage": "dev"}
    env["recovery_transport"] = {
        "tls_secret": "configured",
        "credential_secret": "configured",
    }
    env["lifecycle_observation"] = {"regions": ["us-west-2"]}
    validate(env)
    docs = [
        {
            "kind": "Deployment",
            "metadata": {"name": "superplane-api", "labels": {}},
            "spec": {"template": {"spec": {"containers": [{"env": []}]}}},
        }
    ]
    project_api(env, lock, docs)
    values = docs[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {
        "name": "SUPERPLANE_RECOVERY_OBSERVATION_ROLE_ARN",
        "value": role_arn(env),
    } in values
    for regions in [[], ["*"], ["us-west-2", "us-west-2"], "us-west-2"]:
        env["lifecycle_observation"]["regions"] = regions
        with pytest.raises(Refusal):
            validate(env)
