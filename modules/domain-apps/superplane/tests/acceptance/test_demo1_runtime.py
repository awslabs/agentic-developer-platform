"""Credential-free running-artifact probes using the producer's response shapes."""

import base64
import copy
import json
import re
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_demo1_cli import identifier, write_private
from test_demo1_live import inputs

from superplane_acceptance import demo1_cli, demo1_runtime
from superplane_acceptance.demo1_browser import CreationCheckpoint
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError
from superplane_acceptance.demo1_live import LiveEnvelope, PrivateCheckpoint
from superplane_acceptance.demo1_runtime import RuntimeReader, RuntimeTarget


def documents():
    selected, authority, session = inputs()
    target = {
        "connection_id": identifier(40),
        "broker_label": "example-runtime-reader",
        "account": "123456789012",
        "role": "ExampleRuntimeReader",
        "region": "us-east-1",
        "cluster_name": "example-management",
        "namespace": "example-domain",
        "release_id": "e" * 64,
    }
    authority.update(version="demo1-live-v2", runtime_target=target)
    return selected, authority, session


class Producer:
    def __init__(self, selected, target):
        self.calls = []
        self.target = target
        self.cluster = {
            "cluster": {
                "arn": f"arn:aws:eks:us-east-1:{target['account']}:cluster/{target['cluster_name']}",
                "status": "ACTIVE",
                "endpoint": "https://example.invalid",
                "certificateAuthority": {
                    "data": base64.b64encode(b"synthetic CA").decode()
                },
            }
        }
        self.deployment = {
            "metadata": {
                "name": "superplane-api",
                "namespace": target["namespace"],
                "uid": "deployment-uid",
                "resourceVersion": "1",
                "generation": 1,
            },
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
                "readyReplicas": 1,
            },
        }
        self.pod = {
            "metadata": {
                "name": "superplane-api-example",
                "namespace": target["namespace"],
                "uid": "pod-uid",
                "resourceVersion": "2",
                "annotations": {"adp.aws-e.io/release": target["release_id"]},
                "ownerReferences": [
                    {
                        "kind": "ReplicaSet",
                        "name": "superplane-api-example",
                        "uid": "replicaset-uid",
                        "controller": True,
                    }
                ],
            },
            "spec": {
                "containers": [
                    {
                        "name": "superplane-api",
                        "image": "example.invalid/api@sha256:"
                        + selected["image_digest"],
                    }
                ]
            },
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "name": "superplane-api",
                        "ready": True,
                        "imageID": "example.invalid/api@sha256:"
                        + selected["image_digest"],
                    }
                ],
            },
        }
        self.replica_set = {
            "metadata": {
                "namespace": target["namespace"],
                "uid": "replicaset-uid",
                "ownerReferences": [
                    {"kind": "Deployment", "uid": "deployment-uid", "controller": True}
                ],
            }
        }
        self.runtime = {
            "release_id": target["release_id"],
            "source_revision": selected["release_source"],
            "domain_auth_enforced": True,
            "paid_admission_enabled": False,
        }
        self.database = {"revision": selected["schema_revision"]}
        self.changed_pod = None
        self.changed_deployment = None
        self.denied = None
        self.bad_role = False
        self.config_paths = []

    def __call__(self, command, **options):
        self.calls.append(command)
        assert command[:8] == [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            self.target["broker_label"],
            "--exec",
            command[7],
        ]
        assert 0 < options["timeout"] <= 30
        if command[7] == "aws":
            if command[8:10] == ["sts", "get-caller-identity"]:
                role = "ForeignRole" if self.bad_role else self.target["role"]
                result = {
                    "Account": self.target["account"],
                    "Arn": f"arn:aws:sts::{self.target['account']}:assumed-role/{role}/session",
                }
            else:
                assert command[8:10] == ["eks", "describe-cluster"]
                result = self.cluster
        else:
            assert command[7] == "kubectl"
            config = Path(command[9])
            self.config_paths.append(config)
            assert config.stat().st_mode & 0o777 == 0o600
            document = json.loads(config.read_text())
            assert document["current-context"] == "selected"
            assert document["clusters"][0]["cluster"] == {
                "server": self.cluster["cluster"]["endpoint"],
                "certificate-authority-data": self.cluster["cluster"][
                    "certificateAuthority"
                ]["data"],
            }
            assert document["users"][0]["user"]["exec"]["args"] == [
                "eks",
                "get-token",
                "--cluster-name",
                self.target["cluster_name"],
                "--region",
                self.target["region"],
                "--output",
                "json",
            ]
            assert command[12] == self.target["namespace"]
            arguments = command[13:]
            if self.denied:
                return SimpleNamespace(returncode=1, stdout="", stderr=self.denied)
            match arguments:
                case ["get", "deployment/superplane-api", "-o", "json"]:
                    result = self.deployment
                    if (
                        self.changed_deployment
                        and sum(
                            "deployment/superplane-api" in call for call in self.calls
                        )
                        > 1
                    ):
                        result = self.changed_deployment
                case [
                    "get",
                    "pods",
                    "-l",
                    "app.kubernetes.io/name=superplane-api",
                    "-o",
                    "json",
                ]:
                    result = {"items": [self.pod]}
                case ["get", "replicaset/superplane-api-example", "-o", "json"]:
                    result = self.replica_set
                case ["get", "pod/superplane-api-example", "-o", "json"]:
                    result = self.changed_pod or self.pod
                case [
                    "exec",
                    "pod/superplane-api-example",
                    "-c",
                    "superplane-api",
                    "--",
                    "python",
                    "-m",
                    "app.installation",
                    action,
                ] if action in ("readiness", "database"):
                    result = self.runtime if action == "readiness" else self.database
                case _:
                    pytest.fail("Unexpected command (including any mutation)")
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")


