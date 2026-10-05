"""
Chat artifact bucket recoverability (#5660 / A07).

The shared chat artifact bucket holds every tenant's uploads under one prefix
layout, and the session sweeper deletes under a prefix it derives per session.
A bug or a hostile session id in that path is an unrecoverable data-loss event
unless object versions survive the delete, so the versioning configuration is
asserted here rather than left to a reviewer to notice.

These are static configuration checks against the Terraform source. They need no
AWS credentials and no built Lambda bundle, so they run in ordinary CI alongside
the unit tests rather than only against live state.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TF_FILE = (
    Path(__file__).resolve().parents[2] / "infra" / "chat-agent-infra.tf"
)


@pytest.fixture(scope="module")
def tf_source() -> str:
    if not TF_FILE.exists():
        pytest.skip(f"Terraform source not found: {TF_FILE}")
    return TF_FILE.read_text()


def _block(source: str, header: str) -> str:
    """Return the body of the top-level block introduced by `header`.

    Terraform blocks are brace-balanced, so the block ends at the first column-0
    closing brace. Good enough for a config assertion and avoids adding an HCL
    parser dependency for three tests.
    """
    start = source.index(header)
    end = source.index("\n}", start)
    return source[start:end]


class TestArtifactBucketVersioning:
    def test_versioning_is_enabled(self, tf_source: str):
        block = _block(
            tf_source, 'resource "aws_s3_bucket_versioning" "chat_artifacts"'
        )
        assert re.search(r'status\s*=\s*"Enabled"', block), (
            "chat artifact bucket must have versioning Enabled, or a faulty or "
            "hostile sweeper delete is terminal with nothing to restore from"
        )

    def test_noncurrent_versions_expire(self, tf_source: str):
        """Versioning without version expiry grows storage without bound.

        With versioning on, the existing 30-day expiry only writes a delete
        marker, so retained versions need their own expiry.
        """
        block = _block(
            tf_source,
            'resource "aws_s3_bucket_lifecycle_configuration" "chat_artifacts"',
        )
        assert "noncurrent_version_expiration" in block
        assert re.search(r"noncurrent_days\s*=\s*\d+", block)

    def test_sweeper_cannot_delete_object_versions(self, tf_source: str):
        """The sweeper must not be able to destroy version history.

        `DeleteObject` on a versioned bucket leaves the prior version in place,
        which is what makes an erroneous sweep recoverable. Granting
        `DeleteObjectVersion` would let the same bug delete permanently and
        defeat the safety net this issue adds.
        """
        block = _block(
            tf_source, 'resource "aws_iam_role_policy" "session_sweeper_s3"'
        )
        # Read the Action array itself, not every quoted string in the block:
        # the comment explaining this rule has to name the action it withholds,
        # and a substring scan would read that prose as a grant.
        action_arrays = re.findall(r"Action\s*=\s*\[(.*?)\]", block, re.DOTALL)
        assert action_arrays, f"no Action array found in session_sweeper_s3:\n{block}"
        granted = [
            action
            for action_array in action_arrays
            for action in re.findall(r'"([^"]+)"', action_array)
        ]

        assert "s3:DeleteObjectVersion" not in granted, granted
        assert "s3:DeleteObject" in granted, granted

    def test_sweeper_delete_is_limited_to_full_depth_keys(self, tf_source: str):
        block = _block(
            tf_source, 'resource "aws_iam_role_policy" "session_sweeper_s3"'
        )
        assert '${aws_s3_bucket.chat_artifacts.arn}/*' not in block
        assert '${aws_s3_bucket.chat_artifacts.arn}/o/*/t/*/u/*/s/*/*' in block
        assert '"s3:prefix" = ["o/*/t/*/u/*/s/*/"]' in block

    def test_agent_has_no_delete_and_no_bucket_wide_object_access(self, tf_source: str):
        block = _block(
            tf_source, 'resource "aws_iam_role_policy" "gateway_agent_chat_s3"'
        )
        assert '"s3:DeleteObject"' not in block
        assert '${aws_s3_bucket.chat_artifacts.arn}/*' not in block
        assert '${aws_s3_bucket.chat_artifacts.arn}/o/*/t/*/u/*/s/*/*' in block

    def test_sweeper_defaults_to_dry_run(self, tf_source: str):
        variables = (TF_FILE.parent / "variables.tf").read_text()
        variable_block = _block(
            variables, 'variable "chat_session_sweeper_dry_run"'
        )
        assert re.search(r"default\s*=\s*true", variable_block)
        lambda_block = _block(
            tf_source, 'resource "aws_lambda_function" "session_sweeper"'
        )
        assert "SWEEPER_DRY_RUN" in lambda_block
        assert "var.chat_session_sweeper_dry_run" in lambda_block

    def test_sweeper_can_verify_and_conditionally_delete_sessions(
        self, tf_source: str
    ):
        block = _block(
            tf_source,
            'resource "aws_iam_role_policy" "session_sweeper_dynamodb"',
        )
        cleanup_statement = block[block.index('Sid    = "CleanupTables"') :]
        actions = re.search(
            r"Action\s*=\s*\[(.*?)\]", cleanup_statement, re.DOTALL
        )
        assert actions, f"no CleanupTables Action array found:\n{block}"
        granted = re.findall(r'"([^"]+)"', actions.group(1))

        assert "dynamodb:GetItem" in granted, granted
        assert "dynamodb:TransactWriteItems" in granted, granted

    def test_transaction_guard_is_authorized_only_on_context_table(self, tf_source: str):
        block = _block(
            tf_source, 'resource "aws_iam_role_policy" "session_sweeper_dynamodb"'
        )
        guard = re.search(
            r'Sid\s*=\s*"CheckCurrentSessionHeader"(.*?)\n\s*\}',
            block,
            re.DOTALL,
        )
        assert guard, "transaction ConditionCheck requires a context-table grant"
        statement = guard.group(1)
        assert re.search(r'Effect\s*=\s*"Allow"', statement)
        actions = re.search(r'Action\s*=\s*\[(.*?)\]', statement, re.DOTALL)
        assert actions
        assert re.findall(r'"([^"]+)"', actions.group(1)) == [
            "dynamodb:ConditionCheckItem"
        ]
        resource = re.search(r'Resource\s*=\s*([^\n]+)', statement)
        assert resource
        assert resource.group(1).strip() == "aws_dynamodb_table.chat_context.arn"
