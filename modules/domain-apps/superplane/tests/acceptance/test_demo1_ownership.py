"""Offline immutable producer records, not observed live resources or authority."""

import asyncio
import copy
import json
import runpy
import shlex
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_demo1_cli import identifier, write_private
from test_demo1_runtime import Producer, documents

from superplane_acceptance import demo1_cli, demo1_runtime
from superplane_acceptance._demo1_ownership_probe import collect
from superplane_acceptance.demo1_browser import CreationCheckpoint
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError
from superplane_acceptance.demo1_live import LiveEnvelope, PrivateCheckpoint
from superplane_acceptance.demo1_ownership import observe_ownership, ownership_report
from superplane_acceptance.demo1_runtime import RuntimeReader, RuntimeTarget
from workspace_provisioning.artifacts import (
    canonical,
    continuation_parameters,
    digest,
    initial_execution_steps,
)
from workspace_provisioning.runtime_config import LifecycleRefused


@pytest.fixture(autouse=True)
def producer_imports(monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[4] / "harness" / "jobs")
    )


class Records:
    def __init__(self):
        self.selected, self.authority, self.session = documents()
        self.now = datetime.now(UTC)
        self.scope = {
            key: self.selected[key]
            for key in (
                "org_id",
                "request_id",
                "plan_revision",
                "account",
                "region",
                "authorized_at",
            )
        } | {"workspace_id": identifier(61), "observed_at": self.now.isoformat()}
        self.target = {key: self.scope[key] for key in ("org_id", "workspace_id")}
        self.target.update(
            account_id=self.scope["account"], aws_region=self.scope["region"]
        )
        self.rows, self.operations, self.calls = {}, {}, []
        self.in_snapshot = False
        parameters = {
            "plan_revision": self.scope["plan_revision"],
            "lifecycle_request": canonical({"mode": "existing-account-managed"}),
            "lifecycle_inputs": canonical({"cluster_placement": "dedicated"}),
            "lifecycle_allocation_max_resource_units": "10",
            "lifecycle_allocation_max_runtime_seconds": "900",
            "lifecycle_allocation_max_cost_micros": "1000000",
        }
        parameters["execution_steps"] = initial_execution_steps(parameters)
        prepared = self.record(
            parameters,
            self.scope["request_id"],
            identifier(62),
            {
                "next_phase": "apply-infrastructure",
                "module_sha256": "a" * 64,
                "inventory": {"planned": "must-not-be-used"},
            },
        )
        self.prepared = prepared["artifact_id"]
        cluster = f"arn:aws:eks:{self.scope['region']}:{self.scope['account']}:cluster/example-workspace"
        self.outputs = {
            **self.target,
            "cluster_name": "example-workspace",
            "cluster_arn": cluster,
            "vpc_id": "vpc-12345678",
            "private_subnet_ids": ["subnet-12345678"],
            "public_subnet_ids": ["subnet-23456789"],
            "network_ownership": "adp-created",
            "workspace_api_security_group_id": "sg-12345678",
            "workspace_node_security_group_id": "sg-23456789",
            "workspace_node_group": {
                "arn": cluster.replace("cluster/", "nodegroup/") + "/default/example",
                "launch_template_id": "lt-12345678",
                "launch_template_version": "1",
            },
            "node_role_arn": f"arn:aws:iam::{self.scope['account']}:role/ExampleNodes",
            "tenant_scheduling_prerequisites": {
                "cni_role_arn": f"arn:aws:iam::{self.scope['account']}:role/ExampleCNI",
                "cni_addon_version": "v1.19.0-eksbuild.1",
            },
            "sts_endpoint_id": "vpce-12345678",
        }
        snapshot = {
            "cluster_arn": cluster,
            "nodegroup_arn": self.outputs["workspace_node_group"]["arn"],
            "node_role_arn": self.outputs["node_role_arn"],
            "launch_template_id": "lt-12345678",
            "launch_template_version": "1",
            **self.outputs["tenant_scheduling_prerequisites"],
            "sts_endpoint_id": "vpce-12345678",
            "retained_sts_rule_id": "sgr-12345678",
        }
        applied = self.record(
            continuation_parameters(prepared),
            identifier(63),
            identifier(64),
            {
                "next_phase": "bootstrap-workspace",
                "source_artifact_id": self.prepared,
                "allocation_source_operation_id": identifier(64),
                "module_sha256": "a" * 64,
                "outputs": {
                    key: {"value": value} for key, value in self.outputs.items()
                },
                "provider_snapshot": snapshot,
            },
        )
        self.applied = applied["artifact_id"]

    def record(self, parameters, request_id, operation_id, metadata):
        from harness_jobs.identity import (
            OperationRequest,
            encode_payload,
            payload_digest,
        )

        request = OperationRequest("provision", request_id, parameters)
        row = {
            "org_id": self.scope["org_id"],
            "workspace_id": self.scope["workspace_id"],
            "source_operation_id": operation_id,
            "source_job_id": identifier(65),
            "source_attempt_id": identifier(66),
            "producer_holder": "example-worker",
            "producer_attempt_id": identifier(67),
            "producer_fence_token": 3,
            "source_payload_digest": payload_digest(request),
            "source_request_payload": encode_payload(request),
            "request_revision": parameters["plan_revision"],
            "account_id": self.scope["account"],
            "target_json": canonical(self.target),
            "parameters_json": canonical(parameters),
            "artifact_metadata_json": canonical(metadata),
        }
        row["artifact_id"] = digest(row)
        row["created_at"] = self.now - timedelta(seconds=30 - len(self.rows))
        self.rows[row["artifact_id"]] = row
        self.operations[operation_id] = {
            "operation_id": operation_id,
            "org_id": row["org_id"],
            "workspace_id": row["workspace_id"],
            "job_id": row["source_job_id"],
            "attempt_id": row["source_attempt_id"],
            "plan_digest": payload_digest(request),
            "request_payload": encode_payload(request),
            "idempotency_key": request_id,
            "state": "succeeded",
        }
        return row

    def rewrite(self, identity, column, value):
        row = self.rows.pop(identity)
        row[column] = value
        row["artifact_id"] = digest(
            {
                key: item
                for key, item in row.items()
                if key not in {"artifact_id", "created_at"}
            }
        )
        self.rows[row["artifact_id"]] = row

    @asynccontextmanager
    async def connect(self):
        yield self

    @asynccontextmanager
    async def transaction(self, **options):
        assert options == {"isolation": "repeatable_read", "readonly": True}
        self.in_snapshot = True
        yield
        self.in_snapshot = False

    async def fetch(self, sql, *args):
        assert self.in_snapshot and sql.startswith("SELECT ") and "LIMIT 2" in sql
        self.calls.append((sql, args))
        if "harness_operations" in sql:
            return [
                row
                for row in self.operations.values()
                if tuple(
                    row[key] for key in ("org_id", "workspace_id", "idempotency_key")
                )
                == args
            ]
        assert args == (self.scope["org_id"], self.scope["workspace_id"])
        return [
            {"artifact_id": row["artifact_id"]}
            for row in self.rows.values()
            if json.loads(row["artifact_metadata_json"])["next_phase"]
            == "bootstrap-workspace"
        ]

    async def fetchrow(self, sql, *args):
        assert (
            self.in_snapshot
            and sql.startswith("SELECT ")
            and "org_id=$2 AND workspace_id=$3" in sql
        )
        self.calls.append((sql, args))
        row = (self.operations if "harness_operations" in sql else self.rows).get(
            args[0]
        )
        return row if row and (row["org_id"], row["workspace_id"]) == args[1:] else None

    def collect(self):
        return asyncio.run(collect(self.connect, self.scope))


