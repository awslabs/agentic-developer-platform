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
        "vault": {
            "url": "http://gateway.gateway.svc.cluster.local:80",
            "secret_key_ref": {"name": "existing-vault-evidence", "key": "key"},
            "transport": {
                "namespace": "gateway",
                "service": "gateway",
                "port": 80,
                "target_port": 8080,
                "selector": {"app": "gateway"},
                "security": "reviewed-cluster-http",
            },
        },
        "dispatcher": {
            "role_arn": f"arn:aws:iam::{account}:role/producer",
            "endpoint": f"https://abcdefghij.execute-api.{region}.amazonaws.com/dev",
            "api_id": "abcdefghij",
            "region": region,
            "stage": "dev",
        },
        "verification": {
            "workspace_id": environment["workspace_id"],
            "connection_id": "50000000-0000-0000-0000-000000000005",
            "credential_id": "existing-credential",
            "service": "aws",
            "label": "existing",
        },
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
    by_kind = {
        doc["kind"]: doc
        for doc in docs[1:]
        if doc["metadata"]["name"] != "superplane-paid-worker-binding"
    }
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


def test_valid_native_installer_plan_remains_preparation_only(native, tmp_path):
    env, lock = native
    commands = Mock()
    installer = Installer(env, lock, tmp_path, commands)
    scaled = next(doc for doc in installer.docs if doc["kind"] == "ScaledJob")
    assert scaled["metadata"]["annotations"]["autoscaling.keda.sh/paused"] == "true"
    assert scaled["spec"]["maxReplicaCount"] == 0
    report = paid_worker.preparation_report(installer.env, lock)
    assert report["paid_worker_image"].endswith(
        "@" + lock["images"][paid_worker.COMPONENT]
    )
    assert report["activation_available"] is False
    assert report["shared_binding_verified"] is False
    with pytest.raises(Refusal, match=paid_worker.UNAVAILABLE):
        installer.preflight()
    commands.call.assert_not_called()


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


def test_verified_caller_receipt_cannot_bypass_worker_activation_gate(native):
    from installation import adapter_staging

    env, _ = native
    installer = SimpleNamespace(
        env=env,
        receipt={"adapter_stage": {"state": "verified-disabled"}},
        apply=Mock(),
        save=Mock(),
    )
    with pytest.raises(Refusal, match=paid_worker.UNAVAILABLE):
        adapter_staging.activate(installer)
    installer.apply.assert_not_called()
    installer.save.assert_not_called()


def lifecycle_config(env):
    env["paid_worker"].update(
        mode="native-lifecycle",
        operation_schema="superplane_operations",
        lifecycle_policy_configmap="reviewed-lifecycle-policy",
        lifecycle_state_claim="reviewed-lifecycle-state",
        lifecycle_policy_sha256="a" * 64,
    )


def test_lifecycle_projection_preserves_reviewed_mounts_and_separates_schemas(native):
    env, lock = native
    lifecycle_config(env)
    paid_worker.validate(env, lock)
    docs = [{"metadata": {"labels": {LABEL: "owned"}}}]
    paid_worker.project(env, lock, docs)
    by_kind = {
        doc["kind"]: doc
        for doc in docs[1:]
        if doc["metadata"]["name"] != "superplane-paid-worker-binding"
    }
    job = by_kind["ScaledJob"]
    assert job["spec"]["maxReplicaCount"] == 0
    assert job["metadata"]["annotations"]["autoscaling.keda.sh/paused"] == "true"
    pod = job["spec"]["jobTargetRef"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["policy"]["configMap"]["name"] == "reviewed-lifecycle-policy"
    assert (
        volumes["state"]["persistentVolumeClaim"]["claimName"]
        == "reviewed-lifecycle-state"
    )
    assert {v["key"] for v in volumes["database"]["secret"]["items"]} == {
        "domain-dsn",
        "execution-dsn",
        "ca.pem",
    }
    data = by_kind["ConfigMap"]["data"]
    assert data["SUPERPLANE_DOMAIN_SCHEMA"] == "superplane"
    assert data["SUPERPLANE_OPERATION_SCHEMA"] == "superplane_operations"
    assert by_kind["NetworkPolicy"]["spec"]["egress"] == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("operation_schema", "superplane"),
        ("lifecycle_policy_configmap", "../policy"),
        ("lifecycle_state_claim", ""),
        ("lifecycle_policy_sha256", "approved"),
    ],
)
def test_lifecycle_requires_separated_schemas_and_pinned_policy(native, key, value):
    env, lock = native
    lifecycle_config(env)
    env["paid_worker"][key] = value
    with pytest.raises(Refusal):
        paid_worker.validate(env, lock)


