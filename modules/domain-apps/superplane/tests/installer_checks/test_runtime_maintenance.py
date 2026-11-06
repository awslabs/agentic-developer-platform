"""Exercise saved plans, Kubernetes compare-and-swap and the native S3 lock."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from installation import paid_worker
from installation.config import LABEL, Refusal, digest, image
from installation.lifecycle_worker import expected_binding
from installation.runner import Installer
from installation.runtime_maintenance import (
    TARGETS,
    apply,
    observe,
    payload,
    plan,
    reviewed,
)

from .test_paid_worker import lifecycle_config, native

__all__ = ["native"]


class Server:
    def __init__(self, installer, values):
        self.installer = installer
        self.values = {
            target: copy.deepcopy(v) for target, v in zip(TARGETS, values, strict=True)
        }
        self.lock = False
        self.patches = []
        self.worker_jobs = []
        self.worker_pods = []
        self.api_dispatch = "false"
        self.race = None
        self.lose_reply = False
        self.bad_image_id = False
        self.reported_digests = {}
        self.qualified_digests = {}
        self.unowned_pods = []
        self.identity_reads = 0
        self.schema_report = {
            "database": installer.env["database"]["database"],
            "schema": installer.env["database"]["schema"],
            "revision": installer.lock["schema"]["observed"]["head"],
        }

    def pods(self):
        deployment = self.values[TARGETS[0]]
        pod = copy.deepcopy(deployment["spec"]["template"])
        pod["metadata"]["name"] = "owned-api-pod"
        pod["metadata"]["ownerReferences"] = [
            {"kind": "ReplicaSet", "uid": "owned-replica", "controller": True}
        ]
        primary = pod["spec"]["containers"][0]
        for v in primary["env"]:
            if v["name"] == "SUPERPLANE_OPERATION_DISPATCH_ENABLED":
                v["value"] = self.api_dispatch
        pod["status"] = {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": c["name"],
                    "ready": True,
                    "imageID": self.reported_digests.get(c["image"], c["image"])
                    if not self.bad_image_id
                    else "wrong@sha256:" + "0" * 64,
                }
                for c in pod["spec"]["containers"]
            ],
        }
        return [pod, *self.unowned_pods]

    def call(self, args, *, data=None, **kwargs):
        if args[0] == "aws":
            if "get-caller-identity" in args:
                self.identity_reads += 1
                selected = self.installer.env["deployment_identity"]
                role_name = selected["expected_role_arn"].rsplit("/", 1)[1]
                result = {
                    "Account": self.installer.env["account_id"],
                    "Arn": f"arn:aws:sts::{self.installer.env['account_id']}:assumed-role/{role_name}/fixture",
                    "UserId": selected["expected_role_id"] + ":fixture",
                }
            elif "get-role" in args:
                selected = self.installer.env["deployment_identity"]
                result = {
                    "Role": {
                        "Arn": selected["expected_role_arn"],
                        "RoleId": selected["expected_role_id"],
                    }
                }
            elif "put-object" in args:
                assert "--if-none-match" in args and not self.lock
                self.lock = True
                result = {"ETag": '"native-lock"'}
            elif "delete-object" in args:
                assert "--if-match" in args and self.lock
                self.lock = False
                result = {}
            else:
                raise AssertionError(args)
        else:
            command = args[4:]
            verb = command[0]
            if verb == "get":
                kind = command[1]
                if kind == "replicasets":
                    result = {
                        "items": [
                            {
                                "metadata": {
                                    "uid": "owned-replica",
                                    "ownerReferences": [
                                        {
                                            "kind": "Deployment",
                                            "uid": self.values[TARGETS[0]]["metadata"][
                                                "uid"
                                            ],
                                            "controller": True,
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                elif kind in {"jobs", "pods"}:
                    selector = command[command.index("-l") + 1]
                    result = {
                        "items": self.pods()
                        if selector != "scaledjob.keda.sh/name=superplane-paid-worker"
                        else self.worker_jobs
                        if kind == "jobs"
                        else self.worker_pods
                    }
                else:
                    result = self.values[(kind, command[2])]
            elif verb == "patch":
                assert self.lock
                assert "--force-conflicts" not in command
                key = (command[1], command[2])
                value = self.values[key]
                if self.race:
                    if self.race == "uid":
                        value["metadata"]["uid"] = "replacement-object"
                    elif self.race == "owner":
                        value["metadata"]["labels"][LABEL] = "foreign"
                    else:
                        value["metadata"]["resourceVersion"] = "concurrent-update"
                    self.race = None
                updated = copy.deepcopy(value)
                for operation in json.loads(data):
                    parts = [
                        p.replace("~1", "/").replace("~0", "~")
                        for p in operation["path"].split("/")[1:]
                    ]
                    parent = updated
                    for part in parts[:-1]:
                        parent = (
                            parent[int(part)]
                            if isinstance(parent, list)
                            else parent[part]
                        )
                    keypart = int(parts[-1]) if isinstance(parent, list) else parts[-1]
                    if operation["op"] == "test":
                        if parent[keypart] != operation["value"]:
                            raise Refusal("Kubernetes JSON Patch test refused")
                    else:
                        assert operation["op"] == "replace"
                        parent[keypart] = operation["value"]
                updated["metadata"]["resourceVersion"] = str(
                    int(value["metadata"]["resourceVersion"]) + 1
                )
                if key[0] == "Deployment":
                    annotations = updated["metadata"].setdefault("annotations", {})
                    annotations["deployment.kubernetes.io/revision"] = str(
                        int(annotations.get("deployment.kubernetes.io/revision", "0"))
                        + 1
                    )
                self.values[key] = updated
                self.patches.append(json.loads(data))
                result = updated
                if self.lose_reply:
                    self.lose_reply = False
                    raise Refusal("Patch response lost")
            elif verb == "rollout":
                result = {}
            elif verb == "exec":
                assert command[command.index("-c") + 1] == "superplane-api"
                assert command[-4:] == ["python", "-m", "app.installation", "database"]
                result = self.schema_report
            else:
                raise AssertionError(args)
        return SimpleNamespace(stdout=json.dumps(result), stderr="", returncode=0)


@pytest.fixture
def maintenance(environment, release, tmp_path, monkeypatch):
    installer = Installer(environment, release, tmp_path)
    installer.verify_deployment_identity = lambda: {
        "Account": environment["account_id"]
    }
    installer.env["api_adapters"] = {
        "dispatcher": {"role_arn": "arn:aws:iam::879318057152:role/producer"}
    }
    installer.env["paid_worker"] = {
        "role_arn": "arn:aws:iam::879318057152:role/worker",
        "operation_schema": "superplane_jobs",
        "queue_arn": "arn:aws:sqs:us-east-1:879318057152:original-queue",
    }
    release["images"]["superplane-paid-worker"] = "sha256:" + "b" * 64
    release["image_sources"]["superplane-paid-worker"] = {
        **release["image_sources"]["superplane-api"],
        "repository": "adp-superplane-paid-worker",
    }
    installer.release = digest(release)
    installer.receipt["release_id"] = installer.release
    api = copy.deepcopy(
        next(
            d
            for d in installer.docs
            if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
        )
    )
    primary = api["spec"]["template"]["spec"]["containers"][0]
    primary["image"] = "example.test/api@sha256:" + "c" * 64
    primary["env"].append(
        {"name": "SUPERPLANE_OPERATION_DISPATCH_ENABLED", "value": "false"}
    )
    transport = {
        "name": "recovery-transport",
        "image": "example.test/transport@sha256:" + "d" * 64,
        "command": ["python", "-m", "uvicorn", "app.recovery_transport:app"],
        "volumeMounts": [
            {"name": "recovery-tls", "mountPath": "/recovery-tls", "readOnly": True}
        ],
    }
    api["spec"]["template"]["spec"]["containers"].append(transport)
    api["spec"]["template"]["spec"]["volumes"] = [
        {"name": "recovery-tls", "secret": {"secretName": "original-tls"}}
    ]
    worker = {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledJob",
        "metadata": {
            "name": "superplane-paid-worker",
            "annotations": {"autoscaling.keda.sh/paused": "true"},
        },
        "spec": {
            "maxReplicaCount": 1,
            "jobTargetRef": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "paid-worker",
                                "image": "example.test/worker@sha256:" + "e" * 64,
                            }
                        ],
                        "volumes": [
                            {
                                "name": "original-state",
                                "persistentVolumeClaim": {
                                    "claimName": "original-state"
                                },
                            }
                        ],
                    }
                }
            },
        },
    }
    binding = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "superplane-paid-worker-binding"},
        "data": {
            "binding.json": json.dumps(
                dict(
                    expected_binding(installer.env, release),
                    worker_image_digest="sha256:" + "e" * 64,
                )
            ),
            "unrelated": "preserve-exact-bytes",
        },
    }
    worker_config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "superplane-paid-worker-config"},
        "data": {
            "SUPERPLANE_DOMAIN_SCHEMA": installer.env["database"]["schema"],
            "SUPERPLANE_OPERATION_SCHEMA": installer.env["paid_worker"][
                "operation_schema"
            ],
            "SUPERPLANE_DOMAIN_SCHEMA_HEAD": "054_unstarted_bootstrap",
            "unrelated": "preserve-this-too",
        },
    }
    values = [api, worker, binding, worker_config]
    for i, value in enumerate(values):
        value["metadata"].update(
            uid=f"original-{i}",
            resourceVersion="10",
            namespace=environment["namespace"],
        )
        value["metadata"].setdefault("labels", {})[LABEL] = installer.owner
    server = Server(installer, values)
    installer.commands = server
    monkeypatch.setattr(
        "installation.runtime_maintenance.runtime_digests",
        lambda i, ref: server.qualified_digests.get(ref, [ref.rsplit("@", 1)[1]]),
    )
    return installer, server, values


@pytest.mark.parametrize("mode", ["preserve", "matched-api"])
def test_bounded_maintenance_preserves_tls_state_and_native_history(maintenance, mode):
    installer, server, values = maintenance
    before = copy.deepcopy(values)
    value = plan(installer, values, mode, "f" * 64 if mode == "matched-api" else None)
    receipt = apply(installer, value, digest(value))
    assert not server.lock and "remote_lock" not in receipt
    assert receipt["completed"] == []  # No invented migration/bootstrap history.
    assert receipt["status"] == "runtime-maintenance-complete"
    assert len(server.patches) == 4
    assert [payload(server.values[t]) for t in TARGETS] == value["after"]
    assert values == before
    pod = value["after"][0]["spec"]["template"]["spec"]
    oldpod = value["before"][0]["spec"]["template"]["spec"]
    assert pod["volumes"] == oldpod["volumes"]
    sidecar = copy.deepcopy(pod["containers"][1])
    if mode == "matched-api":
        assert sidecar["image"] == image(installer.lock, "superplane-api")
        sidecar["image"] = oldpod["containers"][1]["image"]
    assert sidecar == oldpod["containers"][1]
    assert (
        value["after"][1]["spec"]["jobTargetRef"]["template"]["spec"]["volumes"]
        == before[1]["spec"]["jobTargetRef"]["template"]["spec"]["volumes"]
    )
    assert value["after"][2]["data"]["unrelated"] == "preserve-exact-bytes"
    apply(installer, value, digest(value))
    assert len(server.patches) == 4  # Already-observed desired state is not replayed.


@pytest.mark.parametrize(
    "fault",
    [
        "plan",
        "tamper",
        "owner",
        "uid",
        "spec",
        "binding",
        "dispatch",
        "keda",
        "job",
        "pod",
        "actual-dispatch",
    ],
)
def test_refuses_drift_and_nonquiescent_runtime_before_writes(maintenance, fault):
    installer, server, values = maintenance
    value = plan(installer, values)
    approved = digest(value)
    api, worker, binding, _ = [server.values[t] for t in TARGETS]
    if fault == "plan":
        approved = "0" * 64
    elif fault == "tamper":
        value["after"][0]["spec"]["replicas"] = 8
        approved = digest(value)
    elif fault == "owner":
        worker["metadata"]["labels"][LABEL] = "foreign"
    elif fault == "uid":
        worker["metadata"]["uid"] = "replacement"
    elif fault == "spec":
        api["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"] = (
            "different-tls"
        )
    elif fault == "binding":
        binding["data"]["unrelated"] = "changed"
    elif fault == "dispatch":
        api["spec"]["template"]["spec"]["containers"][0]["env"][-1]["value"] = "true"
    elif fault == "keda":
        worker["metadata"]["annotations"]["autoscaling.keda.sh/paused"] = "false"
    elif fault == "job":
        server.worker_jobs = [{"status": {}}]  # Pending, not merely active>0.
    elif fault == "pod":
        server.worker_pods = [{"status": {"phase": "Running"}}]
    else:
        server.api_dispatch = "true"
    with pytest.raises(Refusal):
        apply(installer, value, approved)
    assert server.patches == []


@pytest.mark.parametrize("race", ["uid", "resourceVersion", "owner"])
def test_atomic_patch_refuses_identity_race_and_retains_native_lock(maintenance, race):
    installer, server, values = maintenance
    value = plan(installer, values)
    server.race = race
    with pytest.raises(Refusal, match="JSON Patch test"):
        apply(installer, value, digest(value))
    assert not server.patches
    assert server.lock and installer.receipt["status"] == "recovery-required"
    assert (
        installer.receipt["runtime_maintenance"]["objects"]["superplane-paid-worker"][
            "state"
        ]
        == "write-pending"
    )


def test_lost_reply_observes_actual_partial_state_without_replay_or_false_completion(
    maintenance,
):
    installer, server, values = maintenance
    value = plan(installer, values)
    server.lose_reply = True
    with pytest.raises(Refusal, match="response lost"):
        apply(installer, value, digest(value))
    assert len(server.patches) == 1 and server.lock
    _, states = observe(installer, value)
    assert states == ["previous", "desired", "previous", "previous"]
    assert installer.receipt["status"] == "recovery-required"
    with pytest.raises(Refusal, match="Recover the retained"):
        apply(installer, value, digest(value))
    # Simulate the separately confirmed native lock-recovery step. The next
    # maintenance run must observe the first write, never send it a second time.
    installer.release_lock(installer.receipt["remote_lock"])
    apply(installer, value, digest(value))
    assert len(server.patches) == 4 and not server.lock
    assert (
        installer.receipt["runtime_maintenance"]["objects"]["superplane-paid-worker"][
            "state"
        ]
        == "observed-desired"
    )


def test_wrong_actual_image_keeps_paused_runtime_and_failure_receipt(maintenance):
    installer, server, values = maintenance
    value = plan(installer, values)
    server.bad_image_id = True
    with pytest.raises(Refusal, match="Actual API or transport"):
        apply(installer, value, digest(value))
    assert server.lock and installer.receipt["status"] == "recovery-required"
    assert observe(installer, value)[1] == ["desired"] * 4


def test_binding_identity_and_transport_qualification_are_closed_inputs(maintenance):
    installer, _, values = maintenance
    with pytest.raises(Refusal, match="qualification"):
        plan(installer, values, "matched-api")
    value = plan(installer, values)
    installer.lock["source_revision"] = "9" * 40
    with pytest.raises(Refusal):
        reviewed(installer, value, digest(value))
    data = json.loads(values[2]["data"]["binding.json"])
    data["worker_registry_id"] = "another-worker"
    values[2]["data"]["binding.json"] = json.dumps(data)
    with pytest.raises(Refusal, match="binding differs"):
        plan(installer, values)


def test_qualified_index_platform_is_accepted_but_other_runtime_digest_refuses(
    maintenance,
):
    installer, server, values = maintenance
    reference = image(installer.lock, "superplane-api")
    platform = "sha256:" + "7" * 64
    server.qualified_digests[reference] = [reference.rsplit("@", 1)[1], platform]
    server.reported_digests[reference] = reference.split("@")[0] + "@" + platform
    value = plan(installer, values)
    apply(installer, value, digest(value))
    server.reported_digests[reference] = reference.split("@")[0] + "@sha256:" + "8" * 64
    with pytest.raises(Refusal, match="Actual API or transport"):
        apply(installer, value, digest(value))


@pytest.mark.parametrize("state", ["terminal", "unready", "ready", "unknown"])
def test_service_selector_pods_require_real_owner_or_proven_nonserving_state(
    maintenance, state
):
    installer, server, values = maintenance
    value = plan(installer, values)
    server.unowned_pods = [
        {
            "metadata": {
                "ownerReferences": [
                    {"kind": "ReplicaSet", "uid": "foreign", "controller": True}
                ]
            },
            "status": {
                "phase": "Succeeded" if state == "terminal" else "Running",
                "conditions": []
                if state == "unknown"
                else [
                    {
                        "type": "Ready",
                        "status": "False" if state == "unready" else "True",
                    }
                ],
            },
        }
    ]
    if state in {"ready", "unknown"}:
        with pytest.raises(Refusal, match="foreign owner"):
            apply(installer, value, digest(value))
        assert not server.patches
    else:
        apply(installer, value, digest(value))


@pytest.mark.parametrize("fault", [None, "hash", "arm64", "duplicate", "child"])
def test_manifest_platform_mapping_is_bound_to_exact_registry_bytes(fault):
    from installation.runtime_maintenance import runtime_digests

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": "sha256:" + "0" * 64},
        "layers": [],
    }
    child = json.dumps(manifest)
    child_digest = "sha256:" + hashlib.sha256(child.encode()).hexdigest()
    descriptor = {
        "mediaType": manifest["mediaType"],
        "digest": child_digest,
        "platform": {
            "os": "linux",
            "architecture": "arm64" if fault == "arm64" else "amd64",
        },
    }
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [descriptor] * (2 if fault == "duplicate" else 1),
        }
    )
    index_digest = "sha256:" + hashlib.sha256(index.encode()).hexdigest()
    blobs = {index_digest: index, child_digest: child}
    if fault == "hash":
        blobs[index_digest] += " "
    if fault == "child":
        blobs[child_digest] += " "
    installer = SimpleNamespace(
        env={"account_id": "879318057152", "region": "us-east-1"},
        json=lambda value: value,
    )
    installer.aws = lambda *args: {
        "images": [{"imageManifest": blobs[args[-1].split("=", 1)[1]]}]
    }
    reference = (
        "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-api@"
        + index_digest
    )
    if fault:
        with pytest.raises(Refusal):
            runtime_digests(installer, reference)
    else:
        assert runtime_digests(installer, reference) == sorted(
            [index_digest, child_digest]
        )


@pytest.fixture
def maintenance_cli(maintenance, monkeypatch, tmp_path):
    from installation import runtime_maintenance as command

    installer, server, _ = maintenance
    env_path, lock_path = tmp_path / "environment.json", tmp_path / "release.json"
    env_path.write_text(json.dumps(installer.env))
    lock_path.write_text(json.dumps(installer.lock))
    # Each CLI invocation starts a real fresh Installer, as a new process would.
    # Keep real identity verification/save behavior to expose receipt ordering.
    instances = []

    def fresh(env, lock, directory):
        current = Installer(env, lock, directory, commands=server)
        current.target = lambda **kwargs: current.verify_deployment_identity()
        server.installer = current
        instances.append(current)
        return current

    monkeypatch.setattr(
        "installation.runner.render", lambda *args, **kwargs: installer.docs
    )
    monkeypatch.setattr(command, "Installer", fresh)
    monkeypatch.setattr(command, "validate", lambda *args, **kwargs: None)
    args = [
        "--environment",
        str(env_path),
        "--release-lock",
        str(lock_path),
        "--output",
        str(tmp_path),
    ]
    return command, args, server, instances


def test_cli_plan_observe_and_apply_keep_the_saved_plan_and_truthful_receipt_separate(
    maintenance_cli, tmp_path
):
    command, args, server, instances = maintenance_cli
    assert command.main([*args, "--plan"]) == 0
    installer = instances[-1]
    original_run = installer.run_id
    original_receipt = installer.receipt_path.read_bytes()
    path = tmp_path / "maintenance-plan.json"
    original = path.read_bytes()
    approved = digest(json.loads(original))
    assert not server.patches and not server.lock
    assert command.main([*args, "--plan"]) == 2
    assert path.read_bytes() == original
    assert command.main([*args, "--apply", "--approved-plan-sha256", "0" * 64]) == 2
    assert not server.patches
    assert installer.receipt_path.read_bytes() == original_receipt
    assert command.main([*args, "--observe", "--approved-plan-sha256", approved]) == 0
    installer = instances[-1]
    assert installer.run_id == original_run
    assert installer.receipt["status"] == "planned" and not server.lock
    assert command.main([*args, "--apply", "--approved-plan-sha256", approved]) == 0
    assert len(server.patches) == 4 and path.read_bytes() == original
    receipt = json.loads(installer.receipt_path.read_text())
    assert len({id(i) for i in instances}) == len(instances)
    assert receipt["run_id"] == original_run
    assert receipt["deployment_identity_verification"]["assumed_role_arn"].endswith(
        "/fixture"
    )
    assert (
        receipt["status"] == "runtime-maintenance-complete"
        and receipt["completed"] == []
    )
    assert not receipt.get("remote_lock") and not receipt.get("lock_attempt")


@pytest.mark.parametrize("mode", ["apply", "observe", "recover-lock"])
@pytest.mark.parametrize(
    "field",
    ["installation_id", "environment", "release_lock", "runtime_maintenance", "run_id"],
)
def test_cli_refuses_foreign_receipt_without_overwriting_it_or_reading_live_state(
    maintenance_cli, tmp_path, mode, field
):
    command, args, server, _ = maintenance_cli
    assert command.main([*args, "--plan"]) == 0
    path = tmp_path / "receipt.json"
    previous = json.loads(path.read_text())
    previous[field] = {} if isinstance(previous[field], dict) else "another-owner"
    path.write_text(json.dumps(previous))
    original = path.read_bytes()
    reads = server.identity_reads
    approved = digest(json.loads((tmp_path / "maintenance-plan.json").read_text()))
    assert command.main([*args, "--" + mode, "--approved-plan-sha256", approved]) == 2
    assert path.read_bytes() == original
    assert server.identity_reads == reads
    assert not server.patches and not server.lock


def test_fresh_cli_observation_preserves_retained_lock_and_pending_write_journal(
    maintenance_cli, tmp_path
):
    command, args, server, _ = maintenance_cli
    assert command.main([*args, "--plan"]) == 0
    approved = digest(json.loads((tmp_path / "maintenance-plan.json").read_text()))
    server.lose_reply = True
    assert command.main([*args, "--apply", "--approved-plan-sha256", approved]) == 2
    path = tmp_path / "receipt.json"
    previous = json.loads(path.read_text())
    assert previous["remote_lock"] and server.lock and len(server.patches) == 1
    assert command.main([*args, "--observe", "--approved-plan-sha256", approved]) == 0
    assert command.main([*args, "--apply", "--approved-plan-sha256", approved]) == 2
    current = json.loads(path.read_text())
    for key in ("run_id", "remote_lock", "runtime_maintenance"):
        assert current[key] == previous[key]
    assert server.lock and len(server.patches) == 1


def test_rollout_waits_for_owned_terminating_pod_without_reapplying(
    maintenance, monkeypatch
):
    from installation import runtime_maintenance as command

    installer, server, values = maintenance
    value = plan(installer, values)
    apply(installer, value, digest(value))
    current = server.pods()
    old = copy.deepcopy(current[0])
    old["metadata"]["deletionTimestamp"] = "2026-10-26T00:00:00Z"
    sequence = iter([[old, *current], current])
    monkeypatch.setattr(command, "api_pods", lambda *args: next(sequence))
    monkeypatch.setattr(command.time, "sleep", lambda _: None)
    command.verify_rollout(installer, value)
    assert len(server.patches) == 4


def test_only_root_deployment_controller_revision_is_normalized(maintenance):
    _, _, values = maintenance
    before = values[0]
    changed = copy.deepcopy(before)
    changed["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "155"}
    assert payload(changed) == payload(before)
    changed["metadata"]["annotations"] = {}
    assert payload(changed) == payload(before)
    changed["metadata"]["annotations"] = {"reviewed-setting": "changed"}
    assert payload(changed) != payload(before)
    changed = copy.deepcopy(before)
    changed["spec"]["template"]["metadata"].setdefault("annotations", {})[
        "deployment.kubernetes.io/revision"
    ] = "unexpected-pod-change"
    assert payload(changed) != payload(before)


@pytest.mark.parametrize("fault", ["database", "schema", "revision", "worker-schema"])
def test_real_schema_and_worker_identity_are_required_before_runtime_writes(
    maintenance, fault
):
    installer, server, values = maintenance
    value = plan(installer, values)
    if fault == "worker-schema":
        server.values[TARGETS[3]]["data"]["SUPERPLANE_DOMAIN_SCHEMA"] = "another"
    else:
        server.schema_report[fault] = (
            "054_unstarted_bootstrap" if fault == "revision" else "another"
        )
    with pytest.raises(Refusal):
        apply(installer, value, digest(value))
    assert server.patches == []


def test_schema_config_change_preserves_other_values_and_does_not_claim_migration(
    maintenance,
):
    installer, server, values = maintenance
    value = plan(installer, values)
    assert value["database"]["revision"] == installer.lock["schema"]["observed"]["head"]
    apply(installer, value, digest(value))
    config = server.values[TARGETS[3]]["data"]
    assert config["SUPERPLANE_DOMAIN_SCHEMA_HEAD"] == value["database"]["revision"]
    assert config["unrelated"] == "preserve-this-too"
    assert "migration" not in installer.receipt and installer.receipt["completed"] == []


@pytest.fixture
def retained(maintenance, native):
    installer, server, values = maintenance
    env, lock = native
    lifecycle_config(env)
    installer.env.update(copy.deepcopy(env))
    installer.env["paid_worker"]["active_deadline_seconds"] = 10800
    lock["image_sources"]["superplane-paid-worker"]["source_revision"] = "b" * 40
    installer.release = digest(lock)
    installer.receipt["release_id"] = installer.release
    worker = values[1]["spec"]["jobTargetRef"]
    worker["activeDeadlineSeconds"] = 3600
    worker["template"]["spec"]["containers"][0]["image"] = image(
        lock, "superplane-paid-worker"
    )
    # Noncanonical whitespace must survive retained mode byte for byte.
    values[2]["data"]["binding.json"] = json.dumps(
        expected_binding(installer.env, lock), indent=2
    )
    docs = [{"metadata": {"labels": {LABEL: installer.owner}}}]
    paid_worker.project(installer.env, lock, docs)
    values[3]["data"].update(
        next(
            d["data"]
            for d in docs
            if d.get("kind") == "ConfigMap"
            and d["metadata"]["name"] == "superplane-paid-worker-config"
        )
    )
    server.values = {t: copy.deepcopy(v) for t, v in zip(TARGETS, values, strict=True)}
    return installer, server, values


@pytest.mark.parametrize("deadline", [None, 10800])
def test_retained_worker_maintenance_changes_only_explicit_deadline(retained, deadline):
    installer, server, values = retained
    original_lock = copy.deepcopy(installer.lock)
    value = plan(
        installer, values, retain_worker=True, worker_active_deadline_seconds=deadline
    )
    assert value["retain_worker"] is True
    assert value.get("worker_active_deadline_seconds") == deadline
    expected_worker = copy.deepcopy(value["before"][1])
    if deadline:
        expected_worker["spec"]["jobTargetRef"]["activeDeadlineSeconds"] = deadline
    assert value["after"][1] == expected_worker
    assert value["after"][2:] == value["before"][2:]
    assert (
        value["after"][0]["spec"]["template"]["spec"]["containers"][1]
        == values[0]["spec"]["template"]["spec"]["containers"][1]
    )
    apply(installer, value, digest(value))
    assert len(server.patches) == (2 if deadline else 1)
    assert not server.lock
    assert installer.lock == original_lock
    apply(installer, value, digest(value))
    assert len(server.patches) == (2 if deadline else 1)


@pytest.mark.parametrize(
    "fault", ["image", "binding", "schema", "config", "deadline", "missing-deadline"]
)
def test_retained_worker_refuses_incompatible_existing_worker(retained, fault):
    installer, server, values = retained
    deadline = 10800
    if fault == "image":
        values[1]["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0][
            "image"
        ] = image(installer.lock, "superplane-api")
    elif fault == "binding":
        binding = json.loads(values[2]["data"]["binding.json"])
        binding["worker_image_digest"] = "sha256:" + "c" * 64
        values[2]["data"]["binding.json"] = json.dumps(binding)
    elif fault == "schema":
        values[3]["data"]["SUPERPLANE_DOMAIN_SCHEMA_HEAD"] = "old-schema"
    elif fault == "config":
        values[3]["data"]["ADP_EXECUTION_AUTHORITY_ENDPOINT"] = (
            "https://foreign.example.test"
        )
    elif fault == "deadline":
        deadline = 7200
    else:
        del values[1]["spec"]["jobTargetRef"]["activeDeadlineSeconds"]
    with pytest.raises(Refusal):
        plan(
            installer,
            values,
            retain_worker=True,
            worker_active_deadline_seconds=deadline,
        )
    assert not server.patches and not server.lock


@pytest.mark.parametrize(
    "fault", ["image", "binding", "config", "deadline", "remove-option", "type"]
)
def test_retained_plan_replay_refuses_changes_outside_its_saved_selection(
    retained, fault
):
    installer, server, values = retained
    value = plan(
        installer, values, retain_worker=True, worker_active_deadline_seconds=10800
    )
    if fault == "image":
        value["after"][1]["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0][
            "image"
        ] = image(installer.lock, "superplane-api")
    elif fault == "binding":
        value["after"][2]["data"]["binding.json"] += " "
    elif fault == "config":
        value["after"][3]["data"]["unrelated"] = "changed"
    elif fault == "deadline":
        value["after"][1]["spec"]["jobTargetRef"]["activeDeadlineSeconds"] = 7200
    elif fault == "remove-option":
        del value["worker_active_deadline_seconds"]
    else:
        value["retain_worker"] = "true"
    with pytest.raises(Refusal):
        apply(installer, value, digest(value))
    assert not server.patches and not server.lock


def test_cli_retained_selection_is_saved_and_used_without_relaxing_default(
    retained, maintenance_cli, tmp_path, monkeypatch
):
    installer, _, _ = retained
    command, args, server, instances = maintenance_cli
    (tmp_path / "environment.json").write_text(json.dumps(installer.env))
    (tmp_path / "release.json").write_text(json.dumps(installer.lock))
    monkeypatch.setattr(
        command,
        "validate",
        lambda env, lock, **kwargs: paid_worker.validate(
            env, lock, retain_worker=kwargs.get("retain_worker", False)
        ),
    )
    assert command.main([*args, "--plan"]) == 2
    assert not instances and not server.patches
    assert (
        command.main(
            [
                *args,
                "--plan",
                "--retain-worker",
                "--worker-active-deadline-seconds",
                "10800",
            ]
        )
        == 0
    )
    path = tmp_path / "maintenance-plan.json"
    original = path.read_bytes()
    approved = digest(json.loads(original))
    assert (
        command.main(
            [*args, "--apply", "--approved-plan-sha256", approved, "--retain-worker"]
        )
        == 2
    )
    assert not server.patches
    assert command.main([*args, "--observe", "--approved-plan-sha256", approved]) == 0
    assert command.main([*args, "--apply", "--approved-plan-sha256", approved]) == 0
    assert len(server.patches) == 2 and path.read_bytes() == original
