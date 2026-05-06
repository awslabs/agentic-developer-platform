"""
Unit tests for evidence_store.py — S3 persistence of screenshots + envelopes.

Issue #499: Validates safety rails (PNG magic, size limits), key layout,
presigned URL generation, and base64 stripping.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# evidence_store is in the parent directory (added to path by conftest.py)
import evidence_store
from evidence_schema import Evidence, ScreenshotCapture


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PNG_HEADER = b"\x89PNG\r\n\x1a\n"  # 8-byte PNG signature


def _make_png(size: int = 100) -> bytes:
    """Return bytes that start with PNG magic, padded to `size`."""
    return PNG_HEADER + b"\x00" * (size - len(PNG_HEADER))


def _make_evidence(*, image_base64: str = "c29tZWRhdGE=") -> Evidence:
    """Return a minimal Evidence object."""
    return Evidence(
        target_url="https://example.com",
        final_url="https://example.com",
        http_status=200,
        page_title="Example",
        screenshots=[
            ScreenshotCapture(
                session_id="sess-1",
                image_base64=image_base64,
                captured_at="2026-05-06T00:00:00Z",
            )
        ],
        run_started_at="2026-05-06T00:00:00Z",
        run_completed_at="2026-05-06T00:00:01Z",
        session_id="sess-1",
    )


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reset cached S3 client and set bucket env var for each test."""
    evidence_store._reset_client()
    monkeypatch.setenv(
        "URL_ANALYSIS_EVIDENCE_BUCKET", "adp-dev-url-analysis-evidence-123456789012"
    )
    yield
    evidence_store._reset_client()


@pytest.fixture
def mock_s3():
    """Patch boto3 S3 client with a MagicMock."""
    mock_client = MagicMock()
    mock_client.generate_presigned_url.return_value = (
        "https://adp-dev-url-analysis-evidence-123456789012.s3.amazonaws.com/"
        "tenant%3Dadp-default/issue%3D42/run%3Drun-1/url-0/screenshot-1.png"
        "?X-Amz-Expires=86400&X-Amz-Signature=abc"
    )
    with patch.object(evidence_store, "_s3_client", mock_client):
        yield mock_client


# ---------------------------------------------------------------------------
# upload_screenshot tests
# ---------------------------------------------------------------------------


class TestUploadScreenshot:
    def test_rejects_non_png(self, mock_s3):
        """PE header (MZ) must be rejected."""
        pe_bytes = b"MZ" + b"\x00" * 100
        with pytest.raises(ValueError, match="non-PNG"):
            evidence_store.upload_screenshot(
                pe_bytes, run_id="run-1", url_index=0, shot_index=1
            )
        mock_s3.put_object.assert_not_called()

    def test_rejects_oversize(self, mock_s3):
        """>5 MB PNG must be rejected."""
        big_png = _make_png(6 * 1024 * 1024)
        with pytest.raises(ValueError, match="oversize"):
            evidence_store.upload_screenshot(
                big_png, run_id="run-1", url_index=0, shot_index=1
            )
        mock_s3.put_object.assert_not_called()

    def test_uploads_valid_png(self, mock_s3):
        """Valid PNG under limit should be uploaded."""
        png = _make_png(1024)
        uri = evidence_store.upload_screenshot(
            png,
            tenant_id="tenant-abc",
            issue_number=42,
            run_id="run-1",
            url_index=0,
            shot_index=1,
        )
        assert (
            uri
            == "s3://adp-dev-url-analysis-evidence-123456789012/tenant=tenant-abc/issue=42/run=run-1/url-0/screenshot-1.png"
        )
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["ContentType"] == "image/png"
        assert call_kwargs["Body"] == png

    def test_key_layout_includes_tenant_prefix(self, mock_s3):
        """Uploaded object key must start with tenant=<id>/."""
        png = _make_png(100)
        uri = evidence_store.upload_screenshot(
            png,
            tenant_id="my-tenant",
            issue_number=99,
            run_id="run-x",
            url_index=2,
            shot_index=0,
        )
        key = uri.split("/", 3)[3]  # strip s3://bucket/
        assert key.startswith("tenant=my-tenant/")

    def test_returns_empty_when_bucket_unset(self, monkeypatch):
        """When env var is unset, gracefully return empty string."""
        monkeypatch.delenv("URL_ANALYSIS_EVIDENCE_BUCKET", raising=False)
        png = _make_png(100)
        uri = evidence_store.upload_screenshot(
            png, run_id="run-1", url_index=0, shot_index=1
        )
        assert uri == ""


