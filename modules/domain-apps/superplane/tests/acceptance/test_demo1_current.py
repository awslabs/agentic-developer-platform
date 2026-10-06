"""Offline CLI wiring from immutable ownership to bounded current AWS observations."""

import json
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_demo1_journey as journey_fixtures
import test_demo1_ownership as ownership_fixtures
from test_demo1_aws import arn
from test_demo1_cli import identifier, write_private
from test_demo1_ownership import OwnershipProducer, Records, reader_checkpoint

from superplane_acceptance import demo1_cli, demo1_current
from superplane_acceptance.demo1_discovery import COLLECTIONS
from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_live import LiveEnvelope, PrivateCheckpoint
from superplane_acceptance.demo1_report import reference
from workspace_provisioning.artifacts import continuation_parameters

driver = journey_fixtures.driver
producer_imports = ownership_fixtures.producer_imports


def records_for(workspace=None, original_operation=None):
    records = Records()
    records.selected["survivors"] = [arn("eks", "cluster", "example-peer")]
    if workspace is not None:
        parameters = json.loads(records.rows[records.prepared]["parameters_json"])
        prepared = json.loads(records.rows[records.prepared]["artifact_metadata_json"])
        applied = json.loads(records.rows[records.applied]["artifact_metadata_json"])
        records.rows.clear()
        records.operations.clear()
        records.scope["workspace_id"] = workspace
        records.target["workspace_id"] = workspace
        root = records.record(
            parameters, records.scope["request_id"], original_operation, prepared
        )
        records.prepared = root["artifact_id"]
        applied["source_artifact_id"] = root["artifact_id"]
        applied["outputs"]["workspace_id"]["value"] = workspace
        records.applied = records.record(
            continuation_parameters(root), identifier(63), identifier(64), applied
        )["artifact_id"]
    return records


class CurrentProducer:
    def __init__(self, records):
        self.records = records
        self.runtime = OwnershipProducer(records)
        self.calls = []
        self.absent = set()
        self.denied = set()
        self.incomplete = set()
        self.wrong_role = False
        self.timeout = False
        self.key_state = "Enabled"

    def __call__(self, command, **options):
        if command[5] == self.records.authority["runtime_target"]["broker_label"]:
            return self.runtime(command, **options)
        assert command[:8] == [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            self.records.authority["broker_label"],
            "--exec",
            "aws",
        ]
        assert 0 < options["timeout"] <= 30
        self.calls.append(command)
        if self.timeout:
            raise subprocess.TimeoutExpired(command, options["timeout"])
        service, operation = command[8:10]
        if (service, operation) == ("sts", "get-caller-identity"):
            role = "ForeignRole" if self.wrong_role else self.records.selected["role"]
            result = {
                "Account": self.records.selected["account"],
                "Arn": f"arn:aws:sts::{self.records.selected['account']}:assumed-role/{role}/example",
            }
        elif service == "kms":
            assert operation == "describe-key" and command[10] == "--key-id"
            assert self.calls[-2][8:10] == ["sts", "get-caller-identity"]
            resource = command[11]
            if resource in self.denied:
                return subprocess.CompletedProcess(command, 254, "", "private denial")
            if resource in self.absent:
                return subprocess.CompletedProcess(
                    command,
                    254,
                    "",
                    f"An error occurred (NotFoundException) when calling the DescribeKey operation: Key {resource} does not exist",
                )
            result = {
                "KeyMetadata": {
                    "Arn": resource,
                    "AWSAccountId": self.records.scope["account"],
                    "Enabled": self.key_state == "Enabled",
                    "KeyState": self.key_state,
                }
            }
        elif (
            service == "resourcegroupstaggingapi"
            or "--filters" in command
            or "--filter" in command
        ):
            assert self.calls[-2][8:10] == ["sts", "get-caller-identity"]
            assert "--no-paginate" in command
            collection = (
                "ResourceTagMappingList"
                if service == "resourcegroupstaggingapi"
                else COLLECTIONS[operation.removeprefix("describe-")][0]
            )
            result = {collection: []}
        else:
            assert self.calls[-2][8:10] == ["sts", "get-caller-identity"]
            kind, collection, field, error, api = {
                "describe-cluster": (
                    "cluster",
                    "cluster",
                    "arn",
                    "ResourceNotFoundException",
                    "DescribeCluster",
                ),
                "describe-security-groups": (
                    "security-group",
                    "SecurityGroups",
                    "GroupId",
                    "InvalidGroup.NotFound",
                    "DescribeSecurityGroups",
                ),
                "describe-vpcs": (
                    "vpc",
                    "Vpcs",
                    "VpcId",
                    "InvalidVpcID.NotFound",
                    "DescribeVpcs",
                ),
                "describe-subnets": (
                    "subnet",
                    "Subnets",
                    "SubnetId",
                    "InvalidSubnetID.NotFound",
                    "DescribeSubnets",
                ),
            }[operation]
            name = command[-5]
            resource = arn(service, kind, name)
            assert resource in set(
                self.runtime.observation["owned_resources"]
                + self.runtime.observation["preserved_resources"]
                + self.records.selected["survivors"]
            )
            if resource in self.denied:
                return subprocess.CompletedProcess(
                    command, 255, "", "private provider denial"
                )
            if resource in self.absent:
                return subprocess.CompletedProcess(
                    command,
                    254,
                    "",
                    f"An error occurred ({error}) when calling the {api} operation: The resource '{name}' does not exist.",
                )
            entry = {field: resource if kind == "cluster" else name}
            result = {collection: entry if kind == "cluster" else [entry]}
            if resource in self.incomplete:
                result = {}
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")