def setup_reader():
    selected, authority, _ = documents()
    producer = Producer(selected, authority["runtime_target"])
    reader = RuntimeReader(
        DemoInput.parse(selected),
        RuntimeTarget.parse(authority["runtime_target"]),
        runner=producer,
    )
    return reader, producer


def test_matches_running_artifact_source_and_schema_without_claiming_admission():
    reader, producer = setup_reader()
    result = reader.observe(60)
    assert result["status"] == "OBSERVED"
    assert "lifecycle admission unverified" in result["scope"]
    assert len(result["pod_refs"]) == 1
    for index, command in enumerate(producer.calls):
        if command[7] == "kubectl" or command[8:10] == ["eks", "describe-cluster"]:
            assert producer.calls[index - 1][8:10] == ["sts", "get-caller-identity"]
    assert producer.config_paths and not any(
        path.exists() for path in producer.config_paths
    )
    assert producer.target["account"] not in json.dumps(result)


@pytest.mark.parametrize(
    "document,path,value",
    [
        ("cluster", ("cluster", "arn"), "foreign-cluster"),
        ("cluster", ("cluster", "status"), "UPDATING"),
        ("cluster", ("cluster", "endpoint"), "http://example.invalid"),
        ("cluster", ("cluster", "certificateAuthority", "data"), "bad-CA"),
        ("deployment", ("status", "updatedReplicas"), 0),
        ("deployment", ("status", "observedGeneration"), 0),
        ("deployment", ("metadata", "namespace"), "foreign-namespace"),
        ("pod", ("metadata", "annotations", "adp.aws-e.io/release"), "foreign-release"),
        ("pod", ("metadata", "deletionTimestamp"), "deleting"),
        ("pod", ("status", "conditions", 0, "status"), "False"),
        ("pod", ("status", "containerStatuses", 0, "imageID"), "sha256:" + "f" * 64),
        ("pod", ("spec", "containers", 0, "image"), "example.invalid/api:latest"),
        (
            "replica_set",
            ("metadata", "ownerReferences", 0, "uid"),
            "foreign-deployment",
        ),
        ("runtime", ("release_id",), "foreign-release"),
        ("runtime", ("source_revision",), "f" * 40),
        ("runtime", ("domain_auth_enforced",), False),
        ("database", ("revision",), "stale-schema"),
    ],
)
def test_rejects_foreign_stale_or_incomplete_runtime(document, path, value):
    reader, producer = setup_reader()
    target = getattr(producer, document)
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(EvidenceError, match="mismatched observation"):
        reader.observe(60)


@pytest.mark.parametrize("resource", ["pod", "deployment"])
def test_refuses_runtime_replacement_during_probe(resource):
    reader, producer = setup_reader()
    changed = copy.deepcopy(getattr(producer, resource))
    changed["metadata"]["uid"] = "replacement"
    setattr(producer, "changed_" + resource, changed)
    with pytest.raises(EvidenceError):
        reader.observe(60)


