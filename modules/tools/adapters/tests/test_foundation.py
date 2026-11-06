"""Foundation evidence must carry measured outcomes and exact runtime bindings."""

import copy
import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

SOURCE = "a" * 40
GATEWAY = "b" * 40
GATEWAY_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "c" * 64
ADAPTER_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-dev-adapters@sha256:" + "d" * 64
BUILD_ID = "adp-tools-dev-adapters-foundation-check:00000000-0000-0000-0000-000000000001"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    return SimpleNamespace(
        **{name: importlib.import_module("foundation_" + name) for name in ("contract", "runtime", "release")}
    )


def junit(contract, values=None):
    values = values or {
        "setup": {
            "provider_registrations": 1,
            "secret_creates": 0,
            "ledger_remaining": False,
            "first": {"callback_url": "https://identity.example/callback", "return_url": "https://app.example/return"},
            "second": {"callback_url": "https://identity.example/callback", "return_url": "https://app.example/return"},
        },
        "readiness": {
            "regular_status": 403,
            "blocked_status": 403,
            "callback_statuses": [303, 303, 405],
            "callback_locations": [
                "/settings/connections?oauth_consent=error",
                "/settings/connections?oauth_consent=error",
                None,
            ],
            "callback_payload_reflected": False,
            "callback_effect_calls": 0,
            "provider_registrations": 1,
            "policy": {"status": "policy_blocked", "owner": "atlassian_organization_admin"},
        },
        "recovery": {
            "provider_creates": 1,
            "registration_requests": 1,
            "failed_repair_exit": 1,
            "failed_repair_state": "leased",
            "restart_exit": 0,
            "job_state": "done",
            "setup_phase": "prepared",
            "attempts": 3,
        },
        "lease": {
            "competing": "conflict",
            "expired": "conflict",
            "stale": "conflict",
            "successor_applied": True,
            "fixture_one": "isolated-one",
            "fixture_two": "isolated-two",
        },
    }
    root = ElementTree.Element("testsuite")
    for kind, name in contract.CASES.items():
        case = ElementTree.SubElement(root, "testcase", name=name)
        properties = ElementTree.SubElement(case, "properties")
        ElementTree.SubElement(properties, "property", name="foundation_observation", value=json.dumps(values[kind]))
        if kind == "recovery":
            ElementTree.SubElement(properties, "property", name="foundation_ledger_remaining", value="false")
    return ElementTree.tostring(root)


@pytest.fixture
def candidate(modules):
    return {
        "schema_version": 1,
        "source_revision": SOURCE,
        "gateway_revision": GATEWAY,
        "gateway_image": GATEWAY_IMAGE,
        "adapter_image": ADAPTER_IMAGE,
        "build_id": BUILD_ID,
        "junit": junit(modules.contract).decode(),
        "test_exit": 0,
        "database_names_before": ["postgres", "template0", "template1"],
        "database_names_after": ["postgres", "template0", "template1"],
        "remaining_containers": [],
        "remaining_networks": [],
        "gateway_import": "/app/src/shared/identity/oauth_setup.py",
        "adapter_import": "/opt/adapter-venv/lib/python3.13/site-packages/tool_adapters/server.py",
        "forbidden_imports": [],
        "adapter_statuses": {"/healthz": 200, "/contract": 200, "/readyz": 200, "/internal/observe": 403},
        "gateway_source_sha256": "e" * 64,
        "expected_gateway_source_sha256": "e" * 64,
        "adapter_source_sha256": "f" * 64,
        "expected_adapter_source_sha256": "f" * 64,
    }


def pins():
    return dict(
        source=SOURCE,
        gateway_source=GATEWAY,
        gateway_image=GATEWAY_IMAGE,
        adapter_image=ADAPTER_IMAGE,
        build_id=BUILD_ID,
    )


def native():
    return {
        "before": {"gateway": "observed", "adapter": "observed"},
        "after": {"gateway": "observed", "adapter": "observed"},
        "lease_checks": 2,
        "workflow_revision": "1" * 40,
        "run_id": 17,
        "run_attempt": 1,
    }


