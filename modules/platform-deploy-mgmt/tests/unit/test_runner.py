"""Unit tests for runner.py — phase verification orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from platform_deploy_mgmt.checks.boto_helpers import Context
from platform_deploy_mgmt.checks.runner import run_phase
from platform_deploy_mgmt.checks.shape import CheckResult, Result, Severity


def _make_ctx() -> Context:
    """Create a test context with mock sessions."""
    return Context(
        customer_account_id="123456789012",
        region="us-east-1",
        environment="dev",
        customer_session=MagicMock(),
        platform_session=MagicMock(),
        deploy_id="test-deploy-1",
        mode="deploy",
        workflow_run_id="12345",
        workflow_run_url="http://example.com/runs/12345",
    )


class TestRunPhaseExitCode:
    """Runner exits 0 when all hard checks pass, 1 when any hard check fails."""

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_all_pass_exits_zero(self, mock_summary, mock_ddb, mock_s3):
        """All checks pass -> exit 0."""
        mock_checks = [
            (
                "1.1",
                "Check A",
                lambda ctx: CheckResult("1.1", "A", Result.PASS, Severity.HARD, 10, "OK"),
                Severity.HARD,
                "cheap",
            ),
            (
                "1.2",
                "Check B",
                lambda ctx: CheckResult("1.2", "B", Result.PASS, Severity.SOFT, 5, "OK"),
                Severity.SOFT,
                "cheap",
            ),
        ]
        with patch("platform_deploy_mgmt.checks.runner.PHASE_REGISTRY", {1: ("test_mod", "Test Phase")}):
            with patch("platform_deploy_mgmt.checks.runner.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_module.CHECKS = mock_checks
                mock_import.return_value = mock_module

                ctx = _make_ctx()
                exit_code = run_phase(1, ctx)
                assert exit_code == 0

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_hard_fail_exits_one(self, mock_summary, mock_ddb, mock_s3):
        """Hard check fails -> exit 1."""
        mock_checks = [
            (
                "1.1",
                "Check A",
                lambda ctx: CheckResult("1.1", "A", Result.PASS, Severity.HARD, 10, "OK"),
                Severity.HARD,
                "cheap",
            ),
            (
                "1.2",
                "Check B",
                lambda ctx: CheckResult("1.2", "B", Result.FAIL, Severity.HARD, 5, "Bad"),
                Severity.HARD,
                "cheap",
            ),
        ]
        with patch("platform_deploy_mgmt.checks.runner.PHASE_REGISTRY", {1: ("test_mod", "Test Phase")}):
            with patch("platform_deploy_mgmt.checks.runner.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_module.CHECKS = mock_checks
                mock_import.return_value = mock_module

                ctx = _make_ctx()
                exit_code = run_phase(1, ctx)
                assert exit_code == 1

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_soft_fail_exits_zero(self, mock_summary, mock_ddb, mock_s3):
        """Soft check fails -> exit 0 (soft failures don't change exit code)."""
        mock_checks = [
            (
                "1.1",
                "Check A",
                lambda ctx: CheckResult("1.1", "A", Result.PASS, Severity.HARD, 10, "OK"),
                Severity.HARD,
                "cheap",
            ),
            (
                "1.2",
                "Check B",
                lambda ctx: CheckResult("1.2", "B", Result.FAIL, Severity.SOFT, 5, "Warn"),
                Severity.SOFT,
                "cheap",
            ),
        ]
        with patch("platform_deploy_mgmt.checks.runner.PHASE_REGISTRY", {1: ("test_mod", "Test Phase")}):
            with patch("platform_deploy_mgmt.checks.runner.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_module.CHECKS = mock_checks
                mock_import.return_value = mock_module

                ctx = _make_ctx()
                exit_code = run_phase(1, ctx)
                assert exit_code == 0

    def test_unknown_phase_exits_one(self):
        """Unknown phase number -> exit 1."""
        ctx = _make_ctx()
        exit_code = run_phase(99, ctx)
        assert exit_code == 1


class TestRunPhaseRunsAllChecks:
    """Runner continues past failures (run-all-checks-then-fail-with-summary mode)."""

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_all_checks_run_even_on_early_failure(self, mock_summary, mock_ddb, mock_s3):
        """All checks execute even if the first one fails."""
        call_order = []
        mock_checks = [
            (
                "1.1",
                "A",
                lambda ctx: (call_order.append("1.1"), CheckResult("1.1", "A", Result.FAIL, Severity.HARD, 1, "Fail"))[
                    -1
                ],
                Severity.HARD,
                "cheap",
            ),
            (
                "1.2",
                "B",
                lambda ctx: (call_order.append("1.2"), CheckResult("1.2", "B", Result.PASS, Severity.HARD, 1, "OK"))[
                    -1
                ],
                Severity.HARD,
                "cheap",
            ),
            (
                "1.3",
                "C",
                lambda ctx: (call_order.append("1.3"), CheckResult("1.3", "C", Result.PASS, Severity.SOFT, 1, "OK"))[
                    -1
                ],
                Severity.SOFT,
                "cheap",
            ),
        ]
        with patch("platform_deploy_mgmt.checks.runner.PHASE_REGISTRY", {1: ("test_mod", "Test")}):
            with patch("platform_deploy_mgmt.checks.runner.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_module.CHECKS = mock_checks
                mock_import.return_value = mock_module

                ctx = _make_ctx()
                run_phase(1, ctx)
                assert call_order == ["1.1", "1.2", "1.3"]


class TestRunPhaseExceptionHandling:
    """Runner handles unhandled exceptions in check functions gracefully."""

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_exception_in_check_becomes_fail(self, mock_summary, mock_ddb, mock_s3):
        """If a check raises, it becomes a FAIL result."""

        def exploding_check(ctx):
            raise RuntimeError("boom")

        mock_checks = [
            ("1.1", "Exploding", exploding_check, Severity.HARD, "cheap"),
        ]
        with patch("platform_deploy_mgmt.checks.runner.PHASE_REGISTRY", {1: ("test_mod", "Test")}):
            with patch("platform_deploy_mgmt.checks.runner.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_module.CHECKS = mock_checks
                mock_import.return_value = mock_module

                ctx = _make_ctx()
                exit_code = run_phase(1, ctx)
                assert exit_code == 1


class TestRunPhaseReporting:
    """Runner calls upload and status functions."""

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_upload_and_ddb_called(self, mock_summary, mock_ddb, mock_s3):
        """After running checks, evidence is uploaded and DDB is updated."""
        mock_checks = [
            (
                "1.1",
                "A",
                lambda ctx: CheckResult("1.1", "A", Result.PASS, Severity.HARD, 1, "OK"),
                Severity.HARD,
                "cheap",
            ),
        ]
        with patch("platform_deploy_mgmt.checks.runner.PHASE_REGISTRY", {1: ("test_mod", "Test")}):
            with patch("platform_deploy_mgmt.checks.runner.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_module.CHECKS = mock_checks
                mock_import.return_value = mock_module

                ctx = _make_ctx()
                run_phase(1, ctx)

                mock_s3.assert_called_once()
                mock_ddb.assert_called_once()
                mock_summary.assert_called_once()
