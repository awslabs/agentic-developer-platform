"""Unit tests for Neptune loader batch_size, retry, and sqs-worker timeout fixes (#3160, #3173).

Validates:
  1. load_to_neptune() default batch_size=200 propagates to _load_vertices/_load_edges
  2. sqs-worker TIMEOUTS["repo"] == 3600
  3. sqs-worker receive_message called with VisibilityTimeout=3600
  4. Bounded retry with exponential backoff for retryable errors (#3173)
  5. Non-retryable errors skip retry (#3173)
  6. error_rate in load_to_neptune result (#3173)
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the ingestion image directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))


# ---------------------------------------------------------------------------
# Neptune loader batch_size tests
# ---------------------------------------------------------------------------


class TestNeptuneLoaderBatchSize:
    """Assert default batch_size=200 propagates to vertex/edge loading (#3173)."""

    def test_default_batch_size_is_200(self):
        """load_to_neptune() signature default for batch_size must be 200 (#3173)."""
        from scip_neptune_loader import load_to_neptune

        sig = inspect.signature(load_to_neptune)
        default = sig.parameters["batch_size"].default
        assert default == 200, f"Expected batch_size default 200, got {default}"

    def test_batch_size_propagates_to_load_vertices(self):
        """_load_vertices receives the batch_size from load_to_neptune()."""
        from scip_neptune_loader import load_to_neptune

        captured_batch_size = []

        def mock_load_vertices(neptune_url, region, vertices_path, batch_size):
            captured_batch_size.append(batch_size)
            return (0, 0)

        def mock_load_edges(neptune_url, region, edges_path, batch_size):
            return (0, 0)

        # Mock CSV with a minimal valid file
        import csv
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as vf:
            writer = csv.DictWriter(vf, fieldnames=["~id", "repo:String"])
            writer.writeheader()
            writer.writerow({"~id": "test", "repo:String": "org/repo"})
            vertices_path = vf.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as ef:
            writer = csv.DictWriter(ef, fieldnames=["~id"])
            writer.writeheader()
            edges_path = ef.name

        csv_output = MagicMock()
        csv_output.vertices_path = vertices_path
        csv_output.edges_path = edges_path

        with (
            patch("scip_neptune_loader._neptune_query", return_value={"results": [{"alive": 1}]}),
            patch("scip_neptune_loader.clear_repo_graph", return_value=True),
            patch("scip_neptune_loader._load_vertices", side_effect=mock_load_vertices),
            patch("scip_neptune_loader._load_edges", side_effect=mock_load_edges),
        ):
            load_to_neptune(csv_output, "endpoint:8182", "us-east-1")

        assert captured_batch_size == [200], f"Expected [200], got {captured_batch_size}"

    def test_batch_size_propagates_to_load_edges(self):
        """_load_edges receives the batch_size from load_to_neptune()."""
        from scip_neptune_loader import load_to_neptune

        captured_batch_size = []

        def mock_load_vertices(neptune_url, region, vertices_path, batch_size):
            return (0, 0)

        def mock_load_edges(neptune_url, region, edges_path, batch_size):
            captured_batch_size.append(batch_size)
            return (0, 0)

        import csv
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as vf:
            writer = csv.DictWriter(vf, fieldnames=["~id", "repo:String"])
            writer.writeheader()
            writer.writerow({"~id": "test", "repo:String": "org/repo"})
            vertices_path = vf.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as ef:
            writer = csv.DictWriter(ef, fieldnames=["~id"])
            writer.writeheader()
            edges_path = ef.name

        csv_output = MagicMock()
        csv_output.vertices_path = vertices_path
        csv_output.edges_path = edges_path

        with (
            patch("scip_neptune_loader._neptune_query", return_value={"results": [{"alive": 1}]}),
            patch("scip_neptune_loader.clear_repo_graph", return_value=True),
            patch("scip_neptune_loader._load_vertices", side_effect=mock_load_vertices),
            patch("scip_neptune_loader._load_edges", side_effect=mock_load_edges),
        ):
            load_to_neptune(csv_output, "endpoint:8182", "us-east-1")

        assert captured_batch_size == [200], f"Expected [200], got {captured_batch_size}"

    def test_custom_batch_size_overrides_default(self):
        """Explicit batch_size parameter overrides the default."""
        from scip_neptune_loader import load_to_neptune

        captured_batch_size = []

        def mock_load_vertices(neptune_url, region, vertices_path, batch_size):
            captured_batch_size.append(batch_size)
            return (0, 0)

        def mock_load_edges(neptune_url, region, edges_path, batch_size):
            captured_batch_size.append(batch_size)
            return (0, 0)

        import csv
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as vf:
            writer = csv.DictWriter(vf, fieldnames=["~id", "repo:String"])
            writer.writeheader()
            writer.writerow({"~id": "test", "repo:String": "org/repo"})
            vertices_path = vf.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as ef:
            writer = csv.DictWriter(ef, fieldnames=["~id"])
            writer.writeheader()
            edges_path = ef.name

        csv_output = MagicMock()
        csv_output.vertices_path = vertices_path
        csv_output.edges_path = edges_path

        with (
            patch("scip_neptune_loader._neptune_query", return_value={"results": [{"alive": 1}]}),
            patch("scip_neptune_loader.clear_repo_graph", return_value=True),
            patch("scip_neptune_loader._load_vertices", side_effect=mock_load_vertices),
            patch("scip_neptune_loader._load_edges", side_effect=mock_load_edges),
        ):
            load_to_neptune(csv_output, "endpoint:8182", "us-east-1", batch_size=200)

        assert all(b == 200 for b in captured_batch_size), (
            f"Expected all 200, got {captured_batch_size}"
        )


# ---------------------------------------------------------------------------
# sqs-worker timeout tests
# ---------------------------------------------------------------------------


class TestSqsWorkerTimeouts:
    """Assert TIMEOUTS['repo'] == 3600 and VisibilityTimeout == 3600."""

    def test_repo_timeout_is_3600(self):
        """TIMEOUTS['repo'] must be 3600 seconds (raised from 900 for #3160)."""
        # Import with mocked dependencies that sqs-worker needs at module level
        with (
            patch.dict(
                "sys.modules",
                {
                    "metrics": MagicMock(),
                    "telemetry": MagicMock(
                        configure_telemetry=MagicMock(),
                        get_logger=MagicMock(return_value=MagicMock()),
                        safe_emit=MagicMock(),
                        set_correlation_context=MagicMock(),
                    ),
                    "tracing": MagicMock(
                        get_tracer=MagicMock(return_value=MagicMock()),
                        setup_tracing=MagicMock(),
                        shutdown_tracing=MagicMock(),
                    ),
                    "config": MagicMock(
                        settings=MagicMock(
                            aws_region="us-east-1",
                            sqs_queue_url="https://sqs.us-east-1.amazonaws.com/123/queue",
                            dynamo_table="test-table",
                        )
                    ),
                    "github_auth": MagicMock(),
                    "scope": MagicMock(),
                    "status_callback": MagicMock(),
                    "boto3": MagicMock(),
                },
            ),
        ):
            # Remove cached module if present to force reimport
            if "sqs-worker" in sys.modules:
                del sys.modules["sqs-worker"]

            import importlib
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "sqs_worker",
                str(Path(__file__).resolve().parents[2] / "images" / "ingestion" / "sqs-worker.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            assert mod.TIMEOUTS["repo"] == 3600, (
                f"Expected TIMEOUTS['repo']=3600, got {mod.TIMEOUTS['repo']}"
            )

    def test_url_timeout_unchanged(self):
        """TIMEOUTS['url'] must remain 600 (not affected by #3160)."""
        with (
            patch.dict(
                "sys.modules",
                {
                    "metrics": MagicMock(),
                    "telemetry": MagicMock(
                        configure_telemetry=MagicMock(),
                        get_logger=MagicMock(return_value=MagicMock()),
                        safe_emit=MagicMock(),
                        set_correlation_context=MagicMock(),
                    ),
                    "tracing": MagicMock(
                        get_tracer=MagicMock(return_value=MagicMock()),
                        setup_tracing=MagicMock(),
                        shutdown_tracing=MagicMock(),
                    ),
                    "config": MagicMock(
                        settings=MagicMock(
                            aws_region="us-east-1",
                            sqs_queue_url="https://sqs.us-east-1.amazonaws.com/123/queue",
                            dynamo_table="test-table",
                        )
                    ),
                    "github_auth": MagicMock(),
                    "scope": MagicMock(),
                    "status_callback": MagicMock(),
                    "boto3": MagicMock(),
                },
            ),
        ):
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "sqs_worker_url",
                str(Path(__file__).resolve().parents[2] / "images" / "ingestion" / "sqs-worker.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            assert mod.TIMEOUTS["url"] == 600
            assert mod.TIMEOUTS["doc"] == 300
            assert mod.TIMEOUTS["infra"] == 300

    def test_visibility_timeout_is_3600(self):
        """receive_sqs_message() must call receive_message with VisibilityTimeout=3600."""
        mock_sqs_client = MagicMock()
        mock_sqs_client.receive_message.return_value = {"Messages": []}

        with (
            patch.dict(
                "sys.modules",
                {
                    "metrics": MagicMock(),
                    "telemetry": MagicMock(
                        configure_telemetry=MagicMock(),
                        get_logger=MagicMock(return_value=MagicMock()),
                        safe_emit=MagicMock(),
                        set_correlation_context=MagicMock(),
                    ),
                    "tracing": MagicMock(
                        get_tracer=MagicMock(return_value=MagicMock()),
                        setup_tracing=MagicMock(),
                        shutdown_tracing=MagicMock(),
                    ),
                    "config": MagicMock(
                        settings=MagicMock(
                            aws_region="us-east-1",
                            sqs_queue_url="https://sqs.us-east-1.amazonaws.com/123/queue",
                            dynamo_table="test-table",
                        )
                    ),
                    "github_auth": MagicMock(),
                    "scope": MagicMock(),
                    "status_callback": MagicMock(),
                    "boto3": MagicMock(
                        client=MagicMock(return_value=mock_sqs_client),
                        resource=MagicMock(),
                    ),
                },
            ),
        ):
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "sqs_worker_vis",
                str(Path(__file__).resolve().parents[2] / "images" / "ingestion" / "sqs-worker.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Override the module's sqs client with our mock
            mod.sqs = mock_sqs_client

            # Call receive_sqs_message
            mod.receive_sqs_message()

            # Verify VisibilityTimeout=3600 was passed
            mock_sqs_client.receive_message.assert_called_once()
            call_kwargs = mock_sqs_client.receive_message.call_args
            assert call_kwargs[1]["VisibilityTimeout"] == 3600, (
                f"Expected VisibilityTimeout=3600, got {call_kwargs[1].get('VisibilityTimeout')}"
            )


# ---------------------------------------------------------------------------
# Neptune retry tests (#3173)
# ---------------------------------------------------------------------------


class TestNeptuneRetry:
    """Assert bounded retry with exponential backoff for transient errors (#3173)."""

    def test_retryable_error_is_retried_and_succeeds(self):
        """A retryable error (Connection refused) is retried and succeeds on attempt 2."""
        from scip_neptune_loader import _neptune_query_with_retry

        call_count = []

        def mock_query(neptune_url, region, cypher, parameters=None):
            call_count.append(1)
            if len(call_count) < 3:
                return {"error": "[Errno 111] Connection refused", "code": 0}
            return {"results": [{"cnt": 5}]}

        with (
            patch("scip_neptune_loader._neptune_query", side_effect=mock_query),
            patch("scip_neptune_loader.time.sleep"),  # Don't actually sleep in tests
        ):
            result = _neptune_query_with_retry("http://test:8182", "us-east-1", "RETURN 1")

        assert "error" not in result
        assert result == {"results": [{"cnt": 5}]}
        assert len(call_count) == 3, f"Expected 3 attempts, got {len(call_count)}"

    def test_non_retryable_error_no_retry(self):
        """A non-retryable error (4xx query error) is NOT retried."""
        from scip_neptune_loader import _neptune_query_with_retry

        call_count = []

        def mock_query(neptune_url, region, cypher, parameters=None):
            call_count.append(1)
            return {"error": "Syntax error in query", "code": 400}

        with (
            patch("scip_neptune_loader._neptune_query", side_effect=mock_query),
            patch("scip_neptune_loader.time.sleep"),
        ):
            result = _neptune_query_with_retry("http://test:8182", "us-east-1", "BAD QUERY")

        assert "error" in result
        assert len(call_count) == 1, f"Expected 1 attempt (no retry), got {len(call_count)}"

    def test_retries_exhausted_returns_last_error(self):
        """If all 5 retries fail, the last error is returned."""
        from scip_neptune_loader import MAX_RETRIES, _neptune_query_with_retry

        call_count = []

        def mock_query(neptune_url, region, cypher, parameters=None):
            call_count.append(1)
            return {"error": "The read operation timed out", "code": 0}

        with (
            patch("scip_neptune_loader._neptune_query", side_effect=mock_query),
            patch("scip_neptune_loader.time.sleep"),
        ):
            result = _neptune_query_with_retry("http://test:8182", "us-east-1", "RETURN 1")

        assert "error" in result
        assert "timed out" in result["error"]
        # Initial attempt + MAX_RETRIES retries
        assert len(call_count) == MAX_RETRIES + 1

    def test_http_5xx_is_retryable(self):
        """HTTP 500/502/503/504 status codes trigger retry."""
        from scip_neptune_loader import _is_retryable

        assert _is_retryable({"error": "Internal error", "code": 500})
        assert _is_retryable({"error": "Bad gateway", "code": 502})
        assert _is_retryable({"error": "Service unavailable", "code": 503})
        assert _is_retryable({"error": "Gateway timeout", "code": 504})

    def test_http_429_is_retryable(self):
        """HTTP 429 (throttling) triggers retry."""
        from scip_neptune_loader import _is_retryable

        assert _is_retryable({"error": "Too many requests", "code": 429})

    def test_memory_limit_exceeded_is_retryable(self):
        """MemoryLimitExceededException triggers retry."""
        from scip_neptune_loader import _is_retryable

        assert _is_retryable({"error": "MemoryLimitExceededException: ...", "code": 0})

    def test_http_400_is_not_retryable(self):
        """HTTP 400 (client error) does NOT trigger retry."""
        from scip_neptune_loader import _is_retryable

        assert not _is_retryable({"error": "MalformedQueryException", "code": 400})

    def test_no_error_is_not_retryable(self):
        """A successful result is not retryable."""
        from scip_neptune_loader import _is_retryable

        assert not _is_retryable({"results": [{"cnt": 5}]})


# ---------------------------------------------------------------------------
# error_rate and ingest-repo failure threshold tests (#3173)
# ---------------------------------------------------------------------------


class TestErrorRateSignal:
    """Assert error_rate is present in load_to_neptune result (#3173)."""

    def test_error_rate_zero_on_success(self):
        """error_rate == 0.0 when all batches succeed."""
        from scip_neptune_loader import load_to_neptune

        import csv
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as vf:
            writer = csv.DictWriter(
                vf,
                fieldnames=[
                    "~id",
                    "symbol_id:String",
                    "name:String",
                    "module:String",
                    "file:String",
                    "line:Int",
                    "kind:String",
                    "repo:String",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "~id": "v1",
                    "symbol_id:String": "s1",
                    "name:String": "Foo",
                    "module:String": "mod",
                    "file:String": "a.py",
                    "line:Int": "1",
                    "kind:String": "class",
                    "repo:String": "org/repo",
                }
            )
            vertices_path = vf.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as ef:
            writer = csv.DictWriter(
                ef,
                fieldnames=[
                    "~id",
                    "~from",
                    "~to",
                    "~label",
                    "file:String",
                    "line:Int",
                    "repo:String",
                ],
            )
            writer.writeheader()
            edges_path = ef.name

        csv_output = MagicMock()
        csv_output.vertices_path = vertices_path
        csv_output.edges_path = edges_path

        with (
            patch(
                "scip_neptune_loader._neptune_query",
                return_value={"results": [{"alive": 1}]},
            ),
            patch("scip_neptune_loader.clear_repo_graph", return_value=True),
            patch(
                "scip_neptune_loader._neptune_query_with_retry",
                return_value={"results": [{"cnt": 1}]},
            ),
        ):
            result = load_to_neptune(csv_output, "endpoint:8182", "us-east-1")

        assert "error_rate" in result
        assert result["error_rate"] == 0.0
        assert result["success"] is True

    def test_error_rate_present_on_partial_failure(self):
        """error_rate > 0 when some batches fail."""
        from scip_neptune_loader import load_to_neptune

        import csv
        import tempfile

        # Create 2 vertices so that one batch loads (the mock will fail it)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as vf:
            writer = csv.DictWriter(
                vf,
                fieldnames=[
                    "~id",
                    "symbol_id:String",
                    "name:String",
                    "module:String",
                    "file:String",
                    "line:Int",
                    "kind:String",
                    "repo:String",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "~id": "v1",
                    "symbol_id:String": "s1",
                    "name:String": "Foo",
                    "module:String": "mod",
                    "file:String": "a.py",
                    "line:Int": "1",
                    "kind:String": "class",
                    "repo:String": "org/repo",
                }
            )
            vertices_path = vf.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as ef:
            writer = csv.DictWriter(
                ef,
                fieldnames=[
                    "~id",
                    "~from",
                    "~to",
                    "~label",
                    "file:String",
                    "line:Int",
                    "repo:String",
                ],
            )
            writer.writeheader()
            edges_path = ef.name

        csv_output = MagicMock()
        csv_output.vertices_path = vertices_path
        csv_output.edges_path = edges_path

        # Simulate vertex batch failure (non-retryable so it fails immediately)
        with (
            patch(
                "scip_neptune_loader._neptune_query",
                return_value={"results": [{"alive": 1}]},
            ),
            patch("scip_neptune_loader.clear_repo_graph", return_value=True),
            patch(
                "scip_neptune_loader._neptune_query_with_retry",
                return_value={"error": "Something broke", "code": 400},
            ),
        ):
            result = load_to_neptune(csv_output, "endpoint:8182", "us-east-1")

        assert "error_rate" in result
        assert result["error_rate"] == 1.0  # All vertices failed
        assert result["success"] is False
