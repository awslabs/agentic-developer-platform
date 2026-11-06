"""Identity evidence is measured and fully bound before a stage can pass."""

import base64
import copy
import hashlib
import importlib
import io
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

SOURCE = "a" * 40
GATEWAY = SOURCE
GATEWAY_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "c" * 64
ADAPTER_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-dev-adapters@sha256:" + "d" * 64
BUILD_ID = "adp-tools-dev-adapters-foundation-check:00000000-0000-0000-0000-000000000001"


@pytest.fixture
def contract(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    return importlib.import_module("identity_contract")


def measured():
    generation = "b" * 64
    return {
        "consent": [
            {
                "owner": "user-alice",
                "tenant": "org-acme",
                "attempt": "first",
                "site": "site-2",
                "provider_calls": 1,
                "token_persisted": False,
                "foreign_site_status": 403,
                "stale_site_status": 409,
            }
        ],
        "denial": [
            {
                "fault": fault,
                "status": {"changed_site": 409, "callback_site": 422}.get(fault, 403),
                "provider_calls_before": [1, 1, 0],
                "provider_calls_after": [1, 1, 0],
                "observation_calls": 0,
                "attempt": "first",
            }
            for fault in ("foreign_owner", "foreign_tenant", "changed_site", "callback_site", "wrong_session")
        ],
        "admission": [
            {
                "owner": "alice",
                "foreign_status": 403,
                "callback_status": 201,
                "replay_status": 403,
                "old_dispatch_status": 403,
                "fenced_dispatch_status": 403,
                "fresh_admission_status": 200,
                "fenced_admission_status": 403,
                "old_attempt": "first",
                "generation": 1,
                "provider_calls": 1,
            }
        ],
        "retry": [
            {
                "retried": retry,
                "old_attempt": "first",
                "new_attempt": "second",
                "owner": "user-alice",
                "stale_status": 409,
                "current_status": 201,
                "identity_completions": 2,
                "token_persisted": False,
            }
            for retry in (False, True)
        ],
        "disconnect": [
            {
                "owner": "user-alice",
                "tenant": "org-acme",
                "generations": ["a" * 64, "revision-current"],
                "pending": 2,
                "wrong_revision_status": 409,
                "disconnect_status": 200,
                "provider_calls": 0,
                "token_persisted": False,
            }
        ],
        "cancel": [
            {
                "legacy_revision": "legacy-revision",
                "legacy_consent": True,
                "v2_jobs": 1,
                "v2_generation": generation,
                "disconnect_status": 200,
                "provider_calls": 0,
            }
        ],
        "repair": [
            {
                "job": "job-1",
                "generation": generation,
                "failed_repair_exit": 1,
                "restart_exit": 0,
                "worker_state": "done",
                "attempts": 3,
                "ledger": {"calls": [generation, generation], "effects": [generation]},
                "legacy_revision": "legacy-revision",
                "cancel_status": 200,
            }
        ],
    }


def xml(contract, values=None):
    root = ElementTree.Element("testsuite")
    for kind, rows in (values or measured()).items():
        for index, row in enumerate(rows):
            case = ElementTree.SubElement(
                root, "testcase", name=contract.CASES[kind] + (f"[{index}]" if len(rows) > 1 else "")
            )
            properties = ElementTree.SubElement(case, "properties")
            ElementTree.SubElement(properties, "property", name="identity_observation", value=json.dumps(row))
    return ElementTree.tostring(root).decode()


def adapter_xml(contract):
    root = ElementTree.Element("testsuite")
    values = {
        "methods": [
            {
                "subject": "subject-1",
                "site_count": 2,
                "schema_rejected": True,
                "methods": ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"],
            }
        ],
        "forged": [{"forged_role_status": 403, "wrong_purpose_status": 403, "identity_calls": 0}],
        "refresh": [
            {
                "change": change,
                "status": {"unchanged": 200, "substituted": 403}.get(change, 503),
                "owner": "alice",
                "attempt": "attempt-1",
                "generation": 1,
                "core_checks": 2,
                "identity_refreshes": 1,
                "provider_calls": 1 if change == "unchanged" else 0,
                "token_reflected": False,
            }
            for change in ("unchanged", "reconnect", "disconnect", "substituted")
        ],
    }
    for kind, rows in values.items():
        for index, row in enumerate(rows):
            case = ElementTree.SubElement(
                root, "testcase", name=contract.ADAPTER_CASES[kind] + (f"[{index}]" if len(rows) > 1 else "")
            )
            ElementTree.SubElement(
                ElementTree.SubElement(case, "properties"),
                "property",
                name="identity_observation",
                value=json.dumps(row),
            )
    return ElementTree.tostring(root).decode()


@pytest.fixture
def report(contract):
    return {
        "schema_version": 1,
        "stage_id": "identity",
        "source_revision": SOURCE,
        "gateway_revision": GATEWAY,
        "gateway_image": GATEWAY_IMAGE,
        "adapter_image": ADAPTER_IMAGE,
        "build_id": BUILD_ID,
        "junit": xml(contract),
        "test_exit": 0,
        "adapter_junit": adapter_xml(contract),
        "adapter_test_exit": 0,
        "database_names_before": ["postgres", "template0", "template1"],
        "database_names_after": ["postgres", "template0", "template1"],
        "remaining_containers": [],
        "remaining_networks": [],
        "gateway_import": "/app/src/auth/atlassian_mcp_routes.py",
        "adapter_import": "/opt/adapter-venv/lib/python3.13/site-packages/tool_adapters/server.py",
        "gateway_source_sha256": "e" * 64,
        "expected_gateway_source_sha256": "e" * 64,
        "adapter_source_sha256": "f" * 64,
        "expected_adapter_source_sha256": "f" * 64,
        "forbidden_imports": [],
        "adapter_statuses": {"/healthz": 200, "/contract": 200, "/readyz": 200, "/internal/observe": 403},
    }


def check(contract, report):
    return contract.summary(
        report,
        source=SOURCE,
        gateway_source=GATEWAY,
        gateway_image=GATEWAY_IMAGE,
        adapter_image=ADAPTER_IMAGE,
        build_id=BUILD_ID,
        native={
            "before": {"adapter": {"cluster": "fictional-cluster", "namespace": "adp-tools"}},
            "after": {"adapter": {"cluster": "fictional-cluster", "namespace": "adp-tools"}},
            "lease_checks": 2,
            "workflow_revision": "1" * 40,
            "run_id": 17,
            "run_attempt": 1,
            "correlation": "orch:00000000-0000-4000-8000-000000000001",
        },
    )


def test_identity_summary_requires_all_measured_observations(contract, report):
    outcome = check(contract, report)
    assert outcome["criteria"] == [
        {"criterion_id": f"8448.AC-{index:02}", "phase_status": "pass", "evidence_verified": True}
        for index in range(1, 5)
    ]
    assert outcome["gateway_image"] == GATEWAY_IMAGE and outcome["adapter_image"] == ADAPTER_IMAGE
    assert outcome["runtime_verified"] and outcome["cleanup_status"] == "confirmed"
    assert outcome["correlation"] == "orch:00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    "field,value",
    [
        ("stage_id", "foundation"),
        ("source_revision", "0" * 40),
        ("gateway_revision", "b" * 40),
        ("gateway_image", ADAPTER_IMAGE),
        ("build_id", BUILD_ID + "-stale"),
        ("gateway_source_sha256", "0" * 64),
        ("adapter_source_sha256", "0" * 64),
        ("test_exit", 1),
        ("adapter_test_exit", 1),
        ("remaining_containers", ["fixture"]),
        ("database_names_after", ["unremoved-fixture"]),
    ],
)
def test_reader_rejects_wrong_provenance_or_incomplete_runtime(contract, report, field, value):
    report[field] = value
    with pytest.raises(ValueError):
        check(contract, report)


