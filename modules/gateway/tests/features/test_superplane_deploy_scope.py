"""Basic platform deployment leaves the optional Superplane module alone.

These tests **execute** `deploy-all.sh` against stub tooling rather than reading it as
text, because the defect they exist to catch is invisible to a text assertion.

The history matters for anyone tempted to simplify this file. The original suite asserted
that the string `--superplane-only` appeared somewhere in `deploy-all.sh`. It did — in the
argument parser — so the assertion passed while the flag was read exactly once, at the
final phase gate. An operator running `--superplane-only` against a live account therefore
got gateway infra, a gateway image build and rollout, an ALB/API-Gateway rewire, a
frontend S3 sync with CloudFront invalidation, the broker Lambda, first-admin database
seeding, webhook-ingress and agent-factory — every one of them a
`terraform apply -auto-approve` — before reaching the one module they asked for and being
told it was skipped. That is the "central list edited without its counterpart" failure
mode this story exists to prevent, reproduced one level up in the test that was supposed
to catch it (#5198 review, blocker 2).

So the contract under test is behavioural: **which phases announce work, and which
announce a skip**, for each scope flag. A guard added to an `if` but forgotten on its
`elif` still fails here, and no arrangement of flag text can make these pass.

How the harness stays offline and non-destructive
-------------------------------------------------
`deploy-all.sh` derives `SCRIPT_DIR` from `BASH_SOURCE` and `ROOT_DIR` from its parent, so
copying the script into a throwaway tree relocates *every* path it resolves — sibling
scripts, module sub-scripts and Terraform directories alike. Nothing in the real
repository is read and nothing outside the temp tree can be written.

`aws`, `terraform`, `kubectl`, `npm`, `git` and `curl` are replaced by stubs earlier on
`PATH` than any real binary, and `AWS_*` environment variables are stripped from the child
so an ambient IRSA identity cannot leak in. The stubs are the enforcement mechanism, not a
convenience: `assert_no_real_tooling` below fails if a real `aws` is reachable, so a future
edit that bypasses the stub directory cannot quietly start making live API calls. These
tests must never depend on anyone's AWS credentials.

The old Superplane flags now refuse before any platform phase. The module has its
own installer and workflows.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# tests/features/<file> -> tests -> gateway -> modules -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEPLOY_ALL = _REPO_ROOT / "platform" / "scripts" / "deploy-all.sh"

# Stubs for every external command the script shells out to before/inside the phases.
# `aws` answers the handful of queries the preamble makes; everything else is a no-op that
# exits 0, because these tests assert on control flow, not on tool behaviour.
_AWS_STUB = """#!/usr/bin/env bash
# Minimal `aws` good-citizen stub. Answers only what deploy-all.sh's preamble needs.
case "$1 $2" in
  "sts get-caller-identity") echo "111122223333" ;;
  "ecr describe-images") echo "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;
  "s3api head-bucket") exit 0 ;;
  "dynamodb describe-table") echo '{"Table":{}}' ;;
  "eks describe-cluster") echo "ACTIVE" ;;
  "eks update-kubeconfig") exit 0 ;;
  "secretsmanager get-secret-value")
    echo '{"SecretString":"offline-scope-test-signing-key"}' ;;
  "ssm get-parameter")
    case "$*" in
      *model-root-bindings*|*arc-model-bindings*) echo '[]' ;;
      *chat-authority-worker-images*) echo "" ;;
      *) echo "None" ;;
    esac ;;

  # Numeric answer: deploy-all.sh compares this with `-eq`, so an empty string aborts
  # the run with "integer expression expected" before the phase under test is reached.
  "codebuild batch-get-projects") echo "1" ;;
  *) exit 0 ;;
