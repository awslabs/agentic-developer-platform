"""The original diagnostic provenance is finite and cannot excuse arbitrary exec."""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from installation import bootstrap_diagnostics as diagnostics
from installation.planning_recovery import source_pod


@pytest.fixture(params=["original", "components"])
def history(monkeypatch, request):
    from installation import planning_recovery

    components = request.param == "components"
    root = diagnostics.COMPONENT_ROOT if components else diagnostics.ROOT
    prefix = "bootstrap-components-diagnostic" if components else "bootstrap-diagnostic"
    known = json.loads((root / "qualification.json").read_text())
    events = json.loads(
        (Path(__file__).with_name("fixtures") / (prefix + "-source.json")).read_text()
    )
    for execution in known["execs"]:
        code = (root / known["sources"][execution["query_hash"]]).read_text()
        command = ["python", "-B"] if components else ["/opt/executor/bin/python", "-S"]
        query = [("command", value) for value in (*command, "-c", code)]
        query += [
            ("container", "paid-worker"),
            ("stderr", "true"),
            *(([("stdin", "true")]) if not components else []),
            ("stdout", "true"),
        ]
        uri = (
            f"/api/v1/namespaces/{known['namespace']}/pods/{known['pod_name']}/exec?"
            + urlencode(query)
        )
        actor = known["actors"][execution["actor"]] if components else execution
        for stage in execution["stages"]:
            value = {
                "auditID": execution["audit_id"],
                "stage": stage["stage"],
                "verb": "get",
                "responseStatus": {"code": 101},
                "requestReceivedTimestamp": execution["time"],
                "stageTimestamp": stage["stage_timestamp"],
                "requestURI": uri,
                "objectRef": {
                    "resource": "pods",
                    "namespace": known["namespace"],
                    "name": known["pod_name"],
                    "apiVersion": "v1",
                    "subresource": "exec",
                },
                "user": {
                    "username": actor["user_arn"],
                    "extra": {
                        "arn": [actor["user_arn"]],
                        "principalId": actor["principal_id"],
                        "canonicalArn": actor["canonical_arn"],
                        "sessionName": [actor["user_arn"].rsplit("/", 1)[1]],
                    },
                },
                "sourceIPs": actor["source_ips"],
                "userAgent": actor["user_agent"],
            }
            events.append(
                {
                    "eventId": stage["event_id"],
                    "logStreamName": "kube-apiserver-audit-original",
                    "message": json.dumps(value),
                }
            )
    environment = json.loads(
        (
            Path(__file__).with_name("fixtures") / (prefix + "-configmap.json")
        ).read_text()
    )
    monkeypatch.setattr(
        planning_recovery, "kubectl", lambda *args: json.dumps(environment)
    )
    config_events = [
        {
            "logStreamName": "kube-apiserver-audit-original",
            "message": json.dumps(
                {
                    "verb": "get",
                    "objectRef": {
                        "resource": "configmaps",
                        "namespace": "superplane",
                        "name": "superplane-paid-worker-config",
                    },
                }
            ),
        }
    ]
    queries = []

    def pages(**kwargs):
        queries.append(kwargs)
        return [
            {
                "events": config_events
                if "superplane-paid-worker-config" in kwargs["filterPattern"]
                else events
            }
        ]

    client = SimpleNamespace(get_paginator=lambda name: SimpleNamespace(paginate=pages))
    session = SimpleNamespace(
        client=lambda *args, **kwargs: client,
        fixture_environment=environment,
        fixture_config_events=config_events,
        fixture_queries=queries,
    )
    args = dict(
        cluster=known["cluster"],
        namespace=known["namespace"],
        name=known["pod_name"],
        uid=known["pod_uid"],
        acquired_at=(
            datetime(2026, 11, 2, 13, 23, 23, tzinfo=timezone.utc)
            if components
            else datetime(2026, 10, 28, 21, 24, tzinfo=timezone.utc)
        ),
        now=(
            datetime(2026, 11, 2, 16, 30, tzinfo=timezone.utc)
            if components
            else datetime(2026, 10, 28, 23, 20, tzinfo=timezone.utc)
        ),
    )
    return session, events, args


def test_exact_original_diagnostics_are_bootstrap_only(history):
    session, _, args = history
    with pytest.raises(ValueError):
        source_pod(session, **args)
    pod, evidence = source_pod(session, **args, bootstrap_diagnostics=True)
    assert pod["metadata"]["uid"] == args["uid"]
    assert set(evidence) == {"log_group", "creation_event_id", "terminal_event_ids"}