@pytest.mark.parametrize("change", ["missing", "duplicate", "failed", "skipped", "forged", "token", "ledger"])
def test_reader_rejects_missing_duplicate_failed_or_forged_observations(contract, report, change):
    root = ElementTree.fromstring(report["junit"])
    if change == "missing":
        root.remove(root[-1])
    elif change == "duplicate":
        root.append(copy.deepcopy(root[0]))
    elif change in {"failed", "skipped"}:
        ElementTree.SubElement(root[0], "failure" if change == "failed" else "skipped")
    else:
        value = json.loads(root[0].find("properties/property").get("value"))
        if change == "forged":
            value["site"] = "site-forged"
        elif change == "token":
            value["token_persisted"] = True
        else:
            case = next(case for case in root if case.get("name") == contract.CASES["repair"])
            value = json.loads(case.find("properties/property").get("value"))
            value["ledger"]["effects"].append("duplicate-effect")
        target = root[0] if change != "ledger" else case
        target.find("properties/property").set("value", json.dumps(value))
    report["junit"] = ElementTree.tostring(root).decode()
    with pytest.raises(ValueError):
        check(contract, report)


@pytest.mark.parametrize("change", ["missing", "duplicate", "skipped", "forged", "repeated_provider"])
def test_reader_rejects_unqualified_packaged_adapter_observation(contract, report, change):
    root = ElementTree.fromstring(report["adapter_junit"])
    if change == "missing":
        root.remove(root[0])
    elif change == "duplicate":
        root.append(copy.deepcopy(root[0]))
    elif change == "skipped":
        ElementTree.SubElement(root[0], "skipped")
    else:
        target = root[-1]
        value = json.loads(target.find("properties/property").get("value"))
        value["provider_calls" if change == "repeated_provider" else "owner"] = (
            2 if change == "repeated_provider" else "bob"
        )
        target.find("properties/property").set("value", json.dumps(value))
    report["adapter_junit"] = ElementTree.tostring(root).decode()
    with pytest.raises(ValueError):
        check(contract, report)


