"""Real control and artifact adapters over simulated Kubernetes, S3 and Product boundaries."""

import base64
import io
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from l5_services.adapters.transform.artifacts import TransformArtifacts
from l5_services.adapters.transform.authority import TransformWorkAuthority
from l5_services.adapters.transform.check_control import TransformCheckControl
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.pod_control import TransformPodControl
from l5_services.adapters.transform.provenance import compare_source_trees
from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE, finite_provider_job
from l5_services.adapters.transform.source import ProductSealedSource
from l5_services.adapters.transform.verification import DetachedCheckPending
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION
from l5_services.workers.execution import AuthorityRevoked
from test_executor_source import fixture as source_fixture
from test_provider_job import admitted


NODE_UID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
IMAGE = "example.invalid/transform@sha256:" + "b" * 64


class S3Boundary:
    def __init__(self):
        self.objects = {}

    def put_object(self, **request):
        if request.get("IfNoneMatch") == "*" and any(key == request["Key"] for key, _ in self.objects):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        version = str(len(self.objects) + 1)
        self.objects[(request["Key"], version)] = request
        return {"VersionId": version}

    def get_object(self, **request):
        version = request.get("VersionId") or [version for key, version in self.objects if key == request["Key"]][-1]
        saved = self.objects[(request["Key"], version)]
        return {"VersionId": version, "Body": io.BytesIO(saved["Body"]),
                "ContentLength": len(saved["Body"]), "Metadata": saved["Metadata"]}

    def list_objects_v2(self, **request):
        return {"Contents": [{"Key": key} for key in sorted({key for key, _ in self.objects})
                             if key.startswith(request["Prefix"])][:request["MaxKeys"]]}


