"""Render the supported idle/active pod contract and inspect credential boundaries."""

import copy

import pytest

from installation.config import Refusal, validate
from installation.manifests import render
from installation.execution import read_only_workspace_rules


@pytest.mark.parametrize(
    "resource,verb,allowed",
    [
        ("pods", "list", True),
        ("jobs", "create", False),
        ("secrets", "get", False),
        ("pods/exec", "get", False),
        ("*", "get", False),
    ],
)
def test_manager_preflight_refuses_effect_and_credential_authority(
    resource, verb, allowed
):
    status = {
        "incomplete": False,
        "resourceRules": [
            {"resources": [resource], "verbs": [verb], "apiGroups": [""]}
        ],
    }
    assert read_only_workspace_rules(status) is allowed
    status["incomplete"] = True
    assert not read_only_workspace_rules(status)


def test_executor_cannot_reuse_manager_workspace_projection(environment, release):
    env, lock = configured(environment, release)
    env["execution"]["workspace_credentials_secret"] = "superplane-workspace-access"
    with pytest.raises(Refusal, match="separate"):
        validate(env, lock)


def configured(environment, release):
    env, lock = copy.deepcopy(environment), copy.deepcopy(release)
    env["execution"] = {
        "authority_endpoint": "https://authority.example.test",
        "role_arn": f"arn:aws:iam::{env['account_id']}:role/selected-executor",
        "provider_role_arn": f"arn:aws:iam::{env['account_id']}:role/selected-provider",
        "run_projection_secret": "selected-adp-run",
        "database_secret": "selected-execution-database",
        "workspace_credentials_secret": "selected-workspace-execution",
    }
    lock["images"]["superplane-executor"] = "sha256:" + "e" * 64
    lock["image_sources"]["superplane-executor"] = {
        "registry": f"{env['account_id']}.dkr.ecr.{env['region']}.amazonaws.com",
        "repository": "adp-superplane-executor",
        "source_revision": lock["source_revision"],
    }
    return env, lock


@pytest.mark.parametrize("idle", [False, True])
def test_executor_owns_credentials_and_worker_only_reads_socket(
    environment, release, idle
):
    env, lock = configured(environment, release)
    if idle:
        env["secrets"].pop("workspace_access")
    validate(env, lock, control_plane_only=idle)
    docs = render(env, lock, control_plane_only=idle)
    deployment = next(
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["metadata"]["name"] == "superplane-controller"
    )
    pod = deployment["spec"]["template"]["spec"]
    worker, executor = pod["containers"]
    assert (
        deployment["spec"]["template"]["metadata"]["annotations"][
            "eks.amazonaws.com/skip-containers"
        ]
        == worker["name"]
    )
    assert not pod["automountServiceAccountToken"]
    assert (
        executor["securityContext"]["runAsUser"] != pod["securityContext"]["runAsUser"]
    )
    worker_mounts = {m["name"]: m for m in worker["volumeMounts"]}
    executor_mounts = {m["name"]: m for m in executor["volumeMounts"]}
    assert worker_mounts["execution"]["readOnly"]
    assert executor_mounts["controller-instance"]["readOnly"]
    assert (
        not {
            "execution-run",
            "execution-database",
            "execution-skypilot",
            "executor-tmp",
            "execution-workspace",
        }
        & worker_mounts.keys()
    )
    assert {
        "execution-run",
        "execution-database",
        "execution-skypilot",
        "execution-workspace",
    } <= executor_mounts.keys()
    assert "registry" not in executor_mounts
    assert "workspace-access" not in executor_mounts
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert (
        volumes["workspace-access"]["secret"]["secretName"]
        != volumes["execution-workspace"]["secret"]["secretName"]
    )
    for volume in pod["volumes"]:
        if volume["name"] in {
            "workspace-access",
            "workspace-observations",
            "execution-run",
            "execution-database",
            "execution-skypilot",
            "execution-workspace",
        }:
            assert volume["secret"]["optional"] is True
    account = next(
        d
        for d in docs
        if d["kind"] == "ServiceAccount"
        and d["metadata"]["name"] == "superplane-controller"
    )
    assert (
        account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
        == env["execution"]["role_arn"]
    )


def test_executor_requires_real_release_and_explicit_same_account_role(
    environment, release
):
    env, lock = configured(environment, release)
    lock["pending_images"]["superplane-executor"] = {"blocked_by": "not built"}
    with pytest.raises(Refusal, match="unresolved"):
        validate(env, lock)
    lock["pending_images"].clear()
    env["execution"]["role_arn"] = "arn:aws:iam::000000000000:role/foreign"
    with pytest.raises(Refusal, match="selected account"):
        validate(env, lock)


@pytest.mark.parametrize("refused", [None, "namespace", "nodepools", "superplanenodes"])
def test_workspace_permission_probes_match_actual_crd_scopes(tmp_path, refused):
    import json
    from types import SimpleNamespace
    from installation.runner import Installer

    probes = []

    class Commands:
        def call(self, args, **kwargs):
            if "can-i" in args:
                probe = args[args.index("can-i") + 1 :]
                probes.append(probe)
                if probe[1] == "namespaces":
                    assert probe == ["get", "namespaces", "--resource-name", "tenant-a"]
                    selected = "namespace"
                elif probe[1] == "nodepools.superplane.ai":
                    assert "-n" not in probe
                    selected = "nodepools"
                else:
                    selected = (
                        "superplanenodes"
                        if probe[1] == "superplanenodes.superplane.ai"
                        else None
                    )
                    if selected:
                        assert probe[-2:] == ["-n", "tenant-a"]
                return SimpleNamespace(
                    stdout="no" if refused and refused == selected else "yes"
                )
            return SimpleNamespace(
                stdout=json.dumps(
                    {"status": {"incomplete": False, "resourceRules": []}}
                )
            )

    installer = Installer.__new__(Installer)
    installer.env = {
        "workspace_namespace": "tenant-a",
        "workspace_cluster": "workspace",
    }
    installer.directory = tmp_path
    installer.commands = Commands()
    installer.secret_values = {
        "workspace_access": {"kubeconfig": "explicit-test-credential"}
    }
    installer.aws = lambda *_: None
    if refused:
        with pytest.raises(Refusal, match="scoped permissions"):
            installer.workspace()
    else:
        installer.workspace()
        assert ["list", "nodepools.superplane.ai"] in probes
        assert ["watch", "nodepools.superplane.ai"] in probes
        assert ["list", "superplanenodes.superplane.ai", "-n", "tenant-a"] in probes
