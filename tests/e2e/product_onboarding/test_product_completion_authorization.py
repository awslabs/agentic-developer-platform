"""Offline authorization transport checks, not a live gateway approval."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.e2e.product_onboarding import (
    product_completion_authorization as authorization,
)
from tests.e2e.product_onboarding import product_completion_context as context_publisher
from tests.e2e.product_onboarding.product_completion_regression import required_checks
from tests.e2e.product_onboarding.product_completion_regression import guard_engine


@pytest.fixture
def attempt(tmp_path):
    tmp_path.chmod(0o700)
    environment = {
        "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": "aws-e/adp/.github/workflows/product-profile-regression.yml@refs/heads/main",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW_SHA": "b" * 40,
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "b" * 40,
        "ADP_CONTRACT_DIGEST": "c" * 64,
        "ADP_CORRELATION": "orch:12345678-1234-1234-1234-123456789abc",
        "PRODUCT_ENVIRONMENT": "dev",
        "PRODUCT_SUITE": "product-profile",
        "PRODUCT_MAX_PARALLEL": "8",
        "ADP_GATEWAY_BASE_URL": "https://gateway.example.invalid",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipelines.actions.githubusercontent.com/oidc?job=1",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-request-token",
        "PRODUCT_DELIVERY_ROLE_ARN": "arn:aws:iam::000000000000:role/product-job",
        "PRODUCT_MUTATION_ROLE_ARN": "arn:aws:iam::000000000000:role/product-mutation",
    }
    return environment, tmp_path / "engine-authorization.json"


class Opener:
    def __init__(self, answers=None):
        self.requests = []
        self.answers = answers or [{"value": "test-oidc"}, {"authorized": True}]

    def open(self, request, timeout):
        assert timeout == 15
        self.requests.append(request)
        answer = self.answers[len(self.requests) - 1]

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self, size):
                return json.dumps(answer).encode()

        return Response()


@pytest.mark.parametrize("suffix", ["", "/", "/api", "/api/"])
def test_engine_receipt_requires_gateway_generic_contract_authorization(attempt, suffix):
    environment, receipt = attempt
    environment["ADP_GATEWAY_BASE_URL"] += suffix
    opener = Opener()
    authorization.authorize_engine(environment, receipt, opener=opener)
    guard_engine(environment, receipt)
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert len(opener.requests) == 2
    assert "audience=adp-operations-release-job-v1" in opener.requests[0].full_url
    sent = opener.requests[1]
    assert (
        sent.full_url
        == "https://gateway.example.invalid/api/orchestration/operations-release-job/check"
    )
    assert sent.get_method() == "POST"
    assert json.loads(sent.data) == {
        "correlation": environment["ADP_CORRELATION"],
        "source_revision": environment["ADP_SOURCE_REVISION"],
        "dispatch_revision": environment["ADP_DEFINITION_REVISION"],
        "workflow": "product-profile-regression.yml",
        "role_arn": environment["PRODUCT_DELIVERY_ROLE_ARN"],
        "mutation_role_arn": environment["PRODUCT_MUTATION_ROLE_ARN"],
        "contract_digest": environment["ADP_CONTRACT_DIGEST"],
    }
    from src.orchestration.operations_release_job import NativeJobRequest

    NativeJobRequest.model_validate(json.loads(sent.data))
    assert sent.get_header("Authorization") == "Bearer test-oidc"


@pytest.mark.parametrize(
    "field,value",
    [
        ("ADP_GATEWAY_BASE_URL", "http://gateway.example.invalid"),
        ("ACTIONS_ID_TOKEN_REQUEST_URL", "https://notgithub.example.invalid/oidc"),
        ("PRODUCT_MUTATION_ROLE_ARN", "arn:aws:iam::111111111111:role/other"),
        ("ADP_CORRELATION", "d" * 64),
        (
            "GITHUB_WORKFLOW_REF",
            "foreign/project/.github/workflows/product-profile-regression.yml@refs/heads/main",
        ),
        ("ADP_SOURCE_REVISION", "0" * 40),
    ],
)
def test_invalid_engine_identity_never_requests_oidc(attempt, field, value):
    environment, receipt = attempt
    environment[field] = value
    opener = Opener()
    with pytest.raises(ValueError):
        authorization.authorize_engine(environment, receipt, opener=opener)
    assert not opener.requests
    assert not receipt.exists()


@pytest.mark.parametrize(
    "answers",
    [
        [{"value": ""}],
        [{"value": "bad token"}],
        [{"value": "test-oidc"}, {"authorized": False}],
        [{"value": "test-oidc"}, {"authorized": True, "extra": True}],
    ],
)
def test_unverified_gateway_or_oidc_response_never_writes_receipt(attempt, answers):
    environment, receipt = attempt
    with pytest.raises(ValueError):
        authorization.authorize_engine(environment, receipt, opener=Opener(answers))
    assert not receipt.exists()


def test_engine_workflow_mints_its_own_receipt_before_reading_it():
    workflow = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / ".github/workflows/product-profile-regression.yml"
        ).read_text()
    )
    assert workflow["permissions"]["id-token"] == "write"
    job = workflow["jobs"]["regression"]
    assert (
        job["env"]["PRODUCT_MUTATION_ROLE_ARN"]
        == "${{ vars.PRODUCT_PROFILE_MUTATION_ROLE_ARN }}"
    )
    verify = next(
        step
        for step in job["steps"]
        if step.get("name") == "Verify source and authenticated engine ownership"
    )
    assert verify["run"].index("product_completion_authorization") < verify[
        "run"
    ].index("guard_engine")


def accepted_attempt(attempt):
    from src.orchestration.delivery_contract import DeliveryContract

    environment, receipt = attempt
    contract = DeliveryContract(
        schema_version=2,
        connection_id="fictional-connection",
        resource_kind="service",
        resource_id="fictional/product",
        definition_revision=environment["ADP_DEFINITION_REVISION"],
        baseline_revision=environment["ADP_SOURCE_REVISION"],
        workflow_inputs={
            "environment": "dev",
            "suite": "product-profile",
            "max_parallel": "8",
        },
        required_checks=list(required_checks()),
        cleanup_required=True,
        job_mutation_role_arn=environment["PRODUCT_MUTATION_ROLE_ARN"],
    )
    environment["ADP_CONTRACT_DIGEST"] = contract.digest
    environment["PRODUCT_ACCEPTED_CONTRACT_JSON"] = contract.model_dump_json()
    environment["PRODUCT_TARGET_REGION"] = "us-east-1"
    authorization.authorize_engine(environment, receipt, opener=Opener())
    return environment, receipt.parent, contract


def test_authenticated_contract_publishes_private_provider_context(attempt):
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    environment, root, contract = accepted_attempt(attempt)
    context = context_publisher.publish_context(environment, root)
    assert context == WorkflowContext.model_validate_json(
        (root / "deployment-context.json").read_text()
    )
    assert context.run_id == 42 and context.run_attempt == 2
    assert context.account_id == "000000000000"
    assert context.workflow_path == ".github/workflows/product-profile-regression.yml"
    assert context.inputs == contract.inputs
    assert (root / "delivery-contract.json").read_text() == json.dumps(
        contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    for name in (
        "workflow-context.json",
        "delivery-contract.json",
        "deployment-context.json",
    ):
        assert (root / name).stat().st_mode & 0o777 == 0o600


def test_release_workflow_uses_its_own_authorization_and_context(attempt):
    environment, _ = attempt
    environment["GITHUB_WORKFLOW_REF"] = (
        "aws-e/adp/.github/workflows/product-profile-release.yml@refs/heads/main"
    )
    environment, root, _ = accepted_attempt(attempt)
    context = context_publisher.publish_context(environment, root)
    assert context.workflow_path == ".github/workflows/product-profile-release.yml"


def test_context_cli_writes_provider_artifact_inside_this_attempt(attempt):
    environment, root, _ = accepted_attempt(attempt)
    attempt_dir = root / "product-profile-regression"
    attempt_dir.mkdir(mode=0o700)
    receipt = attempt_dir / "engine-authorization.json"
    receipt.write_bytes((root / "engine-authorization.json").read_bytes())
    receipt.chmod(0o600)
    repository = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.e2e.product_onboarding.product_completion_context",
        ],
        env={
            **os.environ,
            **environment,
            "RUNNER_TEMP": str(root),
            "PYTHONPATH": f"{repository}:{repository}/modules/gateway",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (attempt_dir / "deployment-context.json").is_file()


@pytest.mark.parametrize(
    "field,value",
    [
        ("PRODUCT_ACCEPTED_CONTRACT_JSON", "{}"),
        ("PRODUCT_TARGET_REGION", "wrong region"),
        ("PRODUCT_MUTATION_ROLE_ARN", "arn:aws:iam::000000000000:role/changed"),
        ("ADP_CONTRACT_DIGEST", "0" * 64),
        ("GITHUB_RUN_ATTEMPT", "3"),
    ],
)
def test_unbound_contract_or_stale_attempt_cannot_publish_context(
    attempt, field, value
):
    environment, root, _ = accepted_attempt(attempt)
    environment[field] = value
    with pytest.raises(ValueError):
        context_publisher.publish_context(environment, root)
    for name in (
        "workflow-context.json",
        "delivery-contract.json",
        "deployment-context.json",
    ):
        assert not (root / name).exists()


def test_context_artifact_uses_provider_name_and_precedes_delivery_run():
    workflow = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / ".github/workflows/product-profile-regression.yml"
        ).read_text()
    )
    steps = workflow["jobs"]["regression"]["steps"]
    names = [step.get("name") for step in steps]
    assert (
        names.index("Refuse source outside reviewed main")
        < names.index("Install live browser and gateway dependencies")
        < names.index("Verify source and authenticated engine ownership")
    )
    assert (
        names.index("Bind the accepted contract to the authenticated workflow")
        < names.index("Retain provider-owned attempt context")
        < names.index("Execute all selected Product cases and verify delivery")
    )
    upload = next(
        step
        for step in steps
        if step.get("name") == "Retain provider-owned attempt context"
    )
    assert workflow["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert names.index("Establish trusted deployment identity") < names.index("Retain provider-owned attempt context")
    assert "publish-deployment-evidence.py context product-profile-regression.yml" in upload["run"]
    assert upload["run"].endswith('/deployment-context.json"')


def _git_pins(tmp_path, repaired_workflow):
    """Two real commits: the accepted baseline and a later merged repair."""
    repository = tmp_path / "pinned-source"
    (repository / ".github/workflows").mkdir(parents=True)
    workflow = repository / ".github/workflows/product-profile-regression.yml"

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "worker@example.invalid")
    git("config", "user.name", "Repair worker")
    workflow.write_text("name: accepted\n")
    git("add", "-A")
    git("commit", "-qm", "accepted baseline")
    baseline = git("rev-parse", "HEAD")
    workflow.write_text(repaired_workflow)
    # An unrelated change keeps the repair commit non-empty even when the
    # workflow definition itself is untouched.
    (repository / "note.txt").write_text("merged repair\n")
    git("add", "-A")
    git("commit", "-qm", "merged repair")
    return repository, baseline, git("rev-parse", "HEAD")


@pytest.fixture
def pinned_source(tmp_path, monkeypatch):
    repository, baseline, repaired = _git_pins(tmp_path, "name: repaired\n")
    monkeypatch.setattr(context_publisher, "SOURCE_ROOT", repository)
    return baseline, repaired


def _pinned_contract(environment, **overrides):
    from src.orchestration.delivery_contract import DeliveryContract

    fields = {
        "schema_version": 2,
        "connection_id": "fictional-connection",
        "resource_kind": "service",
        "resource_id": "fictional/product",
        "definition_revision": environment["ADP_DEFINITION_REVISION"],
        "baseline_revision": environment["ADP_SOURCE_REVISION"],
        "workflow_inputs": {
            "environment": "dev",
            "suite": "product-profile",
            "max_parallel": "8",
        },
        "required_checks": list(required_checks()),
        "cleanup_required": True,
        "job_mutation_role_arn": environment["PRODUCT_MUTATION_ROLE_ARN"],
    }
    return DeliveryContract(**{**fields, **overrides})


def _authenticated_pins(attempt, baseline, repaired, *, source=None, **overrides):
    """Mint the receipt the authenticated run itself would obtain."""
    environment, receipt = attempt
    source = source or repaired
    environment.update(
        ADP_SOURCE_REVISION=source,
        GITHUB_SHA=source,
        ADP_DEFINITION_REVISION=repaired,
        GITHUB_WORKFLOW_SHA=repaired,
    )
    contract = _pinned_contract(
        environment,
        **{
            "baseline_revision": baseline,
            "definition_revision": baseline,
            **overrides,
        },
    )
    environment["ADP_CONTRACT_DIGEST"] = contract.digest
    authorization.authorize_engine(environment, receipt, opener=Opener())
    return environment, receipt, contract


def test_unauthenticated_pins_still_reject_a_changed_workflow(attempt, pinned_source):
    baseline, repaired = pinned_source
    environment, _, contract = _authenticated_pins(attempt, baseline, repaired)
    with pytest.raises(ValueError, match="differs from the accepted definition"):
        context_publisher.verify_contract_pins(contract, environment)


def test_unauthenticated_pins_accept_an_unchanged_workflow(
    attempt, tmp_path, monkeypatch
):
    repository, baseline, repaired = _git_pins(tmp_path, "name: accepted\n")
    monkeypatch.setattr(context_publisher, "SOURCE_ROOT", repository)
    environment, _, contract = _authenticated_pins(attempt, baseline, repaired)
    context_publisher.verify_contract_pins(contract, environment)


def test_authenticated_receipt_accepts_the_merged_repair(attempt, pinned_source):
    baseline, repaired = pinned_source
    environment, receipt, contract = _authenticated_pins(attempt, baseline, repaired)
    assert contract.definition_revision != environment["ADP_DEFINITION_REVISION"]
    context_publisher.verify_contract_pins(
        contract, environment, authorization=receipt
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("ADP_SOURCE_REVISION", "0" * 40),
        ("ADP_DEFINITION_REVISION", "0" * 40),
        ("GITHUB_RUN_ID", "43"),
        ("GITHUB_RUN_ATTEMPT", "3"),
        ("ADP_CORRELATION", "orch:00000000-0000-0000-0000-000000000000"),
        ("ADP_CONTRACT_DIGEST", "0" * 64),
    ],
)
def test_receipt_from_another_attempt_rejects_the_pins(
    attempt, pinned_source, field, value
):
    baseline, repaired = pinned_source
    environment, receipt, contract = _authenticated_pins(attempt, baseline, repaired)
    environment[field] = value
    with pytest.raises(ValueError):
        context_publisher.verify_contract_pins(
            contract, environment, authorization=receipt
        )


def test_authorization_for_another_contract_rejects_the_pins(attempt, pinned_source):
    baseline, repaired = pinned_source
    environment, receipt, _ = _authenticated_pins(attempt, baseline, repaired)
    foreign = _pinned_contract(
        environment,
        baseline_revision=baseline,
        definition_revision=baseline,
        resource_id="fictional/other-product",
    )
    assert foreign.digest != environment["ADP_CONTRACT_DIGEST"]
    with pytest.raises(ValueError, match="belongs to another contract"):
        context_publisher.verify_contract_pins(
            foreign, environment, authorization=receipt
        )


def test_missing_receipt_rejects_the_pins(attempt, pinned_source):
    baseline, repaired = pinned_source
    environment, receipt, contract = _authenticated_pins(attempt, baseline, repaired)
    receipt.unlink()
    with pytest.raises(ValueError, match="missing or symlinked"):
        context_publisher.verify_contract_pins(
            contract, environment, authorization=receipt
        )


def test_authenticated_pins_still_require_the_accepted_baseline(
    attempt, pinned_source
):
    baseline, repaired = pinned_source
    environment, receipt, contract = _authenticated_pins(
        attempt, baseline, repaired, source=baseline, baseline_revision=repaired
    )
    with pytest.raises(ValueError, match="does not contain the accepted baseline"):
        context_publisher.verify_contract_pins(
            contract, environment, authorization=receipt
        )


def test_context_publishes_through_an_accepted_repair(attempt, pinned_source):
    baseline, repaired = pinned_source
    environment, receipt, contract = _authenticated_pins(attempt, baseline, repaired)
    environment["PRODUCT_ACCEPTED_CONTRACT_JSON"] = contract.model_dump_json()
    environment["PRODUCT_TARGET_REGION"] = "us-east-1"
    context = context_publisher.publish_context(environment, receipt.parent)
    assert contract.definition_revision == baseline
    assert context.workflow_revision == repaired
    assert context.source_revision == repaired