class KubernetesBoundary:
    def __init__(self):
        self.objects, self.logs, self.calls = {}, {}, []
        self.lost_create = False

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if path == "/api/v1/nodes/isolated-example":
            return {"metadata": {"uid": NODE_UID, "labels": {"adp.dev/transform-isolation": "v1"}}}
        if "/networkpolicies/deny-all" in path:
            return {"spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}
        if path.endswith("/log"):
            return self.logs.get(path)
        if method == "POST":
            body = deepcopy(kwargs["body"])
            key = path + "/" + body["metadata"]["name"]
            assert key not in self.objects, "duplicate Kubernetes create"
            body["metadata"]["uid"] = "uid-" + str(len(self.objects))
            body["metadata"]["resourceVersion"] = "1"
            self.objects[key] = body
            if self.lost_create and body["kind"] == "Pod":
                self.lost_create = False
                raise TimeoutError("synthetic lost create acknowledgement")
            return deepcopy(body)
        if method == "PATCH":
            pod = self.objects[path]
            assert kwargs["body"][0]["value"] == pod["metadata"]["uid"]
            assert kwargs["body"][1]["value"] == pod["metadata"]["resourceVersion"]
            change = kwargs["body"][2]
            if change["op"] == "replace" and change["path"] == "/data/next_chunk":
                pod["data"]["next_chunk"] = change["value"]
            else:
                index = int(change["path"].rsplit("/", 1)[1])
                pod["metadata"]["finalizers"].pop(index)
            pod["metadata"]["resourceVersion"] = str(int(pod["metadata"]["resourceVersion"]) + 1)
            return deepcopy(pod)
        if method == "DELETE":
            resource = self.objects.get(path)
            if resource is not None:
                assert kwargs["body"]["preconditions"]["uid"] == resource["metadata"]["uid"]
                if not resource["metadata"].get("finalizers"):
                    del self.objects[path]
            return deepcopy(resource)
        return deepcopy(self.objects.get(path))


def prepared(*, source_contents=b"original\n"):
    client, manifest, projection_authority, contents = source_fixture(source_contents)
    run, _, attempt = admitted()
    run.admission = deepcopy(run.admission)
    run.input = {**run.input, "sources": manifest["sources"]}
    run.admission["source_snapshots"] = [{"snapshot_id": "sealed-a"}]
    run.admission["transform_settings"]["write_paths"] = ["src/example.py"]
    qualification = run.admission["transform_qualification"]
    qualification.update(interface=NDJSON_INTERFACE, cli_version="3.19.0", binary_sha256=NDJSON_BINARY_SHA256)
    digest = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    token = sha256(f"{run.tenant_id}:{run.run_id}:{TRANSFORM_SUBMIT_OPERATION}:{digest}".encode()).hexdigest()
    attempt = replace(attempt, input_hash=digest, idempotency_token=token)
    intent = TransformAdapter(object())._intent(run, attempt)
    intent["provider_job"] = finite_provider_job(intent, manifest, qualification, run.admission["limits"])
    authority = TransformWorkAuthority(scope=manifest["scope"], source_request=projection_authority.source_request,
        capture_limits=projection_authority.capture_limits, credentials={"access_key_id": "fixture-access",
            "secret_access_key": "fixture-secret", "session_token": "fixture-token"},
        identity={"region": "us-east-1"}, expires_at=datetime.now(UTC) + timedelta(minutes=15),
        clock=lambda: datetime.now(UTC), provider_deadline_seconds=intent["provider_job"]["active_deadline_seconds"])
    api, s3 = KubernetesBoundary(), S3Boundary()
    artifacts = TransformArtifacts(s3, "example-artifacts")
    control = TransformPodControl(api, namespace="transform-provider", node_name="isolated-example",
                                  node_uid=NODE_UID, source=ProductSealedSource(client), artifacts=artifacts,
                                  provider_image=IMAGE)
    return control, api, artifacts, intent, manifest, authority, contents


def observation(intent, state="running", **extra):
    return json.dumps({key: value for key, value in intent.items() if key != "provider_job"} | {
        "kind": "transform-provider-observation-v1", "readback": {
            "native_id": "native-a", "attempt_token": intent["attempt_token"], "input_hash": intent["input_hash"],
        "state": state}, **extra}).encode() + b"\n"


def mark_terminal(pod, exit_code=0):
    pod["status"] = {"containerStatuses": [{"name": "provider", "imageID": IMAGE,
        "state": {"terminated": {"exitCode": exit_code, "reason": "Completed"}}}]}


@pytest.mark.asyncio
async def test_launch_poll_and_collect_keep_original_patch_and_native_identity():
    control, api, artifacts, intent, manifest, authority, _ = prepared()
    initial = await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    assert initial["readback"] is None
    pod_path = f"/api/v1/namespaces/transform-provider/pods/{intent['executor_name']}"
    pod = api.objects[pod_path]
    assert pod["spec"]["restartPolicy"] == "Never"
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert not any("host" in key.lower() for key in pod["spec"])
    assert pod["spec"]["containers"][0]["image"] == IMAGE
    assert "fixture-secret" not in json.dumps(pod)
    api.logs[pod_path + "/log"] = observation(intent)
    read = await control.read_provider(intent=intent, authority=authority)
    assert read["readback"]["native_id"] == "native-a"
    patch = b"diff --git a/src/example.py b/src/example.py\n--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-original\n+changed\n"
    api.logs[pod_path + "/log"] += observation(intent, "completed", patch=base64.b64encode(patch).decode(),
        candidate_files={"src/example.py": base64.b64encode(b"changed\n").decode()},
        candidate_modes={"src/example.py": "100644"})
    assert await control.collect_provider(intent=intent, authority=authority) is None
    assert pod_path in api.objects
    mark_terminal(pod)
    collected = await control.collect_provider(intent=intent, authority=authority)
    assert collected["native_job_ref"]["executor_uid"] == pod["metadata"]["uid"]
    assert collected["native_job_ref"]["native_id"] == "native-a"
    assert await artifacts.read(reference=collected["artifacts"]["original_patch"], authority=authority) == patch
    reference = collected["artifacts"]["original_patch"]
    assert "/" not in reference["artifact_id"]
    encoded = reference["artifact_id"].removeprefix("tf-artifact-")
    legacy = {**reference, "artifact_id": base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()}
    assert await artifacts.read(reference=legacy, authority=authority) == patch
    assert api.objects == {}
    recovered = await control.read_provider(intent=intent, authority=authority)
    assert recovered["readback"]["native_id"] == "native-a"
    assert recovered["executor_uid"] == pod["metadata"]["uid"]
    assert await control.collect_provider(intent=intent, authority=authority) == collected
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/pods")]) == 1


@pytest.mark.asyncio
async def test_retained_completion_recovers_interrupted_cleanup_without_relaunch():
    control, api, artifacts, intent, manifest, authority, _ = prepared()
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    path = f"/api/v1/namespaces/transform-provider/pods/{intent['executor_name']}"
    mark_terminal(api.objects[path])
    api.logs[path + "/log"] = observation(intent, "completed", patch="", candidate_files={}, candidate_modes={})
    request = api.request

    def lose_delete(method, resource_path, **kwargs):
        result = request(method, resource_path, **kwargs)
        if method == "DELETE" and resource_path == path:
            raise TimeoutError("lost delete acknowledgement")
        return result

    api.request = lose_delete
    with pytest.raises(TimeoutError):
        await control.collect_provider(intent=intent, authority=authority)
    assert path not in api.objects and api.objects
    assert await artifacts.read_checkpoint(name=intent["executor_name"], authority=authority) is not None
    api.request = request
    assert (await control.read_provider(intent=intent, authority=authority))["readback"]["state"] == "completed"
    assert api.objects == {}
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/pods")]) == 1


@pytest.mark.asyncio
async def test_cleanup_refuses_a_replaced_resource_uid():
    control, api, _, intent, manifest, authority, _ = prepared()
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    path = f"/api/v1/namespaces/transform-provider/pods/{intent['executor_name']}"
    mark_terminal(api.objects[path])
    api.logs[path + "/log"] = observation(intent, "completed", patch="", candidate_files={}, candidate_modes={})
    request = api.request
    secret_path = path.replace("/pods/", "/secrets/")

    def replace_secret(method, resource_path, **kwargs):
        result = request(method, resource_path, **kwargs)
        if method == "DELETE" and resource_path == path:
            api.objects[secret_path]["metadata"]["uid"] = "foreign-replacement"
        return result

    api.request = replace_secret
    with pytest.raises(AuthorityRevoked, match="UID or owner"):
        await control.collect_provider(intent=intent, authority=authority)
    assert api.objects[secret_path]["metadata"]["uid"] == "foreign-replacement"
    assert not any(method == "DELETE" and resource_path == secret_path for method, resource_path, _ in api.calls)


@pytest.mark.asyncio
async def test_failed_provider_exit_cannot_be_archived_as_success_or_deleted():
    control, api, artifacts, intent, manifest, authority, _ = prepared()
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    path = f"/api/v1/namespaces/transform-provider/pods/{intent['executor_name']}"
    mark_terminal(api.objects[path], exit_code=1)
    api.logs[path + "/log"] = observation(intent, "completed", patch="")
    with pytest.raises(ValueError, match="exited unsuccessfully"):
        await control.collect_provider(intent=intent, authority=authority)
    assert path in api.objects
    assert artifacts.client.objects == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failed", "stopped"])
async def test_definitive_native_failure_reclaims_owned_inputs_and_cannot_relaunch(state):
    control, api, _, intent, manifest, authority, _ = prepared()
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    path = f"/api/v1/namespaces/transform-provider/pods/{intent['executor_name']}"
    mark_terminal(api.objects[path], exit_code=1)
    api.logs[path + "/log"] = observation(intent, state)
    assert (await control.read_provider(intent=intent, authority=authority))["readback"]["state"] == state
    assert api.objects == {}
    assert await control.collect_provider(intent=intent, authority=authority) is None
    assert (await control.launch_provider(intent=intent, manifest=manifest, authority=authority))["readback"]["state"] == state
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/pods")]) == 1


@pytest.mark.asyncio
async def test_terminal_pod_without_definitive_native_outcome_is_not_cleaned():
    control, api, _, intent, manifest, authority, _ = prepared()
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    path = f"/api/v1/namespaces/transform-provider/pods/{intent['executor_name']}"
    mark_terminal(api.objects[path], exit_code=1)
    api.logs[path + "/log"] = observation(intent, "running")
    assert (await control.read_provider(intent=intent, authority=authority))["readback"]["state"] == "running"
    assert path in api.objects
    assert not any(method == "DELETE" for method, _, _ in api.calls)


@pytest.mark.asyncio
async def test_lost_create_acknowledgement_is_read_without_a_second_launch():
    control, api, _, intent, manifest, authority, _ = prepared()
    api.lost_create = True
    with pytest.raises(TimeoutError):
        await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/pods")]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["scope", "fence", "expiry", "image"])
async def test_invalid_launch_has_no_cluster_writes(mutation):
    control, api, _, intent, manifest, authority, _ = prepared()
    if mutation == "scope":
        authority.scope = {**authority.scope, "product_id": "foreign"}
    elif mutation == "fence":
        authority.source_request["lease_fence"] = 0
    elif mutation == "expiry":
        authority.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        intent["image_digest"] = "sha256:" + "f" * 64
    with pytest.raises(AuthorityRevoked):
        await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    assert not any(call[0] == "POST" for call in api.calls)


@pytest.mark.asyncio
async def test_foreign_native_output_cannot_be_collected():
    control, api, _, intent, manifest, authority, _ = prepared()
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    path = f"/api/v1/namespaces/transform-provider/pods/{intent['executor_name']}"
    api.logs[path + "/log"] = observation({**intent, "project_id": "foreign"}, "completed", patch="")
    with pytest.raises(ValueError, match="sealed work"):
        await control.collect_provider(intent=intent, authority=authority)


@pytest.mark.asyncio
async def test_injected_workload_identity_is_rejected_before_native_readback():
    control, api, _, intent, manifest, authority, _ = prepared()
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    path = f"/api/v1/namespaces/transform-provider/pods/{intent['executor_name']}"
    api.objects[path]["spec"]["containers"][0]["env"] = [{"name": "AWS_ROLE_ARN", "value": "unexpected"}]
    api.logs[path + "/log"] = observation(intent)
    with pytest.raises(AuthorityRevoked):
        await control.read_provider(intent=intent, authority=authority)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_detached_check_is_nonblocking_and_uses_native_exit_and_versioned_logs(exit_code):
    _, api, artifacts, _, _, authority, contents = prepared()
    runner = TransformCheckControl(api, artifacts, namespace="transform-checks", image=IMAGE,
                                   node_name="isolated-example", node_uid=NODE_UID)
    files, modes = {"src/example.py": contents}, {"src/example.py": "100644"}
    tree = compare_source_trees(files, files)["base_tree"]
    command = {"argv": ["pytest", "-q"], "cwd": ".", "environment": {}, "timeout_seconds": 120, "minimum_test_count": 1}
    policy = {"commands": [command], "image_digest": IMAGE.split("@")[1], "bundle_sha256": "c" * 64,
              "lock_sha256": "d" * 64, "oracle_version": "pytest-v1"}
    request = {"stage": "before", "files": files, "file_modes": modes, "source_tree": tree, "command": command,
               "image_digest": policy["image_digest"], "bundle_sha256": policy["bundle_sha256"],
               "oracle_version": policy["oracle_version"], "policy": policy, "authority": authority}
    with pytest.raises(DetachedCheckPending):
        await runner.run_check(**request)
    path, pod = next((path, pod) for path, pod in api.objects.items() if pod["kind"] == "Pod")
    assert "envFrom" not in pod["spec"]["containers"][0]
    assert "secret" not in json.dumps(pod).lower()
    with pytest.raises(DetachedCheckPending):
        await runner.run_check(**request)
    now = datetime.now(UTC)
    pod["status"] = {"containerStatuses": [{"name": "check", "imageID": IMAGE,
        "state": {"terminated": {"startedAt": now.isoformat(), "finishedAt": (now + timedelta(seconds=1)).isoformat(),
                                 "exitCode": exit_code, "reason": "Completed"}}}]}
    api.logs[path + "/log"] = b"2 passed in 1.0s\n"
    receipt = await runner.run_check(**request)
    assert receipt["test_count"] == 2 and receipt["exit_code"] == exit_code
    assert receipt["stream_layout"] == "kubernetes_combined"
    assert receipt["stdout"] == receipt["stderr"]
    scoped = {**receipt["stdout"], "tenant_id": authority.scope["tenant_id"], "project_id": authority.scope["project_id"]}
    assert await artifacts.read(reference=scoped, authority=authority) == api.logs[path + "/log"]
    assert api.objects == {}
    assert await runner.run_check(**request) == receipt
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/pods")]) == 1


@pytest.mark.asyncio
async def test_large_check_upload_makes_progress_across_slow_bounded_calls(monkeypatch):
    from l5_services.adapters.transform import check_control

    _, api, artifacts, _, _, authority, _ = prepared()
    elapsed = [0.0]
    request_api = api.request

    def delayed_request(method, path, **kwargs):
        elapsed[0] += 0.25
        return request_api(method, path, **kwargs)

    api.request = delayed_request
    monkeypatch.setattr(check_control, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    files = {"src/large.txt": b"x" * (5 * 1024 * 1024)}
    modes = {"src/large.txt": "100644"}
    command = {"argv": ["pytest", "-q"], "cwd": ".", "environment": {}, "timeout_seconds": 120, "minimum_test_count": 1}
    policy = {"commands": [command], "image_digest": IMAGE.split("@")[1], "bundle_sha256": "c" * 64,
              "lock_sha256": "d" * 64, "oracle_version": "pytest-v1"}
    request = {"stage": "before", "files": files, "file_modes": modes,
        "source_tree": compare_source_trees(files, files)["base_tree"], "command": command,
        "image_digest": policy["image_digest"], "bundle_sha256": policy["bundle_sha256"],
        "oracle_version": policy["oracle_version"], "policy": policy, "authority": authority}
    for _ in range(20):
        runner = TransformCheckControl(api, artifacts, namespace="transform-checks", image=IMAGE,
                                       node_name="isolated-example", node_uid=NODE_UID)
        started = elapsed[0]
        with pytest.raises(DetachedCheckPending):
            await runner.run_check(**request)
        assert elapsed[0] - started <= 4
        pods = [resource for resource in api.objects.values() if resource["kind"] == "Pod"]
        if pods:
            break
    assert len(pods) == 1
    sources = pods[0]["spec"]["volumes"][0]["projected"]["sources"]
    payload = "".join(api.objects["/api/v1/namespaces/transform-checks/configmaps/" + source["configMap"]["name"]]
                      ["data"][f"part-{index:04}"] for index, source in enumerate(sources[:-1]))
    assert base64.b64decode(json.loads(payload)["files"]["src/large.txt"]) == files["src/large.txt"]
    assert pods[0]["spec"]["containers"][0]["command"][-1] == "--input-sha256=" + sha256(payload.encode()).hexdigest()
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/pods")]) == 1


def test_check_entrypoint_rejects_replaced_uploaded_content(monkeypatch, tmp_path):
    from pathlib import Path
    from l5_services.adapters.transform import check_runner

    original = b'{"files":{"src/app.py":"b3JpZ2luYWw="}}'
    (tmp_path / "part-0000").write_bytes(b'{"files":{"src/app.py":"cmVwbGFjZWQ="}}')
    monkeypatch.setattr(check_runner, "Path", lambda value: tmp_path if value == "/input" else Path(value))
    monkeypatch.setattr(check_runner.sys, "argv", ["check_runner", "--input-sha256=" + sha256(original).hexdigest()])
    with pytest.raises(ValueError, match="mounted input differs"):
        check_runner.main()
