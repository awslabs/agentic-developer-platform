"""Tests for the legacy-ACL backfill (#5658).

The properties that matter, in order:

1. Dry-run writes nothing. This is the whole safety contract of the script — an
   operator runs it against production to see the plan. A dry run that mutates is
   worse than no script.
2. A confirmed-public repo is left alone. ``["*"]`` is the correct value there, and
   rewriting it would break every public repo's reads.
3. An indeterminate row is not silently denied. Batch-denying on a transient GitHub
   error would take down reads across an unbounded number of repos at once.
4. Rollback restores the exact prior value.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_MODULE_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _MODULE_ROOT / "scripts" / "backfill_repo_acls.py"
_INGESTION_DIR = str(_MODULE_ROOT / "images" / "ingestion")


@pytest.fixture(scope="module")
def backfill():
    """Load the backfill script as a module without executing main()."""
    if _INGESTION_DIR not in sys.path:
        sys.path.insert(0, _INGESTION_DIR)
    spec = importlib.util.spec_from_file_location("backfill_repo_acls", str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.cursor.return_value = MagicMock()
    conn.cursor.return_value.rowcount = 1
    return conn


def _legacy_row(name="org/repo", row_id="uuid-1"):
    return {
        "id": row_id,
        "repo_name": name,
        "owner": name.split("/")[0],
        "allowed_principals": ["*"],
        "tenant_id": None,
    }


def _updates(cursor):
    """Return the UPDATE statements issued against the cursor."""
    return [call[0][0] for call in cursor.execute.call_args_list if "UPDATE" in call[0][0].upper()]


class TestCandidateSelection:
    def test_only_public_sentinel_rows_are_candidates(self, backfill, mock_conn):
        """Rows that already deny are not touched — the next ingest fills them in.

        ``ensure_repo_exists``'s ON CONFLICT overwrites `[]`/NULL, so those
        self-heal. Only `["*"]` is stuck and needs this script.
        """
        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = []
        backfill.find_legacy_rows(mock_conn)

        sql = cursor.execute.call_args[0][0]
        assert "'[\"*\"]'::jsonb" in sql
        assert "allowed_principals =" in sql

    def test_repo_filter_is_parameterized(self, backfill, mock_conn):
        """The repo name reaches the query as a bound parameter, not interpolated."""
        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = []
        backfill.find_legacy_rows(mock_conn, "org/repo")

        sql, params = cursor.execute.call_args[0]
        assert "%s" in sql
        assert params == ("org/repo",)
        assert "org/repo" not in sql


class TestClassification:
    def test_public_repo_is_confirmed_not_changed(self, backfill, monkeypatch):
        import repo_acl

        monkeypatch.setattr(repo_acl, "resolve_allowed_principals", lambda *a, **k: ["*"])
        result = backfill.classify(_legacy_row(), token="t0ken")

        assert result["outcome"] == backfill.OUTCOME_PUBLIC_CONFIRMED
        assert result["new_acl"] == ["*"]

    def test_private_repo_is_marked_for_tightening(self, backfill, monkeypatch):
        import repo_acl

        monkeypatch.setattr(
            repo_acl, "resolve_allowed_principals", lambda *a, **k: ["alice", "org/team"]
        )
        result = backfill.classify(_legacy_row(), token="t0ken")

        assert result["outcome"] == backfill.OUTCOME_TIGHTEN
        assert result["new_acl"] == ["alice", "org/team"]

    def test_empty_derivation_is_indeterminate_not_public(self, backfill, monkeypatch):
        """An empty result must not be read as "no restrictions"."""
        import repo_acl

        monkeypatch.setattr(repo_acl, "resolve_allowed_principals", lambda *a, **k: [])
        result = backfill.classify(_legacy_row(), token="t0ken")

        assert result["outcome"] == backfill.OUTCOME_INDETERMINATE
        assert result["new_acl"] == []

    def test_derivation_exception_is_indeterminate(self, backfill, monkeypatch):
        """A raised error classifies the row, it does not abort the whole batch."""
        import repo_acl

        def boom(*a, **k):
            raise OSError("GitHub unreachable")

        monkeypatch.setattr(repo_acl, "resolve_allowed_principals", boom)
        result = backfill.classify(_legacy_row(), token="t0ken")
        assert result["outcome"] == backfill.OUTCOME_INDETERMINATE


class TestApplySemantics:
    def test_confirmed_public_rows_are_never_updated(self, backfill, mock_conn):
        """Rewriting a genuinely public repo's ACL would break its reads."""
        plan = [{**_legacy_row(), "outcome": backfill.OUTCOME_PUBLIC_CONFIRMED, "new_acl": ["*"]}]
        journal = backfill.apply_changes(mock_conn, plan, deny_unknown=False)

        assert journal == []
        assert _updates(mock_conn.cursor.return_value) == []

    def test_indeterminate_rows_are_skipped_by_default(self, backfill, mock_conn):
        """A transient GitHub failure must not deny reads across the fleet."""
        plan = [{**_legacy_row(), "outcome": backfill.OUTCOME_INDETERMINATE, "new_acl": []}]
        journal = backfill.apply_changes(mock_conn, plan, deny_unknown=False)

        assert journal == []
        assert _updates(mock_conn.cursor.return_value) == []

    def test_indeterminate_rows_are_denied_with_the_explicit_flag(self, backfill, mock_conn):
        """--deny-unknown is available for operators who prefer fail-closed."""
        plan = [{**_legacy_row(), "outcome": backfill.OUTCOME_INDETERMINATE, "new_acl": []}]
        journal = backfill.apply_changes(mock_conn, plan, deny_unknown=True)

        assert len(journal) == 1
        assert journal[0]["new_acl"] == []

    def test_tighten_writes_the_derived_acl_and_journals_the_old_one(self, backfill, mock_conn):
        plan = [{**_legacy_row(), "outcome": backfill.OUTCOME_TIGHTEN, "new_acl": ["alice"]}]
        journal = backfill.apply_changes(mock_conn, plan, deny_unknown=False)

        assert journal == [
            {
                "id": "uuid-1",
                "repo_name": "org/repo",
                "previous_acl": ["*"],
                "new_acl": ["alice"],
            }
        ]
        cursor = mock_conn.cursor.return_value
        assert len(_updates(cursor)) == 1
        mock_conn.commit.assert_called()

    def test_a_failed_write_rolls_back_the_transaction(self, backfill, mock_conn):
        """A partial batch must not be left committed."""
        cursor = mock_conn.cursor.return_value
        cursor.execute.side_effect = RuntimeError("deadlock")
        plan = [{**_legacy_row(), "outcome": backfill.OUTCOME_TIGHTEN, "new_acl": ["alice"]}]

        with pytest.raises(RuntimeError):
            backfill.apply_changes(mock_conn, plan, deny_unknown=False)

        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()


