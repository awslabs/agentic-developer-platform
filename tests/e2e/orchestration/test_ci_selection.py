"""CI wiring tests (#5156).

Proves the claim "ordinary PR CI runs offline tests only" by reading the
workflow files rather than trusting a comment in them. The risk being guarded
against is a later edit adding a `pull_request` trigger, a credential step or a
`--run` mode to the paid workflow, which no other test would catch.

The workflows are parsed with a deliberately small line-based reader: PyYAML is
not available to the repo-root test tree, and adding a dependency to satisfy a
test would defeat the point of a network-free, dependency-free suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
OFFLINE_WORKFLOW = WORKFLOWS / "orchestration-harness-ci.yml"
LIVE_WORKFLOW = WORKFLOWS / "orchestration-live-tests.yml"


def _strip_comments(text: str) -> str:
    """Drop full-line comments so a trigger named in prose is not a match."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _top_level_keys(text: str) -> list[str]:
    return re.findall(r"^([a-z_]+):", _strip_comments(text), re.MULTILINE)


def _trigger_block(text: str) -> str:
    """Return the `on:` block, which is where a paid trigger would appear."""
    body = _strip_comments(text)
    match = re.search(r"^on:\n(.*?)(?=^[a-z_]+:)", body, re.MULTILINE | re.DOTALL)
    assert match, "workflow has no parsable 'on:' block"
    return match.group(1)


def _permissions(text: str) -> dict[str, str]:
    """Return the workflow-level `permissions:` block as scope -> level.

    Parsed rather than substring-matched because the question is what the block
    GRANTS. A declared block disables every scope it omits, so an absent entry is
    a denial and has to be distinguishable from a granted one.
    """
    body = _strip_comments(text)
    match = re.search(r"^permissions:\n(.*?)(?=^[a-z_]+:)", body, re.MULTILINE | re.DOTALL)
    assert match, "workflow has no parsable 'permissions:' block"
    return dict(re.findall(r"^  ([a-z-]+):\s*([a-z-]+)\s*$", match.group(1), re.MULTILINE))


@pytest.fixture(scope="module")
def offline_text() -> str:
    return OFFLINE_WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def live_text() -> str:
    return LIVE_WORKFLOW.read_text(encoding="utf-8")


class TestWorkflowsExist:
    def test_offline_workflow_exists(self):
        assert OFFLINE_WORKFLOW.is_file()

    def test_live_workflow_exists(self):
        assert LIVE_WORKFLOW.is_file()


class TestPullRequestsRunOfflineOnly:
    def test_offline_workflow_runs_on_pull_request(self, offline_text):
        """The harness tests are cheap and safe, so they gate every PR."""
        assert "pull_request:" in _trigger_block(offline_text)

    def test_offline_workflow_selects_only_this_package(self, offline_text):
        assert "python -m pytest tests/e2e/orchestration" in offline_text

    def test_offline_workflow_configures_no_aws_credentials(self, offline_text):
        """A credential in the PR job would make an offline claim false."""
        body = _strip_comments(offline_text)
        assert "configure-aws-credentials" not in body
        assert "role-to-assume" not in body
        assert "id-token" not in body

    def test_offline_workflow_never_invokes_the_paid_runner(self, offline_text):
        """It must run pytest only — never the qualification CLI."""
        body = _strip_comments(offline_text)
        assert "tests.e2e.orchestration.run" not in body
        for mode in ("--run", "--resume", "--cleanup", "--preflight"):
            assert mode not in body

    def test_offline_workflow_asserts_it_was_not_vacuous(self, offline_text):
        """A fully skipped run must fail, not report green."""
        assert "0 tests executed" in offline_text

    def test_offline_workflow_installs_no_aws_sdk(self, offline_text):
        """boto3 absent from the offline job proves the suite cannot need it."""
        install = [line for line in _strip_comments(offline_text).splitlines() if "pip install" in line]
        assert install, "expected a dependency install step"
        for line in install:
            assert "boto3" not in line


