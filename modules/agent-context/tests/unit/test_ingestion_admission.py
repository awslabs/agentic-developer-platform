"""Admission control on the ingestion side (#5658).

Scope of this file
------------------
Everything here is about the boundary where *untrusted input becomes a fetch*.
Three inputs are untrusted:

1. The crawl/document URL in a registration request.
2. Every URL the target itself serves back — sitemap ``<loc>`` entries and
   ``Location`` redirect headers. These are attacker-authored even when the
   originally submitted URL was legitimate, which is why validating only the
   submitted URL is not a control.
3. The ``s3://`` source in a document request, and the ``Content-Disposition``
   filename a remote server returns.

The tests are written as the attack rather than as a demonstration that the happy
path works: each one names a specific way in to the pipeline's network or
filesystem position, and asserts it is closed. The legitimate-traffic class at the
bottom exists because a guard that also breaks normal ingestion would be reverted,
and then nothing is guarded.

No test here performs real network I/O. ``check_url`` resolves DNS, so hostnames
are either IP literals (no resolution) or patched.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_INGESTION_DIR = str(Path(__file__).resolve().parents[2] / "images" / "ingestion")
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)

import s3_source_guard  # noqa: E402
import url_denylist  # noqa: E402
import url_fetch  # noqa: E402
from scope import IngestionScope  # noqa: E402

# Addresses that must never be reachable from the ingestion pods. Each is a
# distinct class of target, not three spellings of one.
IMDS = "169.254.169.254"  # cloud credentials
LOOPBACK = "127.0.0.1"  # the pod's own sidecars, incl. the Door on :5100
RFC1918 = "10.0.0.5"  # in-VPC services: RDS, Neptune, OpenSearch


# ---------------------------------------------------------------------------
# URL admission
# ---------------------------------------------------------------------------


class TestInternalDestinationsAreRefused:
    """The destinations that make SSRF worth doing are refused."""

    @pytest.mark.parametrize(
        "url",
        [
            f"http://{IMDS}/latest/meta-data/iam/security-credentials/",
            f"http://{LOOPBACK}:5100/mcp/call",
            f"http://{RFC1918}:5432/",
            "http://[::1]:8080/",
            "http://0.0.0.0/",
        ],
    )
    def test_internal_url_is_refused(self, url):
        assert not url_fetch.validate_url(url).allowed, f"{url} must be refused"

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://127.0.0.1:11211/",
            "dict://127.0.0.1:11211/",
            "ftp://internal.example/",
        ],
    )
    def test_non_http_scheme_is_refused(self, url):
        """Only http(s) is fetchable.

        The exotic schemes are listed because they are the classic way to turn a
        URL fetch into a protocol-confusion write against an internal service.
        """
        result = url_fetch.validate_url(url)
        assert not result.allowed
        assert result.reason_code == url_denylist.REASON_SCHEME_NOT_ALLOWED


class TestObfuscatedInternalAddressesAreRefused:
    """A blocked address written differently is still blocked.

    Each spelling below is a real bypass against implementations that compare
    host strings instead of canonicalising to an address first.
    """

    @pytest.mark.parametrize(
        ("url", "what"),
        [
            ("http://2130706433/", "127.0.0.1 as a 32-bit integer"),
            ("http://0177.0.0.1/", "octal first octet"),
            ("http://0x7f.0.0.1/", "hex first octet"),
            ("http://127.1/", "short-form IPv4"),
            ("http://[::ffff:169.254.169.254]/", "IPv4-mapped IPv6 IMDS"),
            ("http://[0:0:0:0:0:ffff:a9fe:a9fe]/", "IPv4-mapped IMDS in hex"),
        ],
    )
    def test_obfuscated_form_is_refused(self, url, what):
        assert not url_fetch.validate_url(url).allowed, f"bypass via {what}: {url}"

    def test_backslash_authority_confusion_is_refused(self):
        """``http://good.test\\@169.254.169.254/`` must not be treated as good.test.

        Browsers and several parsers read ``\\`` as ``/``, so the authority ends at
        the backslash for them while ``urlsplit`` sees ``good.test\\@...`` with
        userinfo. Whichever component ends up fetching, the destination is IMDS.
        """
        assert not url_fetch.validate_url(f"http://good.test\\@{IMDS}/").allowed

    def test_userinfo_host_confusion_is_refused(self):
        """A hostname in the userinfo position does not authorise the real host."""
        assert not url_fetch.validate_url(f"http://example.com@{IMDS}/").allowed


class TestRedirectsAreValidatedPerHop:
    """A redirect is a new fetch of a new attacker-chosen URL."""

    def test_redirect_to_internal_address_is_refused(self):
        """Hop 0 is public, hop 1 is IMDS — the fetch must not complete.

        This is the defect the pre-#5658 code had by construction:
        ``requests.get`` follows redirects by default, so only hop 0 was ever
        seen by any check, and an open redirect on a legitimate documentation
        host reached the metadata service.
        """
        # Only the TRANSPORT is stubbed. The real per-hop admission check still
        # runs inside _single_fetch, which is the thing under test — stubbing
        # _single_fetch itself would remove the check and the test would pass
        # against a completely unguarded implementation.
        calls: list[str] = []

        def fake_transport(method, normalized, headers, timeout, config, verdict):
            calls.append(normalized)
            return url_fetch.FetchResponse(
                url=normalized,
                status_code=302,
                headers={"location": f"http://{IMDS}/latest/meta-data/"},
                content=b"",
            )

        with patch.object(url_fetch, "_transport_fetch", side_effect=fake_transport):
            with patch.object(
                url_denylist,
                "_resolve_hostname",
                return_value=[url_denylist.ipaddress.ip_address("93.184.216.34")],
            ):
                with pytest.raises(url_fetch.DestinationRefused) as excinfo:
                    url_fetch.fetch("https://docs.example.com/redirect")

        assert calls == ["https://docs.example.com/redirect"], (
            "the internal redirect target must not be fetched"
        )
        assert excinfo.value.result.reason_code == url_denylist.REASON_BLOCKED_ADDRESS

    def test_redirect_chain_is_bounded(self):
        """An endless redirect loop is refused rather than followed forever."""

        def always_redirect(method, normalized, headers, timeout, config, verdict):
            return url_fetch.FetchResponse(
                url=normalized,
                status_code=302,
                headers={"location": "https://docs.example.com/next"},
                content=b"",
            )

        with patch.object(url_fetch, "_transport_fetch", side_effect=always_redirect):
            with patch.object(
                url_denylist,
                "_resolve_hostname",
                return_value=[url_denylist.ipaddress.ip_address("93.184.216.34")],
            ):
                with pytest.raises(url_fetch.DestinationRefused) as excinfo:
                    url_fetch.fetch("https://docs.example.com/start", max_redirects=3)

        assert excinfo.value.reason_code == "too_many_redirects"

    def test_allowed_redirect_chain_is_recorded(self):
        """A legitimate redirect is followed, and every hop is reported.

        The chain is part of the contract: an auditor needs to see what was
        actually fetched, not just where the request started.
        """
        pages = {
            "https://docs.example.com/old": (
                302,
                {"location": "https://docs.example.com/new"},
            ),
            "https://docs.example.com/new": (200, {"content-type": "text/html"}),
        }

        def fake_transport(method, normalized, headers, timeout, config, verdict):
            status, hdrs = pages[normalized]
            return url_fetch.FetchResponse(
                url=normalized, status_code=status, headers=hdrs, content=b"ok"
            )

        with patch.object(url_fetch, "_transport_fetch", side_effect=fake_transport):
            with patch.object(
                url_denylist,
                "_resolve_hostname",
                return_value=[url_denylist.ipaddress.ip_address("93.184.216.34")],
            ):
                response = url_fetch.fetch("https://docs.example.com/old")

        assert response.status_code == 200
        assert response.chain == [
            "https://docs.example.com/old",
            "https://docs.example.com/new",
        ]


class TestConnectTimeRevalidation:
    """Validation is re-asserted at the moment the socket opens.

    Without this, a DNS server that answers public-then-private (rebinding) passes
    the check and connects somewhere else, because ``requests`` resolves the name a
    second time after the decision was made.
    """

    def test_unapproved_address_is_refused_at_connect(self):
        factory = url_fetch._pinned_connection_factory(
            ["93.184.216.34"], url_denylist.DenylistConfig()
        )
        with pytest.raises(url_fetch.DestinationRefused):
            factory((IMDS, 80), 10)

    def test_approved_address_reaches_the_real_connector(self):
        """The guard permits the approved address through to the socket layer."""
        factory = url_fetch._pinned_connection_factory(
            ["93.184.216.34"], url_denylist.DenylistConfig()
        )
        with patch.object(
            url_fetch.urllib3_connection, "create_connection", return_value="socket"
        ) as real_connect:
            assert factory(("93.184.216.34", 443), 10) == "socket"
        real_connect.assert_called_once()

    def test_second_resolution_to_a_private_address_is_refused(self):
        """Rebinding: approved a public IP, the connector is handed a private one."""
        factory = url_fetch._pinned_connection_factory(
            ["93.184.216.34"], url_denylist.DenylistConfig()
        )
        with pytest.raises(url_fetch.DestinationRefused):
            factory((RFC1918, 443), 10)


class TestSameOriginIsCanonical:
    """Crawl-scope containment compares addresses, not host spellings."""

    def test_alternate_spellings_of_one_host_are_one_origin(self):
        assert url_fetch.same_origin("http://127.1/a", "http://127.0.0.1/b")
        assert url_fetch.same_origin("http://2130706433/a", "http://127.0.0.1/b")

    def test_different_hosts_are_not_same_origin(self):
        assert not url_fetch.same_origin("https://evil.test/", "https://docs.example.com/")

    def test_scheme_and_port_are_part_of_the_origin(self):
        """A netloc-only comparison ignores both, permitting a downgrade."""
        assert not url_fetch.same_origin("http://docs.example.com/", "https://docs.example.com/")
        assert not url_fetch.same_origin(
            "https://docs.example.com:8443/", "https://docs.example.com/"
        )

    def test_default_port_matches_implicit_port(self):
        """Legitimate: an explicit default port is the same origin as none."""
        assert url_fetch.same_origin("https://docs.example.com:443/a", "https://docs.example.com/b")


class TestLegitimateUrlsAreStillAllowed:
    """Public destinations pass. A guard that blocks everything is not a guard."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://93.184.216.34/docs",  # public IP literal: no DNS needed
            "http://93.184.216.34/sitemap.xml",
        ],
    )
    def test_public_destination_is_allowed(self, url):
        result = url_fetch.validate_url(url)
        assert result.allowed, f"{url} should be allowed: {result.reason}"
        assert result.resolved_ips == ["93.184.216.34"]