def test_native_activation_repeats_proof_before_worker_enable(native, monkeypatch):
    from installation import lifecycle_worker

    env, lock = native
    lifecycle_config(env)
    events = []
    installer = SimpleNamespace(
        env=env,
        lock=lock,
        receipt={
            "adapter_stage": {
                "native_worker": {
                    "snapshot": {"uid": "selected"},
                    "proof": {"binding_sha256": "a" * 64},
                }
            }
        },
        apply=lambda docs: events.append(("apply", docs)),
    )
    monkeypatch.setattr(
        lifecycle_worker,
        "installed_snapshot",
        lambda _, active=False: events.append(("snapshot", active))
        or {"uid": "selected"},
    )
    monkeypatch.setattr(
        lifecycle_worker,
        "proof",
        lambda _, state: events.append(("proof", state))
        or {"state": state, "binding_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        lifecycle_worker,
        "worker_documents",
        lambda _, active=False: ["active" if active else "paused"],
    )
    assert lifecycle_worker.activate(installer) == {
        "state": "executable",
        "binding_sha256": "a" * 64,
    }
    assert events == [
        ("snapshot", False),
        ("proof", "prepared"),
        ("apply", ["active"]),
        ("snapshot", True),
        ("proof", "executable"),
    ]


def test_native_activation_refuses_replaced_preparation_before_mutation(
    native, monkeypatch
):
    from installation import lifecycle_worker

    env, lock = native
    lifecycle_config(env)
    installer = SimpleNamespace(
        env=env,
        lock=lock,
        receipt={
            "adapter_stage": {
                "native_worker": {
                    "snapshot": {"uid": "selected"},
                    "proof": {"binding_sha256": "a" * 64},
                }
            }
        },
        apply=Mock(),
    )
    monkeypatch.setattr(
        lifecycle_worker, "installed_snapshot", lambda _: {"uid": "replaced"}
    )
    with pytest.raises(Refusal, match="changed before activation"):
        lifecycle_worker.activate(installer)
    installer.apply.assert_not_called()


@pytest.mark.parametrize(
    "change", [None, "stale", "wrong-role", "caller-receipt", "nonquiescent"]
)
def test_live_proof_requires_current_exact_protected_response(native, change):
    from datetime import UTC, datetime, timedelta
    from installation import lifecycle_worker

    env, lock = native
    lifecycle_config(env)
    report = {
        **lifecycle_worker.expected_binding(env, lock),
        "version": 1,
        "binding_sha256": "a" * 64,
        "checked_at": datetime.now(UTC).isoformat(),
        "installed": True,
        "state": "prepared",
        "quiescent": True,
        "domain": "superplane",
        "org_id": env["org_id"],
        "adp_org_id": env["adp_org_id"],
        "domain_schema": env["database"]["schema"],
    }
    if change == "stale":
        report["checked_at"] = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    if change == "wrong-role":
        report["worker_role_arn"] += "-replacement"
    if change == "caller-receipt":
        report = {"verified": True}
    if change == "nonquiescent":
        report["quiescent"] = False
    installer = SimpleNamespace(
        env=env, lock=lock, kube=Mock(return_value=report), json=lambda value: value
    )
    if change:
        with pytest.raises(Refusal):
            lifecycle_worker.proof(installer, "prepared")
    else:
        assert lifecycle_worker.proof(installer, "prepared") == report
    assert installer.kube.call_args.kwargs["data"]
    assert "python" in installer.kube.call_args.args


def test_activation_keeps_exact_network_endpoints_and_inert_source(native):
    from installation import lifecycle_worker

    env, lock = native
    lifecycle_config(env)
    docs = [
        {
            "kind": "Namespace",
            "metadata": {"name": "source", "labels": {LABEL: "owned"}},
        }
    ]
    paid_worker.project(env, lock, docs)
    installer = SimpleNamespace(env=env, docs=docs)
    active = lifecycle_worker.worker_documents(installer, active=True)
    job = next(d for d in active if d["kind"] == "ScaledJob")
    network = next(d for d in active if d["kind"] == "NetworkPolicy")
    assert job["spec"]["maxReplicaCount"] == env["paid_worker"]["max_replica_count"]
    assert job["metadata"]["annotations"]["autoscaling.keda.sh/paused"] == "false"
    assert {r["to"][0]["ipBlock"]["cidr"] for r in network["spec"]["egress"][:-1]} == {
        v["cidr"] for v in env["paid_worker"]["egress"].values()
    }
    assert (
        next(d for d in docs if d["kind"] == "ScaledJob")["spec"]["maxReplicaCount"]
        == 0
    )


@pytest.mark.parametrize("worker_ready", [False, True])
def test_api_admission_waits_for_executable_worker_and_restores_on_failure(
    native, monkeypatch, worker_ready
):
    from datetime import UTC, datetime, timedelta
    from installation import adapter_staging, lifecycle_worker

    env, lock = native
    lifecycle_config(env)
    events = []
    now = datetime.now(UTC)
    stage = {
        "state": "verified-disabled",
        "verified_at": now.isoformat(),
        "credential_metadata": {
            "evidence_expires_at": (now + timedelta(minutes=3)).isoformat()
        },
        "binding": {"source": "exact"},
        "deployment_uid": "api",
    }
    installer = SimpleNamespace(
        env=env,
        lock=lock,
        receipt={"adapter_stage": stage},
        docs=[],
        save=Mock(),
        apply=lambda docs: events.append("API enabled"),
    )
    monkeypatch.setattr(adapter_staging, "snapshot", lambda _: {"source": "exact"})
    monkeypatch.setattr(
        adapter_staging, "wait_api", lambda _: {"metadata": {"uid": "api"}}
    )
    monkeypatch.setattr(adapter_staging, "api_document", lambda _: {})
    monkeypatch.setattr(adapter_staging, "project", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        adapter_staging, "restore_disabled", lambda _: events.append("restore disabled")
    )

    def activate(_):
        events.append("worker executable proof")
        if not worker_ready:
            raise Refusal("executable proof failed")
        return {"state": "executable"}

    monkeypatch.setattr(lifecycle_worker, "activate", activate)
    if worker_ready:
        adapter_staging.activate(installer)
        assert events == ["worker executable proof", "API enabled"]
        assert stage["state"] == "activated-awaiting-full-verification"
    else:
        with pytest.raises(Refusal, match="executable proof failed"):
            adapter_staging.activate(installer)
        assert events == ["worker executable proof", "restore disabled"]


def test_shared_quiescence_uses_new_image_without_old_api_or_worker_mutation(
    native, monkeypatch
):
    from datetime import UTC, datetime
    from installation import adapter_staging, lifecycle_worker

    env, lock = native
    lifecycle_config(env)
    env["timeout_seconds"] = 120
    role = {
        "role_arn": env["api_adapters"]["dispatcher"]["role_arn"],
        "role_id": "AROAEXACT",
    }
    monkeypatch.setattr(adapter_staging, "role_identity", lambda *_: role)
    docs = [
        {
            "kind": "Namespace",
            "metadata": {"name": env["namespace"], "labels": {LABEL: "owned"}},
        },
        {
            "kind": "ServiceAccount",
            "metadata": {
                "name": "superplane-api",
                "namespace": env["namespace"],
                "labels": {LABEL: "owned"},
                "annotations": {"eks.amazonaws.com/role-arn": role["role_arn"]},
            },
        },
    ]
    report = {
        "version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "installed": False,
        "state": "quiescent",
        "quiescent": True,
        "domain": "superplane",
        "org_id": env["org_id"],
        "adp_org_id": env["adp_org_id"],
    }
    installer = SimpleNamespace(
        env=env,
        lock=lock,
        owner="owned",
        docs=docs,
        receipt={},
        aws=Mock(return_value={"cluster": {}}),
        json=lambda v: v,
        kube=Mock(return_value=report),
        apply=Mock(),
        wait_job=Mock(),
        save=Mock(),
        existing=Mock(side_effect=AssertionError("old API must not be used")),
    )
    assert lifecycle_worker.quiescence_job(installer) == report
    rendered = installer.apply.call_args.args[0]
    assert {d["kind"] for d in rendered} == {
        "Namespace",
        "ServiceAccount",
        "NetworkPolicy",
        "Job",
    }
    job = next(d for d in rendered if d["kind"] == "Job")
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "superplane-api"
    assert pod["containers"][0]["image"].endswith(
        "@" + lock["images"]["superplane-api"]
    )
    assert not pod.get("volumes")
    assert not pod["containers"][0].get("envFrom")
    assert all("DATABASE" not in e["name"] for e in pod["containers"][0]["env"])
    assert installer.kube.call_args.args[0] == "logs"
    assert (
        installer.receipt["shared_execution_quiescence"]["proof"]["installed"] is False
    )


def test_absent_api_does_not_skip_shared_quiescence(native, monkeypatch):
    from installation import adapter_staging, lifecycle_worker

    env, _ = native
    lifecycle_config(env)
    installer = SimpleNamespace(env=env, receipt={}, existing=lambda _: None)
    monkeypatch.setattr(adapter_staging, "api_document", lambda _: {})
    check = Mock(side_effect=Refusal("shared active operations"))
    monkeypatch.setattr(lifecycle_worker, "quiescence_job", check)
    with pytest.raises(Refusal, match="shared active operations"):
        adapter_staging.require_quiescent(installer)
    check.assert_called_once_with(installer)
    assert "adapter_quiescence" not in installer.receipt


def test_upgrade_checks_shared_store_after_role_plan_before_worker_resources(
    native, monkeypatch, tmp_path
):
    from contextlib import nullcontext
    from installation import adapter_staging, lifecycle_worker

    env, _ = native
    lifecycle_config(env)
    events = []
    monkeypatch.setattr(
        adapter_staging,
        "require_quiescent",
        lambda _, **kwargs: events.append(("domain-check", kwargs)),
    )

    def shared(_):
        events.append(("shared-new-image-check", {}))
        raise Refusal("existing shared operation")

    monkeypatch.setattr(lifecycle_worker, "quiescence_job", shared)
    installer = SimpleNamespace(
        env=env,
        receipt={"plan_sha256": "reviewed", "completed": []},
        preflight=Mock(),
        exclusive=nullcontext,
        disable_route=Mock(),
        directory=tmp_path,
        commands=SimpleNamespace(
            call=lambda *a, **k: events.append(("approved-role-plan", {}))
        ),
        foundations=Mock(),
        save=Mock(),
    )
    installer.phase = lambda name, function: function()
    with pytest.raises(Refusal, match="existing shared operation"):
        Installer.execute(installer, "reviewed", "private-token")
    assert events == [
        ("domain-check", {"shared": False}),
        ("approved-role-plan", {}),
        ("shared-new-image-check", {}),
    ]
    installer.foundations.assert_not_called()
