"""
Unit tests for evidence_store.py — S3 persistence of screenshots + envelopes.

Issue #499: Validates safety rails (PNG magic, size limits), key layout,
presigned URL generation, and base64 stripping.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

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

    def test_returns_empty_on_nosuchbucket(self, mock_s3):
        """NoSuchBucket (bucket missing in AWS) must degrade gracefully,
        not crash the analysis run."""
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "The specified bucket does not exist"}},
            "PutObject",
        )
        png = _make_png(100)
        uri = evidence_store.upload_screenshot(
            png, run_id="run-1", url_index=0, shot_index=1
        )
        assert uri == ""

    def test_returns_empty_on_accessdenied(self, mock_s3):
        """AccessDenied must also degrade gracefully."""
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )
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

    def test_returns_empty_on_nosuchbucket(self, mock_s3):
        """NoSuchBucket must not crash the analysis run."""
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "missing"}},
            "PutObject",
        )
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

    def test_presign_empty_uri_returns_empty(self, mock_s3):
        """Empty string (from a failed/skipped upload) must return ""
        so the report can render the inline-base64 fallback."""
        url = evidence_store.presign("")
        assert url == ""
        mock_s3.generate_presigned_url.assert_not_called()


# ---------------------------------------------------------------------------
# shrink_for_claude tests
# ---------------------------------------------------------------------------


def _make_real_png(width: int, height: int, fill=(0, 128, 0)) -> bytes:
    """Create a real PNG of given dimensions (requires Pillow)."""
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (width, height), fill)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class TestShrinkForClaude:
    def test_small_image_unchanged(self):
        """A 200x200 image under the byte cap should pass through unchanged."""
        png = _make_real_png(200, 200)
        result = evidence_store.shrink_for_claude(png)
        assert result == png

    def test_large_dimension_resized(self):
        """2000x2000 must be resized so longest side = max_side (1024)."""
        png = _make_real_png(2000, 2000)
        result = evidence_store.shrink_for_claude(png, max_side=1024)
        assert result != png
        # Verify the resize actually happened
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(result))
        assert max(img.size) <= 1024

    def test_asymmetric_dimension_preserves_aspect(self):
        """A 4000x1000 image should become ~1024x256, keeping aspect ratio."""
        png = _make_real_png(4000, 1000)
        result = evidence_store.shrink_for_claude(png, max_side=1024)
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(result))
        assert img.size == (1024, 256)

    def test_result_under_claude_cap(self):
        """Output must be under the 3MB Claude image cap even for huge input."""
        # Create a large image with noise so PNG compression can't shrink it trivially
        from io import BytesIO

        from PIL import Image

        img = Image.new("RGB", (5000, 5000))
        # Tile a colorful pattern
        for y in range(0, 5000, 10):
            for x in range(0, 5000, 10):
                img.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256))
        out = BytesIO()
        img.save(out, format="PNG")
        big_png = out.getvalue()

        result = evidence_store.shrink_for_claude(big_png, max_side=1024)
        assert len(result) <= evidence_store._CLAUDE_IMAGE_BYTES_CAP
        assert result.startswith(b"\x89PNG")

    def test_never_raises(self):
        """Invalid bytes should not raise — return fallback."""
        # Random bytes that aren't a valid PNG
        garbage = b"not a png" * 100
        # Under the byte cap → returns original
        result = evidence_store.shrink_for_claude(garbage)
        assert result == garbage
