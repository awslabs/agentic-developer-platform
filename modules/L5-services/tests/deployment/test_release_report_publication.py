"""The real delivery parser and evaluator consume only bounded observed metadata."""

import base64
import copy
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from test_shared_release import environment as release_environment, context, SOURCE, DEFINITION

import test_worker_lifecycle as workers

from src.orchestration.delivery_contract import (
    DeliveryContract, WorkflowDeliveryEvidence, verify_delivery_evidence,
)
from src.orchestration.repository_evaluation_contract import Predicate, predicate_passes


ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "modules/L5-services/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("publish_release_reports", SCRIPTS / "publish-release-reports.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
SIGNED_SPEC = importlib.util.spec_from_file_location(
    "signed_release_publisher", ROOT / "modules/gateway/scripts/publish-deployment-evidence.py")
signed_publisher = importlib.util.module_from_spec(SIGNED_SPEC)
SIGNED_SPEC.loader.exec_module(signed_publisher)
NOW = datetime.now(UTC)
TARGET = {"connection_id": "selected", "account_id": workers.ACCOUNT, "region": "us-east-1",
          "resource_kind": "eks-namespace", "resource_id": "example-cluster/example-namespace"}



@pytest.fixture(name="environment")
def selected_environment():
    return release_environment.__wrapped__()


def native_report(identity):
    report = {"schema_version": 1, **{key: identity[key] for key in (
        "source_revision", "definition_revision", "run_id", "run_attempt", "contract_digest")},
        "correlation_id": identity["correlation"], "workflow_path": publisher.WORKFLOW,
        "requirements_revision": "reviewed-contract", "unresolved": [], "cleanup": {"outcome": "passed"},
        "native_observation": {"answer": "PRIVATE-ANSWER", "token": "PRIVATE-TOKEN"}}
    for section in publisher.SECTIONS:
        report[section] = [{"id": "native-result" if section == "live_checks" else "criterion-one",
            "outcome": "passed", "evidence_verified": True, "observation": "independent-live",
            "evidence_ref": "https://native.example.invalid?token=PRIVATE-TOKEN", "answer": "PRIVATE-ANSWER"}]
    report["portability"] = {"assertions": copy.deepcopy(report["live_checks"])}
    if identity["service"] == "well_architected":
        import qualification_well_architected_acceptance as wa

        stories, report["requirements_revision"] = wa.catalog()
        ids = {"epic_criteria": [f"EC-{index:02d}" for index in range(1, 9)],
               "story_criteria": [name for name, _ in stories],
               "scenarios": [name.replace("/AC-", "/SCN-") for name, _ in stories],
               "live_checks": list(wa.LIVE_CHECKS)}
        for section, identifiers in ids.items():
            report[section] = [{**report[section][0], "id": identifier} for identifier in identifiers]
    if identity["service"] == "transform":
        import qualification_transform_acceptance as transform
        from qualification_transform_journal import digest

        catalog, revision = transform.catalog()
        receipts = {name: {"receipt_sha256": digest([name, "receipt"]), "facts_sha256": digest([name, "facts"])}
                    for name in catalog["epic_criteria"]}
        report.update(requirements_revision=revision, qualification_scope=transform.SCOPE,
                      release_context=identity, repository_id=identity["repository_id"], fixture_sha256="f" * 64,
                      observed_criteria=list(receipts), collector_receipts=receipts,
                      binding_sha256=digest({"context": identity, "fixture_sha256": "f" * 64,
                                             "requirements_revision": revision}))
        sections = {
            "epic_criteria": {name: [name] for name in catalog["epic_criteria"]},
            "story_criteria": {item["id"]: item["epics"] for item in catalog["story_criteria"]},
            "scenarios": {name: item["epics"] for item in catalog["story_criteria"] for name in item["scenarios"]},
            "live_checks": {item["id"]: item["epics"] for item in catalog["live_checks"]},
        }
        for section, identifiers in sections.items():
            report[section] = [{**report[section][0], "id": identifier,
                                "collector_receipts": [receipts[name]["receipt_sha256"] for name in dependencies],
                                "verification_layer": "installed-product-and-native",
                                "evidence_ref": "sha256:" + digest([receipts[name]["receipt_sha256"] for name in dependencies])}
                               for identifier, dependencies in identifiers.items()]
    return report