class TestPaidQualificationIsManualOnly:
    def test_live_workflow_is_dispatch_only(self, live_text):
        """The core guarantee: no PR, push or schedule can start a paid run."""
        triggers = _trigger_block(live_text)
        assert "workflow_dispatch:" in triggers
        for forbidden in ("pull_request", "push:", "schedule:", "workflow_run:", "workflow_call:"):
            assert forbidden not in triggers, f"paid qualification must not trigger on {forbidden}"

    def test_live_workflow_has_exactly_one_trigger(self, live_text):
        triggers = re.findall(r"^  ([a-z_]+):", _trigger_block(live_text), re.MULTILINE)
        assert triggers == ["workflow_dispatch"]

    def test_live_workflow_defaults_to_the_read_only_mode(self, live_text):
        """An operator who accepts every default mutates nothing."""
        mode_block = live_text.split("mode:", 1)[1].split("qualification_id:", 1)[0]
        assert re.search(r"default:\s*preflight", mode_block)

    def test_live_workflow_accepts_no_credential_input(self, live_text):
        """A dispatch input is recorded in the run log, so it must never be a secret.

        Asserts on the declared input NAMES, not the surrounding prose — the
        comments in that file legitimately discuss secret handling.
        """
        inputs_block = _strip_comments(live_text).split("inputs:", 1)[1].split("permissions:", 1)[0]
        declared = re.findall(r"^      ([a-z_]+):", inputs_block, re.MULTILINE)
        assert declared, "expected at least one declared dispatch input"
        for name in declared:
            for forbidden in ("secret", "token", "password", "private_key", "access_key", "credential"):
                assert forbidden not in name.lower(), f"input '{name}' looks like a credential input"

    def test_live_workflow_takes_a_committed_config_path(self, live_text):
        """The config is reviewed in-repo, not pasted into the dispatch form."""
        assert "config_path:" in live_text
        assert "does not exist in the checked-out tree" in live_text

    def test_live_workflow_rejects_a_traversing_config_path(self, live_text):
        assert "without '..' segments" in live_text

    def test_live_workflow_verifies_the_config_matches_the_selected_environment(self, live_text):
        """A dev config dispatched at staging must not proceed."""
        assert "but the dispatch selected" in live_text

    def test_live_workflow_reports_the_verified_account_before_mutating(self, live_text):
        """The log records the account actually touched."""
        report_index = live_text.index("Report the verified target account")
        run_index = live_text.index("Run qualification")
        assert report_index < run_index
        assert "aws sts get-caller-identity" in live_text

    def test_live_workflow_treats_nothing_ran_as_an_error(self, live_text):
        """Exit code 4 must not read as success in the run log."""
        assert "This is NOT a pass" in live_text

    def test_live_workflow_uses_a_scoped_role(self, live_text):
        assert "role-to-assume:" in live_text
        assert "AWS_QUALIFICATION_ROLE_ARN" in live_text

    def test_live_workflow_does_not_cancel_a_run_in_progress(self, live_text):
        """Cancelling mid-provision is exactly how a fixture leaks."""
        concurrency = live_text.split("concurrency:", 1)[1].split("jobs:", 1)[0]
        assert "cancel-in-progress: false" in concurrency

    def test_live_workflow_uploads_artifacts_even_on_failure(self, live_text):
        """A failed run is when the inventory is most needed."""
        assert "if: always()" in live_text
        assert "qualification-" in live_text


class TestOfflineTestsAreNotVacuous:
    """The offline suite must actually execute, however it is invoked.

    Regression guard for a real defect in this package: the sibling live
    conftests skip every collected item when ``E2E_CHAT_ENABLED`` is unset, and
    a plain ``pytest_collection_modifyitems`` hook here was overridden by hook
    call order — so ``pytest tests/`` reported 258 skipped, 0 passed and still
    exited 0. Making the hook a hookwrapper fixed it.
    """

    def test_this_test_is_running(self):
        """Trivially true when executed — and the point is that it executes.

        If a sibling's unfiltered skip reaches this package again, this test is
        reported skipped rather than passed, and the CI vacuity check fails the
        run instead of reporting green.
        """
        assert True

    def test_the_conftest_strips_the_inherited_skip_as_a_hookwrapper(self):
        """A plain hook here would silently regress under `pytest tests/`."""
        conftest = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
        hook = conftest.split("def pytest_collection_modifyitems", 1)[0]
        assert "hookwrapper=True" in hook, (
            "the skip-stripping hook must be a hookwrapper so it runs after the sibling "
            "conftests add their unfiltered skip markers"
        )


class TestNoOtherWorkflowRunsTheQualification:
    def test_only_the_live_workflow_invokes_the_qualification_cli(self):
        """Guards against another workflow quietly gaining a paid trigger."""
        offenders = []
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            if workflow.name == LIVE_WORKFLOW.name:
                continue
            if "tests.e2e.orchestration.run" in _strip_comments(workflow.read_text(encoding="utf-8")):
                offenders.append(workflow.name)
        assert offenders == [], f"unexpected workflow(s) invoke the qualification CLI: {offenders}"

    def test_no_pull_request_workflow_runs_the_whole_root_test_tree(self):
        """A blanket `pytest tests/` on PRs could pick up live suites.

        The live suites next door are env-gated, but a PR job running the whole
        tree would still be a surprising place for them to appear.
        """
        offenders = []
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            body = _strip_comments(workflow.read_text(encoding="utf-8"))
            if "pull_request" not in body:
                continue
            if re.search(r"pytest\s+tests/\s*(\\|$|\n)", body, re.MULTILINE):
                offenders.append(workflow.name)
        assert offenders == [], f"workflow(s) run the whole root test tree on PRs: {offenders}"


