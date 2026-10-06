"""Cleanup preview wiring using synthetic ownership and the maintained request compiler."""

import json
import shlex
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_demo1_journey as journey_fixtures
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest
from test_demo1_browser import identity, native_proof, native_responses

from superplane_acceptance import demo1_cli
from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_retirement import validate_access_review
from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.retirement_access_authority import (
    access_request,
    request_revision,
)
from workspace_provisioning.retirement_access_plan import PHASE, access_identity
from workspace_provisioning.retirement_managed_access import ManagedRetirementAccessPlan
from workspace_provisioning.tests.test_lifecycle_policy import policy

driver = journey_fixtures.driver


def review_document(selected, original, source):
    deployment = policy()
    deployment["permitted_target_accounts"] = [selected.account]
    deployment["permitted_regions"] = [selected.region]
    deployment["credential_references"] = {
        selected.account: next(iter(deployment["credential_references"].values()))
    }
    deployment["operation_max_runtime_seconds"] = 300
    request_id, allocation = access_identity(
        selected.org_id,
        original.workspace_id,
        "original-allocation",
        original.retirement_request_id,
    )
    cluster = (
        f"arn:aws:eks:{selected.region}:{selected.account}:cluster/example-workspace"
    )
    plan = ManagedRetirementAccessPlan(
        request_id=request_id,
        allocation_id=allocation,
        original_allocation_id="original-allocation",
        retirement_request_id=original.retirement_request_id,
        org_id=selected.org_id,
        workspace_id=original.workspace_id,
        cluster_arn=cluster,
        namespace_uid="example-namespace",
        inventory_sha256="a" * 64,
        runtime_config_sha256=digest(deployment["runtime"]),
        generation="b" * 64,
        registrar_namespaces=(),
        owned_objects=(),
        revocation_order=("cleaner-entry",),
        grants=(
            {
                "key": "cleaner-entry",
                "kind": "eks-entry",
                "principal_arn": f"arn:aws:iam::{selected.account}:role/ExampleCleanup",
                "groups": ["example-cleaner"],
                "username": "example-cleaner",
                "client_token": "c" * 64,
            },
        ),
        retained_grants=({"fixture": "synthetic retained grant"},),
        cleanup_group="example-cleaner",
        bootstrap_artifact_id="d" * 64,
        fence_recipe={
            "activate-retirement-fence": {
                "service": "kubernetes",
                "method": "patch_validating_admission_policy",
                "account_id": selected.account,
                "arguments": {
                    "cluster_arn": cluster,
                    "name": "example-fence",
                    "policy_uid": "example-policy",
                    "binding_uid": "example-binding",
                    "generation": "example-generation",
                    "active_spec_sha256": "e" * 64,
                },
            }
        },
    )
    bootstrap = OperationRequest(
        "provision",
        identity(32),
        {
            "allocation_id": "bootstrap-allocation",
            "lifecycle_phase": "bootstrap-workspace",
            "lifecycle_source_operation_id": identity(13),
            "lifecycle_artifact_id": plan.bootstrap_artifact_id,
            "lifecycle_request": canonical(
                {
                    "mode": "existing-account-managed",
                    "region": selected.region,
                    "target_account_id": selected.account,
                    "workspace_id": original.workspace_id,
                }
            ),
            "lifecycle_inputs": canonical({"isolation_mode": "dedicated"}),
            "aws_account_id": selected.account,
        },
    )
    record = SimpleNamespace(
        state="succeeded",
        operation_id=source,
        org_id=selected.org_id,
        workspace_id=original.workspace_id,
        job_id=identity(71),
        attempt_id=identity(72),
        plan_digest=payload_digest(bootstrap),
        request_payload=encode_payload(bootstrap),
    )
    paid = SimpleNamespace(
        state="succeeded",
        operation_id=identity(13),
        org_id=selected.org_id,
        workspace_id=original.workspace_id,
        admitted_request=lambda: OperationRequest(
            "provision",
            identity(31),
            {
                "allocation_id": "original-allocation",
                "lifecycle_phase": "apply-infrastructure",
            },
        ),
    )
    request = access_request(
        plan, record, deployment, allocation_source=paid, prepare_destroy=True
    )
    return json.loads(
        json.dumps(
            {
                "retirement_request_id": original.retirement_request_id,
                "request_id": request_id,
                "workspace_id": original.workspace_id,
                "phase": PHASE,
                "revision": payload_digest(request),
                "source_operation_id": source,
                "allocation_id": allocation,
                "original_allocation_id": plan.original_allocation_id,
                "inventory_sha256": plan.inventory_sha256,
                "access_plan": asdict(plan),
                "authority": {},
                "preserved": [],
                "max_resource_units": 0,
                "max_cost_micros": 0,
                "admission_available": True,
                "approval_request": {
                    "workspace_id": original.workspace_id,
                    "action": "provision",
                    "idempotency_key": request_id,
                    "parameters": dict(request.parameters),
                },
            }
        )
    )


