"""Superplane is registered in every central list that governs it (#5037).

R1 acc. 3 is about a class of defect rather than a behaviour: several hardcoded central
lists live outside the module directory, so somebody working inside `modules/domain-apps/
superplane/` cannot see them and therefore misses one. `modules/domain-apps/cyber/` is the
worked example — it is absent from `deploy-all.sh` entirely, so its resources survive
teardown and bill silently.

Every assertion here reads a file as **text**, because none of these lists is importable
Python: they are Bash arrays, a GitHub Actions workflow, a TypeScript module and a Markdown
table. No runtime test can observe any of them, which is precisely why a PR that lands two
of the five edits goes green everywhere else.

**The undeploy pairing is the sharp edge.** `undeploy.sh` does not define its phase
functions. `_run_phase` builds the name dynamically (`local phase_fn="phase_${phase}"`) and
calls it, with the bodies in the separately-sourced `undeploy-phases.sh`. So a name in
`PHASE_ORDER` without a matching function is not a lint error — it is a teardown that fails
on every run, retries twice and reports FAILED. The issue's own registration inventory lists
four deploy/undeploy edit points and does not mention `undeploy-phases.sh`; this suite pins
it as the fifth.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
import yaml

# tests/features/<file> -> tests -> gateway -> modules -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEPLOY_ALL = _REPO_ROOT / "platform" / "scripts" / "deploy-all.sh"
_UNDEPLOY = _REPO_ROOT / "platform" / "scripts" / "undeploy.sh"
_UNDEPLOY_PHASES = _REPO_ROOT / "platform" / "scripts" / "undeploy-phases.sh"
_UNDEPLOY_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "undeploy.yml"
_MANIFEST = _REPO_ROOT / "docs" / "adp-platform-deployment" / "deployment-manifest.md"
_MODULE_DIR = _REPO_ROOT / "modules" / "domain-apps" / "superplane"
_CI_LANE = _REPO_ROOT / ".github" / "workflows" / "superplane-domain-ci.yml"

PHASE = "superplane"


class TestModuleLayout:
    """The directories the design note specifies, and none it forbids."""

    # Design §3 lines 149-162.
    EXPECTED = [
        "agent/personas",
        "agent/skills",
        "tools/superplane-mcp",
        "cli",
        "contracts",
        "integrations/mlflow",
        "ui",
        "events",
        "infra/control-plane",
        "infra/workspaces",
        "releases",
        "tests/acceptance",
    ]

    def test_every_specified_directory_exists(self):
        missing = [d for d in self.EXPECTED if not (_MODULE_DIR / d).is_dir()]
        assert not missing, f"Missing directories from design §3 lines 149-162: {missing}"

    def test_no_api_directory(self):
        """Design §3 line 171: domain server routes and migrations stay upstream.

        Asserted as an absence because revision 3 of the design explicitly withdrew a
        second writable ADP-hosted domain service. An `api/` appearing here would be that
        service arriving by accident, one directory at a time.
        """
        assert not (_MODULE_DIR / "api").exists(), (
            "modules/domain-apps/superplane/api/ must not exist: new domain server routes "
            "and migrations live beside the Superplane API upstream (design §3 line 171)."
        )


class TestDeployRegistration:
    """`deploy-all.sh` gates the phase off by default and offers explicit flags."""

    def test_gate_variable_defaults_false(self):
        body = _DEPLOY_ALL.read_text()
        assert 'SUPERPLANE_ENABLED="${SUPERPLANE_ENABLED:-false}"' in body, (
            "SUPERPLANE_ENABLED must default to false, following the AGENT_CONTEXT_ENABLED "
            "precedent. A default-true gate deploys the domain app in every environment."
        )

    def test_enable_and_skip_flags_exist(self):
        body = _DEPLOY_ALL.read_text()
        for flag in ("--superplane-only", "--skip-superplane"):
            assert flag in body, f"{flag} missing from deploy-all.sh argument parsing"

    def test_phase_is_present_and_conditional(self):
        body = _DEPLOY_ALL.read_text()
        assert "DEPLOY_SUPERPLANE=false" in body, "No DEPLOY_SUPERPLANE gate computation found"
        assert 'if [ "$DEPLOY_SUPERPLANE" = true ]; then' in body, "The superplane deploy step must be guarded by DEPLOY_SUPERPLANE"

    def test_step_denominators_are_consistent(self):
        """Adding a 12th phase must renumber the `Step N/M` labels.

        These labels are display-only, but a run that prints "Step 12/11" tells an
        operator the script is confused about its own phase count.
        """
        body = _DEPLOY_ALL.read_text()
        denominators = set(re.findall(r"Step \d+/(\d+)", body))
        assert denominators == {"12"}, f"Inconsistent step denominators in deploy-all.sh: {denominators}"
        numerators = {int(n) for n in re.findall(r"Step (\d+)/12", body)}
        assert max(numerators) == 12, f"Expected a Step 12/12; highest numerator is {max(numerators)}"


class TestUndeployRegistration:
    """The phase is in PHASE_ORDER, has a function, and both arrays stay aligned."""

    def test_phase_order_contains_superplane_first(self):
        """First in destroy order, because deploy order is the reverse."""
        body = _UNDEPLOY.read_text()
        match = re.search(r"^PHASE_ORDER=\(([^)]*)\)", body, re.MULTILINE)
        assert match, "Could not find PHASE_ORDER in undeploy.sh"
        phases = match.group(1).split()
        assert PHASE in phases, f"{PHASE} missing from PHASE_ORDER: {phases}"
        assert phases[0] == PHASE, (
            f"{PHASE} must be the FIRST undeploy phase (got {phases}). A domain app sits on "
            "top of the platform, gateway and agent runtime, so it must be destroyed before "
            "them — destroying the platform first would orphan its resources."
        )
        assert phases.index(PHASE) < phases.index("agent_context")

    def test_phase_function_is_defined(self):
        """The pairing the issue's inventory omits.

        `_run_phase` calls `phase_${phase}` dynamically, so a PHASE_ORDER entry without a
        function here fails on every teardown rather than at lint time.
        """
        body = _UNDEPLOY_PHASES.read_text()
        assert re.search(rf"^phase_{PHASE}\(\) \{{", body, re.MULTILINE), (
            f"phase_{PHASE}() is not defined in undeploy-phases.sh, but {PHASE} is in "
            "PHASE_ORDER. undeploy.sh resolves phase functions by name at run time, so this "
            "combination fails on every teardown and reports FAILED after two retries."
        )

    def test_estimated_time_array_stays_aligned(self):
        """`PHASE_ESTIMATED_TIME` is indexed positionally against `PHASE_ORDER`.

        A missing entry does not error — it silently shifts every later phase's estimate by
        one, so each phase reports its neighbour's duration.
        """
        body = _UNDEPLOY.read_text()
        order = re.search(r"^PHASE_ORDER=\(([^)]*)\)", body, re.MULTILINE).group(1).split()
        times = re.search(r"^PHASE_ESTIMATED_TIME=\((.*?)^\)", body, re.MULTILINE | re.DOTALL).group(1)
        entries = re.findall(r'"[^"]+"', times)
        assert len(entries) == len(order), (
            f"PHASE_ESTIMATED_TIME has {len(entries)} entries but PHASE_ORDER has {len(order)}. "
            "The two are read by the same index, so a mismatch misreports every later phase."
        )

    def test_dry_run_report_covers_the_phase(self):
        """Without a case arm the dry-run prints an empty block for this phase."""
        body = _UNDEPLOY.read_text()
        assert re.search(rf"^\s+{PHASE}\)$", body, re.MULTILINE), (
            f"No `{PHASE})` arm in undeploy.sh's dry-run case statement; a dry run would "
            "print the phase header with no detail, reading as 'nothing will happen'."
        )

    def test_workflow_registers_the_phase(self):
        body = _UNDEPLOY_WORKFLOW.read_text()
        assert f"phase_{PHASE}" in body, f"undeploy.yml never calls phase_{PHASE}"
        assert f'is_skipped "{PHASE}"' in body, f"undeploy.yml has no skip check for {PHASE}"
        assert f"{PHASE},agent-context" in body, "The skip_phases input description must list superplane first, matching destroy order"

    def test_workflow_phase_labels_are_consistent(self):
        body = _UNDEPLOY_WORKFLOW.read_text()
        denominators = set(re.findall(r"Phase \d+/(\d+):", body))
        assert denominators == {"6"}, f"Inconsistent phase denominators in undeploy.yml: {denominators}"


class TestManifestRegistration:
    """The manifest carries a validation row, per the repo's resource→validation contract."""

    def test_manifest_has_a_superplane_section(self):
        body = _MANIFEST.read_text()
        assert "## Superplane Domain App" in body, "deployment-manifest.md has no Superplane section"

    def test_manifest_documents_the_default_off_gate(self):
        body = _MANIFEST.read_text()
        section = body.split("## Superplane Domain App", 1)[1].split("\n## ", 1)[0]
        assert "SUPERPLANE_ENABLED" in section, "Manifest section must name the deploy gate"
        assert "FEATURE_SUPERPLANE_ENABLED" in section, "Manifest section must name the runtime flag"
        assert "phase_superplane" in section, "Manifest section must name the undeploy phase function, since that is the pairing most easily missed"