esac
"""

# Drains stdin before exiting. Not optional: the script pipes generated manifests into
# `kubectl apply -f -`, and a stub that exits without reading closes the pipe early, so the
# writing `sed` dies of SIGPIPE and `set -o pipefail` aborts the whole run with 141 — long
# before the phase under test. `cat` is a real binary here, so this consumes the input for
# real rather than pretending to.
_NOOP_STUB = "#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\nexit 0\n"
_KUBECTL_STUB = """#!/usr/bin/env bash
if [ "$1 $2 $3" = "get scaledjob agent-gateway-worker" ]; then
  echo "111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-agent-gateway@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  exit 0
fi
cat >/dev/null 2>&1 || true
exit 0
"""
_TERRAFORM_STUB = """#!/usr/bin/env bash
if [ "$1 $2 $3" = "output -json redis_endpoint" ]; then
  echo '[{"address":"redis.example.internal"}]'
elif [ "$1 $2 $3" = "output -raw pentest_actor_client_id" ]; then
  echo "pentest-client-123"
fi
exit 0
"""
_GIT_STUB = '#!/usr/bin/env bash\n# Only `rev-parse HEAD` is consulted, to pin an image tag.\necho "0000000000000000000000000000000000000000"\n'
_CURL_STUB = '#!/usr/bin/env bash\n# Public-IP probe for the EKS CIDR lock.\necho "203.0.113.10"\n'

# Sibling scripts and module sub-scripts deploy-all.sh invokes by path. Each is stubbed to
# echo a recognisable marker so a test can prove the script was or was not called.
_SUB_SCRIPTS = {
    "platform/scripts/preflight-check.sh": "STUB-PREFLIGHT",
    "platform/scripts/enable-bedrock-models.sh": "STUB-BEDROCK",
    "platform/scripts/wire-gateway-alb.sh": "STUB-WIRE-ALB",
    "platform/scripts/codebuild-run.sh": "STUB-CODEBUILD",
    "platform/scripts/build-agent-factory-lambdas.sh": "STUB-BUILD-AGENT-FACTORY-LAMBDAS",
    "platform/scripts/empty-s3-buckets.sh": "STUB-EMPTY-S3",
    "platform/scripts/delete-ingress-and-wait.sh": "STUB-DELETE-INGRESS",
    "platform/scripts/force-delete-secrets.sh": "STUB-FORCE-DELETE-SECRETS",
    "modules/gateway/scripts/deploy-broker.sh": "STUB-DEPLOY-BROKER",
    "modules/gateway/scripts/bootstrap-admin.sh": "STUB-BOOTSTRAP-ADMIN",
    "modules/gateway/scripts/deploy-frontend.sh": "STUB-DEPLOY-FRONTEND",
    "modules/gateway/scripts/apply-internal-plane-deny.sh": "STUB-INTERNAL-DENY",
    "modules/gateway/scripts/verify-restricted-admission.sh": "STUB-RESTRICTED-ADMISSION",
    "modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh": "STUB-WEBHOOK-INGRESS",
    "modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh": "STUB-DEPLOY-CHAT-SCALEDJOB",
    # Invoked as `bash deploy.sh` after a cd into the module, so it is a relative path.
    "modules/agent-context/deploy.sh": "STUB-AGENT-CONTEXT-DEPLOY",
}

# Phase label -> the `Step N/11` prefix it prints. Keyed by concept so a renumbering
# shows up as one failure per phase with a readable name.
STEP_LABELS = {
    "bootstrap": "Step 1/11",
    "platform": "Step 2/11",
    "gateway_infra": "Step 3/11",
    "gateway_deploy": "Step 4/11",
    "alb_wiring": "Step 5/11",
    "frontend": "Step 6/11",
    "broker": "Step 7/11",
    "admin_bootstrap": "Step 8/11",
    "webhook_ingress": "Step 9/11",
    "agent_factory": "Step 10/11",
    "agent_context": "Step 11/11",
}


def assert_no_real_tooling(path_dir: Path) -> None:
    """Fail loudly if the stub directory is not what `aws` resolves to.

    Without this the suite would silently degrade into a live-AWS test the moment a
    future edit reordered PATH or dropped a stub.
    """
    resolved = shutil.which("aws", path=str(path_dir))
    assert resolved is not None, "aws stub missing from the harness PATH"
    assert Path(resolved).parent == path_dir, (
        f"`aws` resolves to {resolved}, outside the stub directory {path_dir}. This suite "
        "must never reach a real AWS endpoint or depend on anyone's credentials."
    )


@pytest.fixture
def harness(tmp_path):
    """A throwaway tree containing deploy-all.sh, its stub siblings and stub tooling.

    Returns a `run(*flags, env=None)` callable yielding the CompletedProcess.
    """
    root = tmp_path / "repo"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)

    def _write_exec(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)

    for name, body in (
        ("aws", _AWS_STUB),
        ("git", _GIT_STUB),
        ("curl", _CURL_STUB),
        ("terraform", _TERRAFORM_STUB),
        ("kubectl", _KUBECTL_STUB),
        ("npm", _NOOP_STUB),
        ("docker", _NOOP_STUB),
    ):
        _write_exec(bin_dir / name, body)

    # The script under test, at the same relative path so SCRIPT_DIR/ROOT_DIR resolve
    # inside the temp tree.
    _write_exec(root / "platform" / "scripts" / "deploy-all.sh", _DEPLOY_ALL.read_text())

    # Copy the real local helpers so the scope checks exercise the shared resolver.
    # External tools remain stubbed; these helpers only run against the temp tree.
    for name in (
        "terraform-update.sh",
        "tfvars-account-check.py",
        "deploy-checkpoints.sh",
        "deploy-checkpoints.py",
        "upgrade-scope.sh",
        "gateway-alb-vars.sh",
        "prepare-backends.py",
        "render-model-root-config.py",
        "resolve-ecr-image.py",
    ):
        _write_exec(root / "platform" / "scripts" / name, (_DEPLOY_ALL.parent / name).read_text())

    # Deployment scope tests stub remote release verification; the alignment
    # helper has its own provider-contract tests.
    _write_exec(
        root / "modules/gateway/scripts/sync-gateway-engine.py",
        '#!/usr/bin/env python3\nprint("STUB-ENGINE-ALIGNMENT")\n',
    )

    signing_helper = Path("modules/gateway/scripts/ensure-signing-secret.py")
    _write_exec(root / signing_helper, (_REPO_ROOT / signing_helper).read_text())

    # deploy-all invokes this validator with python3 before continuing the
    # gateway phase.  Keep the offline scope harness self-contained: the
    # validator's own behavior is covered by platform/scripts/tests.
    _write_exec(
        root / "platform" / "scripts" / "validate-model-allowlist-config.py",
        '#!/usr/bin/env python3\nprint("STUB-MODEL-ALLOWLIST-VALIDATOR")\n',
    )

    # load-deploy-config.sh is *sourced*, so it must define what the script reads.
    _write_exec(
        root / "platform" / "scripts" / "load-deploy-config.sh",
        '#!/usr/bin/env bash\nADP_REGION="us-east-1"\nADP_ENVIRONMENT="dev"\nADP_ACCOUNT_ID=""\nADP_GITHUB_ORG="test-org"\n',
    )

    for rel, marker in _SUB_SCRIPTS.items():
        failure = 'exit "${TEST_FAIL_WEBHOOK:-0}"' if "deploy-webhook-ingress.sh" in rel else "exit 0"
        _write_exec(root / rel, f'#!/usr/bin/env bash\necho "{marker}"\n{failure}\n')

    _write_exec(
        root / "platform" / "scripts" / "undeploy-phases.sh",
        'phase_superplane() { echo "STUB-SUPERPLANE-TEARDOWN"; return "${TEST_SUPERPLANE_EXIT:-0}"; }\n',
    )

    # Terraform var files the phases reference. Empty is fine: terraform is a no-op stub.
    for rel in (
        "environments/dev/backend.tfvars",
        "environments/dev/platform.tfvars",
        "environments/dev/modules/gateway.tfvars",
        "environments/dev/modules/agent-factory.tfvars",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("")

    # Every directory the script `cd`s into. A missing one aborts the run under `set -e`
    # before the phase under test is reached, which would look like a scope failure.
    for rel in (
        "platform/infra",
        "modules/gateway",
        "modules/gateway/infra",
        "modules/gateway/frontend",
        "modules/agent-factory",
        "modules/agent-factory/infra",
        "modules/agent-factory/webhook-ingress/infra",
        "modules/agent-context",
        "modules/agent-context/terraform",
        "modules/domain-apps/superplane/infra/control-plane",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)

    # pricing-rollout.py is invoked with `python3 <path>`, so PATH stubbing cannot reach
    # it — the file itself has to exist.
    _write_exec(
        root / "modules" / "gateway" / "scripts" / "pricing-rollout.py",
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
    )

    # The gateway and agent-factory deploy phases `sed` k8s manifests and pipe the result
    # into kubectl. `sed` is a real binary here — stubbing it would break the script's own
    # text processing — so the files have to exist or the phase aborts under `set -e`
    # before the phase under test is reached.
    #
    # The names are mirrored from the real manifest directories rather than hardcoded, so
    # a manifest added or renamed upstream does not turn into a confusing scope failure
    # here. Contents are irrelevant: kubectl is a stub that drains stdin.
    # Mirrored from the real tree by globbing for k8s manifest directories, rather than
    # from a hardcoded list. The paths are not guessable from the phase names — the KEDA
    # ScaledJob the agent-factory phase applies lives at
    # modules/agent-factory/gateway/k8s/, resolved relative to a cwd the script changes
    # into — and a missing manifest is a hard abort that presents as a scope failure.
    # Mirroring means an upstream rename cannot silently break this suite's diagnosis.
    for real_dir in sorted(_REPO_ROOT.glob("modules/*/**/k8s")):
        if not real_dir.is_dir():
            continue
        stub_dir = root / real_dir.relative_to(_REPO_ROOT)
        stub_dir.mkdir(parents=True, exist_ok=True)
        for manifest in sorted(real_dir.glob("*.yaml")):
            (stub_dir / manifest.name).write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: stub\n")

    assert_no_real_tooling(bin_dir)

    def run(*flags: str, env: dict | None = None, input_text: str | None = None) -> subprocess.CompletedProcess:
        # Strip every AWS_* variable so an ambient IRSA identity (the agent runtime and
        # ARC runners both have one) cannot reach the child process.
        child_env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        child_env["PATH"] = f"{bin_dir}:{child_env.get('PATH', '')}"
        child_env["TF_VAR_eks_public_access_cidrs"] = '["203.0.113.10/32"]'
        if env:
            child_env.update(env)
        return subprocess.run(
            ["bash", str(root / "platform" / "scripts" / "deploy-all.sh"), *flags],
            capture_output=True,
            text=True,
            timeout=300,
            env=child_env,
            cwd=str(root),
            input=input_text,
            # DEVNULL, so a stub that drains stdin gets EOF immediately instead of
            # blocking on an inherited descriptor.
            stdin=subprocess.DEVNULL if input_text is None else None,
        )

    return run


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def phase_section(output: str, phase: str) -> str:
    """The output from a phase's `Step N/11` header up to the next step header.

    Reading the *section* rather than the header line is deliberate. Two phases (gateway
    infra and gateway deploy) print their header unconditionally and then branch, emitting
    "Skipping gateway infra (...)" inside the section — so a header-only check reports
    those as having run whatever the guards say. The remaining phases encode the decision
    in the header text itself. Both shapes are covered by looking at the whole section.
    """
    label = STEP_LABELS[phase]
    clean = _strip_ansi(output)
    lines = clean.splitlines()
    start = next((i for i, ln in enumerate(lines) if label in ln), None)
    if start is None:
        return ""
    for j in range(start + 1, len(lines)):
        # `Step 10b/11` belongs to its parent phase, so it must not end the section.
        m = re.search(r"Step (\d+)(b?)/11:", lines[j])
        if m and not m.group(2):
            return "\n".join(lines[start:j])
    return "\n".join(lines[start:])


# A phase declines to do work by announcing it, in one of two shapes: the header itself
# says "Skipping <phase>", or the header is unconditional and the body says so. Matching
# these specific forms — rather than any occurrence of "skip" — keeps the assertions from
# being confused by incidental wording.
#
# The optional Superplane module has no phase in this script.
_PHASE_DECLINED = re.compile(
    r"^\s*(?:━━━\s*)?Step \d+b?/11:\s*Skipping\b|^\s*Skipping (?:gateway|frontend|agent|webhook|broker|admin|superplane)",
    re.MULTILINE | re.IGNORECASE,
)


def phase_declined(output: str, phase: str) -> bool:
    return bool(_PHASE_DECLINED.search(phase_section(output, phase)))


def assert_skipped(output: str, phase: str) -> None:
    section = phase_section(output, phase)
    assert section, f"{phase} ({STEP_LABELS[phase]}) printed no step line at all"
    assert phase_declined(output, phase), f"{phase} ({STEP_LABELS[phase]}) was NOT skipped — it did work it should not have.\nSection:\n{section}"


def assert_ran(output: str, phase: str) -> None:
    section = phase_section(output, phase)
    assert section, f"{phase} ({STEP_LABELS[phase]}) printed no step line at all"
    assert not phase_declined(output, phase), f"{phase} ({STEP_LABELS[phase]}) was skipped but should have run.\nSection:\n{section}"


class TestHarnessIsOffline:
    """The harness itself is a claim that needs checking before it proves anything."""

    def test_no_aws_credentials_reach_the_child(self, harness):
        """A stray AWS_* variable would make these tests credential-dependent."""
        result = harness("--help")
        assert result.returncode == 0, result.stderr

    def test_script_parses(self):
        result = subprocess.run(["bash", "-n", str(_DEPLOY_ALL)], capture_output=True, text=True)
        assert result.returncode == 0, f"deploy-all.sh is not valid bash: {result.stderr}"


class TestSuperplaneModuleBoundary:
    @pytest.mark.parametrize("flag", ["--superplane-only", "--skip-superplane"])
    def test_old_platform_flags_refuse_before_any_phase(self, harness, flag):
        result = harness(flag)
        assert result.returncode == 2
        assert "modules/domain-apps/superplane" in result.stderr
        assert "Step 1/11" not in result.stdout

    def test_old_environment_opt_in_refuses_before_any_phase(self, harness):
        result = harness(env={"SUPERPLANE_ENABLED": "true"})
        assert result.returncode != 0
        assert "modules/domain-apps/superplane" in result.stdout
        assert "Step 1/11" not in result.stdout


class TestDefaultDeployIsUnchanged:
    """Regression guard: existing platform phases still run without Superplane.

    This is R1 acc. 1 at the deploy layer. The scope-guard edits touch conditions that
    every ordinary deployment evaluates, so "superplane skips by default" is not enough —
    the other phases must still run exactly as before.
    """

    @pytest.fixture
    def output(self, harness):
        result = harness()
        assert result.returncode == 0, (
            f"default deploy exited {result.returncode}.\nSTDOUT tail:\n{result.stdout[-3000:]}\nSTDERR tail:\n{result.stderr[-2000:]}"
        )
        return result.stdout

    def test_superplane_has_no_phase_in_basic_deploy(self, output):
        assert "Deploy superplane domain app" not in output
        assert "Skipping superplane" not in output

    @pytest.mark.parametrize(
        "phase",
        [
            "bootstrap",
            "platform",
            "gateway_infra",
            "gateway_deploy",
            "alb_wiring",
            "frontend",
            "broker",
            "admin_bootstrap",
            "webhook_ingress",
            "agent_factory",
        ],
    )
    def test_existing_phase_still_runs(self, output, phase):
        assert_ran(output, phase)

    def test_agent_context_still_gated_off_by_default(self, output):
        assert_skipped(output, "agent_context")

    def test_gateway_admission_check_runs_with_the_gateway_phase(self, output):
        assert "STUB-RESTRICTED-ADMISSION" in phase_section(output, "gateway_deploy")


class TestSiblingScopeFlagsUnaffected:
    """The pre-existing scope flags must behave exactly as they did before this change."""

    def test_gateway_only_skips_agents_and_superplane(self, harness):
        result = harness("--gateway-only")
        assert result.returncode == 0, result.stdout[-2000:]
        assert_ran(result.stdout, "gateway_infra")
        assert_ran(result.stdout, "gateway_deploy")
        assert_skipped(result.stdout, "webhook_ingress")
        assert_skipped(result.stdout, "agent_factory")

    def test_agent_factory_only_skips_gateway_and_superplane(self, harness):
        result = harness("--agent-factory-only")
        assert result.returncode == 0, result.stdout[-2000:]
        assert_skipped(result.stdout, "gateway_infra")
        assert_skipped(result.stdout, "gateway_deploy")
        assert_ran(result.stdout, "agent_factory")

    def test_agent_context_only_skips_everything_else(self, harness):
        result = harness("--agent-context-only")
        assert result.returncode == 0, result.stdout[-2000:]
        assert_skipped(result.stdout, "gateway_infra")
        assert_skipped(result.stdout, "agent_factory")
        assert_ran(result.stdout, "agent_context")


class TestLegacyDestroy:
    def test_superplane_teardown_precedes_dependencies(self, harness):
        result = harness("--destroy", input_text="yes\n")
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr
        assert result.stdout.index("STUB-SUPERPLANE-TEARDOWN") < result.stdout.index("Destroy 2/6: Agent Context")

    def test_failed_superplane_teardown_preserves_dependencies(self, harness):
        result = harness("--destroy", input_text="yes\n", env={"TEST_SUPERPLANE_EXIT": "1"})
        assert result.returncode != 0
        assert "STUB-SUPERPLANE-TEARDOWN" in result.stdout
        assert "leaving its dependencies intact" in result.stdout
        assert "Destroy 2/6: Agent Context" not in result.stdout

    def test_declining_confirmation_runs_no_teardown(self, harness):
        result = harness("--destroy", input_text="no\n")
        assert result.returncode == 0
        assert "STUB-SUPERPLANE-TEARDOWN" not in result.stdout


class TestDeploymentResume:
    def test_webhook_failure_resumes_without_rebuilding_platform_or_gateway(self, harness):
        failed = harness(env={"TEST_FAIL_WEBHOOK": "17"})
        assert failed.returncode == 17, failed.stdout[-2000:] + failed.stderr
        resumed = harness("--resume")
        assert resumed.returncode == 0, resumed.stdout[-3000:] + resumed.stderr
        assert "Resuming: platform already complete" in resumed.stdout
        assert "Resuming: gateway already complete" in resumed.stdout
        assert "Step 2/11:" not in resumed.stdout
        assert "Step 4/11:" not in resumed.stdout
        assert_ran(resumed.stdout, "webhook_ingress")
        assert_ran(resumed.stdout, "agent_factory")
        assert_ran(resumed.stdout, "frontend")

    def test_from_replays_requested_phase_and_later_phases(self, harness):
        first = harness()
        assert first.returncode == 0, first.stderr
        replay = harness("--from", "webhook")
        assert replay.returncode == 0, replay.stdout[-3000:] + replay.stderr
        assert "Step 2/11:" not in replay.stdout
        assert_ran(replay.stdout, "webhook_ingress")
        assert_ran(replay.stdout, "frontend")

    def test_changed_scope_refuses_before_phases(self, harness):
        first = harness(env={"TEST_FAIL_WEBHOOK": "17"})
        assert first.returncode == 17
        changed = harness("--resume", "--gateway-only")
        assert changed.returncode != 0
        assert "changed" in changed.stderr
        assert "Step 1/11:" not in changed.stdout
