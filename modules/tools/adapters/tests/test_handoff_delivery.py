"""Only a completed, correctly bound route transition can satisfy delivery."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "handoff_delivery.py"


@pytest.fixture
def reporter():
    spec = importlib.util.spec_from_file_location("handoff_delivery_test_subject", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture(reporter):
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "42",
        "GITHUB_REPOSITORY_ID": "17",
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_WORKFLOW_SHA": "b" * 40,
        "GITHUB_SHA": "b" * 40,
        "GITHUB_WORKFLOW_REF": "example/repo/" + reporter.WORKFLOW + "@refs/heads/main",
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "b" * 40,
        "ADP_CORRELATION": "orch:11111111-2222-3333-4444-555555555555",
        "ADP_IDENTITY_CONTRACT_DIGEST": "c" * 64,
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "ADP_TOOLS_ACCOUNT_ID": "123456789012",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "ADP_TOOLS_GATEWAY_ID": "gateway123",
        "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-tools-handoff-trusted-deployment",
        "ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN": "arn:aws:iam::123456789012:role/ADP-Agent-dev-platform",
    }
    inputs = {
        "mode": "release",
        "stage": "tool-adapters",
        "environment": "dev",
        "adapter_action": "handoff",
        "adp_contract_digest": env["ADP_IDENTITY_CONTRACT_DIGEST"],
        "adp_source_revision": env["ADP_SOURCE_REVISION"],
        "adp_definition_revision": env["ADP_DEFINITION_REVISION"],
        "adp_correlation": env["ADP_CORRELATION"],
    }
    routes = {
        name: {
            "backend": "disabled" if name == "atlassian" else "legacy",
            "target_id": name + "123",
            "digest": "sha256:" + "d" * 64,
        }
        for name in reporter.CONNECTORS
    }
    after_release = {
        "contract_version": "1.0",
        "source_revision": env["ADP_SOURCE_REVISION"],
        "image_digest": "sha256:" + "e" * 64,
        "group": "tool-connectors",
        "group_generation": 1,
        "backend_generation": 1,
    }
    before = {"status": "legacy", "release": None, "routes": routes, "deployment_id": "old123"}
    after = {
        "status": "active",
        "release": after_release,
        "deployment_id": "new123",
        "routes": {
            name: {**route, "backend": "adapter", "digest": "sha256:" + "f" * 64} for name, route in routes.items()
        },
    }
    plan = {"api_id": "api123", "stage": "dev", "before": before, "after": after}
    config = {
        "plan": plan,
        "selectors": {
            "account_id": env["ADP_TOOLS_ACCOUNT_ID"],
            "region": env["ADP_TOOLS_AWS_REGION"],
            "gateway_id": env["ADP_TOOLS_GATEWAY_ID"],
        },
    }
    state = {
        "status": "active",
        "release": copy.deepcopy(after_release),
        "transition": {
            "phase": "active",
            "plan": copy.deepcopy(plan),
            "plan_sha256": reporter.fingerprint(plan),
            "verified_routes": copy.deepcopy(after["routes"]),
            "verified_deployment_id": after["deployment_id"],
            "drain_until": 100,
            "owner": {
                "correlation": env["ADP_CORRELATION"],
                "source_revision": env["ADP_SOURCE_REVISION"],
                "workflow": "tools-platform-deploy.yml",
                "role_arn": env["ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN"],
                "mutation_role_arn": env["ADP_TOOLS_DEPLOY_ROLE_ARN"],
            },
        },
    }
    return {"config": config, "state": state, "observed": copy.deepcopy(after)}, env, inputs


def test_completed_transition_binds_all_documents_without_certifying_live_acceptance(reporter, fixture):
    result, env, inputs = fixture
    result["config"]["selectors"]["private_extra"] = "must-not-be-published"
    output = reporter.documents(result, env, inputs, now=200)
    assert set(output) == {"deployment-context.json", "delivery.json", "handoff-readback.json"}
    delivery = output["delivery.json"]
    assert delivery["component"] == "tools-platform"
    assert delivery["resource_id"] == "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gateway123"
    assert delivery["actual_revision"] == env["ADP_SOURCE_REVISION"]
    assert delivery["cleanup"] == "not_required"
    assert {check["id"] for check in delivery["checks"]} == set(reporter.CHECKS)
    readback_bytes = reporter.encoded(output["handoff-readback.json"]) + b"\n"
    assert {check["evidence_ref"] for check in delivery["checks"]} == {
        "handoff-readback.json#sha256:" + hashlib.sha256(readback_bytes).hexdigest()
    }
    assert "must-not-be-published" not in json.dumps(output)
    assert "provider" not in json.dumps(output)
    assert output["deployment-context.json"]["inputs"]["adp_contract_digest"] == inputs["adp_contract_digest"]


def test_delivery_keeps_prior_image_identity_after_restored_legacy_replacement(reporter, fixture):
    result, env, inputs = fixture
    plan = result["config"]["plan"]
    plan["before"]["release"] = {
        **plan["after"]["release"],
        "source_revision": "d" * 40,
        "image_digest": "sha256:" + "9" * 64,
        "group_generation": 2,
        "backend_generation": 2,
    }
    plan["after"]["release"].update(group_generation=3, backend_generation=3)
    result["observed"] = copy.deepcopy(plan["after"])
    result["state"]["release"] = copy.deepcopy(plan["after"]["release"])
    result["state"]["transition"].update(plan=copy.deepcopy(plan), plan_sha256=reporter.fingerprint(plan))
    output = reporter.documents(result, env, inputs, now=200)
    assert output["delivery.json"]["actual_revision"] == env["ADP_SOURCE_REVISION"]


@pytest.mark.parametrize(
    "path,value",
    [
        (("observed", "deployment_id"), "foreign"),
        (("observed", "routes", "github", "digest"), "sha256:" + "1" * 64),
        (("observed", "routes", "slack", "backend"), "legacy"),
        (("observed", "routes", "slack", "target_id"), "github123"),
        (("observed", "release", "source_revision"), "1" * 40),
        (("observed", "release", "backend_generation"), True),
        (("state", "status"), "fenced"),
        (("state", "transition", "phase"), "restored"),
        (("state", "transition", "plan_sha256"), "1" * 64),
        (("state", "transition", "plan", "api_id"), "foreign"),
        (("state", "transition", "verified_deployment_id"), "foreign"),
        (("state", "transition", "verified_routes", "jira", "digest"), "sha256:" + "1" * 64),
        (("state", "transition", "owner", "correlation"), "orch:22222222-2222-3333-4444-555555555555"),
        (("state", "transition", "owner", "mutation_role_arn"), "arn:aws:iam::123456789012:role/other"),
        (("state", "transition", "drain_until"), 201),
        (("state", "transition", "drain_until"), True),
        (("config", "selectors", "account_id"), "987654321098"),
        (("config", "selectors", "gateway_id"), "foreign"),
    ],
)
def test_rejects_partial_foreign_or_stale_transition(reporter, fixture, path, value):
    result, env, inputs = fixture
    node = result
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    with pytest.raises(ValueError):
        reporter.documents(result, env, inputs, now=200)


@pytest.mark.parametrize(
    "field,value",
    [
        ("adapter_action", "foundation"),
        ("mode", "identity"),
        ("stage", "platform"),
        ("environment", "prod"),
        ("manual_source_revision", "a" * 40),
        ("adp_correlation", "orch:22222222-2222-3333-4444-555555555555"),
        ("adp_source_revision", "f" * 40),
        ("adp_contract_digest", "f" * 64),
        ("unreviewed_input", "enabled"),
    ],
)
def test_rejects_changed_workflow_inputs(reporter, fixture, field, value):
    result, env, inputs = fixture
    inputs[field] = value
    with pytest.raises(ValueError):
        reporter.documents(result, env, inputs, now=200)


def test_explicit_rollback_records_restored_generations_only(reporter, fixture):
    result, env, inputs = fixture
    inputs["adapter_action"] = "rollback"
    restored = copy.deepcopy(result["config"]["plan"]["before"])
    restored["release"] = {
        **result["config"]["plan"]["after"]["release"],
        "group_generation": 2,
        "backend_generation": 2,
    }
    result["observed"] = restored
    result["state"].update(status="legacy", release=restored["release"])
    result["state"]["transition"].update(
        phase="restored", verified_routes=restored["routes"], verified_deployment_id="old123"
    )
    assert reporter.documents(result, env, inputs, now=200)["handoff-readback.json"]["phase"] == "restored"
    inputs["adapter_action"] = "handoff"
    with pytest.raises(ValueError):
        reporter.documents(result, env, inputs, now=200)


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_decode_rejects_ambiguous_evidence(reporter, raw):
    with pytest.raises(ValueError):
        reporter.decode(raw)


@pytest.fixture
def entrypoint(reporter, fixture, tmp_path):
    result, env, inputs = fixture
    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    workflow = repo / reporter.WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: Fixture\n")
    git("add", ".")
    git("commit", "-m", "fixture")
    source = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "unrelated main change")
    definition = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", definition)
    git("checkout", "--detach", definition)
    env.update(
        ADP_SOURCE_REVISION=source,
        ADP_DEFINITION_REVISION=definition,
        GITHUB_SHA=definition,
        GITHUB_WORKFLOW_SHA=definition,
    )
    inputs.update(adp_source_revision=source, adp_definition_revision=source)
    result["config"]["plan"]["after"]["release"]["source_revision"] = source
    result["observed"]["release"]["source_revision"] = source
    result["state"]["release"]["source_revision"] = source
    result["state"]["transition"]["plan"] = copy.deepcopy(result["config"]["plan"])
    result["state"]["transition"]["plan_sha256"] = reporter.fingerprint(result["config"]["plan"])
    result["state"]["transition"]["owner"]["source_revision"] = source
    private = tmp_path / "result.json"
    private.write_bytes(reporter.encoded(result))
    private.chmod(0o600)
    output = tmp_path / "evidence"

    def run():
        selected_env = {**os.environ, **env, "ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON": json.dumps(inputs)}
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--result", str(private), "--output", str(output)],
            cwd=repo,
            env=selected_env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return run, private, output, git, env, inputs, result


def test_actual_entrypoint_accepts_unchanged_workflow_on_newer_main_and_never_overwrites(entrypoint):
    run, _, output, *_ = entrypoint
    completed = run()
    assert completed.returncode == 0, completed.stderr
    assert output.stat().st_mode & 0o077 == 0
    assert len(list(output.iterdir())) == 3
    original = {path.name: path.read_bytes() for path in output.iterdir()}
    assert all(path.stat().st_mode & 0o077 == 0 for path in output.iterdir())
    assert run().returncode != 0
    assert {path.name: path.read_bytes() for path in output.iterdir()} == original


@pytest.mark.parametrize("failure", ["symlink", "public", "hardlink", "checkout", "definition", "foreign-result"])
def test_actual_entrypoint_refuses_before_publishing(entrypoint, failure):
    run, private, output, git, env, inputs, result = entrypoint
    if failure == "symlink":
        real = private.with_suffix(".real")
        private.rename(real)
        private.symlink_to(real)
    elif failure == "hardlink":
        os.link(private, private.with_suffix(".other"))
    elif failure == "public":
        private.chmod(0o644)
    elif failure == "checkout":
        git("checkout", "--detach", env["ADP_SOURCE_REVISION"])
    elif failure == "definition":
        inputs["adp_definition_revision"] = "f" * 40
    else:
        result["observed"]["deployment_id"] = "foreign"
        private.write_text(json.dumps(result))
    completed = run()
    assert completed.returncode != 0
    assert not output.exists()
    assert "Traceback" not in completed.stderr
