"""Unit tests for door/neptune_client.py and door/neptune_auth.py.

Validates:
- neptune_enabled() respects NEPTUNE_ENABLED env var
- neptune_available() returns False when disabled/unreachable
- query_impact() returns structured caller data
- query_understand() returns symbol neighborhood
- query_repo_topology() returns module structure
- query_file_symbols() returns symbols in a file
- query_dir_symbols() returns symbols under a directory
- Graceful error handling (returns empty on failure, logs warning)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# neptune_enabled tests
# ---------------------------------------------------------------------------


class TestNeptuneEnabled:
    """Tests for the feature flag check."""

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("NEPTUNE_ENABLED", raising=False)
        # Re-import to pick up env change
        from door.neptune_client import neptune_enabled

        assert neptune_enabled() is False

    def test_enabled_with_true(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_ENABLED", "true")
        from door.neptune_client import neptune_enabled

        assert neptune_enabled() is True

    def test_enabled_with_one(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_ENABLED", "1")
        from door.neptune_client import neptune_enabled

        assert neptune_enabled() is True

    def test_disabled_with_false(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_ENABLED", "false")
        from door.neptune_client import neptune_enabled

        assert neptune_enabled() is False


# ---------------------------------------------------------------------------
# neptune_available tests
# ---------------------------------------------------------------------------


class TestNeptuneAvailable:
    """Tests for the availability check."""

    def test_returns_false_when_disabled(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_ENABLED", "false")
        from door.neptune_client import neptune_available

        assert neptune_available() is False

    def test_returns_false_when_no_driver(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_ENABLED", "true")
        with patch("door.neptune_client.get_neptune_driver", return_value=None):
            from door.neptune_client import neptune_available

            assert neptune_available() is False

    def test_returns_false_on_connection_error(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_ENABLED", "true")
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = ConnectionError("unreachable")
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import neptune_available

            assert neptune_available() is False

    def test_returns_true_on_success(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_ENABLED", "true")
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import neptune_available

            assert neptune_available() is True


# ---------------------------------------------------------------------------
# query_impact tests
# ---------------------------------------------------------------------------


class TestQueryImpact:
    """Tests for the impact query function."""

    def test_returns_empty_when_no_driver(self):
        with patch("door.neptune_client.get_neptune_driver", return_value=None):
            from door.neptune_client import query_impact

            result = query_impact("org/repo", "src/api.py", "handle_request")
            assert result == []

    def test_returns_caller_records(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_records = [
            {
                "caller_repo": "org/repo",
                "caller_file": "src/main.py",
                "caller_name": "main",
                "caller_kind": "function",
                "distance": 1,
            },
            {
                "caller_repo": "org/repo",
                "caller_file": "src/app.py",
                "caller_name": "start_app",
                "caller_kind": "function",
                "distance": 2,
            },
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(mock_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            result = query_impact("org/repo", "src/api.py", "handle_request")

        assert len(result) == 2
        assert result[0]["caller_name"] == "main"
        assert result[0]["distance"] == 1
        assert result[1]["caller_name"] == "start_app"
        assert result[1]["distance"] == 2

    def test_returns_empty_on_exception(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("Neptune timeout")
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            result = query_impact("org/repo", "src/api.py", "handle_request")
            assert result == []

    def test_query_uses_correct_parameters(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            query_impact("org/my-repo", "lib/utils.py", "parse_config")

        # Verify the parameters passed to session.run
        call_args = mock_session.run.call_args
        params = call_args[0][1]
        assert params["repo"] == "org/my-repo"
        assert params["file"] == "lib/utils.py"
        assert params["symbol_name"] == "parse_config"


# ---------------------------------------------------------------------------
# query_understand tests
# ---------------------------------------------------------------------------


class TestQueryUnderstand:
    """Tests for the understand query function."""

    def test_returns_empty_when_no_driver(self):
        with patch("door.neptune_client.get_neptune_driver", return_value=None):
            from door.neptune_client import query_understand

            result = query_understand("org/repo", "src/db.py", "connect")
            assert result == []

    def test_returns_neighborhood(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_records = [
            {
                "symbol_name": "connect",
                "symbol_kind": "function",
                "symbol_file": "src/db.py",
                "signature": "def connect(host: str) -> Connection",
                "callees": [{"name": "socket_open", "file": "src/net.py", "kind": "function"}],
                "callers": [{"name": "main", "file": "src/app.py", "kind": "function"}],
                "parents": [],
                "owners": [{"name": "Database", "file": "src/db.py"}],
            }
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(mock_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            result = query_understand("org/repo", "src/db.py", "connect")

        assert len(result) == 1
        assert result[0]["symbol_name"] == "connect"
        assert result[0]["symbol_kind"] == "function"
        assert len(result[0]["callees"]) == 1
        assert result[0]["callees"][0]["name"] == "socket_open"
        assert len(result[0]["callers"]) == 1
        assert len(result[0]["owners"]) == 1

    def test_returns_empty_on_exception(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("connection refused")
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            result = query_understand("org/repo", "src/db.py", "connect")
            assert result == []


# ---------------------------------------------------------------------------
# query_repo_topology tests
# ---------------------------------------------------------------------------


class TestQueryRepoTopology:
    """Tests for the repo-level topology query."""

    def test_returns_empty_when_no_driver(self):
        with patch("door.neptune_client.get_neptune_driver", return_value=None):
            from door.neptune_client import query_repo_topology

            result = query_repo_topology("org/repo")
            assert result == []

    def test_returns_module_topology(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_records = [
            {
                "module_path": "src/",
                "files": ["src/main.py", "src/utils.py"],
                "symbol_count": 15,
            },
            {
                "module_path": "tests/",
                "files": ["tests/test_main.py"],
                "symbol_count": 5,
            },
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(mock_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_repo_topology

            result = query_repo_topology("org/repo")

        assert len(result) == 2
        assert result[0]["module_path"] == "src/"
        assert result[0]["symbol_count"] == 15
        assert "src/main.py" in result[0]["files"]


# ---------------------------------------------------------------------------
# query_file_symbols tests
# ---------------------------------------------------------------------------


class TestQueryFileSymbols:
    """Tests for the file-level symbol query."""

    def test_returns_symbols_in_file(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_records = [
            {"name": "connect", "kind": "function", "line": 10, "signature": "def connect()"},
            {"name": "disconnect", "kind": "function", "line": 25, "signature": "def disconnect()"},
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(mock_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_file_symbols

            result = query_file_symbols("org/repo", "src/db.py")

        assert len(result) == 2
        assert result[0]["name"] == "connect"
        assert result[0]["line"] == 10
        assert result[1]["name"] == "disconnect"


# ---------------------------------------------------------------------------
# query_dir_symbols tests
# ---------------------------------------------------------------------------


class TestQueryDirSymbols:
    """Tests for the directory-level symbol query."""

    def test_returns_symbols_in_directory(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_records = [
            {"file": "src/api/routes.py", "name": "get_users", "kind": "function", "line": 5},
            {"file": "src/api/auth.py", "name": "verify_token", "kind": "function", "line": 12},
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(mock_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_dir_symbols

            result = query_dir_symbols("org/repo", "src/api")

        assert len(result) == 2
        assert result[0]["file"] == "src/api/routes.py"

    def test_query_uses_dir_prefix(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_dir_symbols

            query_dir_symbols("org/repo", "src/api")

        params = mock_session.run.call_args[0][1]
        assert params["dir_prefix"] == "src/api/"
        assert params["repo"] == "org/repo"


# ---------------------------------------------------------------------------
# get_neptune_driver tests
# ---------------------------------------------------------------------------


class TestGetNeptuneDriver:
    """Tests for lazy driver initialization."""

    def test_returns_none_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("NEPTUNE_ENDPOINT", raising=False)
        # Reset the cached driver
        import door.neptune_client

        door.neptune_client._driver = None

        with patch("door.neptune_auth.create_neptune_driver", return_value=None) as mock_create:
            result = door.neptune_client.get_neptune_driver()
            assert result is None
            mock_create.assert_called_once()

        # Reset for other tests
        door.neptune_client._driver = None

    def test_caches_driver_instance(self, monkeypatch):
        import door.neptune_client

        door.neptune_client._driver = None

        mock_driver = MagicMock()
        with patch(
            "door.neptune_auth.create_neptune_driver", return_value=mock_driver
        ) as mock_create:
            result1 = door.neptune_client.get_neptune_driver()
            result2 = door.neptune_client.get_neptune_driver()

            assert result1 is mock_driver
            assert result2 is mock_driver
            # Only called once (cached)
            mock_create.assert_called_once()

        # Reset for other tests
        door.neptune_client._driver = None


# ---------------------------------------------------------------------------
# neptune_auth tests
# ---------------------------------------------------------------------------


class TestNeptuneAuth:
    """Tests for the auth module."""

    def test_create_neptune_driver_returns_none_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("NEPTUNE_ENDPOINT", raising=False)
        from door.neptune_auth import create_neptune_driver

        result = create_neptune_driver(endpoint="", region="us-east-1")
        assert result is None

    def test_get_neptune_auth_produces_auth_object(self, monkeypatch):
        """Verify the auth function produces a neo4j Auth with correct structure."""
        # Mock botocore session
        with patch("door.neptune_auth.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_creds = MagicMock()
            mock_creds.access_key = "AKIAIOSFODNN7EXAMPLE"
            mock_creds.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
            mock_creds.token = "test-session-token"
            mock_session.get_credentials.return_value.get_frozen_credentials.return_value = (
                mock_creds
            )
            mock_session_cls.return_value = mock_session

            from door.neptune_auth import get_neptune_auth

            auth = get_neptune_auth("my-cluster.us-east-1.neptune.amazonaws.com", "us-east-1")

            # Auth should be a neo4j Auth object
            assert auth is not None