def test_real_artifact_reader_traces_apply_to_original_preparation():
    records = Records()
    result = records.collect()
    assert result["status"] == "OBSERVED" and result["inventory_complete"] is False
    assert result["original_operation_id"] == identifier(62)
    assert result["apply_operation_id"] == identifier(64)
    assert len(result["owned_resources"]) == 6 and result["preserved_resources"] == []
    assert "must-not-be-used" not in json.dumps(result)


@pytest.mark.parametrize(
    "failure",
    [
        "fabricated",
        "planned-only",
        "no-snapshot",
        "changed-snapshot",
        "foreign-account",
        "foreign-root",
        "changed-parent",
        "wrong-module",
        "foreign-source",
        "not-succeeded",
        "changed-digest",
        "changed-attempt",
        "stale",
        "future",
        "missing-parent",
        "ambiguous",
    ],
)
def test_unverified_ownership_is_refused(failure):
    records = Records()
    row = records.rows[records.applied]
    metadata = json.loads(row["artifact_metadata_json"])
    operation = records.operations[identifier(64)]
    if failure == "fabricated":
        row["producer_fence_token"] += 1
    elif failure == "planned-only":
        metadata.pop("outputs")
    elif failure == "no-snapshot":
        metadata.pop("provider_snapshot")
    elif failure == "changed-snapshot":
        metadata["provider_snapshot"]["cluster_arn"] += "-foreign"
    elif failure == "foreign-account":
        metadata["outputs"]["account_id"]["value"] = "000000000002"
    elif failure == "foreign-root":
        records.scope["request_id"] = identifier(99)
    elif failure == "changed-parent":
        metadata["source_artifact_id"] = "f" * 64
    elif failure == "wrong-module":
        metadata["module_sha256"] = "f" * 64
    elif failure == "foreign-source":
        operation["workspace_id"] = identifier(99)
    elif failure == "not-succeeded":
        operation["state"] = "failed"
    elif failure == "changed-digest":
        operation["plan_digest"] = "f" * 64
    elif failure == "changed-attempt":
        operation["attempt_id"] = identifier(99)
    elif failure == "stale":
        row["created_at"] = datetime.fromisoformat(
            records.scope["authorized_at"]
        ) - timedelta(seconds=1)
    elif failure == "future":
        row["created_at"] = records.now + timedelta(seconds=1)
    elif failure == "missing-parent":
        del records.rows[records.prepared]
    else:
        duplicate = copy.deepcopy(row)
        duplicate["artifact_id"] = "f" * 64
        records.rows[duplicate["artifact_id"]] = duplicate
    if failure in {
        "planned-only",
        "no-snapshot",
        "changed-snapshot",
        "foreign-account",
        "changed-parent",
        "wrong-module",
    }:
        records.rewrite(records.applied, "artifact_metadata_json", canonical(metadata))
    with pytest.raises((ValueError, KeyError, RuntimeError, LifecycleRefused)):
        records.collect()