# ---------------------------------------------------------------------------
# S3 source ownership
# ---------------------------------------------------------------------------


class TestS3SourceOwnership:
    """``--source s3://...`` is authorised by allowlist, not by IAM reachability."""

    ALLOWED = "adp-context-data"

    def test_unlisted_bucket_is_refused(self):
        """The core case: another tenant's bucket that the task role can read."""
        decision = s3_source_guard.check_s3_source(
            "s3://other-tenant-private/secrets.pdf",
            allowlist_raw="",
            default_bucket=self.ALLOWED,
        )
        assert not decision.allowed
        assert decision.reason_code == "no_allowlist"

    def test_empty_allowlist_and_no_default_refuses_everything(self):
        """Absent configuration is not permission (fail closed)."""
        decision = s3_source_guard.check_s3_source(
            f"s3://{self.ALLOWED}/doc.pdf", allowlist_raw="", default_bucket=""
        )
        assert not decision.allowed
        assert decision.reason_code == "no_allowlist"

    def test_own_tenant_prefix_is_allowed(self):
        decision = s3_source_guard.check_s3_source(
            f"s3://{self.ALLOWED}/tenants/team-a/docs/sprint.pdf",
            allowlist_raw="",
            default_bucket=self.ALLOWED,
            scope=IngestionScope(visibility="tenant", tenant_id="team-a"),
        )
        assert decision.allowed
        assert decision.bucket == self.ALLOWED
        assert decision.key == "tenants/team-a/docs/sprint.pdf"

    def test_prefix_scoped_entry_confines_the_key(self):
        """``bucket/prefix`` permits that prefix and nothing else in the bucket."""
        allow = "shared-docs/public"
        assert s3_source_guard.check_s3_source(
            "s3://shared-docs/public/handbook.pdf", allowlist_raw=allow
        ).allowed
        refused = s3_source_guard.check_s3_source(
            "s3://shared-docs/private/salaries.xlsx", allowlist_raw=allow
        )
        assert not refused.allowed
        assert refused.reason_code == "bucket_not_allowed"

    def test_prefix_match_respects_segment_boundaries(self):
        """Prefix ``tenant-a`` must not match key ``tenant-abc/...``.

        The same class of defect as the Door's short-name collision: a raw
        ``startswith`` silently spans two different tenants.
        """
        decision = s3_source_guard.check_s3_source(
            "s3://shared-docs/tenant-abc/secret.pdf", allowlist_raw="shared-docs/tenant-a"
        )
        assert not decision.allowed

    @pytest.mark.parametrize(
        "uri",
        [
            "s3://shared-docs/public/../private/salaries.xlsx",
            "s3://shared-docs/public/%2e%2e/private/salaries.xlsx",
            "s3://shared-docs/public/./../private/x.pdf",
        ],
    )
    def test_traversal_out_of_an_allowed_prefix_is_refused(self, uri):
        """``..`` is normalised before comparison, so it cannot escape the prefix."""
        decision = s3_source_guard.check_s3_source(uri, allowlist_raw="shared-docs/public")
        assert not decision.allowed, f"{uri} escaped its allowed prefix"

    def test_key_escaping_the_bucket_root_is_refused(self):
        decision = s3_source_guard.check_s3_source(
            "s3://shared-docs/../etc/passwd", allowlist_raw="shared-docs"
        )
        assert not decision.allowed
        assert decision.reason_code == "malformed_key"

    @pytest.mark.parametrize(
        "uri",
        [
            "s3://evil.test@shared-docs/doc.pdf",
            "s3://shared-docs:8080/doc.pdf",
            "s3://UPPERCASE-Bucket/doc.pdf",
            "s3:///doc.pdf",
        ],
    )
    def test_malformed_authority_is_refused(self, uri):
        assert not s3_source_guard.check_s3_source(uri, allowlist_raw="shared-docs").allowed

    def test_non_s3_uri_is_refused(self):
        decision = s3_source_guard.check_s3_source(
            "https://evil.test/doc.pdf", allowlist_raw="shared-docs"
        )
        assert not decision.allowed
        assert decision.reason_code == "not_s3_uri"

    def test_malformed_allowlist_entry_does_not_widen_access(self):
        """A junk entry is dropped, not interpreted generously."""
        decision = s3_source_guard.check_s3_source(
            "s3://other-bucket/x.pdf", allowlist_raw="*, , //, s3://"
        )
        assert not decision.allowed

    def test_multiple_entries_are_all_honoured(self):
        allow = "bucket-one/public, bucket-two/reports"
        assert s3_source_guard.check_s3_source(
            "s3://bucket-one/public/a.pdf", allowlist_raw=allow
        ).allowed
        assert s3_source_guard.check_s3_source(
            "s3://bucket-two/reports/q1.pdf", allowlist_raw=allow
        ).allowed
        assert not s3_source_guard.check_s3_source(
            "s3://bucket-two/hr/q1.pdf", allowlist_raw=allow
        ).allowed


