"""The release selector and credential-bound WA qualification path stay fail closed offline."""

from contextlib import nullcontext
import base64
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import importlib.util
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import runpy
import subprocess
import ssl
from threading import Thread
import sys
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from l5_services.adapters.well_architected.source_evidence import read_source_files

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/L5-services/tests/well_architected"))
from test_wa07_native import cases, fixture, native_reader
from test_wa_qualification import selected_review, source_bound_observation
from wa_source_fixture import read_archive, source_fixture
from test_installed_probe import fixture_data
SCRIPT = ROOT / "modules/L5-services/scripts/qualification_well_architected.py"
ROLLUP = SCRIPT.with_name("qualification-rollup.py")
SPEC = importlib.util.spec_from_file_location("l5_wa_release_driver_test", SCRIPT)
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


def test_product_native_session_rejects_unselected_provider_identifiers():
    session = object.__new__(DRIVER.ProductNativeSession)
    session.observation = {"native_job_ref": {"profile_arn": "selected-profile", "generation_id": "selected-generation"},
                           "recommendations": []}
    with pytest.raises(ValueError, match="Profile"):
        session.get_agent_profile(profileArn="foreign-profile")
    with pytest.raises(ValueError, match="generation"):
        session.get_agent_recommendation_generation(profileArn="selected-profile", generationId="foreign-generation")
    with pytest.raises(ValueError, match="recommendation"):
        session.get_agent_recommendation(recommendationArn="foreign-recommendation")
    with pytest.raises(ValueError, match="unrecognized"):
        session._call("wellarchitected", "start-agent-recommendation-generation")


