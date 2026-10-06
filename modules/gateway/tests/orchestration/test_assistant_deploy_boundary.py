"""Assistant changes cannot deploy from an unreviewed main push or an unapproved dispatch."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

GUARD = Path(__file__).resolve().parents[4] / "scripts/check-assistant-deploy-boundary.sh"
REPO = GUARD.parents[1]
WORKFLOWS = REPO / ".github/workflows"
INVENTORY = REPO / "docs/architecture/assistant-deploy-boundary.md"

# Source roots of the components audited by docs/architecture/assistant-deploy-boundary.md.
COMPONENT_ROOTS = {
    "gateway": ["modules/gateway/src"],
    "chat-worker": ["modules/agent-factory/agent"],
    "agent-worker": [
        "modules/agent-factory/agent",
        "modules/agent-factory/agent-worker-image",
        "modules/agent-factory/codex-reviewer",
        "modules/agent-factory/codex-harness",
    ],
    "frontend": ["modules/gateway/frontend"],
}

GUARDED_WORKFLOWS = [
    ("gateway-deploy.yml", "deploy-backend", "gateway", "adp-gateway-deploy-${{ inputs.environment || 'dev' }}"),
    ("chat-agent-deploy.yml", "build-and-deploy", "chat-worker", "adp-chat-deploy-dev"),
    ("agent-worker-image.yml", "deploy", "agent-worker", "adp-worker-deploy-${{ inputs.environment || 'dev' }}"),
]


def git(repo, *args):
    return subprocess.check_output(["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args], text=True).strip()


def commit(repo, path, content="fixture\n"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", str(path))
    git(repo, "commit", "-qm", path)
    return git(repo, "rev-parse", "HEAD")


def guard(repo, approved, event="push", component="gateway", **overrides):
    """Run the guard. ``approved=None`` leaves the persistent approval variables unset.

    Override values of ``None`` remove the variable from the environment.
    """
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("ADP_")},
        "GITHUB_EVENT_NAME": event,
        "GITHUB_SHA": git(repo, "rev-parse", "HEAD"),
        "ADP_ASSISTANT_DEPLOY_COMPONENT": component,
        "ADP_ASSISTANT_DEPLOY_TARGET": f"adp-{component}-deploy-test",
    }
    if approved is not None:
        env["ADP_ASSISTANT_APPROVED_TARGET"] = f"adp-{component}-deploy-test"
        env["ADP_ASSISTANT_APPROVED_REVISION"] = approved
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(["bash", str(GUARD)], cwd=repo, capture_output=True, text=True, env=env, check=False)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.com")
    git(tmp_path, "init", "-q")
    commit(tmp_path, "README.md")
    return tmp_path


# ── Persistent approval ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "component,path,allowed",
    [
        ("gateway", "modules/gateway/src/orchestration/chat_data_migration.py", False),
        ("gateway", "modules/gateway/src/orchestration/chat_data/routes.py", False),
        ("gateway", "modules/gateway/src/orchestration/intake_session.py", False),
        ("gateway", "modules/gateway/src/agentauth/chat_model.py", False),
        ("gateway", "modules/gateway/src/agentauth/bootstrap.py", False),
        ("gateway", "modules/gateway/src/chat_data/routes.py", False),
        ("gateway", "modules/gateway/src/main.py", False),
        ("gateway", "modules/gateway/src/app.py", False),
        ("chat-worker", "modules/agent-factory/agent/src/complex-task-chat/context/store/port.ts", False),
        ("chat-worker", "modules/agent-factory/agent/k8s/chat-scaledjob.yaml", False),
        ("chat-worker", "modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh", False),
        ("agent-worker", "modules/agent-factory/agent/src/complex-task-chat/tools.ts", False),
        ("agent-worker", "modules/agent-factory/agent/src/complex-task-chat/context/store/port.ts", False),
        ("agent-worker", "modules/agent-factory/agent/k8s/chat-scaledjob.yaml", False),
        ("gateway", "modules/gateway/src/tasks/unrelated.py", True),
        ("chat-worker", "modules/agent-factory/agent/src/unrelated.ts", True),
        ("gateway", "modules/agent-factory/agent/src/complex-task-chat/tools.ts", True),
        ("chat-worker", "modules/gateway/src/agentauth/chat_model.py", True),
        ("agent-worker", "modules/agent-factory/agent/src/unrelated.ts", True),
        ("agent-worker", "modules/agent-factory/agent/src/agent-worker.ts", True),
        ("agent-worker", "modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh", True),
        ("agent-worker", "modules/agent-factory/agent-worker-image/entrypoint.py", True),
        ("agent-worker", "modules/agent-factory/codex-reviewer/src/index.ts", True),
        ("agent-worker", "modules/gateway/src/agentauth/chat_model.py", True),
    ],
)
def test_candidate_requires_approved_assistant_source(repository, component, path, allowed):
    approved = git(repository, "rev-parse", "HEAD")
    held = commit(repository, path)
    result = guard(repository, approved, component=component)
    assert (result.returncode == 0) is allowed
    if not allowed:
        assert "Candidate contains held assistant changes" in result.stdout
        # A dispatch inherits no authority from the event itself: the same approval is still too old.
        dispatch = guard(repository, approved, component=component, event="workflow_dispatch")
        assert dispatch.returncode == 1
        assert "Candidate contains held assistant changes" in dispatch.stdout
        # The dispatch input for exactly this candidate is the explicit approval path.
        approved_dispatch = guard(repository, approved, component=component, event="workflow_dispatch", ADP_ASSISTANT_DISPATCH_APPROVED_REVISION=held)
        assert approved_dispatch.returncode == 0


@pytest.mark.parametrize(
    "component,held_path,unrelated_path",
    [
        ("gateway", "modules/gateway/src/agentauth/chat_model.py", "modules/gateway/src/tasks/unrelated.py"),
        ("chat-worker", "modules/agent-factory/agent/src/complex-task-chat/tools.ts", "modules/agent-factory/agent/src/unrelated.ts"),
        ("agent-worker", "modules/agent-factory/agent/src/complex-task-chat/tools.ts", "modules/agent-factory/agent-worker-image/entrypoint.py"),
    ],
)
def test_unrelated_followup_push_cannot_carry_held_changes(repository, component, held_path, unrelated_path):
    approved = git(repository, "rev-parse", "HEAD")
    held = commit(repository, held_path)
    assert guard(repository, approved, component=component, ADP_DEPLOYED_BASELINE=approved).returncode == 1
    commit(repository, unrelated_path)
    # The deployed baseline must not weaken a recorded approval.
    result = guard(repository, approved, component=component, ADP_DEPLOYED_BASELINE=held)
    assert result.returncode == 1
    assert "Candidate contains held assistant changes" in result.stdout
    assert guard(repository, approved, component=component, event="workflow_dispatch").returncode == 1
    assert guard(repository, approved, component=component).returncode == 1
    assert guard(repository, held, component=component).returncode == 0


def test_assistant_route_registration_remains_held_against_persistent_approval(repository):
    source = "modules/gateway/src/app.py"
    approved = commit(repository, source, 'UNIT_MODULES = ["src.agentauth.chat_model"]\n')
    held = commit(repository, source, 'UNIT_MODULES = ["src.agentauth.chat_model", "src.agentauth.chat_data_routes"]\n')
    assert git(repository, "diff", "--name-only", approved, held) == source
    result = guard(repository, approved)
    assert result.returncode == 1
    assert "Candidate contains held assistant changes" in result.stdout

    commit(repository, "modules/gateway/src/tasks/unrelated.py")
    result = guard(repository, approved, ADP_DEPLOYED_BASELINE=held)
    assert result.returncode == 1
    assert "Candidate contains held assistant changes" in result.stdout
    assert guard(repository, approved, event="workflow_dispatch").returncode == 1
    assert guard(repository, approved).returncode == 1
    assert guard(repository, held).returncode == 0


@pytest.mark.parametrize("approved", ["", "0" * 40, "not-a-sha", "f" * 40])
def test_unknown_approval_fails_closed(repository, approved):
    assert guard(repository, approved).returncode == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"ADP_ASSISTANT_APPROVED_TARGET": ""},
        {"ADP_ASSISTANT_APPROVED_TARGET": "another-target"},
        {"ADP_ASSISTANT_DEPLOY_TARGET": ""},
        {"ADP_ASSISTANT_DEPLOY_COMPONENT": "unknown"},
        {"GITHUB_SHA": "not-a-sha"},
        {"GITHUB_SHA": "f" * 40},
    ],
)
def test_wrong_target_or_candidate_fails_closed(repository, overrides):
    assert guard(repository, git(repository, "rev-parse", "HEAD"), **overrides).returncode == 1


def test_non_ancestor_approval_fails_closed(repository):
    other_history = git(repository, "commit-tree", git(repository, "rev-parse", "HEAD^{tree}"), "-m", "unrelated approval")
    assert guard(repository, other_history).returncode == 1


@pytest.mark.parametrize("event", ["", "schedule", "workflow_run", "pull_request"])
def test_other_events_do_not_inherit_dispatch_authority(repository, event):
    head = git(repository, "rev-parse", "HEAD")
    assert guard(repository, head, event=event).returncode == 1
    assert guard(repository, None, event=event, ADP_ASSISTANT_DISPATCH_APPROVED_REVISION=head).returncode == 1


@pytest.mark.parametrize("change", ["delete", "rename"])
def test_protected_file_removal_is_still_a_held_change(repository, change):
    source = "modules/gateway/src/orchestration/chat_data.py"
    approved = commit(repository, source)
    if change == "delete":
        git(repository, "rm", source)
    else:
        git(repository, "mv", source, "unrelated.py")
    git(repository, "commit", "-qm", change)
    assert guard(repository, approved).returncode == 1


def test_reverted_assistant_changes_do_not_hold_unrelated_deployment(repository):
    approved = git(repository, "rev-parse", "HEAD")
    source = "modules/gateway/src/orchestration/chat_data.py"
    commit(repository, source)
    git(repository, "rm", source)
    commit(repository, "modules/gateway/src/tasks/unrelated.py")
    assert guard(repository, approved).returncode == 0


# ── workflow_dispatch: no bypass ──────────────────────────────────────────────


@pytest.mark.parametrize("component", ["gateway", "chat-worker", "agent-worker"])
def test_dispatch_without_any_approval_is_refused(repository, component):
    deployed = git(repository, "rev-parse", "HEAD")
    commit(repository, "unrelated.txt")
    # No approval and no deployed baseline: nothing authorizes the dispatch.
    result = guard(repository, None, event="workflow_dispatch", component=component)
    assert result.returncode == 1
    assert "No assistant approval is recorded" in result.stdout
    assert "adp_approved_revision" in result.stdout
    assert "unknown" in result.stdout
    # The deployed baseline is the only no-approval path, for dispatch exactly as for push.
    assert guard(repository, None, event="workflow_dispatch", component=component, ADP_DEPLOYED_BASELINE=deployed).returncode == 0
    commit(repository, {"gateway": "modules/gateway/src/app.py"}.get(component, "modules/agent-factory/agent/src/complex-task-chat/tools.ts"))
    refused = guard(repository, None, event="workflow_dispatch", component=component, ADP_DEPLOYED_BASELINE=deployed)
    assert refused.returncode == 1
    assert "Candidate changes protected assistant paths" in refused.stdout


def test_dispatch_with_approved_revision_input_passes(repository):
    approved = commit(repository, "modules/gateway/src/agentauth/chat_model.py")
    commit(repository, "modules/gateway/src/tasks/unrelated.py")
    head = git(repository, "rev-parse", "HEAD")
    for revision in (approved, head):
        result = guard(repository, None, event="workflow_dispatch", ADP_ASSISTANT_DISPATCH_APPROVED_REVISION=revision)
        assert result.returncode == 0
        assert "adp_approved_revision" in result.stdout
    # The input also overrides a stale persistent approval for this run only.
    stale = git(repository, "rev-parse", "HEAD~2")
    assert guard(repository, stale, event="workflow_dispatch").returncode == 1
    assert guard(repository, stale, event="workflow_dispatch", ADP_ASSISTANT_DISPATCH_APPROVED_REVISION=head).returncode == 0


def test_dispatch_input_must_cover_the_candidate_assistant_files(repository):
    before = git(repository, "rev-parse", "HEAD")
    commit(repository, "modules/gateway/src/agentauth/chat_model.py")
    result = guard(repository, None, event="workflow_dispatch", ADP_ASSISTANT_DISPATCH_APPROVED_REVISION=before)
    assert result.returncode == 1
    assert "Candidate contains held assistant changes" in result.stdout


@pytest.mark.parametrize("revision", ["not-a-sha", "f" * 40, "abc123"])
def test_dispatch_input_must_be_an_available_full_commit(repository, revision):
    result = guard(repository, None, event="workflow_dispatch", ADP_ASSISTANT_DISPATCH_APPROVED_REVISION=revision)
    assert result.returncode == 1


def test_dispatch_input_is_ignored_for_push_events(repository):
    deployed = git(repository, "rev-parse", "HEAD")
    head = commit(repository, "modules/gateway/src/agentauth/chat_model.py")
    assert guard(repository, None, ADP_ASSISTANT_DISPATCH_APPROVED_REVISION=head, ADP_DEPLOYED_BASELINE=deployed).returncode == 1


# ── No approval recorded: compare against the last revision this workflow deployed ──


@pytest.mark.parametrize(
    "component,unrelated_path",
    [
        ("gateway", "modules/gateway/src/tasks/hotfix.py"),
        ("chat-worker", "modules/agent-factory/agent/src/hotfix.ts"),
        ("agent-worker", "modules/agent-factory/agent-worker-image/entrypoint.py"),
    ],
)
def test_push_without_approval_and_no_protected_changes_since_deployed_baseline_passes(repository, component, unrelated_path):
    deployed = git(repository, "rev-parse", "HEAD")
    commit(repository, unrelated_path)
    result = guard(repository, None, component=component, ADP_DEPLOYED_BASELINE=deployed)
    assert result.returncode == 0
    assert "No assistant changes in this candidate" in result.stdout
    assert "unrelated deployment continues" in result.stdout


@pytest.mark.parametrize(
    "component,protected_path",
    [
        ("gateway", "modules/gateway/src/agentauth/chat_model.py"),
        ("gateway", "modules/gateway/src/app.py"),
        ("chat-worker", "modules/agent-factory/agent/src/complex-task-chat/tools.ts"),
        ("agent-worker", "modules/agent-factory/agent/src/complex-task-chat/tools.ts"),
        ("agent-worker", "modules/agent-factory/agent/k8s/chat-scaledjob.yaml"),
    ],
)
def test_push_without_approval_and_protected_changes_is_refused(repository, component, protected_path):
    deployed = git(repository, "rev-parse", "HEAD")
    commit(repository, protected_path)
    commit(repository, "unrelated.txt")
    result = guard(repository, None, component=component, ADP_DEPLOYED_BASELINE=deployed)
    assert result.returncode == 1
    assert "Candidate changes protected assistant paths" in result.stdout
    assert "No assistant approval is recorded" in result.stdout


@pytest.mark.parametrize(
    "component,held_path,unrelated_path",
    [
        ("gateway", "modules/gateway/src/agentauth/chat_model.py", "modules/gateway/src/tasks/hotfix.py"),
        ("chat-worker", "modules/agent-factory/agent/src/complex-task-chat/tools.ts", "modules/agent-factory/agent/src/hotfix.ts"),
        ("agent-worker", "modules/agent-factory/agent/k8s/chat-scaledjob.yaml", "modules/agent-factory/agent-worker-image/entrypoint.py"),
    ],
)
def test_refused_push_is_not_carried_by_a_later_unrelated_push(repository, component, held_path, unrelated_path):
    """Push A (assistant change) is refused and never deployed; push B (unrelated) must not deploy A."""
    deployed = git(repository, "rev-parse", "HEAD")
    held = commit(repository, held_path)
    assert guard(repository, None, component=component, ADP_DEPLOYED_BASELINE=deployed).returncode == 1
    commit(repository, unrelated_path)
    # The deployed baseline is still the pre-A revision because A never succeeded, so B is held too.
    result = guard(repository, None, component=component, ADP_DEPLOYED_BASELINE=deployed)
    assert result.returncode == 1
    assert "Candidate changes protected assistant paths" in result.stdout
    # Only if A had actually deployed (i.e. been approved) would B be an unrelated follow-up.
    assert guard(repository, None, component=component, ADP_DEPLOYED_BASELINE=held).returncode == 0
    assert guard(repository, held, component=component).returncode == 0


def test_push_without_approval_needs_a_known_ancestor_deployed_baseline(repository):
    deployed = git(repository, "rev-parse", "HEAD")
    commit(repository, "unrelated.txt")
    other_history = git(repository, "commit-tree", git(repository, "rev-parse", "HEAD^{tree}"), "-m", "unrelated history")
    for overrides in ({}, {"ADP_DEPLOYED_BASELINE": None}):
        result = guard(repository, None, **overrides)
        assert result.returncode == 1
        assert "unknown" in result.stdout and "ADP_ASSISTANT_APPROVED_TARGET" in result.stdout and "adp_approved_revision" in result.stdout
    for baseline in ("", "0" * 40, "not-a-sha", "f" * 40, other_history, git(repository, "rev-parse", "HEAD")[:12]):
        result = guard(repository, None, ADP_DEPLOYED_BASELINE=baseline)
        assert result.returncode == 1
        assert "No assistant approval is recorded" in result.stdout
    assert guard(repository, None, ADP_DEPLOYED_BASELINE=deployed).returncode == 0


def test_approval_for_another_target_does_not_apply_but_does_not_block_unrelated_pushes(repository):
    deployed = git(repository, "rev-parse", "HEAD")
    commit(repository, "modules/gateway/src/tasks/hotfix.py")
    result = guard(repository, deployed, ADP_ASSISTANT_APPROVED_TARGET="adp-gateway-deploy-prod", ADP_DEPLOYED_BASELINE=deployed)
    assert result.returncode == 0
    assert "does not apply here" in result.stdout
    commit(repository, "modules/gateway/src/agentauth/chat_model.py")
    assert guard(repository, deployed, ADP_ASSISTANT_APPROVED_TARGET="adp-gateway-deploy-prod", ADP_DEPLOYED_BASELINE=deployed).returncode == 1


def test_malformed_persistent_approval_for_this_target_is_a_configuration_error(repository):
    deployed = git(repository, "rev-parse", "HEAD")
    commit(repository, "modules/gateway/src/tasks/hotfix.py")
    # Matching target with a malformed revision must not silently degrade to the baseline comparison.
    assert guard(repository, "not-a-sha", ADP_DEPLOYED_BASELINE=deployed).returncode == 1


# ── Workflow wiring ───────────────────────────────────────────────────────────


def load_workflow(name):
    config = yaml.safe_load((WORKFLOWS / name).read_text())
    return config, config.get("on") or config.get(True)


@pytest.mark.parametrize("workflow,job,component,target", GUARDED_WORKFLOWS)
def test_workflows_bind_guard_to_protected_target_before_credentials(workflow, job, component, target):
    config, triggers = load_workflow(workflow)
    job_config = config["jobs"][job]
    assert job_config["environment"] == target
    assert job_config["permissions"]["actions"] in ("read", "write")
    steps = job_config["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    baseline_step = next(step for step in steps if step.get("id") == "deployed")
    if workflow == "gateway-deploy.yml":
        assert baseline_step["run"] == 'python3 "$RUNNER_TEMP/adp-gateway-control/receipt.py" resolve'
        assert baseline_step["env"]["ADP_RECEIPT_TARGET"] == target
        assert baseline_step["env"]["GH_TOKEN"] == "${{ github.token }}"
        guard_step = next(step for step in steps if step.get("name") == "Guard assistant auto-deployment")
        assert guard_step["run"] == 'bash "$RUNNER_TEMP/adp-gateway-control/assistant-guard.sh"'
    else:
        assert baseline_step["env"] == {"GH_TOKEN": "${{ github.token }}"}
        assert f"actions/workflows/{workflow}/runs?" in baseline_step["run"]
        assert "status=success" in baseline_step["run"] and "per_page=10" in baseline_step["run"]
        assert "branch=${{ github.ref_name }}" in baseline_step["run"]
        # Job-level selection: the GitHub jobs API matches on the job's display name.
        assert "/actions/runs/${run_id}/jobs" in baseline_step["run"]
        assert f'.name == "{job_config["name"]}" and .conclusion == "success"' in baseline_step["run"]
        if workflow == "chat-agent-deploy.yml":
            # skip_deploy=true leaves the job successful but skips the rollout step.
            rollout = next(step for step in steps if step.get("name") == "Update chat ScaledJob manifest")
            assert rollout["if"] == "${{ inputs.skip_deploy != true }}"
            assert '.name == "Update chat ScaledJob manifest" and .conclusion == "success"' in baseline_step["run"]
        assert '.workflow_runs[] | "\\(.id) \\(.head_sha)"' in baseline_step["run"]
        guard_step = next(step for step in steps if step.get("run") == "bash scripts/check-assistant-deploy-boundary.sh")
    expected = {
        "ADP_ASSISTANT_DEPLOY_COMPONENT": component,
        "ADP_ASSISTANT_DEPLOY_TARGET": target,
        "ADP_ASSISTANT_APPROVED_TARGET": "${{ vars.ADP_ASSISTANT_APPROVED_TARGET }}",
        "ADP_ASSISTANT_APPROVED_REVISION": "${{ vars.ADP_ASSISTANT_APPROVED_REVISION }}",
        "ADP_ASSISTANT_DISPATCH_APPROVED_REVISION": "${{ inputs.adp_approved_revision }}",
        "ADP_DEPLOYED_BASELINE": "${{ steps.deployed.outputs.baseline }}",
    }
    if workflow == "gateway-deploy.yml":
        # The engine may dispatch an explicit source revision; the guard must judge that checkout.
        expected["ADP_ASSISTANT_DEPLOY_CANDIDATE"] = "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
        assert checkout["with"]["ref"] == "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
    assert guard_step["env"] == expected
    credentials = next(step for step in steps if step.get("uses") == "./.github/actions/trusted-deployment")
    assert steps.index(checkout) < steps.index(baseline_step) < steps.index(guard_step) < steps.index(credentials)
    for step in steps[: steps.index(guard_step)]:
        run = step.get("run", "")
        assert "aws " not in run and "kubectl" not in run and "KUBECONFIG" not in run
    dispatch_input = triggers["workflow_dispatch"]["inputs"]["adp_approved_revision"]
    assert dispatch_input["type"] == "string"
    assert dispatch_input["default"] == ""
    assert dispatch_input["required"] is False


def test_previous_push_revision_is_no_longer_a_baseline_anywhere():
    retired = ("ADP_PUSH_" + "BEFORE", "event." + "before")
    sources = [GUARD, Path(__file__), REPO / "docs/architecture/adp-assistant-6929.md", INVENTORY]
    sources += [WORKFLOWS / workflow for workflow, _, _, _ in GUARDED_WORKFLOWS]
    for source in sources:
        text = source.read_text()
        for token in retired:
            if source == INVENTORY and token == retired[1]:
                continue  # the inventory names the push's previous revision only to say it is never used
            assert token not in text, f"{source.name} still references {token}"


def test_agent_worker_image_build_jobs_stay_unguarded():
    config, _ = load_workflow("agent-worker-image.yml")
    for job in ("build", "installation-boundary-tests", "codex-tests", "task-investigator-tests", "model-readiness"):
        assert not any(step.get("run") == "bash scripts/check-assistant-deploy-boundary.sh" for step in config["jobs"][job]["steps"])
    assert config["jobs"]["deploy"]["needs"] == "build"


# ── Inventory ─────────────────────────────────────────────────────────────────


def pattern_covers(pattern, root):
    """True when a GitHub push path filter can match a file under ``root``."""
    if pattern.startswith("!"):
        return False
    prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0].rstrip("/")
    if not prefix:
        return True
    return f"{prefix}/".startswith(f"{root}/") or f"{root}/".startswith(f"{prefix}/")


def push_workflows_covering_components():
    covering = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        _, triggers = load_workflow(path.name)
        push = triggers.get("push") if isinstance(triggers, dict) else None
        if not isinstance(push, dict) or not push.get("paths"):
            continue
        components = {
            component
            for component, roots in COMPONENT_ROOTS.items()
            for root in roots
            if any(pattern_covers(pattern, root) for pattern in push["paths"])
        }
        if components:
            covering[path.name] = components
    return covering


def test_component_coverage_derivation_is_sound():
    covering = push_workflows_covering_components()
    assert covering["gateway-deploy.yml"] == {"gateway"}
    assert {"chat-worker", "agent-worker"} <= covering["agent-worker-image.yml"]
    assert covering["gateway-frontend-deploy.yml"] == {"frontend"}
    assert "github-auth-broker-deploy.yml" not in covering
    assert "webhook-ingress-deploy.yml" not in covering


def test_inventory_lists_every_push_workflow_covering_the_components():
    text = INVENTORY.read_text()
    listed = set(re.findall(r"^\| `([a-z0-9-]+\.ya?ml)` \|", text, flags=re.MULTILINE))
    expected = push_workflows_covering_components()
    assert expected, "no push workflows cover the audited components; the derivation is broken"
    missing = set(expected) - listed
    assert not missing, f"push workflows covering audited components are missing from the inventory: {sorted(missing)}"
    stale = listed - {path.name for path in WORKFLOWS.glob("*.yml")}
    assert not stale, f"inventory lists workflows that no longer exist: {sorted(stale)}"
    for workflow, _, component, _ in GUARDED_WORKFLOWS:
        row = next(line for line in text.splitlines() if line.startswith(f"| `{workflow}` |"))
        assert "Guarded" in row and f"`{component}`" in row