class TestDownloadFilenamesAreConfined:
    """Remote-supplied filenames stay inside the download directory."""

    @pytest.mark.parametrize(
        "candidate",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32\\config\\sam",
            "/etc/shadow",
            "....//....//etc/passwd",
            "%2e%2e%2fetc%2fpasswd",
            "..",
            ".",
            "",
        ],
    )
    def test_traversal_filename_is_reduced_to_a_safe_component(self, candidate):
        """The result is a single component that cannot escape when joined."""
        safe = s3_source_guard.safe_download_name(candidate)
        assert "/" not in safe and "\\" not in safe
        assert safe not in ("", ".", "..")

        with tempfile.TemporaryDirectory() as tmp:
            joined = os.path.realpath(os.path.join(tmp, safe))
            assert joined.startswith(os.path.realpath(tmp) + os.sep), (
                f"{candidate!r} -> {safe!r} escaped the download directory"
            )

    def test_null_byte_is_stripped(self):
        """A NUL truncates the path in some syscalls; it must not survive."""
        assert "\x00" not in s3_source_guard.safe_download_name("doc.pdf\x00.exe")

    def test_ordinary_filename_survives_recognisably(self):
        """Legitimate names are preserved — sanitising to a hash would be useless."""
        assert s3_source_guard.safe_download_name('"Sprint Review.pdf"') == "Sprint_Review.pdf"
        assert s3_source_guard.safe_download_name("report-2026_v2.docx") == "report-2026_v2.docx"

    def test_quoted_content_disposition_value_is_unwrapped(self):
        assert s3_source_guard.safe_download_name('"quarterly.pdf"') == "quarterly.pdf"


@pytest.mark.parametrize("name", ["ETag", "etag", "ETAG"])
def test_guarded_response_preserves_case_insensitive_cache_headers(name):
    response = url_fetch.FetchResponse(
        url="https://docs.example.com/",
        status_code=200,
        headers={"etag": '"revision-1"', "last-modified": "Wed, 23 Sep 2026 10:00:00 GMT"},
        content=b"document",
    )
    assert response.headers[name] == '"revision-1"'
    assert response.headers["Last-Modified"] == "Wed, 23 Sep 2026 10:00:00 GMT"
