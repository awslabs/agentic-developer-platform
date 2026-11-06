"""Offline contracts for the protected S3 evidence consumer."""

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from tests.e2e.tools import evidence_reader, runner
from tests.e2e.tools.test_runner import GATEWAY_IMAGE, IMAGE, SHA
from tests.e2e.tools.release_authorization import authorization_cases
from tests.e2e.tools.test_release_observer import fixtures as release_fixtures


BINDING = {"account": "000000000000", "region": "us-east-1", "cluster": "fixture-cluster",
           "role": "arn:aws:iam::000000000000:role/adp-dev-tools-trusted-deployment",
           "group": "adp:tools-release-dev", "components": release_fixtures()[0]["components"]}


RUN = {"repository": "aws-e/adp", "account": "000000000000", "run_id": "314",
       "attempt": "2", "revision": SHA, "gateway": SHA, "release_source_revision": "e" * 40, "gateway_image": GATEWAY_IMAGE,
       "facade_image": IMAGE, "scenario": "SCN-8067-01", "authorization_binding": BINDING}


def fixture(tmp_path, change=None, *, run=RUN):
    report = {"evidence_schema": "workflow-evaluation/v1", "schema_version": 1,
              "live_attestation": True, "scenario_filter": run["scenario"], "matrix_complete": True,
              "selected_scenario_ids": [run["scenario"]], "harness_revision": run["revision"],
              "github_run_id": run["run_id"], "github_run_attempt": run["attempt"],
              "deployed_revisions": {"gateway": run["gateway"], "gateway_image": GATEWAY_IMAGE,
                                     "facade_image": IMAGE}, "scenarios": []}
    window = {"source_revision": run["release_source_revision"], "components": ["gateway", "facade and reconciler"],
              "receipts": {name: {"source_revision": run["gateway"] if name == "gateway" else run["release_source_revision"],
                                  "run_id": 5, "run_attempt": 1}
                           for name in ("gateway", "facade and reconciler")}}
    for client in runner.CLIENTS:
        report["scenarios"].append({"scenario_id": run["scenario"], "tags": ["@scenario SCN-8067-01",
               "@story 8067", "@acceptance AC-01"], "client": client, "cell_id": client + ":install",
               "tool": "install", "status": "pass", "evidence_scope": "deployed-target",
               "client_version": "1.0.0", "observations": {"source": "verified-installed-cli",
                 "installed_cli_sha256": "b" * 64, "installed_mcp_sha256": "c" * 64,
                 "discovery_sha256": "d" * 64, "runtime_window": window}})
    if change:
        change(report)
    runner.write_results(tmp_path, report)
    files = {name: (tmp_path / name).read_bytes() for name in ("evidence.json", "junit.xml")}
    files["authorization.json"] = json.dumps({"version": 1, "status": "pass",
        "definition_revision": run["revision"], "workflow_run_id": run["run_id"],
        "workflow_run_attempt": run["attempt"], "identity": {"aws_matches": True,
        "kubernetes_matches": True}, "reason": "verified",
        "binding_sha256": hashlib.sha256(json.dumps(BINDING, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "expected_checks": 43, "checks": [{"name": case.name, "expected": case.allowed,
            "observed": case.allowed, "pass": True} for case in authorization_cases(BINDING["components"])]}).encode()
    for phase in ("before", "after"):
        files[f"runtime-{phase}.json"] = json.dumps({"phase": phase,
            "runtime": {"gateway": {"image": GATEWAY_IMAGE.rsplit("@", 1)[-1]},
                        "facade and reconciler": {"image": IMAGE.rsplit("@", 1)[-1]}}}).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as packed:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            packed.addfile(info, io.BytesIO(content))
    packed = archive.getvalue()
    artifact_sha = hashlib.sha256(packed).hexdigest()
    bucket = f"adp-dev-ci-evidence-{run['account']}"
    prefix = f"artifacts/14/{run['repository']}/{run['run_id']}/{run['attempt']}/evaluate/tools-live-evaluation-{run['run_id']}-{run['attempt']}/"
    artifact_key = prefix + artifact_sha + ".tar.gz"
    manifest = {"schema_version": 1, "repository": run["repository"], "run_id": run["run_id"],
                "run_attempt": run["attempt"], "job": "evaluate",
                "name": f"tools-live-evaluation-{run['run_id']}-{run['attempt']}",
                "retention_days": 14, "checked_out_revision": run["revision"], "workflow_revision": run["revision"],
                "artifact": {"bucket": bucket, "key": artifact_key, "sha256": artifact_sha,
                             "version_id": "v1", "size": len(packed)},
                "files": [{"path": name, "size": len(content),
                           "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items()]}
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    manifest_key = prefix + hashlib.sha256(manifest_bytes).hexdigest() + ".manifest.json"
    uri = "s3://" + bucket + "/" + manifest_key
    objects = {manifest_key: manifest_bytes, artifact_key: packed}

    def fetch(received_bucket, key):
        assert received_bucket == bucket and key in objects
        data = objects[key]
        return data, {"VersionId": "v1", "ContentLength": len(data),
                      "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode()}

    return uri, fetch, objects


def test_reader_consumes_versioned_private_manifest_and_exact_client_records(tmp_path):
    uri, fetch, _ = fixture(tmp_path)
    assert evidence_reader.verify_retained(uri, "v1", **RUN, fetch=fetch) == 3


@pytest.mark.parametrize("tamper", ("missing", "wrong_version", "wrong_bytes", "wrong_target", "not_pass"))
def test_reader_rejects_missing_or_changed_evidence(tmp_path, tamper):
    change = None
    if tamper == "not_pass":
        change = lambda report: report["scenarios"][1].update(status="blocked")
    uri, fetch, objects = fixture(tmp_path, change)
    if tamper == "missing":
        fetch = lambda bucket, key: (_ for _ in ()).throw(ValueError("unavailable"))
    if tamper == "wrong_version":
        with pytest.raises(ValueError, match="incomplete"):
            evidence_reader.verify_retained(uri, "v2", **RUN, fetch=fetch)
        return
    if tamper == "wrong_bytes":
        key = next(key for key in objects if key.endswith(".tar.gz"))
        objects[key] += b"changed"
    if tamper == "wrong_target":
        with pytest.raises(ValueError, match="incomplete"):
            evidence_reader.verify_retained(uri, "v1", **{**RUN, "gateway_image": IMAGE}, fetch=fetch)
        return
    with pytest.raises(ValueError):
        evidence_reader.verify_retained(uri, "v1", **RUN, fetch=fetch)


def test_workflow_binds_only_dedicated_client_session_and_consumes_published_receipt():
    workflow = yaml.load((Path(__file__).resolve().parents[3] / ".github/workflows/tools-live-evaluation.yml").read_text(),
                         Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["evaluate"]["steps"]
    bind = next(step for step in steps if step.get("name") == "Bind the dedicated installed-client test session")
    assert "secrets.ADP_TOOLS_DEDICATED_EVAL_REFRESH_TOKEN" in bind["env"]["DEDICATED_REFRESH"]
    assert "ADP_TOOLS_REFRESH_FILE=%s" in bind["run"] and "umask 077" in bind["run"]
    assert "TLS_EVAL_REFRESH_TOKEN" not in str(bind)
    upload = next(step for step in steps if step.get("id") == "publish")
    consume = next(step for step in steps if step.get("name") == "Retrieve and validate protected client evidence")
    assert steps.index(bind) < steps.index(upload) < steps.index(consume)
    assert consume["if"].startswith("always()")
    assert consume["env"]["CI_EVIDENCE_MANIFEST_URI"] == "${{ steps.publish.outputs.manifest-uri }}"
    assert consume["env"]["CI_EVIDENCE_MANIFEST_VERSION"] == "${{ steps.publish.outputs.manifest-version }}"
    assert "tests.e2e.tools.evidence_reader" in consume["run"]
    cleanup = next(step for step in steps if step.get("name") == "Remove owned evaluation configuration and test-user session")
    assert "tools-dedicated-refresh" in cleanup["run"] and steps.index(consume) < steps.index(cleanup)


def test_reader_rejects_changed_independent_snapshot_or_missing_client(tmp_path):
    uri, fetch, objects = fixture(tmp_path)
    archive = next(value for key, value in objects.items() if key.endswith(".tar.gz"))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
        files = {member.name: packed.extractfile(member).read() for member in packed}
    inputs = {name: RUN[name] for name in ("scenario", "run_id", "attempt", "revision", "gateway",
                                           "gateway_image", "facade_image", "authorization_binding", "release_source_revision")}
    snapshot = json.loads(files["runtime-after.json"])
    snapshot["runtime"]["gateway"]["image"] = "sha256:" + "f" * 64
    files["runtime-after.json"] = json.dumps(snapshot).encode()
    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.validate_rows(files, **inputs)
    files["runtime-after.json"] = json.dumps({"phase": "after", "runtime": json.loads(files["runtime-before.json"])["runtime"]}).encode()
    report = json.loads(files["evidence.json"])
    report["scenarios"].pop()
    files["evidence.json"] = json.dumps(report).encode()
    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.validate_rows(files, **inputs)


def test_s3_reader_uses_only_existing_scoped_runner_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace
    token = tmp_path / "runner-token"
    token.write_text("synthetic")
    monkeypatch.setenv("CI_EVIDENCE_RUNNER_ROLE_ARN", "arn:aws:iam::000000000000:role/runner")
    monkeypatch.setenv("CI_EVIDENCE_RUNNER_TOKEN_FILE", str(token))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "unsafe-ambient")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))

    def aws(command, *, env, **kwargs):
        assert env["AWS_ROLE_ARN"] == "arn:aws:iam::000000000000:role/runner"
        assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == str(token)
        assert "AWS_ACCESS_KEY_ID" not in env and "AWS_SECURITY_TOKEN" not in env
        Path(command[-3]).write_bytes(b"readback")
        return SimpleNamespace(stdout="{}")

    monkeypatch.setattr(evidence_reader.subprocess, "run", aws)
    assert evidence_reader.fetch_s3("private", "key") == (b"readback", {})
    monkeypatch.delenv("CI_EVIDENCE_RUNNER_ROLE_ARN")
    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.fetch_s3("private", "key")


def test_reader_rejects_missing_or_failed_authorization_proof(tmp_path):
    uri, fetch, objects = fixture(tmp_path)
    archive = next(value for key, value in objects.items() if key.endswith(".tar.gz"))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
        files = {member.name: packed.extractfile(member).read() for member in packed}
    inputs = {name: RUN[name] for name in ("scenario", "run_id", "attempt", "revision", "gateway",
                                           "gateway_image", "facade_image", "authorization_binding", "release_source_revision")}
    files.pop("authorization.json")
    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.validate_rows(files, **inputs)
    files["authorization.json"] = json.dumps({"version": 1, "status": "fail"}).encode()
    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.validate_rows(files, **inputs)


@pytest.mark.parametrize("tamper", ("anonymous", "missing_case", "duplicate", "unknown", "expected", "observed",
                                     "integer_pass", "binding", "run", "attempt", "revision", "reason", "other_components"))
def test_reader_requires_full_bound_authorization_contract(tmp_path, tamper):
    _, _, objects = fixture(tmp_path)
    archive = next(value for key, value in objects.items() if key.endswith(".tar.gz"))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
        files = {member.name: packed.extractfile(member).read() for member in packed}
    inputs = {name: RUN[name] for name in ("scenario", "run_id", "attempt", "revision", "gateway",
                                          "gateway_image", "facade_image", "authorization_binding", "release_source_revision")}
    auth = json.loads(files["authorization.json"])
    assert auth["expected_checks"] == len(auth["checks"]) == 43
    if tamper == "anonymous":
        auth.update(expected_checks=1, checks=[{"pass": True}])
    elif tamper == "missing_case":
        auth["checks"].pop()
        auth["expected_checks"] = 42
    elif tamper == "duplicate":
        auth["checks"][-1] = auth["checks"][0]
    elif tamper == "unknown":
        auth["checks"][0]["name"] = "unapproved"
    elif tamper in ("expected", "observed"):
        auth["checks"][0][tamper] = False
    elif tamper == "integer_pass":
        auth["checks"][0]["pass"] = 1
    elif tamper == "binding":
        auth["binding_sha256"] = "f" * 64
    elif tamper == "run":
        auth["workflow_run_id"] = "315"
    elif tamper == "attempt":
        auth["workflow_run_attempt"] = "3"
    elif tamper == "revision":
        auth["definition_revision"] = "f" * 40
    elif tamper == "reason":
        auth["reason"] = "unverified"
    else:
        inputs["authorization_binding"] = {**BINDING, "components": {
            key: value for key, value in BINDING["components"].items() if key != "shared Lambda and Browser"}}
    files["authorization.json"] = json.dumps(auth).encode()
    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.validate_rows(files, **inputs)


@pytest.mark.parametrize("source,gateway", [(SHA, SHA), ("e" * 40, SHA), ("e" * 40, "d" * 40)])
def test_retained_install_binds_harness_gateway_and_release_sources_separately(tmp_path, source, gateway):
    from tests.e2e.tools.test_release_window import fixture as release_fixture

    run = {**RUN, "release_source_revision": source, "gateway": gateway}
    release = release_fixture()
    release.update(source_revision=source, account=RUN["account"], paths=["modules/tools/facade/tools_facade/server.py"])
    release["components"] = {name: release["components"][name] for name in ("gateway", "facade and reconciler")}
    expected = {}
    for name, image in (("gateway", GATEWAY_IMAGE), ("facade and reconciler", IMAGE)):
        component = release["components"][name]
        revision = gateway if name == "gateway" else source
        expected[name] = {"image": image.rsplit("@", 1)[-1]}
        component.update(source_revision=revision, expected_runtime=expected[name])
        component["receipt"].update(source_revision=revision, account=RUN["account"],
                                    component_digests=expected[name], runtime_digests=expected[name])

    def readback(phase):
        assert phase in {"before", "after"}
        return expected

    def change(report):
        for row in report["scenarios"]:
            row["observations"]["runtime_window"] = window

    with runner.client_runtime_window(release, readback, source=source, account=RUN["account"],
                                      gateway_image=GATEWAY_IMAGE, facade_image=IMAGE) as window:
        uri, fetch, _ = fixture(tmp_path, change, run=run)
    assert evidence_reader.verify_retained(uri, "v1", **run, fetch=fetch) == len(runner.CLIENTS)


@pytest.mark.parametrize("tamper", ["window", "gateway_receipt", "facade_receipt", "harness", "expected_source"])
def test_retained_install_cannot_self_select_a_different_source(tmp_path, tamper):
    def change(report):
        if tamper == "harness":
            report["harness_revision"] = RUN["release_source_revision"]
        for row in report["scenarios"]:
            window = row["observations"]["runtime_window"]
            if tamper == "window":
                window["source_revision"] = SHA  # Valid hash, wrong independently supplied release.
            elif tamper == "gateway_receipt":
                window["receipts"]["gateway"]["source_revision"] = RUN["release_source_revision"]
            elif tamper == "facade_receipt":
                window["receipts"]["facade and reconciler"]["source_revision"] = SHA

    uri, fetch, _ = fixture(tmp_path, change)
    inputs = {**RUN, **({"release_source_revision": "f" * 40} if tamper == "expected_source" else {})}
    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.verify_retained(uri, "v1", **inputs, fetch=fetch)


@pytest.mark.parametrize("source", [None, "", "E" * 40, "e" * 39])
def test_release_source_must_be_explicit_and_valid_before_private_readback(source):
    def unexpected(*args):
        pytest.fail("readback attempted without trusted release source")

    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.verify_retained("unused", "v1", **{**RUN, "release_source_revision": source}, fetch=unexpected)


def matrix_report(report):
    report["scenarios"] = []
    for client in runner.CLIENTS:
        for operation in runner.OPERATIONS:
            cleanup = {"status": "confirmed", "observed_at": "2026-10-18T01:00:00Z"}
            report["scenarios"].append({
                "scenario_id": "SCN-8067-02", "tags": ["@scenario SCN-8067-02", "@acceptance AC-02"],
                "client": client, "cell_id": client + ":" + operation, "tool": operation,
                "status": "pass", "client_version": "1.0.0", "cleanup": cleanup,
                "reason": "Bearer fixture-private-value",
                "observations": {"source": "installed-cli-mcp" if client == "cli" else client + "-native-mcp",
                    "installed_cli_sha256": "b" * 64, "installed_mcp_sha256": "c" * 64, "discovery_sha256": "d" * 64,
                    "authorization": "Bearer fixture-private-value", "receipt_id": "secret=fixture-private-value",
                    "provider_observation": {"source": "provider-control-plane", "provider_request_count": 1,
                        "assertion": {"source": "provider-control-plane", "operation": operation, "passed": True,
                                      "secret_access_key": "fixture-private-value"},
                        "cleanup": cleanup, "raw_tool_result": "fixture-private-value"}},
            })


def test_all_operation_cells_survive_publish_archive_and_retained_consumer_without_secret_values(tmp_path):
    run = {**RUN, "scenario": "SCN-8067-02"}
    uri, fetch, _ = fixture(tmp_path, matrix_report, run=run)
    # This executes the public producer and private manifest/archive consumer,
    # including every row's canonical checksum, without repairing the output.
    assert evidence_reader.verify_retained(uri, "v1", **run, fetch=fetch) == 90
    published = json.loads((tmp_path / "evidence.json").read_text())
    for row in published["scenarios"]:
        assert row["observations"]["provider_observation"]["assertion"]["operation"] == row["tool"]
    assert len([row for row in published["scenarios"] if row["tool"].startswith("credentials___")]) == 12
    for name in ("evidence.json", "junit.xml"):
        assert "fixture-private-value" not in (tmp_path / name).read_text()


@pytest.mark.parametrize("operation", ["credentials___list/token=fixture-private-value", "Bearer fixture-private-value",
                                       "credentials___assume_role", "credentials___unapproved"])
def test_assertion_operation_exception_never_accepts_secret_text_or_another_cell(tmp_path, operation):
    def change(report):
        matrix_report(report)
        row = next(row for row in report["scenarios"] if row["tool"] == "credentials___list")
        row["observations"]["provider_observation"]["assertion"]["operation"] = operation

    run = {**RUN, "scenario": "SCN-8067-02"}
    uri, fetch, _ = fixture(tmp_path, change, run=run)
    with pytest.raises(ValueError, match="incomplete"):
        evidence_reader.verify_retained(uri, "v1", **run, fetch=fetch)
    assert "fixture-private-value" not in (tmp_path / "evidence.json").read_text()
    published = json.loads((tmp_path / "evidence.json").read_text())
    row = next(row for row in published["scenarios"] if row["tool"] == "credentials___list")
    assert row["observations"]["provider_observation"]["assertion"]["operation"] == "[redacted]"


def test_reader_cli_binds_release_source_from_protected_workflow_environment(monkeypatch):
    from types import SimpleNamespace

    for name, value in {"GITHUB_REPOSITORY": RUN["repository"], "EXPECTED_ACCOUNT": RUN["account"],
                        "GITHUB_RUN_ID": RUN["run_id"], "GITHUB_RUN_ATTEMPT": RUN["attempt"],
                        "GITHUB_SHA": RUN["revision"], "GATEWAY_REVISION": RUN["gateway"],
                        "GATEWAY_IMAGE": GATEWAY_IMAGE, "FACADE_IMAGE": IMAGE,
                        "SCENARIO_FILTER": RUN["scenario"], "SOURCE_REVISION": RUN["release_source_revision"],
                        "EXPECTED_ROLE": BINDING["role"]}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(evidence_reader, "observer_from_environment", lambda env: SimpleNamespace(**{
        key: BINDING[key] for key in ("account", "region", "cluster", "components")}))
    calls = []
    monkeypatch.setattr(evidence_reader, "verify_retained", lambda *args, **kwargs: calls.append(kwargs) or 3)
    evidence_reader.main()
    assert calls[0]["release_source_revision"] == RUN["release_source_revision"] != calls[0]["revision"]
    monkeypatch.delenv("SOURCE_REVISION")
    with pytest.raises(SystemExit, match="could not be independently validated"):
        evidence_reader.main()
    assert len(calls) == 1
