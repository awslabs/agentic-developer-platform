"""Serving policy must be explicit, pinned and projected only into the API."""

import copy
import json

import pytest

from installation.config import Refusal, validate
from installation.controller_profiles import (
    PATH,
    policy,
    projection,
    selected_profiles,
    verify_cluster_profiles,
    verify_result,
)
from installation.manifests import render


def test_installed_gpu_requirements_do_not_need_a_preselected_machine(
    environment, release
):
    profile = selected_profiles(environment)["approved-model"]
    del profile["instance_type"]
    profile.update(accelerators=["A10G:1", "L4:1"], max_gpus_per_node=1)
    validate(environment, release)
    encoded = json.loads(policy(environment))
    assert encoded == environment["controller_profiles"]
    assert "instance_type" not in projection(environment)["content"]


@pytest.mark.parametrize("invalid", [None, "port", "auth", "model"])
def test_installed_batch_policy_cannot_expose_serving_or_mutable_model_options(
    environment, release, invalid
):
    profile = selected_profiles(environment)["approved-model"]
    profile["model_options"] = {}
    profile["serving_auth_contract"] = None
    profile["workload"].update(kind="batch", port=None, auth_secret=None)
    if invalid == "port":
        profile["workload"]["port"] = 8000
    elif invalid == "auth":
        profile["serving_auth_contract"] = "superplane-token-file-header-v1"
    elif invalid == "model":
        profile["model_options"] = {"model_name": "unreviewed"}
    if invalid:
        with pytest.raises(Refusal):
            validate(environment, release)
    else:
        validate(environment, release)
        assert (
            json.loads(policy(environment))["tenants"][environment["org_id"]][
                "workspaces"
            ][environment["workspace_id"]]["approved-model"]["workload"]["kind"]
            == "batch"
        )


@pytest.mark.parametrize(
    "failure", ["missing", "tag", "secret", "tenant", "workspace", "oversize"]
)
def test_full_install_refuses_missing_or_unpinned_serving_policy(
    environment, release, failure
):
    document = environment["controller_profiles"]
    tenant = document["tenants"][environment["org_id"]]
    profile = tenant["workspaces"][environment["workspace_id"]]["approved-model"]
    if failure == "missing":
        environment.pop("controller_profiles")
    elif failure == "tag":
        profile["workload"]["image"] = "registry.example/model:latest"
    elif failure == "secret":
        profile["workload"]["auth_secret"] = None
    elif failure == "tenant":
        tenant["adp_org_id"] = "another-organization"
    elif failure == "workspace":
        tenant["workspaces"] = {}
    else:
        profile["workload"]["args"] = ["x" * 65536]
    with pytest.raises(Refusal):
        validate(environment, release)


def test_exact_immutable_policy_projection_changes_with_reviewed_content(
    environment, release
):
    docs = render(environment, release)
    configured = projection(environment)
    configmap = next(
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == configured["name"]
    )
    assert configmap["immutable"] is True
    assert (
        json.loads(configmap["data"]["profiles.json"])
        == environment["controller_profiles"]
    )
    api = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "superplane-api"
    )
    pod = api["spec"]["template"]["spec"]
    assert {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}[
        "SUPERPLANE_CONTROLLER_PROFILES_FILE"
    ] == PATH
    mount = next(
        item
        for item in pod["containers"][0]["volumeMounts"]
        if item["name"] == "controller-profiles"
    )
    assert mount["readOnly"] is True
    volume = next(
        item for item in pod["volumes"] if item["name"] == "controller-profiles"
    )
    assert volume["configMap"]["optional"] is False
    assert volume["configMap"]["name"] == configured["name"]
    changed = copy.deepcopy(environment)
    changed["controller_profiles"]["tenants"][environment["org_id"]]["workspaces"][
        environment["workspace_id"]
    ]["approved-model"]["max_runtime_seconds"] = 600
    assert projection(changed)["name"] != configured["name"]
    assert all(
        "controller-profiles" not in json.dumps(doc)
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] != "superplane-api"
    )


def test_missing_policy_has_no_projection_or_fallback(environment, release):
    environment.pop("controller_profiles")
    assert policy(environment) is None
    docs = render(environment, release, control_plane_only=True)
    assert "SUPERPLANE_CONTROLLER_PROFILES_FILE" not in json.dumps(docs)


@pytest.mark.parametrize(
    "field",
    [
        "cluster_id",
        "cluster_arn",
        "namespace",
        "provider_account_id",
        "region",
        "endpoint",
        "certificate_authority",
    ],
)
def test_profile_cannot_select_another_installation_target(environment, release, field):
    profile = selected_profiles(environment)["approved-model"]
    observed = {
        "arn": profile["cluster_arn"],
        "endpoint": profile["endpoint"],
        "certificateAuthority": {"data": profile["certificate_authority"]},
    }
    verify_cluster_profiles(environment, observed)
    profile[field] = "another-target"
    with pytest.raises(Refusal):
        if field in {"endpoint", "certificate_authority"}:
            verify_cluster_profiles(environment, observed)
        else:
            validate(environment, release)


def test_changed_or_unvalidated_image_report_cannot_verify_installed_policy(
    environment,
):
    result = {
        "sha256": projection(environment)["sha256"],
        "profiles": 1,
        "validated": True,
        "workload_ready": False,
    }
    assert verify_result(result, environment) == result
    for change in (
        {"sha256": "0" * 64},
        {"validated": False},
        {"workload_ready": True},
    ):
        with pytest.raises(Refusal):
            verify_result({**result, **change}, environment)
