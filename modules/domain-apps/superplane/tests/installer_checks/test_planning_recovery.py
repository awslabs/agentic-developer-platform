# ruff: noqa: F811 -- shared owner fixture is injected by pytest.
"""Start-barrier evidence needs original audit provenance and an inert probe."""

import copy
import json
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest

from installation.lifecycle_foundations import probe_matches
from installation.planning_probe import probe_job
from installation.planning_recovery import source_pod
from installation import planning_recovery as owner
from .test_provider_authority import setup as setup


def test_probe_cannot_inherit_worker_credentials_or_write_state():
    template = {
        "containers": [
            {
                "image": "registry/worker@sha256:" + "a" * 64,
                "env": [{"name": "AWS_SECRET_ACCESS_KEY", "value": "never-copy"}],
            }
        ],
        "nodeSelector": {"kubernetes.io/os": "linux"},
        "volumes": [{"name": "credentials", "secret": {"secretName": "worker-db"}}],
    }
    job = probe_job(
        template,
        namespace="superplane",
        claim_name="original-state",
        identity=["org", "workspace", "operation"],
        run_id="a" * 16,
    )
    spec = job["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == "superplane-state-probe"
    assert len(spec["containers"]) == 1
    assert spec["volumes"] == [
        {"name": "state", "persistentVolumeClaim": {"claimName": "original-state"}},
        {"name": "temporary", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}},
    ]
    container = spec["containers"][0]
    assert "env" not in container and "envFrom" not in container
    assert container["volumeMounts"] == [
        {"name": "state", "mountPath": "/run/state", "readOnly": True},
        {"name": "temporary", "mountPath": "/tmp"},
    ]
    assert "os.ST_RDONLY" in container["command"][-1]
    actual = copy.deepcopy(spec)
    actual["containers"][0]["terminationMessagePolicy"] = "File"
    assert probe_matches(actual, spec)
    actual["containers"][0]["volumeMounts"][0]["readOnly"] = False
    assert not probe_matches(actual, spec)


@pytest.fixture
def provenance():
    now = datetime.now(timezone.utc)
    acquired = now - timedelta(hours=2)
    pod = {
        "metadata": {
            "uid": "original-uid",
            "name": "worker-original",
            "namespace": "superplane",
            "creationTimestamp": (acquired - timedelta(seconds=10)).isoformat(),
        },
        "spec": {
            "containers": [
                {"name": "worker", "image": "registry/worker@sha256:" + "a" * 64}
            ]
        },
    }
    terminal = copy.deepcopy(pod)
    terminal["status"] = {"phase": "Failed"}

    def event(verb, response, number):
        return {
            "logStreamName": "kube-apiserver-audit-original",
            "eventId": str(number),
            "message": json.dumps(
                {
                    "stage": "ResponseComplete",
                    "verb": verb,
                    "responseStatus": {"code": 201 if verb == "create" else 200},
                    "objectRef": {
                        "resource": "pods",
                        "namespace": "superplane",
                        "name": "worker-original",
                    },
                    "responseObject": response,
                }
            ),
        }

    events = [event("create", pod, 1), event("delete", terminal, 2)]
    client = SimpleNamespace(
        get_paginator=lambda _: SimpleNamespace(
            paginate=lambda **_: [{"events": events}]
        )
    )
    session = SimpleNamespace(client=lambda _: client)
    return (
        session,
        events,
        dict(
            cluster="cluster",
            namespace="superplane",
            name="worker-original",
            uid="original-uid",
            acquired_at=acquired,
            now=now,
        ),
    )


def test_provenance_recovers_original_uid_after_live_pod_deletion(provenance):
    session, _, args = provenance
    pod, proof = source_pod(session, **args)
    assert pod["metadata"]["uid"] == "original-uid"
    assert proof["creation_event_id"] == "1" and proof["terminal_event_ids"] == ["2"]


@pytest.fixture
def historical_provenance(provenance):
    session, events, args = provenance
    args["acquired_at"] = args["now"] - timedelta(days=12)
    for event in events:
        value = json.loads(event["message"])
        value["responseObject"]["metadata"]["creationTimestamp"] = (
            args["acquired_at"] - timedelta(seconds=10)
        ).isoformat()
        event["message"] = json.dumps(value)
    groups = [{"logGroupName": "/aws/eks/cluster/cluster", "creationTime": 0}]
    queries = []

    def paginator(operation):
        def paginate(**kwargs):
            queries.append((operation, kwargs))
            return [
                {"logGroups": groups}
                if operation == "describe_log_groups"
                else {"events": events}
            ]

        return SimpleNamespace(paginate=paginate)

    session.client = lambda _: SimpleNamespace(get_paginator=paginator)
    return session, events, args, groups, queries