def test_observed_foundation_summary_preserves_separate_gateway_source(modules, candidate):
    summary = modules.contract.summary(candidate, native=native(), **pins())
    assert summary["criteria"] == {
        f"8447.AC-{index:02}": {"phase_status": "pass", "evidence_verified": True} for index in range(1, 5)
    }
    assert summary["source_revision"] == SOURCE and summary["gateway_revision"] == GATEWAY
    assert summary["provider_live_attestation"] is False
    assert summary["unresolved_operations"] == [] and summary["run_id"] == "17" and summary["run_attempt"] == "1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "9" * 40),
        ("gateway_image", ADAPTER_IMAGE),
        ("build_id", BUILD_ID + "wrong"),
        ("test_exit", 1),
        ("database_names_after", ["postgres", "leaked_fixture"]),
        ("remaining_containers", ["fixture"]),
        ("remaining_networks", ["fixture"]),
        ("gateway_import", "/checkout/src/shared/identity/oauth_setup.py"),
        ("gateway_source_sha256", "0" * 64),
        ("adapter_source_sha256", "0" * 64),
        ("forbidden_imports", ["sqlalchemy"]),
        ("adapter_statuses", {"/healthz": 200, "/contract": 200, "/readyz": 200, "/internal/observe": 200}),
    ],
)
def test_unverified_runtime_or_cleanup_never_emits_pass(modules, candidate, field, value):
    candidate[field] = value
    with pytest.raises(ValueError):
        modules.contract.summary(candidate, native=native(), **pins())


@pytest.mark.parametrize(
    "change",
    [
        "skip",
        "duplicate",
        "missing",
        "unmeasured",
        "registration",
        "recovery",
        "isolation",
        "ledger",
        "callback_status",
        "callback_redirect",
        "callback_forged_success",
        "callback_effect",
        "callback_reflection",
    ],
)
def test_junit_pass_counts_cannot_substitute_for_independent_observations(modules, candidate, change):
    root = ElementTree.fromstring(candidate["junit"])
    if change == "skip":
        ElementTree.SubElement(root[0], "skipped")
    elif change == "duplicate":
        root.append(copy.deepcopy(root[0]))
    elif change == "missing":
        root.remove(root[0])
    elif change == "unmeasured":
        root[0].remove(root[0][0])
    elif change == "ledger":
        root[2][0][1].set("value", "true")
    else:
        position, key, value = {
            "registration": (0, "provider_registrations", 2),
            "recovery": (2, "job_state", "leased"),
            "isolation": (3, "fixture_two", "isolated-one"),
            "callback_status": (1, "callback_statuses", [404, 404]),
            "callback_redirect": (1, "callback_locations", ["https://example.invalid/", None, None]),
            "callback_forged_success": (
                1,
                "callback_locations",
                ["/settings/connections?oauth_consent=returned", "/settings/connections?oauth_consent=error", None],
            ),
            "callback_effect": (1, "callback_effect_calls", 1),
            "callback_reflection": (1, "callback_payload_reflected", True),
        }[change]
        prop = root[position][0][0]
        prop.set("value", json.dumps({**json.loads(prop.get("value")), key: value}))
    candidate["junit"] = ElementTree.tostring(root).decode()
    with pytest.raises(ValueError):
        modules.contract.summary(candidate, native=native(), **pins())


@pytest.mark.parametrize("change", ["runtime", "lease", "attempt"])
def test_native_window_must_remain_verified(modules, candidate, change):
    window = native()
    window[{"runtime": "after", "lease": "lease_checks", "attempt": "run_attempt"}[change]] = {
        "runtime": {"gateway": "changed"},
        "lease": 1,
        "attempt": 2,
    }[change]
    with pytest.raises(ValueError):
        modules.contract.summary(candidate, native=window, **pins())


def test_missing_immutable_image_binding_precedes_any_provider_call(modules, tmp_path, monkeypatch):
    monkeypatch.setattr(
        modules.runtime, "run", lambda *args, **kwargs: pytest.fail("must validate before provider access")
    )
    env = {
        "ADP_SOURCE_SHA": SOURCE,
        "ADP_FOUNDATION_GATEWAY_REVISION": GATEWAY,
        "ADP_FOUNDATION_GATEWAY_IMAGE": GATEWAY_IMAGE.replace("@sha256:", ":"),
        "ADP_FOUNDATION_ADAPTER_IMAGE": ADAPTER_IMAGE,
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "CODEBUILD_BUILD_ID": BUILD_ID,
    }
    with pytest.raises(ValueError):
        modules.runtime.execute(env, tmp_path)


