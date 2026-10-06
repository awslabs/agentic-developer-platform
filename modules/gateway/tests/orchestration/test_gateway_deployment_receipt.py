"""Release evidence must identify deployed source, even when main differs."""

import copy
import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "modules/gateway/scripts/gateway-deployment-receipt.py"
spec = importlib.util.spec_from_file_location("gateway_receipt", SCRIPT)
receipt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receipt)
SOURCE, DEFINITION = "a" * 40, "b" * 40
DIGEST = "sha256:" + "c" * 64


@pytest.fixture
def setup(tmp_path):
    env = {
        "ADP_RECEIPT_ROLE": "arn:aws:iam::123456789012:role/adp-dev-gateway-trusted-deployment",
        "ADP_RECEIPT_TARGET": "adp-gateway-deploy-dev",
        "ADP_RECEIPT_ENVIRONMENT": "dev",
        "ADP_RECEIPT_REGION": "us-east-1",
        "ADP_RECEIPT_REPOSITORY_ID": "123",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "99",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": DEFINITION,
        "GITHUB_REF": "refs/heads/main",
        "RUNNER_TEMP": str(tmp_path),
        "ADP_RECEIPT_SOURCE": SOURCE,
        "ADP_RECEIPT_DEFINITION": DEFINITION,
        "ADP_RECEIPT_IMAGE": DIGEST,
    }
    run = {
        "id": 42,
        "run_attempt": 2,
        "head_sha": DEFINITION,
        "repository": {"id": 123},
        "head_branch": "main",
        "event": "workflow_dispatch",
        "path": receipt.WORKFLOW,
    }
    release = {
        **receipt.target(env),
        "schema_version": 1,
        "run_id": 42,
        "run_attempt": 2,
        "workflow_revision": DEFINITION,
        "source_revision": SOURCE,
        "image_digest": DIGEST,
        "component": "gateway-backend",
        "workflow_path": receipt.WORKFLOW,
        "assets": {},
    }
    job = {
        "name": receipt.JOB,
        "status": "completed",
        "conclusion": "success",
        "steps": [{"name": receipt.ROLLOUT, "status": "completed", "conclusion": "success"}, {"name": receipt.RECEIPT_STEP, "conclusion": "success"}],
    }
    deployment = {"id": 7, "sha": SOURCE, "environment": env["ADP_RECEIPT_TARGET"], "task": receipt.TASK, "payload": release}
    state = {
        "runs": [run],
        "job": job,
        "deployments": [deployment],
        "release": release,
        "statuses": [{"state": "success", "log_url": "https://github.com/owner/repo/actions/runs/42/attempts/2"}],
        "writes": [],
    }

    def api(path, binary=False, body=None):
        if body is not None:
            state["writes"].append((path, body))
            return {"id": 7, "sha": SOURCE} if path.endswith("/deployments") else {"state": "success"}
        if "/workflows/" in path:
            return {"workflow_runs": state["runs"]}
        if path.endswith("/actions/runs/42"):
            return run
        if "/jobs?" in path:
            return {"jobs": [job]}
        if "/artifacts?" in path:
            return {"artifacts": state["artifacts"]}
        if path.endswith("/zip"):
            return state["archive"]
        if "/statuses?" in path:
            return state["statuses"]
        if "/deployments?" in path:
            return state["deployments"]
        raise AssertionError(path)

    return env, state, api


def legacy(state):
    state["job"]["steps"] = [s for s in state["job"]["steps"] if s["name"] != receipt.RECEIPT_STEP]
    state["deployments"] = []
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("release.json", json.dumps(state["release"]))
    payload = stream.getvalue()
    state["archive"] = payload
    state["artifacts"] = [
        {
            "id": 8,
            "name": "adp-release-gateway-backend-2",
            "expired": False,
            "size_in_bytes": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "workflow_run": {"id": 42, "repository_id": 123, "head_repository_id": 123, "head_branch": "main", "head_sha": DEFINITION},
        }
    ]