@pytest.mark.parametrize("retention_days", [None, 14])
def test_historical_bootstrap_reads_original_window_through_actual_now(
    historical_provenance, retention_days
):
    session, _, args, groups, queries = historical_provenance
    with pytest.raises(ValueError):
        source_pod(session, **args)
    assert queries == []
    if retention_days is not None:
        groups[0]["retentionInDays"] = retention_days
    pod, proof = source_pod(session, **args, historical_bootstrap=True)
    assert pod["metadata"]["uid"] == args["uid"]
    assert (
        datetime.fromisoformat(pod["metadata"]["creationTimestamp"])
        < args["acquired_at"]
    )
    assert proof["creation_event_id"] == "1" and proof["terminal_event_ids"] == ["2"]
    assert queries == [
        ("describe_log_groups", {"logGroupNamePrefix": "/aws/eks/cluster/cluster"}),
        (
            "filter_log_events",
            {
                "logGroupName": "/aws/eks/cluster/cluster",
                "logStreamNamePrefix": "kube-apiserver-audit-",
                "startTime": int(
                    (args["acquired_at"] - timedelta(minutes=5)).timestamp() * 1000
                ),
                "endTime": int(args["now"].timestamp() * 1000),
                "filterPattern": '"worker-original"',
            },
        ),
    ]


@pytest.mark.parametrize(
    "fault",
    [
        "too-old",
        "too-recent",
        "non-boolean",
        "missing-group",
        "recreated-group",
        "short-retention",
        "missing-creation",
        "no-terminal",
        "wrong-uid",
        "exec",
        "attach",
        "ephemeralcontainers",
        "changed-image",
        "event-bound",
    ],
)
def test_historical_bootstrap_does_not_relax_audit_proof(historical_provenance, fault):
    session, events, args, groups, _ = historical_provenance
    selected = True
    if fault == "too-old":
        args["acquired_at"] = args["now"] - timedelta(days=30)
    elif fault == "too-recent":
        args["acquired_at"] = args["now"] - timedelta(minutes=5)
    elif fault == "non-boolean":
        selected = 1
    elif fault == "missing-group":
        groups.clear()
    elif fault == "recreated-group":
        groups[0]["creationTime"] = int(args["acquired_at"].timestamp() * 1000)
    elif fault == "short-retention":
        groups[0]["retentionInDays"] = 7
    elif fault == "missing-creation":
        groups[0].pop("creationTime")
    elif fault == "no-terminal":
        events.pop()
    elif fault == "wrong-uid":
        args["uid"] = "replacement"
    elif fault == "event-bound":
        events.extend([events[1]] * 10000)
    else:
        value = json.loads(events[1]["message"])
        if fault == "changed-image":
            value["responseObject"]["spec"]["containers"][0]["image"] = "changed"
        else:
            value["objectRef"]["subresource"] = fault
        events[1]["message"] = json.dumps(value)
    with pytest.raises(ValueError):
        source_pod(session, **args, historical_bootstrap=selected)


@pytest.mark.parametrize(
    "change",
    [
        "no-terminal",
        "replacement",
        "exec",
        "changed-image",
        "override-command",
        "untrusted-stream",
    ],
)
def test_uncertain_provenance_cannot_attest_unstarted_planning(provenance, change):
    session, events, args = provenance
    if change == "no-terminal":
        events.pop()
    elif change == "replacement":
        args["uid"] = "replacement-uid"
    elif change == "untrusted-stream":
        events[0]["logStreamName"] = "worker-supplied-log"
    else:
        index = 0 if change == "override-command" else 1
        value = json.loads(events[index]["message"])
        if change == "exec":
            value["objectRef"]["subresource"] = "exec"
        elif change == "changed-image":
            value["responseObject"]["spec"]["containers"][0]["image"] = "unqualified"
        else:
            value["responseObject"]["spec"]["containers"][0]["command"] = [
                "arbitrary-code"
            ]
        events[index]["message"] = json.dumps(value)
    with pytest.raises(ValueError):
        source_pod(session, **args)


