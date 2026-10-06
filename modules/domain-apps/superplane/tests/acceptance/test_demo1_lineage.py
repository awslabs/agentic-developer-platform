"""Offline continuation ancestry using the maintained immutable artifact validator."""

import asyncio
import copy
import json
import runpy
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_demo1_journey as journey_fixtures
import test_demo1_ownership as ownership_fixtures
from test_demo1_cli import identifier
from test_demo1_current import records_for
from test_demo1_runtime import Producer

from superplane_acceptance import demo1_browser, demo1_journey
from superplane_acceptance._demo1_lineage_probe import collect
from superplane_acceptance.demo1_browser import CreationCheckpoint
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError
from superplane_acceptance.demo1_lineage import lineage_report, observe_lineage
from superplane_acceptance.demo1_report import LIFECYCLE_PHASES, reference
from superplane_acceptance.demo1_runtime import RuntimeReader, RuntimeTarget
from workspace_provisioning.artifacts import continuation_parameters
from workspace_provisioning.runtime_config import LifecycleRefused

driver = journey_fixtures.driver
producer_imports = ownership_fixtures.producer_imports


class Records(ownership_fixtures.Records):
    def __init__(self, *, bootstrap=False, browser=False):
        self.__dict__.update(
            records_for(identifier(10), identifier(12)).__dict__
            if browser
            else records_for().__dict__
        )
        current = identifier(64)
        if bootstrap:
            current = identifier(68)
            self.record(
                continuation_parameters(self.rows[self.applied]),
                identifier(69),
                current,
                {"next_phase": "complete", "source_artifact_id": self.applied},
            )
        self.scope.update(
            original_operation_id=identifier(12 if browser else 62),
            current_operation_id=current,
        )
        self.workspace_current = current

    async def fetchrow(self, sql, *args):
        if "FROM workspaces " in sql:
            assert self.in_snapshot
            assert tuple(map(str, args)) == (
                self.scope["workspace_id"],
                self.scope["org_id"],
            )
            return {"provisioning_operation_id": self.workspace_current}
        return await super().fetchrow(sql, *args)

    def lineage(self):
        return asyncio.run(collect(self.connect, self.scope))


class LineageProducer(Producer):
    def __init__(self, records):
        super().__init__(records.selected, records.authority["runtime_target"])
        self.records = records
        self.probes = []
        self.result = None

    def __call__(self, command, **options):
        if "python" in command and "app.installation" not in command:
            assert command[-4:-1] == [
                "python",
                "-c",
                Path(demo1_browser.__file__)
                .with_name("_demo1_lineage_probe.py")
                .read_text(),
            ]
            assert self.calls[-1][8:10] == ["sts", "get-caller-identity"]
            assert 0 < options["timeout"] <= 30
            self.calls.append(command)
            self.probes.append(json.loads(command[-1]))
            result = self.result
            if result is None:
                result = asyncio.run(collect(self.records.connect, self.probes[-1]))
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")
        return super().__call__(command, **options)


def reader_checkpoint(records):
    producer = LineageProducer(records)
    reader = RuntimeReader(
        DemoInput.parse(records.selected),
        RuntimeTarget.parse(records.authority["runtime_target"]),
        runner=producer,
    )
    checkpoint = CreationCheckpoint(
        records.scope["request_id"],
        records.scope["workspace_id"],
        records.scope["plan_revision"],
        identifier(70),
        identifier(71),
        True,
    )
    return reader, checkpoint, producer


@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("state", ["pending", "succeeded", "failed"])
def test_admitted_continuation_traces_to_original_without_requiring_new_approval(
    bootstrap, state
):
    records = Records(bootstrap=bootstrap)
    records.operations[records.scope["current_operation_id"]]["state"] = state
    records.scope["authorized_at"] = (records.now - timedelta(hours=2)).isoformat()
    for row in records.rows.values():
        row["created_at"] -= timedelta(minutes=90)
    observed = records.lineage()
    assert observed["original_operation_id"] == identifier(62)
    assert observed["current_request_id"] == identifier(69 if bootstrap else 63)
    assert observed["artifact_ids"] == (
        [records.applied, records.prepared] if bootstrap else [records.prepared]
    )
    assert "Ready" in lineage_report(observed)["scope"]
    assert [item["phase"] for item in observed["operations"]] == list(
        LIFECYCLE_PHASES[: 3 if bootstrap else 2]
    )
    assert observed["operations"][0]["operation_id"] == identifier(62)
    assert observed["operations"][-1] == {
        "phase": "bootstrap-workspace" if bootstrap else "apply-infrastructure",
        "operation_id": identifier(68 if bootstrap else 64),
        "request_id": identifier(69 if bootstrap else 63),
        "state": state,
    }
    assert all(item["state"] == "succeeded" for item in observed["operations"][:-1])
    assert all(sql.startswith("SELECT ") for sql, _ in records.calls)


