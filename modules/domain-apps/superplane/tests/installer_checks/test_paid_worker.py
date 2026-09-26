"""Inert native projection and explicit unavailable activation boundary."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from installation import paid_worker
from installation.config import LABEL, Refusal
from installation.runner import Installer


@pytest.fixture
def native(environment, release):
    account, region = environment["account_id"], environment["region"]
    environment["api_adapters"] = {
        "dispatcher": {
            "role_arn": f"arn:aws:iam::{account}:role/producer",
            "endpoint": f"https://abcdefghij.execute-api.{region}.amazonaws.com/dev",
        }
    }
    environment["paid_worker"] = {
        "mode": "native-controller",
        "namespace": environment["namespace"],
        "role_arn": f"arn:aws:iam::{account}:role/native-paid-worker",
        "queue_observer_role_arn": f"arn:aws:iam::{account}:role/queue-observer",
        "queue_url": f"https://sqs.{region}.amazonaws.com/{account}/paid",
        "queue_arn": f"arn:aws:sqs:{region}:{account}:paid",
        "database_secret": "paid-database",
        "workspace_credentials_secret": "paid-workspaces",
        "provider_secret": "paid-provider",
        "operation_schema": environment["database"]["schema"],
        "skypilot_url": f"http://skypilot-api.{environment['skypilot_namespace']}.svc.cluster.local:46580",
        "management_api_server": "https://management.example.test",
        "node_selector": {"kubernetes.io/arch": "amd64"},
        "max_replica_count": 2,
        "active_deadline_seconds": 600,
        "egress": {
            key: {"cidr": f"10.0.1.{index}/32", "port": 443}
            for index, key in enumerate(
                ("gateway", "sts", "database", "skypilot", "workspace", "management"), 1
            )
        },
    }
    release["images"][paid_worker.COMPONENT] = "sha256:" + "9" * 64
    release["image_sources"][paid_worker.COMPONENT] = {
        "registry": f"{account}.dkr.ecr.{region}.amazonaws.com",
        "repository": "adp-" + paid_worker.COMPONENT,
        "source_revision": release["source_revision"],
    }
    return environment, release


def test_omitted_paid_worker_preserves_default(environment, release):
    docs = [{"metadata": {"labels": {LABEL: "owned"}}}]
    before = copy.deepcopy(docs)
    paid_worker.validate(environment, release)
    paid_worker.project(environment, release, docs)
    paid_worker.require_activation_available(environment)
    assert docs == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("mode", "workspace-lifecycle"),
        ("binding_receipt", {"verified": True}),
        ("namespace", "foreign"),
        ("operation_schema", "public"),
        ("max_replica_count", True),
        ("active_deadline_seconds", 3601),
        ("workspace_credentials_secret", "superplane-workspace-access"),
        ("queue_arn", "arn:aws:sqs:us-east-1:111111111111:paid"),
    ],
)
def test_native_projection_rejects_unsupported_or_ambiguous_inputs(native, key, value):
    env, lock = native
    env["paid_worker"][key] = value
    with pytest.raises(Refusal):
        paid_worker.validate(env, lock)


@pytest.mark.parametrize("problem", ["missing", "pending", "reused", "wrong-source"])
def test_paid_image_is_a_separate_reviewed_release(native, problem):
    env, lock = native
    if problem == "missing":
        lock["images"].pop(paid_worker.COMPONENT)
    elif problem == "pending":
        lock["pending_images"][paid_worker.COMPONENT] = {}
    elif problem == "reused":
        lock["images"][paid_worker.COMPONENT] = lock["images"]["superplane-executor"]
    else:
        lock["image_sources"][paid_worker.COMPONENT]["source_revision"] = "b" * 40
    with pytest.raises(Refusal):
        paid_worker.validate(env, lock)


def test_native_source_projection_is_paused_without_lifecycle_mounts(native):
    env, lock = native
    paid_worker.validate(env, lock)
    docs = [{"metadata": {"labels": {LABEL: "owned"}}}]
    paid_worker.project(env, lock, docs)
    by_kind = {doc["kind"]: doc for doc in docs[1:]}
    scaled = by_kind["ScaledJob"]
    assert scaled["metadata"]["annotations"]["autoscaling.keda.sh/paused"] == "true"
    assert scaled["spec"]["maxReplicaCount"] == 0
    pod = scaled["spec"]["jobTargetRef"]["template"]["spec"]
    worker = pod["containers"][0]
    assert "command" not in worker and "args" not in worker
    assert worker["image"].endswith("@" + lock["images"][paid_worker.COMPONENT])
    assert not {"state", "policy"} & {volume["name"] for volume in pod["volumes"]}
    assert not any("LIFECYCLE" in value["name"] for value in worker["env"])
    assert {value["name"]: value.get("value") for value in worker["env"]}[
        "SUPERPLANE_PAID_WORKER_MODE"
    ] == "native-controller"
    assert by_kind["NetworkPolicy"]["spec"]["egress"] == []
    assert all(
        mount["name"] == "task" for mount in pod["initContainers"][0]["volumeMounts"]
    )
    assert paid_worker.preparation_report(env, lock)["activation_available"] is False


def test_preflight_refuses_missing_shared_contract_before_tools(native):
    env, _ = native
    installer = SimpleNamespace(
        env=env,
        phase=Mock(side_effect=AssertionError("must refuse before external phases")),
    )
    with pytest.raises(Refusal, match=paid_worker.UNAVAILABLE):
        Installer.preflight(installer)
    installer.phase.assert_not_called()


@pytest.mark.parametrize(
    "cidr", ["169.254.169.254/32", "169.254.170.2/32", "fe80::/128"]
)
def test_prepared_network_intent_rejects_link_local_endpoints(native, cidr):
    env, lock = native
    env["paid_worker"]["egress"]["gateway"]["cidr"] = cidr
    with pytest.raises(Refusal, match="routable host CIDRs"):
        paid_worker.validate(env, lock)
