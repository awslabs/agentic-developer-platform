"""Unit tests for Neptune loader batch_size and sqs-worker timeout fixes (#3160).

Validates:
  1. load_to_neptune() default batch_size=400 propagates to _load_vertices/_load_edges
  2. sqs-worker TIMEOUTS["repo"] == 3600
  3. sqs-worker receive_message called with VisibilityTimeout=3600
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
    """Assert default batch_size=400 propagates to vertex/edge loading."""

    def test_default_batch_size_is_400(self):
        """load_to_neptune() signature default for batch_size must be 400."""
        from scip_neptune_loader import load_to_neptune

        sig = inspect.signature(load_to_neptune)
        default = sig.parameters["batch_size"].default
        assert default == 400, f"Expected batch_size default 400, got {default}"

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

        assert captured_batch_size == [400], f"Expected [400], got {captured_batch_size}"

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

        assert captured_batch_size == [400], f"Expected [400], got {captured_batch_size}"

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