class TestDryRunWritesNothing:
    """The safety contract: without --apply, no UPDATE is ever issued."""

    def test_main_dry_run_issues_no_updates(self, backfill, monkeypatch, mock_conn):
        import repo_acl

        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = [("uuid-1", "org/priv", "org", ["*"], None)]

        monkeypatch.setattr(backfill, "get_connection", lambda: mock_conn)
        monkeypatch.setattr(repo_acl, "resolve_allowed_principals", lambda *a, **k: ["alice"])
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        monkeypatch.setattr(sys, "argv", ["backfill_repo_acls.py"])

        assert backfill.main() == 0
        assert _updates(cursor) == [], "dry run must not mutate the database"
        mock_conn.commit.assert_not_called()

    def test_main_apply_does_issue_updates(self, backfill, monkeypatch, mock_conn, tmp_path):
        """The mirror of the above, so "writes nothing" isn't passing vacuously."""
        import repo_acl

        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = [("uuid-1", "org/priv", "org", ["*"], None)]
        journal_path = tmp_path / "journal.json"

        monkeypatch.setattr(backfill, "get_connection", lambda: mock_conn)
        monkeypatch.setattr(repo_acl, "resolve_allowed_principals", lambda *a, **k: ["alice"])
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        monkeypatch.setattr(
            sys, "argv", ["backfill_repo_acls.py", "--apply", "--journal", str(journal_path)]
        )

        assert backfill.main() == 0
        assert len(_updates(cursor)) == 1
        assert json.loads(journal_path.read_text())[0]["previous_acl"] == ["*"]

    def test_missing_token_aborts_rather_than_denying_everything(
        self, backfill, monkeypatch, mock_conn
    ):
        """No token means no derivation; the run must not proceed to write [] everywhere."""
        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = [("uuid-1", "org/priv", "org", ["*"], None)]

        monkeypatch.setattr(backfill, "get_connection", lambda: mock_conn)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(sys, "argv", ["backfill_repo_acls.py", "--apply"])

        assert backfill.main() == 1
        assert _updates(cursor) == []