def test_supplied_network_is_preserved_and_never_in_owned_set():
    records = Records()
    metadata = json.loads(records.rows[records.applied]["artifact_metadata_json"])
    metadata["outputs"]["network_ownership"]["value"] = "supplied"
    records.rewrite(records.applied, "artifact_metadata_json", canonical(metadata))
    result = records.collect()
    assert len(result["preserved_resources"]) == 3
    assert len(result["owned_resources"]) == 3
    assert not set(result["owned_resources"]) & set(result["preserved_resources"])


def test_historical_record_is_readable_after_admission_freshness_expires():
    records = Records()
    records.scope["authorized_at"] = (records.now - timedelta(hours=2)).isoformat()
    for row in records.rows.values():
        row["created_at"] -= timedelta(minutes=90)
    result = records.collect()
    assert result["status"] == "OBSERVED" and result["inventory_complete"] is False
    assert datetime.fromisoformat(result["recorded_at"]) < records.now - timedelta(
        hours=1
    )


class OwnershipProducer(Producer):
    def __init__(self, records):
        super().__init__(records.selected, records.authority["runtime_target"])
        self.records = records
        self.observation = records.collect()

    def __call__(self, command, **options):
        if (
            "-c" in command
            and "python" in command
            and "app.installation" not in command
        ):
            assert command[13:20] == [
                "exec",
                "pod/superplane-api-example",
                "-c",
                "superplane-api",
                "--",
                "python",
                "-c",
            ]
            assert "from workspace_provisioning.artifacts import" in command[-2]
            assert "readonly=True" in command[-2]
            scope = json.loads(command[-1])
            assert scope["request_id"] == self.records.scope["request_id"]
            assert self.calls[-1][8:10] == ["sts", "get-caller-identity"]
            self.calls.append(command)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(self.observation), stderr=""
            )
        return super().__call__(command, **options)


def reader_checkpoint(records):
    producer = OwnershipProducer(records)
    reader = RuntimeReader(
        DemoInput.parse(records.selected),
        RuntimeTarget.parse(records.authority["runtime_target"]),
        runner=producer,
    )
    checkpoint = CreationCheckpoint(
        records.scope["request_id"],
        records.scope["workspace_id"],
        records.scope["plan_revision"],
        identifier(68),
        identifier(69),
        True,
    )
    return reader, checkpoint, producer


def test_selected_runtime_protects_private_read_and_report_is_sanitized():
    records = Records()
    reader, checkpoint, producer = reader_checkpoint(records)
    runtime, observation = observe_ownership(reader, checkpoint, 900)
    assert runtime["status"] == "OBSERVED" and "ownership" not in runtime
    report = json.dumps(ownership_report(observation))
    for private in (
        records.scope["account"],
        checkpoint.workspace_id,
        identifier(64),
        records.applied,
        "example-workspace",
    ):
        assert private not in report
    assert any(
        "python" in call and "app.installation" not in call for call in producer.calls
    )


