"""Actual provider/check controls reject mutated API readbacks before evidence."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from l5_services.adapters.transform.check_control import TransformCheckControl
from l5_services.adapters.transform.provenance import compare_source_trees
from l5_services.adapters.transform.verification import DetachedCheckPending
from l5_services.workers.execution import AuthorityRevoked
from test_isolated_executor import IMAGE, NODE_UID, observation, prepared


async def running(kind, *, admission_mutation=None):
    source = b"x" * 220_000 if admission_mutation == "drop_part" else b"original\n"
    control, api, artifacts, intent, manifest, authority, contents = prepared(source_contents=source)
    if admission_mutation:
        request = api.request

        def admit(method, path, **kwargs):
            result = request(method, path, **kwargs)
            if method == "POST" and path.endswith("/pods"):
                mutate(result, admission_mutation)
                api.objects[path + "/" + result["metadata"]["name"]] = deepcopy(result)
            return result

        api.request = admit
    if kind == "provider":
        await control.launch_provider(intent=intent, manifest=manifest, authority=authority)

        async def read():
            # Poll/recovery control intents omit launch-only provider_job.
            return await control.read_provider(intent={key: value for key, value in intent.items()
                                                       if key != "provider_job"}, authority=authority)
    else:
        control = TransformCheckControl(api, artifacts, namespace="transform-checks", image=IMAGE,
                                        node_name="isolated-example", node_uid=NODE_UID)
        files = {"src/example.py": contents}
        command = {"argv": ["pytest", "-q"], "cwd": ".", "environment": {},
                   "timeout_seconds": 120, "minimum_test_count": 1}
        policy = {"commands": [command], "image_digest": IMAGE.split("@")[1], "bundle_sha256": "c" * 64,
                  "lock_sha256": "d" * 64, "oracle_version": "pytest-v1"}

        async def read():
            return await control.run_check(policy=policy, authority=authority, stage="before", files=files,
                file_modes={"src/example.py": "100644"}, source_tree=compare_source_trees(files, files)["base_tree"],
                command=command, image_digest=policy["image_digest"], bundle_sha256=policy["bundle_sha256"],
                oracle_version=policy["oracle_version"])

        with pytest.raises(DetachedCheckPending):
            await read()
    path, pod = next((path, value) for path, value in api.objects.items() if value["kind"] == "Pod")
    now = datetime.now(UTC)
    pod["status"] = {"containerStatuses": [{"name": kind, "imageID": IMAGE,
        "state": {"terminated": {"startedAt": now.isoformat(), "finishedAt": (now + timedelta(seconds=1)).isoformat(),
                                 "exitCode": 0, "reason": "Completed"}}}]}
    api.logs[path + "/log"] = observation(intent) if kind == "provider" else b"2 passed in 1.0s\n"
    api.calls.clear()
    return read, control, api, artifacts, pod, path, intent, authority


MUTATIONS = {
    "host_network": ("hostNetwork", True),
    "host_pid": ("hostPID", True),
    "host_ipc": ("hostIPC", True),
    "shared_processes": ("shareProcessNamespace", True),
    "sidecar": ("containers", [{"name": "foreign", "image": IMAGE}]),
    "init_container": ("initContainers", [{"name": "foreign", "image": IMAGE}]),
    "ephemeral_container": ("ephemeralContainers", [{"name": "foreign", "image": IMAGE}]),
    "automount_identity": ("automountServiceAccountToken", True),
    "service_account": ("serviceAccountName", "default"),
    "service_links": ("enableServiceLinks", True),
    "pull_secret": ("imagePullSecrets", [{"name": "foreign"}]),
    "runtime_class": ("runtimeClassName", "foreign"),
    "deadline": ("activeDeadlineSeconds", 86401),
    "unscheduled": ("nodeName", None),
    "host_volume": ("volumes", [{"name": "foreign", "hostPath": {"path": "/"}}]),
}
DEEP_MUTATIONS = {
    "root_user": (("securityContext", "runAsUser"), 0),
    "root_group": (("securityContext", "runAsGroup"), 0),
    "supplemental_group": (("securityContext", "supplementalGroups"), [0]),
    "seccomp": (("securityContext", "seccompProfile", "type"), "Unconfined"),
    "nonroot_flag": (("securityContext", "runAsNonRoot"), False),
    "privileged": (("containers", 0, "securityContext", "privileged"), True),
    "writable_root": (("containers", 0, "securityContext", "readOnlyRootFilesystem"), False),
    "escalation": (("containers", 0, "securityContext", "allowPrivilegeEscalation"), True),
    "capability": (("containers", 0, "securityContext", "capabilities", "add"), ["SYS_ADMIN"]),
    "proc_mount": (("containers", 0, "securityContext", "procMount"), "Unmasked"),
    "command": (("containers", 0, "command"), ["sh", "-c", "true"]),
    "arguments": (("containers", 0, "args"), ["--foreign"]),
    "workdir": (("containers", 0, "workingDir"), "/input"),
    "lifecycle": (("containers", 0, "lifecycle"), {"postStart": {"exec": {"command": ["true"]}}}),
    "environment": (("containers", 0, "env"), [{"name": "AWS_ROLE_ARN", "value": "foreign"}]),
    "foreign_secret": (("containers", 0, "envFrom"), [{"secretRef": {"name": "foreign"}}]),
    "unbounded_cpu": (("containers", 0, "resources", "limits", "cpu"), "100"),
    "input_write": (("containers", 0, "volumeMounts", 0, "readOnly"), False),
    "mount_subpath": (("containers", 0, "volumeMounts", 0, "subPath"), "foreign"),
    "mount_propagation": (("containers", 0, "volumeMounts", 1, "mountPropagation"), "Bidirectional"),
    "foreign_input": (("volumes", 0, "projected", "sources", 0, "configMap", "name"), "foreign"),
    "token_projection": (("volumes", 0, "projected", "sources"), [{"serviceAccountToken": {"path": "token"}}]),
    "unbounded_work": (("volumes", 1, "emptyDir", "sizeLimit"), "100Gi"),
}


def mutate(pod, change):
    if change == "drop_part":
        sources = pod["spec"]["volumes"][0]["projected"]["sources"]
        assert len(sources) == 2
        sources.pop()
    elif change == "shorten_deadline":
        assert pod["spec"]["activeDeadlineSeconds"] > 1
        pod["spec"]["activeDeadlineSeconds"] = 1
    elif change in MUTATIONS:
        key, value = MUTATIONS[change]
        if key in {"containers", "volumes"}:
            pod["spec"][key].extend(deepcopy(value))
        else:
            pod["spec"][key] = deepcopy(value)
    elif change == "legacy_apparmor":
        pod["metadata"].setdefault("annotations", {})[
            "container.apparmor.security.beta.kubernetes.io/" + pod["spec"]["containers"][0]["name"]] = "unconfined"
    else:
        path, value = DEEP_MUTATIONS[change]
        current = pod["spec"]
        for key in path[:-1]:
            current = current[key]
        current[path[-1]] = deepcopy(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "check"])
@pytest.mark.parametrize("change", [*MUTATIONS, *DEEP_MUTATIONS, "legacy_apparmor"])
async def test_mutated_execution_shape_cannot_be_read_or_retained(kind, change):
    read, _, api, artifacts, pod, _, _, _ = await running(kind)
    mutate(pod, change)
    with pytest.raises(AuthorityRevoked):
        await read()
    assert not artifacts.client.objects
    assert all(method == "GET" and not path.endswith("/log") for method, path, _ in api.calls)


def kubernetes_defaults(pod):
    spec = pod["spec"]
    spec.update(dnsPolicy="ClusterFirst", schedulerName="default-scheduler", terminationGracePeriodSeconds=30,
                hostNetwork=False, hostPID=False, hostIPC=False, shareProcessNamespace=False, hostUsers=True,
                serviceAccount="l5-transform-sandbox", priority=0, preemptionPolicy="PreemptLowerPriority")
    spec["tolerations"] = [{"key": f"node.kubernetes.io/{key}", "operator": "Exists", "effect": "NoExecute",
                           "tolerationSeconds": 300} for key in ("unreachable", "not-ready")]
    spec["securityContext"].update(supplementalGroups=[], supplementalGroupsPolicy="Merge", fsGroupChangePolicy="Always")
    container = spec["containers"][0]
    container.update(terminationMessagePath="/dev/termination-log", terminationMessagePolicy="File",
                     imagePullPolicy="IfNotPresent", stdin=False, stdinOnce=False, tty=False)
    container["securityContext"].update(privileged=False, procMount="Default")
    container["securityContext"]["capabilities"]["add"] = []
    container["volumeMounts"][1]["readOnly"] = False
    for mount in container["volumeMounts"]:
        mount.update(mountPropagation="None", recursiveReadOnly="Disabled")
    for source in container.get("envFrom", []):
        source["secretRef"]["optional"] = False
    spec["volumes"][0]["projected"]["defaultMode"] = 420
    for source in spec["volumes"][0]["projected"]["sources"]:
        source["configMap"]["optional"] = False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "check"])
async def test_harmless_api_defaults_preserve_actual_readback_and_check_cleanup(kind):
    read, _, api, _, pod, path, _, _ = await running(kind)
    kubernetes_defaults(pod)
    result = await read()
    if kind == "provider":
        assert result["readback"]["state"] == "running"
    else:
        assert result["test_count"] == 2
        assert path not in api.objects


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "check"])
async def test_admission_mutation_is_rejected_on_initial_create_readback(kind):
    with pytest.raises(AuthorityRevoked, match="isolation"):
        await running(kind, admission_mutation="privileged")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["drop_part", "shorten_deadline"])
async def test_provider_initial_readback_preserves_exact_launch_facts(mutation):
    # Both mutations remain within the generic resumed Pod bounds. The initial
    # response must match the full source-owned request, whose exact facts exist.
    with pytest.raises(AuthorityRevoked, match="isolation"):
        await running("provider", admission_mutation=mutation)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "check"])
async def test_diagnostic_never_copies_api_values_or_unknown_keys(kind):
    read, _, _, _, pod, _, _, _ = await running(kind)
    pod["spec"]["containers"][0]["private-value-must-not-appear"] = "another-private-value"
    with pytest.raises(AuthorityRevoked) as error:
        await read()
    assert "spec.containers[0] (additional fields)" in str(error.value)
    assert "private-value" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "check"])
async def test_mutation_during_log_read_cannot_be_retained(kind):
    read, _, api, artifacts, pod, _, _, _ = await running(kind)
    request = api.request

    def change_after_log(method, path, **kwargs):
        result = request(method, path, **kwargs)
        if path.endswith("/log"):
            mutate(pod, "ephemeral_container")
        return result

    api.request = change_after_log
    with pytest.raises(AuthorityRevoked):
        await read()
    assert not artifacts.client.objects
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.asyncio
async def test_drift_cannot_prevent_uid_fenced_emergency_stop():
    _, control, api, _, pod, _, intent, authority = await running("provider")
    mutate(pod, "privileged")
    native = {"executor_name": intent["executor_name"], "executor_uid": pod["metadata"]["uid"]}
    assert (await control.request_stop(intent=intent, native_job_ref=native, authority=authority))["local_stop_requested"]
    assert api.calls[-1][0] == "DELETE"
    assert api.calls[-1][2]["body"]["preconditions"] == {"uid": native["executor_uid"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, True, 0, 86401])
async def test_missing_or_invalid_admitted_budget_has_no_cluster_writes(budget):
    control, api, _, intent, manifest, authority, _ = prepared()
    authority.provider_deadline_seconds = budget
    with pytest.raises(AuthorityRevoked, match="deadline"):
        await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    assert not api.calls
