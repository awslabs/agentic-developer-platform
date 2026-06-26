"""Unit tests for seed-shared-corpus.py.

Issue #2089: Validates that the seed script correctly inserts 14 public
evaluation-corpus repos as shared-tier rows (tenant_id=NULL, owner_sub=NULL,
registered_by='platform') and is idempotent on re-run.

Tests use mocked psycopg2 (no live DB required).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# Import the seed module — add parent to path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importlib import import_module

seed_module = import_module("seed-shared-corpus")

SHARED_CORPUS_REPOS = seed_module.SHARED_CORPUS_REPOS
REGISTERED_BY = seed_module.REGISTERED_BY
ASSET_TYPE = seed_module.ASSET_TYPE
INITIAL_STATUS = seed_module.INITIAL_STATUS
build_source_ref = seed_module.build_source_ref
seed_shared_corpus = seed_module.seed_shared_corpus


# ---------------------------------------------------------------------------
# Tests: corpus list correctness
# ---------------------------------------------------------------------------


class TestCorpusList:
    """Tests for the hardcoded corpus list."""

    def test_exactly_14_repos(self):
        """The shared corpus contains exactly 14 public repos."""
        assert len(SHARED_CORPUS_REPOS) == 14

    def test_all_are_owner_slash_repo_format(self):
        """Every entry is in owner/repo format (no URL prefix)."""
        for repo in SHARED_CORPUS_REPOS:
            parts = repo.split("/")
            assert len(parts) == 2, f"Expected owner/repo format, got: {repo}"
            assert parts[0], f"Empty owner in: {repo}"
            assert parts[1], f"Empty repo in: {repo}"

    def test_no_duplicates(self):
        """No duplicate entries in the corpus list."""
        assert len(SHARED_CORPUS_REPOS) == len(set(SHARED_CORPUS_REPOS))

    def test_private_repo_excluded(self):
        """The private aws-e/adp repo is NOT in the shared corpus."""
        assert "aws-e/adp" not in SHARED_CORPUS_REPOS


# ---------------------------------------------------------------------------
# Tests: source_ref building
# ---------------------------------------------------------------------------


class TestBuildSourceRef:
    """Tests for build_source_ref."""

    def test_builds_github_url(self):
        assert build_source_ref("acme/repo") == "https://github.com/acme/repo"

    def test_preserves_case(self):
        assert (
            build_source_ref("CloakHQ/CloakBrowser")
            == "https://github.com/CloakHQ/CloakBrowser"
        )


# ---------------------------------------------------------------------------
# Tests: seed logic
# ---------------------------------------------------------------------------


class TestSeedSharedCorpus:
    """Tests for the core seed function."""

    @patch("psycopg2.connect")
    def test_inserts_all_14_with_correct_scoping(self, mock_connect):
        """Each repo is inserted with tenant_id=NULL, owner_sub=NULL, registered_by='platform'."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        # Simulate all 14 as new inserts (fetchone returns a row)
        mock_cur.fetchone.side_effect = [("fake-uuid-" + str(i),) for i in range(14)]

        result = seed_shared_corpus("postgresql://test", skip_dispatch=True)

        assert result["total_repos"] == 14
        assert result["inserted"] == 14
        assert result["skipped"] == 0

        # Verify all 14 execute calls used the correct parameters
        assert mock_cur.execute.call_count == 14

        for i, call_args in enumerate(mock_cur.execute.call_args_list):
            sql, params = call_args[0]

            # Verify SQL contains ON CONFLICT DO NOTHING
            assert "ON CONFLICT" in sql
            assert "DO NOTHING" in sql

            # Verify params: (id, asset_type, source_ref, status, registered_by, display_name)
            assert params[1] == ASSET_TYPE  # asset_type = 'repo'
            assert params[3] == INITIAL_STATUS  # status = 'registered'
            assert params[4] == REGISTERED_BY  # registered_by = 'platform'

            # source_ref is a GitHub URL
            assert params[2].startswith("https://github.com/")

            # SQL inserts NULL for tenant_id, owner_sub, project_id
            assert "NULL, NULL, NULL" in sql

    @patch("psycopg2.connect")
    def test_idempotent_rerun_skips_existing(self, mock_connect):
        """Re-running the seed when rows already exist skips them (no duplicates)."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        # Simulate all 14 as already existing (fetchone returns None = ON CONFLICT)
        mock_cur.fetchone.return_value = None

        result = seed_shared_corpus("postgresql://test", skip_dispatch=True)

        assert result["total_repos"] == 14
        assert result["inserted"] == 0
        assert result["skipped"] == 14

    @patch("psycopg2.connect")
    def test_partial_insert_some_new_some_existing(self, mock_connect):
        """Mixed scenario: some repos are new, some already exist."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        # First 7 are new, last 7 already exist
        results = [("uuid-" + str(i),) for i in range(7)] + [None] * 7
        mock_cur.fetchone.side_effect = results

        result = seed_shared_corpus("postgresql://test", skip_dispatch=True)

        assert result["total_repos"] == 14
        assert result["inserted"] == 7
        assert result["skipped"] == 7

    @patch("psycopg2.connect")
    def test_dry_run_no_db_writes(self, mock_connect):
        """Dry-run mode does not call cursor.execute."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        result = seed_shared_corpus(
            "postgresql://test", dry_run=True, skip_dispatch=True
        )

        assert result["dry_run"] is True
        assert result["inserted"] == 14
        # execute should not be called in dry-run mode
        mock_cur.execute.assert_not_called()

    @patch("psycopg2.connect")
    def test_source_refs_match_repos_txt(self, mock_connect):
        """All 14 source_refs match the repos in the designated corpus."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.side_effect = [("id",)] * 14

        seed_shared_corpus("postgresql://test", skip_dispatch=True)

        source_refs = []
        for call_args in mock_cur.execute.call_args_list:
            _, params = call_args[0]
            source_refs.append(params[2])  # source_ref is the 3rd param

        expected_refs = [f"https://github.com/{r}" for r in SHARED_CORPUS_REPOS]
        assert source_refs == expected_refs


