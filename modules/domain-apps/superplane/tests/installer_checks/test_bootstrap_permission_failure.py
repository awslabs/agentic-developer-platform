"""Authenticated logs must locate the original gate, not merely mention SSAR."""

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from installation import bootstrap_permission_failure as owner


def recorded_failure():
    # Sanitized original worker diagnostic, captured before recovery. Keeping
    # this independent of the collector catches drift in its historical parser.
    groups = [
        [("diagnostics.py", 38, "call"), ("runtime.py", 555, "hook")],
        [],
        [
            ("workspace.py", 851, "bootstrap_workspace"),
            ("temporary_authority.py", 143, "revoke"),
        ],
        [
            ("endpoint.py", 198, "_send_request"),
            ("endpoint.py", 132, "create_request"),
            ("hooks.py", 412, "emit"),
            ("hooks.py", 256, "emit"),
            ("hooks.py", 239, "_emit"),
            ("signers.py", 108, "handler"),
            ("signers.py", 191, "sign"),
            ("signers.py", 312, "get_auth_instance"),
            ("credentials.py", 697, "get_frozen_credentials"),
            ("credentials.py", 584, "_refresh"),
            ("credentials.py", 600, "_protected_refresh"),
            ("credentials.py", 32, "refresh"),
        ],
        [
            ("cluster_clients.py", 179, "self_subject_access_review"),
            ("cluster_clients.py", 156, "call_api"),
            ("api_client.py", 348, "call_api"),
            ("api_client.py", 166, "__call_api"),
            ("api_client.py", 524, "update_params_for_auth"),
            ("configuration.py", 344, "auth_settings"),
            ("configuration.py", 309, "get_api_key_with_prefix"),
            ("cluster_clients.py", 109, "refresh"),
            ("credentials.py", 697, "get_frozen_credentials"),
            ("credentials.py", 584, "_refresh"),
            ("credentials.py", 600, "_protected_refresh"),
            ("credentials.py", 32, "refresh"),
        ],
    ]
    return {
        "event": "lifecycle_provider_failed",
        "exceptions": [
            {
                "exception_type": kind,
                "frames": [
                    {"file": file, "line": line, "function": function}
                    for file, line, function in locations
                ],
            }
            for kind, locations in zip(
                [
                    "LifecycleRefused",
                    "BootstrapRefused",
                    "BootstrapRefused",
                    "LifecycleRefused",
                    "LifecycleRefused",
                ],
                groups,
                strict=True,
            )
        ],
    }


def test_standalone_verifier_rejects_a_different_event():
    event = recorded_failure()
    event["event"] = "another_failure"
    with pytest.raises(ValueError):
        owner.verify_failure(event)


