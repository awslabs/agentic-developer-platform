"""Offline checks for DA19 native qualification and delivery evidence; not live acceptance."""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from types import SimpleNamespace
from datetime import UTC, datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "modules/L5-services/scripts/qualification_devops.py"
ENTRYPOINT = SCRIPT.with_name("qualification-rollup.py")
PLAN = ROOT / "docs/l5-services/devopsagnent/delivery-plan.json"
RUN_ID = "da19-qualification-run-0001"
DEPLOYMENT = "l5-devops-deployment-0001"
SOURCE_SHA = "a" * 40
DEPLOYED_AT = "2026-10-28T00:00:00+00:00"
OBSERVED_AT = "2026-10-28T01:00:00+00:00"


def rollup():
    specification = importlib.util.spec_from_file_location("qualification_rollup", SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module():
    return rollup()


@pytest.fixture(scope="module")
def contract(module):
    return module.accepted_contract()


def artifacts():
    return {name: hashlib.sha256(name.encode()).hexdigest() for name in
            ("api", "ui", "cli", "mcp", "worker")}


def release():
    return {"source_sha": SOURCE_SHA, "deployment_id": DEPLOYMENT,
            "deployed_at": DEPLOYED_AT, "artifacts": artifacts()}


def common(name):
    return {"selector": name, "status": "passed", "run_id": RUN_ID,
            "evidence_ref": f"devops-{RUN_ID}-{name}.json",
            "evidence_sha256": hashlib.sha256(name.encode()).hexdigest(),
            "observed_at": OBSERVED_AT, "readback_refs": [f"{name}-readback-1"],
            "artifacts": artifacts(), "deployment_id": DEPLOYMENT}


def native(name, count=2):
    return [{"capability": f"{name}-capability-{index}", "status": "completed",
             "native_id": f"{name}-native-{index}",
             "readback_ref": f"{name}-native-readback-{index}"} for index in range(count)]


CLEANUP = {"status": "confirmed", "owned_absent": True, "retained_unchanged": True}


def summaries(pairs, epic):
    journeys = common("journeys") | {"native_results": native("journeys", 4)}
    recovery = common("recovery") | {
        "users": ["operator-one-session", "operator-two-session"],
        "denied_provider_calls": 6, "cleanup": CLEANUP}
    context = common("context") | {
        "native_results": native("context", 1),
        "reuse": {"result_ref": "later-result", "publication_ref": "later-publication",
                  "source_ref": "later-source", "later_decision_ref": "later-decision",
                  "denial_ref": "later-denial"}}
    portability = common("portability") | {
        "native_results": native("portability", 2),
        "cross_account_role": "arn:aws:iam::222222222222:role/selected-cross-account",
        "retained_unchanged": True,
        "projects": [
            {"product": "product-alpha", "project": "project-alpha",
             "repository": "repository-alpha", "team": "team-alpha", "policy": "policy-alpha",
             "environment": "environment-alpha", "source_sha": "b" * 40, "workload": "lambda",
             "account": "222222222222"},
            {"product": "product-beta", "project": "project-beta",
             "repository": "repository-beta", "team": "team-beta", "policy": "policy-beta",
             "environment": "environment-beta", "source_sha": "c" * 40, "workload": "eks",
             "account": "333333333333"},
        ]}
    acceptance = common("acceptance") | {
        "epic_criteria": list(epic),
        "cleanup": CLEANUP,
        "live_checks": {
            name: {"status": "passed", "readback_ref": f"{name}-readback",
                   "source_sha": SOURCE_SHA, "deployment_id": DEPLOYMENT,
                   "observed_at": OBSERVED_AT}
            for name in ("sso-gateway-only-ingress", "sso-denial-before-federation")},
        "cases": [
            {"story": story, "acceptance": acceptance_id, "scenario": scenario,
             "status": "passed", "native_id": f"native-{index}",
             "scope": f"scope-{index}", "readback_ref": f"case-readback-{index}",
             "readback_sha256": hashlib.sha256(f"case-{index}".encode()).hexdigest()}
            for index, (story, acceptance_id, scenario) in enumerate(pairs)],
    }
    return {"journeys": journeys, "recovery": recovery, "context": context,
            "portability": portability, "acceptance": acceptance}


def write(tmp_path, documents, release_document=None):
    evidence = tmp_path / "private-evidence"
    evidence.mkdir(exist_ok=True)
    for name, document in documents.items():
        (evidence / f"{name}.json").write_text(json.dumps(document))
    identity = tmp_path / "release.json"
    identity.write_text(json.dumps(release() if release_document is None else release_document))
    return identity, evidence


def run(module, tmp_path, documents, release_document=None, report_only=True):
    identity, evidence = write(tmp_path, documents, release_document)
    output = tmp_path / "qualification.json"
    arguments = ["--release", str(identity), "--evidence", str(evidence), "--output", str(output)]
    if report_only:
        arguments.append("--report-only")
    code = module.main(arguments)
    return code, module.build(SimpleNamespace(release=identity, evidence=evidence))


def test_accepted_contract_is_the_exact_published_r3_set(module, contract):
    pairs, epic = contract
    plan = json.loads(PLAN.read_text())
    assert plan["requirements_revision"] == module.REVISION
    assert len(pairs) == 63
    assert len(epic) == 10
    assert len(set(pairs)) == len(pairs)


@pytest.mark.parametrize("selection", [[], ["--service", "devops"]])
def test_shared_entrypoint_preserves_devops_contract(contract, tmp_path, selection):
    identity, evidence = write(tmp_path, summaries(*contract))
    output = tmp_path / "qualification.json"
    environment = {key: value for key, value in os.environ.items() if key != "L5_SERVICE"}
    result = subprocess.run(
        [sys.executable, str(ENTRYPOINT), *selection, "--release", str(identity),
         "--evidence", str(evidence), "--output", str(output)],
        env=environment, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 1
    document = json.loads(output.read_text())
    assert document["schema_version"] == 1
    assert document["component"] == "l5-devops-qualification"
    assert len(document["story_criteria"]) == 63
    assert all(not record["evidence_verified"] for record in document["story_criteria"])


def test_complete_private_readbacks_qualify_with_published_revisions(module, contract, tmp_path,
                                                                     monkeypatch):
    pairs, epic = contract
    monkeypatch.setenv("GITHUB_RUN_ID", "40700000001")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_WORKFLOW", "L5 Security Artifact Release")
    monkeypatch.setenv("DEFINITION_SHA", "d" * 40)
    monkeypatch.setenv("TARGET_ENVIRONMENT", "adp-deploy-dev")
    code, document = run(module, tmp_path, summaries(pairs, epic), report_only=False)
    assert code == 1
    assert document["qualified"] is True and document["blockers"] == []
    assert document["schema"] == "delivery-evidence/v2"
    assert document["evidence"] == "l5-devops-qualification"
    assert document["run"]["job"] == "L5 release and live qualification"
    assert document["run"]["run_id"] == "40700000001"
    assert document["run"]["run_attempt"] == "1"
    assert document["revisions"] == {
        "source": SOURCE_SHA, "definition": "d" * 40,
        "runtime": {"deployment_id": DEPLOYMENT, "deployed_at": DEPLOYED_AT,
                    "artifacts": artifacts()}}
    assert document["criteria"]["cases"] == [list(pair) for pair in pairs]
    assert document["criteria"]["missing_cases"] == []
    assert len(document["criteria"]["case_evidence"]) == 63
    assert document["criteria"]["epic"] == list(epic)
    assert set(document["live_checks"]) == {"sso-gateway-only-ingress",
                                            "sso-denial-before-federation"}
    assert document["recovery"]["users"] == 2
    assert document["scope"]["project_count"] == 2
    assert document["scope"]["non_eks_workloads"] == 1
    assert len(document["native_results"]) == 7
    assert len(document["context_reuse"]) == 5
    assert document["cleanup"] == CLEANUP
    assert set(document["selectors"]) == set(module.SELECTORS)


def test_public_record_keeps_no_private_identity_or_address(module, contract, tmp_path):
    pairs, epic = contract
    _, document = run(module, tmp_path, summaries(pairs, epic))
    rendered = json.dumps(document)
    for secret in ("product-alpha", "project-beta", "team-alpha", "repository-beta",
                   "operator-one-session", "arn:aws:iam::222222222222:role/selected-cross-account",
                   "journeys-native-0", "case-readback-7", RUN_ID):
        assert secret not in rendered
    assert document["scope"]["cross_account_role"].startswith("sha256:")
    assert document["run"]["qualification_run_id"].startswith("sha256:")


def test_missing_selector_blocks_and_still_writes_the_record(module, contract, tmp_path):
    pairs, epic = contract
    documents = summaries(pairs, epic)
    del documents["portability"]
    code, document = run(module, tmp_path, documents, report_only=False)
    assert code == 1
    assert document["qualified"] is False
    assert "portability selector produced no run-bound summary" in document["blockers"]


@pytest.mark.parametrize("mutate,expected", [
    (lambda documents: documents["journeys"].update(status="passed", artifacts={}),
     "journeys selector observed different artifact digests than the release"),
    (lambda documents: documents["journeys"].update(deployment_id="other-deployment"),
     "journeys selector observed another deployment"),
    (lambda documents: documents["journeys"].update(readback_refs=[]),
     "journeys selector cites no independent readback"),
    (lambda documents: documents["journeys"].update(evidence_sha256="not-a-digest"),
     "journeys selector summary lacks a private evidence digest reference"),
    (lambda documents: documents["journeys"].update(observed_at="2026-10-27T00:00:00+00:00"),
     "journeys selector observation is missing, naive or older than the release"),
    (lambda documents: documents["journeys"].update(native_results=[]),
     "journeys selector reported no completed native provider results"),
    (lambda documents: documents["recovery"].update(users=["only-one-session"]),
     "two real authorized users are required for recovery qualification"),
    (lambda documents: documents["recovery"].update(denied_provider_calls=0),
     "denied launches before provider federation were not observed"),
    (lambda documents: documents["recovery"].update(cleanup={"status": "assumed"}),
     "recovery cleanup of owned POC routes was not independently confirmed"),
    (lambda documents: documents["context"].update(reuse={"result_ref": "later-result"}),
     "later Context reuse needs independent result, publication, source, "
     "later-decision and denial readbacks"),
    (lambda documents: documents["portability"].update(
        projects=documents["portability"]["projects"][:1]),
     "exactly two selected non-ADP projects are required"),
    (lambda documents: documents["portability"].update(cross_account_role=""),
     "the selected cross-account resource role is missing"),
    (lambda documents: documents["portability"].update(retained_unchanged=False),
     "retained assets were not confirmed unchanged"),
    (lambda documents: documents["acceptance"].update(live_checks={}),
     "both R3 gateway-only SSO live checks are required"),
    (lambda documents: documents["acceptance"].update(cleanup=None),
     "owned cleanup was not independently confirmed"),
    (lambda documents: documents["acceptance"].update(epic_criteria=["EC-01"]),
     "the ten epic criteria need independent current observations"),
])
def test_success_booleans_alone_cannot_qualify(module, contract, tmp_path, mutate, expected):
    pairs, epic = contract
    documents = summaries(pairs, epic)
    mutate(documents)
    code, document = run(module, tmp_path, documents, report_only=False)
    assert code == 1 and document["qualified"] is False
    assert expected in document["blockers"]


def test_both_selected_projects_must_be_distinct_and_not_adp(module, contract, tmp_path):
    pairs, epic = contract
    documents = summaries(pairs, epic)
    documents["portability"]["projects"][1]["product"] = "adp"
    documents["portability"]["projects"][1]["team"] = "team-alpha"
    _, document = run(module, tmp_path, documents)
    assert "the two selected projects need distinct non-ADP product values" in document["blockers"]
    assert "the two selected projects need distinct non-ADP team values" in document["blockers"]


def test_incomplete_cases_report_the_exact_missing_pairs(module, contract, tmp_path):
    pairs, epic = contract
    documents = summaries(pairs, epic)
    documents["acceptance"]["cases"] = documents["acceptance"]["cases"][:60]
    _, document = run(module, tmp_path, documents)
    assert "3 of 63 AC/SCN pairs have no passing native evidence" in document["blockers"]
    assert document["criteria"]["missing_cases"] == [list(pair) for pair in pairs[60:]]


def test_duplicated_case_readback_digests_are_rejected(module, contract, tmp_path):
    pairs, epic = contract
    documents = summaries(pairs, epic)
    shared = documents["acceptance"]["cases"][0]["readback_sha256"]
    for case in documents["acceptance"]["cases"]:
        case["readback_sha256"] = shared
    _, document = run(module, tmp_path, documents)
    assert "AC/SCN evidence reuses the same readback digest" in document["blockers"]


def test_release_identity_must_pin_every_artifact_digest(module, contract, tmp_path):
    pairs, epic = contract
    incomplete = release()
    incomplete["artifacts"].pop("ui")
    _, document = run(module, tmp_path, summaries(pairs, epic), release_document=incomplete)
    assert any("all five artifact digests" in blocker for blocker in document["blockers"])


def test_private_selector_file_names_are_accepted(module, contract, tmp_path):
    pairs, epic = contract
    documents = summaries(pairs, epic)
    identity, evidence = write(tmp_path, documents)
    for name in module.SELECTORS:
        source = evidence / f"{name}.json"
        source.rename(evidence / f"devops-{RUN_ID}-{name}.json")
    output = tmp_path / "named.json"
    code = module.main(["--release", str(identity), "--evidence", str(evidence),
                        "--output", str(output)])
    assert code == 1
    assert json.loads(output.read_text())["unresolved"]


def independent_observations(module, contract):
    pairs, epic = contract
    observed_at = datetime.now(UTC).isoformat()
    def observation(identifier, live=False):
        record = {"id": identifier, "outcome": "passed", "evidence_verified": True,
                  "requirement_revision": module.REVISION, "source_revision": SOURCE_SHA,
                  "definition_revision": "d" * 40, "workflow_path": ".github/workflows/l5-services-release.yml",
                  "runtime_revision": DEPLOYMENT, "run_id": "40700000001",
                  "run_attempt": "1", "verification_layer": "independent-live",
                  "evidence_ref": f"private://readback/{identifier}", "observed_at": observed_at}
        if live:
            record["observation"] = "independent-live"
            record["evidence_ref"] += "#sha256:" + hashlib.sha256(identifier.encode()).hexdigest()
        return record

    return {"schema_version": 1, "source_revision": SOURCE_SHA,
            "definition_revision": "d" * 40, "workflow_path": ".github/workflows/l5-services-release.yml",
            "qualification_run_id": RUN_ID,
            "gateway_revision": SOURCE_SHA, "run_id": "40700000001", "run_attempt": "1",
            "reader": {"identity": "independent-reader", "revision": "b" * 40},
            "target": {"connection_id": "fictional-connection", "resource_kind": "product",
                       "resource_id": "fictional-product"},
            "epic_criteria": [observation(identifier) for identifier in epic],
            "story_criteria": [observation(f"{story}/{acceptance}")
                               for story, acceptance, _ in pairs],
            "scenarios": [observation(f"{story}/{scenario}") for story, _, scenario in pairs],
            "live_checks": [observation(identifier, live=True) for identifier in module.LIVE_CHECKS],
            "portability": {"assertions": [observation(identifier)
                                            for identifier in module.PORTABILITY_ASSERTIONS]},
            "cleanup": {"outcome": "passed", "observed_at": observed_at,
                        "readback": ["private://owned-cleanup"]}}


@pytest.mark.parametrize("mode,override", [
    ("standalone-unset", None), ("standalone-empty", None), ("engine", None),
    ("engine", "CORRELATION_ID"), ("engine", "CONTRACT_DIGEST"),
    ("standalone-unset", "CORRELATION_ID"), ("standalone-unset", "CONTRACT_DIGEST"),
])
def test_emitted_report_matches_canonical_publication_identity(
        module, contract, tmp_path, monkeypatch, mode, override):
    from test_shared_release import context, environment

    specification = importlib.util.spec_from_file_location(
        "devops_report_publisher", SCRIPT.with_name("publish-release-reports.py"))
    publisher = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(publisher)
    selected = environment.__wrapped__()
    selected.update(L5_SERVICE="devops", GITHUB_RUN_ID="40700000001", GITHUB_RUN_ATTEMPT="1",
                    GITHUB_WORKFLOW_SHA="d" * 40, GITHUB_SHA="d" * 40,
                    ADP_DEFINITION_REVISION="d" * 40)
    if mode.startswith("standalone"):
        for key in context.TRANSPORT:
            selected.pop(key, None)
        selected["REVIEWED_SOURCE_SHA"] = SOURCE_SHA
    release_identity = context.release_identity(selected)
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        monkeypatch.setenv(key, selected[key])
    monkeypatch.setenv("SOURCE_SHA", SOURCE_SHA)
    monkeypatch.setenv("DEFINITION_SHA", selected["GITHUB_WORKFLOW_SHA"])
    for key, value in (("CORRELATION_ID", release_identity["correlation"]),
                       ("CONTRACT_DIGEST", release_identity["contract_digest"])):
        if mode == "standalone-unset":
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value or "")
    if override is not None:
        monkeypatch.setenv(override, "orch:00000000-0000-4000-8000-000000000002"
                           if override == "CORRELATION_ID" else "f" * 64)
    release_path, evidence = write(tmp_path, summaries(*contract))
    (evidence / "independent-observations.json").write_text(
        json.dumps(independent_observations(module, contract)))
    output = tmp_path / "qualification.json"
    assert module.main(["--release", str(release_path), "--evidence", str(evidence),
                        "--output", str(output)]) == 0
    raw = json.loads(output.read_text())
    assert raw["unresolved"] == []
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    if override is not None:
        with pytest.raises(ValueError, match="another release attempt"):
            publisher.normalized_report(raw, release_identity, digest)
        return
    published = publisher.normalized_report(raw, release_identity, digest)
    assert raw["correlation_id"] == published["correlation_id"] == release_identity["correlation"]
    assert raw["contract_digest"] == published["contract_digest"] == release_identity["contract_digest"]
    assert published["unresolved"] == []
    for section in publisher.SECTIONS:
        assert [record["id"] for record in published[section]] == [record["id"] for record in raw[section]]
        assert all(record["outcome"] == "passed" and record["evidence_verified"]
                   for record in published[section])


def test_native_report_matches_gateway_predicates(module, contract, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "modules/gateway"))
    from src.orchestration.repository_evaluation_contract import Predicate, predicate_passes

    monkeypatch.setenv("GITHUB_RUN_ID", "40700000001")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("DEFINITION_SHA", "d" * 40)
    identity, evidence = write(tmp_path, summaries(*contract))
    (evidence / "independent-observations.json").write_text(
        json.dumps(independent_observations(module, contract)))
    output = tmp_path / "qualification.json"
    assert module.main(["--release", str(identity), "--evidence", str(evidence),
                        "--output", str(output)]) == 0
    document = json.loads(output.read_text())
    pairs, epic = contract
    expected = {"epic_criteria": epic,
                "story_criteria": sorted({f"{story}/{acceptance}" for story, acceptance, _ in pairs}),
                "scenarios": sorted({f"{story}/{scenario}" for story, _, scenario in pairs}),
                "live_checks": list(module.LIVE_CHECKS),
                "portability/assertions": list(module.PORTABILITY_ASSERTIONS)}
    assert document["schema_version"] == 1 and document["unresolved"] == []
    assert document["workflow_path"] == ".github/workflows/l5-services-release.yml"
    assert document["source_revision"] == SOURCE_SHA
    assert document["definition_revision"] == "d" * 40
    assert document["run_id"] == "40700000001" and document["run_attempt"] == "1"
    for section, identifiers in expected.items():
        pointer = "/" + section
        predicate = Predicate(criterion_id=section, pointer=pointer, operation="records",
                              expected=identifiers, id_field="id",
                              required_fields={"outcome": ["passed"], "evidence_verified": [True]})
        assert predicate_passes(predicate, document), section
    delivery = json.loads((tmp_path / "delivery-evidence.json").read_text())
    assert delivery["schema_version"] == 2
    assert delivery["qualification_artifact"] == "l5-devops-qualification/qualification.json"
    assert delivery["unresolved"] == []
    for private in ("fictional-product", "fictional-connection", "private://readback",
                    "private://owned-cleanup"):
        assert private not in json.dumps(document)
        assert private not in json.dumps(delivery)