class TestRollback:
    def test_rollback_dry_run_writes_nothing(self, backfill, mock_conn, tmp_path):
        journal = tmp_path / "j.json"
        journal.write_text(
            json.dumps(
                [
                    {
                        "id": "uuid-1",
                        "repo_name": "org/priv",
                        "previous_acl": ["*"],
                        "new_acl": ["alice"],
                    }
                ]
            )
        )

        assert backfill.rollback(mock_conn, str(journal), apply=False) == 0
        assert _updates(mock_conn.cursor.return_value) == []

    def test_rollback_restores_the_previous_acl_verbatim(self, backfill, mock_conn, tmp_path):
        journal = tmp_path / "j.json"
        journal.write_text(
            json.dumps(
                [
                    {
                        "id": "uuid-1",
                        "repo_name": "org/priv",
                        "previous_acl": ["bob", "org/old-team"],
                        "new_acl": ["alice"],
                    }
                ]
            )
        )

        assert backfill.rollback(mock_conn, str(journal), apply=True) == 0
        cursor = mock_conn.cursor.return_value
        update = next(c for c in cursor.execute.call_args_list if "UPDATE" in c[0][0].upper())
        assert json.loads(update[0][1][0]) == ["bob", "org/old-team"]
        mock_conn.commit.assert_called()


class TestVerify:
    def test_verify_fails_while_legacy_rows_remain(self, backfill, mock_conn):
        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = [("uuid-1", "org/priv", "org", ["*"], None)]
        cursor.fetchone.side_effect = [(10,), (2,)]

        assert backfill.verify(mock_conn) is False

    def test_verify_passes_when_no_sentinel_rows_remain(self, backfill, mock_conn):
        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = []
        cursor.fetchone.side_effect = [(10,), (2,)]

        assert backfill.verify(mock_conn) is True


def test_journal_is_durable_before_database_commit(backfill, mock_conn, tmp_path):
    path = tmp_path / "journal.json"
    plan = [{**_legacy_row(), "outcome": backfill.OUTCOME_TIGHTEN, "new_acl": ["alice"]}]

    def commit():
        assert json.loads(path.read_text())[0]["previous_acl"] == ["*"]
        assert path.stat().st_mode & 0o777 == 0o600

    mock_conn.commit.side_effect = commit
    backfill.apply_changes(mock_conn, plan, deny_unknown=False, journal_path=str(path))
    mock_conn.commit.assert_called_once()


def test_existing_journal_is_not_overwritten(backfill, mock_conn, tmp_path):
    path = tmp_path / "journal.json"
    path.write_text("earlier recovery data")
    plan = [{**_legacy_row(), "outcome": backfill.OUTCOME_TIGHTEN, "new_acl": ["alice"]}]
    with pytest.raises(FileExistsError):
        backfill.apply_changes(mock_conn, plan, deny_unknown=False, journal_path=str(path))
    assert path.read_text() == "earlier recovery data"
    mock_conn.rollback.assert_called_once()
    mock_conn.commit.assert_not_called()


def test_concurrent_acl_change_is_not_overwritten_or_journaled(backfill, mock_conn):
    mock_conn.cursor.return_value.rowcount = 0
    plan = [{**_legacy_row(), "outcome": backfill.OUTCOME_TIGHTEN, "new_acl": ["alice"]}]
    assert backfill.apply_changes(mock_conn, plan, deny_unknown=False) == []
    sql, params = mock_conn.cursor.return_value.execute.call_args.args
    assert "AND allowed_principals =" in sql
    assert json.loads(params[-1]) == ["*"]


def test_verified_public_rows_do_not_make_verification_impossible(backfill, mock_conn, monkeypatch):
    cursor = mock_conn.cursor.return_value
    cursor.fetchall.return_value = [("uuid-1", "org/public", "org", ["*"], None)]
    cursor.fetchone.side_effect = [(10,), (2,)]
    monkeypatch.setattr("repo_acl.resolve_allowed_principals", lambda *a, **kw: ["*"])
    assert backfill.verify(mock_conn)
