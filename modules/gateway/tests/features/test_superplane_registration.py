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

import ast
import json
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


def _strip_jsonc_comments(text: str) -> str:
    """Drop `//` comments so a tsconfig can be parsed as JSON.

    TypeScript accepts comments in tsconfig.json and the file uses them to record
    why its path mappings exist, but `json.loads` rejects them. Only whole-line
    comments are stripped, which is all the file has; doing it generally would
    need to respect string literals, and a path value containing `//` would then
    be silently truncated.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


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


class TestDomainUiBuildWiring:
    """The domain UI is only really domain-owned if the toolchain follows it.

    Superplane's onboarding interface lives in `ui/` and is consumed by the
    Gateway SPA through an alias. That indirection has a specific failure mode
    worth testing: every link can break WITHOUT any test failing, because a test
    file that is never collected and a workflow lane that never triggers both look
    exactly like success. The move would then be cosmetic — the code would sit in
    the domain directory while nothing checked it.

    So each link in the chain is pinned here (#5730).
    """

    UI_DIR = _MODULE_DIR / "ui"
    FRONTEND = _REPO_ROOT / "modules" / "gateway" / "frontend"
    GATEWAY_LANE = _REPO_ROOT / ".github" / "workflows" / "gateway-ci.yml"
    ALIAS = "@superplane-ui"
    # Relative to the frontend directory, as both configs express it.
    UI_FROM_FRONTEND = "../../domain-apps/superplane/ui"

    def test_the_onboarding_modules_live_in_the_domain_app(self):
        """Not in the Gateway's own src/, which is where they started."""
        for module in ("contract.ts", "client.ts", "operations.ts", "readiness.ts", "OnboardingView.tsx"):
            assert (self.UI_DIR / module).is_file(), f"ui/{module} is missing from the domain app"
        assert not (self.FRONTEND / "src" / "superplane").exists(), (
            "modules/gateway/frontend/src/superplane/ must not exist: the domain UI belongs to "
            "the domain app, and a copy here is the drift this move removes."
        )

    def test_the_gateway_page_only_mounts_the_domain_ui(self):
        """The Gateway keeps the route and the session; not the interface.

        A relative import reaching into the domain app would work at runtime and
        quietly re-establish the Gateway as the owner of these components.
        """
        page = (self.FRONTEND / "src" / "pages" / "Superplane.tsx").read_text()
        assert f"{self.ALIAS}/OnboardingView" in page, "Superplane.tsx must mount the domain UI through the alias."
        assert "domain-apps" not in page, "Superplane.tsx must not reach into the domain app by relative path; use the alias."

    @pytest.mark.parametrize("config", ["vite.config.ts", "vitest.config.ts"])
    def test_build_configs_agree_on_the_domain_ui_alias(self, config):
        """Both, or the application and its tests resolve different code.

        vite.config.ts and vitest.config.ts do not share a resolve block, so an
        alias added to one and not the other produces tests that pass against a
        module the shipped bundle cannot resolve — a green suite and a broken page.
        """
        body = (self.FRONTEND / config).read_text()
        assert self.ALIAS in body, f"{config} is missing the {self.ALIAS} alias"
        assert self.UI_FROM_FRONTEND in body, f"{config}'s alias must point at {self.UI_FROM_FRONTEND}"

    def test_vitest_collects_the_domain_ui_tests(self):
        """The include glob, without which the domain tests silently do not run.

        This is the load-bearing one. `test.root` anchors the globs to the
        frontend directory, so domain UI tests are simply invisible to the runner
        unless they are named — and an uncollected file reports nothing at all.
        """
        body = (self.FRONTEND / "vitest.config.ts").read_text()
        assert f"{self.UI_FROM_FRONTEND}/**/*.{{test,spec}}.{{ts,tsx}}" in body, (
            "vitest.config.ts's test.include must cover the domain UI, or its tests are "
            "never collected and the suite reports green without running them."
        )

    def test_vitest_may_read_the_domain_app(self):
        """Vite refuses to serve files outside its root unless allowed."""
        body = (self.FRONTEND / "vitest.config.ts").read_text()
        assert "fs:" in body and "allow" in body, (
            "vitest.config.ts must widen server.fs.allow to the domain app, or every domain UI test is collected and then fails to import."
        )

    def test_typecheck_covers_the_domain_ui(self):
        """tsc `include`, plus the React type mapping it needs to be meaningful.

        The domain app has no node_modules, so a bare `react` import is
        unresolvable there. Mapping it to the runtime package instead of the type
        package is the subtle wrong answer: tsc then resolves the module and
        degrades every React value to implicit `any` under strict mode.
        """
        config = json.loads(_strip_jsonc_comments((self.FRONTEND / "tsconfig.json").read_text()))
        assert self.UI_FROM_FRONTEND in config["include"], "tsconfig.json's include must cover the domain UI, or it is never typechecked."
        paths = config["compilerOptions"]["paths"]
        assert paths.get("react") == ["node_modules/@types/react"], (
            "tsconfig paths must map react to @types/react. Mapping it to node_modules/react "
            "resolves a package with no declarations, which makes every React value implicitly "
            "`any` instead of failing."
        )

    def test_lint_covers_the_domain_ui(self):
        """Its own flat config, invoked by the frontend's lint script.

        ESLint's flat config refuses files outside its own directory, so passing
        the domain app to the frontend's `eslint .` exits non-zero having linted
        nothing — a failure that reads like a lint error.
        """
        assert (self.UI_DIR / "eslint.config.js").is_file(), (
            "the domain UI needs its own eslint.config.js: flat config cannot lint outside its base path."
        )
        scripts = json.loads((self.FRONTEND / "package.json").read_text())["scripts"]
        assert "lint:superplane-ui" in scripts["lint"], "the frontend lint script must also lint the domain UI."

    def test_the_gateway_lane_triggers_on_domain_ui_changes(self):
        """The check that makes "affected frontend CI passes" verifiable.

        The superplane domain lane matches this path too, but it has no Node
        toolchain — so without this entry a UI-only pull request runs that lane's
        Python and Go steps, reports green, and executes no frontend check at all.
        """
        lane = yaml.safe_load(self.GATEWAY_LANE.read_text())
        triggers = lane.get("on") or lane.get(True)
        paths = triggers["pull_request"]["paths"]
        assert "modules/domain-apps/superplane/ui/**" in paths, (
            "gateway-ci.yml's pull_request paths must include the domain UI, or a UI-only change never runs the frontend tests that cover it."
        )

    def test_the_gateway_lane_typechecks(self):
        """Vitest alone cannot catch a broken type mapping; tsc can."""
        lane = yaml.safe_load(self.GATEWAY_LANE.read_text())
        steps = lane["jobs"]["frontend-test"]["steps"]
        assert any("tsc --noEmit" in (step.get("run") or "") for step in steps), (
            "the frontend lane must run tsc --noEmit: a tsconfig path regression degrades React types to `any` without failing a single test."
        )


class TestVaultCredentialFixturesMatchTheRealSchema:
    """The onboarding clients' vault fixtures must match the server's own model.

    WHY THIS CLASS EXISTS (#5730)
    -----------------------------
    Both onboarding clients shipped a defect that a full green suite could not
    see: they read a `credential_id` field off `GET /vault/credentials` rows. That
    field does not exist. The real rows carry `id` (the registry's row key) and
    `adp_credential_id` (the vault handle), and the server matches a submitted
    reference against the latter:

        CredentialRegistry.adp_credential_id == credential_id

    The browser tests passed because their stub emitted the shape the client
    expected, and the CLI tests passed for the same reason. A fixture written from
    the client's assumption agrees with the client's bug, so it can only ever
    confirm it.

    These tests therefore read the server's Pydantic model off disk and assert the
    TEST FIXTURES agree with it. That is the link that was missing: it fails when
    the schema and the fixtures diverge, in either direction, instead of letting
    both clients keep talking to a server that does not exist.
    """

    SCHEMA = _MODULE_DIR / "src" / "superplane-api" / "app" / "schemas" / "account.py"
    CONTRACTS = _MODULE_DIR / "contracts" / "superplane_contracts" / "connections.py"
    UI_FIXTURES = _MODULE_DIR / "ui" / "__tests__" / "vault-fixtures.ts"
    CLI_TESTS = _REPO_ROOT / "modules" / "gateway" / "tests" / "cli" / "test_superplane_onboarding.py"

    def _credential_response_fields(self) -> list[str]:
        """Field names declared by `CredentialResponse`, read from the source.

        Parsed with `ast` rather than imported: the schema module pulls in FastAPI
        and SQLAlchemy, and this assertion should not depend on the domain app's
        dependencies being installed in the Gateway's test environment.
        """
        tree = ast.parse(self.SCHEMA.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "CredentialResponse":
                return [stmt.target.id for stmt in node.body if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)]
        raise AssertionError("CredentialResponse not found in the vault schema module")

    def test_the_vault_row_has_no_credential_id_field(self):
        """The precise misreading that shipped, pinned so it cannot return.

        If a future schema revision DOES add `credential_id`, this test fails and
        forces a deliberate decision about which id is the bind reference, rather
        than letting a client silently pick the wrong one again.
        """
        fields = self._credential_response_fields()
        assert "credential_id" not in fields, (
            "CredentialResponse now has a `credential_id`: decide explicitly whether it or "
            "`adp_credential_id` is the bind reference, and update both clients together."
        )
        assert "adp_credential_id" in fields, "the vault handle field is missing from CredentialResponse"
        assert "id" in fields, "the registry row key is missing from CredentialResponse"

    def test_the_ui_fixture_declares_exactly_the_real_response_fields(self):
        """A UI fixture that drifts from the model stops being evidence."""
        declared = re.search(r"CREDENTIAL_RESPONSE_FIELDS = \[(.*?)\]", self.UI_FIXTURES.read_text(), re.S)
        assert declared, "vault-fixtures.ts must declare CREDENTIAL_RESPONSE_FIELDS"
        fixture_fields = re.findall(r"'([a-z_]+)'", declared.group(1))
        assert fixture_fields == self._credential_response_fields(), (
            "the UI's vault fixture no longer matches CredentialResponse; regenerate it from the schema."
        )

    def test_the_ui_fixture_keeps_the_two_ids_distinct(self):
        """Equal ids would let a client read the wrong one and still pass.

        This is the property that made the original defect invisible, so it is
        asserted on the fixture itself rather than left to each test's care.
        """
        text = self.UI_FIXTURES.read_text()
        row_id = re.search(r"^\s+id: '([^']+)'", text, re.M)
        vault_id = re.search(r"^\s+adp_credential_id: '([^']+)'", text, re.M)
        assert row_id and vault_id, "the vault fixture must set both `id` and `adp_credential_id`"
        assert row_id.group(1) != vault_id.group(1), (
            "the fixture's `id` and `adp_credential_id` must differ, or a client reading the "
            "registry row key instead of the vault handle still passes every test."
        )

    def test_both_clients_send_all_three_required_reference_fields(self):
        """`accept_connection_request` refuses a blank one of the three.

        Asserted against the contract source, so the requirement is read from the
        code that enforces it rather than restated here.
        """
        required = re.search(r"for k in \((.*?)\) if not payload\.get\(k\)", self.CONTRACTS.read_text())
        assert required, "accept_connection_request's required-field list was not found"
        fields = set(re.findall(r'"([a-z_]+)"', required.group(1)))
        assert fields == {"credential_id", "service", "label"}, (
            f"the server's required reference fields changed to {sorted(fields)}; update both clients."
        )
        # Both clients' bind bodies must name every one of them.
        ui_client = (_MODULE_DIR / "ui" / "client.ts").read_text()
        bind_body = re.search(r"export function buildBindBody\((.*?)\n}", ui_client, re.S)
        assert bind_body, "client.ts must build the bind body in one named place"
        for field in fields:
            assert field in bind_body.group(1), f"the browser bind body omits `{field}`"
        cli = (_REPO_ROOT / "modules" / "gateway" / "cli" / "adp-superplane-onboarding.py").read_text()
        cli_body = re.search(r"def build_bind_body\(.*?\n    return \{(.*?)\}", cli, re.S)
        assert cli_body, "the CLI must build the bind body in one named place"
        for field in fields:
            assert field in cli_body.group(1), f"the CLI bind body omits `{field}`"