@pytest.mark.parametrize("service", ["security", "devops", "transform", "well_architected", "finops"])
def test_published_metadata_passes_real_delivery_contract_and_record_predicates(environment, service):
    import qualification_well_architected_acceptance as wa
    import qualification_transform_acceptance as transform

    contract = DeliveryContract(schema_version=2, connection_id=TARGET["connection_id"],
        resource_kind=TARGET["resource_kind"], resource_id=TARGET["resource_id"],
        definition_revision=DEFINITION, baseline_revision=SOURCE,
        required_checks=list(wa.LIVE_CHECKS) if service == "well_architected" else
            [item["id"] for item in transform.catalog()[0]["live_checks"]] if service == "transform" else ["native-result"],
        cleanup_required=True)
    environment.update(L5_SERVICE=service, ADP_CONTRACT_DIGEST=contract.digest)
    identity = context.release_identity(environment)
    raw = native_report(identity)
    report = publisher.normalized_report(raw, identity, "d" * 64)
    for section in publisher.SECTIONS:
        predicate = Predicate(criterion_id=section, pointer="/" + section, operation="records",
            expected=[row["id"] for row in raw[section]], id_field="id",
            required_fields={"outcome": ["passed"], "evidence_verified": [True]})
        assert predicate_passes(predicate, report)
    doc = publisher.delivery_evidence(report, identity, TARGET, SOURCE, NOW)
    evidence = WorkflowDeliveryEvidence.model_validate(doc)
    expected = SimpleNamespace(**{key: doc[key] for key in (
        "repository_id", "run_id", "run_attempt", "workflow_path", "workflow_revision",
        "source_revision", "account_id", "region", "resource_kind", "resource_id", "correlation")})
    receipt = verify_delivery_evidence(evidence, contract=contract, context=expected,
        component="l5-" + service.replace("_", "-"), digest="e" * 64, reference="github-artifact:example")
    assert receipt.actual_revision == SOURCE
    assert "PRIVATE-" not in json.dumps([report, doc])
    assert "required_checks" not in doc and "definition_revision" not in doc


@pytest.mark.parametrize("field,value", [("source_revision", "f" * 40), ("run_id", "1"),
    ("run_attempt", "1"), ("contract_digest", "0" * 64), ("correlation_id", "foreign")])
def test_other_attempt_reports_cannot_be_published(environment, field, value):
    identity = context.release_identity(environment)
    raw = native_report(identity)
    raw[field] = value
    with pytest.raises(ValueError, match="another release"):
        publisher.normalized_report(raw, identity, "d" * 64)


@pytest.mark.parametrize("change", ["failed", "unverified", "missing", "cleanup", "unresolved", "portability"])
def test_incomplete_native_checks_remain_incomplete(environment, change):
    environment["L5_SERVICE"] = "devops"
    identity = context.release_identity(environment)
    raw = native_report(identity)
    if change == "failed":
        raw["live_checks"][0]["outcome"] = "failed"
    elif change == "unverified":
        raw["live_checks"][0]["evidence_verified"] = False
    elif change == "missing":
        raw.pop("scenarios")
    elif change == "cleanup":
        raw["cleanup"]["outcome"] = "failed"
    elif change == "portability":
        raw.pop("portability")
    else:
        raw["unresolved"] = ["Provider returned PRIVATE-ANSWER"]
    report = publisher.normalized_report(raw, identity, "d" * 64)
    assert not publisher.complete(report)
    assert "PRIVATE-" not in json.dumps(report)
    with pytest.raises(ValueError, match="Incomplete"):
        publisher.delivery_evidence(report, identity, TARGET, SOURCE, NOW)


def test_actual_revision_cannot_be_substituted_from_the_desired_revision(environment):
    identity = context.release_identity(environment)
    report = publisher.normalized_report(native_report(identity), identity, "d" * 64)
    with pytest.raises(ValueError, match="unverified"):
        publisher.delivery_evidence(report, identity, TARGET, "f" * 40, NOW)