@pytest.mark.parametrize(
    "failure",
    [
        "fabricated",
        "wrong-request",
        "wrong-root",
        "wrong-workspace",
        "wrong-org",
        "wrong-account",
        "wrong-region",
        "wrong-plan",
        "wrong-digest",
        "wrong-action",
        "wrong-source",
        "wrong-attempt",
        "wrong-job",
        "parent-pending",
        "missing-parent",
        "old-parent",
        "future-parent",
        "reversed-time",
        "changed-current",
        "reused-request",
        "changed-parameters",
        "ambiguous-root",
        "foreign-current",
        "unsupported-phase",
    ],
)
def test_unverified_or_foreign_continuation_never_proves_reentry(failure):
    from harness_jobs.identity import (
        OperationRequest,
        decode_payload,
        encode_payload,
        payload_digest,
    )

    records = Records(bootstrap=True)
    parent = records.rows[records.applied]
    current = records.operations[records.scope["current_operation_id"]]
    if failure == "fabricated":
        parent["producer_fence_token"] += 1
    elif failure in (
        "wrong-request",
        "wrong-root",
        "wrong-workspace",
        "wrong-org",
        "wrong-account",
        "wrong-region",
        "wrong-plan",
    ):
        key = {
            "request": "request_id",
            "root": "original_operation_id",
            "workspace": "workspace_id",
            "org": "org_id",
            "account": "account",
            "region": "region",
            "plan": "plan_revision",
        }[failure[6:]]
        records.scope[key] = identifier(90) if key.endswith("_id") else "foreign"
    elif failure == "wrong-digest":
        current["plan_digest"] = "f" * 64
    elif failure in (
        "wrong-action",
        "wrong-source",
        "changed-parameters",
        "unsupported-phase",
    ):
        request = decode_payload(current["request_payload"])
        parameters = dict(request.parameters)
        if failure == "wrong-source":
            parameters["lifecycle_source_operation_id"] = identifier(90)
        if failure == "changed-parameters":
            parameters["max_cost_micros"] = "2000000"
        if failure == "unsupported-phase":
            parameters["lifecycle_phase"] = "retire-workspace"
        request = OperationRequest(
            "teardown" if failure == "wrong-action" else "provision",
            request.idempotency_key,
            parameters,
        )
        current["request_payload"] = encode_payload(request)
        current["plan_digest"] = payload_digest(request)
    elif failure in ("wrong-attempt", "wrong-job", "parent-pending"):
        key, value = {
            "wrong-attempt": ("attempt_id", identifier(90)),
            "wrong-job": ("job_id", identifier(90)),
            "parent-pending": ("state", "pending"),
        }[failure]
        records.operations[parent["source_operation_id"]][key] = value
    elif failure == "missing-parent":
        del records.rows[records.prepared]
    elif failure == "old-parent":
        parent["created_at"] -= timedelta(days=1)
    elif failure == "future-parent":
        parent["created_at"] += timedelta(days=1)
    elif failure == "reversed-time":
        records.rows[records.prepared]["created_at"] = records.now
    elif failure == "changed-current":
        records.workspace_current = identifier(90)
    elif failure == "reused-request":
        current["idempotency_key"] = records.scope["request_id"]
    elif failure == "ambiguous-root":
        records.operations[identifier(90)] = dict(records.operations[identifier(62)])
    elif failure == "foreign-current":
        current["workspace_id"] = identifier(90)
    with pytest.raises((ValueError, KeyError, LifecycleRefused)):
        records.lineage()