@pytest.fixture
def collection(setup, monkeypatch):
    setup.run(check_only=False)
    value = setup.doc["authority"]
    namespace = "superplane-demo"
    descriptor = {k: value[k] for k in ("org_id", "workspace_id")}
    descriptor.update(
        operation_id="operation",
        source_principal="invocation#1",
        source_key="operation-step:original",
        source_fence=3,
        plan_digest="a" * 64,
        workload_binding="pod-uid",
        source_pod_name="worker-original",
        source_acquired_at=(
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat(),
    )
    original = {
        "installation_id": value["installation_id"],
        "status": "installed-and-verified",
        "environment": {
            "namespace": namespace,
            "paid_worker": {"lifecycle_state_claim": "state"},
        },
        "lifecycle_foundations": {
            "PersistentVolumeClaim": {"uid": "claim-uid"},
            "PersistentVolume": {"uid": "volume-uid"},
        },
    }
    template = {
        "containers": [{"image": "registry/worker@sha256:" + "a" * 64}],
        "nodeSelector": {"kubernetes.io/os": "linux"},
    }
    job = probe_job(
        template,
        namespace=namespace,
        claim_name="state",
        identity=[descriptor[k] for k in ("org_id", "workspace_id", "operation_id")],
        run_id="a" * 16,
    )
    job["metadata"]["uid"] = "probe-uid"
    job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    objects = {
        "scaledjob": {
            "metadata": {"annotations": {"autoscaling.keda.sh/paused": "true"}},
            "spec": {"jobTargetRef": {"template": {"spec": template}}},
        },
        "deployment": {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "superplane-api",
                                "env": [
                                    {
                                        "name": "SUPERPLANE_OPERATION_DISPATCH_ENABLED",
                                        "value": "false",
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        },
        "pvc": {
            "metadata": {"uid": "claim-uid"},
            "spec": {"volumeName": "volume"},
            "status": {"phase": "Bound"},
        },
        "pv": {
            "metadata": {"uid": "volume-uid"},
            "spec": {"claimRef": {"uid": "claim-uid"}},
            "status": {"phase": "Bound"},
        },
        "pods": {"items": []},
        "job": job,
    }
    root = Path(owner.__file__).resolve().parents[1] / "workspace_provisioning"
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*.py")
        if "tests" not in p.parts
    }
    report = {
        "version": 1,
        "identity": [descriptor[k] for k in ("org_id", "workspace_id", "operation_id")],
        "read_only": True,
        "operation_directory_exists": False,
        "runtime_sources": hashes,
    }
    pod = {
        "spec": {
            "containers": [
                {
                    "image": template["containers"][0]["image"],
                    "securityContext": {"runAsUser": 65531, "runAsGroup": 65532},
                    "volumeMounts": [{"name": "state", "mountPath": "/run/state"}],
                    "env": [
                        {
                            "name": "SUPERPLANE_LIFECYCLE_STATE_DIR",
                            "value": "/run/state",
                        }
                    ],
                }
            ],
            "volumes": [
                {"name": "state", "persistentVolumeClaim": {"claimName": "state"}}
            ],
        }
    }
    monkeypatch.setattr(owner, "installed_config", lambda *args: setup.config)
    monkeypatch.setattr(
        owner,
        "source_pod",
        lambda *args, **kw: (pod, {"creation_event_id": "protected-event"}),
    )

    def kube(*args):
        return json.dumps(report if args[2] == "logs" else objects[args[3]])

    def collect(**kwargs):
        return owner.collect(
            setup.doc,
            original,
            descriptor,
            job["metadata"]["name"],
            setup.session,
            read_kube=kube,
            image_sources=lambda _: dict(hashes),
            **kwargs,
        )

    return SimpleNamespace(
        run=collect,
        setup=setup,
        objects=objects,
        report=report,
        pod=pod,
        descriptor=descriptor,
        original=original,
    )


def test_collection_rechecks_live_evidence_without_publishing_or_approving(collection):
    before = collection.setup.state.writes
    evidence = collection.run()
    assert (
        evidence["planning_started"] is False and evidence["storage_uid"] == "claim-uid"
    )
    assert collection.setup.state.writes == before


@pytest.mark.parametrize(
    "change",
    [
        "worker-active",
        "dispatch-active",
        "replaced-claim",
        "replaced-volume",
        "writable-probe",
        "extra-credentials",
        "unreviewed-resources",
        "directory-exists",
        "wrong-owner",
        "source-user",
    ],
)
def test_collection_refuses_storage_or_authority_drift(collection, change):
    c = collection
    if change == "worker-active":
        c.objects["scaledjob"]["metadata"]["annotations"][
            "autoscaling.keda.sh/paused"
        ] = "false"
    elif change == "dispatch-active":
        c.objects["deployment"]["spec"]["template"]["spec"]["containers"][0]["env"][0][
            "value"
        ] = "true"
    elif change == "replaced-claim":
        c.objects["pvc"]["metadata"]["uid"] = "replacement"
    elif change == "replaced-volume":
        c.objects["pv"]["metadata"]["uid"] = "replacement"
    elif change == "directory-exists":
        c.report["operation_directory_exists"] = True
    elif change == "wrong-owner":
        c.setup.state.selected_role_id = "AROA" + "B" * 17
    elif change == "source-user":
        c.pod["spec"]["containers"][0]["securityContext"]["runAsUser"] = 1000
    else:
        container = c.objects["job"]["spec"]["template"]["spec"]["containers"][0]
        if change == "writable-probe":
            container["volumeMounts"][0]["readOnly"] = False
        elif change == "unreviewed-resources":
            container["resources"]["limits"]["memory"] = "1Gi"
        else:
            container["envFrom"] = [{"secretRef": {"name": "credentials"}}]
    before = c.setup.state.writes
    with pytest.raises(ValueError):
        c.run()
    assert c.setup.state.writes == before


def test_owner_records_explicit_extension_without_changing_live_state(collection):
    c = collection
    c.descriptor["attempt_extension"] = {
        "authorized_by": c.setup.doc["authority"]["subject"],
        "authorization": "5 more additional attempts are authorized",
        "previous_limit": 5,
        "recorded_attempts": 5,
        "additional_attempts": 5,
    }
    before = c.setup.state.writes
    result = c.run()
    assert result["attempt_extension"] == c.descriptor["attempt_extension"]
    assert c.setup.state.writes == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("authorized_by", "another-user"),
        ("additional_attempts", 6),
        ("additional_attempts", True),
        ("authorization", ""),
        ("previous_limit", 6),
    ],
)
def test_owner_refuses_unbound_extension(collection, key, value):
    c = collection
    c.descriptor["attempt_extension"] = {
        "authorized_by": c.setup.doc["authority"]["subject"],
        "authorization": "Five extra attempts",
        "previous_limit": 5,
        "recorded_attempts": 5,
        "additional_attempts": 5,
    }
    c.descriptor["attempt_extension"][key] = value
    with pytest.raises(ValueError):
        c.run()