def ownership_transport(records, producer=None):
    """Authenticated API fixture with server-produced native ownership projection."""
    source = producer.runtime if isinstance(producer, CurrentProducer) else producer
    source = source or OwnershipProducer(records)
    root = records.rows[records.prepared]["source_operation_id"]
    applied = identifier(64)
    current = identifier(70)
    calls = []

    def request(method, path, body=None):
        assert method == "GET" and body is None
        calls.append((method, path, body))
        if path == "/api/auth/me":
            return 200, {
                "user_id": records.selected["requester_id"],
                "org_id": records.scope["org_id"],
            }
        if path.endswith("/capabilities"):
            return 200, {"features": ["create-operation-id-v1"]}
        if path.endswith("/operations/by-idempotency/" + records.scope["request_id"]):
            projection = {
                **source.observation,
                "version": 1,
                "current_operation_id": current,
                "plan_revision": records.scope["plan_revision"],
                "account_id": records.scope["account"],
                "region": records.scope["region"],
            }
            phases = []
            for index, (phase, operation_id) in enumerate(
                zip(
                    (
                        "prepare-infrastructure",
                        "apply-infrastructure",
                        "bootstrap-workspace",
                    ),
                    (root, applied, current),
                    strict=True,
                )
            ):
                phases.append(
                    {
                        "phase": phase,
                        "operation_id": operation_id,
                        "request_id": records.scope["request_id"]
                        if index == 0
                        else identifier(80 + index),
                        "state": "succeeded",
                        "payload_digest": str(index + 1) * 64,
                        "source_artifact_id": str(index) * 64 if index else None,
                    }
                )
            return 200, {
                "request_id": records.scope["request_id"],
                "workspace_id": records.scope["workspace_id"],
                "provisioning_operation_id": root,
                "state": "succeeded",
                "phase": "execution",
                "lifecycle_lineage": {
                    "version": 1,
                    "org_id": records.scope["org_id"],
                    "workspace_id": records.scope["workspace_id"],
                    "root_request_id": records.scope["request_id"],
                    "root_operation_id": root,
                    "current_operation_id": current,
                    "plan_revision": records.scope["plan_revision"],
                    "phases": phases,
                },
                "applied_ownership": projection,
            }
        if path.endswith("/workspaces/" + records.scope["workspace_id"]):
            return 200, {
                "id": records.scope["workspace_id"],
                "org_id": records.scope["org_id"],
                "provisioning_operation_id": current,
            }
        raise AssertionError("unexpected ownership API request")

    return SimpleNamespace(
        request=request, calls=calls, origin=records.authority["origin"]
    )