@pytest.mark.parametrize(
    "failure", ["none", "unsubmitted", "request", "revision", "expired"]
)
def test_lineage_requires_original_checkpoint_and_current_read_authority(failure):
    records = Records()
    reader, checkpoint, producer = reader_checkpoint(records)
    now = records.now
    if failure == "none":
        checkpoint = None
    elif failure == "unsubmitted":
        checkpoint = replace(checkpoint, submitted=False)
    elif failure == "request":
        checkpoint = replace(checkpoint, request_id=identifier(90))
    elif failure == "revision":
        checkpoint = replace(checkpoint, plan_revision="f" * 64)
    else:
        now = reader.selected.deadline
    with pytest.raises(EvidenceError, match="checkpoint and current read authority"):
        observe_lineage(reader, checkpoint, identifier(62), identifier(64), 30, now=now)
    assert not producer.calls


@pytest.mark.parametrize(
    "failure", [None, "blocked", "foreign", "partial", "same-request", "changed-pod"]
)
def test_runtime_bound_lineage_reader_and_sanitized_report(failure):
    records = Records()
    reader, checkpoint, producer = reader_checkpoint(records)
    producer.result = records.lineage()
    if failure == "blocked":
        producer.result = {"status": "BLOCKED"}
    elif failure == "foreign":
        producer.result["workspace_id"] = identifier(90)
    elif failure == "partial":
        producer.result["artifact_ids"] = []
    elif failure == "same-request":
        producer.result["current_request_id"] = records.scope["request_id"]
    elif failure == "changed-pod":
        producer.changed_pod = copy.deepcopy(producer.pod)
        producer.changed_pod["metadata"]["uid"] = "replacement"
    if failure:
        with pytest.raises(EvidenceError):
            observe_lineage(
                reader, checkpoint, identifier(62), identifier(64), 30, now=records.now
            )
        return
    observed = observe_lineage(
        reader, checkpoint, identifier(62), identifier(64), 30, now=records.now
    )
    report = json.dumps(lineage_report(observed))
    for private in (
        records.scope["account"],
        identifier(62),
        identifier(64),
        records.prepared,
    ):
        assert private not in report
    assert len(producer.probes) == 1


def test_probe_errors_do_not_expose_private_database_details(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["probe", "{}"])

    def refused(coroutine):
        coroutine.close()
        raise RuntimeError("private-password-and-database-url")

    monkeypatch.setattr(asyncio, "run", refused)
    runpy.run_path(
        str(Path(demo1_browser.__file__).with_name("_demo1_lineage_probe.py")),
        run_name="__main__",
    )
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "BLOCKED"
    assert "private-password" not in output


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "incomplete",
        "reordered",
        "root-request",
        "root-operation",
        "current-request",
        "current-operation",
        "duplicate-request",
        "duplicate-operation",
        "unknown-state",
        "parent-pending",
        "extra-field",
    ],
)
def test_runtime_lineage_rejects_unbound_or_invalid_operation_snapshots(failure):
    records = Records(bootstrap=True)
    reader, checkpoint, producer = reader_checkpoint(records)
    producer.result = records.lineage()
    operations = producer.result["operations"]
    if failure == "missing":
        del producer.result["operations"]
    elif failure == "incomplete":
        operations.pop()
    elif failure == "reordered":
        operations.reverse()
    else:
        index, key, value = {
            "root-request": (0, "request_id", identifier(90)),
            "root-operation": (0, "operation_id", identifier(90)),
            "current-request": (-1, "request_id", identifier(90)),
            "current-operation": (-1, "operation_id", identifier(90)),
            "duplicate-request": (-1, "request_id", operations[0]["request_id"]),
            "duplicate-operation": (-1, "operation_id", operations[0]["operation_id"]),
            "unknown-state": (-1, "state", "Ready"),
            "parent-pending": (1, "state", "pending"),
            "extra-field": (-1, "private-data", "private-value"),
        }[failure]
        operations[index][key] = value
    with pytest.raises(EvidenceError):
        observe_lineage(
            reader, checkpoint, identifier(62), identifier(68), 30, now=records.now
        )