@pytest.mark.parametrize("change", ["missing", "duplicate", "revision", "invented"])
def test_wa_original_catalog_cannot_be_omitted_or_replaced(environment, change):
    environment["L5_SERVICE"] = "well_architected"
    identity = context.release_identity(environment)
    raw = native_report(identity)
    if change == "missing":
        raw["story_criteria"].pop()
    elif change == "duplicate":
        raw["scenarios"][-1] = raw["scenarios"][0]
    elif change == "invented":
        raw["live_checks"][0]["id"] = "invented-easy-check"
    else:
        raw["requirements_revision"] = "unknown-catalog"
    with pytest.raises(ValueError, match="catalog|identifier"):
        publisher.normalized_report(raw, identity, "d" * 64)


def test_wa_incomplete_catalog_has_source_controlled_actionable_diagnostics(environment):
    environment["L5_SERVICE"] = "well_architected"
    identity = context.release_identity(environment)
    raw = native_report(identity)
    next(row for row in raw["live_checks"] if row["id"] == "owned-cleanup")["outcome"] = "incomplete"
    raw["unresolved"] = [{"code": "owned-cleanup:not-observed", "next_action": "PRIVATE-CREDENTIAL"}]
    report = publisher.normalized_report(raw, identity, "d" * 64)
    assert report["diagnostics"] == [{"code": "owned-cleanup:not-observed", "next_action":
        "Clean only explicitly selected terminal receipt-owned resources, then independently verify absence and adopted/result retention."}]
    assert not publisher.complete(report)
    assert "PRIVATE-" not in json.dumps(report)


@pytest.mark.parametrize("service", ["security", "devops", "transform", "well_architected", "finops"])
def test_installed_revision_comes_from_manifest_bytes_and_every_owned_function(environment, monkeypatch, service):
    fake = workers.FakeAWS(service)
    fake.source = SOURCE
    digest = hashlib.sha256(fake.archive).hexdigest()
    fake.code = dict.fromkeys(fake.code, base64.b64encode(bytes.fromhex(digest)).decode())
    environment.update(L5_SERVICE=service, L5_MUTATION_ROLE_ARN=workers.ROLE,
        AWS_REGION="us-east-1", WHEEL_DIGEST=workers.WHEEL, ARCHIVE_DIGEST=digest, MANIFEST_VERSION="manifest-v1")
    monkeypatch.setattr(workers.lifecycle, "aws", fake)
    monkeypatch.setattr(publisher, "load_lifecycle", lambda: workers.lifecycle)
    identity = context.release_identity(environment)
    assert publisher.installed_revision(environment, identity, TARGET) == SOURCE
    fake.code[next(reversed(fake.code))] = "stale-code"
    with pytest.raises(ValueError, match="Installed service"):
        publisher.installed_revision(environment, identity, TARGET)
    fake.code = dict.fromkeys(fake.code, base64.b64encode(bytes.fromhex(digest)).decode())
    fake.source = "f" * 40
    with pytest.raises(ValueError, match="reviewed source"):
        publisher.installed_revision(environment, identity, TARGET)


def test_incomplete_report_stages_diagnostics_without_a_delivery_artifact(environment, tmp_path, monkeypatch):
    environment.update(RUNNER_TEMP=str(tmp_path), GITHUB_OUTPUT=str(tmp_path / "outputs"))
    identity = context.release_identity(environment)
    report = native_report(identity)
    report["scenarios"][0]["outcome"] = "failed"
    publisher.write_new(tmp_path / "l5-security-qualification/qualification.json", report)
    monkeypatch.setattr(publisher, "installed_revision", lambda *args: pytest.fail("must not qualify incomplete work"))
    with pytest.raises(ValueError, match="incomplete"):
        publisher.main(environment)
    output = tmp_path / "l5-published-reports"
    assert json.loads((output / "l5-security-qualification/qualification.json").read_text())["scenarios"][0]["outcome"] == "failed"
    assert not (output / "delivery-evidence").exists()
    assert (tmp_path / "outputs").read_text() == "service=security\nqualification=staged\n"