@pytest.fixture
def runtime_env():
    return {
        "ADP_SOURCE_SHA": SOURCE,
        "ADP_FOUNDATION_GATEWAY_REVISION": GATEWAY,
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


@pytest.mark.parametrize("failure", [None, "test", "startup", "cleanup", "cleanup_timeout", "database"])
def test_image_runner_isolates_fixtures_and_retains_failed_cleanup(
    modules, candidate, runtime_env, tmp_path, monkeypatch, failure
):
    calls = []
    database_reads = 0

    def command(args, **kwargs):
        nonlocal database_reads
        calls.append((args, kwargs))
        output, code = b"", 0
        if args[:3] == ["aws", "sts", "get-caller-identity"]:
            output = json.dumps(
                {
                    "Account": "123456789012",
                    "Arn": "arn:aws:sts::123456789012:assumed-role/" + BUILD_ID.split(":")[0] + "/session",
                }
            ).encode()
        elif args[:3] == ["aws", "ecr", "get-login-password"]:
            output = b"synthetic-login-password"
        elif args[:2] == ["docker", "run"]:
            if failure == "startup" and GATEWAY_IMAGE in args:
                raise ValueError("controlled startup failure")
        elif args[:2] == ["docker", "exec"]:
            if "psql" in args:
                database_reads += 1
                output = b"postgres\ntemplate0\ntemplate1\n"
                if failure == "database" and database_reads == 2:
                    output += b"leaked-database\n"
            elif "src.shared.identity.oauth_setup" in args or "tool_adapters.server" in args:
                kind = "gateway" if "src.shared.identity.oauth_setup" in args else "adapter"
                output = json.dumps(
                    {
                        "import": candidate[kind + "_import"],
                        "sha256": candidate[kind + "_source_sha256"],
                        "distributions": {},
                    }
                ).encode()
            elif any("pytest.main(" in item for item in args):
                code = 1 if failure == "test" else 0
            elif any("read_text()" in item for item in args):
                output = candidate["junit"].encode()
            elif any("find_spec" in item for item in args):
                output = b"[]"
            elif any("HTTPConnection" in item for item in args):
                output = json.dumps(candidate["adapter_statuses"]).encode()
        elif args[:2] == ["docker", "rm"] and failure == "cleanup_timeout":
            raise subprocess.TimeoutExpired(args, 60)
        elif args[:2] == ["docker", "ps"] and failure in {"cleanup", "cleanup_timeout"}:
            if failure == "cleanup_timeout":
                raise subprocess.TimeoutExpired(args, 60)
            output = b"remaining-owned-container\n"
        return subprocess.CompletedProcess(args, code, output, b"")

    monkeypatch.setattr(modules.runtime, "run", command)
    if failure:
        with pytest.raises(ValueError):
            modules.runtime.execute(runtime_env, tmp_path)
    else:
        measured = modules.runtime.execute(runtime_env, tmp_path)
        assert measured["test_exit"] == 0 and measured["remaining_networks"] == []
    report = json.loads((tmp_path / "result.json").read_text())
    if failure in {"cleanup", "cleanup_timeout"}:
        assert report["remaining_containers"] == [
            "unverified" if failure == "cleanup_timeout" else "remaining-owned-container"
        ]
    removals = [args for args, _ in calls if args[:2] == ["docker", "rm"]]
    assert len(removals) == 3 and len({args[-1] for args in removals}) == 3
    assert all(args[-1].startswith("adp-foundation-") for args in removals)
    assert any(args[:3] == ["docker", "network", "rm"] for args, _ in calls)
    network = next(args for args, _ in calls if args[:3] == ["docker", "network", "create"])
    assert "--internal" in network
    gateway = next(args for args, _ in calls if args[:2] == ["docker", "run"] and GATEWAY_IMAGE in args)
    mounts = [gateway[index + 1] for index, value in enumerate(gateway) if value == "-v"]
    assert len(mounts) == 2 and all(mount.endswith(":ro") for mount in mounts)
    assert {mount.split(":")[-2] for mount in mounts} == {"/app/tests", "/wheels"}
    assert not any("AWS_ACCESS_KEY_ID" in item or "AWS_CONTAINER_CREDENTIALS" in item for item in gateway)
    assert "--read-only" in gateway and "--cap-drop=ALL" in gateway and "--security-opt=no-new-privileges" in gateway
    assert [args[-1] for args, _ in calls if args[:2] == ["docker", "pull"]] == [
        GATEWAY_IMAGE,
        ADAPTER_IMAGE,
        runtime_env["ADP_FOUNDATION_POSTGRES_IMAGE"],
    ]
    login = next(kwargs for args, kwargs in calls if args[:2] == ["docker", "login"])
    assert login["input"] == b"synthetic-login-password"
    if failure != "startup":
        download = next(args for args, _ in calls if "download" in args)
        assert "--only-binary=:all:" in download and "--no-deps" in download
        install = next(args for args, _ in calls if "install" in args)
        assert "--no-index" in install and "--no-deps" in install and "/testdeps" in install
        adapter = next(args for args, _ in calls if args[:2] == ["docker", "run"] and ADAPTER_IMAGE in args)
        assert not any("/testdeps" in item for item in adapter)
    tmpfs = [gateway[index + 1] for index, value in enumerate(gateway) if value == "--tmpfs"]
    assert "/tmp:rw,noexec,mode=1777" in tmpfs
    assert "/testdeps:rw,exec,nosuid,nodev,uid=65532,gid=65532,mode=0755" in tmpfs
    assert "PYTHONPATH=/testdeps:/app" in gateway


@pytest.fixture
def native_check(modules, monkeypatch):
    check = modules.release.FoundationCheck(
        {
            "ADP_TOOLS_ACCOUNT_ID": "123456789012",
            "ADP_TOOLS_AWS_REGION": "us-east-1",
            "ADP_TOOLS_ENVIRONMENT": "dev",
            "ADP_SOURCE_REVISION": SOURCE,
            "ADP_TOOLS_STATE_BUCKET": "example-state-bucket",
            "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment",
            "ADP_TOOLS_FOUNDATION_GATEWAY_REVISION": GATEWAY,
            "ADP_DEFINITION_REVISION": "1" * 40,
            "GITHUB_RUN_ID": "17",
            "GITHUB_RUN_ATTEMPT": "1",
        }
    )

    def guard():
        check.lease_checks += 1

    monkeypatch.setattr(check, "guard", guard)
    monkeypatch.setattr(modules.release.time, "sleep", lambda _: None)
    return check


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "late_log",
        "missing_log",
        "duplicate_log",
        "role",
        "source",
        "environment",
        "result_key",
        "version",
        "checksum",
        "bytes",
    ],
)
def test_native_result_binds_immutable_source_and_versioned_measured_bytes(
    modules, candidate, native_check, monkeypatch, tmp_path, failure
):
    import base64
    import hashlib

    check = native_check
    body = json.dumps(candidate).encode()
    digest = hashlib.sha256(body).hexdigest()
    checksum = base64.b64encode(bytes.fromhex(digest)).decode()
    reference = dict(
        key="codebuild/src/" + check.project + "/source.zip", version="source-v1", checksum="source-checksum"
    )
    expected = {"ADP_SOURCE_SHA": SOURCE}
    build = dict(
        id=BUILD_ID,
        buildStatus="SUCCEEDED",
        serviceRole="arn:aws:iam::123456789012:role/" + check.project,
        sourceVersion="source-v1",
        source=dict(type="S3", location=check.bucket + "/" + reference["key"], buildspec=modules.release.BUILDSPEC),
        environment=dict(environmentVariables=[dict(name="ADP_SOURCE_SHA", value=SOURCE, type="PLAINTEXT")]),
        logs=dict(groupName="/aws/codebuild/" + check.project, streamName="owned-stream"),
    )
    record = dict(
        key=f"codebuild/artifacts/{check.project}/{BUILD_ID.split(':')[1]}/result.json",
        version="result-v1",
        sha256=digest,
    )
    if failure == "role":
        build["serviceRole"] += "-other"
    if failure == "source":
        build["sourceVersion"] = "other-version"
    if failure == "environment":
        build["environment"]["environmentVariables"][0]["value"] = "9" * 40
    if failure == "result_key":
        record["key"] = "other/result.json"
    calls = []
    log_reads = 0

    def aws(*args, **kwargs):
        nonlocal log_reads
        calls.append(args)
        if args[:2] == ("s3api", "head-object"):
            assert args[args.index("--version-id") + 1] == "source-v1"
            return dict(VersionId="source-v1", ChecksumSHA256="source-checksum")
        if args[:2] == ("logs", "filter-log-events"):
            log_reads += 1
            assert args[args.index("--log-stream-names") + 1] == "owned-stream"
            events = [dict(message=modules.runtime.MARKER + json.dumps(record))]
            if failure == "missing_log" or (failure == "late_log" and log_reads == 1):
                events = []
            if failure == "duplicate_log":
                events *= 2
            return dict(events=events)
        if args[:2] == ("s3api", "get-object"):
            assert args[args.index("--version-id") + 1] == "result-v1"
            Path(args[-1]).write_bytes(b"tampered" if failure == "bytes" else body)
            return dict(
                VersionId="wrong" if failure == "version" else "result-v1",
                ContentLength=len(body),
                ChecksumSHA256="wrong" if failure == "checksum" else checksum,
            )
        pytest.fail("unexpected native call")

    monkeypatch.setattr(check, "aws", aws)
    if failure not in {None, "late_log"}:
        with pytest.raises(importlib.import_module("deploy").DeploymentError):
            check.result(build, reference, tmp_path, expected)
    else:
        assert check.result(build, reference, tmp_path, expected) == candidate
    if failure == "late_log":
        assert log_reads == 2 and check.lease_checks == 2
    if failure == "missing_log":
        assert log_reads == 12
    if failure in {"role", "source", "environment", "result_key", "duplicate_log", "missing_log"}:
        assert not any(args[:2] == ("s3api", "get-object") for args in calls)