def run_observation(records, producer, **options):
    reader, checkpoint, _ = reader_checkpoint(records)
    envelope = LiveEnvelope.parse(records.authority, reader.selected)
    return demo1_current.observe_current_provider(
        reader.selected,
        envelope,
        checkpoint,
        900,
        runner=producer,
        transport=ownership_transport(records, producer),
        **options,
    )


def test_partial_ownership_reaches_exact_provider_reads_without_cleanup_pass():
    records = records_for()
    producer = CurrentProducer(records)
    report = run_observation(records, producer)
    assert producer.calls and producer.runtime.calls
    assert report["provider"]["lookup_status"] == "OBSERVED"
    assert len(report["provider"]["owned_present_refs"]) == 6
    assert report["provider"]["inventory_complete"] is False
    assert report["provider"]["artifact_ref"] == report["ownership"]["artifact_ref"]
    assert report["provider"]["workspace_ref"] == reference(
        records.scope["workspace_id"]
    )
    assert report["provider"]["cost_usd"] is None
    assert report["checks"]["survivors"]["status"] == "OBSERVED"
    assert all(
        report["checks"][check]["status"] == "BLOCKED"
        for check in ("current_inventory", "cleanup", "cost")
    )
    text = json.dumps(report)
    for private in (
        records.scope["account"],
        records.scope["workspace_id"],
        "example-peer",
        "private provider denial",
    ):
        assert private not in text


@pytest.mark.parametrize(
    "failure",
    [
        "missing-artifact",
        "foreign-ownership",
        "wrong-role",
        "denied",
        "incomplete",
        "timeout",
    ],
)
def test_denial_and_incomplete_reads_never_report_absence(failure):
    records = records_for()
    producer = CurrentProducer(records)
    owned = producer.runtime.observation["owned_resources"]
    if failure == "missing-artifact":
        producer.runtime.observation = {"status": "BLOCKED"}
    elif failure == "foreign-ownership":
        producer.runtime.observation["workspace_id"] = identifier(99)
    elif failure == "wrong-role":
        producer.wrong_role = True
    elif failure == "denied":
        producer.denied.add(owned[0])
    elif failure == "incomplete":
        producer.incomplete.add(owned[0])
    else:
        producer.timeout = True
    if failure in {"missing-artifact", "foreign-ownership"}:
        with pytest.raises(EvidenceError):
            run_observation(records, producer)
        assert not producer.calls
        return
    report = run_observation(records, producer)
    assert report["provider"]["status"] == "BLOCKED"
    assert "owned_absent_refs" not in report["provider"]
    assert "survivor_missing_refs" not in report["provider"]
    assert all(check["status"] == "BLOCKED" for check in report["checks"].values())
    assert len(producer.calls) <= 2


def test_absent_recorded_subset_is_not_complete_cleanup_and_missing_peer_fails():
    records = records_for()
    producer = CurrentProducer(records)
    producer.absent.update(
        producer.runtime.observation["owned_resources"] + records.selected["survivors"]
    )
    report = run_observation(records, producer)
    assert len(report["provider"]["owned_absent_refs"]) == 6
    assert report["checks"]["cleanup"]["status"] == "BLOCKED"
    assert report["checks"]["cost"]["status"] == "BLOCKED"
    assert report["checks"]["survivors"]["status"] == "FAIL"


def test_supplied_network_is_observed_as_preserved_not_owned():
    records = records_for()
    metadata = json.loads(records.rows[records.applied]["artifact_metadata_json"])
    metadata["outputs"]["network_ownership"]["value"] = "supplied"
    records.rewrite(
        records.applied,
        "artifact_metadata_json",
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    )
    producer = CurrentProducer(records)
    missing = producer.runtime.observation["preserved_resources"][0]
    producer.absent.add(missing)
    report = run_observation(records, producer)
    assert len(report["provider"]["owned_present_refs"]) == 3
    assert reference(missing) in report["provider"]["survivor_missing_refs"]
    assert report["checks"]["survivors"]["status"] == "FAIL"