def limited_readback(service, identity):
    if service == "transform":
        return {"schema": "transform-release-qualification/v1", "source_revision": identity["source_revision"],
                "status": "evaluated", "live_accepted": False, "runs": [{"private": "PRIVATE-OBSERVATION"}]}
    return {"schema_version": 1, **identity, "workflow_path": publisher.WORKFLOW,
            "qualification_scope": "selected_product_native_readback", "outcome": "passed",
            "live_acceptance": "not_evaluated",
            "limitations": ["provider_source_coverage_unverified", "full_live_acceptance_not_evaluated"],
            "cases": [{"private": "PRIVATE-OBSERVATION"}]}


@pytest.mark.parametrize("service", ["well_architected", "transform"])
def test_limited_readback_stages_actionable_incomplete_diagnostics(environment, tmp_path, monkeypatch, capsys, service):
    directory = tmp_path / "private-evidence"
    environment.update(L5_SERVICE=service, RUNNER_TEMP=str(tmp_path), PRIVATE_EVIDENCE_DIR=str(directory),
                       GITHUB_OUTPUT=str(tmp_path / "outputs"))
    identity = context.release_identity(environment)
    publisher.write_new(directory / "qualification.json", limited_readback(service, identity))
    monkeypatch.setattr(publisher, "installed_revision", lambda *args: pytest.fail("incomplete readback cannot authorize delivery"))
    monkeypatch.setattr(publisher, "authorize_engine", lambda *args, **kwargs: pytest.fail("incomplete readback cannot authorize delivery"))
    with pytest.raises(ValueError, match="incomplete"):
        publisher.main(environment)
    stage = tmp_path / "l5-published-reports"
    selected = service.replace("_", "-")
    report = json.loads((stage / f"l5-{selected}-qualification/qualification.json").read_text())
    assert report["live_acceptance"] == "not_evaluated" and report["outcome"] == "incomplete"
    assert report["reported_attempt_identity_verified"] is (service == "well_architected")
    assert all(report[section] == [] for section in publisher.SECTIONS)
    assert report["cleanup"]["outcome"] == "incomplete" and not publisher.complete(report)
    assert "acceptance_catalog_not_evaluated" in report["unresolved"]
    assert all(issue["next_action"] for issue in report["diagnostics"])
    assert "PRIVATE-" not in json.dumps(report)
    assert not (stage / "delivery-evidence").exists()
    assert (tmp_path / "outputs").read_text() == f"service={selected}\nqualification=staged\n"
    printed = capsys.readouterr().out
    assert "acceptance_catalog_not_evaluated" in printed and "PRIVATE-" not in printed
    with pytest.raises(ValueError, match="Incomplete"):
        publisher.delivery_evidence(report, identity, TARGET, SOURCE, NOW)


@pytest.mark.parametrize("service", ["well_architected", "transform"])
def test_limited_readback_from_another_source_cannot_be_staged(environment, service):
    environment["L5_SERVICE"] = service
    identity = context.release_identity(environment)
    raw = limited_readback(service, identity)
    raw["source_revision"] = "f" * 40
    with pytest.raises(ValueError):
        publisher.normalized_report(raw, identity, "d" * 64)


@pytest.mark.parametrize("change", ["acceptance", "schema", "identity", "empty"])
def test_transform_legacy_diagnostic_cannot_become_accepted_evidence(environment, change):
    environment["L5_SERVICE"] = "transform"
    identity = context.release_identity(environment)
    raw = limited_readback("transform", identity)
    if change == "acceptance":
        raw["live_accepted"] = True
    elif change == "schema":
        raw["schema"] = "unknown"
    elif change == "identity":
        raw["run_id"] = "foreign-attempt"
    else:
        raw["runs"] = []
    with pytest.raises(ValueError):
        publisher.normalized_report(raw, identity, "d" * 64)