class TestOfflineCiLane:
    """The lane U12/U2/U8 depend on exists, filters this module, and needs no AWS."""

    def test_lane_exists(self):
        assert _CI_LANE.is_file(), (
            ".github/workflows/superplane-domain-ci.yml is missing. U12, U2 and U8 declare it "
            "as a required check, so it must exist before they are dispatched."
        )

    def test_lane_filters_the_module_path(self):
        body = _CI_LANE.read_text()
        assert "modules/domain-apps/superplane/**" in body, (
            "The lane must filter on modules/domain-apps/superplane/** so it triggers on changes to the module it gates"
        )

    def test_lane_declares_no_aws_credentials(self):
        """An offline lane that acquires credentials is no longer offline.

        Parsed structurally rather than grepped as text. A substring search over the whole
        file cannot tell a credential *step* from a comment explaining the ARC runner's
        identity and the offline test environment. Comments are not credential setup.

        So this checks the executable surface — job steps and permissions — and leaves
        comments alone.
        """
        lane = yaml.safe_load(_CI_LANE.read_text())
        job = lane["jobs"]["superplane-domain-tests"]

        # No OIDC token can be minted: without id-token there is nothing to exchange for
        # a role, whatever a step asks for.
        permissions = lane.get("permissions") or {}
        assert "id-token" not in permissions, (
            "superplane-domain-ci.yml grants id-token permission; the lane must not be able "
            "to assume an AWS role via OIDC. It runs lint and unit tests only."
        )

        for step in job["steps"]:
            uses = step.get("uses", "")
            assert "configure-aws-credentials" not in uses, (
                f"superplane-domain-ci.yml step {step.get('name', uses)!r} configures AWS "
                "credentials; the lane must not acquire them — it runs lint and unit tests only."
            )
            assert "role-to-assume" not in str(step.get("with") or {}), (
                f"superplane-domain-ci.yml step {step.get('name', uses)!r} names a role to assume."
            )

    @pytest.mark.parametrize(
        ("workflow", "job_name"),
        [
            ("superplane-domain-ci.yml", "superplane-domain-tests"),
            ("superplane-infra-plan.yml", "tests"),
        ],
    )
    def test_self_hosted_tests_disable_aws_discovery(self, workflow, job_name, monkeypatch, tmp_path):
        """Owner-selected ARC jobs must not inherit credentials into normal SDK calls.

        Exercise the actual workflow environment against boto3 with valid-looking
        ambient sources. The runner still has identity; this checks provider discovery,
        not isolation against code deliberately opening a mounted service-account token.
        """
        lane = yaml.safe_load((_CI_LANE.parent / workflow).read_text())
        job = lane["jobs"][job_name]
        assert job["runs-on"] == "arc-runner-org"
        assert "id-token" not in (job.get("permissions") or lane.get("permissions") or {})

        credentials = tmp_path / "credentials"
        credentials.write_text("[default]\naws_access_key_id = test-key\naws_secret_access_key = test-secret\n")
        config = tmp_path / "config"
        config.write_text("[profile ambient]\ncredential_process = false\n")
        token = tmp_path / "token"
        token.write_text("test-token")
        ambient = {
            "AWS_ACCESS_KEY_ID": "test-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
            "AWS_SESSION_TOKEN": "test-session",
            "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/test",
            "AWS_WEB_IDENTITY_TOKEN_FILE": str(token),
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:9/credentials",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/credentials",
            "AWS_CONFIG_FILE": str(config),
            "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
            "AWS_PROFILE": "ambient",
            "AWS_DEFAULT_PROFILE": "ambient",
        }
        for name, value in ambient.items():
            monkeypatch.setenv(name, value)
        # Without the job environment, boto3 really can find the inherited key.
        assert boto3.Session().get_credentials().access_key == "test-key"

        effective_env = {**(lane.get("env") or {}), **job["env"]}
        for name, value in effective_env.items():
            monkeypatch.setenv(name, str(value))
        assert effective_env["AWS_CONFIG_FILE"] == "/dev/null"
        assert effective_env["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
        assert effective_env.get("AWS_REGION") == "us-east-1"
        assert effective_env.get("AWS_DEFAULT_REGION") == "us-east-1"

        # Execute the actual default shell used by every test step.
        script = tmp_path / "step.sh"
        command = [part.replace("{0}", str(script)) for part in shlex.split(job["defaults"]["run"]["shell"])]
        probe = "import boto3; assert boto3.Session().get_credentials() is None"
        script.write_text(f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}\n")
        subprocess.run(command, check=True)
        guard = next(s for s in job["steps"] if s.get("name") == "Verify no AWS credentials were configured")
        script.write_text(guard["run"])
        subprocess.run(command, check=True)

    def test_lane_is_pinned_to_main(self):
        """All sibling `pull_request` lanes pin `branches: [main]`; this one must too."""
        lane = yaml.safe_load(_CI_LANE.read_text())
        # `on:` parses as the boolean True in YAML 1.1, so accept either key.
        triggers = lane.get("on") or lane.get(True)
        assert triggers["pull_request"].get("branches") == ["main"], (
            "superplane-domain-ci.yml's pull_request trigger must pin branches: [main], matching every other pull_request lane in the repo."
        )