def test_one_budget_bounds_runtime_and_each_provider_call():
    records = records_for()
    producer = CurrentProducer(records)
    elapsed = [0]

    def runner(command, **options):
        result = producer(command, **options)
        if producer.calls:
            elapsed[0] = 901
        return result

    reader, checkpoint, _ = reader_checkpoint(records)
    envelope = LiveEnvelope.parse(records.authority, reader.selected)
    report = demo1_current.observe_current_provider(
        reader.selected,
        envelope,
        checkpoint,
        900,
        runner=runner,
        transport=ownership_transport(records, producer),
        monotonic=lambda: elapsed[0],
    )
    assert len(producer.calls) == 1
    assert report["provider"]["status"] == "BLOCKED"
    assert "owned_absent_refs" not in report["provider"]


@pytest.mark.parametrize(
    "failure", ["unsubmitted", "expired-budget", "foreign-survivor", "overlap"]
)
def test_invalid_selection_cannot_reach_provider(failure):
    records = records_for()
    producer = CurrentProducer(records)
    reader, checkpoint, _ = reader_checkpoint(records)
    selected = reader.selected
    budget = 900
    if failure == "unsubmitted":
        checkpoint = replace(checkpoint, submitted=False)
    elif failure == "expired-budget":
        budget = 0
    elif failure == "foreign-survivor":
        selected = replace(
            selected,
            survivors=(
                records.selected["survivors"][0].replace(
                    "123456789012", "000000000002"
                ),
            ),
        )
    else:
        selected = replace(
            selected, survivors=(producer.runtime.observation["owned_resources"][0],)
        )
    envelope = LiveEnvelope.parse(records.authority, selected)
    with pytest.raises(EvidenceError):
        demo1_current.observe_current_provider(
            selected,
            envelope,
            checkpoint,
            budget,
            runner=producer,
            transport=ownership_transport(records, producer),
        )
    assert not producer.calls


def install_observer(monkeypatch, producer):
    observe = demo1_current.observe_current_provider
    monkeypatch.setattr(
        demo1_current,
        "observe_current_provider",
        lambda *args, **kwargs: observe(*args, runner=producer, **kwargs),
    )

    from superplane_acceptance import demo1_session

    monkeypatch.setattr(
        demo1_session,
        "observe_in_browser",
        lambda selected, envelope, session, callback, **kwargs: callback(
            ownership_transport(producer.records, producer)
        ),
    )


def test_documented_provider_cli_keeps_criteria_blocked_and_checkpoint_unchanged(
    tmp_path, monkeypatch
):
    records = records_for()
    producer = CurrentProducer(records)
    reader, checkpoint, _ = reader_checkpoint(records)
    envelope = LiveEnvelope.parse(records.authority, reader.selected)
    install_observer(monkeypatch, producer)
    tmp_path.chmod(0o700)
    for name, document in (
        ("selection.json", records.selected),
        ("authority.json", records.authority),
        ("requester-state.json", records.session),
    ):
        write_private(tmp_path / name, document)
    path = tmp_path / "checkpoint.json"
    with PrivateCheckpoint(
        path, reader.selected, envelope.origin, envelope=envelope
    ) as store:
        store.save(checkpoint)
    original = path.read_bytes()
    readme = Path(__file__).with_name("README.md").read_text()
    command = next(
        block.split("```", 1)[0]
        for block in readme.split("```bash\n")[1:]
        if "--observe-provider" in block.split("```", 1)[0]
    )
    arguments = shlex.split(
        command.replace("\\\n", "").replace("$DEMO1_PRIVATE_DIR", str(tmp_path))
    )
    assert demo1_cli.main(arguments[arguments.index("--mode") :]) == 2
    report = json.loads((tmp_path / "provider-report.json").read_text())
    assert report["provider"]["lookup_status"] == "OBSERVED"
    assert report["criteria"]["AC-02"] == "BLOCKED"
    assert report["status"] == "BLOCKED" and report["live_acceptance"] is False
    assert path.read_bytes() == original