class TestInventoryRecoveryAcrossRuns:
    """resume/cleanup run in a SEPARATE workflow run with an empty workspace.

    Without the originating run's inventory restored, the fixtures it recorded are
    unreachable and cannot be cleaned up — so the wiring that carries the
    inventory between runs is asserted against the file, not assumed.
    """

    def test_resume_and_cleanup_require_the_source_run_id(self, live_text):
        """Dispatching a cleanup with no inventory to act on must be refused."""
        assert "source_run_id:" in live_text
        assert "requires source_run_id" in live_text

    def test_the_workflow_downloads_the_originating_runs_artifact(self, live_text):
        assert "download-artifact" in _strip_comments(live_text)
        assert "run-id: ${{ github.event.inputs.source_run_id }}" in live_text

    def test_the_restore_step_is_scoped_to_resume_and_cleanup(self, live_text):
        """A fresh run has nothing to restore and must not try."""
        block = live_text.split("Restore the originating run's inventory", 1)[1]
        condition = block.split("uses:", 1)[0]
        assert "mode == 'resume'" in condition
        assert "mode == 'cleanup'" in condition

    def test_the_restored_inventory_is_passed_to_the_cli(self, live_text):
        assert "--restore-from" in live_text

    def test_the_restore_happens_before_the_qualification_runs(self, live_text):
        restore = live_text.index("Restore the originating run's inventory")
        run = live_text.index("- name: Run qualification")
        assert restore < run, "the inventory must be in place before the mode executes"

    def test_the_upload_uses_the_configured_artifact_directory(self, live_text):
        """A hardcoded path can silently miss the inventory the config wrote.

        The uploaded artifact is exactly what a later resume/cleanup restores, so
        it must follow artifacts.directory from the config.
        """
        assert "steps.dispatch.outputs.artifact_dir" in live_text
        upload = live_text.split("Upload qualification artifacts", 1)[1]
        assert "artifact_dir" in upload
        assert "\n            artifacts/\n" not in upload, "the upload path must not be hardcoded"

    def test_the_artifact_directory_is_read_from_the_config(self, live_text):
        assert "['artifacts']['directory']" in live_text

    def test_a_traversing_artifact_directory_is_refused(self, live_text):
        assert "artifacts.directory must be a relative path" in live_text

    def test_the_failure_reminder_names_the_run_id_needed_to_clean_up(self, live_text):
        """An operator must be told how to reach the leaked fixtures."""
        assert "source_run_id=${{ github.run_id }}" in live_text

    def test_the_workflow_can_read_another_runs_artifact(self, live_text):
        """Downloading the originating run's inventory needs `actions: read`.

        `download-artifact` with `run-id` is an Actions API read of a DIFFERENT
        run. A declared `permissions:` block disables every scope it omits, so
        without this entry the restore step fails and cleanup has no inventory —
        the recorded fixtures become unreachable. Asserted because the symptom
        appears only on a live cleanup dispatch, which no test exercises.
        """
        assert _permissions(live_text).get("actions") == "read"

    def test_the_workflow_keeps_least_privilege(self, live_text):
        """The added scope must be read-only, and nothing else may be widened."""
        granted = _permissions(live_text)
        assert granted == {"id-token": "write", "contents": "read", "actions": "read"}, (
            f"unexpected workflow permissions: {granted}"
        )


class TestTargetVerificationIsSurfaced:
    def test_the_unverified_target_exit_code_is_handled(self, live_text):
        """Exit 7 means nothing was touched; it must not read as a generic failure."""
        assert "NOTHING was mutated" in live_text
        assert "connection.expected_account_id" in live_text

    def test_the_refusal_message_names_connection_resolution_as_a_cause(self, live_text):
        """Exit 7 has two causes, and an operator debugs the wrong one otherwise.

        An unregistered or revoked connection_ref refuses identically to an
        account mismatch, so the guidance must mention both rather than sending
        the operator to check only the account.
        """
        message = live_text.split("7) echo", 1)[1].split(";;", 1)[0]
        assert "connection_ref" in message
        assert "registered" in message
