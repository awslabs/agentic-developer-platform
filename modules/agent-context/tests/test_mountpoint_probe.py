"""Render real probe resources and check isolation/ownership contracts offline."""

import ast
import fnmatch
import importlib.util
from pathlib import Path
from string import Template

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location(
        "mount_probe", ROOT / "scripts/prepare-mountpoint-probe.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def plan(module):
    return module.plan(
        "ab0123456789",
        "879318057152",
        "agent-context-platform-data-879318057152",
        "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE",
    )


def test_production_mount_modes_and_reader_group_are_consistent(module):
    pv, _ = list(yaml.safe_load_all((ROOT / "manifests/s3-files-storage.yaml").read_text()))
    options = pv["spec"]["mountOptions"]
    assert all(option in options for option in module.MODES)
    modes = {k: int(v, 8) for k, v in (item.split("=") for item in options if "-mode=" in item)}
    assert modes == {"file-mode": 0o640, "dir-mode": 0o750}
    assert all(
        mode & 0o027 == 0 for mode in modes.values()
    )  # group never writes; other gets no access
    source = (ROOT / "manifests/zoekt.yaml").read_text()
    rendered = Template(source).safe_substitute(
        NAMESPACE="agent-context", SERVICE_ACCOUNT="agent-context-sa", ZOEKT_IMAGE="example.invalid/zoekt@sha256:test"
    )
    deployment = next(doc for doc in yaml.safe_load_all(rendered) if doc["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "supplementalGroups": [10001],
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    readers = pod["initContainers"] + pod["containers"]
    mounts = [
        mount
        for reader in readers
        for mount in reader["volumeMounts"]
        if mount["name"] == "index-data"
    ]
    assert len(mounts) == 2 and all(
        mount["readOnly"] and mount["subPath"] == "zoekt-shards" for mount in mounts
    )


def test_probe_uses_own_prefix_and_per_pod_irsa_never_production_claim(plan, module):
    pv = next(obj for obj in plan["setup"] if obj["kind"] == "PersistentVolume")
    spec = pv["spec"]
    assert spec["csi"]["volumeAttributes"]["authenticationSource"] == "pod"
    assert spec["csi"]["volumeAttributes"]["stsRegion"] == "us-east-1"
    assert "prefix=" + plan["prefix"] in spec["mountOptions"]
    assert plan["prefix"] == "security-validation/s15/ab0123456789/"
    assert all(option in spec["mountOptions"] for option in module.MODES)
    assert spec["persistentVolumeReclaimPolicy"] == "Retain"
    assert spec["claimRef"] == {"namespace": plan["namespace"], "name": "probe-data"}
    assert not any(
        obj["kind"] in {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
        for obj in plan["setup"]
    )
    assert spec["csi"]["volumeHandle"] != "s3-csi-agent-context-platform-data-879318057152"


def test_iam_scope_positive_and_neighbor_prefix_refusal(plan):
    own = (
        "arn:aws:s3:::agent-context-platform-data-879318057152/" + plan["prefix"] + "roundtrip.json"
    )
    neighbor = own.replace("ab0123456789", "ab0123456780")
    for access, role in plan["roles"].items():
        statements = role["permissions"]["Statement"]
        listing, objects = statements
        assert listing["Action"] == ["s3:ListBucket"]
        patterns = listing["Condition"]["StringLike"]["s3:prefix"]
        assert any(fnmatch.fnmatchcase(plan["prefix"], pattern) for pattern in patterns)
        assert not any(
            fnmatch.fnmatchcase("content/tenant-victim/", pattern) for pattern in patterns
        )
        assert fnmatch.fnmatchcase(own, objects["Resource"])
        assert not fnmatch.fnmatchcase(neighbor, objects["Resource"])
        assert not fnmatch.fnmatchcase(own.replace(plan["prefix"], "content/"), objects["Resource"])
        assert objects["Action"] == (
            ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
            if access == "writer"
            else ["s3:GetObject"]
        )
        trust = role["trust"]["Statement"][0]
        condition = trust["Condition"]["StringEquals"]
        assert sorted(condition.values()) == sorted(
            ["sts.amazonaws.com", f"system:serviceaccount:{plan['namespace']}:probe-{access}"]
        )
        assert "*" not in str(trust)


def test_jobs_have_no_app_credentials_and_bounded_execution(plan, module):
    assert set(plan["jobs"]) == {"writer", "reader", "outsider"}
    for mode, job in plan["jobs"].items():
        assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 180
        template = job["spec"]["template"]
        pod = template["spec"]
        assert template["metadata"]["annotations"]["eks.amazonaws.com/skip-containers"] == "probe"
        assert not pod["automountServiceAccountToken"]
        assert pod["securityContext"]["supplementalGroupsPolicy"] == "Strict"
        assert "fsGroup" not in pod["securityContext"]
        assert not any(name in pod for name in ("hostNetwork", "hostPID", "hostIPC"))
        (container,) = pod["containers"]
        assert container["image"] == module.IMAGE and "@sha256:" in container["image"]
        assert not container.get("envFrom")
        assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
        assert container["securityContext"]["readOnlyRootFilesystem"]
        assert not container["securityContext"]["allowPrivilegeEscalation"]
        assert container["command"][-2:] == [mode, "live-csi"]
        assert pod["schedulingGates"] == [{"name": "security.adp.dev/s15-ab0123456789"}]
        assert container["volumeMounts"][0]["readOnly"] == (mode != "writer")
        assert all(
            "secret" not in volume and "hostPath" not in volume and "projected" not in volume
            for volume in pod["volumes"]
        )
        assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "probe-data"
    reader = plan["jobs"]["reader"]["spec"]["template"]["spec"]["securityContext"]
    outsider = plan["jobs"]["outsider"]["spec"]["template"]["spec"]["securityContext"]
    assert (reader["runAsUser"], reader["runAsGroup"]) == (0, 10001)
    assert (outsider["runAsUser"], outsider["runAsGroup"], outsider["supplementalGroups"]) == (
        2002,
        2002,
        [],
    )


@pytest.mark.parametrize(
    "bad", ["../other", "abc", "ABCDEF012345", "ab0123456789/", "ab01234567*9"]
)
def test_run_identifier_cannot_escape_own_prefix(module, bad):
    with pytest.raises(ValueError):
        module.plan(
            bad, "879318057152", "example-bucket", "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
        )


def test_probe_python_compiles_and_contains_only_run_owned_mount_paths(module):
    compile(module.PROBE, "probe.py", "exec")
    assert "chmod" not in module.PROBE and "chown" not in module.PROBE
    assert "/platform-data" not in module.PROBE
    assert "roundtrip.json" in module.PROBE and "reader-fixture.txt" in module.PROBE


def test_negative_prefix_stage_cannot_use_allowed_prefix_or_prod_claim(plan):
    denial = plan["denied_prefix_stage"]
    pv, pvc = denial["setup"]
    assert denial["prefix"] != plan["prefix"]
    allowed = plan["roles"]["reader"]["permissions"]["Statement"][0]["Condition"]["StringLike"][
        "s3:prefix"
    ]
    assert not any(fnmatch.fnmatchcase(denial["prefix"], pattern) for pattern in allowed)
    assert pv["spec"]["csi"]["volumeAttributes"]["authenticationSource"] == "pod"
    assert pvc["spec"]["volumeName"] == pv["metadata"]["name"]
    pod = denial["job"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "probe-reader"
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "probe-denied"
    assert "unexpectedly succeeded" in pod["containers"][0]["command"][-1]


def test_live_filesystem_gate_rejects_posix_and_unrelated_fuse(module):
    tree = ast.parse(module.PROBE)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "verify_filesystem"
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "probe-validator", "exec"), namespace
    )
    check = namespace["verify_filesystem"]
    for filesystem in ("fuse", "fuse.mountpoint-s3"):
        assert check("live-csi", filesystem, "mountpoint-s3") == "live-csi-mount"
    for filesystem, source in (
        ("ext4", "/dev/local"),
        ("overlay", "overlay"),
        ("fuse", "unrelated"),
        ("fuse.sshfs", "mountpoint-s3"),
    ):
        with pytest.raises(AssertionError):
            check("live-csi", filesystem, source)
    assert check("local-posix-fixture", "ext4", "/dev/local") == "local-fixture-only"
    with pytest.raises(AssertionError):
        check("local-posix-fixture", "fuse", "mountpoint-s3")
    with pytest.raises(AssertionError):
        check("unknown", "fuse", "mountpoint-s3")


def test_denied_prefix_pod_also_waits_for_admission_review(plan):
    pod = plan["denied_prefix_stage"]["job"]["spec"]["template"]["spec"]
    assert pod["schedulingGates"] == [{"name": "security.adp.dev/s15-ab0123456789"}]
