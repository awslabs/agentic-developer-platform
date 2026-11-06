"""The current stopped source requires all protected programs and no new event."""

import copy
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from installation import bootstrap_current_diagnostics as current
from installation import planning_recovery


@pytest.fixture
def history(monkeypatch):
    root = Path(__file__).with_name("fixtures")
    events = json.loads((root / "bootstrap-current-diagnostic-source.json").read_text())
    # The real protected history was independently qualified live. This fixture
    # redacts temporary access-key identifiers and binds the redacted test bytes.
    # Production profile() always checks the committed manifest's exact hash.
    from installation.bootstrap_diagnostics import digest, mutation_identity

    known = copy.deepcopy(current.profile())
    by_id = {e["eventId"]: json.loads(e["message"]) for e in events}
    known["protected_event_hashes"] = {
        e["eventId"]: hashlib.sha256(e["message"].encode()).hexdigest() for e in events
    }
    known["mutations"] = {
        key: mutation_identity(by_id[key]) for key in known["mutations"]
    }
    for run in known["execs"]:
        run["actor_sha256"] = digest(by_id[run["stages"][0]["event_id"]]["user"])
    monkeypatch.setattr(current, "profile", lambda: copy.deepcopy(known))
    environment = json.loads(
        (root / "bootstrap-current-diagnostic-configmap.json").read_text()
    )
    monkeypatch.setattr(
        planning_recovery, "kubectl", lambda *args: json.dumps(environment)
    )
    config_events = [
        {
            "logStreamName": "kube-apiserver-audit-test",
            "message": json.dumps(
                {
                    "verb": "get",
                    "objectRef": {"resource": "configmaps", "namespace": "superplane"},
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
                if "configmaps" in kwargs["filterPattern"]
                else events
            }
        ]

    client = SimpleNamespace(get_paginator=lambda name: SimpleNamespace(paginate=pages))
    session = SimpleNamespace(client=lambda *args, **kwargs: client)
    args = dict(
        cluster=current.IDENTITY[0],
        namespace=current.IDENTITY[1],
        name=current.IDENTITY[2],
        uid=current.IDENTITY[3],
        acquired_at=datetime(2026, 11, 4, 18, 51, 35, tzinfo=timezone.utc),
        now=datetime(2026, 11, 4, 21, 20, tzinfo=timezone.utc),
    )
    return session, events, environment, config_events, queries, args


def test_all_recorded_programs_qualify_without_extending_stdin_assumption(history):
    session, _, _, _, queries, args = history
    with pytest.raises(ValueError):
        planning_recovery.source_pod(session, **args)
    pod, audit = planning_recovery.source_pod(
        session, **args, bootstrap_diagnostics=True
    )
    assert pod["metadata"]["uid"] == current.IDENTITY[3]
    assert set(audit) == {"log_group", "creation_event_id", "terminal_event_ids"}
    assert "$.objectRef.subresource" in queries[-2]["filterPattern"]


@pytest.mark.parametrize(
    "change", ["extra", "missing", "actor", "program", "stdin", "attach", "pod"]
)
def test_changed_protected_history_refuses(history, change):
    session, events, _, _, _, args = history
    chosen = next(
        e
        for e in events
        if json.loads(e["message"]).get("objectRef", {}).get("subresource") == "exec"
    )
    if change == "extra":
        extra = copy.deepcopy(chosen)
        extra["eventId"] = "unrecorded-event"
        events.append(extra)
    elif change == "missing":
        events.remove(chosen)
    elif change == "pod":
        args["uid"] = "another-pod"
    else:
        value = json.loads(chosen["message"])
        if change == "actor":
            value["user"]["username"] = "another-actor"
        elif change == "program":
            value["requestURI"] += "&command=unreviewed"
        elif change == "stdin":
            value["requestURI"] = value["requestURI"].replace(
                "stdin=true", "stdin=false"
            )
        else:
            value["objectRef"]["subresource"] = "attach"
        chosen["message"] = json.dumps(value)
    with pytest.raises(ValueError):
        planning_recovery.source_pod(session, **args, bootstrap_diagnostics=True)


@pytest.mark.parametrize("change", ["data", "version", "mutation"])
def test_original_environment_must_remain_independently_proven(history, change):
    session, _, environment, config_events, _, args = history
    if change == "data":
        environment["data"]["PYTHONPATH"] = "/unreviewed"
    elif change == "version":
        environment["metadata"]["resourceVersion"] = "different"
    else:
        value = json.loads(config_events[0]["message"])
        value["verb"] = "patch"
        config_events[0]["message"] = json.dumps(value)
    with pytest.raises(ValueError):
        planning_recovery.source_pod(session, **args, bootstrap_diagnostics=True)


def test_manifest_and_program_hashes_are_not_caller_assertions(monkeypatch, tmp_path):
    (tmp_path / "qualification.json").write_text("{}")
    monkeypatch.setattr(current, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        current.profile()
