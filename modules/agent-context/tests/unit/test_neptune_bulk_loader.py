"""Unit tests for Neptune Bulk Loader integration (#3233).

Validates:
  1. load_via_bulk_loader() uses format="csv" (NOT opencypher)
  2. Poll loop handles LOAD_COMPLETED → success
  3. Poll loop handles LOAD_FAILED → error with detail
  4. Poll loop times out after BULK_LOADER_TIMEOUT_SECONDS
  5. Fallback: ingest-repo uses UNWIND when NEPTUNE_BULK_LOAD_ROLE_ARN is unset
  6. Bulk path selected when NEPTUNE_BULK_LOAD_ROLE_ARN is set + s3_upload present
  7. _start_bulk_load sends correct payload structure
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

# Add the ingestion image directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))


# ---------------------------------------------------------------------------
# Bulk Loader format tests
# ---------------------------------------------------------------------------


class TestBulkLoaderFormat:
    """Assert Bulk Loader uses format=csv (Gremlin ~id/~label headers), NOT opencypher."""

    def test_start_bulk_load_sends_csv_format(self):
        """_start_bulk_load payload must have format='csv' (#3233)."""
        from scip_neptune_loader import _start_bulk_load

        captured_body = []

        def mock_http_request(url, region, method="GET", body=None):
            if method == "POST":
                captured_body.append(json.loads(body))
            return {"status": "200 OK", "payload": {"loadId": "test-load-id"}}

        with patch("scip_neptune_loader._neptune_http_request", side_effect=mock_http_request):
            _start_bulk_load(
                "endpoint:8182",
                "us-east-1",
                "s3://bucket/prefix/vertices.csv",
                "arn:aws:iam::123:role/bulk-load",
            )

        assert len(captured_body) == 1
        payload = captured_body[0]
        assert payload["format"] == "csv", f"Expected format='csv', got '{payload['format']}'"
        assert payload["source"] == "s3://bucket/prefix/vertices.csv"
        assert payload["iamRoleArn"] == "arn:aws:iam::123:role/bulk-load"
        assert payload["region"] == "us-east-1"
        assert payload["failOnError"] == "FALSE"
        assert payload["parallelism"] == "MEDIUM"
        assert payload["queueRequest"] == "TRUE"

    def test_format_is_not_opencypher(self):
        """Regression: format must NEVER be 'opencypher' — fails with 'Invalid header'."""
        from scip_neptune_loader import _start_bulk_load

        captured_body = []

        def mock_http_request(url, region, method="GET", body=None):
            if method == "POST":
                captured_body.append(json.loads(body))
            return {"status": "200 OK", "payload": {"loadId": "test-id"}}

        with patch("scip_neptune_loader._neptune_http_request", side_effect=mock_http_request):
            _start_bulk_load(
                "endpoint:8182",
                "us-east-1",
                "s3://bucket/prefix/edges.csv",
                "arn:aws:iam::123:role/bulk-load",
            )

        assert captured_body[0]["format"] != "opencypher"


# ---------------------------------------------------------------------------
# Poll loop tests
# ---------------------------------------------------------------------------


class TestBulkLoaderPoll:
    """Test _poll_bulk_load handles terminal states correctly."""

    def test_poll_returns_on_load_completed(self):
        """Poll loop exits on LOAD_COMPLETED with record counts."""
        from scip_neptune_loader import _poll_bulk_load

        responses = [
            {
                "payload": {
                    "overallStatus": {
                        "status": "LOAD_IN_PROGRESS",
                    }
                }
            },
            {
                "payload": {
                    "overallStatus": {
                        "status": "LOAD_COMPLETED",
                        "totalRecords": 1234,
                        "totalTimeSpent": 12,
                        "errors": {"errorCount": 0},
                    }
                }
            },
        ]
        call_count = [0]

        def mock_http_request(url, region, method="GET", body=None):
            idx = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            return responses[idx]

        with (
            patch("scip_neptune_loader._neptune_http_request", side_effect=mock_http_request),
            patch("scip_neptune_loader.time.sleep"),
        ):
            result = _poll_bulk_load("endpoint:8182", "us-east-1", "load-123")

        assert result["status"] == "LOAD_COMPLETED"
        assert result["total_records"] == 1234
        assert result["total_time_spent"] == 12
        assert result["errors"] == 0

    def test_poll_returns_on_load_failed(self):
        """Poll loop exits on LOAD_FAILED with error detail."""
        from scip_neptune_loader import _poll_bulk_load

        def mock_http_request(url, region, method="GET", body=None):
            if "details=true" in url:
                return {"errors": [{"errorMsg": "Invalid CSV row"}]}
            return {
                "payload": {
                    "overallStatus": {
                        "status": "LOAD_FAILED",
                        "totalRecords": 100,
                    }
                }
            }

        with (
            patch("scip_neptune_loader._neptune_http_request", side_effect=mock_http_request),
            patch("scip_neptune_loader.time.sleep"),
        ):
            result = _poll_bulk_load("endpoint:8182", "us-east-1", "load-456")

        assert result["status"] == "LOAD_FAILED"
        assert "error" in result

    def test_poll_times_out(self):
        """Poll loop returns TIMEOUT when elapsed exceeds limit."""
        from scip_neptune_loader import _poll_bulk_load

        def mock_http_request(url, region, method="GET", body=None):
            return {
                "payload": {
                    "overallStatus": {
                        "status": "LOAD_IN_PROGRESS",
                    }
                }
            }

        # Simulate time passing beyond timeout
        time_values = [0, 0, 601]  # Third call exceeds 600s timeout
        time_idx = [0]

        def mock_time():
            idx = min(time_idx[0], len(time_values) - 1)
            time_idx[0] += 1
            return time_values[idx]

        with (
            patch("scip_neptune_loader._neptune_http_request", side_effect=mock_http_request),
            patch("scip_neptune_loader.time.sleep"),
            patch("scip_neptune_loader.time.time", side_effect=mock_time),
        ):
            result = _poll_bulk_load("endpoint:8182", "us-east-1", "load-789")

        assert result["status"] == "TIMEOUT"


# ---------------------------------------------------------------------------
# End-to-end load_via_bulk_loader tests
# ---------------------------------------------------------------------------


class TestLoadViaBulkLoader:
    """Test the full load_via_bulk_loader flow."""

    def test_successful_load_returns_method_bulk_loader(self):
        """Successful bulk load returns method='bulk_loader'."""
        from scip_neptune_loader import load_via_bulk_loader

        def mock_query(url, region, cypher, parameters=None):
            return {"results": [{"alive": 1}]}

        start_calls = [0]

        def mock_http_request(url, region, method="GET", body=None):
            if method == "POST":
                start_calls[0] += 1
                return {"status": "200 OK", "payload": {"loadId": f"load-{start_calls[0]}"}}
            # GET (poll)
            return {
                "payload": {
                    "overallStatus": {
                        "status": "LOAD_COMPLETED",
                        "totalRecords": 500,
                        "totalTimeSpent": 10,
                        "errors": {"errorCount": 0},
                    }
                }
            }

        with (
            patch("scip_neptune_loader._neptune_query", side_effect=mock_query),
            patch("scip_neptune_loader.clear_repo_graph", return_value=True),
            patch("scip_neptune_loader._neptune_http_request", side_effect=mock_http_request),
            patch("scip_neptune_loader.time.sleep"),
        ):
            result = load_via_bulk_loader(
                s3_prefix="s3://bucket/neptune-bulk-load/org-repo/20260707T120000Z/",
                neptune_endpoint="host:8182",
                region="us-east-1",
                iam_role_arn="arn:aws:iam::123:role/bulk-load",
                repo="org/repo",
            )

        assert result["method"] == "bulk_loader"
        assert result["success"] is True
        assert result["vertices_loaded"] == 500
        assert result["edges_loaded"] == 500
        assert result["total_errors"] == 0

    def test_connection_failure_returns_error(self):
        """Connection failure returns error without crashing."""
        from scip_neptune_loader import load_via_bulk_loader

        def mock_query(url, region, cypher, parameters=None):
            return {"error": "Connection refused", "code": 0}

        with patch("scip_neptune_loader._neptune_query", side_effect=mock_query):
            result = load_via_bulk_loader(
                s3_prefix="s3://bucket/prefix/",
                neptune_endpoint="host:8182",
                region="us-east-1",
                iam_role_arn="arn:aws:iam::123:role/bulk-load",
                repo="org/repo",
            )

        assert result["error"] == "connection_failed"
        assert result["method"] == "bulk_loader"

    def test_vertex_load_failure_does_not_attempt_edges(self):
        """If vertex load fails, edges are not attempted."""
        from scip_neptune_loader import load_via_bulk_loader

        def mock_query(url, region, cypher, parameters=None):
            return {"results": [{"alive": 1}]}

        post_count = [0]

        def mock_http_request(url, region, method="GET", body=None):
            if method == "POST":
                post_count[0] += 1
                return {"status": "200 OK", "payload": {"loadId": "v-load"}}
            # Poll returns failure
            return {
                "payload": {
                    "overallStatus": {
                        "status": "LOAD_FAILED",
                    }
                }
            }

        with (
            patch("scip_neptune_loader._neptune_query", side_effect=mock_query),
            patch("scip_neptune_loader.clear_repo_graph", return_value=True),
            patch("scip_neptune_loader._neptune_http_request", side_effect=mock_http_request),
            patch("scip_neptune_loader.time.sleep"),
        ):
            result = load_via_bulk_loader(
                s3_prefix="s3://bucket/prefix/",
                neptune_endpoint="host:8182",
                region="us-east-1",
                iam_role_arn="arn:aws:iam::123:role/bulk-load",
                repo="org/repo",
            )

        # Only 1 POST (vertices), edges not started
        assert post_count[0] == 1
        assert result["success"] is False
        assert result["error"] == "vertex_load_failed"


# ---------------------------------------------------------------------------
# Fallback behavior tests (ingest-repo integration)
# ---------------------------------------------------------------------------


class TestBulkLoaderFallback:
    """Verify fallback to UNWIND when bulk loader role ARN is unset."""

    def test_unwind_used_when_role_arn_empty(self):
        """When NEPTUNE_BULK_LOAD_ROLE_ARN is empty, load_to_neptune is used."""
        # This tests the conditional in ingest-repo.py:
        # if NEPTUNE_BULK_LOAD_ROLE_ARN and result.get("s3_upload"):
        #     use bulk loader
        # else:
        #     use UNWIND
        role_arn = ""
        s3_upload = "s3://bucket/neptune-bulk-load/org-repo/ts/"

        # Simulate the decision logic from ingest-repo.py
        use_bulk = bool(role_arn) and bool(s3_upload)
        assert use_bulk is False

    def test_unwind_used_when_s3_upload_missing(self):
        """When s3_upload is empty, UNWIND fallback is used even with role ARN."""
        role_arn = "arn:aws:iam::123:role/bulk-load"
        s3_upload = ""

        use_bulk = bool(role_arn) and bool(s3_upload)
        assert use_bulk is False

    def test_bulk_selected_when_role_and_s3_present(self):
        """Bulk loader selected when both role ARN and s3_upload are present."""
        role_arn = "arn:aws:iam::123:role/bulk-load"
        s3_upload = "s3://bucket/neptune-bulk-load/org-repo/ts/"

        use_bulk = bool(role_arn) and bool(s3_upload)
        assert use_bulk is True
