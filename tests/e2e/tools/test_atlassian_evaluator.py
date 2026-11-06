"""Generated-only summary handoff to the native repository evaluator."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from tests.e2e.tools.atlassian_evaluator import stage_summary
from tests.e2e.tools.atlassian_phase import readback
from tests.e2e.tools.test_atlassian_phase import (
    ACCOUNT,
    DEFINITION,
    PINS,
    REPOSITORY,
    SOURCE,
    retained,
)


def summary(tmp_path, **changes):
    fresh = tmp_path / "fresh"
    fresh.mkdir(exist_ok=True)
    uri, fetch, _ = retained(fresh)
    params = {
        "repository": REPOSITORY, "account": ACCOUNT, "source": SOURCE, "definition": DEFINITION,
        "run_id": "314", "attempt": "2", "uri": uri, "version": "v1",
        "verify": lambda uri, version, **pins: readback(uri, version, fetch=fetch, **pins),
    }
    return stage_summary(**(params | changes))


def test_private_readback_only_exports_unproven_rows(tmp_path):
    document = summary(tmp_path)
    assert document["criterion_rows"] == [
        {"issue_number": 8446, "criterion_id": criterion, "scenario_id": scenario,
         "controlled": "candidate", "live": "unproven"}
        for criterion, scenario in (("AC-01", "SCN-01"), ("AC-02", "SCN-02"), ("AC-03", "SCN-03"))
    ]
    assert (document["run_id"], document["run_attempt"]) == ("314", "2")
    assert document["runtime"]["verified"] is False
    assert document["cleanup"] == {"generated_verified": True, "remote_verified": False, "status": "generated_only"}
    assert document["unresolved_operations"] == []
    assert document["fixture_source"] == "generated"
    assert document["test_source_matches_release"] is True
    assert document["independent_verification"] == {
        "private_phase_readback": True, "live_provider": False,
    }
    payload = json.dumps(document)
    assert all(sensitive not in payload for sensitive in (
        ACCOUNT, "s3:", "arn:", "token", "site", "atlassian.net", "bucket",
    ))


@pytest.mark.parametrize("field,value", [
    ("repository", "other/repo"), ("account", "invalid"),
    ("source", "b" * 40), ("definition", "c" * 40),
    ("run_id", "315"), ("attempt", "3"), ("uri", "s3://other/private"),
    ("version", "v2"),
])
def test_refuses_unbound_or_missing_evidence(tmp_path, field, value):
    with pytest.raises(ValueError):
        summary(tmp_path, **{field: value})


@pytest.mark.parametrize("change", [
    "schema_version", "phase", "evidence_kind", "controlled_status", "live_status", "source_revision",
    "definition_revision", "gateway_revision", "run_id", "run_attempt",
    "scenarios", "contract_sha256", "readiness_sha256", "issue_number", "generated_cleanup",
    "cleanup_observation", "unresolved_operations",
])
def test_refuses_tampered_report(tmp_path, change):
    uri, fetch, _ = retained(tmp_path)
    report = readback(uri, "v1", fetch=fetch, repository=REPOSITORY, account=ACCOUNT, **PINS)
    report[change] = {} if change == "scenarios" else "incorrect"
    with pytest.raises(ValueError):
        summary(tmp_path, verify=lambda *_args, **_kwargs: report)


def test_summary_never_copies_unexpected_private_fields(tmp_path):
    uri, fetch, _ = retained(tmp_path)
    report = readback(uri, "v1", fetch=fetch, repository=REPOSITORY, account=ACCOUNT, **PINS)
    report["private_token"] = "secret-sentinel"
    assert "secret-sentinel" not in json.dumps(summary(tmp_path, verify=lambda *_args, **_kwargs: report))


def test_native_evaluator_contract_rejects_live_promotion(tmp_path):
    from src.orchestration.repository_evaluation_contract import (
        RepositoryEvaluationSpecification,
        predicate_passes,
    )

    amendment = json.loads((Path(__file__).resolve().parents[3] /
                            "docs/tools/atlassian-mcp/evaluator-amendment-8446.json").read_text())
    specification = RepositoryEvaluationSpecification.model_validate(amendment["evaluation"])
    assert specification.producer is None
    workflow = specification.workflows[0]
    assert workflow.path == ".github/workflows/gateway-deploy.yml"
    assert workflow.dispatch_only is False
    assert workflow.source.predecessor == workflow.definition.predecessor == specification.predecessors[0].address
    assert workflow.required_jobs == ["Build and Deploy Backend", "identity-proof"]
    assert workflow.artifacts[0].bind_workflow_provenance is True
    document = summary(tmp_path)
    original = deepcopy(document)
    predicates = workflow.artifacts[0].predicates
    assert all(predicate_passes(predicate, document) for predicate in predicates)
    document["criterion_rows"][0]["live"] = "pass"
    assert not all(predicate_passes(predicate, document) for predicate in predicates)
    document = original
    document["independent_verification"]["private_phase_readback"] = False
    assert not all(predicate_passes(predicate, document) for predicate in predicates)


@pytest.mark.parametrize("alter", [
    lambda doc: doc["criterion_rows"][0].update(issue_number=8445),
    lambda doc: doc["criterion_rows"][0].update(scenario_id="SCN-03"),
    lambda doc: doc["criterion_rows"].pop(),
    lambda doc: doc["cleanup"].update(generated_verified=False),
    lambda doc: doc["cleanup"].update(status="pending"),
    lambda doc: doc["cleanup"].update(remote_verified=True),
    lambda doc: doc["runtime"].update(verified=True),
    lambda doc: doc.update(unresolved_operations=["pending-cleanup"]),
    lambda doc: doc.update(fixture_source="provider"),
    lambda doc: doc.update(test_source_matches_release=False),
    lambda doc: doc["independent_verification"].update(live_provider=True),
])
def test_native_evaluator_rejects_false_cleanup_runtime_or_issue_qualification(tmp_path, alter):
    from src.orchestration.repository_evaluation_contract import RepositoryEvaluationSpecification, predicate_passes

    amendment = json.loads((Path(__file__).resolve().parents[3] /
                            "docs/tools/atlassian-mcp/evaluator-amendment-8446.json").read_text())
    workflow = RepositoryEvaluationSpecification.model_validate(amendment["evaluation"]).workflows[0]
    document = summary(tmp_path)
    alter(document)
    assert not all(predicate_passes(predicate, document) for predicate in workflow.artifacts[0].predicates)


def test_gateway_produces_only_bounded_summary_after_private_readback():
    workflow = yaml.load(
        (Path(__file__).resolve().parents[3] / ".github/workflows/gateway-deploy.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    job = workflow["jobs"]["identity-proof"]
    assert job["needs"] == ["deploy-backend"]
    assert "needs.deploy-backend.result == 'success'" in job["if"]
    steps = job["steps"]
    install = next(step for step in steps if step.get("name") == "Install generated-fixture test dependencies")
    assert install["run"] == "bash tests/e2e/tools/atlassian_identity_ci.sh install"
    driver = Path(__file__).with_name("atlassian_identity_ci.sh").read_text()
    assert "pytest==9.0.3 pytest-asyncio==1.4.0 PyYAML==6.0.3" in driver
    assert "-e 'modules/gateway[dev]' -e 'libs/python/adp-common[l5-consumers]' -e modules/tools -e modules/tools/facade moto==5.0.27" in driver
    assert steps.index(install) < next(index for index, step in enumerate(steps) if step.get("run") == "bash tests/e2e/tools/atlassian_identity_ci.sh run")
    private = next(step for step in steps if step.get("id") == "publish-proof")
    reader = next(step for step in steps if "atlassian_phase readback" in step.get("run", ""))
    producer = next(step for step in steps if "tests.e2e.tools.atlassian_evaluator" in step.get("run", ""))
    trusted = next(step for step in steps if step.get("uses") == "./.github/actions/trusted-deployment")
    publication = next(step for step in steps if "repository-artifact identity-proof" in step.get("run", ""))
    assert steps.index(trusted) < steps.index(private) < steps.index(reader) < steps.index(producer) < steps.index(publication)
    preserved = next(step for step in steps if step.get("name") == "Preserve runner identity for private readback")
    assert steps.index(preserved) < steps.index(trusted)
    assert '"$RUNNER_TEMP/atlassian-stage-summary.json"' in publication["run"]
    assert publication["env"]["EXPECTED_SOURCE_REVISION"] == "${{ inputs.adp_source_revision }}"
    assert publication["env"]["ADP_WORKFLOW_REVISION"] == "${{ github.workflow_sha }}"
    assert job["permissions"]["id-token"] == "write"
    assert workflow["env"]["ADP_IDENTITY_PROOF_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert not any(step.get("uses", "").startswith("actions/upload-artifact@") for step in steps)
    assert all("tools-live-evaluation" not in str(step) for step in steps)
