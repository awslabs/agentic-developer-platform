"""Image-only writer rollout under controlled Kubernetes observations (#8105)."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("writer_sync", ROOT / "modules/gateway/scripts/sync-gateway-writers.py")
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway"
NEW, OLD = (REGISTRY + "@sha256:" + c * 64 for c in ("a", "b"))
HASHES = {p: str(i + 1) * 64 for i, p in enumerate(script.SOURCE_FILES)}
PRIMARY, CHAT, AUX = "bedrockgateway", "chat-sandbox-gateway", "operator-probe"


def deployment(name, *, image=OLD, container=None, command=None):
    container = container or name
    app = {
        "name": container,
        "image": image,
        "envFrom": [{"configMapRef": {"name": "bedrockgateway-config"}}],
        "env": [{"name": "ADP_WORK_CLAIMS_ENABLED", "value": "true"}],
        "volumeMounts": [{"name": "tls", "mountPath": "/var/run/tls", "readOnly": True}],
        "ports": [{"containerPort": 8443}],
        "readinessProbe": {"httpGet": {"path": "/health", "scheme": "HTTPS", "port": 8443}},
    }
    if command:
        app["command"] = command
    return {
        "metadata": {"name": name, "namespace": "adp-gateway", "uid": name + "-uid", "resourceVersion": "10", "generation": 1},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}, "annotations": {"unchanged": "yes"}},
                "spec": {
                    "serviceAccountName": "gateway-service",
                    "securityContext": {"runAsNonRoot": True},
                    "volumes": [{"name": "tls", "secret": {"secretName": "existing-tls"}}],
                    "containers": [app],
                },
            },
        },
        "status": {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1},
    }


def children(dep):
    meta = dep["metadata"]
    rs = {
        "metadata": {
            "name": meta["name"] + "-rs",
            "namespace": "adp-gateway",
            "uid": meta["uid"] + "-rs",
            "ownerReferences": [{"kind": "Deployment", "name": meta["name"], "uid": meta["uid"], "controller": True}],
        }
    }
    pod = {
        "metadata": {
            "name": meta["name"] + "-pod",
            "namespace": "adp-gateway",
            "uid": meta["uid"] + "-pod",
            "ownerReferences": [{"kind": "ReplicaSet", "name": rs["metadata"]["name"], "uid": rs["metadata"]["uid"], "controller": True}],
        },
        "spec": copy.deepcopy(dep["spec"]["template"]["spec"]),
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": c["name"], "ready": True, "imageID": "docker-pullable://" + c["image"], "containerID": "containerd://" + meta["uid"]}
                for c in dep["spec"]["template"]["spec"]["containers"]
            ],
        },
    }
    return rs, pod


class Cluster(script.Writers):
    def __init__(self, deployments=None, *, enrolled=True):
        self.data = {
            "deployments": deployments
            if deployments is not None
            else [
                deployment(PRIMARY, image=NEW),
                deployment(CHAT, command=["python", "-m", "src.agentauth.chat_tls"]),
                deployment(AUX, container=PRIMARY),
                deployment(
                    "dev-box-controller", container="controller", command=["python", "-m", "controller.worker", "--manifest", "/etc/release.json"]
                ),
            ],
            "replicasets": [],
            "pods": [],
        }
        for dep in self.data["deployments"]:
            rs, pod = children(dep)
            self.data["replicasets"].append(rs)
            self.data["pods"].append(pod)
        inventory = [{"deployment": AUX, "uid": AUX + "-uid", "container": PRIMARY, "entrypoint": "gateway"}] if enrolled else []
        super().__init__(namespace="adp-gateway", image=NEW, inventory=script.bindings(json.dumps(inventory)), source_hashes=HASHES, timeout=0)
        self.calls, self.patches = [], []
        self.denied = None
        self.before_patch = self.after_patch = self.after_exec = None
        self.keep_old = False
        self.hashes = copy.deepcopy(HASHES)
        self.trust_config = {'metadata': {'name': 'adp-worker-authority-config', 'namespace': 'adp-gateway', 'uid': 'trust-uid'},
            'data': {key: 'sha256:' + 'c' * 64 for key in script.TRUST_KEYS}}
        self.runtime_trust_override = None

    def get(self, name):
        return next(d for d in self.data["deployments"] if d["metadata"]["name"] == name)

    def kubectl(self, *args, timeout=60):
        self.calls.append(args)
        if args[:2] == ("auth", "can-i"):
            resource = args[3] + ("/exec" if args[4:] == ("--subresource=exec",) else "")
            return "no" if (args[2], resource) == self.denied else "yes"
        if args[0] == "get":
            if args[1] == 'configmap/adp-worker-authority-config':
                return json.dumps(self.trust_config) if self.trust_config is not None else ''
            if args[1].startswith("deployment/"):
                return json.dumps(self.get(args[1].split("/", 1)[1]))
            return json.dumps({"items": copy.deepcopy(self.data[args[1]])})
        if args[0] == "exec":
            assert args[-4:-1] == ("python", "-S", "-c")
            if self.after_exec:
                action, self.after_exec = self.after_exec, None
                action(self)
            if args[-1] in (script.TRUST_READ, script.INACTIVE_BOOTSTRAP_READ):
                pod = next(p for p in self.data['pods'] if p['metadata']['name'] == args[1].removeprefix('pod/'))
                container = next(c for c in pod['spec']['containers'] if c['name'] == args[args.index('-c') + 1])
                env = {e['name']: e.get('value', '') for e in container.get('env', [])}
                if args[-1] == script.INACTIVE_BOOTSTRAP_READ:
                    return subprocess.check_output([sys.executable, '-S', '-c', args[-1]], env=env, text=True)
                defaults = self.trust_config['data'] if self.trust_config is not None else {}
                trust = {key: env.get(key, defaults.get(key, '')) for key in script.TRUST_KEYS}
                return json.dumps({'worker_trust_sha256': self.runtime_trust_override or script.digest(trust)})
            return json.dumps({"authority_enabled": True, "claims_enabled": True, "source_hashes": self.hashes})
        if args[0] == "patch":
            name = args[1].split("/", 1)[1]
            dep = self.get(name)
            patch = json.loads(Path(args[args.index("--patch-file") + 1]).read_text())
            assert Path(args[args.index("--patch-file") + 1]).stat().st_mode & 0o777 == 0o600
            if self.before_patch:
                self.before_patch(dep)
            # Independent RFC6902 test evaluator: a competing update must fail
            # before image replacement, not be silently retried on a new spec.
            for op in patch:
                parts = op["path"].strip("/").split("/")
                value = dep
                for part in parts:
                    value = value[int(part)] if isinstance(value, list) else value[part]
                if op["op"] == "test" and value != op["value"]:
                    raise RuntimeError("JSON Patch test failed")
            writes = [op for op in patch if op["op"] != "test"]
            assert len(writes) == 1
            field = writes[0]['path'].rsplit('/', 1)[1]
            assert (writes[0]['op'], field) in {('replace', 'image'), ('add', 'env')}
            self.patches.append(patch)
            index = int(writes[0]["path"].split("/")[-2])
            dep["spec"]["template"]["spec"]["containers"][index][field] = writes[0]["value"]
            dep["metadata"]["resourceVersion"] = "11"
            for pod in list(self.data["pods"]):
                if pod["metadata"]["name"] != name + "-pod":
                    continue
                if self.keep_old:
                    old = copy.deepcopy(pod)
                    old["metadata"].update(name=name + "-old", uid=name + "-old-uid", deletionTimestamp="2026-01-01T00:00:00Z")
                    self.data["pods"].append(old)
                pod["spec"] = copy.deepcopy(dep["spec"]["template"]["spec"])
                pod["status"]["containerStatuses"][index]["imageID"] = "docker-pullable://" + NEW
            if self.after_patch:
                self.after_patch(dep)
            return "deployment.apps/" + name
        raise AssertionError(f"Unexpected command {args[:2]}")


class TrustCluster(script.WorkerTrust):
    def __init__(self, *, enrolled=True):
        cluster = Cluster(enrolled=enrolled)
        self.__dict__.update(cluster.__dict__)
        for dep in self.data['deployments']:
            if dep['metadata']['name'] not in (PRIMARY, CHAT, AUX):
                continue
            container = dep['spec']['template']['spec']['containers'][0]
            container['image'] = NEW
            # A inherited second key and two stale explicit values both occur
            # in real clusters; do not assume every env entry already exists.
            container['env'].append({'name': script.TRUST_KEYS[0], 'value': 'sha256:' + 'd' * 64})
            if dep['metadata']['name'] == CHAT:
                container['env'].append({'name': script.TRUST_KEYS[1], 'valueFrom': {'configMapKeyRef': {'name': 'obsolete', 'key': 'trust'}}})
            for pod in self.data['pods']:
                if pod['metadata']['name'] == dep['metadata']['name'] + '-pod':
                    pod['spec'] = copy.deepcopy(dep['spec']['template']['spec'])
                    pod['status']['containerStatuses'][0]['imageID'] = 'docker-pullable://' + NEW
        super().__init__(namespace='adp-gateway', image=NEW, inventory=cluster.inventory, timeout=0)

    get = Cluster.get
    kubectl = Cluster.kubectl


def test_trust_sync_covers_primary_chat_and_enrolled_auxiliary_preserving_every_other_field():
    cluster = TrustCluster()
    before = copy.deepcopy(cluster.data['deployments'])
    result = cluster.run()
    assert len(cluster.patches) == 3
    assert all(p['worker_trust_matches'] for p in result['pods'])
    for original, actual in zip(before, cluster.data['deployments'], strict=True):
        expected = copy.deepcopy(original['spec'])
        if original['metadata']['name'] in (PRIMARY, CHAT, AUX):
            target = expected['template']['spec']['containers'][0]
            untouched = [x for x in target['env'] if x['name'] not in script.TRUST_KEYS]
            actual_env = actual['spec']['template']['spec']['containers'][0]['env']
            assert [x for x in actual_env if x['name'] not in script.TRUST_KEYS] == untouched
            target['env'] = actual_env
            assert {x['name']: x['value'] for x in actual_env if x['name'] in script.TRUST_KEYS} == cluster.trust_config['data']
        assert actual['spec'] == expected
    assert all([(p['op'], p['path']) for p in patch[:3]] == [('test', '/metadata/uid'), ('test', '/metadata/generation'), ('test', '/spec')] for patch in cluster.patches)
    assert all(patch[-1]['path'].endswith('/env') for patch in cluster.patches)


def test_trust_sync_preview_reports_stale_values_without_mutation_and_replay_is_idempotent():
    cluster = TrustCluster()
    preview = cluster.run(preview=True)
    assert not cluster.patches
    assert not any(p['worker_trust_matches'] for p in preview['pods'])
    cluster.run()
    cluster.patches.clear()
    assert cluster.run()['status'] == 'verified'
    assert cluster.run(verify_only=True)['status'] == 'verified'
    assert not cluster.patches


def test_trust_sync_refuses_duplicate_explicit_keys_before_any_patch():
    cluster = TrustCluster()
    target = cluster.get(CHAT)['spec']['template']['spec']['containers'][0]
    target['env'].append({'name': script.TRUST_KEYS[0], 'value': 'disabled'})
    with pytest.raises(ValueError, match='Duplicate explicit'):
        cluster.run()
    assert not cluster.patches


def test_trust_verification_accepts_correct_inherited_values_without_patching():
    cluster = TrustCluster()
    for dep in cluster.data['deployments']:
        for container in dep['spec']['template']['spec']['containers']:
            container['env'] = [x for x in container.get('env', []) if x['name'] not in script.TRUST_KEYS]
    for pod in cluster.data['pods']:
        for container in pod['spec']['containers']:
            container['env'] = [x for x in container.get('env', []) if x['name'] not in script.TRUST_KEYS]
    result = cluster.run(verify_only=True)
    assert all(p['worker_trust_matches'] for p in result['pods'])
    assert not cluster.patches


def test_trust_sync_refuses_unregistered_application_before_any_patch():
    cluster = TrustCluster(enrolled=False)
    with pytest.raises(ValueError, match='Unregistered or uncertain application writer'):
        cluster.run()
    assert not cluster.patches


def test_trust_sync_refuses_replaced_registered_uid_before_any_patch():
    cluster = TrustCluster()
    cluster.get(AUX)['metadata']['uid'] = 'replacement-uid'
    with pytest.raises(ValueError, match='UID changed'):
        cluster.run()
    assert not cluster.patches


def test_trust_sync_fences_concurrent_configuration_update():
    cluster = TrustCluster()
    cluster.before_patch = lambda dep: dep['spec'].update(replicas=2)
    with pytest.raises(RuntimeError, match='JSON Patch test failed'):
        cluster.run()
    assert not cluster.patches


def test_trust_sync_waits_for_old_terminating_processes_with_stale_trust():
    cluster = TrustCluster()
    cluster.keep_old = True
    with pytest.raises(ValueError, match='including old terminating processes'):
        cluster.run()


def test_trust_sync_refuses_stale_runtime_even_if_pod_spec_is_current():
    cluster = TrustCluster()
    cluster.runtime_trust_override = 'f' * 64
    with pytest.raises(ValueError, match='Running Gateway worker trust differs'):
        cluster.run()


def test_trust_sync_refuses_changed_authority_configmap_during_observation():
    cluster = TrustCluster()
    cluster.after_exec = lambda c: c.trust_config['data'].update({key: 'sha256:' + 'e' * 64 for key in script.TRUST_KEYS})
    with pytest.raises(ValueError, match='ConfigMap changed'):
        cluster.run()


@pytest.mark.parametrize('values', [{}, {script.TRUST_KEYS[0]: 'disabled', script.TRUST_KEYS[1]: 'sha256:' + 'a' * 64},
    {key: 'sha256:' + 'z' * 64 for key in script.TRUST_KEYS}, {key: ','.join(['sha256:' + 'a' * 64] * 2) for key in script.TRUST_KEYS}])
def test_trust_sync_rejects_missing_inconsistent_or_invalid_authority_without_patching(values):
    cluster = TrustCluster()
    cluster.trust_config['data'] = values
    with pytest.raises(ValueError, match='ConfigMap'):
        cluster.run()
    assert not cluster.patches


def test_trust_probe_reports_only_hash_without_environment_values():
    values = {key: 'sha256:' + 'e' * 64 for key in script.TRUST_KEYS}
    output = subprocess.check_output([sys.executable, '-S', '-c', script.TRUST_READ], env={**os.environ, **values}, text=True)
    assert json.loads(output) == {'worker_trust_sha256': script.digest(values)}
    assert all(value not in output for value in values.values())


def bootstrap_cluster():
    cluster = TrustCluster()
    cluster.allow_inactive_bootstrap = True
    cluster.trust_config = None
    for spec in [d['spec']['template']['spec'] for d in cluster.data['deployments']] + [p['spec'] for p in cluster.data['pods']]:
        for container in spec['containers']:
            container['env'] = [{'name': 'AGENT_AUTHORITY_ENABLED', 'value': 'false'},
                {'name': 'ADP_WORK_CLAIMS_ENABLED', 'value': 'false'},
                *[{'name': key, 'value': 'disabled'} for key in script.TRUST_KEYS]]
    return cluster


def test_missing_worker_configmap_allows_proven_inactive_gateway_bootstrap_without_mutation():
    cluster = bootstrap_cluster()
    before = copy.deepcopy(cluster.data)
    result = cluster.run()
    assert result['status'] == 'inactive_bootstrap'
    assert result['trust_mutations'] == 0 and not result['worker_authority_config_present']
    assert len(result['pods']) == 3 and all(p['inactive_authority_proven'] for p in result['pods'])
    assert not cluster.patches and cluster.data == before


@pytest.mark.parametrize('key,value', [('AGENT_AUTHORITY_ENABLED', 'true'), ('ADP_AGENT_AUTHORITY_ENABLED', 'true'),
    ('ADP_WORK_CLAIMS_ENABLED', 'true'), ('ADP_TASK_API_READ_ENABLED', 'true'),
    ('ADP_TASK_API_WORKER_ENABLED', 'true'), ('AGENT_AUTHORITY_ENABLED', 'invalid'),
    ('AGENT_WORKER_IMAGE_DIGESTS', 'sha256:' + 'a' * 64), ('AGENT_TASK_SOURCE_ROLE_ARN', 'arn:example:worker'),
    ('AGENT_DISPATCH_QUEUE_URL', 'https://example.invalid/worker')])
def test_missing_configmap_is_not_bootstrap_for_enabled_or_configured_writer(key, value):
    cluster = bootstrap_cluster()
    target = next(p for p in cluster.data['pods'] if p['metadata']['name'] == AUX + '-pod')['spec']['containers'][0]
    target['env'] = [e for e in target['env'] if e['name'] != key] + [{'name': key, 'value': value}]
    with pytest.raises(ValueError, match='enabled or configured Gateway'):
        cluster.run()
    assert not cluster.patches


def test_worker_rollout_never_uses_inactive_gateway_bootstrap_exception():
    cluster = bootstrap_cluster()
    cluster.allow_inactive_bootstrap = False
    with pytest.raises(ValueError, match='ConfigMap is missing'):
        cluster.run()
    assert not cluster.patches


def test_bootstrap_still_refuses_duplicate_explicit_trust_keys():
    cluster = bootstrap_cluster()
    cluster.get(PRIMARY)['spec']['template']['spec']['containers'][0]['env'].append({'name': script.TRUST_KEYS[0], 'value': 'disabled'})
    with pytest.raises(ValueError, match='Duplicate explicit'):
        cluster.run()
    assert not cluster.patches


def test_configmap_disappearance_never_becomes_inactive_bootstrap():
    cluster = TrustCluster()
    cluster.allow_inactive_bootstrap = True
    cluster.after_exec = lambda c: setattr(c, 'trust_config', None)
    with pytest.raises(ValueError, match='disappeared during rollout'):
        cluster.run()


def test_bootstrap_includes_old_terminating_writers_in_inactive_proof():
    cluster = bootstrap_cluster()
    old = copy.deepcopy(next(p for p in cluster.data['pods'] if p['metadata']['name'] == AUX + '-pod'))
    old['metadata'].update(name=AUX + '-old', uid=AUX + '-old-uid', deletionTimestamp='2026-01-01T00:00:00Z')
    old['spec']['containers'][0]['env'].append({'name': 'AGENT_WORKER_ROLE_ARN', 'value': 'arn:example:worker'})
    cluster.data['pods'].append(old)
    with pytest.raises(ValueError, match='enabled or configured Gateway'):
        cluster.run()
    assert not cluster.patches


def test_configmap_access_error_is_not_absence():
    cluster = bootstrap_cluster()
    original = cluster.kubectl

    def denied(*args, **kwargs):
        if args[:2] == ('get', 'configmap/adp-worker-authority-config'):
            raise RuntimeError('read forbidden')
        return original(*args, **kwargs)

    cluster.kubectl = denied
    with pytest.raises(RuntimeError, match='read forbidden'):
        cluster.run()
    assert not cluster.patches


def test_updates_chat_and_explicit_auxiliary_images_preserving_every_other_spec_field():
    cluster = Cluster()
    before = copy.deepcopy(cluster.data["deployments"])
    report = cluster.run()
    assert report["status"] == "verified"
    assert len(cluster.patches) == 2
    assert {p["enrollment"] for p in report["pods"]} == {"primary", "maintained-chat", "explicit-inventory"}
    for original, actual in zip(before, cluster.data["deployments"], strict=True):
        expected = copy.deepcopy(original["spec"])
        if actual["metadata"]["name"] != "dev-box-controller":
            expected["template"]["spec"]["containers"][0]["image"] = NEW
        assert actual["spec"] == expected
    assert not any("dev-box-controller" in arg for call in cluster.calls if call[0] in {"exec", "patch"} for arg in call)
    for patch in cluster.patches:
        assert [(op["op"], op["path"]) for op in patch[:3]] == [("test", "/metadata/uid"), ("test", "/metadata/generation"), ("test", "/spec")]


def test_preview_observes_all_writers_without_changes_or_secret_reads():
    cluster = Cluster()
    report = cluster.run(preview=True)
    assert report["status"] == "preview" and len(report["pods"]) == 3
    assert not cluster.patches
    assert {c[1] for c in cluster.calls if c[0] == "get"} == {"deployments", "replicasets", "pods"}
    assert all("spec" not in row and "spec_sha256" in row for row in report["deployments"])


def test_replay_is_idempotent_and_verification_performs_no_patch():
    cluster = Cluster()
    cluster.run()
    cluster.patches.clear()
    assert cluster.run()["status"] == "verified"
    assert cluster.run(verify_only=True)["status"] == "verified"
    assert not cluster.patches


def test_optional_chat_absence_is_supported():
    cluster = Cluster([deployment(PRIMARY, image=NEW)], enrolled=False)
    assert len(cluster.run()["pods"]) == 1


def gitlab_reconciler():
    manifest = next(
        item
        for item in yaml.safe_load_all((ROOT / "modules/source-control/gitlab/infra/home-reconciler.yaml").read_text())
        if item["kind"] == "Deployment"
    )
    result = deployment("gitlab-home-reconciler", container="reconciler")
    result["spec"] = copy.deepcopy(manifest["spec"])
    result["spec"]["template"]["spec"]["containers"][0]["image"] = OLD
    return result


def test_gitlab_reconciliation_does_not_join_gateway_application_rollout():
    reconciler = gitlab_reconciler()
    before = copy.deepcopy(reconciler)
    cluster = Cluster([deployment(PRIMARY, image=NEW), reconciler], enrolled=False)
    report = cluster.run()
    assert report["status"] == "verified"
    assert [item["deployment"] for item in report["pods"]] == [PRIMARY]
    assert reconciler == before
    assert not cluster.patches
    assert not any("gitlab-home-reconciler" in arg for call in cluster.calls if call[0] in {"exec", "patch"} for arg in call)


@pytest.mark.parametrize("change", ["principal", "image", "wrapper", "extra_argument", "application"])
def test_gitlab_reconciler_exclusion_requires_its_exact_process_and_principal(change):
    reconciler = gitlab_reconciler()
    spec = reconciler["spec"]["template"]["spec"]
    container = spec["containers"][0]
    if change == "principal":
        spec["serviceAccountName"] = "gateway-service"
    elif change == "image":
        container["image"] = "untrusted.example/reconciler:latest"
    elif change == "wrapper":
        container["command"] = ["sh", "-c", "python -m src.gitlab.home_worker"]
    elif change == "extra_argument":
        container["args"] = ["--unknown"]
    else:
        container["command"] = ["python", "-m", "uvicorn", "src.app:create_app"]
    cluster = Cluster([deployment(PRIMARY, image=NEW), reconciler], enrolled=False)
    with pytest.raises(ValueError, match="Unregistered or uncertain application writer"):
        cluster.run()
    assert not cluster.patches


def recovery_pod():
    manifest = yaml.safe_load((ROOT / "modules/gateway/k8s/atlassian-mcp-recovery.yaml").read_text())
    spec = copy.deepcopy(manifest["spec"]["jobTemplate"]["spec"]["template"]["spec"])
    spec["containers"][0]["image"] = OLD
    return {
        "metadata": {
            "name": "atlassian-mcp-recovery-job-pod",
            "namespace": "adp-gateway",
            "uid": "recovery-pod-uid",
            "ownerReferences": [{"kind": "Job", "name": "recovery-job", "uid": "recovery-job-uid", "controller": True}],
        },
        "spec": spec,
        "status": {"phase": "Running"},
    }


def test_active_identity_recovery_job_does_not_block_or_join_application_rollout():
    cluster = Cluster()
    pod = recovery_pod()
    before = copy.deepcopy(pod)
    cluster.data["pods"].append(pod)
    report = cluster.run()
    assert report["status"] == "verified"
    assert {p["deployment"] for p in report["pods"]} == {PRIMARY, CHAT, AUX}
    assert len(cluster.patches) == 2
    assert pod == before
    assert not any(pod["metadata"]["name"] in call for call in cluster.calls if call[0] == "exec")


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(command=["python", "-m", "src.app"]),
        lambda c: c.update(command=["sh", "-c", "python -m src.shared.identity.oauth_setup_worker --once"]),
        lambda c: c.update(args=["unexpected"]),
        lambda c: c.update(image="example.invalid/unknown:v1"),
    ],
)
def test_recovery_job_name_does_not_exempt_unknown_processes(change):
    cluster = Cluster()
    pod = recovery_pod()
    change(pod["spec"]["containers"][0])
    cluster.data["pods"].append(pod)
    with pytest.raises(ValueError, match="Unregistered or uncertain active writer pod"):
        cluster.run()
    assert not cluster.patches


@pytest.mark.parametrize("field", ["containers", "initContainers", "ephemeralContainers"])
def test_recovery_job_does_not_hide_other_active_processes(field):
    cluster = Cluster()
    pod = recovery_pod()
    pod["spec"].setdefault(field, []).append({"name": "unexpected", "image": OLD})
    cluster.data["pods"].append(pod)
    with pytest.raises(ValueError, match="Unregistered or uncertain active writer pod"):
        cluster.run()
    assert not cluster.patches


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "null",
        "[null]",
        '[{"deployment":"x"}]',
        '[{"deployment":"bedrockgateway","uid":"u","container":"x","entrypoint":"gateway"}]',
        '[{"deployment":"x","uid":"u","container":"x","entrypoint":"controller"}]',
        '[{"deployment":"x","uid":"u","container":"x","entrypoint":"gateway","extra":true}]',
        '[{"deployment":"../x","uid":"u","container":"x","entrypoint":"gateway"}]',
        '[{"deployment":"x","uid":3,"container":"x","entrypoint":"gateway"}]',
    ],
)
def test_malformed_or_conflicting_inventory_is_rejected(raw):
    with pytest.raises(ValueError):
        script.bindings(raw)


@pytest.mark.parametrize("same_uid", [False, True])
def test_duplicate_names_or_uids_are_rejected(same_uid):
    first = {"deployment": "one", "uid": "first-uid", "container": "gateway", "entrypoint": "gateway"}
    second = {**first, "deployment": "two"} if same_uid else {**first, "uid": "second-uid"}
    with pytest.raises(ValueError, match="Duplicate"):
        script.bindings(json.dumps([first, second]))


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda d: d["metadata"].update(uid="replacement"), "UID changed"),
        (lambda d: d["spec"]["template"]["spec"].update(serviceAccountName="another"), "service account"),
        (lambda d: d["spec"]["template"]["spec"]["containers"][0].update(command=["python", "-m", "controller.worker"]), "entrypoint"),
        (lambda d: d["spec"]["template"]["spec"]["containers"][0].update(image=REGISTRY + ":latest"), "pinned"),
        (lambda d: d["spec"]["template"]["spec"]["containers"][0].update(image=OLD.replace("123456789012", "999999999999")), "entrypoint"),
    ],
)
def test_enrollment_drift_fails_before_any_patch(change, message):
    cluster = Cluster()
    change(cluster.get(AUX))
    with pytest.raises(ValueError, match=message):
        cluster.run()
    assert not cluster.patches


def test_unregistered_application_fails_before_any_patch():
    cluster = Cluster(enrolled=False)
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.run()
    assert not cluster.patches


@pytest.mark.parametrize("field", ["containers", "initContainers", "ephemeralContainers"])
def test_unknown_active_pod_or_sidecar_is_not_hidden_by_deployment_inventory(field):
    cluster = Cluster()
    rogue = copy.deepcopy(cluster.data["pods"][0])
    rogue["metadata"].update(name="unknown-pod", uid="unknown-uid", ownerReferences=[])
    rogue["spec"][field] = [{"name": "unregistered", "image": OLD}]
    if field != "containers":
        rogue["spec"]["containers"] = [{"name": "ordinary", "image": "example.invalid/other:v1"}]
    cluster.data["pods"].append(rogue)
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.run()
    assert not cluster.patches


@pytest.mark.parametrize(
    "change",
    [lambda d: d["metadata"].update(uid="new-uid"), lambda d: d["metadata"].update(generation=2), lambda d: d["spec"].update(replicas=3)],
)
def test_compare_and_swap_rejects_concurrent_identity_generation_or_spec_change(change):
    cluster = Cluster()
    cluster.before_patch = change
    with pytest.raises(RuntimeError, match="JSON Patch test failed"):
        cluster.run()
    assert not cluster.patches
    assert cluster.get(CHAT)["spec"]["template"]["spec"]["containers"][0]["image"] == OLD


def test_status_update_between_snapshot_and_patch_does_not_abort_image_rollout():
    cluster = Cluster()

    def update_health(dep):
        dep["metadata"]["resourceVersion"] = "new-status-version"
        dep["status"]["conditions"] = [{"type": "Available", "status": "True"}]

    cluster.before_patch = update_health
    report = cluster.run()
    assert report["status"] == "verified"
    assert len(cluster.patches) == 2
    for name in (CHAT, AUX):
        assert cluster.get(name)["spec"]["template"]["spec"]["containers"][0]["image"] == NEW
        assert cluster.get(name)["status"]["conditions"] == [{"type": "Available", "status": "True"}]


def test_readback_rejects_admission_or_concurrent_changes_to_tls_spec():
    cluster = Cluster()
    cluster.after_patch = lambda d: d["spec"]["template"]["spec"].update(volumes=[])
    with pytest.raises(ValueError, match="image-only patch readback"):
        cluster.run()


def test_stale_terminating_writer_prevents_release_success():
    cluster = Cluster()
    cluster.keep_old = True
    with pytest.raises(ValueError, match="old terminating processes"):
        cluster.run()
    assert any(p["metadata"].get("deletionTimestamp") for p in cluster.data["pods"])


def test_mismatched_runtime_guard_hash_prevents_release_success():
    cluster = Cluster()
    cluster.hashes["src/orchestration/work_claims.py"] = "f" * 64
    with pytest.raises(ValueError, match="claim-cleanup source"):
        cluster.run()


def test_verify_only_does_not_fix_drift():
    cluster = Cluster()
    with pytest.raises(ValueError, match="incomplete"):
        cluster.run(verify_only=True)
    assert not cluster.patches


@pytest.mark.parametrize("permission", [("list", "pods"), ("create", "pods/exec"), ("patch", "deployment/" + AUX)])
def test_missing_permissions_are_reported_without_mutation(permission):
    cluster = Cluster()
    cluster.denied = permission
    with pytest.raises(ValueError, match="Release identity lacks"):
        cluster.run()
    assert not cluster.patches


@pytest.mark.parametrize("permission", [("get", "deployments", None), ("patch", "deployment/" + AUX, None), ("get", "configmap/adp-worker-authority-config", None), ("create", "pods", "exec")])
def test_real_kubectl_denial_reports_the_permission_and_recovery(tmp_path, monkeypatch, permission):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text('#!/bin/sh\nprintf "no\\n"\nexit 1\n')
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    writer = script.Writers(namespace="adp-gateway", image=NEW, inventory={}, source_hashes={})
    with pytest.raises(ValueError) as error:
        writer.require_permission(*permission[:2], subresource=permission[2])
    display_resource = permission[1] + ('/' + permission[2] if permission[2] else '')
    assert f"Release identity lacks {permission[0]} {display_resource} in namespace adp-gateway" in str(error.value)
    assert "reconcile the release Role" in str(error.value)
    assert "platform/automation-infra/README.md" in str(error.value)


@pytest.mark.parametrize("stdout,stderr,code", [
    ("", "error: authentication required; private-diagnostic", 1),
    ("", "Unable to connect: private-diagnostic", 1),
    ("no", "error: private-diagnostic", 1),
    ("", "", 1),
    ("no", "", 2),
])
@pytest.mark.parametrize("subresource", [None, "exec"])
def test_kubectl_errors_do_not_claim_rbac_denial_or_leak_diagnostics(tmp_path, monkeypatch, stdout, stderr, code, subresource):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text('#!/bin/sh\nprintf "%s" "$CHECK_STDOUT"\nprintf "%s" "$CHECK_STDERR" >&2\nexit "$CHECK_CODE"\n')
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CHECK_STDOUT", stdout)
    monkeypatch.setenv("CHECK_STDERR", stderr)
    monkeypatch.setenv("CHECK_CODE", str(code))
    writer = script.Writers(namespace="adp-gateway", image=NEW, inventory={}, source_hashes={})
    with pytest.raises(RuntimeError) as error:
        writer.require_permission("get", "pods", subresource=subresource)
    display_resource = "pods/exec" if subresource else "pods"
    assert f"Unable to check release permission: get {display_resource} in namespace adp-gateway" in str(error.value)
    assert "authentication and API connectivity" in str(error.value)
    assert "private-diagnostic" not in str(error.value)
    assert "Release identity lacks" not in str(error.value)
    assert error.value.__suppress_context__


def test_exec_preflight_uses_subresource_without_requiring_pod_creation(tmp_path, monkeypatch):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text('''#!/bin/sh
case "$*" in
  "-n adp-gateway auth can-i get pods --subresource=exec"|"-n adp-gateway auth can-i create pods --subresource=exec") echo yes ;;
  "-n adp-gateway auth can-i get deployments"|"-n adp-gateway auth can-i list deployments"|"-n adp-gateway auth can-i get replicasets"|"-n adp-gateway auth can-i list replicasets"|"-n adp-gateway auth can-i get pods"|"-n adp-gateway auth can-i list pods") echo yes ;;
  *) echo no; exit 1 ;;
esac
''')
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    writer = script.Writers(namespace="adp-gateway", image=NEW, inventory={}, source_hashes={})
    writer.permissions()
    with pytest.raises(ValueError, match="lacks create pods"):
        writer.require_permission("create", "pods")
    with pytest.raises(ValueError, match="lacks create pods/exec"):
        writer.require_permission("create", "pods/exec")


def test_permission_timeout_is_not_reported_as_rbac_denial(monkeypatch):
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 60, output="private-diagnostic")

    monkeypatch.setattr(script.subprocess, "check_output", timeout)
    writer = script.Writers(namespace="adp-gateway", image=NEW, inventory={}, source_hashes={})
    with pytest.raises(RuntimeError, match="Unable to check release permission") as error:
        writer.require_permission("get", "pods")
    assert "private-diagnostic" not in str(error.value)


def test_pod_restart_during_hash_readback_is_not_accepted():
    cluster = Cluster()
    cluster.after_exec = lambda c: c.data["pods"][0]["status"]["containerStatuses"][0].update(containerID="containerd://replacement")
    with pytest.raises(ValueError, match="changed during runtime"):
        cluster.run()


def test_preview_retries_only_proven_pod_turnover():
    cluster = Cluster()

    def turnover(c):
        c.data["pods"][0]["status"]["containerStatuses"][0]["containerID"] = "containerd://replacement"
        raise RuntimeError("pod exited during exec")

    cluster.after_exec = turnover
    report = cluster.run(preview=True)
    assert report["status"] == "preview" and len(report["pods"]) == 3
    assert not cluster.patches
    assert sum(c[0] == "exec" for c in cluster.calls) == 6  # Two complete concurrent inspections.


def test_preview_does_not_retry_stable_exec_failure():
    cluster = Cluster()

    def failure(c):
        raise RuntimeError("permission denied")

    cluster.after_exec = failure
    with pytest.raises(RuntimeError, match="permission denied"):
        cluster.run(preview=True)
    assert sum(c[0] == "exec" for c in cluster.calls) == 3  # One concurrent inspection, no retry.
    assert not cluster.patches


def test_preview_turnover_retry_is_bounded():
    cluster = Cluster()

    def turnover(c):
        c.data["pods"][0]["status"]["containerStatuses"][0]["containerID"] += "x"
        c.after_exec = turnover
        raise RuntimeError("pod exited during exec")

    cluster.after_exec = turnover
    with pytest.raises(script.InventoryTurnoverError):
        cluster.run(preview=True)
    assert sum(c[0] == "exec" for c in cluster.calls) == 9  # Three bounded concurrent inspections.


@pytest.mark.parametrize("surface", ["deployment", "pod"])
def test_gateway_principal_with_renamed_image_cannot_evade_inventory(surface):
    cluster = Cluster()
    rogue = deployment("hidden-writer", image="example.invalid/private/app:v1")
    target = rogue["spec"]["template"]["spec"]["containers"][0]
    target.pop("env")
    target.pop("envFrom")
    if surface == "deployment":
        cluster.data["deployments"].append(rogue)
    else:
        _, pod = children(rogue)
        cluster.data["pods"].append(pod)
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.run()
    assert not cluster.patches


def inject_init(pod, *, state=None, restart_policy=None):
    init = {"name": "opentelemetry-auto-instrumentation", "image": "example.invalid/otel/injector:v1"}
    if restart_policy:
        init["restartPolicy"] = restart_policy
    pod["spec"]["initContainers"] = [init]
    pod["status"]["initContainerStatuses"] = [{"name": init["name"], "state": state or {"running": {"startedAt": "2026-01-01T00:00:00Z"}}}]


def test_completed_injected_init_is_not_an_active_writer():
    cluster = Cluster()
    for pod in cluster.data["pods"]:
        inject_init(pod, state={"terminated": {"exitCode": 0}})
    assert len(cluster.run(preview=True)["pods"]) == 3
    assert not cluster.patches


@pytest.mark.parametrize(
    "state,restart_policy",
    [({"running": {}}, None), ({"waiting": {}}, None), ({"terminated": {"exitCode": 1}}, None), ({"terminated": {"exitCode": 0}}, "Always")],
)
def test_active_or_restarting_init_never_passes_the_initial_inventory(state, restart_policy):
    cluster = Cluster()
    inject_init(cluster.data["pods"][0], state=state, restart_policy=restart_policy)
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.run()
    assert not cluster.patches


def test_completed_ephemeral_process_is_excluded_but_running_process_is_not():
    cluster = Cluster()
    pod = cluster.data["pods"][0]
    pod["spec"]["ephemeralContainers"] = [{"name": "debug", "image": "example.invalid/debug:v1"}]
    pod["status"]["ephemeralContainerStatuses"] = [{"name": "debug", "state": {"terminated": {"exitCode": 1}}}]
    assert len(cluster.run(preview=True)["pods"]) == 3
    pod["status"]["ephemeralContainerStatuses"][0]["state"] = {"running": {}}
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.run(preview=True)


def pending_init_after_patch(cluster, *, restart_policy=None):
    def inject(dep):
        name = dep["metadata"]["name"]
        pod = next(p for p in cluster.data["pods"] if p["metadata"]["name"] == name + "-pod")
        inject_init(pod, restart_policy=restart_policy)
        pod["status"]["phase"] = "Pending"
        pod["status"]["containerStatuses"][0]["ready"] = False

    return inject


def test_full_apply_waits_for_ordinary_injected_init_then_verifies_new_processes(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10
    cluster.after_patch = pending_init_after_patch(cluster)
    waited = []

    def complete_init(seconds):
        waited.append(seconds)
        assert len(cluster.patches) == 2
        assert not any(c[0] == "exec" for c in cluster.calls)
        for pod in cluster.data["pods"]:
            if "initContainerStatuses" in pod["status"]:
                pod["status"]["initContainerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}
                pod["status"]["phase"] = "Running"
                pod["status"]["containerStatuses"][0]["ready"] = True

    monkeypatch.setattr(script.time, "sleep", complete_init)
    report = cluster.run()
    assert report["status"] == "verified" and len(report["pods"]) == 3
    assert len(waited) == 1


def test_full_apply_refuses_ordinary_init_still_active_at_deadline():
    cluster = Cluster()
    cluster.after_patch = pending_init_after_patch(cluster)
    with pytest.raises(script.WriterStartupPendingError, match="initialization is still active"):
        cluster.run()
    assert len(cluster.patches) == 2
    assert not any(c[0] == "exec" for c in cluster.calls)


def test_full_apply_never_treats_unknown_native_sidecar_as_waitable_ordinary_init(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10
    cluster.after_patch = pending_init_after_patch(cluster, restart_policy="Always")
    monkeypatch.setattr(script.time, "sleep", lambda _: pytest.fail("Must not wait on an unknown native sidecar"))
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.run()


def test_starting_init_requires_enrolled_deployment_owner():
    cluster = Cluster()
    pod = cluster.data["pods"][0]
    pod["metadata"]["ownerReferences"] = []
    pod["status"]["phase"] = "Pending"
    inject_init(pod)
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.inspect(cluster.snapshot(), allow_starting_init=True)


@pytest.mark.parametrize("surface", ["later-pod", "same-pod-sidecar"])
def test_starting_init_does_not_mask_other_unknown_active_writers(surface):
    cluster = Cluster()
    pod = cluster.data["pods"][0]
    pod["status"]["phase"] = "Pending"
    inject_init(pod)
    if surface == "later-pod":
        rogue = copy.deepcopy(pod)
        rogue["metadata"].update(name="unknown-later-pod", uid="unknown-uid", ownerReferences=[])
        cluster.data["pods"].append(rogue)
    else:
        pod["spec"]["initContainers"].append({"name": "unknown-native-sidecar", "image": OLD, "restartPolicy": "Always"})
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.inspect(cluster.snapshot(), allow_starting_init=True)


def mark_pending_init(pod):
    inject_init(pod)
    pod["status"]["phase"] = "Pending"
    for status in pod["status"]["containerStatuses"]:
        status.update(ready=False, state={"waiting": {"reason": "PodInitializing"}})


def test_orphan_controller_init_is_not_waitable_even_when_main_container_is_excluded(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10

    def append_orphan(dep):
        if dep["metadata"]["name"] == AUX:
            orphan = copy.deepcopy(cluster.data["pods"][-1])  # The controller process itself is an explicit nonwriter.
            orphan["metadata"].update(name="orphan-controller", uid="orphan-uid", ownerReferences=[])
            mark_pending_init(orphan)
            cluster.data["pods"].append(orphan)

    cluster.after_patch = append_orphan
    monkeypatch.setattr(script.time, "sleep", lambda _: pytest.fail("Must not wait on orphan init"))
    with pytest.raises(ValueError, match="Unregistered") as raised:
        cluster.run()
    assert not isinstance(raised.value, script.WriterStartupPendingError)


def test_final_runtime_observation_retries_init_turnover_only_after_completion(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10
    cluster.after_exec = lambda c: mark_pending_init(c.data["pods"][0])
    sleeps = []

    def finish(seconds):
        sleeps.append(seconds)
        assert sum(call[0] == "exec" for call in cluster.calls) == 3
        pod = cluster.data["pods"][0]
        pod["status"]["initContainerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}
        pod["status"]["phase"] = "Running"
        for status in pod["status"]["containerStatuses"]:
            status.update(ready=True, state={"running": {}})

    monkeypatch.setattr(script.time, "sleep", finish)
    assert cluster.run()["status"] == "verified"
    assert len(sleeps) == 1
    assert sum(call[0] == "exec" for call in cluster.calls) == 6  # Every runtime is independently observed again.


def test_verify_only_refuses_init_appearing_during_final_observation_without_waiting(monkeypatch):
    cluster = Cluster()
    cluster.run()
    cluster.calls.clear()
    cluster.patches.clear()
    cluster.after_exec = lambda c: mark_pending_init(c.data["pods"][0])
    monkeypatch.setattr(script.time, "sleep", lambda _: pytest.fail("Verify-only cannot wait on newly observed init"))
    with pytest.raises(script.WriterStartupPendingError):
        cluster.run(verify_only=True)
    assert not cluster.patches


def test_native_sidecar_appearing_during_final_observation_cannot_pass_even_with_zero_exit(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10

    def restart(c):
        pod = c.data["pods"][0]
        mark_pending_init(pod)
        pod["spec"]["initContainers"][0]["restartPolicy"] = "Always"
        pod["status"]["initContainerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}

    cluster.after_exec = restart
    monkeypatch.setattr(script.time, "sleep", lambda _: pytest.fail("Must not wait on an unknown native sidecar"))
    with pytest.raises(ValueError, match="Unregistered") as raised:
        cluster.run()
    assert not isinstance(raised.value, script.WriterStartupPendingError)


def test_workflow_uses_maintained_helper_and_pinned_digest_before_release_receipts():
    source = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
    doc = yaml.safe_load(source)
    backend = doc["jobs"]["deploy-backend"]
    assert "vars.ADP_GATEWAY_WRITER_INVENTORY" in backend["env"]["ADP_GATEWAY_WRITER_INVENTORY"]
    steps = backend["steps"]
    names = [s.get("name") for s in steps]
    load = next(s for s in steps if s.get("name") == "Load maintained deployment guards")
    assert "$ADP_WORKFLOW_REVISION:modules/gateway/scripts/sync-gateway-writers.py" in load["run"]
    for name in ("Converge registered auxiliary gateway writers", "Verify every active gateway writer before release evidence"):
        step = next(s for s in steps if s.get("name") == name)
        assert "@${{ steps.release.outputs.digest }}" in step["env"]["WRITER_IMAGE"]
        assert '--gateway-root modules/gateway --source-revision "$WRITER_SOURCE"' in step["run"]
        assert names.index(name) < names.index("Record gateway-backend build evidence")
    assert names.index("Converge registered auxiliary gateway writers") < names.index("Update the scheduled orchestration engine")
    assert "--verify-only" in next(s for s in steps if s.get("name") == "Verify every active gateway writer before release evidence")["run"]
    assert "sync-gateway-writers" in source.split('echo "backend=$BACKEND"')[0]


def test_runtime_probe_hashes_real_bytes_and_exposes_only_allowlisted_fields(tmp_path):
    for relative in script.SOURCE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source: " + relative)
    probe = script.RUNTIME_READ.replace("Path('/app')", "Path(" + repr(str(tmp_path)) + ")")
    env = {**os.environ, "AGENT_AUTHORITY_ENABLED": "true", "ADP_WORK_CLAIMS_ENABLED": "false", "PRIVATE_TEST_TOKEN": "must-not-appear"}
    raw = subprocess.check_output([sys.executable, "-S", "-c", probe], env=env, text=True)
    result = json.loads(raw)
    assert set(result) == {"authority_enabled", "claims_enabled", "source_hashes"}
    assert result["authority_enabled"] is True and result["claims_enabled"] is False
    assert result["source_hashes"] == {p: hashlib.sha256((tmp_path / p).read_bytes()).hexdigest() for p in script.SOURCE_FILES}
    assert "must-not-appear" not in raw


def commit_fixture(gateway_root):
    checkout = gateway_root.parents[1]
    subprocess.check_call(["git", "-C", str(checkout), "add", "modules/gateway"], stdout=subprocess.DEVNULL)
    subprocess.check_call(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=WriterTests",
            "-c",
            "user.email=writer@example.invalid",
            "commit",
            "-qm",
            "fixture source",
        ],
        stdout=subprocess.DEVNULL,
    )
    return subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()


@pytest.fixture
def cli(monkeypatch, tmp_path):
    checkout = tmp_path
    subprocess.check_call(["git", "init", "-q", str(checkout)])
    tmp_path = checkout / "modules/gateway"
    for relative in script.SOURCE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# selected source\n")
    (tmp_path / "src/orchestration/work_claims.py").write_text(
        "async def _unfinished_bound_review(): pass\nasync def release_work(): await _unfinished_bound_review()\n"
    )
    revision = commit_fixture(tmp_path)
    args = [
        "writers.py",
        "--namespace",
        "adp-gateway",
        "--account",
        "123456789012",
        "--region",
        "us-east-1",
        "--cluster",
        "test-cluster",
        "--image",
        NEW,
        "--gateway-root",
        str(tmp_path),
        "--source-revision",
        revision,
        "--report",
        str(tmp_path / "report.json"),
    ]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.delenv("ADP_GATEWAY_WRITER_INVENTORY", raising=False)
    calls = []

    def command(argv):
        calls.append(argv)
        if argv[:2] == ["aws", "sts"]:
            return json.dumps({"Account": "123456789012"})
        if argv[:3] == ["kubectl", "config", "view"]:
            return "arn:aws:eks:us-east-1:123456789012:cluster/test-cluster"
        if argv[0] == "git":
            return subprocess.check_output(argv, text=True).strip()
        raise AssertionError("Unexpected external command")

    monkeypatch.setattr(script, "command", command)
    return tmp_path, args, calls


@pytest.mark.parametrize(
    "guard", ["async def release_work(): pass\n", "async def _unfinished_bound_review(): pass\nasync def release_work(): pass\n"]
)
def test_cli_refuses_source_without_called_delivery_retention_guard(cli, guard):
    path, args, calls = cli
    (path / "src/orchestration/work_claims.py").write_text(guard)
    args[args.index("--source-revision") + 1] = commit_fixture(path)
    with pytest.raises(ValueError, match="claim-retention guard"):
        script.main()
    assert all(c[0] == "git" or c[:2] in (["aws", "sts"], ["kubectl", "config"]) for c in calls)
    assert not (path / "report.json").exists()


@pytest.mark.parametrize("field,value", [("--account", "999999999999"), ("--cluster", "another-cluster"), ("--image", REGISTRY + ":latest")])
def test_cli_refuses_wrong_target_and_mutable_image_before_kubernetes_access(cli, field, value):
    _, args, calls = cli
    args[args.index(field) + 1] = value
    with pytest.raises((ValueError, SystemExit)):
        script.main()
    assert all(c[:2] in (["aws", "sts"], ["kubectl", "config"]) for c in calls)


def test_cli_private_report_is_bound_to_selected_source_and_exact_local_hashes(cli, monkeypatch):
    path, args, _ = cli
    captured = {}

    def run(self, **kwargs):
        if self.source_hashes is not None:
            captured.update(self.source_hashes)
        return {"status": "verified", "deployments": [], "pods": []}

    monkeypatch.setattr(script.Writers, "run", run)
    script.main()
    report = path / "report.json"
    assert report.stat().st_mode & 0o777 == 0o600
    assert json.loads(report.read_text())["source_revision"] == args[args.index("--source-revision") + 1]
    assert captured == {p: hashlib.sha256((path / p).read_bytes()).hexdigest() for p in script.SOURCE_FILES}


@pytest.mark.parametrize("dirty", [True, False])
def test_cli_refuses_dirty_or_fabricated_source_before_kubernetes_access(cli, dirty):
    path, args, calls = cli
    if dirty:
        (path / "src/orchestration/work_claims.py").write_text("# altered source\n")
    else:
        args[args.index("--source-revision") + 1] = "f" * 40
    with pytest.raises(ValueError, match="source"):
        script.main()
    assert all(c[0] == "git" or c[:2] in (["aws", "sts"], ["kubectl", "config"]) for c in calls)
    assert not (path / "report.json").exists()


class VerificationClock:
    def __init__(self, monkeypatch, advance=None):
        self.elapsed, self.sleeps, self.advance = 0, [], advance
        monkeypatch.setattr(script.time, "monotonic", lambda: self.elapsed)
        monkeypatch.setattr(script.time, "sleep", self.sleep)

    def sleep(self, seconds):
        assert 0 < seconds <= 5
        self.elapsed += seconds
        self.sleeps.append(seconds)
        if self.advance:
            self.advance()


def ready_read_only_cluster():
    cluster = Cluster()
    cluster.run()
    cluster.patches.clear()
    cluster.calls.clear()
    cluster.timeout = 10
    return cluster


def hpa_startup(cluster, *, init=False):
    primary = cluster.get(PRIMARY)
    primary["spec"]["replicas"] = 2
    primary["metadata"]["generation"] = 2
    primary["status"].update(observedGeneration=2, updatedReplicas=2, readyReplicas=1, availableReplicas=1)
    pod = copy.deepcopy(cluster.data["pods"][0])
    pod["metadata"].update(name="hpa-added-pod", uid="hpa-added-uid")
    pod["status"]["phase"] = "Pending"
    pod["status"]["containerStatuses"][0].update(ready=False, imageID="", containerID="", state={"waiting": {"reason": "PodInitializing"}})
    if init:
        inject_init(pod)
    cluster.data["pods"].append(pod)
    return pod


def finish_hpa_startup(cluster, pod):
    if pod["status"].get("initContainerStatuses"):
        pod["status"]["initContainerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}
    pod["status"]["phase"] = "Running"
    pod["status"]["containerStatuses"][0].update(ready=True, imageID=NEW, containerID="containerd://hpa-added", state={"running": {}})
    cluster.get(PRIMARY)["status"].update(readyReplicas=2, availableReplicas=2)


def assert_verification_was_read_only(cluster):
    assert not cluster.patches
    assert all(call[0] in {"auth", "get", "exec"} for call in cluster.calls)
    assert not any(call[:3] == ("auth", "can-i", "patch") for call in cluster.calls)


@pytest.mark.parametrize("initial_init", [False, True])
def test_verify_only_waits_for_hpa_startup_without_patching(monkeypatch, initial_init):
    cluster = ready_read_only_cluster()
    pod = hpa_startup(cluster, init=initial_init)
    clock = VerificationClock(monkeypatch, lambda: finish_hpa_startup(cluster, pod))
    result = cluster.run(verify_only=True)
    assert result["status"] == "verified" and len(result["pods"]) == 4
    assert clock.sleeps == [5]
    assert_verification_was_read_only(cluster)


def test_verify_only_reobserves_all_sources_after_late_hpa_init(monkeypatch):
    cluster = ready_read_only_cluster()
    pending = []
    cluster.after_exec = lambda c: pending.append(hpa_startup(c, init=True))
    clock = VerificationClock(monkeypatch, lambda: finish_hpa_startup(cluster, pending[0]))
    result = cluster.run(verify_only=True)
    assert result["status"] == "verified" and len(result["pods"]) == 4
    assert clock.sleeps == [5]
    assert sum(call[0] == "exec" for call in cluster.calls) == 7  # Three old observations discarded; four complete fresh reads.
    assert_verification_was_read_only(cluster)


def test_verify_only_reobserves_after_proven_process_turnover(monkeypatch):
    cluster = ready_read_only_cluster()
    cluster.after_exec = lambda c: c.data["pods"][0]["status"]["containerStatuses"][0].update(containerID="containerd://replacement")
    clock = VerificationClock(monkeypatch)
    assert cluster.run(verify_only=True)["status"] == "verified"
    assert clock.sleeps == [5]
    assert sum(call[0] == "exec" for call in cluster.calls) == 6
    assert_verification_was_read_only(cluster)


def test_verify_only_uses_one_deadline_across_initial_init_and_later_unready_state(monkeypatch):
    cluster = ready_read_only_cluster()
    pod = hpa_startup(cluster, init=True)

    def finish_init_only():
        pod["status"]["initContainerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}

    clock = VerificationClock(monkeypatch, finish_init_only)
    with pytest.raises(ValueError, match="release is incomplete"):
        cluster.run(verify_only=True)
    assert clock.elapsed == 10 and clock.sleeps == [5, 5]
    assert not any(call[0] == "exec" for call in cluster.calls)
    assert_verification_was_read_only(cluster)


def test_verify_only_initial_init_deadline_cannot_attest_unfinished_processes(monkeypatch):
    cluster = ready_read_only_cluster()
    hpa_startup(cluster, init=True)
    clock = VerificationClock(monkeypatch)
    with pytest.raises(script.WriterStartupPendingError):
        cluster.run(verify_only=True)
    assert clock.elapsed == 10
    assert not any(call[0] == "exec" for call in cluster.calls)
    assert_verification_was_read_only(cluster)


def test_verify_only_pins_primary_uid_before_waiting_for_initial_init(monkeypatch):
    cluster = ready_read_only_cluster()
    pod = hpa_startup(cluster, init=True)

    def replace_primary():
        finish_hpa_startup(cluster, pod)
        cluster.get(PRIMARY)["metadata"]["uid"] = "replacement-primary"
        cluster.data["replicasets"][0]["metadata"]["ownerReferences"][0]["uid"] = "replacement-primary"

    clock = VerificationClock(monkeypatch, replace_primary)
    with pytest.raises(ValueError, match="inventory changed"):
        cluster.run(verify_only=True)
    assert clock.elapsed == 5
    assert_verification_was_read_only(cluster)


@pytest.mark.parametrize("problem", ["unknown", "source", "permission", "exec"])
def test_verify_only_does_not_retry_hard_failures(monkeypatch, problem):
    cluster = ready_read_only_cluster()
    if problem == "unknown":
        hpa_startup(cluster, init=True)
        cluster.data["pods"][-1]["spec"]["containers"].append({"name": "unknown-active-writer", "image": NEW})
    elif problem == "source":
        cluster.hashes["src/orchestration/work_claims.py"] = "f" * 64
    elif problem == "permission":
        cluster.denied = ("create", "pods/exec")
    else:

        def denied(_):
            raise RuntimeError("stable exec failure")

        cluster.after_exec = denied
    clock = VerificationClock(monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        cluster.run(verify_only=True)
    assert clock.elapsed == 0 and not clock.sleeps
    assert_verification_was_read_only(cluster)


def test_verify_only_never_accepts_wrong_image_at_deadline(monkeypatch):
    cluster = ready_read_only_cluster()
    cluster.get(CHAT)["spec"]["template"]["spec"]["containers"][0]["image"] = OLD
    clock = VerificationClock(monkeypatch)
    with pytest.raises(ValueError, match="release is incomplete"):
        cluster.run(verify_only=True)
    assert clock.elapsed == 10
    assert not any(call[0] == "exec" for call in cluster.calls)
    assert_verification_was_read_only(cluster)


def test_verify_only_zero_timeout_keeps_one_immediate_attempt(monkeypatch):
    cluster = ready_read_only_cluster()
    cluster.timeout = 0
    hpa_startup(cluster)
    clock = VerificationClock(monkeypatch)
    with pytest.raises(ValueError, match="release is incomplete"):
        cluster.run(verify_only=True)
    assert clock.elapsed == 0 and not clock.sleeps
    assert_verification_was_read_only(cluster)


def initial_chat_init(cluster):
    pod = next(p for p in cluster.data["pods"] if p["metadata"]["name"] == CHAT + "-pod")
    inject_init(pod)
    pod["status"]["phase"] = "Pending"
    pod["status"]["containerStatuses"][0].update(ready=False, state={"waiting": {"reason": "PodInitializing"}})
    return pod


def test_apply_waits_for_initial_chat_instrumentation_before_patching(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10
    pod = initial_chat_init(cluster)

    def finish_init():
        assert not cluster.patches
        pod["status"]["initContainerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}
        pod["status"]["phase"] = "Running"
        pod["status"]["containerStatuses"][0].update(ready=True, state={"running": {}})

    clock = VerificationClock(monkeypatch, finish_init)
    result = cluster.run()
    assert result["status"] == "verified" and len(result["pods"]) == 3
    assert len(cluster.patches) == 2 and clock.sleeps == [5]
    assert all(p["source_hashes"] == HASHES for p in result["pods"])


def test_apply_initial_init_timeout_never_patches_or_attests(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10
    initial_chat_init(cluster)
    clock = VerificationClock(monkeypatch)
    with pytest.raises(script.WriterStartupPendingError):
        cluster.run()
    assert clock.elapsed == 10 and clock.sleeps == [5, 5]
    assert not cluster.patches and not any(c[0] == "exec" for c in cluster.calls)


def test_apply_shares_one_deadline_between_initial_init_and_rollout(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10
    pod = initial_chat_init(cluster)

    def finish_only_init():
        pod["status"]["initContainerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}

    clock = VerificationClock(monkeypatch, finish_only_init)
    with pytest.raises(ValueError, match="release is incomplete"):
        cluster.run()
    assert clock.elapsed == 10 and clock.sleeps == [5, 5]
    assert len(cluster.patches) == 2
    assert not any(c[0] == "exec" for c in cluster.calls)


def test_apply_rejects_changed_owner_before_patching_after_init_wait(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10
    initial_chat_init(cluster)

    def replace_chat():
        cluster.get(CHAT)["metadata"]["uid"] = "replacement-chat"

    clock = VerificationClock(monkeypatch, replace_chat)
    with pytest.raises(ValueError, match="inventory changed"):
        cluster.run()
    assert clock.elapsed == 5 and not cluster.patches


def test_apply_initial_init_does_not_delay_refusal_of_unknown_writer(monkeypatch):
    cluster = Cluster()
    cluster.timeout = 10
    pod = initial_chat_init(cluster)
    pod["spec"]["containers"].append({"name": "unknown-active-writer", "image": NEW})
    clock = VerificationClock(monkeypatch)
    with pytest.raises(ValueError, match="Unregistered"):
        cluster.run()
    assert not clock.sleeps and not cluster.patches


def test_runtime_probes_overlap_but_join_before_inventory_recheck(monkeypatch):
    import threading

    cluster = ready_read_only_cluster()
    managed, pods = cluster.inspect(cluster.snapshot())
    barrier = threading.Barrier(len(pods))
    original = cluster.observe
    completed = []

    def observe(*args):
        barrier.wait(timeout=5)
        result = original(*args)
        completed.append(result["pod_uid"])
        return result

    original_snapshot = cluster.snapshot

    def snapshot():
        assert len(completed) == len(pods)
        return original_snapshot()

    monkeypatch.setattr(cluster, "observe", observe)
    monkeypatch.setattr(cluster, "snapshot", snapshot)
    observations = cluster.observe_stable(managed, pods)
    assert [row["pod_uid"] for row in observations] == [
        pod["metadata"]["uid"] for _, pod, _ in pods
    ]