@pytest.mark.parametrize(
    "failure", [None, "unpinned", "source", "unready", "pod_image", "pod_owner", "deployment_owner"]
)
def test_native_readback_verifies_running_gateway_and_adapter(modules, native_check, monkeypatch, tmp_path, failure):
    check = native_check
    deployment = dict(
        metadata=dict(uid="deployment", generation=3),
        status=dict(observedGeneration=3, updatedReplicas=1, availableReplicas=1),
        spec=dict(replicas=1, template=dict(spec=dict(containers=[dict(name="bedrockgateway", image=GATEWAY_IMAGE)]))),
    )
    replicaset = dict(
        metadata=dict(
            name="gateway-rs",
            uid="replicaset",
            ownerReferences=[dict(controller=True, kind="Deployment", uid="deployment")],
        )
    )
    pods = dict(
        items=[
            dict(
                metadata=dict(
                    uid="pod",
                    ownerReferences=[dict(controller=True, kind="ReplicaSet", name="gateway-rs", uid="replicaset")],
                ),
                spec=dict(containers=[dict(image=GATEWAY_IMAGE)]),
                status=dict(
                    containerStatuses=[
                        dict(ready=True, state=dict(running={"startedAt": "now"}), imageID=GATEWAY_IMAGE)
                    ]
                ),
            )
        ]
    )
    if failure == "unpinned":
        deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "gateway:latest"
    if failure == "unready":
        deployment["status"]["availableReplicas"] = 0
    if failure == "pod_image":
        pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = GATEWAY_IMAGE[:-1] + "0"
    if failure == "pod_owner":
        pods["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "other"
    if failure == "deployment_owner":
        replicaset["metadata"]["ownerReferences"][0]["uid"] = "other"
    check.adapter.env["KUBECONFIG"] = "/owned/kubeconfig"
    monkeypatch.setattr(check.adapter, "runtime", lambda work, *, apply: {"observed_at": "now", "release": "verified"})
    monkeypatch.setattr(
        check,
        "aws",
        lambda *args: {"imageDetails": [{"imageDigest": "sha256:" + ("9" if failure == "source" else "c") * 64}]},
    )
    resources = {"deployments/bedrockgateway": deployment, "replicasets/gateway-rs": replicaset, "pods": pods}

    def read(args):
        assert args[:3] == ["kubectl", "--kubeconfig", "/owned/kubeconfig"]
        assert not args[-1].endswith("/replicasets")
        return json.dumps(next(value for path, value in resources.items() if args[-1].endswith("/" + path)))

    monkeypatch.setattr(check, "run", read)
    if failure:
        with pytest.raises(importlib.import_module("deploy").DeploymentError):
            check.readback(tmp_path, GATEWAY)
    else:
        result = check.readback(tmp_path, GATEWAY)
        assert result["gateway_image"] == GATEWAY_IMAGE and result["gateway_pods"] == [
            {"uid": "pod", "image_id": GATEWAY_IMAGE}
        ]
        assert result["adapter"] == {"release": "verified"} and check.lease_checks == 1


@pytest.mark.parametrize("failure", [None, "failed_build", "lost_lease", "drift", "hash"])
def test_qualification_holds_lease_and_stops_only_its_own_unfinished_build(
    modules, native_check, candidate, monkeypatch, tmp_path, failure
):
    import base64
    import hashlib

    check = native_check
    calls = []
    monkeypatch.setattr(check, "preflight", lambda action: None)
    monkeypatch.setattr(
        check, "source_hash", lambda revision, prefix, files: ("e" if revision == GATEWAY else "f") * 64
    )

    def run(args):
        if args[1] == "archive":
            Path(args[args.index("-o") + 1]).write_bytes(b"immutable-source-archive")
        return ""

    monkeypatch.setattr(check, "run", run)
    before = {"gateway_image": GATEWAY_IMAGE, "adapter": {"publication": {"image_digest": "sha256:" + "d" * 64}}}
    readbacks = []

    def readback(work, gateway_source):
        assert gateway_source == GATEWAY
        check.guard()
        readbacks.append(work)
        result = copy.deepcopy(before)
        if failure == "drift" and len(readbacks) == 2:
            result["gateway_image"] = "changed"
        return result

    monkeypatch.setattr(check, "readback", readback)

    def aws(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("s3api", "put-object"):
            assert args[args.index("--if-none-match") + 1] == "*"
            return dict(
                VersionId="source-v1",
                ChecksumSHA256=base64.b64encode(hashlib.sha256(b"immutable-source-archive").digest()).decode(),
            )
        if args[:2] == ("codebuild", "start-build"):
            assert args[args.index("--source-version") + 1] == "source-v1"
            values = json.loads(args[args.index("--environment-variables-override") + 1])
            assert {item["name"]: item["value"] for item in values}["ADP_FOUNDATION_GATEWAY_IMAGE"] == GATEWAY_IMAGE
            return {"build": {"id": BUILD_ID}}
        assert args == ("codebuild", "stop-build", "--id", BUILD_ID)
        return {}

    monkeypatch.setattr(check, "aws", aws)

    if failure == "lost_lease":

        def expire_guard():
            check.lease_checks += 1
            if check.lease_checks > 1:
                raise importlib.import_module("deploy").DeploymentError("adapter_native_release_guard_refused")

        monkeypatch.setattr(check, "guard", expire_guard)

    def build(build_id):
        assert build_id == BUILD_ID
        return {"id": build_id, "buildStatus": "FAILED" if failure == "failed_build" else "SUCCEEDED"}

    monkeypatch.setattr(check, "build", build)
    if failure == "hash":
        candidate["expected_gateway_source_sha256"] = "9" * 64
    monkeypatch.setattr(check, "result", lambda *args: candidate)
    if failure:
        with pytest.raises((ValueError, importlib.import_module("deploy").DeploymentError)):
            check.qualify(tmp_path)
    else:
        report = check.qualify(tmp_path)
        assert report["test_runtime_artifacts_match"] and report["shared_release_lease_verified"]
        assert report["fixture_isolation_verified"] and report["provider_live_attestation"] is False
        assert len(readbacks) == 2
    stopped = [args for args in calls if args[:2] == ("codebuild", "stop-build")]
    assert stopped == (
        [("codebuild", "stop-build", "--id", BUILD_ID)] if failure in {"failed_build", "lost_lease"} else []
    )


@pytest.mark.parametrize("failed", [False, True])
def test_build_retains_versioned_cleanup_result_without_hiding_failure(modules, monkeypatch, capsys, failed):
    import hashlib

    monkeypatch.setenv("DOCKER_CONFIG", "/unused")
    monkeypatch.setenv("ADP_FOUNDATION_ARTIFACT_BUCKET", "example-state-bucket")
    report = {"build_id": BUILD_ID, "remaining_containers": ["unverified"] if failed else []}

    def execute(env, directory):
        (directory / "result.json").write_text(json.dumps(report))
        if failed:
            raise ValueError("private diagnostic must not be logged")
        return report

    monkeypatch.setattr(modules.runtime, "execute", execute)
    stored = []

    def upload(args):
        assert args[:3] == ["aws", "s3api", "put-object"]
        assert args[args.index("--if-none-match") + 1] == "*"
        stored.append(Path(args[args.index("--body") + 1]).read_bytes())
        result = {"VersionId": "v1", "ChecksumSHA256": args[args.index("--checksum-sha256") + 1]}
        return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b"")

    monkeypatch.setattr(modules.runtime, "run", upload)
    if failed:
        with pytest.raises(ValueError, match="foundation_qualification_failed"):
            modules.runtime.main()
    else:
        modules.runtime.main()
    assert len(stored) == 1 and json.loads(stored[0]) == report
    output = capsys.readouterr().out
    assert "private diagnostic" not in output and output.startswith(modules.runtime.MARKER)
    reference = json.loads(output.removeprefix(modules.runtime.MARKER))
    assert reference["sha256"] == hashlib.sha256(stored[0]).hexdigest() and reference["version"] == "v1"