def test_wrong_role_refuses_before_any_resource_read():
    reader, producer = setup_reader()
    producer.bad_role = True
    with pytest.raises(EvidenceError):
        reader.observe(60)
    assert len(producer.calls) == 1


def test_timeout_and_tool_errors_are_sanitized():
    reader, producer = setup_reader()
    with pytest.raises(EvidenceError):
        reader.observe(0)
    assert not producer.calls
    reader.runner = lambda *args, **kwargs: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("private-value", 30)
    )
    with pytest.raises(EvidenceError) as caught:
        reader.observe(60)
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("denied", [None, "private-provider-error-must-not-leak"])
def test_documented_runtime_cli_wires_probes_and_remains_blocked(
    tmp_path, monkeypatch, capsys, denied
):
    selected, authority, session = documents()
    producer = Producer(selected, authority["runtime_target"])
    producer.denied = denied
    original_reader = RuntimeReader
    monkeypatch.setattr(
        demo1_runtime,
        "RuntimeReader",
        lambda selected, target: original_reader(selected, target, runner=producer),
    )
    tmp_path.chmod(0o700)
    for name, value in (
        ("selection.json", selected),
        ("authority.json", authority),
        ("requester-state.json", session),
    ):
        write_private(tmp_path / name, value)
    checkpoint = tmp_path / "checkpoint.json"
    parsed = DemoInput.parse(selected)
    with PrivateCheckpoint(str(checkpoint), parsed, authority["origin"]) as store:
        store.save(
            CreationCheckpoint(
                parsed.request_id,
                identifier(6),
                parsed.plan_revision,
                identifier(8),
                identifier(9),
                submitted=True,
            )
        )
    original_checkpoint = checkpoint.read_bytes()
    documentation = (Path(__file__).parent / "README.md").read_text()
    match = re.search(
        r"### Independent API runtime observation.*?```sh\n(.*?)\n```",
        documentation,
        re.DOTALL,
    )
    assert match is not None
    command = shlex.split(
        match.group(1).replace("\\\n", "").replace("$DEMO1_PRIVATE_DIR", str(tmp_path))
    )
    assert demo1_cli.main(command[4:]) == 2
    report = json.loads((tmp_path / "runtime-report.json").read_text())
    assert report["runtime"]["status"] == ("BLOCKED" if denied else "OBSERVED")
    assert report["status"] == "BLOCKED" and report["live_acceptance"] is False
    assert report["criteria"]["AC-02"] == "BLOCKED"
    assert checkpoint.read_bytes() == original_checkpoint
    assert report["checkpoint"]["submitted"] is True
    printed = capsys.readouterr()
    assert not printed.err
    assert "private-provider-error" not in json.dumps(report) + printed.out


@pytest.mark.parametrize("value", [None, {}, {"cluster_name": "--foreign"}])
def test_incomplete_v2_authority_refuses_before_reads(value):
    selected, authority, _ = documents()
    authority["runtime_target"] = value
    with pytest.raises(EvidenceError):
        LiveEnvelope.parse(authority, DemoInput.parse(selected))


@pytest.mark.parametrize(
    "version,observe", [("demo1-live-v1", True), ("demo1-live-v2", False)]
)
def test_reads_require_both_explicit_flag_and_v2_authority(
    tmp_path, monkeypatch, version, observe
):
    selected, authority, session = documents()
    authority["version"] = version
    if version == "demo1-live-v1":
        del authority["runtime_target"]
    monkeypatch.setattr(
        demo1_runtime,
        "RuntimeReader",
        lambda *args, **kwargs: pytest.fail("Unauthorized remote read"),
    )
    tmp_path.chmod(0o700)
    arguments = ["--mode", "live"]
    for flag, name, value in (
        ("--private-input", "selection.json", selected),
        ("--authority", "authority.json", authority),
        ("--browser-state", "session.json", session),
    ):
        path = tmp_path / name
        write_private(path, value)
        arguments.extend((flag, str(path)))
    report = tmp_path / "report.json"
    arguments.extend(
        ("--checkpoint", str(tmp_path / "checkpoint.json"), "--report", str(report))
    )
    if observe:
        arguments.append("--observe-runtime")
    assert demo1_cli.main(arguments) == 2
    assert report.exists() is not observe
    if report.exists():
        assert "runtime" not in json.loads(report.read_text())