@pytest.fixture
def setup():
    now = datetime.now(UTC)
    image = (
        "registry/worker@sha256:"
        "9fd2a13be7c5e147fe00cf5433fcd792ab751d27cd5ce86206c1d9bc326212bb"
    )
    source = owner.source_contract().qualified_source(image)
    identity = {
        "operation_id": "operation",
        "org_id": "org",
        "workspace_id": "workspace",
        "generation": "a" * 64,
        "plan_sha256": "b" * 64,
        "precomponent_progress_sha256": "c" * 64,
        "prerequisite_inventory": {"workspace_id": "workspace", "prerequisites": []},
    }
    evidence = {
        **{key: identity[key] for key in ("operation_id", "org_id", "workspace_id")},
        "source_image": image,
        "source_revision": source["revision"],
        "source_hashes": copy.deepcopy(source["hashes"]),
        "source_pod_name": "original-worker",
        "source_pod_uid": "original-uid",
        "workload_binding": "original-uid",
        "source_audit": {
            "log_group": "/aws/eks/management/cluster",
            "creation_event_id": "created",
            "terminal_event_ids": ["terminated"],
        },
        "source_acquired_at": (now - timedelta(hours=2)).isoformat(),
        "checked_at": (now - timedelta(minutes=30)).isoformat(),
    }
    metadata = {
        "pod_name": "original-worker",
        "pod_id": "original-uid",
        "container_name": "paid-worker",
        "namespace_name": "superplane",
        "container_hash": image,
        "docker_id": "d" * 64,
    }
    failure_time = now - timedelta(hours=1)
    diagnostic = recorded_failure()
    stopped = {
        "event": "paid_worker_stopped",
        "exception_type": "LifecycleRefused",
        "frames": [{"file": "task_worker.py", "line": 417, "function": "main"}],
    }
    outers = [
        {
            "time": (failure_time + timedelta(seconds=index)).isoformat(),
            "kubernetes": copy.deepcopy(metadata),
            "log": json.dumps(value),
        }
        for index, value in enumerate((diagnostic, stopped))
    ]
    return SimpleNamespace(
        source=source,
        identity=identity,
        evidence=evidence,
        outers=outers,
        diagnostic=diagnostic,
        stopped=stopped,
    )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing-failure",
        "missing-stop",
        "duplicate-failure",
        "duplicate-stop",
        "recovery-frame",
        "ssar-frame",
        "refresh-frame",
        "cause-order",
        "extra-cause",
        "source-hash",
        "source-revision",
        "source-flag",
        "pod-uid",
        "image",
        "container",
        "changed-container-id",
        "stream",
        "before-acquired",
        "after-checked",
        "stop-before-failure",
        "stop-type",
        "timestamp",
        "audit-group",
        "missing-termination",
        "identity",
    ],
)
def test_collector_requires_original_attributed_chain_across_all_pages(setup, fault):
    s = setup
    if fault in {"recovery-frame", "ssar-frame", "refresh-frame"}:
        which, frame = {
            "recovery-frame": (2, 1),
            "ssar-frame": (4, 0),
            "refresh-frame": (4, -1),
        }[fault]
        s.diagnostic["exceptions"][which]["frames"][frame]["function"] = "other"
    elif fault == "cause-order":
        s.diagnostic["exceptions"][-2:] = reversed(s.diagnostic["exceptions"][-2:])
    elif fault == "extra-cause":
        s.diagnostic["exceptions"].append(copy.deepcopy(s.diagnostic["exceptions"][-1]))
    elif fault == "stop-type":
        s.stopped["exception_type"] = "AnotherFailure"
    elif fault == "source-hash":
        s.evidence["source_hashes"]["workspace_provisioning/runtime.py"] = "0" * 64
    elif fault == "source-revision":
        s.evidence["source_revision"] = "0" * 40
    elif fault == "source-flag":
        s.source["precomponent_permission_refusal"] = False
    elif fault in {"pod-uid", "image", "container", "changed-container-id"}:
        key = {
            "pod-uid": "pod_id",
            "image": "container_hash",
            "container": "container_name",
            "changed-container-id": "docker_id",
        }[fault]
        s.outers[1]["kubernetes"][key] = "e" * 64
    elif fault == "before-acquired":
        s.outers[0]["time"] = (
            owner.instant(s.evidence["source_acquired_at"]) - timedelta(seconds=1)
        ).isoformat()
    elif fault == "after-checked":
        s.outers[1]["time"] = s.evidence["checked_at"]
    elif fault == "stop-before-failure":
        s.outers[0]["time"], s.outers[1]["time"] = (
            s.outers[1]["time"],
            s.outers[0]["time"],
        )
    elif fault == "audit-group":
        s.evidence["source_audit"]["log_group"] = "/aws/eks/other/cluster"
    elif fault == "missing-termination":
        s.evidence["source_audit"]["terminal_event_ids"] = []
    elif fault == "identity":
        s.identity["workspace_id"] = "another-workspace"
    s.outers[0]["log"] = json.dumps(s.diagnostic)
    s.outers[1]["log"] = json.dumps(s.stopped)
    events = [
        {
            "eventId": str(index),
            "timestamp": int(owner.instant(outer["time"]).timestamp() * 1000),
            "message": json.dumps(outer),
            "logStreamName": "node-application.var.log.containers."
            "original-worker_superplane_paid-worker-" + "d" * 64 + ".log",
        }
        for index, outer in enumerate(s.outers)
    ]
    if fault == "stream":
        events[1]["logStreamName"] = "another-" + events[1]["logStreamName"]
    elif fault == "timestamp":
        events[0]["timestamp"] += 1000
    elif fault in {"missing-failure", "missing-stop"}:
        events.pop(0 if fault == "missing-failure" else 1)
    elif fault in {"duplicate-failure", "duplicate-stop"}:
        events.append(copy.deepcopy(events[0 if fault == "duplicate-failure" else 1]))
    calls, seen = [], []

    class Logs:
        def get_paginator(self, operation):
            assert operation == "filter_log_events"
            return self

        def paginate(self, **kwargs):
            assert (
                kwargs["logGroupName"]
                == "/aws/containerinsights/management/application"
            )
            assert kwargs["filterPattern"] == '"original-worker"'
            assert kwargs["startTime"] < events[0]["timestamp"]
            for index, event in enumerate(events):
                seen.append(index)
                yield {"events": [event]}

    class Session:
        def client(self, service, **kwargs):
            calls.append((service, kwargs))
            assert service == "logs" and kwargs == {"region_name": "us-west-2"}
            return Logs()

    def run():
        return owner.collect(
            Session(),
            evidence=s.evidence,
            identity=s.identity,
            source=s.source,
            management_cluster="management",
            management_region="us-west-2",
        )

    if fault:
        with pytest.raises(ValueError):
            run()
    else:
        result = run()
        assert seen == [0, 1]
        assert calls == [("logs", {"region_name": "us-west-2"})]
        assert result["kind"] == owner.KIND
        assert result["failure_event_id"] == "0" and result["stopped_event_id"] == "1"
        assert (
            result["precomponent_progress_sha256"]
            == s.identity["precomponent_progress_sha256"]
        )
        assert result["source_audit_sha256"] == owner.digest(s.evidence["source_audit"])
        assert "exceptions" not in result and "kubernetes" not in result