def test_inputs_and_staging_refuse_symlinks_and_existing_outputs(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    linked = tmp_path / "link.json"
    linked.symlink_to(target)
    with pytest.raises(ValueError):
        publisher.private_json(linked)
    with pytest.raises(FileExistsError):
        publisher.write_new(target, {})
    target.chmod(0o644)
    with pytest.raises(ValueError):
        publisher.private_json(target)


def test_workflow_uploads_only_staged_files_from_this_attempt():
    workflow = yaml.safe_load((ROOT / ".github/workflows/l5-services-release.yml").read_text())
    assert workflow["env"]["ADP_DELIVERY_EVIDENCE_STORE"] == "s3-oidc-v1"
    for job in ("qualify-security", "qualify-selected"):
        steps = workflow["jobs"][job]["steps"]
        assert not any(step.get("uses", "").startswith("actions/upload-artifact@") for step in steps)
        metadata = next(step for step in steps if step.get("name") == "Publish qualification metadata")
        assert metadata["uses"] == "./.github/actions/upload-ci-evidence-s3"
        assert metadata["if"] == "always() && steps.reports.outputs.qualification == 'staged'"
        assert metadata["with"]["path"].startswith("${{ runner.temp }}/l5-published-reports/")
        assert "*" not in metadata["with"]["path"]
        assert metadata["with"]["path"].endswith("/qualification.json")
        assert metadata["with"]["if-no-files-found"] == "error"
        assert metadata["with"]["retention-days"] == 14
        assert not metadata.get("continue-on-error")
        delivery = next(step for step in steps if step.get("name") == "Publish verified delivery contract")
        uploads = [step for step in steps if step.get("uses") == "./.github/actions/upload-ci-evidence-s3"
                   or "publish-deployment-evidence.py" in step.get("run", "")]
        assert uploads == [metadata, delivery]
        assert delivery["if"] == "always() && steps.reports.outputs.delivery == 'staged'"
        assert "uses" not in delivery and not delivery.get("continue-on-error")
        assert delivery["env"] == {"ADP_WORKFLOW_REVISION": "${{ github.workflow_sha }}",
                                   "ENVIRONMENT": "${{ inputs.environment }}"}
        assert 'ACCOUNT_ID="$(cut -d: -f5 <<< "$L5_MUTATION_ROLE_ARN")"\nexport ACCOUNT_ID' in delivery["run"]
        assert "python3 modules/gateway/scripts/publish-deployment-evidence.py delivery delivery-evidence" in delivery["run"]
        assert '"$RUNNER_TEMP/l5-published-reports/delivery-evidence/delivery-evidence.json"' in delivery["run"]
        assert "*" not in delivery["run"]


@pytest.mark.parametrize("service", ["security", "devops", "transform", "well_architected", "finops"])
def test_delivery_receipt_uses_shared_signed_s3_publisher(environment, tmp_path, service):
    environment.update(L5_SERVICE=service, ACCOUNT_ID=TARGET["account_id"],
                       ENVIRONMENT="dev", ADP_WORKFLOW_REVISION=DEFINITION)
    identity = context.release_identity(environment)
    report = publisher.normalized_report(native_report(identity), identity, "d" * 64)
    document = publisher.delivery_evidence(report, identity, TARGET, SOURCE, NOW)
    path = tmp_path / "delivery-evidence.json"
    publisher.write_new(path, document)
    stored = {}
    audiences = []

    def aws(arguments):
        if arguments == ["sts", "get-caller-identity"]:
            return {"Account": TARGET["account_id"]}
        assert arguments[:2] == ["s3api", "put-object"]
        assert arguments[arguments.index("--expected-bucket-owner") + 1] == TARGET["account_id"]
        assert arguments[arguments.index("--if-none-match") + 1] == "*"
        stored["envelope"] = json.loads(Path(arguments[arguments.index("--body") + 1]).read_bytes())
        return {"VersionId": "signed-version"}

    def request_token(audience):
        audiences.append(audience)
        return "synthetic-signature"

    receipt = signed_publisher.publish(environment, path, "delivery", "delivery-evidence",
        aws=aws, request_token=request_token)
    assert receipt["version_id"] == "signed-version"
    assert stored["envelope"]["transport"] == "s3-oidc-v1"
    assert stored["envelope"]["signature"] == "synthetic-signature"
    payload = base64.b64decode(stored["envelope"]["payload"], validate=True)
    assert audiences == [signed_publisher.audience(payload)]
    assert WorkflowDeliveryEvidence.model_validate_json(payload).model_dump(mode="json") == document
    assert "PRIVATE-" not in payload.decode()
