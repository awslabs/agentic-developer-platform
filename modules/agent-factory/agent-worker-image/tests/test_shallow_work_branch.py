"""Real Git regression for repeat hosted runs; cloud/GitHub sinks are mocked."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint

BRANCH = "agent/issue-42"


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


@pytest.fixture
def shallow_repo(tmp_path, monkeypatch):
    # Do not inherit a developer's signing, hooks or file-protocol settings.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Branch regression")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")
    remote, seed, clone = (tmp_path / name for name in ("remote.git", "seed", "clone"))
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "init", "--initial-branch=main", str(seed))
    (seed / "README.md").write_text("main\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "main")
    # Exceed the hosted depth so this exercises an actual shallow boundary.
    for index in range(20):
        git(seed, "commit", "--allow-empty", "-m", f"main history {index}")
    git(seed, "checkout", "-b", BRANCH)
    (seed / "prior-work.md").write_text("accepted prior checkpoint\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "prior work")
    prior_sha = git(seed, "rev-parse", "HEAD").stdout.strip()
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "main", BRANCH)
    # file:// is essential: local-path clones ignore --depth.
    git(tmp_path, "clone", "--depth=20", remote.as_uri(), str(clone))
    assert git(clone, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    monkeypatch.setattr(entrypoint, "WORK_DIR", clone)
    return clone, remote, prior_sha


def test_repeat_run_checks_out_prior_work_and_has_a_real_upstream(shallow_repo):
    clone, remote, prior_sha = shallow_repo
    assert git(
        clone, "show-ref", "--verify", f"refs/remotes/origin/{BRANCH}", check=False
    ).returncode

    entrypoint._checkout_existing_work_branch(BRANCH)

    assert git(clone, "branch", "--show-current").stdout.strip() == BRANCH
    assert git(clone, "rev-parse", "HEAD").stdout.strip() == prior_sha
    assert git(clone, "rev-parse", "@{upstream}").stdout.strip() == prior_sha
    assert (clone / "prior-work.md").read_text() == "accepted prior checkpoint\n"
    assert git(remote, "rev-parse", BRANCH).stdout.strip() == prior_sha


def test_repeated_setup_preserves_local_commits_and_uncommitted_work(shallow_repo):
    clone, _, _ = shallow_repo
    entrypoint._checkout_existing_work_branch(BRANCH)
    (clone / "local.md").write_text("not pushed yet\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local checkpoint")
    local_sha = git(clone, "rev-parse", "HEAD").stdout.strip()
    (clone / "prior-work.md").write_text("editing prior checkpoint\n")

    entrypoint._checkout_existing_work_branch(BRANCH)

    assert git(clone, "rev-parse", "HEAD").stdout.strip() == local_sha
    assert (clone / "prior-work.md").read_text() == "editing prior checkpoint\n"


def test_missing_remote_branch_is_not_replaced_with_main(shallow_repo):
    clone, remote, _ = shallow_repo
    main_sha = git(clone, "rev-parse", "HEAD").stdout.strip()
    git(remote, "update-ref", "-d", f"refs/heads/{BRANCH}")
    with pytest.raises(subprocess.CalledProcessError):
        entrypoint._checkout_existing_work_branch(BRANCH)
    assert git(clone, "rev-parse", "HEAD").stdout.strip() == main_sha
    assert git(clone, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False).returncode


def test_reviewer_finalization_publishes_on_the_existing_branch(shallow_repo, monkeypatch):
    clone, remote, prior_sha = shallow_repo
    entrypoint._checkout_existing_work_branch(BRANCH)
    report = "data/code-review/review-42.md"
    (clone / report).parent.mkdir(parents=True)
    (clone / report).write_text("review delivered\n")
    real_run = entrypoint.run_cmd

    def run_cmd(args, **kwargs):
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, "123\n", "")
        assert args[0] == "git", f"unexpected external command: {args}"
        return real_run(args, **kwargs)

    monkeypatch.setattr(entrypoint, "run_cmd", run_cmd)
    for name in ("_ensure_pr_body_marker", "_register_authored_draft", "_post_comment"):
        monkeypatch.setattr(entrypoint, name, Mock(return_value=""))
    monkeypatch.setattr(entrypoint, "_read_result_metadata", dict)
    status = Mock()
    monkeypatch.setattr(entrypoint, "update_invocation_status", status)

    assert entrypoint._handle_success("example/repo", 42, BRANCH, "reviewer", "run", "date") == 0
    assert git(remote, "show", f"{BRANCH}:{report}").stdout == "review delivered\n"
    assert git(remote, "merge-base", "--is-ancestor", prior_sha, BRANCH).returncode == 0
    assert not git(clone, "status", "--porcelain").stdout.strip()
    assert status.call_args.args[2] == "complete"


class AgentLaunched(BaseException):
    """Stop main at the Node boundary without running any finalization sinks."""


@pytest.fixture
def bootstrap(monkeypatch, tmp_path, request):
    # main mutates os.environ directly. Isolate those writes and every external
    # service so a bootstrap regression cannot publish fixture errors to AWS.
    monkeypatch.setattr(
        entrypoint.os,
        "environ",
        {
            "QUEUE_URL": "https://example.invalid/queue",
            "AWS_REGION": "us-east-1",
            "ADP_BEDROCK_VIA": "direct",
        },
    )
    envelope = {
        "version": "1.0",
        "channel": "github",
        "tenant_id": "test",
        "persona": "reviewer",
        "message_id": "branch-test",
        "arrived_at": "2026-09-12T00:00:00Z",
        "source_ref": {"installation_id": 1, "repo": "example/repo", "issue": 42},
        "intent": {"trigger": "issue_labeled", "label": "reviewer"},
    }
    branch_case = getattr(request, "param", "open_pr")
    if branch_case == "aidlc":
        envelope["persona"] = "aidlc"
    monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(
        entrypoint, "_receive_one_message", Mock(return_value=(json.dumps(envelope), "receipt"))
    )
    monkeypatch.setattr(entrypoint, "_load_door_api_key", Mock())
    monkeypatch.setattr(
        entrypoint,
        "_resolve_execution_token",
        Mock(
            return_value=Mock(
                token_mode="app",
                token="",
                github_login="",
            )
        ),
    )
    monkeypatch.setattr(entrypoint, "_gh_token_broker_enabled", lambda: True)
    monkeypatch.setattr(
        entrypoint, "_broker_installation_token", Mock(return_value=("test-token", "1"))
    )
    monkeypatch.setattr(entrypoint, "_is_already_completed", Mock(return_value=False))
    monkeypatch.setattr(entrypoint, "_stage_personas_and_skills", Mock())
    monkeypatch.setattr(
        entrypoint, "create_check_run", Mock(return_value={"id": 1, "html_url": ""})
    )
    monkeypatch.setattr(entrypoint, "VisibilityHeartbeat", Mock())
    monkeypatch.setattr(
        entrypoint.boto3, "client", Mock(side_effect=AssertionError("unexpected AWS call"))
    )
    logger = Mock()
    monkeypatch.setattr(entrypoint, "BootstrapLogger", Mock(return_value=logger))
    status = Mock()
    monkeypatch.setattr(entrypoint, "update_invocation_status", status)
    commands = []

    def execute(args, **kwargs):
        commands.append(args)
        if args[0] == "node":
            raise AgentLaunched()
        if args[:2] == ["git", "ls-remote"] and branch_case == "fresh":
            return subprocess.CompletedProcess(args, 2, "", "")
        if args[:3] == ["gh", "pr", "list"] and branch_case == "aidlc":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "123\n", "")

    monkeypatch.setattr(entrypoint.subprocess, "run", execute)
    return logger, status, commands


@pytest.mark.parametrize(
    ("bootstrap", "failed_command"),
    [
        ("open_pr", "fetch"),
        ("open_pr", "checkout"),
        ("aidlc", "fetch"),
        ("aidlc", "checkout"),
        ("fresh", "checkout"),
    ],
    indirect=["bootstrap"],
)
def test_branch_setup_failure_records_failure_before_launch(bootstrap, monkeypatch, failed_command):
    logger, status, commands = bootstrap

    def run_cmd(args, **kwargs):
        if args[:2] == ["git", failed_command]:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "abc123\n", "")

    monkeypatch.setattr(entrypoint, "run_cmd", run_cmd)
    with pytest.raises(subprocess.CalledProcessError):
        entrypoint.main()

    assert not any(args[0] == "node" for args in commands)
    status.assert_called_once()
    assert status.call_args.args == ("branch-test", "2026-09-12T00:00:00Z", "failed")
    assert BRANCH in status.call_args.kwargs["error_message"]
    logger.close.assert_called_once()


@pytest.mark.parametrize("failed_command", ["commit", "push"])
def test_wip_publication_failure_can_still_launch_on_prepared_branch(
    bootstrap, monkeypatch, failed_command
):
    _, status, _ = bootstrap
    checkout = Mock()

    def run_cmd(args, **kwargs):
        if args[:2] == ["git", failed_command]:
            raise subprocess.CalledProcessError(1, args)
        if args[:2] == ["git", "checkout"]:
            checkout(args[2])
        return subprocess.CompletedProcess(args, 0, "abc123\n", "")

    monkeypatch.setattr(entrypoint, "run_cmd", run_cmd)
    with pytest.raises(AgentLaunched):
        entrypoint.main()

    checkout.assert_called_once_with(BRANCH)
    assert [call.args[2] for call in status.call_args_list] == ["in_progress"]