@pytest.fixture
def prepared(driver, monkeypatch):
    driver.run()
    driver.page.service.approved = True
    driver.run()
    checkpoint = driver.path / "checkpoint.json"
    saved = checkpoint.read_bytes()
    original = SimpleNamespace(**json.loads(saved)["checkpoint"])
    proof = native_proof(driver.selected)
    native_responses(driver.selected, driver.page.service, monkeypatch, proof)
    request = driver.page.service.request
    document = review_document(driver.selected, original, identity(14))

    def serve(method, path, body=None):
        if path.endswith("/retirement/access/preview"):
            driver.page.service.calls.append((method, path, body))
            assert body == {"operation_id": original.retirement_request_id}
            return 200, document
        status, response = request(method, path, body)
        if path.endswith("/workspaces/" + identity(10)):
            response["status"] = "Active"
        return status, response

    monkeypatch.setattr(driver.page.service, "request", serve)
    return SimpleNamespace(
        driver=driver, document=document, original=original, saved=saved
    )


def run_review(driver, *extra):
    report = driver.path / "retirement-review.json"
    result = demo1_cli.main(
        [
            "--mode",
            "live",
            "--review-retirement-access",
            "--private-input",
            str(driver.path / "selection.json"),
            "--authority",
            str(driver.path / "authority.json"),
            "--browser-state",
            str(driver.path / "requester-state.json"),
            "--checkpoint",
            str(driver.path / "checkpoint.json"),
            "--report",
            str(report),
            *extra,
        ]
    )
    assert result == 2
    return json.loads(report.read_text()) if report.exists() else None


def test_cli_reviews_producer_request_without_approval_admission_or_checkpoint_change(
    prepared,
):
    driver = prepared.driver
    before = len(driver.page.service.calls)
    report = run_review(driver)
    review = report["browser"]["retirement_access"]
    assert review["review_status"] == "OBSERVED" and review["status"] == "BLOCKED"
    assert not review["admission_submitted"] and not report["live_acceptance"]
    assert (driver.path / "checkpoint.json").read_bytes() == prepared.saved
    posts = [
        path
        for method, path, _ in driver.page.service.calls[before:]
        if method == "POST"
    ]
    assert posts == [
        "/api/superplane/v1/workspaces/" + identity(10) + "/retirement/access/preview"
    ]
    rendered = json.dumps(report)
    for private in (
        "example-fence",
        "example-cleaner",
        "original-allocation",
        prepared.original.retirement_request_id,
    ):
        assert private not in rendered
    assert all(
        check["status"] == "BLOCKED"
        for check in report["browser"]["lifecycle"]["checks"].values()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_id", identity(99)),
        ("request_id", identity(99)),
        ("source_operation_id", identity(99)),
        ("retirement_request_id", identity(99)),
        ("revision", "f" * 64),
        ("inventory_sha256", "f" * 64),
        ("allocation_id", "original-allocation"),
        ("admission_available", False),
        ("max_cost_micros", False),
        ("phase", "retire-workspace"),
    ],
)
def test_foreign_or_incompatible_review_is_not_observed(prepared, field, value):
    prepared.document[field] = value
    report = run_review(prepared.driver)
    assert "retirement_access" not in report.get("browser", {})
    assert "retirement review:" in report["reason"]
    assert (prepared.driver.path / "checkpoint.json").read_bytes() == prepared.saved


@pytest.mark.parametrize(
    "field,value",
    [
        ("retirement_prepare_destroy", "v2"),
        ("retirement_source_operation_id", identity(99)),
        ("max_runtime_seconds", "901"),
        ("max_resource_units", "1"),
        ("provider_account_id", "000000000000"),
        ("region", "us-west-2"),
        ("retirement_access_recipe_sha256", "f" * 64),
        ("retirement_source_job_id", ""),
        ("lifecycle_inputs", '{"isolation_mode":"shared"}'),
    ],
)
def test_self_consistent_payload_digest_cannot_override_selected_scope(
    prepared, field, value
):
    approval = prepared.document["approval_request"]
    parameters = approval["parameters"]
    parameters[field] = value
    parameters["plan_revision"] = request_revision(parameters)
    prepared.document["revision"] = payload_digest(
        OperationRequest("provision", approval["idempotency_key"], parameters)
    )
    with pytest.raises(EvidenceError):
        validate_access_review(
            prepared.document,
            prepared.driver.selected,
            prepared.driver.envelope,
            prepared.original,
            identity(14),
            datetime.now(UTC),
        )