# ---------------------------------------------------------------------------
# Tests: SQS dispatch
# ---------------------------------------------------------------------------


class TestSQSDispatch:
    """Tests for SQS dispatch of newly inserted rows."""

    @patch("boto3.client")
    @patch("psycopg2.connect")
    def test_dispatch_only_for_new_rows(self, mock_connect, mock_boto_client):
        """SQS messages are sent only for newly inserted rows, not skipped ones."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        # First 3 new, rest already exist
        results = [("uuid-" + str(i),) for i in range(3)] + [None] * 11
        mock_cur.fetchone.side_effect = results

        mock_sqs = MagicMock()
        mock_boto_client.return_value = mock_sqs

        result = seed_shared_corpus(
            "postgresql://test",
            skip_dispatch=False,
            queue_url="https://sqs.us-east-1.amazonaws.com/123/test-queue",
        )

        # Only 3 dispatched (for the 3 new rows)
        assert result["dispatched"] == 3
        assert mock_sqs.send_message.call_count == 3

        # Verify message structure
        for call_args in mock_sqs.send_message.call_args_list:
            kwargs = call_args[1]
            assert (
                kwargs["QueueUrl"]
                == "https://sqs.us-east-1.amazonaws.com/123/test-queue"
            )
            body = __import__("json").loads(kwargs["MessageBody"])
            assert body["content_type"] == "repo"
            assert body["scope"]["tenant_id"] is None
            assert body["scope"]["owner_sub"] is None
            assert body["scope"]["visibility"] == "shared"
            assert body["triggered_by"] == "platform_seed"
            assert body["steps"] == ["s3_upload", "cgc", "deepwiki", "graphrag"]

    @patch("psycopg2.connect")
    def test_skip_dispatch_flag(self, mock_connect):
        """When --skip-dispatch is set, no SQS messages are sent."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.side_effect = [("id",)] * 14

        result = seed_shared_corpus("postgresql://test", skip_dispatch=True)

        assert result["dispatched"] == 0

    @patch("psycopg2.connect")
    def test_no_queue_url_skips_dispatch(self, mock_connect):
        """When INGESTION_QUEUE_URL is empty, dispatch is silently skipped."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.side_effect = [("id",)] * 14

        result = seed_shared_corpus(
            "postgresql://test", skip_dispatch=False, queue_url=""
        )

        assert result["dispatched"] == 0
