"""Exercise the checked-in workflows' revision guard and provider context."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from src.orchestration.deployment_workflow_provider import TRANSPORT_INPUTS, WorkflowContext

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "modules/gateway/scripts/deployment-workflow-context.py"
spec = importlib.util.spec_from_file_location("deployment_context", SCRIPT)
context_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_module)


def test_context_retains_explicit_foundation_selector(evidence):
    env, read = evidence
    env["INPUT_RELEASE_GROUP"] = "atlassian-foundation"
    assert context_module.context(env, read)["inputs"]["release_group"] == "atlassian-foundation"


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


TOOLS_PLATFORM_GUARD = "Verify source belongs to reviewed main history"


def tools_platform_source_guards():
    document = workflow("tools-platform-deploy.yml")
    return [
        (name, step)
        for name, job in document["jobs"].items()
        for step in job.get("steps", [])
        if step.get("name") in {TOOLS_PLATFORM_GUARD, "Verify reviewed source and protected Identity driver"}
    ]


def test_tools_platform_source_guards_never_depend_on_repository_credentials():
    import re

    document = workflow("tools-platform-deploy.yml")
    fetches = 0
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
                assert step["with"]["fetch-depth"] == 0
            for fetch in re.findall(r"git(?: -\S+ \S+)* fetch[^\n]*", str(step.get("run", ""))):
                # persist-credentials: false leaves no credential helper, so a
                # remote fetch dies with "could not read Username for
                # https://github.com"; only local self-fetches may remain.
                assert re.search(r"\bfetch\b[^\n]*\s\.\s", fetch), fetch
                assert " origin" not in fetch, fetch
                fetches += 1
    guards = tools_platform_source_guards()
    assert fetches == len(guards) == 4
    for _, step in guards:
        assert "git merge-base --is-ancestor HEAD FETCH_HEAD" in step["run"]
        assert "secrets." not in json.dumps(step.get("env", {}))
        assert "github.token" not in json.dumps(step.get("env", {}))


@pytest.fixture
def reviewed_history(tmp_path):
    import re

    guards = dict(tools_platform_source_guards())
    guarded = sorted(
        {
            path
            for job in ("adapter-foundation", "adapter-identity")
            for path in re.search(r"for file in (.+?); do", guards[job]["run"], re.S).group(1).split()
        }
    )
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "adp",
        "GIT_AUTHOR_EMAIL": "adp@example.com",
        "GIT_COMMITTER_NAME": "adp",
        "GIT_COMMITTER_EMAIL": "adp@example.com",
    }

    def git(*args, cwd):
        done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)
        return done.stdout.strip()

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=origin)
    for path in guarded:
        target = origin / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"reviewed {path}\n")
    git("add", "-A", cwd=origin)
    git("commit", "--quiet", "-m", "reviewed source", cwd=origin)
    source = git("rev-parse", "HEAD", cwd=origin)
    (origin / "later.md").write_text("reviewed main tip\n")
    git("add", "-A", cwd=origin)
    git("commit", "--quiet", "-m", "main tip", cwd=origin)
    dispatched = git("rev-parse", "HEAD", cwd=origin)
    git("checkout", "--quiet", "-b", "unreviewed", source, cwd=origin)
    (origin / "side.md").write_text("never reviewed\n")
    git("add", "-A", cwd=origin)
    git("commit", "--quiet", "-m", "divergent", cwd=origin)
    divergent = git("rev-parse", "HEAD", cwd=origin)
    git("checkout", "--quiet", "main", cwd=origin)

    def runner(checkout):
        work = tmp_path / f"work-{checkout[:8]}"
        git("clone", "--quiet", "--no-local", str(origin), str(work), cwd=tmp_path)
        git("checkout", "--quiet", "--detach", checkout, cwd=work)
        # The deployment job has no credential helper, so make every network
        # remote unusable: the guard must pass on local objects alone.
        git("remote", "set-url", "origin", "https://127.0.0.1:1/unreachable.git", cwd=work)
        return work

    def execute(job, work, source_revision, github_sha, definition_revision):
        return subprocess.run(
            ["/bin/bash", "-c", guards[job]["run"]],
            cwd=work,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ["PATH"],
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "ADP_SOURCE_REVISION": source_revision,
                "ADP_SELECTED_SOURCE": source_revision,
                "ADP_DEFINITION_REVISION": definition_revision,
                "GITHUB_SHA": github_sha,
            },
        )

    return runner, execute, source, dispatched, divergent, guarded


GUARDED_JOBS = ["adapter-foundation", "adapter-identity", "release", "drift"]


@pytest.mark.parametrize("job", GUARDED_JOBS)
def test_tools_platform_source_guards_prove_reviewed_history_without_reachable_remote(reviewed_history, job):
    runner, execute, source, dispatched, _, _ = reviewed_history
    result = execute(job, runner(source), source, dispatched, dispatched)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("job", GUARDED_JOBS)
@pytest.mark.parametrize(
    "variant",
    ["absent_dispatch", "unreviewed_dispatch", "divergent_source", "moved_checkout", "missing_origin_main"],
)
def test_tools_platform_source_guards_reject_unreviewed_deployment_inputs(reviewed_history, job, variant):
    runner, execute, source, dispatched, divergent, _ = reviewed_history
    checkout = source_revision = source
    github_sha = dispatched
    if variant == "absent_dispatch":
        github_sha = "d" * 40
    elif variant == "unreviewed_dispatch":
        # A locally present commit that never reached main must not qualify.
        github_sha = divergent
    elif variant == "divergent_source":
        checkout = source_revision = divergent
    elif variant == "moved_checkout":
        source_revision = dispatched
    work = runner(checkout)
    if variant == "missing_origin_main":
        subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=work, check=True)
    result = execute(job, work, source_revision, github_sha, dispatched)
    assert result.returncode != 0, result.stdout


def test_adapter_source_guard_rejects_a_guarded_file_changed_after_review(reviewed_history):
    runner, execute, source, dispatched, _, guarded = reviewed_history
    work = runner(source)
    (work / guarded[0]).write_text("mutated after review\n")
    assert execute("adapter-foundation", work, source, dispatched, dispatched).returncode != 0


@pytest.mark.parametrize(
    "name,job_name",
    [
        ("gateway-deploy.yml", "deploy-backend"),
        ("gateway-deploy.yml", "deploy-frontend"),
        ("run-gateway-migrations.yml", "migrate"),
        ("gateway-frontend-deploy.yml", "publish"),
    ],
)
def test_deployment_evidence_publishers_have_oidc_authority_and_no_github_artifact_dependency(name, job_name):
    document = workflow(name)
    assert document["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    job = document["jobs"][job_name]
    assert job["permissions"]["id-token"] == "write"
    assert not any(step.get("uses", "").startswith("actions/upload-artifact@") for step in job["steps"])
    publishers = [step for step in job["steps"] if "publish-deployment-evidence.py" in step.get("run", "") and " release " in step["run"]]
    assert len(publishers) == 1
    assert publishers[0]["env"]["ADP_WORKFLOW_REVISION"] == "${{ github.workflow_sha }}"


@pytest.mark.parametrize("name", ["gateway-deploy.yml", "run-gateway-migrations.yml", "gateway-frontend-deploy.yml"])
def test_revision_guard_runs_before_checkout_and_rejects_moved_definition(name):
    document = workflow(name)
    events = document.get("on", document.get(True))
    assert TRANSPORT_INPUTS <= events["workflow_dispatch"]["inputs"].keys()
    steps = next(iter(document["jobs"].values()))["steps"]
    guard = steps[0]
    assert guard["name"] == "Guard engine delivery revisions"
    assert steps[1]["uses"].startswith("actions/checkout@")
    for source, definition, correlation, actual, expected in [
        ("", "", "", "a" * 40, 0),
        ("b" * 40, "a" * 40, "c" * 64, "a" * 40, 0),
        ("b" * 40, "a" * 40, "c" * 64, "d" * 40, 1),
        ("main", "a" * 40, "c" * 64, "a" * 40, 1),
        ("$(echo untrusted)", "a" * 40, "c" * 64, "a" * 40, 1),
        ("", "a" * 40, "", "a" * 40, 1),
    ]:
        result = subprocess.run(
            ["/bin/bash", "-c", guard["run"]],
            env={"ADP_SOURCE": source, "ADP_DEFINITION": definition, "ADP_CORRELATION": correlation, "ACTUAL_DEFINITION": actual},
            capture_output=True,
        )
        assert (result.returncode != 0) == bool(expected), result.stderr


@pytest.mark.parametrize("name", ["gateway-deploy.yml", "run-gateway-migrations.yml", "gateway-frontend-deploy.yml"])
@pytest.mark.parametrize("variant", ["valid", "moved_definition", "missing_source", "malformed", "suffix", "shell"])
def test_gateway_revision_guard_accepts_engine_attempt_identity_only_with_pinned_revisions(name, variant):
    from src.orchestration.dispatch_pass import attempt_run_id

    correlation = attempt_run_id("fixture-deployment-node", 1)
    source, definition, actual = "b" * 40, "a" * 40, "a" * 40
    if variant == "moved_definition":
        actual = "d" * 40
    elif variant == "missing_source":
        source = ""
    elif variant == "malformed":
        correlation = "orch:not-a-uuid"
    elif variant == "suffix":
        correlation += "extra"
    elif variant == "shell":
        correlation = "$(echo untrusted)"
    guard = next(iter(workflow(name)["jobs"].values()))["steps"][0]
    result = subprocess.run(
        ["/bin/bash", "-c", guard["run"]],
        env={"ADP_SOURCE": source, "ADP_DEFINITION": definition, "ADP_CORRELATION": correlation, "ACTUAL_DEFINITION": actual},
        capture_output=True,
    )
    assert (result.returncode == 0) is (variant == "valid"), result.stderr


def test_source_revision_reaches_every_checkout_and_build_tag():
    document = workflow("gateway-deploy.yml")
    for name, job in document["jobs"].items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                expected = (
                    "${{ inputs.adp_definition_revision || github.sha }}"
                    if name == "identity-proof"
                    else "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
                )
                assert step["with"]["ref"] == expected
    backend = document["jobs"]["deploy-backend"]
    build = next(step for step in backend["steps"] if step.get("uses") == "./.github/actions/codebuild-run")
    assert build["with"]["source_revision"] == "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
    assert (
        "name=IMAGE_TAG,value=${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
        in build["with"]["environment_variables"]
    )
    # Pass the immutable revision through the reusable workflow input: GitHub
    # suppresses job outputs containing the masked AWS account ID.
    assert document["jobs"]["run-migrations"]["with"]["expected_image_digest"] == "${{ needs.deploy-backend.outputs.release_digest }}"
    assert TRANSPORT_INPUTS <= document["jobs"]["run-migrations"]["with"].keys()
    migration = workflow("run-gateway-migrations.yml")
    assert TRANSPORT_INPUTS <= migration[True]["workflow_call"]["inputs"].keys()


@pytest.fixture
def evidence():
    env = dict(
        CONTEXT_WORKFLOW=".github/workflows/gateway-deploy.yml",
        CONTEXT_SOURCE="a" * 40,
        CONTEXT_REVISION="b" * 40,
        CONTEXT_REPOSITORY_ID="123",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="1",
        ACCOUNT_ID="123456789012",
        CUSTOMER_ACCOUNT_ID="999999999999",
        AWS_REGION="us-east-1",
        ENVIRONMENT="dev",
        INPUT_ACCOUNT_ID="",
        INPUT_CUSTOMER_ACCOUNT_ID="",
        INPUT_CUSTOMER_AWS_LABEL="",
        INPUT_CUSTOMER_USER_ID="",
        INPUT_ENVIRONMENT="dev",
        INPUT_PRODUCT_CASES="none",
        INPUT_PROOF_PHASE="none",
        AWS_SECRET_ACCESS_KEY="must-never-be-published",
        UNTRUSTED_INPUT="must-not-be-published",
    )

    def read(args):
        if args[0] == "sts":
            return {"Account": "123456789012"}
        return {"cluster": {"name": "adp-dev-eks-cluster", "arn": "arn:aws:eks:us-east-1:123456789012:cluster/adp-dev-eks-cluster"}}

    return env, read


def test_context_is_valid_bounded_and_contains_only_explicit_nonsecret_fields(evidence):
    env, read = evidence
    document = context_module.context(env, read)
    accepted = WorkflowContext.model_validate(document)
    assert accepted.resource_id == "adp-dev-eks-cluster/adp-gateway"
    assert "must-never" not in json.dumps(document) and "must-not" not in json.dumps(document)
    assert accepted.inputs["environment"] == "dev"


def test_context_records_selected_inventory_without_fixture_secrets(evidence):
    env, read = evidence
    selection = "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
    document = context_module.context({**env, "INPUT_PRODUCT_CASES": selection}, read)
    assert WorkflowContext.model_validate(document).inputs["product_cases"] == selection
    assert "must-never" not in json.dumps(document)


def test_gateway_context_carries_only_reviewed_proof_phase(evidence):
    env, read = evidence
    document = context_module.context({**env, "INPUT_PROOF_PHASE": "stage.identity-proof"}, read)
    assert WorkflowContext.model_validate(document).inputs["proof_phase"] == "stage.identity-proof"
    assert context_module.context(env, read)["inputs"]["proof_phase"] == "none"
    assert "must-never" not in json.dumps(document)
    steps = workflow("gateway-deploy.yml")["jobs"]["deploy-backend"]["steps"]
    record = next(step for step in steps if step.get("name") == "Record deployment workflow context")
    assert record["env"]["INPUT_PROOF_PHASE"] == "${{ inputs.proof_phase || 'none' }}"


def test_product_selection_requires_authenticated_smoke_without_changing_regular_deployments():
    deployment = workflow("gateway-deploy.yml")
    smoke = workflow("gateway-smoke.yml")
    condition = "${{ inputs.product_cases != '' && inputs.product_cases != 'none' }}"
    assert deployment["jobs"]["smoke-test"]["with"]["require_token"] == condition
    assert smoke[True]["workflow_call"]["inputs"]["require_token"]["default"] is False
    required_step = next(step for step in smoke["jobs"]["smoke"]["steps"] if step.get("name") == "Require smoke coverage for coordinated runs")
    assert required_step["if"] == "${{ inputs.require_token && !inputs.product_profile && steps.token.outputs.token_available != 'true' }}"
    assert "exit 1" in required_step["run"]
    assert deployment["jobs"]["smoke-test"]["with"]["product_profile"] == condition
    assert smoke[True]["workflow_call"]["inputs"]["product_profile"]["default"] is False
    product_step = next(step for step in smoke["jobs"]["smoke"]["steps"] if step.get("id") == "product-smoke")
    assert product_step["if"] == "inputs.product_profile"
    assert product_step["run"] == "python3 platform/scripts/product_authenticated_smoke.py"
    assert not product_step.get("continue-on-error", False)


def test_product_live_uses_protected_evaluator_without_reusing_gateway_checks_identity():
    job = workflow("gateway-live-tests.yml")["jobs"]["live"]
    selected = "inputs.product_cases != '' && inputs.product_cases != 'none'"
    ordinary = "inputs.product_cases == '' || inputs.product_cases == 'none'"
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == ("${{ (" + selected + ") && 'dev' || format('adp-checks-{0}', inputs.target_environment || 'dev') }}")
    steps = job["steps"]
    checks = next(step for step in steps if step.get("name") == "Assume gateway checks identity")
    evaluator = next(step for step in steps if step.get("name") == "Assume scoped Product evaluator identity")
    assert checks["if"] == ordinary
    assert evaluator["if"] == selected
    assert evaluator["with"]["role-to-assume"] == "${{ secrets.AWS_CLI_UPLIFT_EVAL_ROLE_ARN }}"
    assert evaluator["with"]["unset-current-credentials"] is True
    assert evaluator["with"]["role-chaining"] is False
    for name in ("Resolve reviewed live target and owned fixture", "Run live tests"):
        assert next(step for step in steps if step.get("name") == name)["if"] == ordinary
    assert next(step for step in steps if step.get("name") == "Verify complete scenario evidence")["if"] == "always() && (" + ordinary + ")"
    selected_run = next(step for step in steps if step.get("id") == "product")
    assert selected_run["if"] == selected
    assert all(
        selected_run["env"][secret] == "${{ secrets." + secret + " }}"
        for secret in ("CLI_UPLIFT_EVAL_BINDINGS_JSON", "CLI_UPLIFT_EVAL_GATEWAY_CATALOG")
    )
    assert "PRODUCT_API_FIXTURES_JSON" not in selected_run["env"]
    guard = next(step for step in steps if step.get("name") == "Require protected Product evaluator configuration")
    assert guard["env"]["LEGACY_PRODUCT_FIXTURES_PRESENT"] == "${{ secrets.PRODUCT_API_FIXTURES_JSON != '' }}"
    assert guard["env"]["PRODUCT_TARGET_ENV"] == "${{ inputs.target_environment || 'dev' }}"
    for name in ("CREDENTIAL_SECRET_NAME", "INSTANCE_PROFILE", "STATE_BUCKET"):
        assert guard["env"]["EVAL_" + name] == "${{ vars.CLI_UPLIFT_EVAL_" + name + " }}"


@pytest.mark.parametrize(
    "missing",
    [
        None,
        "EVAL_ROLE_ARN",
        "CLI_UPLIFT_EVAL_BINDINGS_JSON",
        "CLI_UPLIFT_EVAL_GATEWAY_CATALOG",
        "EVAL_CREDENTIAL_SECRET_NAME",
        "EVAL_INSTANCE_PROFILE",
        "EVAL_STATE_BUCKET",
        "EVAL_INTERNAL_INVOKE_URL",
        "EXPECTED_REVISION",
    ],
)
def test_product_evaluator_preflight_refuses_unconfigured_secrets_or_source(missing):
    steps = workflow("gateway-live-tests.yml")["jobs"]["live"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Require protected Product evaluator configuration")
    assert guard["if"] == "inputs.product_cases != '' && inputs.product_cases != 'none'"
    env = {
        "EVAL_ROLE_ARN": "arn:aws:iam::000000000101:role/adp-cli-uplift-eval-orchestrator",
        "CLI_UPLIFT_EVAL_BINDINGS_JSON": "private-binding",
        "CLI_UPLIFT_EVAL_GATEWAY_CATALOG": "private-catalog",
        "EVAL_CREDENTIAL_SECRET_NAME": "example/test-admin-credentials",
        "EVAL_INSTANCE_PROFILE": "example-evaluator-instance",
        "EVAL_STATE_BUCKET": "example-evaluator-state",
        "EVAL_INTERNAL_INVOKE_URL": "https://example.execute-api.us-east-1.amazonaws.com/dev",
        "LEGACY_PRODUCT_FIXTURES_PRESENT": "false",
        "EXPECTED_REVISION": "a" * 40,
        "PRODUCT_TARGET_ENV": "dev",
    }
    if missing:
        env[missing] = ""
    result = subprocess.run(["bash", "-c", guard["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (missing is None)
    assert not any(value in result.stdout + result.stderr for value in ("private-binding", "private-catalog", "example/test-admin-credentials"))
    if missing is None:
        env["PRODUCT_TARGET_ENV"] = "other"
        wrong_target = subprocess.run(["bash", "-c", guard["run"]], env=env, capture_output=True, text=True)
        assert wrong_target.returncode != 0
        assert "limited to the reviewed dev environment" in wrong_target.stdout
        env["PRODUCT_TARGET_ENV"] = "dev"
        env["LEGACY_PRODUCT_FIXTURES_PRESENT"] = "true"
        legacy = subprocess.run(["bash", "-c", guard["run"]], env=env, capture_output=True, text=True)
        assert legacy.returncode != 0
        assert "Static Product actor tokens" in legacy.stdout
        assert "example/test-admin-credentials" not in legacy.stdout + legacy.stderr


@pytest.mark.parametrize(
    "role,caller,allowed",
    [
        (
            "arn:aws:iam::000000000101:role/adp-cli-uplift-eval-orchestrator",
            "arn:aws:sts::000000000101:assumed-role/adp-cli-uplift-eval-orchestrator/run-1",
            True,
        ),
        (
            "arn:aws:iam::000000000101:role/adp-cli-uplift-eval-orchestrator",
            "arn:aws:sts::000000000102:assumed-role/adp-cli-uplift-eval-orchestrator/run-1",
            False,
        ),
        ("arn:aws:iam::000000000101:role/adp-cli-uplift-eval-orchestrator", "arn:aws:sts::000000000101:assumed-role/other-role/run-1", False),
        ("arn:aws:iam::000000000101:role/other-evaluator", "arn:aws:sts::000000000101:assumed-role/other-evaluator/run-1", False),
        ("invalid", "arn:aws:sts::000000000101:assumed-role/adp-cli-uplift-eval-orchestrator/run-1", False),
    ],
)
def test_product_evaluator_rejects_mismatched_assumed_role(tmp_path, role, caller, allowed):
    steps = workflow("gateway-live-tests.yml")["jobs"]["live"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Verify scoped Product evaluator identity")
    assert guard["if"] == "inputs.product_cases != '' && inputs.product_cases != 'none'"
    fake_aws = tmp_path / "aws"
    fake_aws.write_text("#!/bin/sh\nprintf '%s\\n' '" + caller + "'\n")
    fake_aws.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", guard["run"]], env={"EVAL_ROLE_ARN": role, "PATH": str(tmp_path) + ":" + os.environ["PATH"]}, capture_output=True
    )
    assert (result.returncode == 0) == allowed


@pytest.mark.parametrize("variant", ["selected", "none", "staging", "manual", "missing_correlation", "missing_source", "skipped_deploy"])
def test_product_workflow_gate_requires_reviewed_successful_deployment(variant):
    document = workflow("gateway-deploy.yml")
    jobs = document["jobs"]
    guard = jobs["product-acceptance-guard"]
    selected = "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
    assert guard["needs"] == ["deploy-backend", "deploy-frontend", "run-migrations", "finalize-pricing", "smoke-test"]
    assert jobs["product-acceptance"]["needs"] == ["product-acceptance-guard", "product-cli-evidence"]
    assert jobs["product-cli-evidence"]["needs"] == "product-acceptance-guard"
    assert jobs["product-cli-evidence"]["uses"] == "./.github/workflows/eval-cli-uplift.yml"
    # This checkpoint executes the installed CLI cases. The full Product suite
    # also contains independent release/rollback scenarios owned by D01.
    assert jobs["product-cli-evidence"]["with"]["suites"] == "case:PR03-C01,case:PR03-C02,case:PR03-C03"
    assert jobs["product-cli-evidence"]["with"]["expected_revision"] == "${{ inputs.adp_source_revision }}"
    assert jobs["product-cli-evidence"]["if"] == jobs["product-acceptance"]["if"] == "inputs.product_cases != '' && inputs.product_cases != 'none'"
    assert jobs["product-acceptance"]["uses"] == "./.github/workflows/gateway-live-tests.yml"
    assert jobs["product-acceptance"]["with"]["product_cases"] == "${{ inputs.product_cases }}"
    assert jobs["product-acceptance"]["with"]["expected_revision"] == "${{ inputs.adp_source_revision }}"
    assert jobs["product-acceptance"]["secrets"] == "inherit"
    env = {
        "PRODUCT_CASES": selected if variant != "none" else "none",
        "ADP_SOURCE": "a" * 40 if variant != "missing_source" else "",
        "ADP_CORRELATION": "b" * 64 if variant != "missing_correlation" else "",
        "ADP_MANUAL_SOURCE": "" if variant != "manual" else "a" * 40,
        "DEPLOY_ENVIRONMENT": "dev" if variant != "staging" else "staging",
        "REQUIRED_JOBS": json.dumps(
            {name: {"result": "skipped" if variant == "skipped_deploy" and name == "deploy-backend" else "success"} for name in guard["needs"]}
        ),
    }
    result = subprocess.run(["/bin/bash", "-c", guard["steps"][0]["run"]], env=env, capture_output=True)
    assert (result.returncode == 0) == (variant == "selected"), result.stderr


@pytest.mark.parametrize(
    "selection",
    [
        "none",
        "live_browser_create,live_browser_conflict_denial,live_browser_evidence",
        "live_scope_isolation,live_scope_denials,live_scope_history_cleanup",
    ],
)
def test_frontend_context_reaches_the_signed_store_and_workflow_receipt(evidence, selection):
    from typing import Annotated

    from pydantic import TypeAdapter, ValidationError

    from deployment_evidence.store import object_key
    from src.orchestration.deployment_workflows import WorkflowReceipt

    env, read = evidence
    env.update(CONTEXT_WORKFLOW=".github/workflows/gateway-frontend-deploy.yml", INPUT_ADP_APPROVED_REVISION="", INPUT_PRODUCT_CASES=selection)
    context = WorkflowContext.model_validate(context_module.context(env, read))
    assert context.workflow_path == env["CONTEXT_WORKFLOW"]
    assert context.inputs == {"environment": "dev", "adp_approved_revision": "", "product_cases": selection}
    key = object_key(123, 42, 1, "context", "gateway-frontend-deploy.yml")
    reference = "s3://example-private-evidence/" + key + "?versionId=version-one"
    adapter = TypeAdapter(Annotated[str | None, WorkflowReceipt.model_fields["artifact_ref"]])
    assert adapter.validate_python(reference) == reference
    with pytest.raises(ValidationError):
        adapter.validate_python(reference.replace("gateway-frontend-deploy", "unreviewed"))
    steps = workflow("gateway-frontend-deploy.yml")["jobs"]["publish"]["steps"]
    publish = next(step for step in steps if " context " in step.get("run", ""))
    assert "context gateway-frontend-deploy.yml" in publish["run"]
    assert publish["if"] == "inputs.adp_correlation != ''"
    record = next(step for step in steps if step.get("name") == "Record deployment workflow context")
    assert record["if"] == "inputs.adp_correlation != ''"
    assert record["env"]["CONTEXT_SOURCE"] == "${{ inputs.adp_source_revision || github.sha }}"
    assert record["env"]["CONTEXT_CORRELATION"] == "${{ inputs.adp_correlation }}"
    assert record["env"]["INPUT_PRODUCT_CASES"] == "${{ inputs.product_cases || 'none' }}"


def test_frontend_deployment_requires_browser_success_for_selected_product_cases():
    document = workflow("gateway-frontend-deploy.yml")
    selection = document[True]["workflow_dispatch"]["inputs"]["product_cases"]
    assert selection["default"] == "none"
    assert selection["options"] == [
        "none",
        "live_browser_create,live_browser_conflict_denial,live_browser_evidence",
        "live_scope_isolation,live_scope_denials,live_scope_history_cleanup",
    ]
    job = document["jobs"]["product-browser"]
    assert job["needs"] == ["publish"]
    assert job["if"] == "inputs.product_cases != '' && inputs.product_cases != 'none'"
    assert not job.get("continue-on-error")
    assert job["uses"] == "./.github/workflows/e2e-new-ui-playwright.yml"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["with"] == {
        "environment": "dev",
        "expected_flag": "on",
        "product_v11": False,
        "product_scope": "${{ inputs.product_cases == 'live_scope_isolation,live_scope_denials,live_scope_history_cleanup' }}",
        "product_source_revision": "${{ inputs.adp_source_revision }}",
        "product_release_run_id": "${{ github.run_id }}",
        "product_release_attempt": "${{ github.run_attempt }}",
        "product_release_workflow_revision": "${{ github.workflow_sha }}",
    }


@pytest.mark.parametrize(
    "selection",
    [
        "live_browser_create,live_browser_conflict_denial,live_browser_evidence",
        "live_scope_isolation,live_scope_denials,live_scope_history_cleanup",
    ],
)
@pytest.mark.parametrize("variant", ["selected", "none", "partial", "wrong_cases", "non_dev", "uncorrelated"])
def test_frontend_product_selection_is_checked_before_publication(variant, selection):
    env = {
        "ADP_SOURCE": "b" * 40,
        "ADP_DEFINITION": "a" * 40,
        "ACTUAL_DEFINITION": "a" * 40,
        "ADP_CORRELATION": "c" * 64,
        "INPUT_ENVIRONMENT": "dev",
        "INPUT_PRODUCT_CASES": selection,
    }
    if variant == "none":
        env["INPUT_PRODUCT_CASES"] = "none"
        env.update(ADP_SOURCE="", ADP_DEFINITION="", ADP_CORRELATION="")
    elif variant == "partial":
        env["INPUT_PRODUCT_CASES"] = selection.split(",")[0]
    elif variant == "wrong_cases":
        env["INPUT_PRODUCT_CASES"] = "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
    elif variant == "non_dev":
        env["INPUT_ENVIRONMENT"] = "prod"
    elif variant == "uncorrelated":
        env.update(ADP_SOURCE="", ADP_DEFINITION="", ADP_CORRELATION="")
    guard = workflow("gateway-frontend-deploy.yml")["jobs"]["publish"]["steps"][0]
    result = subprocess.run(["/bin/bash", "-c", guard["run"]], env=env, capture_output=True)
    assert (result.returncode == 0) is (variant in {"selected", "none"}), result.stderr


@pytest.mark.parametrize("field,value", [("ACCOUNT_ID", "999999999999"), ("EKS_CLUSTER", "other-cluster"), ("AWS_REGION", "us-west-2")])
def test_actual_target_mismatch_cannot_publish_context(evidence, field, value):
    env, read = evidence
    with pytest.raises(ValueError):
        context_module.context({**env, field: value}, read)


@pytest.mark.parametrize("name", ["gateway-deploy.yml", "run-gateway-migrations.yml"])
@pytest.mark.parametrize(
    "manual,source,definition,correlation,valid",
    [
        ("b" * 40, "", "", "", True),
        ("main", "", "", "", False),
        ("b" * 12, "", "", "", False),
        ("$(echo untrusted)", "", "", "", False),
        ("b" * 40, "b" * 40, "a" * 40, "c" * 64, False),
        ("b" * 40, "", "a" * 40, "", False),
        ("", "b" * 40, "", "", False),
    ],
)
def test_manual_source_is_separate_exact_and_mutually_exclusive(name, manual, source, definition, correlation, valid):
    document = workflow(name)
    events = document.get("on", document.get(True))
    assert events["workflow_dispatch"]["inputs"]["manual_source_revision"]["default"] == ""
    if name == "run-gateway-migrations.yml":
        assert "manual_source_revision" in events["workflow_call"]["inputs"]
    guard = next(iter(document["jobs"].values()))["steps"][0]
    result = subprocess.run(
        ["/bin/bash", "-c", guard["run"]],
        env={
            "ADP_MANUAL_SOURCE": manual,
            "ADP_SOURCE": source,
            "ADP_DEFINITION": definition,
            "ADP_CORRELATION": correlation,
            "ACTUAL_DEFINITION": "a" * 40,
        },
        capture_output=True,
    )
    assert (result.returncode == 0) == valid


def test_deployment_control_comes_from_definition_and_receipt_follows_verification():
    document = workflow("gateway-deploy.yml")
    backend = document["jobs"]["deploy-backend"]
    assert "github.ref == 'refs/heads/main'" in backend["if"]
    assert backend["permissions"]["deployments"] == "write"
    steps = backend["steps"]
    control = next(s for s in steps if s.get("name") == "Load maintained deployment guards")
    assert control["env"]["ADP_WORKFLOW_REVISION"] == "${{ github.workflow_sha }}"
    assert 'git show "$ADP_WORKFLOW_REVISION:scripts/check-assistant-deploy-boundary.sh"' in control["run"]
    assert 'git show "$ADP_WORKFLOW_REVISION:modules/gateway/scripts/gateway-deployment-receipt.py"' in control["run"]
    assert 'git merge-base --is-ancestor "$ADP_WORKFLOW_REVISION" "$GITHUB_SHA"' in control["run"]
    receipt = next(s for s in steps if s.get("name") == "Publish successful Gateway source receipt")
    assert steps[-1] == receipt
    assert receipt["env"]["ADP_RECEIPT_IMAGE"] == "${{ steps.release.outputs.digest }}"
    assert document["jobs"]["run-migrations"]["with"]["manual_source_revision"] == "${{ inputs.manual_source_revision || '' }}"
    for name in ("gateway-deploy.yml", "run-gateway-migrations.yml"):
        for job in workflow(name)["jobs"].values():
            for step in job.get("steps", []):
                if step.get("name") == "Verify source belongs to reviewed main history":
                    assert 'test "$(git rev-parse HEAD)" = "$ADP_SELECTED_SOURCE"' in step["run"]
                    assert "git merge-base --is-ancestor HEAD FETCH_HEAD" in step["run"]


@pytest.mark.parametrize(
    "manual,source,definition,correlation,actual,allowed",
    [
        ("", "", "", "", "a" * 40, True),
        ("b" * 40, "", "", "", "a" * 40, True),
        ("", "b" * 40, "a" * 40, "c" * 64, "a" * 40, True),
        ("", "b" * 40, "a" * 40, "c" * 64, "d" * 40, False),
        ("", "b" * 40, "", "c" * 64, "a" * 40, False),
        ("", "main", "a" * 40, "c" * 64, "a" * 40, False),
        ("b" * 40, "b" * 40, "a" * 40, "c" * 64, "a" * 40, False),
    ],
)
def test_chat_dispatch_guard_runs_before_cluster_steps(manual, source, definition, correlation, actual, allowed):
    document = workflow("chat-agent-deploy.yml")
    events = document.get("on", document.get(True))
    assert TRANSPORT_INPUTS <= events["workflow_dispatch"]["inputs"].keys()
    steps = document["jobs"]["build-and-deploy"]["steps"]
    assert steps[0]["name"] == "Guard engine delivery revisions"
    assert steps[1]["uses"].startswith("actions/checkout@")
    assert next(index for index, step in enumerate(steps) if "kubectl" in step.get("run", "")) > 0
    result = subprocess.run(
        ["/bin/bash", "-c", steps[0]["run"]],
        env={
            "ADP_MANUAL_SOURCE": manual,
            "ADP_SOURCE": source,
            "ADP_DEFINITION": definition,
            "ADP_CORRELATION": correlation,
            "ACTUAL_DEFINITION": actual,
        },
        capture_output=True,
    )
    assert (result.returncode == 0) is allowed, result.stderr


def test_chat_selected_source_reaches_checkout_guard_and_build():
    steps = workflow("chat-agent-deploy.yml")["jobs"]["build-and-deploy"]["steps"]
    selected = "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
    checkouts = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
    assert checkouts[0]["with"]["ref"] == "${{ github.workflow_sha }}"
    assert checkouts[1]["with"]["ref"] == selected
    credential_check = next(step for step in steps if "adp_cred/deployment.py configure" in step.get("run", ""))
    assert steps.index(checkouts[0]) < steps.index(credential_check) < steps.index(checkouts[1])
    assert (
        next(step for step in steps if step.get("name") == "Verify source belongs to reviewed main history")["env"]["ADP_SELECTED_SOURCE"] == selected
    )
    assert next(step for step in steps if step.get("name") == "Guard assistant auto-deployment")["env"]["ADP_ASSISTANT_DEPLOY_CANDIDATE"] == selected
    build = next(step for step in steps if step.get("uses") == "./.github/actions/codebuild-run")
    assert build["with"]["source_revision"] == selected
    assert f"name=IMAGE_TAG,value={selected}" in build["with"]["environment_variables"]
    export = next(step for step in steps if step.get("name") == "Export build outputs")
    assert export["env"]["ADP_SELECTED_SOURCE"] == selected
    assert "${{ inputs." not in export["run"]


def test_chat_legacy_security_path_rejects_engine_dispatch_before_checkout():
    steps = workflow("chat-agent-deploy.yml")["jobs"]["legacy-security-update"]["steps"]
    assert steps[0]["name"] == "Guard engine delivery revisions"
    assert steps[1]["uses"].startswith("actions/checkout@")
    sha = "a" * 40
    for correlation, allowed in [("", True), ("c" * 64, False)]:
        result = subprocess.run(
            ["/bin/bash", "-c", steps[0]["run"]],
            env={"ADP_MANUAL_SOURCE": "", "ADP_SOURCE": "", "ADP_DEFINITION": "", "ADP_CORRELATION": correlation, "GITHUB_SHA": sha},
            capture_output=True,
        )
        assert (result.returncode == 0) is allowed, result.stderr


def test_dev_box_module_uses_separate_unresolved_deployment_entry():
    from src.orchestration.deployment_manifest import load_packaged_manifest
    from src.orchestration.deployment_workflows import components_for_paths

    assert components_for_paths(["modules/user-services/dev-box/infra/main.tf"]) == ("dev-box",)
    assert components_for_paths([".github/workflows/dev-box-deploy.yml"]) == ("dev-box",)
    assert components_for_paths(["modules/gateway/src/cloud_desktop/routes.py"]) == ("gateway-backend",)
    manifest = load_packaged_manifest()
    entry = next(entry for entry in manifest.entries if entry.entry_id == "adp-dev-dev-box")
    assert entry.covers("dev-box")
    assert entry.status.value == "unresolved"
    assert entry.workflow.path == ".github/workflows/dev-box-deploy.yml"
    assert entry.verification_adapter == "dev-box-release-verification"
