"""Approved assumptions are exact, explicit and cannot authorize arbitrary stdin."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from harness_jobs import bootstrap_provenance_assumption as contract
from harness_jobs import settled_bootstrap

from installation import bootstrap_diagnostics, planning_recovery
from installation.bootstrap_diagnostic_assumption import ApprovedDiagnosticAssumption


def sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def source(tmp_path, monkeypatch):
    pod = {
        "kind": "Pod",
        "metadata": {
            "namespace": "fixture",
            "name": "worker",
            "uid": "uid",
            "creationTimestamp": "2026-11-02T12:00:00Z",
        },
        "spec": {
            "containers": [
                {
                    "name": "paid-worker",
                    "image": "fixture@sha256:" + "a" * 64,
                    "securityContext": {"readOnlyRootFilesystem": True},
                }
            ],
            "volumes": [],
        },
        "status": {"phase": "Pending"},
    }
    actor = {
        "user_arn": "arn:aws:sts::123456789012:assumed-role/fixture/session",
        "principal_id": ["principal"],
        "canonical_arn": ["arn:aws:iam::123456789012:role/fixture"],
        "source_ips": ["127.0.0.1"],
        "user_agent": "fixture",
    }
    known = {
        "version": 1,
        "cluster": "fixture",
        "namespace": "fixture",
        "pod_name": "worker",
        "pod_uid": "uid",
        "image": pod["spec"]["containers"][0]["image"],
        "container_sha256": contract.digest(pod["spec"]["containers"][0]),
        "pod_volumes_sha256": contract.digest([]),
        "creation_event_id": "mutation-0",
        "terminal_event_ids": ["mutation-7"],
        "actors": {"actor": actor},
        "sources": {},
        "execs": [],
        "mutations": {},
        "protected_event_message_sha256": {},
    }
    events = []

    def event(key, value):
        raw = json.dumps(value)
        item = {
            "eventId": key,
            "logStreamName": "kube-apiserver-audit-fixture",
            "message": raw,
        }
        events.append(item)
        known["protected_event_message_sha256"][key] = sha(raw.encode())
        return item

    for i in range(8):
        observed = copy.deepcopy(pod)
        if i == 7:
            observed["status"]["phase"] = "Failed"
        value = {
            "auditID": "mutation-audit-" + str(i),
            "verb": "create" if i == 0 else "patch",
            "stage": "ResponseComplete",
            "stageTimestamp": "2026-11-02T13:00:00Z"
            if i == 7
            else "2026-11-02T12:00:00Z",
            "responseStatus": {"code": 201 if i == 0 else 200},
            "objectRef": {"resource": "pods", "namespace": "fixture", "name": "worker"},
            "responseObject": observed,
        }
        event("mutation-" + str(i), value)
        known["mutations"]["mutation-" + str(i)] = (
            bootstrap_diagnostics.mutation_identity(value)
        )
    scripts = []
    for i in range(89):
        mode = (
            "approved-stdin"
            if i >= 87
            else "process-list"
            if i == 86
            else "inline-python"
        )
        options = {"container": ["paid-worker"], "stderr": ["true"], "stdout": ["true"]}
        if mode == "process-list":
            command = ["ps", "-eo", "pid,ppid,comm,state,etime,time,wchan:24"]
            query_hash = contract.digest(command)
        else:
            code = "print(" + repr("local" + str(i) if i >= 87 else "inline") + ")\n"
            query_hash = sha(code.encode())
            path = tmp_path / (query_hash + ".py")
            path.write_text(code)
            known["sources"][query_hash] = str(path)
            command = (
                ["python", "-B", "-"]
                if mode == "approved-stdin"
                else ["python", "-B", "-c", code]
            )
            if mode == "approved-stdin":
                options["stdin"] = ["true"]
                scripts.append({"path": str(path), "sha256": query_hash})
        if i == 0 or i >= 86:
            options["timeout"] = ["15s"]
        uri = "/api/v1/namespaces/fixture/pods/worker/exec?" + urlencode(
            {"command": command, **options}, doseq=True
        )
        execution = {
            "audit_id": "exec-" + str(i),
            "time": "2026-11-02T12:30:00Z",
            "actor": "actor",
            "mode": mode,
            "query_hash": query_hash,
            "query_options": options,
            "source_origin": "local-tool-record-owner-assumed"
            if mode == "approved-stdin"
            else "protected-request-uri",
            "request_uri_sha256": sha(uri.encode()),
            "stages": [],
        }
        for stage in ("ResponseStarted", "ResponseComplete"):
            key = execution["audit_id"] + stage
            execution["stages"].append(
                {
                    "event_id": key,
                    "stage": stage,
                    "stage_timestamp": "2026-11-02T12:30:01Z",
                }
            )
            event(
                key,
                {
                    "auditID": execution["audit_id"],
                    "stage": stage,
                    "verb": "get",
                    "responseStatus": {"code": 101},
                    "requestReceivedTimestamp": execution["time"],
                    "stageTimestamp": "2026-11-02T12:30:01Z",
                    "requestURI": uri,
                    "objectRef": {
                        "resource": "pods",
                        "namespace": "fixture",
                        "name": "worker",
                        "apiVersion": "v1",
                        "subresource": "exec",
                    },
                    "user": {
                        "username": actor["user_arn"],
                        "extra": {
                            "arn": [actor["user_arn"]],
                            "principalId": actor["principal_id"],
                            "canonicalArn": actor["canonical_arn"],
                            "sessionName": ["session"],
                        },
                    },
                    "sourceIPs": actor["source_ips"],
                    "userAgent": actor["user_agent"],
                },
            )
        known["execs"].append(execution)
    closure = {"fixture": "closed-current-operation"}
    journal = {
        "generation": "generation",
        "claim": "claim",
        "operation_id": "operation",
        "org_id": "org",
        "workspace_id": "workspace",
        "cluster_arn": "cluster",
        "plan_json": json.dumps(
            {
                "grants": [
                    {"key": "grant"},
                    {"key": "already-revoked"},
                    {"key": "workspace-namespace"},
                ]
            }
        ),
        "progress_json": json.dumps(
            {
                "phase": "revoking",
                "grant": {"phase": "granted", "identity": {"uid": "grant"}},
                "already-revoked": {"phase": "revoked", "identity": {"uid": "gone"}},
                "workspace-namespace": {
                    "phase": "adopted",
                    "identity": {"uid": "namespace"},
                },
                "components": {"owned": True},
            }
        ),
        "revoked": False,
    }
    known.update(
        closure_sha256=contract.digest(closure),
        source_binding={
            "operation_id": "operation",
            "workspace_id": "workspace",
            "source_pod_name": "worker",
            "source_pod_uid": "uid",
            "source_image": known["image"],
        },
        journal_binding={
            k: journal[k] for k in contract.JOURNAL_FIELDS if k != "plan_sha256"
        },
        closed_current_journal=journal,
        closed_current_journal_sha256=contract.digest(journal),
    )
    known["journal_binding"]["plan_sha256"] = contract.digest(
        json.loads(journal["plan_json"])
    )
    approval = {
        "kind": "user-approved-two-session-provenance-assumption-v1",
        "user_response": "yes",
        "workspace_id": "workspace",
        "operation_id": "operation",
        "pod_uid": "uid",
        "image_digest": "sha256:" + "a" * 64,
        "audit_ids": ["exec-87", "exec-88"],
        "scripts": scripts,
    }
    proposal = copy.deepcopy(approval)
    paths = {k: tmp_path / (k + ".json") for k in ("manifest", "approval", "proposal")}
    paths["proposal"].write_text(json.dumps(proposal))
    approval["proposal_sha256"] = sha(paths["proposal"].read_bytes())
    paths["approval"].write_text(json.dumps(approval))
    paths["manifest"].write_text(json.dumps(known))
    for field, value in {
        "MANIFEST_SHA256": sha(paths["manifest"].read_bytes()),
        "APPROVAL_SHA256": sha(paths["approval"].read_bytes()),
        "PROPOSAL_SHA256": sha(paths["proposal"].read_bytes()),
        "CLOSURE_SHA256": known["closure_sha256"],
        "SOURCE_BINDING_SHA256": contract.digest(known["source_binding"]),
        "JOURNAL_BINDING_SHA256": contract.digest(known["journal_binding"]),
        "CLOSED_JOURNAL_SHA256": known["closed_current_journal_sha256"],
    }.items():
        monkeypatch.setattr(contract, field, value)
    environment_calls, queries = [], []
    monkeypatch.setattr(
        "installation.bootstrap_diagnostic_assumption.environment",
        lambda *a, **k: environment_calls.append(k),
    )

    def pages(**kwargs):
        queries.append(kwargs)
        yield {"events": events[:100]}
        yield {"events": events[100:]}

    session = SimpleNamespace(
        client=lambda *a, **k: SimpleNamespace(
            get_paginator=lambda _: SimpleNamespace(paginate=pages)
        )
    )
    kwargs = {
        "cluster": "fixture",
        "namespace": "fixture",
        "name": "worker",
        "uid": "uid",
        "acquired_at": datetime(2026, 11, 2, 12, 0, 1, tzinfo=UTC),
        "now": datetime(2026, 11, 2, 14, tzinfo=UTC),
        "bootstrap_diagnostics": True,
    }

    def context():
        return ApprovedDiagnosticAssumption(
            **{k + "_path": p for k, p in paths.items()}
        )

    return SimpleNamespace(
        known=known,
        events=events,
        closure=closure,
        journal=journal,
        paths=paths,
        context=context,
        session=session,
        kwargs=kwargs,
        environment_calls=environment_calls,
        queries=queries,
    )


def test_exact_private_inputs_yield_explicit_assumption(source):
    context = source.context()
    context.bind_descriptor(
        {**source.known["source_binding"], "settled_bootstrap": source.closure}
    )
    context.bind_journal(source.journal)
    _, audit = planning_recovery.source_pod(
        source.session, **source.kwargs, provenance_assumption=context
    )
    assert audit[contract.FIELD] == contract.metadata(contract.CLOSURE_SHA256)
    assert audit[contract.FIELD]["executed_stdin_verified"] is False
    assert len(source.environment_calls) == 1
    query = source.queries[0]
    assert "$.responseObject.metadata.name" in query["filterPattern"]
    assert all(
        '$.objectRef.subresource = "' + k + '"' in query["filterPattern"]
        for k in ("exec", "attach", "ephemeralcontainers")
    )
    assert query["endTime"] == int(source.kwargs["now"].timestamp() * 1000)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "replay",
        "actor",
        "stdin",
        "attach",
        "mutation",
        "pod",
        "late-page",
    ],
)
def test_changed_protected_history_refused(source, fault):
    context = source.context()
    if fault == "missing":
        source.events.pop()
    elif fault in {"extra", "replay"}:
        source.events.append(copy.deepcopy(source.events[-1]))
        if fault == "extra":
            source.events[-1]["eventId"] = "third-stdin-session"
    else:
        item = source.events[0] if fault in {"mutation", "pod"} else source.events[-1]
        value = json.loads(item["message"])
        if fault == "actor":
            value["user"]["username"] = "other"
        elif fault == "stdin":
            value["requestURI"] += "&command=other"
        elif fault == "attach":
            value["objectRef"]["subresource"] = "attach"
        elif fault == "pod":
            value["responseObject"]["spec"]["containers"][0]["image"] = "changed"
        else:
            value["stageTimestamp"] = "2026-11-02T12:59:00Z"
        item["message"] = json.dumps(value)
    with pytest.raises(ValueError):
        planning_recovery.source_pod(
            source.session, **source.kwargs, provenance_assumption=context
        )
    assert source.environment_calls == []


@pytest.mark.parametrize("document", ["approval", "proposal", "manifest", "script"])
def test_changed_local_input_refused(source, document):
    path = (
        source.paths[document]
        if document != "script"
        else next(iter(source.known["sources"].values()))
    )
    if document == "script":
        path = source.known["sources"][source.known["execs"][-1]["query_hash"]]
    from pathlib import Path

    Path(path).write_text("changed")
    with pytest.raises(ValueError):
        source.context()


def test_context_cannot_expand_another_source_or_skip_approval(source):
    context = source.context()
    with pytest.raises(ValueError):
        context.select("other", "fixture", "worker", "uid")
    with pytest.raises(ValueError):
        context.bind_descriptor(
            {**source.known["source_binding"], "settled_bootstrap": {"other": True}}
        )
    with pytest.raises(ValueError):
        planning_recovery.source_pod(source.session, **source.kwargs)


@pytest.mark.parametrize(
    "fault", [None, "short-retention", "recreated", "too-old", "changed-event"]
)
def test_approved_old_source_requires_retained_complete_protected_history(
    source, fault
):
    from datetime import timedelta

    source.kwargs["now"] = source.kwargs["acquired_at"] + timedelta(days=2)
    groups = [
        {
            "logGroupName": "/aws/eks/fixture/cluster",
            "creationTime": 0,
            "retentionInDays": 7,
        }
    ]
    if fault == "short-retention":
        groups[0]["retentionInDays"] = 1
    elif fault == "recreated":
        groups[0]["creationTime"] = int(source.kwargs["now"].timestamp() * 1000)
    elif fault == "too-old":
        source.kwargs["now"] = source.kwargs["acquired_at"] + timedelta(days=30)
    elif fault == "changed-event":
        source.events[-1]["message"] += " "

    original = source.session.client

    def client(*args, **kwargs):
        delegate = original(*args, **kwargs)
        return SimpleNamespace(
            get_paginator=lambda operation: SimpleNamespace(
                paginate=lambda **_: [{"logGroups": groups}]
            )
            if operation == "describe_log_groups"
            else delegate.get_paginator(operation)
        )

    source.session.client = client
    with pytest.raises(ValueError):
        planning_recovery.source_pod(source.session, **source.kwargs)
    if fault:
        with pytest.raises(ValueError):
            planning_recovery.source_pod(
                source.session, **source.kwargs, provenance_assumption=source.context()
            )
        assert source.environment_calls == []
    else:
        _, audit = planning_recovery.source_pod(
            source.session, **source.kwargs, provenance_assumption=source.context()
        )
        assert audit[contract.FIELD]["executed_stdin_verified"] is False
        assert source.queries[-1]["endTime"] == int(
            source.kwargs["now"].timestamp() * 1000
        )


@pytest.mark.parametrize(
    "fault", [None, "regress", "component", "namespace", "plan", "claim"]
)
def test_only_native_revocation_progress_can_advance(source, fault):
    row = copy.deepcopy(source.journal)
    progress = json.loads(row["progress_json"])
    progress["grant"]["phase"] = "revoked"
    progress.update(phase="revoked", complete=True)
    row["revoked"] = True
    if fault == "regress":
        progress["already-revoked"]["phase"] = "granted"
    elif fault == "component":
        progress["components"] = {}
    elif fault == "namespace":
        progress["workspace-namespace"]["identity"]["uid"] = "changed"
    elif fault in {"plan", "claim"}:
        row["plan_json" if fault == "plan" else "claim"] = "changed"
    row["progress_json"] = json.dumps(progress)
    if fault:
        with pytest.raises(ValueError):
            source.context().bind_journal(row)
    else:
        source.context().bind_journal(row)


def test_owner_proof_and_fresh_review_cannot_drop_or_relabel_assumption(source):
    expected = contract.metadata(contract.CLOSURE_SHA256)
    owner = {
        **source.known["source_binding"],
        "settled_bootstrap": source.closure,
        "evidence_kind": "bootstrap-reader-components-revoked-v1",
        contract.FIELD: expected,
        "source_audit": {contract.FIELD: expected},
        "revoked_bootstrap": {
            "current_generation": "generation",
            "journals": [source.known["journal_binding"]],
        },
    }
    proof = {"owner_evidence": owner, contract.FIELD: expected}
    assert contract.validate_proof(proof) == expected
    terms = {
        "version": 1,
        "kind": "settled-bootstrap-review-v1",
        "closure_sha256": contract.CLOSURE_SHA256,
        "runtime_amendment": {"runtime": "unchanged"},
        contract.FIELD: expected,
    }
    assert settled_bootstrap.review_runtime(terms) == {"runtime": "unchanged"}
    for location in (proof, owner, owner["source_audit"], terms):
        before = location.pop(contract.FIELD)
        with pytest.raises(ValueError):
            contract.validate_review(
                terms
            ) if location is terms else contract.validate_proof(proof)
        location[contract.FIELD] = before
    changed = copy.deepcopy(expected)
    changed["executed_stdin_verified"] = True
    proof[contract.FIELD] = changed
    with pytest.raises(ValueError):
        contract.validate_proof(proof)
