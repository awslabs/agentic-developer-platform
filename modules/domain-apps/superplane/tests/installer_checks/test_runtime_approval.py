"""Authenticated GitHub authority, never local approval dictionaries or cloud calls."""

import base64
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from installation.config import Refusal
from installation.runner import atomic
from installation.runtime_approval import (
    GitHubPlanApproval,
    decode_json,
    manifest,
    manifest_path,
)

HEAD = "b" * 40
URL = "https://github.com/aws-e/adp/pull/42"


def save_plan(directory, environment, *, binary_bound=True):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    terraform = directory / "terraform"
    terraform.mkdir(mode=0o700, exist_ok=True)
    binary = terraform / "installation.tfplan"
    binary.write_bytes(b"synthetic Terraform plan; no live authority")
    binary.chmod(0o600)
    operator = {"review_id": "runtime-demo-review"}
    receipt = {
        "version": 1,
        "status": "planned",
        "review_id": operator["review_id"],
        "installation_id": "a" * 24,
        "worker_ready": False,
        "request_sha256": "1" * 64,
        "proposal_sha256": "2" * 64,
        "source_sha256": "3" * 64,
        "plan_sha256": "4" * 64,
        "resources": {"queue_arn": "arn:aws:sqs:879318057152:us-east-1:runtime"},
    }
    if binary_bound:
        receipt["binary_plan_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    atomic(directory / "runtime-preparation.json", receipt)
    atomic(
        directory / "runtime-plan-manifest.json",
        manifest(environment, operator, directory),
    )
    return operator, receipt


@pytest.fixture
def review_setup(tmp_path, environment):
    directory = tmp_path / "saved"
    operator, receipt = save_plan(directory, environment)
    reviewed = manifest(environment, operator, directory)
    pull = {
        "number": 42,
        "state": "open",
        "draft": False,
        "user": {"id": 1},
        "base": {"ref": "main", "repo": {"id": 1186991269}},
        "head": {"sha": HEAD, "repo": {"id": 1186991269}},
    }
    approval = {
        "id": 100,
        "state": "APPROVED",
        "commit_id": HEAD,
        "user": {"id": 2, "login": "reviewer"},
    }
    state = {
        "pull": pull,
        "manifest": reviewed,
        "reviews": [approval],
        "permission": {"role_name": "admin", "permission": "admin", "user": {"id": 2}},
        "repo": {"id": 1186991269, "full_name": "aws-e/adp", "default_branch": "main"},
        "calls": [],
        "pull_reads": 0,
    }

    def get(path):
        state["calls"].append(path)
        if path == "repos/aws-e/adp":
            return copy.deepcopy(state["repo"])
        if path.endswith("/pulls/42"):
            state["pull_reads"] += 1
            data = copy.deepcopy(state["pull"])
            if state.get("move_head") and state["pull_reads"] > 1:
                data["head"]["sha"] = "c" * 40
            return data
        if "/contents/" in path:
            raw = json.dumps(state["manifest"]).encode()
            return {
                "type": "file",
                "path": manifest_path(operator["review_id"]),
                "encoding": "base64",
                "size": len(raw),
                "sha": hashlib.sha1(
                    b"blob " + str(len(raw)).encode() + b"\0" + raw
                ).hexdigest(),
                "content": base64.b64encode(raw).decode(),
            }
        if "/reviews?" in path:
            return copy.deepcopy(state["reviews"])
        if "/reviews/" in path:
            if state.get("change_binary"):
                (directory / "terraform/installation.tfplan").write_bytes(b"changed")
            return copy.deepcopy(state.get("final_review", state["reviews"][-1]))
        if "/permission" in path:
            return copy.deepcopy(
                state.get("permissions", {}).get(path, state["permission"])
            )
        raise AssertionError(path)

    adapter = GitHubPlanApproval(
        URL, environment, operator, directory, api=SimpleNamespace(get=get)
    )
    arguments = {
        key: receipt[key] for key in ("plan_sha256", "installation_id", "review_id")
    }
    return adapter, arguments, state


def test_real_api_records_bind_manifest_head_actor_and_current_permission(review_setup):
    adapter, args, state = review_setup
    result = adapter.verify_plan(**args)
    assert result == {"approved": True, **args, "approver": "github:user:2"}
    saved = json.loads(
        (adapter.directory / "runtime-plan-review-evidence.json").read_text()
    )
    assert saved["reviewer_id"] == 2 and saved["commit_id"] == HEAD
    assert state["pull_reads"] == 2
    assert sum("/permission" in path for path in state["calls"]) == 2
    assert "database" not in saved["manifest"] and "secrets" not in saved["manifest"]


@pytest.mark.parametrize(
    "change",
    [
        "repo",
        "closed",
        "draft",
        "fork",
        "branch",
        "self",
        "comment",
        "dismissed",
        "changes",
        "old-head",
        "no-review",
        "write-only",
        "bot-none",
        "permission-user",
        "head-moved",
        "revoked",
        "binary-changed",
    ],
)
def test_missing_or_changed_independent_authority_refuses(review_setup, change):
    adapter, args, state = review_setup
    if change == "repo":
        state["repo"]["id"] = 999
    elif change == "closed":
        state["pull"]["state"] = "closed"
    elif change == "draft":
        state["pull"]["draft"] = True
    elif change == "fork":
        state["pull"]["head"]["repo"]["id"] = 999
    elif change == "branch":
        state["pull"]["base"]["ref"] = "other"
    elif change == "self":
        state["pull"]["user"]["id"] = 2
    elif change in ("comment", "dismissed", "changes"):
        state["reviews"][0]["state"] = {
            "comment": "COMMENTED",
            "dismissed": "DISMISSED",
            "changes": "CHANGES_REQUESTED",
        }[change]
    elif change == "old-head":
        state["reviews"][0]["commit_id"] = "d" * 40
    elif change == "no-review":
        state["reviews"] = []
    elif change == "write-only":
        state["permission"].update(role_name="write", permission="write")
    elif change == "bot-none":
        state["reviews"][0]["user"]["login"] = "app-reviewer[bot]"
        state["permission"].update(role_name="", permission="none")
    elif change == "permission-user":
        state["permission"]["user"]["id"] = 999
    elif change == "head-moved":
        state["move_head"] = True
    elif change == "revoked":
        state["final_review"] = {**state["reviews"][0], "state": "DISMISSED"}
    else:
        state["change_binary"] = True
    with pytest.raises(Refusal):
        adapter.verify_plan(**args)
    assert not (adapter.directory / "runtime-plan-review-evidence.json").exists()


@pytest.mark.parametrize(
    "key",
    [
        "plan_sha256",
        "binary_plan_sha256",
        "proposal_sha256",
        "source_sha256",
        "request_sha256",
        "installation_id",
        "review_id",
        "target",
        "resources",
        "deployment_identity",
        "scope",
    ],
)
def test_committed_manifest_must_match_every_target_and_source_binding(
    review_setup, key
):
    adapter, args, state = review_setup
    state["manifest"][key] = "different"
    with pytest.raises(Refusal, match="differs from saved plan"):
        adapter.verify_plan(**args)


def test_later_changes_requested_supersedes_approval(review_setup):
    adapter, args, state = review_setup
    state["reviews"].append(
        {**state["reviews"][0], "id": 101, "state": "CHANGES_REQUESTED"}
    )
    with pytest.raises(Refusal, match="requests changes"):
        adapter.verify_plan(**args)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other/repo/pull/42",
        "http://github.com/aws-e/adp/pull/42",
        "https://github.com/aws-e/adp/pull/42?head=fake",
        "42",
        "https://evil.test/aws-e/adp/pull/42",
    ],
)
def test_installer_cannot_choose_another_approval_authority(review_setup, url):
    adapter, _, _ = review_setup
    with pytest.raises(Refusal):
        GitHubPlanApproval(
            url, adapter.environment, adapter.operator, adapter.directory
        )