@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize(
    ("failure", "state"),
    [
        (None, "pending"),
        (None, "running"),
        (None, "succeeded"),
        (None, "failed"),
        (None, "cancelled"),
        (None, "unknown"),
        ("current-request", "pending"),
        ("current-operation", "pending"),
        ("current-workspace", "pending"),
        ("workspace-race", "pending"),
        ("details", "pending"),
        ("probe-refusal", "pending"),
    ],
)
def test_cli_reentry_preserves_original_checkpoint_across_continuations(
    driver, monkeypatch, bootstrap, failure, state
):
    driver.run()
    driver.page.service.approved = True
    assert driver.run()["browser"]["creation_observed"] is True
    saved = (driver.path / "checkpoint.json").read_bytes()
    records = Records(bootstrap=bootstrap, browser=True)
    producer = LineageProducer(records)
    if failure == "probe-refusal":
        producer.result = {"status": "BLOCKED"}
    monkeypatch.setattr(
        demo1_journey,
        "RuntimeReader",
        lambda selected, target: RuntimeReader(selected, target, runner=producer),
    )
    request = driver.page.service.request
    current_id = records.scope["current_operation_id"]
    records.operations[current_id]["state"] = state
    current_request = records.operations[current_id]["idempotency_key"]
    workspace_reads = 0
    before = len(driver.page.service.calls)

    def continued(method, path, body=None):
        nonlocal workspace_reads
        if path.endswith("/operations/" + current_id):
            return 200, {
                "request_id": identifier(90)
                if failure == "current-request"
                else current_request,
                "provisioning_operation_id": identifier(90)
                if failure == "current-operation"
                else current_id,
                "workspace_id": identifier(90)
                if failure == "current-workspace"
                else identifier(10),
            }
        status, response = request(method, path, body)
        if path.endswith("/workspaces/" + identifier(10)):
            workspace_reads += 1
            response["provisioning_operation_id"] = (
                identifier(90)
                if failure == "workspace-race" and workspace_reads > 1
                else current_id
            )
        return status, response

    details = []
    previews = []
    preview = demo1_browser.retirement_preview

    def inspect_preview(selected, workspace, source, retirement_request, status, body):
        previews.append(source)
        return preview(selected, workspace, source, retirement_request, status, body)

    def inspect(page, workspace, request_id, operation_id):
        details.append((workspace, request_id, operation_id))
        return {
            "reason": "unavailable"
            if failure == "details"
            else "original identities visible; session and provider authority unverified"
        }

    monkeypatch.setattr(driver.page.service, "request", continued)
    monkeypatch.setattr(demo1_browser, "inspect_original_details", inspect)
    monkeypatch.setattr(demo1_browser, "retirement_preview", inspect_preview)
    result = driver.run()
    assert (driver.path / "checkpoint.json").read_bytes() == saved
    assert len(producer.probes) == 1
    calls = driver.page.service.calls[before:]
    assert all(method == "GET" for method, _, _ in calls)
    if failure:
        assert result is None or not result.get("browser", {}).get("creation_observed")
        assert not any(path.endswith("/retirement/preview") for _, path, _ in calls)
    else:
        assert details == [(identifier(10), current_request, current_id)]
        assert previews == []
        assert result["browser"]["creation_observed"] is True
        assert result["browser"]["lineage"]["current_phase"] == (
            "bootstrap-workspace" if bootstrap else "apply-infrastructure"
        )
        assert result["browser"]["retirement"] == "BLOCKED"
        assert result["status"] == "BLOCKED"
        progress = result["browser"]["lifecycle"]
        assert progress["workspace_ref"] == reference(identifier(10))
        assert progress["original_request_ref"] == reference(driver.selected.request_id)
        current = progress["phases"][LIFECYCLE_PHASES[2 if bootstrap else 1]]
        assert current == {
            "status": "OBSERVED",
            "state": state,
            "operation_ref": reference(current_id),
            "request_ref": reference(current_request),
        }
        assert progress["phases"][LIFECYCLE_PHASES[0]]["state"] == "succeeded"
        assert progress["status"] == (
            "FAIL" if state in ("failed", "cancelled") else "BLOCKED"
        )
        assert all(
            check["status"] == "BLOCKED" for check in progress["checks"].values()
        )
        if not bootstrap:
            assert progress["phases"][LIFECYCLE_PHASES[2]]["state"] == "unobserved"
        for private in (current_id, current_request, identifier(10), records.prepared):
            assert private not in json.dumps(progress)