def test_owner_accepts_only_exact_before_init_preparation(collection, tmp_path):
    from workspace_provisioning.terraform import copy_source
    from workspace_provisioning.planning_boundary import snapshot

    directory = tmp_path / "original"
    directory.mkdir()
    copy_source(directory / "module")
    (directory / "review").mkdir()
    (directory / "workspace.tfvars.json").write_text("{}")
    collection.report.update(
        operation_directory_exists=True, retained_entries=snapshot(directory)
    )
    proof = collection.run()
    assert proof["preparation_boundary"]["kind"] == "before-terraform-init-v1"
    assert proof["preparation_boundary"]["entries"] == snapshot(directory)
    (directory / "review/workspace.tfplan").write_text("started")
    collection.report["retained_entries"] = snapshot(directory)
    with pytest.raises(ValueError):
        collection.run()


@pytest.fixture
def backend_observation():
    state = SimpleNamespace(versioning="Enabled", versions={}, item={}, calls=[])
    backend = {
        "bucket": "fixture-state",
        "region": "us-east-1",
        "lock_table": "fixture-lock",
    }
    target = {
        "account_id": "111122223333",
        "environment": "dev",
        "org_id": "org",
        "workspace_id": "workspace",
    }

    def get_item(**kwargs):
        state.calls.append(kwargs)
        return state.item

    clients = {
        "s3": SimpleNamespace(
            get_bucket_versioning=lambda **kw: {"Status": state.versioning},
            list_object_versions=lambda **kw: state.versions,
        ),
        "dynamodb": SimpleNamespace(
            describe_table=lambda **kw: {
                "Table": {
                    "TableArn": "arn:aws:dynamodb:us-east-1:111122223333:table/fixture-lock"
                }
            },
            get_item=get_item,
        ),
    }
    return (
        state,
        SimpleNamespace(client=lambda name, **kw: clients[name]),
        backend,
        target,
    )


def test_backend_observation_checks_both_exact_lock_records(backend_observation):
    state, session, backend, target = backend_observation
    result = owner.empty_initial_backend(session, backend, target)
    assert result["versions_absent"] is True
    assert [call["Key"]["LockID"]["S"] for call in state.calls] == [
        "fixture-state/dev/modules/superplane-workspaces/v2/org/workspace/terraform.tfstate",
        "fixture-state/dev/modules/superplane-workspaces/v2/org/workspace/terraform.tfstate-md5",
    ]
    assert all(call["ConsistentRead"] is True for call in state.calls)


@pytest.mark.parametrize(
    "change",
    ["unversioned", "state", "delete-marker", "truncated", "lock", "account", "denied"],
)
def test_backend_observation_does_not_convert_uncertainty_to_absence(
    backend_observation, change
):
    state, session, backend, target = backend_observation
    if change == "unversioned":
        state.versioning = "Suspended"
    elif change == "state":
        state.versions = {"Versions": [{"VersionId": "existing"}]}
    elif change == "delete-marker":
        state.versions = {"DeleteMarkers": [{"VersionId": "deleted"}]}
    elif change == "truncated":
        state.versions = {"IsTruncated": True}
    elif change == "lock":
        state.item = {"Item": {"LockID": {"S": "owned"}}}
    elif change == "account":
        target["account_id"] = "444455556666"
    else:

        def denied(*args, **kwargs):
            raise PermissionError("AccessDenied")

        session.client = denied
    with pytest.raises((ValueError, PermissionError)):
        owner.empty_initial_backend(session, backend, target)


