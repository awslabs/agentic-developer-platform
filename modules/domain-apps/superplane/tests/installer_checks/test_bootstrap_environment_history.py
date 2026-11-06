"""Retained envFrom provenance never excuses early or unknown mutations."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from installation import bootstrap_environment_history as history
from installation import bootstrap_recovery, bootstrap_revoked


@pytest.fixture
def retained(tmp_path, monkeypatch):
    created = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    ended = created + timedelta(minutes=30)
    snapshot_at = ended + timedelta(minutes=1)
    checked = ended + timedelta(minutes=2)
    authority = {
        k: k
        for k in (
            "org_id",
            "workspace_id",
            "credential_id",
            "installation_id",
            "adp_org_id",
            "account_id",
            "region",
        )
    }
    enrollment = {
        "authority": authority,
        "environment": "test",
        "operator_role_arn": "owner",
        "operator_role_id": "owner-id",
    }
    descriptor = {
        k: k
        for k in (
            "org_id",
            "workspace_id",
            "operation_id",
            "plan_digest",
            "source_key",
            "source_principal",
            "source_pod_name",
        )
    }
    descriptor.update(
        source_fence=1,
        source_pod_uid="source",
        workload_binding="source",
        source_acquired_at=created.isoformat(),
    )
    record_id = "sppr1:bootstrap:" + history.digest(
        {
            k: descriptor[k]
            for k in (
                "org_id",
                "workspace_id",
                "operation_id",
                "plan_digest",
                "source_key",
                "source_fence",
            )
        }
    )
    audit = {
        "log_group": "group",
        "creation_event_id": "created",
        "terminal_event_ids": ["ended"],
    }
    prior = {
        **authority,
        **descriptor,
        "version": 1,
        "record_id": record_id,
        "source_image": "image",
        "source_revision": "revision",
        "source_hashes": {"code": "hash"},
        "source_audit": copy.deepcopy(audit),
        "checked_at": checked.isoformat(),
        "expires_at": (checked + timedelta(hours=1)).isoformat(),
        "evidence_kind": "revoked",
    }
    config = {
        "kind": "ConfigMap",
        "apiVersion": "v1",
        "metadata": {
            "name": "environment",
            "namespace": "namespace",
            "uid": "config-uid",
            "creationTimestamp": (created - timedelta(hours=1)).isoformat(),
            "resourceVersion": "10",
        },
        "data": {"SCHEMA": "original"},
    }
    snapshot = {"configmap": config, "observed_at": snapshot_at.isoformat()}
    env, release = {"region": "region"}, {"images": {"api": "image"}}
    installer = SimpleNamespace(
        env=env, lock=release, release=history.digest(release), owner="installation_id"
    )
    original = {"installation_id": installer.owner, "environment": env}
    refs = {"snapshot": snapshot, "maintenance": []}
    events = []
    before = history.runtime.payload(config)
    for index in (1, 2):
        after = copy.deepcopy(before)
        after["data"]["SCHEMA"] = f"replacement-{index}"
        plan = {
            "installation_id": installer.owner,
            "release_id": installer.release,
            "environment_sha256": history.digest(env),
            "before": [before],
            "after": [after],
            "database": {"revision": f"replacement-{index}"},
        }
        receipt = {
            "installation_id": installer.owner,
            "release_id": installer.release,
            "release_lock": release,
            "environment": env,
            "status": "runtime-maintenance-complete",
            "runtime_maintenance": {
                "plan_sha256": history.digest(plan),
                "state": "observed-paused",
            },
        }
        refs["maintenance"].append({"plan": plan, "receipt": receipt})
        event = {
            "verb": "patch",
            "stage": "ResponseComplete",
            "responseStatus": {"code": 200},
            "objectRef": {
                "resource": "configmaps",
                "namespace": "namespace",
                "name": "environment",
            },
            "user": {"extra": {"canonicalArn": ["owner"], "principalId": ["owner-id"]}},
            "requestReceivedTimestamp": (
                checked + timedelta(minutes=index)
            ).isoformat(),
            "stageTimestamp": (
                checked + timedelta(minutes=index, seconds=1)
            ).isoformat(),
        }
        events.append(
            {"logStreamName": "kube-apiserver-audit-test", "message": json.dumps(event)}
        )
        before = after
    read = json.loads(events[0]["message"])
    read.update(verb="get", stageTimestamp=(created + timedelta(minutes=1)).isoformat())
    events.insert(
        0, {"logStreamName": "kube-apiserver-audit-test", "message": json.dumps(read)}
    )
    pins = {
        "version": 1,
        "original_receipt_sha256": history.digest(original),
        "snapshot_sha256": history.digest(snapshot),
        "predecessor_revision": history.digest(prior),
        "maintenance": [
            {
                "plan_sha256": history.digest(r["plan"]),
                "receipt_sha256": history.digest(r["receipt"]),
                "mutation_message_sha256": hashlib.sha256(
                    e["message"].encode()
                ).hexdigest(),
            }
            for r, e in zip(refs["maintenance"], events[1:])
        ],
    }
    requests, checks = [], []
    item = {
        "record_id": {"S": record_id},
        "document": {"S": history.contract().canonical(prior)},
        "revision": {"S": history.digest(prior)},
    }

    def client(name, **kwargs):
        if name == "dynamodb":

            def get_item(**kwargs):
                assert kwargs["ConsistentRead"] is True
                requests.append(kwargs)
                return {"Item": copy.deepcopy(item)}

            return SimpleNamespace(get_item=get_item)
        assert name == "logs"

        def paginate(**kwargs):
            requests.append(kwargs)
            return [{"events": events}]

        return SimpleNamespace(
            get_paginator=lambda name: SimpleNamespace(paginate=paginate)
        )

    session = SimpleNamespace(client=client)
    source = SimpleNamespace(
        qualified_source=lambda image: {
            "revision": "revision",
            "hashes": {"code": "hash"},
        }
    )
    monkeypatch.setattr(bootstrap_recovery, "source_contract", lambda: source)

    def validate(value):
        assert value["evidence_kind"] == "revoked"
        checks.append("structure")

    monkeypatch.setattr(
        bootstrap_revoked,
        "revoked_contract",
        lambda: SimpleNamespace(validate_revoked=validate),
    )
    monkeypatch.setattr(history, "ROOT", tmp_path)
    current = {"owner": "installation_id", "lock": "lock"}
    monkeypatch.setattr(
        history, "current_context", lambda *args: copy.deepcopy(current)
    )
    objects = copy.deepcopy(refs["maintenance"][-1]["plan"]["after"])
    monkeypatch.setattr(history.runtime, "objects", lambda installer: objects)
    monkeypatch.setattr(
        history.runtime, "paused", lambda *args: checks.append("paused")
    )
    monkeypatch.setattr(
        history.runtime, "verify_rollout", lambda *args: checks.append("rollout")
    )
    monkeypatch.setattr(
        history.runtime,
        "verify_schema",
        lambda *args: refs["maintenance"][-1]["plan"]["database"],
    )
    known = {
        "environment_configmap": {
            "name": "environment",
            "namespace": "namespace",
            "uid": "config-uid",
            "resource_version": "10",
            "data_sha256": history.digest(config["data"]),
        }
    }
    pod = {
        "spec": {
            "containers": [
                {
                    "image": "image",
                    "envFrom": [{"configMapRef": {"name": "environment"}}],
                    "env": [],
                    "volumeMounts": [],
                }
            ],
            "volumes": [],
        }
    }

    def build():
        (tmp_path / "environment-history-fingerprints.json").write_text(
            json.dumps(pins)
        )
        return history.HistoricalEnvironment(
            installer, enrollment, original, descriptor, session, refs
        )

    return SimpleNamespace(**locals())


def test_expired_predecessor_is_historical_only_and_current_checks_repeat(retained):
    r = retained
    before = copy.deepcopy(r.refs)
    value = r.build()
    result = value.qualify(r.known, r.pod, r.created, r.ended, r.audit)
    value.finish()
    assert r.refs == before
    assert result["predecessor_revision"] == r.pins["predecessor_revision"]
    assert "expires_at" not in result
    assert r.checks == ["structure", "paused", "rollout", "paused", "rollout"]
    audit_read = next(x for x in r.requests if "endTime" in x)
    assert audit_read["endTime"] > int(r.checked.timestamp() * 1000)


@pytest.mark.parametrize(
    "fault",
    [
        "snapshot",
        "plan",
        "receipt",
        "prior",
        "prior-revision",
        "original",
        "current-runtime",
        "current-release",
        "identity",
        "source-image",
        "terminal-loss",
    ],
)
def test_changed_private_or_protected_history_refused(retained, fault):
    r = retained
    if fault == "snapshot":
        r.refs["snapshot"]["configmap"]["data"]["PYTHONPATH"] = "/tmp"
    elif fault in {"plan", "receipt"}:
        r.refs["maintenance"][0][fault]["extra"] = "changed"
    elif fault == "prior":
        r.item["document"]["S"] += " "
    elif fault == "prior-revision":
        r.item["revision"]["S"] = "wrong"
    elif fault == "original":
        r.original["installation_id"] = "wrong"
    elif fault == "current-runtime":
        r.objects[0]["data"]["SCHEMA"] = "wrong"
    elif fault == "current-release":
        r.installer.release = "wrong"
    elif fault == "identity":
        r.descriptor["source_pod_uid"] = "wrong"
    elif fault == "source-image":
        r.pod["spec"]["containers"][0]["image"] = "wrong"
    else:
        r.audit["terminal_event_ids"] = []
    with pytest.raises(ValueError):
        value = r.build()
        value.qualify(r.known, r.pod, r.created, r.ended, r.audit)


@pytest.mark.parametrize(
    "fault",
    [
        "early",
        "before-proof",
        "unknown",
        "missing",
        "duplicate",
        "owner",
        "status",
        "no-read",
        "uid",
        "version",
        "projected",
        "volume",
        "subpath",
        "env-key",
    ],
)
def test_audit_and_start_time_environment_boundary_refused(retained, fault):
    r = retained
    if fault in {"early", "before-proof", "owner", "status"}:
        event = json.loads(r.events[1]["message"])
        if fault in {"early", "before-proof"}:
            at = r.created if fault == "early" else r.ended + timedelta(seconds=1)
            event.update(
                requestReceivedTimestamp=at.isoformat(), stageTimestamp=at.isoformat()
            )
        elif fault == "owner":
            event["user"]["extra"]["principalId"] = ["foreign"]
        else:
            event["responseStatus"]["code"] = 403
        r.events[1]["message"] = json.dumps(event)
        # Even a reviewed fingerprint cannot excuse a violated safety boundary.
        r.pins["maintenance"][0]["mutation_message_sha256"] = hashlib.sha256(
            r.events[1]["message"].encode()
        ).hexdigest()
    elif fault == "unknown":
        extra = copy.deepcopy(r.events[1])
        extra["message"] += " "
        r.events.append(extra)
    elif fault == "missing":
        r.events.pop()
    elif fault == "duplicate":
        r.events.append(r.events[-1])
    elif fault == "no-read":
        r.events.pop(0)
    elif fault in {"uid", "version"}:
        r.known["environment_configmap"][
            "uid" if fault == "uid" else "resource_version"
        ] = "wrong"
    elif fault == "projected":
        r.pod["spec"]["volumes"] = [
            {"projected": {"sources": [{"configMap": {"name": "environment"}}]}}
        ]
    elif fault == "volume":
        r.pod["spec"]["volumes"] = [{"configMap": {"name": "environment"}}]
    elif fault == "subpath":
        r.pod["spec"]["containers"][0]["volumeMounts"] = [{"subPath": "file"}]
    else:
        r.pod["spec"]["containers"][0]["env"] = [
            {"valueFrom": {"configMapKeyRef": {"name": "environment"}}}
        ]
    with pytest.raises(ValueError):
        value = r.build()
        value.qualify(r.known, r.pod, r.created, r.ended, r.audit)


def test_finish_refuses_current_context_drift(retained):
    value = retained.build()
    retained.current["lock"] = "replacement"
    with pytest.raises(ValueError):
        value.finish()