def test_review_never_starts_creation_and_refuses_provider_combination(driver):
    assert run_review(driver) is not None
    assert driver.page.service.calls == [] and driver.producer.calls == []
    (driver.path / "retirement-review.json").unlink()
    assert run_review(driver, "--observe-provider") is None
    assert driver.page.service.calls == [] and driver.producer.calls == []


def test_documented_preview_command_is_implemented(prepared):
    document = (Path(__file__).parent / "README.md").read_text()
    command = next(
        block.split("\n```", 1)[0]
        for block in document.split("```sh\n")[1:]
        if "--review-retirement-access" in block.split("\n```", 1)[0]
    )
    arguments = shlex.split(
        command.replace("\\\n", " ").replace(
            "$DEMO1_PRIVATE_DIR", str(prepared.driver.path)
        )
    )
    assert demo1_cli.main(arguments[arguments.index("--mode") :]) == 2
    report = json.loads((prepared.driver.path / "retirement-review.json").read_text())
    assert report["browser"]["retirement_access"]["review_status"] == "OBSERVED"


@pytest.mark.parametrize("status", [403, 503, 404])
def test_denied_preview_is_not_absence_or_admission(prepared, monkeypatch, status):
    driver = prepared.driver
    request = driver.page.service.request
    before = len(driver.page.service.calls)

    def denied(method, path, body=None):
        result = request(method, path, body)
        return (
            (status, {"private": "not published"})
            if path.endswith("/retirement/access/preview")
            else result
        )

    monkeypatch.setattr(driver.page.service, "request", denied)
    report = run_review(driver)
    assert "retirement_access" not in report.get("browser", {})
    assert report["status"] == "BLOCKED" and "not published" not in json.dumps(report)
    assert (
        sum(method == "POST" for method, _, _ in driver.page.service.calls[before:])
        == 1
    )
    assert (driver.path / "checkpoint.json").read_bytes() == prepared.saved


@pytest.mark.parametrize(
    "change",
    [
        "missing_fence",
        "foreign_cluster",
        "foreign_cleaner",
        "malformed_fence",
        "missing_retained",
    ],
)
def test_incomplete_or_swapped_preparation_plan_is_refused(prepared, change):
    plan = prepared.document["access_plan"]
    if change == "missing_fence":
        plan["fence_recipe"] = None
    elif change == "foreign_cluster":
        plan["cluster_arn"] = "arn:aws:eks:us-west-2:000000000000:cluster/foreign"
    elif change == "foreign_cleaner":
        plan["grants"][0]["principal_arn"] = "arn:aws:iam::000000000000:role/Foreign"
    elif change == "malformed_fence":
        plan["fence_recipe"]["activate-retirement-fence"]["arguments"] = []
    elif change == "missing_retained":
        plan["retained_grants"] = []
    report = run_review(prepared.driver)
    assert "retirement_access" not in report.get("browser", {})
    assert "retirement review:" in report["reason"]


def test_workspace_change_after_preview_refuses_observation(prepared, monkeypatch):
    driver = prepared.driver
    request = driver.page.service.request
    previewed = False

    def changed(method, path, body=None):
        nonlocal previewed
        status, body = request(method, path, body)
        if path.endswith("/retirement/access/preview"):
            previewed = True
        elif previewed and path.endswith("/workspaces/" + identity(10)):
            body["provisioning_operation_id"] = identity(99)
        return status, body

    monkeypatch.setattr(driver.page.service, "request", changed)
    report = run_review(driver)
    assert previewed and "retirement_access" not in report.get("browser", {})
    assert "retirement review:" in report["reason"]
    assert (driver.path / "checkpoint.json").read_bytes() == prepared.saved


def test_incomplete_bootstrap_refuses_before_preview(prepared, monkeypatch):
    driver = prepared.driver
    proof = native_proof(driver.selected)
    proof["phases"][-1]["state"] = "running"
    native_responses(driver.selected, driver.page.service, monkeypatch, proof)
    before = len(driver.page.service.calls)
    report = run_review(driver)
    assert "retirement_access" not in report.get("browser", {})
    assert all(method == "GET" for method, _, _ in driver.page.service.calls[before:])


def test_existing_report_refuses_before_any_remote_read(prepared):
    driver = prepared.driver
    existing = driver.path / "retirement-review.json"
    existing.write_text('{"retain":true}')
    before = len(driver.page.service.calls)
    assert run_review(driver) == {"retain": True}
    assert len(driver.page.service.calls) == before


def test_browser_entry_refuses_mixing_review_and_admission_before_remote_calls(driver):
    with pytest.raises(EvidenceError, match="cannot advance a continuation"):
        journey_fixtures.demo1_journey.advance_browser(
            driver.selected,
            driver.envelope,
            driver.session,
            None,
            continuation_store=object(),
            review_retirement_access=True,
        )
    assert driver.page.service.calls == [] and driver.producer.calls == []
