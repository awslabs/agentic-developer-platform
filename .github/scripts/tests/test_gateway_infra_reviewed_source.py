"""Reject unreviewed infra dispatch before authority, using only local seams.

Run the actual workflow guard against a two-commit local Git history. The shell
PATH contains only a restricted local git shim, so an accidental fetch or cloud
command cannot reach a network. The caller's actual shell runs against a gh stub;
no Actions dispatch, deployment action, provider or cloud tool runs in this suite.
"""

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
GIT = shutil.which("git")
BASH = shutil.which("bash")
SOURCE_SELECTION = "${{ inputs.adp_source_revision || github.sha }}"


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def triggers(document):
    # PyYAML's YAML 1.1 loader treats the Actions key `on` as True.
    return document.get("on", document.get(True))


INFRA = workflow("gateway-infra-apply.yml")
STEPS = INFRA["jobs"]["apply"]["steps"]
GUARD = next(
    step
    for step in STEPS
    if step.get("name") == "Verify exact reviewed source before deployment"
)
DEPLOY = workflow("gateway-deploy.yml")
CALLER = next(
    step
    for step in DEPLOY["jobs"]["deploy-backend"]["steps"]
    if "gh workflow run gateway-infra-apply.yml" in step.get("run", "")
)


@pytest.fixture
def local_history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "HOME": str(tmp_path),
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*args):
        return subprocess.run(
            [GIT, "-c", "core.hooksPath=/dev/null", *args],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Reviewed source test")
    git("config", "user.email", "review@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("commit", "--allow-empty", "-m", "Previously reviewed")
    old = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "Future main")
    new = git("rev-parse", "HEAD")
    git("merge-base", "--is-ancestor", old, new)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        '[[ "$#" == 2 && "$1" == rev-parse && "$2" == HEAD ]] || exit 91\n'
        f'exec {shlex.quote(GIT)} "$@"\n'
    )
    git_shim.chmod(0o755)
    env.update(
        PATH=str(bin_dir),
        COMMAND_LOG=str(tmp_path / "commands"),
        BOUNDARY_LOG=str(tmp_path / "boundaries"),
    )
    return repo, env, git, old, new