@pytest.mark.parametrize(
    "change",
    [
        "missing-stage",
        "all-missing",
        "extra-stage",
        "replayed-stage",
        "event-id",
        "audit-id",
        "command",
        "operator",
        "principal",
        "time",
        "status",
        "attach",
        "ephemeral",
        "source-image",
        "source-environment",
        "source-mount",
        "source-workdir",
        "source-readonly",
    ],
)
@pytest.mark.parametrize("historical", [False, True])
def test_changed_diagnostic_provenance_refused(history, change, historical):
    session, events, args = history
    if change == "missing-stage":
        events.pop()
    elif change == "all-missing":
        events[:] = [
            e
            for e in events
            if json.loads(e["message"]).get("objectRef", {}).get("subresource")
            != "exec"
        ]
    elif change in {"extra-stage", "replayed-stage"}:
        events.append(copy.deepcopy(events[-1]))
        if change == "extra-stage":
            events[-1]["eventId"] = "new-event"
    elif change == "event-id":
        events[-1]["eventId"] = "new-event"
    elif change.startswith("source-"):
        for event in events:
            value = json.loads(event["message"])
            if "spec" not in value.get("responseObject", {}):
                continue
            container = value["responseObject"]["spec"]["containers"][0]
            if change == "source-image":
                container["image"] += "different"
            elif change == "source-environment":
                container["env"].append({"name": "PYTHONPATH", "value": "/tmp"})
            elif change == "source-workdir":
                container["workingDir"] = "/tmp"
            elif change == "source-readonly":
                container["securityContext"]["readOnlyRootFilesystem"] = False
            else:
                container["volumeMounts"].append(
                    {"name": "temporary", "mountPath": "/opt/executor"}
                )
            event["message"] = json.dumps(value)
    else:
        value = json.loads(events[-1]["message"])
        if change == "audit-id":
            value["auditID"] = "new-audit"
        elif change == "command":
            value["requestURI"] += "&command=unexpected"
        elif change == "operator":
            value["user"]["username"] += "-other"
        elif change == "principal":
            value["user"]["extra"]["principalId"] = ["different-role"]
        elif change == "time":
            value["stageTimestamp"] = "2026-10-28T23:00:00Z"
        elif change == "status":
            value["responseStatus"]["code"] = 200
        else:
            value["objectRef"]["subresource"] = (
                "attach" if change == "attach" else "ephemeralcontainers"
            )
        events[-1]["message"] = json.dumps(value)

    def never_qualify(*args):
        raise AssertionError("Invalid source audit reached environment qualification")

    with pytest.raises(ValueError):
        source_pod(
            session,
            **args,
            bootstrap_diagnostics=True,
            environment_history=SimpleNamespace(qualify=never_qualify)
            if historical
            else None,
        )


@pytest.mark.parametrize("change", ["data", "uid", "version", "mutation", "no-history"])
def test_original_envfrom_requires_current_data_and_complete_mutation_history(
    history, change
):
    session, _, args = history
    if change == "data":
        session.fixture_environment["data"]["PYTHONPATH"] = "/tmp"
    elif change == "uid":
        session.fixture_environment["metadata"]["uid"] = "replacement"
    elif change == "version":
        session.fixture_environment["metadata"]["resourceVersion"] = "replacement"
    elif change == "no-history":
        session.fixture_config_events.clear()
    else:
        value = json.loads(session.fixture_config_events[0]["message"])
        value["verb"] = "patch"
        session.fixture_config_events[0]["message"] = json.dumps(value)
    with pytest.raises(ValueError):
        source_pod(session, **args, bootstrap_diagnostics=True)


@pytest.mark.parametrize("history", ["components"], indirect=True)
def test_component_history_retains_generated_creation_and_every_mutation(history):
    session, _, args = history
    source_pod(session, **args, bootstrap_diagnostics=True)
    query = session.fixture_queries[0]
    pattern = query["filterPattern"]
    assert '$.objectRef.resource = "pods"' in pattern
    assert '$.responseObject.metadata.name = "' + args["name"] + '"' in pattern
    assert all(
        '$.objectRef.subresource = "' + s + '"' in pattern
        for s in ("exec", "attach", "ephemeralcontainers")
    )
    assert '($.verb != "get" && $.verb != "list" && $.verb != "watch")' in pattern
    assert query["endTime"] == int(args["now"].timestamp() * 1000)


@pytest.mark.parametrize("history", ["components"], indirect=True)
@pytest.mark.parametrize("change", ["missing", "extra", "id", "payload", "uid"])
def test_component_mutation_history_is_finite(history, change):
    session, events, args = history
    if change == "missing":
        events.pop(1)
    elif change == "extra":
        events.append(copy.deepcopy(events[1]))
    elif change == "id":
        events[1]["eventId"] = "another-mutation"
    else:
        value = json.loads(events[1 if change == "payload" else 0]["message"])
        if change == "payload":
            value["requestObject"] = {"changed": True}
        else:
            value["responseObject"]["metadata"]["uid"] = "another-lifetime"
        events[1 if change == "payload" else 0]["message"] = json.dumps(value)
    with pytest.raises(ValueError):
        source_pod(session, **args, bootstrap_diagnostics=True)


@pytest.mark.parametrize("history", ["components"], indirect=True)
def test_component_source_cannot_reuse_older_historical_environment(history):
    session, _, args = history
    with pytest.raises(ValueError):
        source_pod(
            session,
            **args,
            bootstrap_diagnostics=True,
            environment_history=SimpleNamespace(qualify=lambda *a: {}),
        )