def test_browser_driver_wires_provider_only_after_authenticated_reentry(
    driver, monkeypatch
):
    records = records_for(identifier(10), identifier(12))
    selected_path = driver.path / "selection.json"
    selected = json.loads(selected_path.read_text())
    selected["survivors"] = records.selected["survivors"]
    write_private(selected_path, selected)
    producer = CurrentProducer(records)
    install_observer(monkeypatch, producer)
    original_main = demo1_cli.main
    monkeypatch.setattr(
        demo1_cli, "main", lambda args: original_main([*args, "--observe-provider"])
    )
    pending = driver.run()
    assert pending["provider"]["status"] == "BLOCKED"
    assert not producer.calls and not producer.runtime.calls
    driver.page.service.approved = True
    result = driver.run()
    assert result["browser"]["creation_observed"] is True
    assert result["provider"]["lookup_status"] == "OBSERVED"
    assert result["checks"]["cleanup"]["status"] == "BLOCKED"
    assert result["criteria"]["AC-02"] == "BLOCKED"
    assert result["status"] == "BLOCKED" and result["live_acceptance"] is False
    assert (
        len(
            [
                call
                for call in driver.page.service.calls
                if call[0] == "POST" and call[1].endswith("/workspaces")
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "failure", ["lost-reply", "wrong-requester", "foreign-release"]
)
def test_failed_browser_phase_cannot_reach_provider(driver, monkeypatch, failure):
    producer = CurrentProducer(records_for())
    install_observer(monkeypatch, producer)
    original_main = demo1_cli.main
    monkeypatch.setattr(
        demo1_cli, "main", lambda args: original_main([*args, "--observe-provider"])
    )
    driver.run()
    driver.page.service.approved = True
    if failure == "lost-reply":
        driver.page.service.lost = True
    elif failure == "foreign-release":
        driver.page.release = "f" * 64
    else:
        request = driver.page.service.request

        def wrong_requester(method, path, body=None):
            status, response = request(method, path, body)
            if path == "/api/auth/me":
                response["user_id"] = identifier(99)
            return status, response

        monkeypatch.setattr(driver.page.service, "request", wrong_requester)
    result = driver.run()
    assert result["provider"]["status"] == "BLOCKED"
    assert not producer.calls and not producer.runtime.calls


def test_existing_report_prevents_provider_reads(driver, monkeypatch):
    producer = CurrentProducer(records_for())
    install_observer(monkeypatch, producer)
    report = driver.path / "existing.json"
    write_private(report, {"preserve": True})
    arguments = ["--mode", "live", "--observe-provider"]
    for option, filename in (
        ("--private-input", "selection.json"),
        ("--authority", "authority.json"),
        ("--browser-state", "requester-state.json"),
        ("--checkpoint", "checkpoint.json"),
        ("--report", "existing.json"),
    ):
        arguments.extend((option, str(driver.path / filename)))
    assert demo1_cli.main(arguments) == 2
    assert json.loads(report.read_text()) == {"preserve": True}
    assert not producer.calls and not producer.runtime.calls


@pytest.mark.parametrize("state", ["present", "absent", "disabled", "denied"])
def test_current_observer_can_read_declared_retained_kms_survivor(state):
    records = records_for()
    key = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"
    records.selected["survivors"].append(key)
    producer = CurrentProducer(records)
    if state == "absent":
        producer.absent.add(key)
    elif state == "denied":
        producer.denied.add(key)
    elif state == "disabled":
        producer.key_state = "Disabled"
    report = run_observation(records, producer)
    if state == "present":
        assert reference(key) in report["provider"]["survivor_present_refs"]
        assert report["checks"]["survivors"]["status"] == "OBSERVED"
    elif state == "absent":
        assert reference(key) in report["provider"]["survivor_missing_refs"]
        assert report["checks"]["survivors"]["status"] == "FAIL"
    else:
        assert report["checks"]["survivors"]["status"] == "BLOCKED"
    assert key not in json.dumps(report)
    assert report["checks"]["cleanup"]["status"] == "BLOCKED"