def run_guard(local_history, reviewed, run_sha):
    repo, env, _, _, _ = local_history
    env = {**env, "GITHUB_SHA": run_sha}
    if reviewed is not None:
        env["REVIEWED_SOURCE_SHA"] = reviewed
    result = subprocess.run(
        [
            BASH,
            "-c",
            GUARD["run"]
            + '\nprintf "trusted-deployment\\nload-deploy-config\\n" >> "$BOUNDARY_LOG"',
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    boundary_log = Path(env["BOUNDARY_LOG"])
    reached = boundary_log.read_text().splitlines() if boundary_log.exists() else []
    return result, reached


def test_old_callers_must_supply_a_required_string_without_default():
    pin = triggers(INFRA)["workflow_dispatch"]["inputs"]["reviewed_source_sha"]
    assert pin["required"] is True
    assert pin["type"] == "string"
    assert "default" not in pin


def test_guard_precedes_all_authority_and_config_steps_without_bypass():
    assert set(INFRA["jobs"]) == {"apply"}
    job = INFRA["jobs"]["apply"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert not job.get("continue-on-error", False)
    assert STEPS[0]["uses"].startswith("actions/checkout@")
    assert STEPS[1] == GUARD
    assert "ref" not in STEPS[0].get("with", {})
    assert GUARD["shell"] == "bash"
    assert GUARD["env"]["REVIEWED_SOURCE_SHA"] == "${{ inputs.reviewed_source_sha }}"
    assert "${{" not in GUARD["run"]
    assert "if" not in GUARD
    assert not GUARD.get("continue-on-error", False)
    assert STEPS[2]["uses"] == "./.github/actions/trusted-deployment"
    assert STEPS[3]["uses"] == "./.github/actions/load-deploy-config"
    for step in STEPS[2:]:
        if any(
            term in step.get("if", "") for term in ("always(", "failure(", "cancelled(")
        ):
            assert step["name"] == "Summary"


@pytest.mark.parametrize(
    "reviewed",
    [
        None,
        "",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        "main",
        "a" * 40 + "\n",
        "$(exit 0)",
    ],
)
def test_missing_or_malformed_pin_stops_before_any_command(local_history, reviewed):
    result, reached = run_guard(local_history, reviewed, local_history[4])
    assert result.returncode != 0
    assert "required and must be exactly 40 lowercase" in result.stdout
    assert reached == []
    assert not Path(local_history[1]["COMMAND_LOG"]).exists()


@pytest.mark.parametrize(
    ("reviewed_index", "run_index", "checkout_index"),
    [(3, 4, 4), (4, 3, 4), (4, 4, 3), (3, 4, 3), (3, 3, 4)],
)
def test_any_source_mismatch_blocks_including_old_ancestor(
    local_history, reviewed_index, run_index, checkout_index
):
    local_history[2]("checkout", "--detach", local_history[checkout_index])
    result, reached = run_guard(
        local_history, local_history[reviewed_index], local_history[run_index]
    )
    assert result.returncode != 0
    assert "differs from reviewed_source_sha" in result.stdout
    assert reached == []


def test_missing_run_sha_fails_closed(local_history):
    result, reached = run_guard(local_history, local_history[4], "")
    assert result.returncode != 0
    assert reached == []


def test_matching_reviewed_run_and_checkout_reach_deployment(local_history):
    result, reached = run_guard(local_history, local_history[4], local_history[4])
    assert result.returncode == 0, result.stderr
    assert reached == ["trusted-deployment", "load-deploy-config"]
    assert Path(local_history[1]["COMMAND_LOG"]).read_text() == "rev-parse HEAD\n"


@pytest.mark.parametrize("explicit_source", [False, True])
def test_caller_propagates_selected_release_and_stale_dispatch_fails(
    local_history, explicit_source
):
    repo, env, _, old, new = local_history
    assert CALLER["env"]["REVIEWED_SOURCE_SHA"] == SOURCE_SELECTION
    checkout = DEPLOY["jobs"]["deploy-backend"]["steps"][0]
    assert checkout["with"]["ref"] == SOURCE_SELECTION
    assert "${{" not in CALLER["run"]
    # Resolve the asserted Actions expression for an engine pin and push fallback.
    source_input = old if explicit_source else ""
    selected = source_input or new
    gh = Path(env["PATH"]) / "gh"
    gh.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$GH_ARGS_LOG"\n')
    gh.chmod(0o755)
    args_log = repo / "gh-args"
    result = subprocess.run(
        [BASH, "-e", "-c", CALLER["run"]],
        cwd=repo,
        env={
            **env,
            "GH_ARGS_LOG": str(args_log),
            "REVIEWED_SOURCE_SHA": selected,
            "CUSTOMER_ACCOUNT_ID": "",
            "CUSTOMER_AWS_LABEL": "",
            "CUSTOMER_USER_ID": "",
            "ENVIRONMENT": "dev",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    args = args_log.read_bytes().decode().split("\0")[:-1]
    assert args[:3] == ["workflow", "run", "gateway-infra-apply.yml"]
    fields = dict(
        args[index + 1].split("=", 1)
        for index, value in enumerate(args)
        if value == "--field"
    )
    assert fields["reviewed_source_sha"] == selected
    result, reached = run_guard(local_history, fields["reviewed_source_sha"], new)
    if explicit_source:
        assert result.returncode != 0
        assert reached == []
    else:
        assert result.returncode == 0, result.stderr
        assert reached == ["trusted-deployment", "load-deploy-config"]


def test_guard_suite_is_registered_for_both_workflow_sources():
    ci = workflow("script-tests.yml")
    for event in ("push", "pull_request"):
        paths = triggers(ci)[event]["paths"]
        assert ".github/workflows/gateway-infra-apply.yml" in paths
        assert ".github/workflows/gateway-deploy.yml" in paths
    assert any(
        "tests/test_gateway_infra_reviewed_source.py" in step.get("run", "")
        for job in ci["jobs"].values()
        for step in job["steps"]
    )