# ---------------------------------------------------------------------------
# upload_evidence_envelope tests
# ---------------------------------------------------------------------------


class TestUploadEvidenceEnvelope:
    def test_strips_base64(self, mock_s3):
        """Evidence JSON in S3 must not contain image_base64."""
        evidence = _make_evidence(image_base64="aGVsbG8=")
        evidence_store.upload_evidence_envelope(
            evidence,
            tenant_id="t1",
            issue_number=1,
            run_id="run-1",
            url_index=0,
        )
        # Inspect what was uploaded
        call_kwargs = mock_s3.put_object.call_args[1]
        uploaded_json = json.loads(call_kwargs["Body"])
        for screenshot in uploaded_json["screenshots"]:
            assert "image_base64" not in screenshot

    def test_rejects_oversize(self, mock_s3):
        """Envelope > 2 MB must be rejected."""
        evidence = _make_evidence()
        # Stuff visible_text to blow up the serialized size
        evidence.visible_text = "x" * (3 * 1024 * 1024)
        with pytest.raises(ValueError, match="oversize"):
            evidence_store.upload_evidence_envelope(
                evidence, run_id="run-1", url_index=0
            )
        mock_s3.put_object.assert_not_called()

    def test_uploads_valid_envelope(self, mock_s3):
        """Small valid evidence should be uploaded as JSON."""
        evidence = _make_evidence()
        uri = evidence_store.upload_evidence_envelope(
            evidence,
            tenant_id="t1",
            issue_number=7,
            run_id="run-1",
            url_index=0,
        )
        assert "evidence.json" in uri
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["ContentType"] == "application/json"

    def test_returns_empty_when_bucket_unset(self, monkeypatch):
        """When env var is unset, gracefully return empty string."""
        monkeypatch.delenv("URL_ANALYSIS_EVIDENCE_BUCKET", raising=False)
        evidence = _make_evidence()
        uri = evidence_store.upload_evidence_envelope(
            evidence, run_id="run-1", url_index=0
        )
        assert uri == ""


# ---------------------------------------------------------------------------
# presign tests
# ---------------------------------------------------------------------------


class TestPresign:
    def test_presign_expiry_within_24h(self, mock_s3):
        """Presigned URL must use 86400s (24h) expiry."""
        evidence_store.presign(
            "s3://adp-dev-url-analysis-evidence-123456789012/tenant=t/issue=1/run=r/url-0/screenshot-1.png"
        )
        mock_s3.generate_presigned_url.assert_called_once()
        call_kwargs = mock_s3.generate_presigned_url.call_args[1]
        assert call_kwargs["ExpiresIn"] == 86400

    def test_presign_clamps_to_24h(self, mock_s3):
        """Requesting more than 24h should be clamped."""
        evidence_store.presign(
            "s3://adp-dev-url-analysis-evidence-123456789012/tenant=t/issue=1/run=r/url-0/screenshot-1.png",
            expires_in=172800,
        )
        call_kwargs = mock_s3.generate_presigned_url.call_args[1]
        assert call_kwargs["ExpiresIn"] == 86400

    def test_presign_rejects_non_s3_uri(self, mock_s3):
        """Non-s3 URI must raise ValueError."""
        with pytest.raises(ValueError, match="Expected s3://"):
            evidence_store.presign("https://example.com/foo")

    def test_presign_returns_url(self, mock_s3):
        """Should return the presigned URL string."""
        url = evidence_store.presign(
            "s3://adp-dev-url-analysis-evidence-123456789012/tenant=t/issue=1/run=r/url-0/screenshot-1.png"
        )
        assert url.startswith("https://")