@pytest.fixture(scope="module")
def native_worker_archive(tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-devops-worker")
    package = ROOT / "modules/L5-services/scripts"
    subprocess.run(["bash", str(package / "build-package.sh"), str(directory)], check=True)
    wheel, = directory.glob("adp_l5_services-*.whl")
    worker = directory / "worker.zip"
    subprocess.run(["bash", str(package / "build-workers.sh"), str(wheel), str(worker)], check=True)
    subprocess.run(["sha256sum", "--check", "worker.zip.sha256"], cwd=directory,
                   check=True, capture_output=True)
    return worker


def test_packaged_devops_factory_loads_with_installed_resources(native_worker_archive, tmp_path):
    installed = tmp_path / "installed"
    with zipfile.ZipFile(native_worker_archive) as archive:
        archive.extractall(installed)
    environment = {**os.environ, "PYTHONPATH": str(installed),
                   "L5_EXPECTED_PACKAGE_ROOT": str(installed)}
    tests = ROOT / "modules/L5-services/tests/integration/test_runtime_devops.py"
    result = subprocess.run([sys.executable, "-m", "pytest", str(tests), "-q", "--tb=short"],
                            env=environment, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    # Pytest's zero status requires a collected, passing suite. Its cardinality
    # grows with new runtime coverage; a fixed stdout count rejects added tests.


@pytest.fixture
def native_evaluation_artifact(module, contract, native_worker_archive, tmp_path, monkeypatch):
    from src.orchestration.repository_evaluation_contract import Artifact, Predicate

    monkeypatch.setenv("GITHUB_RUN_ID", "40700000001")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("DEFINITION_SHA", "d" * 40)
    original_artifacts = artifacts
    worker_digest = hashlib.sha256(native_worker_archive.read_bytes()).hexdigest()
    monkeypatch.setattr(sys.modules[__name__], "artifacts",
                        lambda: {**original_artifacts(), "worker": worker_digest})
    identity, evidence = write(tmp_path, summaries(*contract))
    (evidence / "independent-observations.json").write_text(
        json.dumps(independent_observations(module, contract)))
    output = tmp_path / "qualification.json"
    assert module.main(["--release", str(identity), "--evidence", str(evidence),
                        "--output", str(output)]) == 0
    delivery = output.with_name("delivery-evidence.json")
    report = json.loads(output.read_text())
    assert report["release"]["artifacts"]["worker"] == worker_digest

    plan = json.loads(PLAN.read_text())
    pairs = [(story["alias"], acceptance, scenario) for story in plan["stories"]
             for acceptance, scenario in zip(story["acceptance_ids"], story["scenario_ids"], strict=True)]
    rows = {
        "epic_criteria": sorted({criterion for story in plan["stories"] for criterion in story["epic_criteria"]}),
        "story_criteria": sorted({f"{story}/{acceptance}" for story, acceptance, _ in pairs}),
        "scenarios": sorted({f"{story}/{scenario}" for story, _, scenario in pairs}),
        "live_checks": list(module.LIVE_CHECKS),
        "portability/assertions": list(module.PORTABILITY_ASSERTIONS),
    }
    required = {"outcome": ["passed"], "evidence_verified": [True],
                "source_revision": [SOURCE_SHA], "run_id": ["40700000001"],
                "run_attempt": ["1"], "runtime_revision": [DEPLOYMENT],
                "requirement_revision": [module.REVISION], "verification_layer": ["independent-live"]}
    predicates = [Predicate(criterion_id="version", pointer="/schema_version", operation="equals", expected=1)]
    predicates.extend(Predicate(criterion_id=section, pointer="/" + section, operation="records",
                                expected=identifiers, id_field="id",
                                required_fields=required | ({"observation": ["independent-live"]}
                                                           if section == "live_checks" else {}))
                      for section, identifiers in rows.items())
    predicates.extend([
        Predicate(criterion_id="worker", pointer="/release/artifacts/worker", operation="equals", expected=worker_digest),
        Predicate(criterion_id="cleanup", pointer="/cleanup/outcome", operation="equals", expected="passed"),
        Predicate(criterion_id="unresolved", pointer="/unresolved", operation="equals", expected=[]),
    ])
    expected = [
        Artifact(name="l5-devops-qualification", path="qualification.json",
                 bind_workflow_provenance=True, predicates=predicates),
        Artifact(name="delivery-evidence", path="delivery-evidence.json", bind_workflow_provenance=True,
                 predicates=[Predicate(criterion_id="delivery-version", pointer="/schema_version",
                                       operation="equals", expected=2),
                             Predicate(criterion_id="qualification-link", pointer="/qualification_artifact",
                                       operation="equals", expected="l5-devops-qualification/qualification.json"),
                             Predicate(criterion_id="delivery-unresolved", pointer="/unresolved",
                                       operation="equals", expected=[])]),
    ]
    return SimpleNamespace(files={"qualification.json": output.read_bytes(),
                                  "delivery-evidence.json": delivery.read_bytes()}, expected=expected)


def evaluate_native_artifact(files, expected, *, recorded_digest=None):
    from src.orchestration.repository_evaluation_provider import (
        artifact_predicate_outcomes, parse_document, require, verified_archive,
    )

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        for name, payload in files.items():
            output.writestr(name, payload)
    content = archive.getvalue()
    digest = recorded_digest or hashlib.sha256(content).hexdigest()
    _, unpacked = verified_archive({"digest": "sha256:" + digest}, content)
    outcomes = []
    for artifact in expected:
        require(artifact.path in unpacked, "artifact_file_missing")
        document = parse_document(unpacked[artifact.path])
        outcomes.extend(artifact_predicate_outcomes(artifact, document, SOURCE_SHA, "d" * 40,
                                                    "40700000001", "1"))
    return outcomes


def test_built_worker_and_emitted_reports_pass_native_evaluator(native_evaluation_artifact):
    fixture = native_evaluation_artifact
    outcomes = evaluate_native_artifact(fixture.files, fixture.expected)
    assert outcomes and all(item["passed"] for item in outcomes)


@pytest.mark.parametrize("change", ["archive-digest", "duplicate-json", "missing-report",
                                    "wrong-source", "wrong-run", "wrong-worker", "missing-record",
                                    "wrong-readback", "cleanup-failed", "wrong-delivery-link"])
def test_native_evaluator_rejects_tampered_devops_artifacts(native_evaluation_artifact, change):
    from src.orchestration.review_cycle import CycleBlockedError

    fixture = native_evaluation_artifact
    files = dict(fixture.files)
    document = json.loads(files["qualification.json"])
    delivery = json.loads(files["delivery-evidence.json"])
    if change == "archive-digest":
        recorded_digest = "0" * 64
    elif change == "duplicate-json":
        files["qualification.json"] = b'{"schema_version":1,' + files["qualification.json"][1:]
    elif change == "missing-report":
        files.pop("qualification.json")
    elif change == "wrong-source":
        document["epic_criteria"][0]["source_revision"] = "f" * 40
    elif change == "wrong-run":
        document["run_id"] = "unrelated-run"
    elif change == "wrong-worker":
        document["release"]["artifacts"]["worker"] = "f" * 64
    elif change == "missing-record":
        document["story_criteria"].pop()
    elif change == "wrong-readback":
        document["live_checks"][0]["evidence_verified"] = False
    elif change == "cleanup-failed":
        document["cleanup"]["outcome"] = "failed"
    else:
        delivery["qualification_artifact"] = "unrelated/qualification.json"
    if change not in {"duplicate-json", "missing-report"}:
        files["qualification.json"] = json.dumps(document).encode()
    files["delivery-evidence.json"] = json.dumps(delivery).encode()
    if change in {"archive-digest", "duplicate-json", "missing-report", "wrong-run"}:
        with pytest.raises((CycleBlockedError, ValueError), match={
            "archive-digest": "artifact_digest", "duplicate-json": "duplicate_json_key",
            "missing-report": "artifact_file_missing", "wrong-run": "artifact_workflow_provenance_mismatch",
        }[change]):
            evaluate_native_artifact(files, fixture.expected,
                                     recorded_digest=recorded_digest if change == "archive-digest" else None)
    else:
        assert not all(item["passed"] for item in evaluate_native_artifact(files, fixture.expected))


@pytest.mark.parametrize("change", ["missing", "wrong-source", "unverified",
                                    "wrong-runtime", "source-as-runtime", "wrong-requirement", "unbound-evidence",
                                    "wrong-qualification-run", "missing-portability",
                                    "self-reported", "selector-as-readback", "stale", "wrong-attempt",
                                    "wrong-ingress", "wrong-gateway", "cleanup-failed", "missing-reader",
                                    "wrong-definition", "wrong-workflow", "record-definition", "record-workflow"])
def test_native_report_cannot_claim_missing_or_unverified_observations(
        module, contract, tmp_path, monkeypatch, change):
    monkeypatch.syspath_prepend(str(ROOT / "modules/gateway"))
    from src.orchestration.repository_evaluation_contract import Predicate, predicate_passes

    monkeypatch.setenv("GITHUB_RUN_ID", "40700000001")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("DEFINITION_SHA", "d" * 40)
    identity, evidence = write(tmp_path, summaries(*contract))
    observations = independent_observations(module, contract)
    if change == "missing":
        observations["scenarios"].pop()
    elif change == "wrong-source":
        observations["story_criteria"][0]["source_revision"] = "f" * 40
    elif change == "wrong-definition":
        observations["definition_revision"] = "f" * 40
    elif change == "wrong-workflow":
        observations["workflow_path"] = ".github/workflows/unrelated.yml"
    elif change == "record-definition":
        observations["story_criteria"][0]["definition_revision"] = "f" * 40
    elif change == "record-workflow":
        observations["story_criteria"][0]["workflow_path"] = ".github/workflows/unrelated.yml"
    elif change == "unverified":
        observations["epic_criteria"][0]["evidence_verified"] = False
    elif change == "wrong-runtime":
        observations["live_checks"][0]["runtime_revision"] = "foreign-deployment"
    elif change == "source-as-runtime":
        observations["live_checks"][0]["runtime_revision"] = SOURCE_SHA
    elif change == "wrong-requirement":
        observations["live_checks"][0]["requirement_revision"] = "unrelated-requirements"
    elif change == "unbound-evidence":
        observations["live_checks"][0]["evidence_ref"] = "private://readback/missing-digest"
    elif change == "wrong-qualification-run":
        observations["qualification_run_id"] = "unrelated-run"
    elif change == "missing-portability":
        observations["portability"]["assertions"].pop()
    elif change == "self-reported":
        observations["scenarios"][0]["verification_layer"] = "selector-summary"
    elif change == "selector-as-readback":
        observations["portability"]["assertions"][0]["evidence_ref"] = summaries(*contract)[
            "acceptance"]["evidence_ref"]
    elif change == "stale":
        observations["epic_criteria"][0]["observed_at"] = "2026-10-27T00:00:00+00:00"
    elif change == "wrong-attempt":
        observations["scenarios"][0]["run_attempt"] = "2"
    elif change == "wrong-ingress":
        observations["live_checks"][0]["observation"] = "self-reported"
    elif change == "wrong-gateway":
        observations["gateway_revision"] = "f" * 40
    elif change == "cleanup-failed":
        observations["cleanup"]["outcome"] = "failed"
    else:
        observations["reader"] = {}
    (evidence / "independent-observations.json").write_text(json.dumps(observations))
    output = tmp_path / "qualification.json"
    assert module.main(["--release", str(identity), "--evidence", str(evidence),
                        "--output", str(output)]) == 1
    document = json.loads(output.read_text())
    assert document["unresolved"]
    assert not predicate_passes(Predicate(criterion_id="qualification-unresolved",
                                          pointer="/unresolved", operation="equals", expected=[]), document)
    if change in ("missing", "wrong-source", "unverified", "wrong-runtime", "self-reported",
                  "selector-as-readback", "stale", "wrong-attempt", "wrong-ingress",
                  "source-as-runtime", "wrong-requirement", "unbound-evidence"):
        section = {"missing": "scenarios", "wrong-source": "story_criteria",
                   "unverified": "epic_criteria", "wrong-runtime": "live_checks",
                   "self-reported": "scenarios", "selector-as-readback": "portability/assertions",
                   "stale": "epic_criteria", "wrong-attempt": "scenarios",
                   "wrong-ingress": "live_checks", "source-as-runtime": "live_checks",
                   "wrong-requirement": "live_checks", "unbound-evidence": "live_checks"}[change]
        records = document["portability"]["assertions"] if section == "portability/assertions" else document[section]
        assert not records[-1 if change == "missing" else 0]["evidence_verified"]
        expected = independent_observations(module, contract)
        original = expected["portability"]["assertions"] if section == "portability/assertions" else expected[section]
        assert not predicate_passes(Predicate(
            criterion_id=section, pointer="/" + section, operation="records",
            expected=[record["id"] for record in original], id_field="id",
            required_fields={"outcome": ["passed"], "evidence_verified": [True]}), document)