@pytest.mark.parametrize("maintenance", [False, True])
@pytest.mark.parametrize(
    "lock_refusal",
    [False, True, "socket", "saved", "saved-pricing", "saved-retry", "interrupted"],
)
def test_owner_attests_partial_init_only_with_bound_policy_and_backend(
    collection, tmp_path, monkeypatch, lock_refusal, maintenance
):
    from workspace_provisioning.terraform import copy_source, source_digest
    from workspace_provisioning.planning_boundary import snapshot, inspection_documents

    c = collection
    directory = tmp_path / "original"
    directory.mkdir()
    copy_source(directory / "module")
    (directory / "review").mkdir()
    authority = c.setup.doc["authority"]
    target = {k: authority[k] for k in ("account_id", "org_id", "workspace_id")}
    target.update(aws_region=authority["region"], environment="dev")
    (directory / "workspace.tfvars.json").write_text(json.dumps(target))
    config = {
        "environment": "dev",
        "backend": {
            "bucket": "fixture-state",
            "region": "us-east-1",
            "lock_table": "fixture-lock",
        },
    }
    backend = dict(config["backend"])
    backend["dynamodb_table"] = backend.pop("lock_table")
    backend.update(
        encrypt=True,
        key=f"dev/modules/superplane-workspaces/v2/{target['org_id']}/{target['workspace_id']}/terraform.tfstate",
    )
    path = directory / "module/.terraform/terraform.tfstate"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "terraform_version": "1.9.8",
                "backend": {"type": "s3", "config": backend, "hash": 123},
            }
        )
    )
    raw = json.dumps({"version": 1, "tenants": {target["org_id"]: {"runtime": config}}})
    digest = hashlib.sha256(raw.encode()).hexdigest()
    c.original["environment"]["paid_worker"].update(
        lifecycle_policy_configmap="original-policy",
        lifecycle_policy_sha256=digest,
    )
    c.original["lifecycle_foundations"]["ConfigMap"] = {"uid": "original-policy-uid"}
    c.pod["spec"]["volumes"].append(
        {"name": "policy", "configMap": {"name": "original-policy"}}
    )
    container = c.pod["spec"]["containers"][0]
    container["volumeMounts"].append(
        {"name": "policy", "mountPath": "/run/policy", "readOnly": True}
    )
    container["env"].append(
        {
            "name": "SUPERPLANE_LIFECYCLE_POLICY_FILE",
            "value": "/run/policy/lifecycle.json",
        }
    )
    c.objects["configmap"] = {
        "metadata": {"uid": "original-policy-uid"},
        "immutable": True,
        "data": {"lifecycle.json": raw},
    }
    c.report.update(
        operation_directory_exists=True,
        retained_entries=snapshot(directory),
        initialization_documents=inspection_documents(directory),
    )
    kwargs = {}
    if lock_refusal:
        import shutil
        from workspace_provisioning.planning_boundary import (
            AWS_PACKAGE,
            TLS_PACKAGE,
            ARCHIVE_ONLY_LOCK_SHA256,
            LOCKED_PACKAGES,
        )

        root = Path(owner.__file__).resolve().parents[1]
        historical = tmp_path / "historical"
        shutil.copytree(
            root / "workspace_provisioning", historical / "workspace_provisioning"
        )
        shutil.copytree(directory / "module", historical / "infra/workspaces")
        if lock_refusal is True or lock_refusal == "socket":
            # These recovery scopes attest historical recipes, whose immutable
            # pins must not change when the current preparation recipe changes.
            fixture = (
                root
                / "workspace_provisioning/tests/fixtures/prepare-workspace-plan-original.py.txt"
            )
            for location in (directory / "module", historical / "infra/workspaces"):
                shutil.copyfile(fixture, location / "scripts/prepare_workspace_plan.py")
        lock = (
            root
            / "workspace_provisioning/tests/fixtures/provider-lock-archive-only.hcl"
        )
        if lock_refusal is True:
            shutil.copyfile(lock, directory / "module/.terraform.lock.hcl")
            shutil.copyfile(lock, historical / "infra/workspaces/.terraform.lock.hcl")
        for package, version, name in (
            (AWS_PACKAGE, "6.65.0", "aws"),
            (TLS_PACKAGE, "4.4.1", "tls"),
        ):
            path = directory / package
            path.mkdir(parents=True)
            (path / "LICENSE.txt").write_text("fixture license")
            (path / f"terraform-provider-{name}_v{version}_x5").write_text(
                "fixture package"
            )
        c.report["retained_entries"] = snapshot(directory)
        c.report["provider_lock_refusal"] = {
            "version": 1,
            "terraform_version": "1.9.8",
            "terraform_sha256": "a" * 64,
            "lock_sha256": ARCHIVE_ONLY_LOCK_SHA256,
            "rejected_packages": LOCKED_PACKAGES,
            "diagnostic_sha256": "b" * 64,
        }
        if lock_refusal == "socket":
            from workspace_provisioning.provider_socket_boundary import (
                SOCKET_LOCK_SHA256,
                SOCKET_RUNTIME_HASHES,
            )

            shutil.copyfile(
                root
                / "workspace_provisioning/tests/fixtures/terraform-original.py.txt",
                historical / "workspace_provisioning/terraform.py",
            )
            c.report["runtime_sources"]["terraform.py"] = SOCKET_RUNTIME_HASHES[
                "terraform.py"
            ]
            shutil.copyfile(
                root
                / "workspace_provisioning/tests/fixtures/process-long-scratch.py.txt",
                historical / "workspace_provisioning/process.py",
            )
            c.report["runtime_sources"]["process.py"] = SOCKET_RUNTIME_HASHES[
                "process.py"
            ]
            del c.report["provider_lock_refusal"]
            c.report["provider_socket_refusal"] = {
                "version": 1,
                "terraform_version": "1.9.8",
                "terraform_sha256": "a" * 64,
                "lock_sha256": SOCKET_LOCK_SHA256,
                "runtime_sources": dict(SOCKET_RUNTIME_HASHES),
                "operation_directory_name": hashlib.sha256(
                    json.dumps(
                        c.report["identity"], sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
                "temporary_path_length": 122,
                "diagnostic_sha256": "b" * 64,
            }
        if str(lock_refusal).startswith("saved"):
            from workspace_provisioning import saved_plan_validation as saved

            kwargs["completion"] = True
            del c.report["provider_lock_refusal"]
            for name in saved.SAVED_PLAN_FILES:
                (directory / "review" / name).write_text("fixture " + name)
            c.report["retained_entries"] = saved.saved_plan_snapshot(directory)
            c.report["saved_plan"] = {
                "offline_guard_passed": True,
                "initial_create_only": True,
                "target": {**target, "workspace_name": "fixture"},
                "files": {
                    name: c.report["retained_entries"]["review/" + name]["sha256"]
                    for name in saved.SAVED_PLAN_FILES
                },
            }
            from workspace_provisioning import pricing_plan_validation as pricing

            pin_module, pin_name = (
                (pricing, "PRICING_RUNTIME")
                if lock_refusal == "saved-pricing"
                else (saved, "COMPLETION_RETRY_RUNTIME")
                if lock_refusal == "saved-retry"
                else (saved, "SAVED_PLAN_RUNTIME")
            )
            monkeypatch.setattr(
                pin_module,
                pin_name,
                {
                    name: hashlib.sha256(
                        (historical / "workspace_provisioning" / name).read_bytes()
                    ).hexdigest()
                    for name in saved.SAVED_PLAN_RUNTIME
                },
            )
            template = c.objects["scaledjob"]["spec"]["jobTargetRef"]["template"][
                "spec"
            ]
            c.objects["job"]["spec"] = probe_job(
                template,
                namespace=c.original["environment"]["namespace"],
                claim_name="state",
                identity=c.report["identity"],
                run_id="a" * 16,
                completion=True,
            )["spec"]
        if lock_refusal == "interrupted":
            from workspace_provisioning import interrupted_plan

            kwargs["interrupted"] = True
            del c.report["provider_lock_refusal"]
            # As with the saved-plan runtime fixtures above, qualify this synthetic
            # recipe only for the owner-join test. Production qualification stays
            # pinned to its historical bytes when the maintained module changes.
            monkeypatch.setattr(
                interrupted_plan,
                "QUALIFIED_MODULE_SHA256",
                source_digest(historical / "infra/workspaces"),
            )
            # Source-pin refusal is tested independently; this exercises the
            # complete owner join against retained files and the stopped pod.
            monkeypatch.setattr(
                interrupted_plan, "verify_source", lambda _: {"runtime.py": "a" * 64}
            )
            c.pod["metadata"] = {"uid": "pod-uid"}
            c.pod["status"] = {
                "containerStatuses": [
                    {
                        "state": {
                            "terminated": {
                                "finishedAt": (
                                    datetime.fromisoformat(
                                        c.descriptor["source_acquired_at"]
                                    )
                                    + timedelta(minutes=5)
                                ).isoformat()
                            }
                        }
                    }
                ]
            }
            c.objects["pod"] = c.pod
        kwargs["source_root"] = historical / "workspace_provisioning"
        monkeypatch.setattr(
            owner.subprocess, "check_output", lambda *args, **kw: "same-revision"
        )
    if maintenance:
        import copy

        c.original["release_id"] = "original-release"
        for key in ("account_id", "region", "cluster", "org_id", "adp_org_id"):
            c.original["environment"][key] = "original-" + key
        c.pod.setdefault("metadata", {})["creationTimestamp"] = "2026-01-02T00:00:00Z"
        c.objects["configmap"]["metadata"]["creationTimestamp"] = "2026-01-01T00:00:00Z"
        release = {
            "images": {"superplane-paid-worker": container["image"].rsplit("@", 1)[-1]}
        }
        kwargs["maintenance_receipt"] = {
            "status": "maintenance-complete",
            "installation_id": c.original["installation_id"],
            "release_id": owner.contract().digest(release),
            "release_lock": release,
            "environment": copy.deepcopy(c.original["environment"]),
            "maintenance": {
                "worker": container["image"],
                "regional_policy": "original-policy",
            },
        }
    checked = []

    def observe(session, backend, selected):
        assert selected == target and backend == config["backend"]
        checked.append(True)
        return {"versions_absent": True}

    monkeypatch.setattr(owner, "empty_initial_backend", observe)
    if lock_refusal == "interrupted":
        from installation import interrupted_plan_backend

        def interrupted_observe(session, backend, selected, **kw):
            assert kw["pod_name"] == c.descriptor["source_pod_name"]
            assert kw["acquired_at"] < kw["stopped_at"]
            return observe(session, backend, selected)

        monkeypatch.setattr(interrupted_plan_backend, "observe", interrupted_observe)
    proof = c.run(**kwargs)
    assert checked == [True]
    assert proof["preparation_boundary"]["kind"] == (
        "interrupted-read-only-plan-v1"
        if lock_refusal == "interrupted"
        else "saved-plan-finalization-v1"
        if str(lock_refusal).startswith("saved")
        else "provider-socket-refusal-v1"
        if lock_refusal == "socket"
        else "provider-lock-refusal-v1"
        if lock_refusal
        else "incomplete-provider-install-v1"
    )
    assert proof["preparation_boundary"]["entries"] == snapshot(directory)
    assert proof["planning_started"] is (
        str(lock_refusal).startswith("saved") or lock_refusal == "interrupted"
    )
    if str(lock_refusal).startswith("saved"):
        assert proof["apply_started"] is False
    if maintenance:
        c.objects["configmap"]["metadata"]["creationTimestamp"] = "2026-01-03T00:00:00Z"
    else:
        c.objects["configmap"]["metadata"]["uid"] = "replacement"
    with pytest.raises(ValueError):
        c.run(**kwargs)
    assert checked == [True]


def test_completion_probe_program_fits_linux_single_argument_limit():
    from installation.planning_probe import probe_program

    program = probe_program("org", "workspace", "operation", completion=True)
    assert len(program.encode()) < 128 * 1024 - 1
    captured = []
    exec(program, {"exec": captured.append})
    assert len(captured) == 1
    decoded = captured[0].decode()
    assert "reproduce_pricing" in decoded
    assert "regional-pricing.json" in decoded
    compile(decoded, "inert_probe", "exec")


def test_original_pod_audit_uses_management_region(provenance):
    session, _, args = provenance
    original_client = session.client
    calls = []

    def client(service, **kwargs):
        calls.append((service, kwargs))
        return original_client(service)

    session.client = client
    pod, _ = source_pod(session, **args, management_region="us-west-2")
    assert pod["metadata"]["uid"] == "original-uid"
    assert calls == [("logs", {"region_name": "us-west-2"})]


def test_collection_uses_separate_management_region(collection):
    c = collection
    c.setup.doc["management_region"] = "us-west-2"
    original_client = c.setup.session.client
    calls = []

    def client(service, **kwargs):
        calls.append((service, kwargs))
        return original_client(service, **kwargs)

    c.setup.session.client = client
    c.run()
    assert ("eks", {"region_name": "us-west-2"}) in calls
    assert ("dynamodb", {"region_name": "us-west-2"}) in calls
    assert ("sts", {"region_name": "us-east-1"}) in calls


@pytest.mark.parametrize(
    "change",
    [
        None,
        "files",
        "module",
        "source",
        "extension",
        "active-nodes",
        "cluster",
        "backend",
        "writable",
    ],
)
def test_partial_owner_keeps_actual_state_distinct_from_empty_backend(
    collection, monkeypatch, change
):
    from workspace_provisioning import saved_plan_validation, partial_apply_backend

    c = collection
    root = Path(owner.__file__).resolve().parents[1] / "workspace_provisioning"
    # Synthetic qualified image fixture: production historical pins are unchanged.
    monkeypatch.setattr(
        saved_plan_validation,
        "COMPLETION_RETRY_RUNTIME",
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in saved_plan_validation.COMPLETION_RETRY_RUNTIME
        },
    )
    authority = c.setup.doc["authority"]
    target = {k: authority[k] for k in ("account_id", "org_id", "workspace_id")}
    target.update(aws_region=authority["region"], environment="dev")
    observation = {
        "kind": "partial-apply-state-v1",
        "apply_started": True,
        "source_operation_id": "original-plan",
        "source_files": {
            name: "a" * 64
            for name in (
                "workspace.tfplan",
                "workspace-plan.json",
                "workspace-inventory.json",
                "workspace-estimate.json",
                "workspace-authorization.proposed.json",
                "workspace-backend.json",
            )
        },
        "module_sha256": "b" * 64,
        "apply_files": {"command-failure-original/stderr": "c" * 64},
    }
    c.descriptor.update(
        original_planning_operation_id="original-plan",
        original_artifact_id="d" * 64,
        original_artifact_files=dict(observation["source_files"]),
        original_module_sha256=observation["module_sha256"],
        source_version=2,
    )
    c.report.update(operation_directory_exists=True, partial_apply=observation)
    template = c.objects["scaledjob"]["spec"]["jobTargetRef"]["template"]["spec"]
    c.objects["job"]["spec"] = probe_job(
        template,
        namespace=c.original["environment"]["namespace"],
        claim_name="state",
        identity=c.report["identity"],
        run_id="a" * 16,
        partial_source="original-plan",
    )["spec"]
    config = {
        "environment": "dev",
        "backend": {
            "bucket": "fixture-state",
            "region": "us-east-1",
            "lock_table": "fixture-lock",
        },
    }
    raw = json.dumps({"version": 1, "tenants": {target["org_id"]: {"runtime": config}}})
    policy_sha = hashlib.sha256(raw.encode()).hexdigest()
    c.original["environment"]["paid_worker"].update(
        lifecycle_policy_configmap="original-policy", lifecycle_policy_sha256=policy_sha
    )
    for key in ("account_id", "region", "cluster", "org_id", "adp_org_id"):
        c.original["environment"][key] = "original-" + key
    c.original["lifecycle_foundations"]["ConfigMap"] = {"uid": "policy-uid"}
    c.pod["metadata"] = {"creationTimestamp": "2026-01-02T00:00:00Z"}
    c.pod["spec"]["volumes"].append(
        {"name": "policy", "configMap": {"name": "original-policy"}}
    )
    container = c.pod["spec"]["containers"][0]
    container["volumeMounts"].append(
        {"name": "policy", "mountPath": "/run/policy", "readOnly": True}
    )
    container["env"].append(
        {
            "name": "SUPERPLANE_LIFECYCLE_POLICY_FILE",
            "value": "/run/policy/lifecycle.json",
        }
    )
    c.objects["configmap"] = {
        "metadata": {"uid": "policy-uid", "creationTimestamp": "2026-01-01T00:00:00Z"},
        "immutable": True,
        "data": {"lifecycle.json": raw},
    }
    release = {
        "images": {"superplane-paid-worker": container["image"].rsplit("@", 1)[-1]}
    }
    maintenance = {
        "status": "maintenance-complete",
        "installation_id": c.original["installation_id"],
        "release_id": owner.contract().digest(release),
        "release_lock": release,
        "environment": copy.deepcopy(c.original["environment"]),
        "maintenance": {
            "worker": container["image"],
            "regional_policy": "original-policy",
        },
    }
    snapshot = {
        "version_id": "partial-version",
        "sha256": "e" * 64,
        "lineage": "original",
        "serial": 2,
        "resources": {
            "aws_eks_cluster.workspace": {
                "type": "aws_eks_cluster",
                "id": "cluster",
                "tainted": False,
            },
            "aws_eip.nat[0]": {
                "type": "aws_eip",
                "id": "eipalloc-0123456789abcdef0",
                "tainted": True,
            },
        },
        "cluster_arn": f"arn:aws:eks:{authority['region']}:{authority['account_id']}:cluster/cluster",
    }
    reads = []

    def read_snapshot(session, backend, selected):
        assert backend == config["backend"] and selected == target
        reads.append(True)
        if change == "backend":
            raise ValueError("backend unreadable")
        return snapshot

    monkeypatch.setattr(partial_apply_backend, "read_snapshot", read_snapshot)
    monkeypatch.setattr(
        owner,
        "empty_initial_backend",
        lambda *_: pytest.fail("partial state is not absence"),
    )
    client = c.setup.session.client

    def aws(service, **kwargs):
        if service == "eks":
            return SimpleNamespace(
                describe_cluster=lambda **_: {
                    "cluster": {
                        "name": "cluster",
                        "arn": snapshot["cluster_arn"],
                        "status": "FAILED" if change == "cluster" else "ACTIVE",
                    }
                },
                list_nodegroups=lambda **_: {
                    "nodegroups": ["unexpected"] if change == "active-nodes" else []
                },
            )
        return client(service, **kwargs)

    monkeypatch.setattr(c.setup.session, "client", aws)
    if change == "files":
        observation["source_files"]["workspace.tfplan"] = "f" * 64
    elif change == "module":
        observation["module_sha256"] = "f" * 64
    elif change == "source":
        observation["source_operation_id"] = "foreign"
    elif change == "extension":
        c.descriptor["attempt_extension"] = {"additional_attempts": 1}
    elif change == "writable":
        c.objects["job"]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][
            0
        ]["readOnly"] = False
    if change is not None:
        with pytest.raises(ValueError):
            c.run(partial_apply=True, maintenance_receipt=maintenance)
    else:
        before = c.setup.state.writes
        proof = c.run(partial_apply=True, maintenance_receipt=maintenance)
        assert (
            proof["record_id"].startswith("sppa1:") and proof["apply_started"] is True
        )
        assert proof["snapshot"] == snapshot and reads == [True]
        assert c.setup.state.writes == before