def delivery_env():
    return {
        "ADP_IDENTITY_CONTRACT_DIGEST": "f" * 64,
        "ADP_SOURCE_REVISION": SOURCE,
        "ADP_TOOLS_FOUNDATION_GATEWAY_REVISION": SOURCE,
        "ADP_DEFINITION_REVISION": "1" * 40,
        "ADP_CORRELATION": "orch:00000000-0000-4000-8000-000000000001",
        "GITHUB_RUN_ID": "17",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY_ID": "12",
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "fictional-cluster",
        "ADP_TOOLS_ADAPTER_NAMESPACE": "adp-tools",
    }


def test_generic_delivery_checks_bind_the_independently_qualified_summary(contract, report, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    producer = importlib.import_module("identity_delivery")
    summary = check(contract, report)
    result = producer.delivery(summary, delivery_env(), "repository-artifact:atlassian-identity:sha256:" + "a" * 64)
    assert [item["id"] for item in result["checks"]] == list(producer.CHECKS)
    assert all(item["outcome"] == "passed" for item in result["checks"])
    assert result["actual_revision"] == SOURCE and result["cleanup"] == "passed"
    assert result["contract_digest"] == delivery_env()["ADP_IDENTITY_CONTRACT_DIGEST"]
    for changed, value in (
        ("gateway_revision", "b" * 40),
        ("criteria", []),
        ("cleanup_status", "failed"),
        ("resource_id", "another/namespace"),
    ):
        forged = {**summary, changed: value}
        with pytest.raises(ValueError):
            producer.delivery(forged, delivery_env(), "private:measured")


def test_identity_delivery_starts_on_a_host_interpreter_without_the_adapter_runtime():
    """The release workflow runs this reporter with the host python3.

    -I -S starts a clean interpreter with no site-packages, no user site and no
    current directory on sys.path, so the script must import stdlib only. Any
    adapter runtime import (deploy -> adp_tools -> pydantic) fails here as the
    ModuleNotFoundError that broke the Identity delivery step.
    """
    script = Path(__file__).parents[1] / "identity_delivery.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr and "adp_tools" not in result.stderr
    assert all(option in result.stdout for option in ("--summary", "--receipt", "--output"))


def test_identity_delivery_writes_its_receipt_privately_and_exclusively(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    producer = importlib.import_module("identity_delivery")
    destination = tmp_path / "delivery.json"
    producer.write_private(destination, {"b": 2, "a": 1})
    assert destination.read_text() == '{"a":1,"b":2}\n'
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        producer.write_private(destination, {"a": 1})
    assert destination.read_text() == '{"a":1,"b":2}\n'
    source = (Path(__file__).parents[1] / "identity_delivery.py").read_text()
    assert "from deploy import" not in source and "import deploy" not in source


def test_private_identity_archive_readback_rejects_mismatched_summary(contract, report, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    producer = importlib.import_module("identity_delivery")
    summary = json.dumps(check(contract, report)).encode()
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as packed:
        packed.writestr("atlassian-stage-summary.json", summary)
    archive = content.getvalue()
    digest = hashlib.sha256(archive).hexdigest()
    key = "v1/12/17/1/repository-artifact/atlassian-identity.json"
    receipt = {
        "bucket": "adp-dev-deployment-evidence-123456789012",
        "key": key,
        "version_id": "descriptor-v1",
        "sha256": "a" * 64,
    }
    descriptor = {
        "component": "atlassian-identity",
        "bucket": receipt["bucket"],
        "source_revision": SOURCE,
        "workflow_revision": "1" * 40,
        "run_id": 17,
        "run_attempt": 1,
        "key": "private/archive.zip",
        "version_id": "archive-v1",
        "size": len(archive),
        "sha256": digest,
    }
    payload = json.dumps(descriptor).encode()
    receipt["sha256"] = hashlib.sha256(payload).hexdigest()
    envelope = json.dumps(
        {"transport": "s3-oidc-v1", "payload": base64.b64encode(payload).decode(), "signature": "fixture"}
    ).encode()

    def read(command, timeout):
        from pathlib import Path

        Path(command[-1]).write_bytes(envelope if "descriptor-v1" in command else archive)
        return json.dumps(
            {"VersionId": command[command.index("--version-id") + 1], "ServerSideEncryption": "AES256"}
        ).encode()

    monkeypatch.setattr(producer.subprocess, "check_output", read)
    assert producer.readback(summary, receipt, delivery_env()).endswith(digest)
    with pytest.raises(ValueError):
        producer.readback(b"forged", receipt, delivery_env())


def test_identity_build_reconciles_lost_upload_ack_without_repeating_build(contract, tmp_path, monkeypatch):
    import base64

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    release = importlib.import_module("foundation_release")
    env = {
        "ADP_TOOLS_ACCOUNT_ID": "123456789012",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "ADP_SOURCE_REVISION": SOURCE,
        "ADP_TOOLS_STATE_BUCKET": "fictional-state-bucket",
        "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment",
        "ADP_TOOLS_FOUNDATION_GATEWAY_REVISION": SOURCE,
        "ADP_DEFINITION_REVISION": "1" * 40,
        "ADP_CORRELATION": delivery_env()["ADP_CORRELATION"],
        "GITHUB_RUN_ID": "17",
        "GITHUB_RUN_ATTEMPT": "1",
        "ADP_QUALIFICATION_STAGE": "identity",
    }
    check = release.FoundationCheck(env)
    calls = []
    monkeypatch.setattr(check, "preflight", lambda action: None)
    monkeypatch.setattr(check, "guard", lambda: setattr(check, "lease_checks", check.lease_checks + 1))
    monkeypatch.setattr(
        check,
        "readback",
        lambda work, gateway_source: {
            "gateway_image": GATEWAY_IMAGE,
            "adapter": {"publication": {"image_digest": ADAPTER_IMAGE.split("@", 1)[1]}},
        },
    )
    monkeypatch.setattr(check, "source_hash", lambda *args: "e" * 64)
    monkeypatch.setattr(check, "build", lambda build_id: {"buildStatus": "SUCCEEDED", "id": build_id})
    monkeypatch.setattr(
        check,
        "result",
        lambda build, reference, work, expected: {
            "expected_gateway_source_sha256": expected["ADP_FOUNDATION_GATEWAY_HASH"],
            "expected_adapter_source_sha256": expected["ADP_FOUNDATION_ADAPTER_HASH"],
        },
    )
    monkeypatch.setattr(contract, "summary", lambda report, **pins: pins["build_id"])

    def command(args):
        if args[:2] == ["git", "archive"]:
            Path(args[args.index("-o") + 1]).write_bytes(b"reviewed-source")
        return ""

    monkeypatch.setattr(check, "run", command)
    checksum = base64.b64encode(hashlib.sha256(b"reviewed-source").digest()).decode()

    def aws(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("s3api", "put-object"):
            raise RuntimeError("controlled acknowledgement loss")
        if args[:2] == ("s3api", "head-object"):
            return {"VersionId": "source-v1", "ChecksumSHA256": checksum}
        if args[:2] == ("codebuild", "start-build"):
            return {"build": {"id": BUILD_ID}}
        raise AssertionError(args)

    monkeypatch.setattr(check, "aws", aws)
    assert check.qualify(tmp_path) == check.qualify(tmp_path) == BUILD_ID
    starts = [args for args in calls if args[:2] == ("codebuild", "start-build")]
    assert len(starts) == 2 and starts[0] == starts[1]
    assert (
        starts[0][starts[0].index("--idempotency-token") + 1] == starts[1][starts[1].index("--idempotency-token") + 1]
    )
    uploads = [args for args in calls if args[:2] == ("s3api", "put-object")]
    assert len(uploads) == 2 and uploads[0][uploads[0].index("--key") + 1] == uploads[1][uploads[1].index("--key") + 1]


def test_image_runner_stages_adapter_wheels_when_gateway_already_has_them(contract, report, tmp_path, monkeypatch):
    runtime = importlib.import_module("foundation_runtime")
    wheels = set()
    installs = []
    env = {
        "ADP_QUALIFICATION_STAGE": "identity",
        "ADP_SOURCE_SHA": SOURCE,
        "ADP_FOUNDATION_GATEWAY_REVISION": SOURCE,
        "ADP_FOUNDATION_GATEWAY_IMAGE": GATEWAY_IMAGE,
        "ADP_FOUNDATION_ADAPTER_IMAGE": ADAPTER_IMAGE,
        "ADP_FOUNDATION_GATEWAY_HASH": "e" * 64,
        "ADP_FOUNDATION_ADAPTER_HASH": "f" * 64,
        "ADP_FOUNDATION_POSTGRES_IMAGE": "public.ecr.aws/docker/library/postgres@sha256:" + "8" * 64,
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "CODEBUILD_BUILD_ID": BUILD_ID,
    }

    def image(container, files, import_name):
        kind = "gateway" if container.endswith("-gateway") else "adapter"
        return {
            "import": report[kind + "_import"],
            "sha256": report[kind + "_source_sha256"],
            "distributions": {"packaging": "26.3", "pygments": "2.21.0"} if kind == "gateway" else {},
        }

    def command(args, **kwargs):
        output = b""
        if args[:3] == ["aws", "sts", "get-caller-identity"]:
            output = json.dumps(
                {
                    "Account": env["ACCOUNT_ID"],
                    "Arn": f"arn:aws:sts::{env['ACCOUNT_ID']}:assumed-role/{BUILD_ID.split(':')[0]}/session",
                }
            ).encode()
        elif "download" in args:
            wheels.update(arg.split("==")[0] for arg in args if "==" in arg)
        elif "install" in args:
            installs.append(args)
            required = args[args.index("/testdeps") + 1 :]
            assert all(arg.split("==")[0] in wheels for arg in required), "offline wheel missing"
        elif args[:2] == ["docker", "exec"]:
            if "psql" in args:
                output = b"postgres\ntemplate0\ntemplate1\n"
            elif any("read_text()" in arg for arg in args):
                output = report[
                    "adapter_junit" if any("adapter-identity.xml" in arg for arg in args) else "junit"
                ].encode()
            elif any("find_spec" in arg for arg in args):
                output = b"[]"
            elif any("HTTPConnection" in arg for arg in args):
                output = json.dumps(report["adapter_statuses"]).encode()
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(runtime, "image_observation", image)
    monkeypatch.setattr(runtime, "run", command)
    result = runtime.execute(env, tmp_path)
    assert result["test_exit"] == result["adapter_test_exit"] == 0
    assert {"packaging", "pygments"} <= wheels
    gateway_install = next(args for args in installs if args[2].endswith("-gateway"))
    assert not any(arg.startswith(("packaging==", "pygments==")) for arg in gateway_install)


def identity_runner_environment():
    return {
        "ADP_QUALIFICATION_STAGE": "identity",
        "ADP_SOURCE_SHA": SOURCE,
        "ADP_FOUNDATION_GATEWAY_REVISION": SOURCE,
        "ADP_FOUNDATION_GATEWAY_IMAGE": GATEWAY_IMAGE,
        "ADP_FOUNDATION_ADAPTER_IMAGE": ADAPTER_IMAGE,
        "ADP_FOUNDATION_GATEWAY_HASH": "e" * 64,
        "ADP_FOUNDATION_ADAPTER_HASH": "f" * 64,
        "ADP_FOUNDATION_POSTGRES_IMAGE": "public.ecr.aws/docker/library/postgres@sha256:" + "8" * 64,
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "CODEBUILD_BUILD_ID": BUILD_ID,
    }


def test_identity_gateway_fixture_generates_its_own_token_secret_key(contract, report, tmp_path, monkeypatch):
    """The gateway image builds TokenManager while src.auth is imported.

    image_observation imports src.auth.atlassian_mcp_routes directly, before any
    gateway conftest can provide a key, so a fixture started without
    BG_TOKEN_SECRET_KEY fails that import and the stage retains cleanup state but
    no gateway_import and no JUnit (#8723). The runner has to supply its own
    single-use value: generated per fixture, never taken from the build
    environment, never given to the adapter and never retained as evidence.
    """
    runtime = importlib.import_module("foundation_runtime")
    env = identity_runner_environment()
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "live-deployment-secret")
    starts, observed = [], []

    def container_environment(args):
        return dict(args[index + 1].split("=", 1) for index, value in enumerate(args) if value == "-e")

    def image(container, files, import_name):
        kind = "gateway" if container.endswith("-gateway") else "adapter"
        if kind == "gateway":
            start = next(args for args in starts if args[args.index("--name") + 1] == container)
            key = container_environment(start).get("BG_TOKEN_SECRET_KEY", "")
            if not key:
                raise ValueError("Secret key must be provided via BG_TOKEN_SECRET_KEY environment variable")
            assert import_name == "src.auth.atlassian_mcp_routes"
            observed.append(key)
        return {
            "import": report[kind + "_import"],
            "sha256": report[kind + "_source_sha256"],
            "distributions": {},
        }

    def command(args, **kwargs):
        output = b""
        if args[:3] == ["aws", "sts", "get-caller-identity"]:
            output = json.dumps(
                {
                    "Account": env["ACCOUNT_ID"],
                    "Arn": f"arn:aws:sts::{env['ACCOUNT_ID']}:assumed-role/{BUILD_ID.split(':')[0]}/session",
                }
            ).encode()
        elif args[:2] == ["docker", "run"]:
            starts.append(args)
        elif args[:2] == ["docker", "exec"]:
            if "psql" in args:
                output = b"postgres\ntemplate0\ntemplate1\n"
            elif any("read_text()" in arg for arg in args):
                output = report[
                    "adapter_junit" if any("adapter-identity.xml" in arg for arg in args) else "junit"
                ].encode()
            elif any("find_spec" in arg for arg in args):
                output = b"[]"
            elif any("HTTPConnection" in arg for arg in args):
                output = json.dumps(report["adapter_statuses"]).encode()
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(runtime, "image_observation", image)
    monkeypatch.setattr(runtime, "run", command)
    for index in range(2):
        work = tmp_path / f"fixture-{index}"
        work.mkdir()
        measured = runtime.execute(env, work)
        assert measured["test_exit"] == measured["adapter_test_exit"] == 0
        assert measured["remaining_containers"] == measured["remaining_networks"] == []
        assert "BG_TOKEN_SECRET_KEY" not in json.dumps(measured)
    assert len(observed) == 2 and observed[0] != observed[1]
    assert all(len(key) >= 32 and key != "live-deployment-secret" for key in observed)
    assert not any(key in json.dumps(measured) for key in observed)
    gateway = next(args for args in starts if GATEWAY_IMAGE in args)
    assert "--read-only" in gateway and "--cap-drop=ALL" in gateway
    assert "--security-opt=no-new-privileges" in gateway and gateway[gateway.index("--network") + 1] != "host"
    adapter = next(args for args in starts if ADAPTER_IMAGE in args)
    assert not any("BG_TOKEN_SECRET_KEY" in item for item in adapter)


def engine_dispatch():
    """The inputs a correlated engine dispatch actually carries.

    GitHub omits a workflow_dispatch input the dispatcher did not send instead
    of applying its declared default, so the optional manual source is absent
    from the Identity context an engine dispatch produces (#8723).
    """
    inputs = {
        "mode": "identity",
        "stage": "tool-adapters",
        "environment": "dev",
        "adp_contract_digest": "d" * 64,
        "adp_source_revision": "a" * 40,
        "adp_definition_revision": "b" * 40,
        "adp_correlation": "c" * 64,
    }
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "42",
        "GITHUB_REPOSITORY_ID": "17",
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_WORKFLOW_SHA": "e" * 40,
        "GITHUB_WORKFLOW_REF": "example/repo/.github/workflows/tools-platform-deploy.yml@refs/heads/main",
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "e" * 40,
        "ADP_CORRELATION": "c" * 64,
        "ADP_IDENTITY_CONTRACT_DIGEST": "d" * 64,
        "ADP_TOOLS_ACCOUNT_ID": "123456789012",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "example-cluster",
        "ADP_TOOLS_ADAPTER_NAMESPACE": "tool-adapters",
    }
    return env, inputs


def dispatch_context(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    return importlib.import_module("identity_dispatch_context")


def test_identity_context_accepts_engine_dispatch_without_optional_manual_source(monkeypatch):
    module = dispatch_context(monkeypatch)
    env, inputs = engine_dispatch()
    document = module.context(env, inputs)
    assert document["inputs"] == {
        "mode": "identity",
        "stage": "tool-adapters",
        "environment": "dev",
        "adp_contract_digest": inputs["adp_contract_digest"],
    }
    assert document["source_revision"] == env["ADP_SOURCE_REVISION"]
    assert document["correlation"] == inputs["adp_correlation"]
    manual = module.context(env, dict(inputs, manual_source_revision=""))
    assert manual["inputs"] == document["inputs"] | {"manual_source_revision": ""}


@pytest.mark.parametrize(
    "change",
    [{"manual_source_revision": "f" * 40}, {"identity_gateway_revision": "f" * 40}, {"adp_source_revision": "f" * 40}],
)
def test_identity_context_still_refuses_changed_or_unknown_inputs(monkeypatch, change):
    module = dispatch_context(monkeypatch)
    env, inputs = engine_dispatch()
    with pytest.raises(RuntimeError):
        module.context(env, dict(inputs, **change))


def test_identity_context_still_refuses_a_missing_required_input(monkeypatch):
    module = dispatch_context(monkeypatch)
    env, inputs = engine_dispatch()
    del inputs["adp_contract_digest"]
    with pytest.raises(RuntimeError):
        module.context(env, inputs)