def test_shared_rollup_selects_real_well_architected_driver_without_aws_calls():
    result = subprocess.run([sys.executable, str(ROLLUP), "--service", "well_architected",
                             "--selection", "/nonexistent/fictional-private-selection.json",
                             "--release", "/nonexistent/fictional-release.json",
                             "--source-sha", "a" * 40,
                             "--output", "/nonexistent/fictional-private-output.json"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "selected qualification unavailable" in result.stderr
    assert "usage:" not in result.stderr


def test_private_input_rejects_checkout_and_world_readable_paths(tmp_path):
    local = ROOT / "modules/L5-services/scripts/qualification_well_architected.py"
    with pytest.raises(ValueError, match="outside the checkout"):
        DRIVER.private_json(local)
    public = tmp_path / "public.json"
    public.write_text("{}")
    public.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe permissions"):
        DRIVER.private_json(public)


def test_export_calls_emitter_and_native_evaluator_with_two_distinct_selected_projects(monkeypatch):
    projects = cases()
    for project in projects:
        project["result"]["scope"]["product_id"] = f"product-{project['architecture']['project_id']}"
    observed = {}
    selections = []
    for project in projects:
        source = project["source_receipt"]
        chosen = {"scope": project["result"]["scope"], "project_kind": "non_adp",
                  "run_id": project["run_id"], "result_revision": project["result_revision"],
                  "sources": source["sources"]}
        selections.append(chosen)
        observed[chosen["scope"]["project_id"]] = {
            "selection": chosen, "product": {
                "binding": {"binding_id": "binding-" + project["run_id"],
                            "provider_identity": {"account_id": "000000000000", "region": "us-east-1",
                                                  "role_arn": "arn:aws:iam::000000000000:role/qualification"}},
                "run": {"input": project["run_input"], "native_job_ref": project["native_job_ref"],
                        "source_manifest": project["source_manifest"]},
                "configuration": {"settings": {"well_architected": {
                    "goal_refs": [project["goal"]]}}}, "result": project["result"]},
            "native": {"generation": {"id": project["native_job_ref"]["generation_id"],
                        "additionalContext": {"s3Uri": source["provider_source_uri"],
                                              "s3VersionId": source["archive"]["version"]}},
                       "recommendations": {project["result"]["recommendations"][0]["native_id"]: {}}},
            "provider_coverage": "provider_coverage_unverified"}
        observed[chosen["scope"]["project_id"]]["source_files"] = read_source_files(
            project["source_manifest"], read_archive)
    called = []
    monkeypatch.setattr(DRIVER, "read_selected_review", lambda product, session, selection: (
        called.append(selection["scope"]["project_id"]) or observed[selection["scope"]["project_id"]]))
    read = native_reader(projects, [])

    def selected_session(selection):
        return SimpleNamespace(account_id="000000000000", region="us-east-1",
                               read_archive=read_archive,
                               _call=lambda service, operation, *arguments: read(service, operation, *arguments))

    result = DRIVER.qualify_cases(selections, fixture(), product_factory=lambda selection: nullcontext(None),
                                  session_factory=selected_session)
    assert called == [case["architecture"]["project_id"] for case in projects]
    assert {case["native_job_ref"]["profile_arn"] for case in result} == {
        case["native_job_ref"]["profile_arn"] for case in projects}
    assert all(case["source_receipt"]["provider_coverage"] == "provider_coverage_unverified"
               for case in result)
    assert [(case["binding_id"], case["provider_identity"]) for case in result] == [
        (value["product"]["binding"]["binding_id"], value["product"]["binding"]["provider_identity"])
        for value in observed.values()]
    with pytest.raises(ValueError, match="share a native Profile"):
        DRIVER.qualify_cases([selections[0], selections[0]], fixture(),
                             product_factory=lambda selection: nullcontext(None),
                             session_factory=selected_session)


def source_bound_release(
        tmp_path, monkeypatch, selected_review, scenario):
    source_bound_observation(selected_review)
    _, _, first, first_records, native, _ = selected_review

    def second(value):
        if isinstance(value, str):
            return value.replace("example", "example-second").replace(
                "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002")
        if isinstance(value, list):
            return [second(item) for item in value]
        if isinstance(value, dict):
            return {second(key): second(item) for key, item in value.items()}
        return value

    selections = [deepcopy(first), second(first)]
    records = [deepcopy(first_records), second(first_records)]
    manifest, citation = source_fixture(selections[1]["sources"][0], selections[1]["scope"], selections[1]["run_id"])
    records[1]["runs/run-example-second"]["source_manifest"] = manifest
    records[1]["results/result-example-second"].update(manifest_ref=manifest["archive"],
                                                      coverage=manifest["architecture"]["files"])
    records[1]["results/result-example-second"]["recommendations"][0]["comparison"]["citations"] = [citation]
    archives = {}
    for selected, record in zip(selections, records, strict=True):
        source = record[f"runs/{selected['run_id']}"]["source_manifest"]
        archives[source["provider_source_uri"]] = source["archive"]
    second_profile = second(native.get_agent_profile())
    fingerprint = sha256(json.dumps({
        "arn": second_profile["arn"], "aggregation": [], "role": second_profile["executionRoleArn"],
        "pillars": second_profile["pillars"], "contexts": [], "eligible": True,
        "scheduled": False, "field_errors": [],
    }, sort_keys=True).encode()).hexdigest()
    records[1]["bindings/binding-example-second"]["native_resource_ref"]["record_revision"] = fingerprint
    records[1]["runs/run-example-second"]["admission"]["native_profile_ref"]["record_revision"] = fingerprint
    records[1]["runs/run-example-second"]["admission"]["profile_hash"] = fingerprint
    for selection, available in zip(selections, records, strict=True):
        run_path = "runs/" + selection["run_id"]
        manifest = available[run_path]["source_manifest"]
        source = selection["sources"][0]
        selected_source = manifest["architecture"]["sources"][0]
        snapshot_id = selected_source["snapshot_id"]
        entries = [{"path": entry["path"], "source_ref": source, "source_revision": source["record_revision"],
                    "bytes": entry["bytes"], "sha256": entry["sha256"]} for entry in manifest["architecture"]["files"]]
        with zipfile.ZipFile(io.BytesIO(read_archive(manifest["provider_source_uri"], manifest["archive"]))) as archive:
            content = archive.read(manifest["architecture"]["files"][0]["archive_path"])
        inventory = json.dumps({"source_ref": source, "exclusions": [], "files": entries}).encode()
        snapshot_path = run_path + "/source-snapshots/" + snapshot_id
        available[run_path + "/source-manifest"] = {**manifest, "run_id": selection["run_id"], "scope": selection["scope"],
                                                   "sources": selection["sources"], "source_snapshot_ids": [snapshot_id], "exclusions": []}
        available[snapshot_path] = {"snapshot_id": snapshot_id, "source_ref": source, "exclusions": [], "file_count": len(entries),
                                   "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat()}
        for part, field, data in (("manifest", "file_manifest", inventory), ("content", "content", content)):
            available[snapshot_path + "/" + part] = data
            available[snapshot_path][field] = {"artifact_id": "products/source-snapshots/" + snapshot_id + "-" + part,
                                               "version": "version-one", "sha256": sha256(data).hexdigest(), "bytes": len(data),
                                               "media_type": "application/json" if part == "manifest" else "text/plain"}
    paths = []
    native_calls = []
    monkeypatch.setenv("FICTIONAL_WA_PRODUCT_TOKEN", "fictional-token")
    for selection in selections:
        selection.update(gateway_url="https://gateway.example.org/",
                         token_env="FICTIONAL_WA_PRODUCT_TOKEN", aws_label="fictional-connection",
                         provider_identity={"account_id": "000000000000", "region": "us-east-1",
                             "role_arn": "arn:aws:iam::000000000000:role/broker-example"})
    selections[1]["provider_identity"]["role_arn"] += "-second"
    if scenario == "fabricated_citation":
        records[1]["results/result-example-second"]["recommendations"][0]["comparison"]["citations"][0]["sha256"] = "f" * 64
    elif scenario == "fabricated_quote":
        records[1]["results/result-example-second"]["recommendations"][0]["comparison"]["citations"][0]["quote"] = "Invented quotation"
    elif scenario == "missing_limitation":
        records[1]["results/result-example-second"]["limitations"] = []
    elif scenario == "wrong_revision":
        selections[1]["result_revision"] += 1

    def respond(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer fictional-token"
        for selection, available in zip(selections, records, strict=True):
            scope = selection["scope"]
            prefix = (f"/api/v1/products/{scope['product_id']}/projects/{scope['project_id']}"
                      "/services/well_architected/")
            if request.url.path.startswith(prefix):
                resource = request.url.path.removeprefix(prefix)
                paths.append((scope["project_id"], resource))
                if resource.endswith("/native-observations"):
                    run = available["runs/" + selection["run_id"]]
                    result = available["results/" + selection["result_id"]]
                    profile = native.get_agent_profile()
                    generation = native.get_agent_recommendation_generation()
                    recommendations = [native.get_agent_recommendation(
                        recommendationArn=item["native_id"]) for item in result["recommendations"]]
                    if selection is selections[1]:
                        profile, generation, recommendations = second(profile), second(generation), second(recommendations)
                        if scenario == "native_source_drift":
                            generation["additionalContext"]["s3Uri"] = "s3://fictional-wa-bucket/foreign.zip"
                        if scenario == "native_bucket_drift":
                            generation["additionalContext"]["s3Uri"] = generation["additionalContext"]["s3Uri"].replace(
                                "fictional-wa-bucket", "foreign-bucket")
                    native_calls.extend([("wellarchitected", operation) for operation in (
                        "get-agent-profile", "get-agent-recommendation-generation", "get-agent-recommendation")])
                    manifest = run["source_manifest"]
                    return httpx.Response(200, json={
                        "schema_version": 1, **{field: selection[field] for field in (
                            "scope", "run_id", "run_revision", "configuration_revision", "binding_id", "binding_revision",
                            "result_id", "provider_identity")}, "result_revision": result["revision"],
                        "native_job_ref": run["native_job_ref"], "source_manifest": manifest,
                        "source_archive_base64": base64.b64encode(read_archive(manifest["provider_source_uri"], manifest["archive"])).decode(),
                        "profile": profile, "contexts": [], "generation": generation, "recommendations": recommendations,
                        "limitations": ["provider_source_coverage_unverified"],
                    })
                value = available[resource]
                return httpx.Response(200, content=value) if isinstance(value, bytes) else httpx.Response(200, json=value)
        return httpx.Response(404)

    original_client = httpx.Client
    monkeypatch.setattr(DRIVER.httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    source_sha = "a" * 40
    selection_path, release_path, output = (private / name for name in (
        "selection.json", "release.json", "observations.json"))
    selection_path.write_text(json.dumps({"schema_version": 1, "installed_release": fixture_data(),
                                        "source_sha": source_sha, "expires_at": (
        datetime.now(UTC) + timedelta(hours=1)).isoformat(), "cases": selections}))
    release_path.write_text(json.dumps({"schema_version": 1, "installed_release": fixture_data()}))
    selection_path.chmod(0o600)
    release_path.chmod(0o600)
    evaluated = []
    native_evaluator = DRIVER.verify_native_reviews

    def evaluate(cases, installed, read, **kwargs):
        evaluated.append(True)
        return native_evaluator(cases, installed, read, **kwargs)

    monkeypatch.setattr(DRIVER, "verify_native_reviews", evaluate)
    return SimpleNamespace(selection_path=selection_path, release_path=release_path, output=output,
                           source_sha=source_sha, evaluated=evaluated, paths=paths, selections=selections,
                           native_calls=native_calls, records=records, respond=respond, client_type=original_client)


@pytest.mark.parametrize("scenario", ["valid", "fabricated_citation", "fabricated_quote", "missing_limitation",
                                     "wrong_revision", "native_source_drift", "native_bucket_drift"])
def test_release_scenario_accepts_and_denies_source_bound_observations(
        tmp_path, monkeypatch, selected_review, scenario):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, scenario)
    selection_path, release_path, output = prepared.selection_path, prepared.release_path, prepared.output
    source_sha, selections = prepared.source_sha, prepared.selections

    completed = DRIVER.main(["--selection", str(selection_path), "--release", str(release_path),
                             "--source-sha", source_sha, "--output", str(output)])
    if scenario == "valid":
        assert completed == 0
        document = json.loads(output.read_text())
        assert len(document["cases"]) == 2
        assert document["provider_coverage"] == "provider_coverage_unverified"
        for selected, case in zip(selections, document["cases"], strict=True):
            assert case["source_receipt"]["sources"] == selected["sources"]
            assert case["source_receipt"]["provider_coverage"] == "provider_coverage_unverified"
            assert case["source_receipt"]["files"]
    else:
        assert completed == 1
        assert not output.exists()
    assert prepared.evaluated == ([True] if scenario == "valid" else [])
    assert len(prepared.paths) == (10 if scenario == "wrong_revision" else 18)
    assert {project for project, _ in prepared.paths} == {selection["scope"]["project_id"]
                                                   for selection in selections}
    assert all(service in {"sts", "iam", "wellarchitected", "s3api"} for service, _ in prepared.native_calls)
    assert ("wellarchitected", "get-agent-recommendation") in prepared.native_calls


@pytest.mark.parametrize("failure", ["bytes", "receipt", "expired", "coverage", "revoked", "unpinned", "manifest"])
def test_release_rejects_changed_or_unavailable_original_product_inputs(tmp_path, monkeypatch, selected_review, failure):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, "valid")
    case, available = prepared.selections[0], prepared.records[0]
    run_path = "runs/" + case["run_id"]
    snapshot_id = available[run_path + "/source-manifest"]["source_snapshot_ids"][0]
    snapshot_path = run_path + "/source-snapshots/" + snapshot_id
    if failure == "bytes":
        available[snapshot_path + "/content"] = b"not the captured source bytes"
    elif failure == "receipt":
        available[snapshot_path]["source_ref"] = {**case["sources"][0], "project_id": "foreign-project"}
    elif failure == "expired":
        available[snapshot_path]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif failure == "coverage":
        inventory = json.loads(available[snapshot_path + "/manifest"])
        inventory["files"][0]["path"] = "different-file.txt"
        content = json.dumps(inventory).encode()
        available[snapshot_path + "/manifest"] = content
        available[snapshot_path]["file_manifest"].update(bytes=len(content), sha256=sha256(content).hexdigest())
    elif failure == "unpinned":
        available[snapshot_path]["content"]["version"] = "latest"
    elif failure == "manifest":
        available[run_path + "/source-manifest"]["sources"] = []
    else:
        def denied(request):
            return httpx.Response(404) if "/source-snapshots/" in request.url.path else prepared.respond(request)
        monkeypatch.setattr(DRIVER, "product_connection", lambda selected: prepared.client_type(
            base_url=selected["gateway_url"], transport=httpx.MockTransport(denied),
            headers={"Authorization": "Bearer fictional-token"}))
    assert DRIVER.main(["--selection", str(prepared.selection_path), "--release", str(prepared.release_path),
                        "--source-sha", prepared.source_sha, "--output", str(prepared.output)]) == 1
    assert not prepared.output.exists()


@pytest.mark.parametrize("failure", [None, "filename", "bytes"])
def test_retained_repository_requires_exact_original_file_names_and_bytes(tmp_path, monkeypatch, selected_review, failure):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, "valid")
    case, available = prepared.selections[0], prepared.records[0]
    source = {**case["sources"][0], "record_type": "repository_commit"}
    case["sources"] = [source]
    manifest = available["runs/" + case["run_id"] + "/source-manifest"]
    manifest["sources"] = [source]
    manifest["architecture"]["sources"][0]["source_ref"] = source
    manifest["architecture"]["files"][0]["source_ref"] = source
    snapshot_path = "runs/" + case["run_id"] + "/source-snapshots/" + manifest["source_snapshot_ids"][0]
    receipt = available[snapshot_path]
    receipt["source_ref"] = source
    inventory = json.loads(available[snapshot_path + "/manifest"])
    inventory["source_ref"] = source
    inventory["files"][0]["source_ref"] = source
    available[snapshot_path + "/manifest"] = json.dumps(inventory).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("foreign.txt" if failure == "filename" else inventory["files"][0]["path"],
                         b"forged bytes" if failure == "bytes" else available[snapshot_path + "/content"])
    available[snapshot_path + "/content"] = output.getvalue()
    for part, field in (("manifest", "file_manifest"), ("content", "content")):
        content = available[snapshot_path + "/" + part]
        receipt[field].update(bytes=len(content), sha256=sha256(content).hexdigest())
    with DRIVER.product_connection(case) as client:
        product = DRIVER.ProductReader(client, case["scope"])
        if failure:
            with pytest.raises(ValueError):
                DRIVER.verify_retained_sources(product, case, manifest)
        else:
            DRIVER.verify_retained_sources(product, case, manifest)


def release_environment(monkeypatch, prepared):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.setitem(sys.modules, "qualification_well_architected", DRIVER)
    for key in ("ADP_CORRELATION", "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_CONTRACT_DIGEST"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "L5_SERVICE": "well_architected", "L5_ENVIRONMENT": "test",
        "GITHUB_REPOSITORY": "example/repository", "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": "example/repository/.github/workflows/l5-services-release.yml@refs/heads/main",
        "GITHUB_WORKFLOW_SHA": "b" * 40, "REVIEWED_SOURCE_SHA": prepared.source_sha,
        "L5_CHECKOUT_REVISION": prepared.source_sha, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        "L5_FIXTURE_DIGEST": sha256(prepared.selection_path.read_bytes()).hexdigest(),
    }.items():
        monkeypatch.setenv(key, value)
    return [prepared.source_sha, str(prepared.selection_path), str(prepared.selection_path.parent)]


def entry_point(monkeypatch, name, arguments):
    path = SCRIPT.with_name(name)
    monkeypatch.setattr(sys, "argv", [str(path), *arguments])
    with pytest.raises(SystemExit) as completed:
        runpy.run_path(str(path), run_name="__main__")
    return completed.value.code


def test_shared_check_driver_accepts_actual_executable_wa_entry_points():
    result = subprocess.run([sys.executable, str(SCRIPT.with_name("qualification-dispatch.py")),
                             "check-driver", "well_architected"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    for name in ("qualify-well-architected-release.py", "emit-well-architected-qualification.py"):
        result = subprocess.run([sys.executable, str(SCRIPT.with_name(name)), "--help"],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failure", [None, "prerequisite", "setup_failed", "run_failed", "run_unknown", "lost_response", "comparison_missing"])
def test_release_bootstrap_orders_setup_run_and_readback_without_repeating_effects(
        tmp_path, monkeypatch, selected_review, failure):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, "valid")
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import qualification_well_architected_bootstrap as bootstrap

    ticks = [0]
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(bootstrap.time, "sleep", lambda interval: ticks.__setitem__(0, ticks[0] + 1))
    for case, available in zip(prepared.selections, prepared.records, strict=True):
        case["result_revision"] = 2
        available["results/" + case["result_id"]]["revision"] = 2
        available["configuration"]["settings"]["well_architected"]["goal_refs"] = [case["sources"][0]]
        available["results/" + case["result_id"]]["goals"] = [case["sources"][0]["record_id"]]
    selected = json.loads(prepared.selection_path.read_text())
    for case, available in zip(selected["cases"], prepared.records, strict=True):
        for field in ("binding_id", "binding_revision", "run_id", "run_revision", "result_id", "result_revision"):
            case.pop(field)
        case["configuration_hash"] = available["configuration"]["hash"]
        case["bootstrap"] = {
            "binding_operation": {"action": "create", "configuration_revision": case["configuration_revision"]},
            "setup_idempotency_key": "setup-" + case["scope"]["project_id"] + "-0001",
            "run_idempotency_key": "run-" + case["scope"]["project_id"] + "-0001",
            "timeout_seconds": 30, "poll_seconds": 1,
        }
    prepared.selection_path.write_text(json.dumps(selected))
    arguments = release_environment(monkeypatch, prepared)
    original_client = prepared.client_type
    posts = []
    resumed = [False]

    def respond(request):
        assert request.headers["Authorization"] == "Bearer fictional-token"
        for case, available in zip(prepared.selections, prepared.records, strict=True):
            prefix = (f"/api/v1/products/{case['scope']['product_id']}/projects/{case['scope']['project_id']}"
                      "/services/well_architected/")
            if not request.url.path.startswith(prefix):
                continue
            resource = request.url.path.removeprefix(prefix)
            if resource == "qualification-prerequisites":
                if failure == "prerequisite":
                    return httpx.Response(403)
                return httpx.Response(200, json={"schema_version": 1, "status": "ready_for_setup",
                    "configuration": available["configuration"], "provider_identity": case["provider_identity"]})
            if request.method == "POST":
                assert resumed[0]
                posts.append((resource, request.headers["Idempotency-Key"], json.loads(request.content)))
                if resource == "binding-operations":
                    return httpx.Response(202, json={"operation_id": "setup-" + case["binding_id"], "state": "accepted"})
                assert resource == "runs"
                assert posts[-1][2]["binding_id"] == case["binding_id"]
                assert posts[-1][2]["sources"] == case["sources"]
                if failure == "lost_response":
                    raise httpx.ReadTimeout("lost admitted run response", request=request)
                return httpx.Response(202, json={"run_id": case["run_id"], "state": "accepted"})
            if resource.startswith("binding-operations/"):
                return httpx.Response(200, json={"operation_id": "setup-" + case["binding_id"],
                    "binding_id": case["binding_id"], "state": "failed" if failure == "setup_failed" else "succeeded"})
            if resource == "results":
                result = available["results/" + case["result_id"]]
                original = {**deepcopy(result), "result_id": "original-" + case["result_id"], "revision": 1}
                for item in original["recommendations"]:
                    item.pop("comparison", None)
                return httpx.Response(200, json={"items": [original] if failure == "comparison_missing" else [original, result]})
            if resource == "runs/" + case["run_id"] and failure in {"run_failed", "run_unknown"}:
                return httpx.Response(200, json={"run_id": case["run_id"],
                    "state": "failed" if failure == "run_failed" else "outcome_unknown"})
        return prepared.respond(request)

    monkeypatch.setattr(DRIVER, "product_connection", lambda case: original_client(
        base_url=case["gateway_url"], transport=httpx.MockTransport(respond),
        headers={"Authorization": "Bearer fictional-token"}))
    runner, emitter = "qualify-well-architected-release.py", "emit-well-architected-qualification.py"
    assert entry_point(monkeypatch, runner, ["preflight", *arguments]) == (1 if failure == "prerequisite" else 0)
    assert not posts and not prepared.native_calls
    assert not (prepared.selection_path.parent / "native-observations.json").exists()
    resumed[0] = True
    assert entry_point(monkeypatch, runner, ["run", *arguments]) == (0 if failure is None else 1)
    assert [resource for resource, _, _ in posts] == (
        [] if failure == "prerequisite" else ["binding-operations"] if failure == "setup_failed"
        else ["binding-operations", "runs"] if failure else ["binding-operations", "runs"] * 2)
    if failure is None:
        before = deepcopy(posts)
        assert entry_point(monkeypatch, emitter, arguments) == 0
        assert posts == before
        report = json.loads((prepared.selection_path.parent / "qualification.json").read_text())
        assert report["live_acceptance"] == "incomplete"
    else:
        assert not (prepared.selection_path.parent / "native-observations.json").exists()


def test_real_release_entrypoints_use_https_product_reads_without_worker_cli(tmp_path, monkeypatch, selected_review):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, "valid")
    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                    "-keyout", str(key), "-out", str(certificate)], check=True, capture_output=True, timeout=15)

    class ProductHTTP(BaseHTTPRequestHandler):
        def do_GET(self):
            response = prepared.respond(httpx.Request("GET", "https://localhost" + self.path, headers=dict(self.headers)))
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProductHTTP)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = json.loads(prepared.selection_path.read_text())
    for selected in payload["cases"]:
        selected["gateway_url"] = f"https://localhost:{server.server_port}/"
        selected.pop("aws_label", None)
    prepared.selection_path.write_text(json.dumps(payload))
    arguments = release_environment(monkeypatch, prepared)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("AWS_", "ADP_", "BG_"))}
    environment.update(PATH=str(empty_path), SSL_CERT_FILE=str(certificate), NO_PROXY="localhost,127.0.0.1")
    try:
        for name, prefix in (("qualify-well-architected-release.py", ["preflight"]),
                             ("qualify-well-architected-release.py", ["run"]),
                             ("emit-well-architected-qualification.py", [])):
            completed = subprocess.run([sys.executable, str(SCRIPT.with_name(name)), *prefix, *arguments],
                                       env=environment, capture_output=True, text=True, timeout=30)
            assert completed.returncode == 0, completed.stderr
        report = json.loads((prepared.selection_path.parent / "qualification.json").read_text())
        assert report["outcome"] == "incomplete" and report["live_acceptance"] == "incomplete"
        assert "provider_source_coverage_unverified" in report["limitations"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("change", [None, "lost_ack", "result", "absence", "adopted"])
def test_owned_cleanup_retains_native_evidence_and_emits_from_fresh_absence_and_retained_data(
        tmp_path, monkeypatch, selected_review, change):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, "valid")
    selected = prepared.selections[0]
    option = {"owned_cleanup_confirmed": True, "idempotency_key": "owned-cleanup-stable-0001", "timeout_seconds": 30, "poll_seconds": 1}
    document = json.loads(prepared.selection_path.read_text())
    document["cases"][0]["qualification_cleanup"] = option
    prepared.selection_path.write_text(json.dumps(document))
    owned = prepared.records[0]["bindings/" + selected["binding_id"]]
    owned["ownership"] = "adp_created"
    prepared.records[1]["bindings/" + prepared.selections[1]["binding_id"]]["ownership"] = "customer_linked"
    posts = []
    original_respond = prepared.respond

    def respond(request):
        path = request.url.path
        if request.method == "POST":
            assert path.endswith("/binding-operations")
            assert (prepared.selection_path.parent / "native-observations.json").is_file()
            posts.append((request.content, request.headers["Idempotency-Key"], request.headers["If-Match"]))
            assert json.loads(request.content) == {"action": "cleanup", "configuration_revision": selected["configuration_revision"],
                "binding_id": selected["binding_id"], "owned_cleanup_confirmed": True}
            owned.update(state="cleaned", revision=selected["binding_revision"] + 2)
            if change == "lost_ack" and len(posts) == 1:
                raise httpx.ReadTimeout("lost cleanup acknowledgement", request=request)
            return httpx.Response(202, json={"operation_id": "cleanup-original", "state": "succeeded",
                "binding_id": selected["binding_id"], "action": "cleanup"})
        if path.endswith("/binding-operations/cleanup-original"):
            return httpx.Response(200, json={"operation_id": "cleanup-original", "state": "succeeded",
                "binding_id": selected["binding_id"], "action": "cleanup"})
        if path.endswith("/cleanup-observations"):
            run = prepared.records[0]["runs/" + selected["run_id"]]
            return httpx.Response(200, json={"schema_version": 1,
                **{field: selected[field] for field in ("run_id", "run_revision", "configuration_revision", "binding_id",
                                                      "result_id", "result_revision", "provider_identity")},
                "scope": {field: selected["scope"][field] for field in ("tenant_id", "product_id", "project_id")},
                "binding_revision": owned["revision"], "native_job_ref": run["native_job_ref"],
                "source_manifest": run["source_manifest"], "runtime": {},
                "cleanup": {"owned_absent": change != "absence", "source": "independent-native-read",
                            "result_retained": True, "native_profile_arn": run["native_job_ref"]["profile_arn"]}})
        if owned["state"] == "cleaned":
            assert not path.endswith("/runs/" + selected["run_id"] + "/native-observations")
            if change == "result" and path.endswith("/results/" + selected["result_id"]):
                return httpx.Response(200, json={"result_id": "foreign"})
            if change == "adopted" and path.endswith("/bindings/" + prepared.selections[1]["binding_id"]):
                return httpx.Response(200, json={"state": "changed"})
        return original_respond(request)

    monkeypatch.setattr(DRIVER, "product_connection", lambda case: prepared.client_type(
        base_url=case["gateway_url"], transport=httpx.MockTransport(respond),
        headers={"Authorization": "Bearer fictional-token"}))
    args = release_environment(monkeypatch, prepared)
    runner, emitter = "qualify-well-architected-release.py", "emit-well-architected-qualification.py"
    assert entry_point(monkeypatch, runner, ["preflight", *args]) == 0
    assert not posts
    assert entry_point(monkeypatch, runner, ["run", *args]) == (0 if change is None else 1)
    if change == "lost_ack":
        assert entry_point(monkeypatch, runner, ["run", *args]) == 0
        assert len(posts) == 2 and posts[0] == posts[1]
    if change in {None, "lost_ack"}:
        count = len(posts)
        assert entry_point(monkeypatch, emitter, args) == 0
        assert len(posts) == count
        report = json.loads((prepared.selection_path.parent / "qualification.json").read_text())
        assert report["cleanup"]["outcome"] == "passed"
        assert next(row for row in report["live_checks"] if row["id"] == "owned-cleanup")["outcome"] == "passed"
        assert report["outcome"] == "incomplete" and not (prepared.selection_path.parent / "delivery-evidence.json").exists()
    else:
        assert entry_point(monkeypatch, emitter, args) == 1
        assert not (prepared.selection_path.parent / "qualification.json").exists()