def test_fresh_partial_record_requires_new_collection_and_keeps_original_expiry(
    monkeypatch,
):
    import copy
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    original = {
        k: k
        for k in ("org_id", "workspace_id", "operation_id", "plan_digest", "source_key")
    }
    original.update(
        version=1,
        source_fence=1,
        record_id="sppa1:" + "a" * 64,
        checked_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    collections = []
    publications = []

    def collect(*args, **kwargs):
        collections.append(kwargs)
        return copy.deepcopy(original)

    monkeypatch.setattr(owner, "collect", collect)
    monkeypatch.setattr(
        owner,
        "_publish",
        lambda enrollment, evidence, session: (
            publications.append(copy.deepcopy(evidence))
            or {"record_id": evidence["record_id"]}
        ),
    )
    installer = SimpleNamespace(
        receipt={"remote_lock": True},
        owner="installed",
        run_id="1" * 16,
        save=lambda: None,
    )
    enrollment = {"authority": {"installation_id": "installed"}}
    owner.record(
        installer,
        enrollment,
        {},
        {},
        "probe",
        None,
        partial_apply=True,
        fresh_inspection=True,
        check_only=False,
    )
    assert len(collections) == len(publications) == 1
    current = publications[0]
    assert current["version"] == 2 and current["inspection_id"] == installer.run_id
    assert current["record_id"] != original["record_id"]
    for key in original.keys() - {"version", "record_id"}:
        assert current[key] == original[key]
    assert original["version"] == 1 and "inspection_id" not in original
    with pytest.raises(ValueError):
        owner.record(
            installer, enrollment, {}, {}, "probe", None, fresh_inspection=True
        )
    assert len(collections) == 1
