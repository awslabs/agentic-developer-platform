import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest

from installation import planning_diagnostics as diagnostics


@pytest.fixture
def source():
    now = datetime.now(UTC)
    pod = {
        "metadata": {
            "name": "original",
            "namespace": "workspaces",
            "uid": "original-uid",
            "creationTimestamp": (now - timedelta(minutes=10)).isoformat(),
        },
        "spec": {
            "containers": [
                {
                    "name": "paid-worker",
                    "securityContext": {"readOnlyRootFilesystem": True},
                }
            ]
        },
        "status": {
            "containerStatuses": [
                {"state": {"terminated": {"finishedAt": now.isoformat()}}}
            ]
        },
    }
    operator = {
        "arn": "arn:aws:iam::123456789012:role/owner",
        "role_id": "AROA" + "A" * 17,
    }
    program = (
        Path(diagnostics.__file__).with_name("diagnostics") / "process_names.txt"
    ).read_text()
    query = {
        "command": ["python", "-c", program],
        "container": ["paid-worker"],
        "stderr": ["true"],
        "stdout": ["true"],
    }
    value = {
        "auditID": "audit-id",
        "verb": "get",
        "responseStatus": {"code": 101},
        "objectRef": {
            "resource": "pods",
            "subresource": "exec",
            "name": "original",
            "namespace": "workspaces",
        },
        "requestURI": "/api/v1/namespaces/workspaces/pods/original/exec?"
        + urlencode(query, doseq=True),
        "requestReceivedTimestamp": (now - timedelta(minutes=5)).isoformat(),
        "stageTimestamp": (now - timedelta(minutes=4)).isoformat(),
        "user": {
            "extra": {
                "canonicalArn": [operator["arn"]],
                "principalId": [operator["role_id"]],
            }
        },
    }
    events = [
        ({"eventId": stage}, {**copy.deepcopy(value), "stage": stage})
        for stage in ("ResponseStarted", "ResponseComplete")
    ]
    return pod, operator, events, query


@pytest.mark.parametrize(
    "change",
    [
        None,
        "stdin",
        "program",
        "operator",
        "attach",
        "missing-stage",
        "foreign-pod",
        "after-stop",
    ],
)
def test_only_exact_operator_read_programs_preserve_provenance(
    source, monkeypatch, change
):
    pod, operator, events, query = source
    monkeypatch.setattr(diagnostics, "environment", lambda *_: [])
    if change == "missing-stage":
        events.pop()
    for _, value in events:
        if change == "stdin":
            query["stdin"] = ["true"]
        elif change == "program":
            query["command"][-1] += "\nPath('/run/state/evidence').unlink()"
        elif change == "operator":
            value["user"]["extra"]["principalId"] = ["different-role"]
        elif change == "attach":
            value["objectRef"]["subresource"] = "attach"
        elif change == "foreign-pod":
            value["objectRef"]["name"] = "other"
        elif change == "after-stop":
            value["stageTimestamp"] = (
                datetime.now(UTC) + timedelta(hours=1)
            ).isoformat()
        value["requestURI"] = (
            "/api/v1/namespaces/workspaces/pods/original/exec?"
            + urlencode(query, doseq=True)
        )
    args = dict(
        cluster="management",
        pod=pod,
        terminals=[({}, pod)],
        session=None,
        management_region="us-east-1",
        operator=operator,
    )
    if change:
        with pytest.raises(ValueError):
            diagnostics.qualify(events, **args)
    else:
        result = diagnostics.qualify(events, **args)
        assert result["executions"][0]["audit_id"] == "audit-id"
        assert len(result["executions"][0]["event_ids"]) == 2