@pytest.mark.parametrize(
    "failure",
    [
        "unsubmitted",
        "request",
        "plan",
        "runtime",
        "pod-replaced",
        "denied",
        "foreign",
        "malformed",
    ],
)
def test_runtime_and_checkpoint_refusals(failure):
    records = Records()
    reader, checkpoint, producer = reader_checkpoint(records)
    if failure == "unsubmitted":
        checkpoint = replace(checkpoint, submitted=False)
    elif failure == "request":
        checkpoint = replace(checkpoint, request_id=identifier(99))
    elif failure == "plan":
        checkpoint = replace(checkpoint, plan_revision="f" * 64)
    elif failure == "runtime":
        producer.runtime["source_revision"] = "f" * 40
    elif failure == "pod-replaced":
        producer.changed_pod = copy.deepcopy(producer.pod)
        producer.changed_pod["metadata"]["uid"] = "replacement"
    elif failure == "denied":
        producer.observation = {"status": "BLOCKED", "reason": "private failure"}
    elif failure == "foreign":
        producer.observation["workspace_id"] = identifier(99)
    else:
        producer.observation = []
    with pytest.raises(EvidenceError) as failure_result:
        observe_ownership(reader, checkpoint, 900)
    assert "private failure" not in str(failure_result.value)
    if failure in {"unsubmitted", "request", "plan", "runtime"}:
        assert not any(
            "python" in call and "app.installation" not in call
            for call in producer.calls
        )


def test_cli_executes_private_read_without_browser_or_mutation(tmp_path, monkeypatch):
    records = Records()
    reader, checkpoint, producer = reader_checkpoint(records)
    monkeypatch.setattr(demo1_runtime, "RuntimeReader", lambda *unused: reader)
    tmp_path.chmod(0o700)
    for name, document in (
        ("selection.json", records.selected),
        ("authority.json", records.authority),
        ("session.json", records.session),
    ):
        write_private(tmp_path / name, document)
    envelope = LiveEnvelope.parse(records.authority, reader.selected)
    path = tmp_path / "checkpoint.json"
    with PrivateCheckpoint(
        path, reader.selected, envelope.origin, envelope=envelope
    ) as store:
        store.save(checkpoint)
    before = path.read_bytes()
    report = tmp_path / "report.json"
    assert (
        demo1_cli.main(
            [
                "--mode",
                "live",
                "--observe-ownership",
                "--private-input",
                str(tmp_path / "selection.json"),
                "--authority",
                str(tmp_path / "authority.json"),
                "--browser-state",
                str(tmp_path / "session.json"),
                "--checkpoint",
                str(path),
                "--report",
                str(report),
            ]
        )
        == 2
    )
    result = json.loads(report.read_text())
    assert result["ownership"]["status"] == "OBSERVED"
    assert result["ownership"]["inventory_complete"] is False
    assert path.read_bytes() == before
    assert "browser" not in result
    assert producer.calls
    readme = Path(__file__).with_name("README.md").read_text()
    command = next(
        block
        for block in readme.split("```bash\n")[1:]
        if "--observe-ownership" in block.split("```", 1)[0]
    )
    arguments = shlex.split(
        command.split("```", 1)[0]
        .replace("\\\n", "")
        .replace("$DEMO1_PRIVATE_DIR", str(tmp_path))
    )
    arguments[arguments.index("--browser-state") + 1] = str(tmp_path / "session.json")
    assert demo1_cli.main(arguments[arguments.index("--mode") :]) == 2
    documented = json.loads((tmp_path / "ownership-report.json").read_text())
    assert documented["ownership"]["status"] == "OBSERVED"
    assert path.read_bytes() == before


@pytest.mark.parametrize("denied", [False, True])
def test_remote_probe_uses_maintained_pool_and_sanitizes_errors(
    monkeypatch, capsys, denied
):
    records = Records()
    events = []

    async def open_pool():
        events.append("open")
        if denied:
            raise RuntimeError("private-database-password")

    async def ensure_ready():
        events.append("check-schema")

    async def close_pool():
        events.append("close")

    pool = SimpleNamespace(
        open=open_pool,
        ensure_ready=ensure_ready,
        aclose=close_pool,
        connect=records.connect,
    )
    monkeypatch.setitem(
        sys.modules,
        "app.adapters.harness_connection",
        SimpleNamespace(build_harness_connections=lambda settings: pool),
    )
    monkeypatch.setitem(sys.modules, "app.config", SimpleNamespace(settings=object()))
    monkeypatch.setattr(sys, "argv", ["probe", json.dumps(records.scope)])
    path = Path(demo1_runtime.__file__).with_name("_demo1_ownership_probe.py")
    runpy.run_path(str(path), run_name="__main__")
    captured = capsys.readouterr()
    assert "private-database-password" not in captured.out + captured.err
    assert events == (
        ["open", "close"] if denied else ["open", "check-schema", "close"]
    )
    assert json.loads(captured.out)["status"] == ("BLOCKED" if denied else "OBSERVED")