@pytest.mark.parametrize("scenario", ["valid", "fabricated_citation", "fabricated_quote", "missing_limitation",
                                     "wrong_revision", "native_source_drift", "native_bucket_drift"])
def test_shared_release_entry_points_use_real_product_native_and_emitter_checks(
        tmp_path, monkeypatch, selected_review, scenario, capsys):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, scenario)
    arguments = release_environment(monkeypatch, prepared)
    directory = prepared.selection_path.parent
    runner = "qualify-well-architected-release.py"
    emitter = "emit-well-architected-qualification.py"
    assert entry_point(monkeypatch, runner, ["preflight", *arguments]) == (0 if scenario == "valid" else 1)
    assert not (directory / "native-observations.json").exists()
    assert entry_point(monkeypatch, runner, ["run", *arguments]) == (0 if scenario == "valid" else 1)
    assert entry_point(monkeypatch, emitter, arguments) == (0 if scenario == "valid" else 1)
    if scenario == "valid":
        for name in ("qualification.json",):
            path = directory / name
            report = json.loads(path.read_text())
            assert report["source_revision"] == prepared.source_sha
            assert report["definition_revision"] == "b" * 40
            assert report["workflow_path"] == ".github/workflows/l5-services-release.yml"
            assert report["live_acceptance"] == "incomplete"
            assert "provider_source_coverage_unverified" in report["limitations"]
            assert path.stat().st_mode & 0o077 == 0
        assert not (directory / "delivery-evidence.json").exists()
        import qualification_well_architected_acceptance as acceptance
        acceptance.validate_catalog(report)
        assert len(report["story_criteria"]) == 26
        assert all(row["outcome"] == "incomplete" for row in report["story_criteria"])
        assert len(prepared.paths) == 54
        assert prepared.evaluated == [True, True, True]
        before = len(prepared.native_calls)
        assert entry_point(monkeypatch, runner, ["run", *arguments]) == 1
        assert entry_point(monkeypatch, emitter, arguments) == 1
        assert len(prepared.native_calls) == before
    else:
        assert not (directory / "qualification.json").exists()
        assert not (directory / "delivery-evidence.json").exists()
    assert "000000000000" not in capsys.readouterr().out