def test_local_approved_dict_does_not_replace_live_review(review_setup):
    adapter, args, state = review_setup
    atomic(
        adapter.directory / "runtime-plan-review-evidence.json",
        {"approved": True, "approver": "administrator"},
    )
    state["reviews"] = []
    with pytest.raises(Refusal, match="No separate"):
        adapter.verify_plan(**args)


@pytest.mark.parametrize(
    "problem", ["symlink", "public", "missing", "binary-different"]
)
def test_untrusted_saved_plan_is_refused_before_github(review_setup, problem):
    adapter, args, state = review_setup
    path = adapter.directory / "terraform/installation.tfplan"
    if problem == "public":
        path.chmod(0o644)
    elif problem == "binary-different":
        path.write_bytes(b"unreviewed")
    else:
        original = path.with_suffix(".old")
        path.rename(original)
        if problem == "symlink":
            path.symlink_to(original)
    with pytest.raises(Refusal):
        adapter.verify_plan(**args)
    assert not state["calls"]


def test_duplicate_json_cannot_rebind_approval():
    with pytest.raises(Refusal):
        decode_json('{"approved":false,"approved":true}')


def test_json_boolean_cannot_impersonate_manifest_version(review_setup):
    adapter, args, state = review_setup
    state["manifest"]["version"] = True
    with pytest.raises(Refusal, match="differs from saved plan"):
        adapter.verify_plan(**args)


@pytest.mark.parametrize("nondecision", ["COMMENTED", "PENDING"])
def test_comments_cannot_erase_an_outstanding_changes_request(
    review_setup, nondecision
):
    adapter, args, state = review_setup
    blocker = {**state["reviews"][0], "state": "CHANGES_REQUESTED"}
    state["reviews"] = [
        blocker,
        {**blocker, "id": 101, "state": nondecision},
        {
            "id": 102,
            "state": "APPROVED",
            "commit_id": HEAD,
            "user": {"id": 3, "login": "second-admin"},
        },
    ]
    state["permissions"] = {
        "repos/aws-e/adp/collaborators/second-admin/permission": {
            "permission": "admin",
            "role_name": "admin",
            "user": {"id": 3},
        },
    }
    with pytest.raises(Refusal, match="requests changes"):
        adapter.verify_plan(**args)


def test_comment_does_not_erase_approval(review_setup):
    adapter, args, state = review_setup
    approved = copy.deepcopy(state["reviews"][0])
    state["reviews"].append({**approved, "id": 101, "state": "COMMENTED"})
    state["final_review"] = approved
    assert adapter.verify_plan(**args)["approved"] is True


def test_dismissed_changes_request_allows_another_current_approval(review_setup):
    adapter, args, state = review_setup
    state["reviews"].insert(
        0,
        {
            "id": 99,
            "state": "DISMISSED",
            "commit_id": HEAD,
            "user": {"id": 3, "login": "second-admin"},
        },
    )
    state["permissions"] = {
        "repos/aws-e/adp/collaborators/second-admin/permission": {
            "permission": "admin",
            "role_name": "admin",
            "user": {"id": 3},
        },
    }
    assert adapter.verify_plan(**args)["approved"] is True