@pytest.mark.parametrize("old", [False, True])
def test_baseline_is_actual_source_never_workflow_head(setup, old):
    env, state, api = setup
    if old:
        legacy(state)
    assert receipt.resolve(env, api) == SOURCE
    assert SOURCE != DEFINITION


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_revision", "main"),
        ("workflow_revision", SOURCE),
        ("run_id", 41),
        ("run_attempt", 1),
        ("repository_id", 124),
        ("account_id", "999999999999"),
        ("region", "us-west-2"),
        ("resource_id", "another/namespace"),
        ("component", "gateway-frontend"),
        ("image_digest", "mutable:latest"),
        ("workflow_path", "other.yml"),
        ("schema_version", 2),
    ],
)
@pytest.mark.parametrize("old", [False, True])
def test_receipt_binding_mismatch_refuses(setup, key, value, old):
    env, state, api = setup
    state["release"][key] = value
    if old:
        legacy(state)
    with pytest.raises(ValueError):
        receipt.resolve(env, api)


@pytest.mark.parametrize("problem", ["missing", "duplicate", "expired", "tampered", "wrong-run", "wrong-repo", "wrong-head", "oversize"])
def test_legacy_artifact_must_be_unique_authenticated_and_bounded(setup, problem):
    env, state, api = setup
    legacy(state)
    if problem == "missing":
        state["artifacts"] = []
    elif problem == "duplicate":
        state["artifacts"] *= 2
    elif problem == "expired":
        state["artifacts"][0]["expired"] = True
    elif problem == "tampered":
        state["archive"] += b"tampered"
    elif problem == "wrong-run":
        state["artifacts"][0]["workflow_run"]["id"] = 43
    elif problem == "wrong-repo":
        state["artifacts"][0]["workflow_run"]["repository_id"] = 124
    elif problem == "wrong-head":
        state["artifacts"][0]["workflow_run"]["head_sha"] = SOURCE
    else:
        state["artifacts"][0]["size_in_bytes"] = receipt.MAX_BYTES + 1
    with pytest.raises(ValueError):
        receipt.resolve(env, api)


@pytest.mark.parametrize("problem", ["duplicate", "source", "environment", "status", "url", "missing"])
def test_durable_receipt_mismatch_or_missing_cannot_fall_back(setup, problem):
    env, state, api = setup
    if problem == "duplicate":
        state["deployments"] *= 2
    elif problem == "source":
        state["deployments"][0]["sha"] = DEFINITION
    elif problem == "environment":
        state["deployments"][0]["environment"] = "adp-gateway-deploy-prod"
    elif problem == "status":
        state["statuses"][0]["state"] = "failure"
    elif problem == "url":
        state["statuses"][0]["log_url"] = "https://example.com"
    else:
        legacy(state)
        state["job"]["steps"].append({"name": receipt.RECEIPT_STEP, "conclusion": "success"})
    with pytest.raises(ValueError):
        receipt.resolve(env, api)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
def test_later_incomplete_rollout_does_not_reuse_stale_baseline(setup, conclusion):
    env, state, api = setup
    state["job"]["conclusion"] = conclusion
    with pytest.raises(ValueError, match="baseline uncertain"):
        receipt.resolve(env, api)


def test_success_without_actual_rollout_is_not_deployment(setup):
    env, state, api = setup
    state["job"]["steps"] = []
    with pytest.raises(ValueError, match="lacks successful rollout"):
        receipt.resolve(env, api)


def test_no_baseline_refuses(setup):
    env, state, api = setup
    state["runs"] = []
    with pytest.raises(ValueError, match="no verified"):
        receipt.resolve(env, api)


def test_publish_binds_exact_source_image_target_and_attempt(setup):
    env, state, api = setup
    data = copy.deepcopy(state["release"])
    data.update(run_id=99, run_attempt=1)
    folder = Path(env["RUNNER_TEMP"]) / "adp-release-gateway-backend"
    folder.mkdir()
    (folder / "release.json").write_text(json.dumps(data))
    assert receipt.publish(env, api) == 7
    deploy, status = (body for _, body in state["writes"])
    assert deploy["ref"] == SOURCE
    assert deploy["auto_merge"] is False
    assert deploy["payload"] == data
    assert deploy["environment"] == env["ADP_RECEIPT_TARGET"]
    assert status["log_url"].endswith("/99/attempts/1")
    assert status["state"] == "success"
    state["writes"].clear()
    env["ADP_RECEIPT_IMAGE"] = "sha256:" + "d" * 64
    with pytest.raises(ValueError, match="deployed release"):
        receipt.publish(env, api)
    assert state["writes"] == []