@pytest.mark.parametrize("change", ["missing", "source", "definition", "run", "fixture", "expired",
                                    "limitation", "citation", "revoked", "wrong_role"])
def test_release_emitter_rejects_missing_forged_stale_or_revoked_observations(
        tmp_path, monkeypatch, selected_review, change):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, "valid")
    arguments = release_environment(monkeypatch, prepared)
    assert entry_point(monkeypatch, "qualify-well-architected-release.py", ["run", *arguments]) == 0
    directory = prepared.selection_path.parent
    observation_path = directory / "native-observations.json"
    observation = json.loads(observation_path.read_text())
    if change in {"source", "definition"}:
        observation["context"][change + "_revision"] = "f" * 40
    elif change == "run":
        observation["context"]["run_id"] += 1
    elif change == "fixture":
        monkeypatch.setenv("L5_FIXTURE_DIGEST", "f" * 64)
    elif change == "expired":
        observation["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif change == "limitation":
        observation["limitations"] = []
    elif change == "citation":
        observation["cases"][0]["result"]["recommendations"][0]["comparison"]["citations"][0]["quote"] = "fabricated"
    elif change == "revoked":
        prepared.records[0]["bindings/" + prepared.selections[0]["binding_id"]]["state"] = "revoked"
    elif change == "wrong_role":
        payload = json.loads(prepared.selection_path.read_text())
        payload["cases"][0]["provider_identity"]["role_arn"] += "-foreign"
        prepared.selection_path.write_text(json.dumps(payload))
        monkeypatch.setenv("L5_FIXTURE_DIGEST", sha256(prepared.selection_path.read_bytes()).hexdigest())
        observation["context"]["fixture_sha256"] = sha256(prepared.selection_path.read_bytes()).hexdigest()
    observation_path.write_text(json.dumps(observation))
    if change == "missing":
        observation_path.unlink()
    assert entry_point(monkeypatch, "emit-well-architected-qualification.py", arguments) == 1
    assert not (directory / "qualification.json").exists()
    assert not (directory / "delivery-evidence.json").exists()


@pytest.mark.parametrize("change", ["source", "expired", "public_fixture", "public_directory", "symlink", "checkout"])
def test_release_preflight_rejects_invalid_inputs_before_external_reads(
        tmp_path, monkeypatch, selected_review, change):
    prepared = source_bound_release(tmp_path, monkeypatch, selected_review, "valid")
    arguments = release_environment(monkeypatch, prepared)
    if change == "source":
        arguments[0] = "f" * 40
    elif change == "expired":
        payload = json.loads(prepared.selection_path.read_text())
        payload["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        prepared.selection_path.write_text(json.dumps(payload))
        monkeypatch.setenv("L5_FIXTURE_DIGEST", sha256(prepared.selection_path.read_bytes()).hexdigest())
    elif change == "public_fixture":
        prepared.selection_path.chmod(0o644)
    elif change == "public_directory":
        prepared.selection_path.parent.chmod(0o755)
    elif change == "symlink":
        link = tmp_path / "linked"
        link.symlink_to(prepared.selection_path.parent, target_is_directory=True)
        arguments[2] = str(link)
    elif change == "checkout":
        arguments[2] = str(ROOT)
    assert entry_point(monkeypatch, "qualify-well-architected-release.py", ["preflight", *arguments]) == 1
    assert not prepared.native_calls and not prepared.paths


def test_export_refuses_stale_private_release_before_any_external_read(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    selection = directory / "selection.json"
    release = directory / "release.json"
    output = directory / "observations.json"
    source_sha = "a" * 40
    selection.write_text(json.dumps({"source_sha": source_sha, "expires_at": (
        datetime.now(UTC) + timedelta(hours=1)).isoformat(), "cases": []}))
    release.write_text("{}")
    selection.chmod(0o600)
    release.chmod(0o600)
    monkeypatch.setattr(DRIVER.InstalledFixture, "from_manifest", lambda path: SimpleNamespace(
        source_revision="b" * 40))
    monkeypatch.setattr(DRIVER, "qualify_cases", lambda *args, **kwargs: pytest.fail("no target reads"))
    assert DRIVER.main(["--selection", str(selection), "--release", str(release),
                        "--source-sha", source_sha, "--output", str(output)]) == 1
    assert not output.exists()


def test_release_export_writes_private_schema_without_provider_calls(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    source_sha = "a" * 40
    selection = directory / "selection.json"
    release = directory / "release.json"
    output = directory / "observations.json"
    selection.write_text(json.dumps({"source_sha": source_sha, "expires_at": (
        datetime.now(UTC) + timedelta(hours=1)).isoformat(), "cases": ["first", "second"]}))
    release.write_text("{}")
    selection.chmod(0o600)
    release.chmod(0o600)
    monkeypatch.setattr(DRIVER.InstalledFixture, "from_manifest", lambda path: SimpleNamespace(
        source_revision=source_sha, account_id="000000000000",
        queue_arn="arn:aws:sqs:us-east-1:000000000000:adp-dev-l5-work"))
    monkeypatch.setattr(DRIVER, "qualify_cases", lambda selected, fixture, **kwargs: cases())
    assert DRIVER.main(["--selection", str(selection), "--release", str(release),
                        "--source-sha", source_sha, "--output", str(output)]) == 0
    content = json.loads(output.read_text())
    assert content["source_sha"] == source_sha
    assert content["provider_coverage"] == "provider_coverage_unverified"
    assert len(content["cases"]) == 2
    assert output.stat().st_mode & 0o077 == 0
    assert "000000000000" not in capsys.readouterr().out
    assert DRIVER.main(["--selection", str(selection), "--release", str(release),
                        "--source-sha", source_sha, "--output", str(output)]) == 1
