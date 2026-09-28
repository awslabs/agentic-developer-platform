"""
Unit tests for URL destination checks.

These assert invariants rather than replaying specific reported inputs:
equivalence across notations, fail-closed resolution, and coverage by address
classification rather than by an enumerated list.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import ClassVar
from unittest.mock import patch

import pytest

from denylist import (
    REASON_ADDRESS_NOT_APPROVED,
    REASON_BLOCKED_ADDRESS,
    REASON_HOST_PATTERN,
    REASON_RESOLUTION_FAILED,
    DenylistConfig,
    canonical_address,
    check_connect_address,
    check_url,
    scrub_url_credentials,
)


def _dns(*ips: str):
    """Build a getaddrinfo return value for the given IPs."""
    entries = []
    for ip in ips:
        family = (
            socket.AF_INET6 if ipaddress.ip_address(ip).version == 6 else socket.AF_INET
        )
        sockaddr = (ip, 80, 0, 0) if family == socket.AF_INET6 else (ip, 80)
        entries.append((family, socket.SOCK_STREAM, 0, "", sockaddr))
    return entries


class TestDenylistRejectsInternalRanges:
    """Every internal/reserved destination class must be refused."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1/admin",
            "http://10.255.255.255/path",
            "http://172.16.0.1/api",
            "http://172.31.255.255/x",
            "http://192.168.1.1/login",
            "http://192.168.0.100:8080/",
            "http://127.0.0.1/",
            "http://127.0.0.53/dns",
            "http://0.0.0.0/",
            "http://169.254.1.1/",
            "http://100.64.1.1/",  # RFC 6598 carrier-grade NAT
            "http://198.18.0.1/",  # RFC 2544 benchmarking
            "http://240.0.0.1/",  # reserved for future use
            "http://224.0.0.1/",  # multicast
            "http://192.88.99.1/",  # 6to4 relay anycast
            "http://[::1]/admin",
            "http://[fe80::1]/",
            "http://[fec0::]/",
            "http://[feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/",
            "http://[fc00::1]/",
            "http://[2001:db8::1]/",
            "http://[ff02::1]/",
        ],
    )
    def test_rejects_internal_destination(self, url: str) -> None:
        result = check_url(url)
        assert not result.allowed
        assert result.reason_code == REASON_BLOCKED_ADDRESS

    def test_rejects_aws_metadata_ip(self) -> None:
        result = check_url("http://169.254.169.254/latest/meta-data/")
        assert not result.allowed
        assert "169.254.169.254" in result.reason

    def test_rejects_aws_metadata_ipv6(self) -> None:
        result = check_url("http://[fd00:ec2::254]/latest/meta-data/")
        assert not result.allowed

    @patch("socket.getaddrinfo")
    def test_rejects_hostname_resolving_to_internal(self, mock_dns) -> None:
        mock_dns.return_value = _dns("10.0.0.5")
        result = check_url("http://evil-internal.example.com/steal")
        assert not result.allowed
        assert "10.0.0.5" in result.reason

    @pytest.mark.parametrize(
        "site_local", ["fec0::", "feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"]
    )
    @patch("socket.getaddrinfo")
    def test_rejects_hostname_resolving_to_site_local(
        self, mock_dns, site_local: str
    ) -> None:
        mock_dns.return_value = _dns(site_local)
        result = check_url("http://site-local.example.com/")
        assert not result.allowed
        assert result.reason_code == REASON_BLOCKED_ADDRESS
        assert site_local in result.resolved_ips


class TestNotationEquivalence:
    """
    A blocked destination must produce the same verdict however it is written.

    This is the core invariant: the decision is made on the canonical address,
    so no alternate spelling can take a different path through the check.
    """

    # Each group is a set of notations that all denote the same address.
    EQUIVALENT_NOTATIONS: ClassVar[list[list[str]]] = [
        # loopback
        ["127.0.0.1", "2130706433", "0177.0.0.1", "0x7f.0.0.1", "127.1", "127.0.1"],
        # AWS instance metadata (link-local)
        ["169.254.169.254", "2852039166", "0251.0376.0251.0376", "0xa9.0xfe.0xa9.0xfe"],
        # RFC 1918 private
        ["10.0.0.5", "167772165", "012.0.0.5"],
        ["192.168.1.1", "3232235777", "0300.0250.01.01"],
        # carrier-grade NAT
        ["100.64.1.1", "1681916161"],
    ]

    @pytest.mark.parametrize("notations", EQUIVALENT_NOTATIONS)
    def test_all_notations_of_blocked_address_are_refused(
        self, notations: list[str]
    ) -> None:
        canonical = canonical_address(notations[0])
        for notation in notations:
            assert canonical_address(notation) == canonical, (
                f"{notation} did not canonicalise to {canonical}"
            )
            result = check_url(f"http://{notation}/")
            assert not result.allowed, f"{notation} was allowed"
            assert result.reason_code == REASON_BLOCKED_ADDRESS

    @pytest.mark.parametrize(
        "notation,canonical",
        [
            ("127.0.0.1.", "127.0.0.1"),
            ("2130706433.", "127.0.0.1"),
            ("0177.0.0.1.", "127.0.0.1"),
            ("0x7f.0.0.1.", "127.0.0.1"),
            ("127.1.", "127.0.0.1"),
            ("169.254.169.254.", "169.254.169.254"),
            ("2852039166.", "169.254.169.254"),
            ("0251.0376.0251.0376.", "169.254.169.254"),
            ("0xa9.0xfe.0xa9.0xfe.", "169.254.169.254"),
        ],
    )
    def test_terminal_dot_uses_whatwg_ipv4_semantics(
        self, notation: str, canonical: str
    ) -> None:
        assert canonical_address(notation) == ipaddress.ip_address(canonical)
        result = check_url(f"http://{notation}/")
        assert not result.allowed
        assert result.reason_code == REASON_BLOCKED_ADDRESS

    @pytest.mark.parametrize(
        "embedded,plain",
        [
            ("[::ffff:127.0.0.1]", "127.0.0.1"),
            ("[::ffff:7f00:1]", "127.0.0.1"),
            ("[::ffff:169.254.169.254]", "169.254.169.254"),
            ("[::ffff:a9fe:a9fe]", "169.254.169.254"),
            ("[::ffff:10.0.0.5]", "10.0.0.5"),
            ("[2002:7f00:1::1]", "127.0.0.1"),  # 6to4
            ("[2002:a9fe:a9fe::1]", "169.254.169.254"),
            ("[64:ff9b::7f00:1]", "127.0.0.1"),  # NAT64
            ("[64:ff9b::a9fe:a9fe]", "169.254.169.254"),
        ],
    )
    def test_ipv4_embedded_in_ipv6_is_refused_like_the_plain_form(
        self, embedded: str, plain: str
    ) -> None:
        """An IPv4 address carried inside IPv6 must not escape the check."""
        embedded_result = check_url(f"http://{embedded}/")
        plain_result = check_url(f"http://{plain}/")
        assert not plain_result.allowed
        assert not embedded_result.allowed, f"{embedded} was allowed"
        assert embedded_result.reason_code == plain_result.reason_code

    @patch("socket.getaddrinfo")
    def test_embedded_form_from_dns_is_refused(self, mock_dns) -> None:
        """Canonicalisation applies to resolved addresses too, not just literals."""
        mock_dns.return_value = _dns("::ffff:169.254.169.254")
        result = check_url("http://rebind.example.com/")
        assert not result.allowed
        assert result.reason_code == REASON_BLOCKED_ADDRESS


class TestClassificationCoverage:
    """
    Coverage is driven by what the standard library says an address *is*, not
    by an enumerated list, so newly reserved space is refused by default.
    """

    @pytest.mark.parametrize("version", [4, 6])
    def test_sweep_refuses_every_non_global_address(self, version: int) -> None:
        """
        Sweep the address space and assert the check agrees with the standard
        library's classification for every address it labels internal.
        """
        refused_classes = (
            "is_loopback",
            "is_link_local",
            "is_site_local",
            "is_private",
        )
        step = (1 << 32) // 4096 if version == 4 else (1 << 128) // 4096
        checked = 0
        for i in range(4096):
            packed = i * step
            try:
                addr = ipaddress.ip_address(packed)
            except ValueError:  # pragma: no cover - range is always valid
                continue
            if version == 6:
                addr = ipaddress.IPv6Address(packed)
            internal = (
                any(getattr(addr, attr, False) for attr in refused_classes)
                or addr.is_reserved
                or addr.is_multicast
                or addr.is_unspecified
                or not addr.is_global
            )
            if not internal:
                continue
            host = f"[{addr}]" if addr.version == 6 else str(addr)
            result = check_url(f"http://{host}/")
            assert not result.allowed, f"{addr} was allowed but is not global"
            checked += 1
        assert checked > 0, "sweep exercised no internal addresses"

    @pytest.mark.parametrize(
        "ip", ["93.184.216.34", "8.8.8.8", "1.1.1.1", "142.250.80.46"]
    )
    def test_globally_routable_addresses_are_allowed(self, ip: str) -> None:
        """Over-blocking real external targets would stall investigations."""
        result = check_url(f"https://{ip}/page")
        assert result.allowed
        assert result.resolved_ips == [ip]

    def test_globally_routable_ipv6_is_allowed(self) -> None:
        result = check_url("https://[2606:4700::1111]/")
        assert result.allowed


class TestFailClosed:
    """Unresolvable and ambiguous destinations must be refused, not allowed."""

    @patch("socket.getaddrinfo")
    def test_refuses_unresolvable_hostname_with_distinct_reason(self, mock_dns) -> None:
        mock_dns.side_effect = socket.gaierror("DNS resolution failed")
        result = check_url("https://unresolvable-domain.example.org/")
        assert not result.allowed
        assert result.reason_code == REASON_RESOLUTION_FAILED
        assert "could not be resolved" in result.reason

    @patch("socket.getaddrinfo")
    def test_resolution_failure_is_distinguishable_from_policy_refusal(
        self, mock_dns
    ) -> None:
        """
        A resolver outage must not look like a policy decision — the analyst
        needs to know which one they are seeing.
        """
        mock_dns.side_effect = socket.gaierror("temporary failure")
        unresolvable = check_url("https://nowhere.example.org/")
        blocked = check_url("http://10.0.0.1/")
        assert unresolvable.reason_code != blocked.reason_code

    @patch("socket.getaddrinfo")
    def test_refuses_mixed_allowed_and_blocked_resolution(self, mock_dns) -> None:
        """One blocked address in the answer set refuses the whole target."""
        mock_dns.return_value = _dns("93.184.216.34", "169.254.169.254")
        result = check_url("http://mixed.example.com/")
        assert not result.allowed
        assert result.reason_code == REASON_BLOCKED_ADDRESS

    @patch("socket.getaddrinfo")
    def test_refuses_mixed_public_and_site_local_resolution(self, mock_dns) -> None:
        mock_dns.return_value = _dns("2606:4700::1111", "fec0::1")
        result = check_url("http://mixed-site-local.example.com/")
        assert not result.allowed
        assert result.reason_code == REASON_BLOCKED_ADDRESS

    @patch("socket.getaddrinfo")
    def test_refuses_mixed_regardless_of_answer_order(self, mock_dns) -> None:
        mock_dns.return_value = _dns("10.0.0.5", "93.184.216.34")
        result = check_url("http://mixed-reverse.example.com/")
        assert not result.allowed

    @patch("socket.getaddrinfo")
    def test_allowed_result_reports_the_vetted_addresses(self, mock_dns) -> None:
        mock_dns.return_value = _dns("93.184.216.34", "1.1.1.1")
        result = check_url("http://example.com/page")
        assert result.allowed
        assert set(result.resolved_ips) == {"93.184.216.34", "1.1.1.1"}


class TestConnectAddressBinding:
    """
    The address a connection is opened to must be one the check approved, so a
    decision made about one address cannot be honoured against another.
    """

    def test_approved_address_is_allowed(self) -> None:
        result = check_connect_address("93.184.216.34", ["93.184.216.34", "1.1.1.1"])
        assert result.allowed

    def test_address_outside_vetted_set_is_refused(self) -> None:
        """Public but unvetted — the decision was made about a different host."""
        result = check_connect_address("8.8.8.8", ["93.184.216.34"])
        assert not result.allowed
        assert result.reason_code == REASON_ADDRESS_NOT_APPROVED

    def test_internal_address_is_refused_even_if_listed_as_approved(self) -> None:
        """Classification wins over the approved set — belt and braces."""
        result = check_connect_address("169.254.169.254", ["169.254.169.254"])
        assert not result.allowed
        assert result.reason_code == REASON_BLOCKED_ADDRESS

    @pytest.mark.parametrize(
        "site_local", ["fec0::", "feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"]
    )
    def test_site_local_address_is_refused_even_if_approved(
        self, site_local: str
    ) -> None:
        result = check_connect_address(site_local, [site_local])
        assert not result.allowed
        assert result.reason_code == REASON_BLOCKED_ADDRESS

    def test_alternate_notation_of_approved_address_is_recognised(self) -> None:
        """Canonicalisation must not cause a false refusal of the same address."""
        result = check_connect_address("::ffff:93.184.216.34", ["93.184.216.34"])
        assert result.allowed

    def test_malformed_connect_address_is_refused(self) -> None:
        result = check_connect_address("not-an-address", ["93.184.216.34"])
        assert not result.allowed


class TestDenylistHostPatterns:
    """Custom host patterns must be enforced."""

    def test_rejects_custom_pattern(self) -> None:
        config = DenylistConfig(denied_host_patterns=["*.internal.corp.com"])
        result = check_url("https://admin.internal.corp.com/api", config)
        assert not result.allowed
        assert "internal.corp.com" in result.reason

    def test_rejects_exact_host_match(self) -> None:
        config = DenylistConfig(denied_host_patterns=["secret-server.local"])
        result = check_url("http://secret-server.local/vault", config)
        assert not result.allowed

    @pytest.mark.parametrize(
        "pattern,hostname",
        [
            ("secret-server.example", "secret-server.example"),
            ("*.example.com", "blocked.example.com"),
        ],
    )
    def test_terminal_dot_cannot_bypass_host_pattern(
        self, pattern: str, hostname: str
    ) -> None:
        config = DenylistConfig(denied_host_patterns=[pattern])

        plain = check_url(f"https://{hostname}/vault", config)
        absolute = check_url(f"https://{hostname}./vault", config)

        assert not plain.allowed
        assert not absolute.allowed
        assert plain.reason_code == absolute.reason_code == REASON_HOST_PATTERN

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_resolution_uses_canonical_hostname(self, mock_dns) -> None:
        result = check_url("https://PUBLIC.EXAMPLE.COM./")

        assert result.allowed
        mock_dns.assert_called_once_with(
            "public.example.com", None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )

    @patch("socket.getaddrinfo")
    def test_allows_non_matching_pattern(self, mock_dns) -> None:
        mock_dns.return_value = _dns("1.2.3.4")
        config = DenylistConfig(denied_host_patterns=["*.internal.corp.com"])
        result = check_url("https://external.example.com/", config)
        assert result.allowed


class TestDenylistEdgeCases:
    """Edge cases and malformed input handling."""

    def test_rejects_non_http_scheme(self) -> None:
        result = check_url("ftp://files.example.com/malware.exe")
        assert not result.allowed
        assert "scheme" in result.reason

    def test_rejects_javascript_scheme(self) -> None:
        result = check_url("javascript:alert(1)")
        assert not result.allowed

    def test_rejects_empty_url(self) -> None:
        result = check_url("")
        assert not result.allowed

    def test_rejects_no_hostname(self) -> None:
        result = check_url("http:///path")
        assert not result.allowed
        assert "no hostname" in result.reason

    def test_rejects_link_local_with_zone_index(self) -> None:
        result = check_url("http://[fe80::1%25eth0]/")
        assert not result.allowed

    def test_canonical_address_rejects_hostnames(self) -> None:
        """A DNS name must not be misread as an alternate numeric form."""
        for host in ["example.com", "beef.cafe", "abc.def", "localhost"]:
            assert canonical_address(host) is None


class TestBackslashNormalization:
    """
    A backslash in an http(s) URL is treated as a path separator by the
    WHATWG URL parser real browsers use (Chromium/CDP included), but Python's
    urlparse does not do this: it reads the text after the last "@" as the
    host, so a string like "http://169.254.169.254\\@good.com/" would decide
    on "good.com" while the browser actually connects to 169.254.169.254.
    """

    def test_backslash_before_at_is_read_as_the_real_host(self) -> None:
        result = check_url("http://169.254.169.254\\@good.com/")
        assert not result.allowed
        assert "169.254.169.254" in result.reason

    def test_backslash_variant_of_loopback_is_refused(self) -> None:
        result = check_url("http://127.0.0.1\\@good.example.com/path")
        assert not result.allowed

    @patch("socket.getaddrinfo")
    def test_ordinary_url_without_backslash_is_unaffected(self, mock_dns) -> None:
        mock_dns.return_value = _dns("93.184.216.34")
        result = check_url("https://example.com/a\\b")
        assert result.allowed


class TestScrubUrlCredentials:
    """Credential scrubbing before persistence."""

    def test_scrubs_api_key(self) -> None:
        url = "https://example.com/callback?api_key=secret123&data=ok"
        result = scrub_url_credentials(url)
        assert "secret123" not in result
        assert "api_key=REDACTED" in result
        assert "data=ok" in result

    def test_scrubs_token(self) -> None:
        url = "https://example.com/?token=abc123"
        result = scrub_url_credentials(url)
        assert "abc123" not in result
        assert "token=REDACTED" in result

    def test_scrubs_password(self) -> None:
        url = "https://login.example.com/?password=hunter2&user=bob"
        result = scrub_url_credentials(url)
        assert "hunter2" not in result
        assert "password=REDACTED" in result
        assert "user=bob" in result

    def test_preserves_non_sensitive_params(self) -> None:
        url = "https://example.com/?page=1&sort=date"
        result = scrub_url_credentials(url)
        assert result == url

    def test_scrubs_multiple_sensitive_params(self) -> None:
        url = "https://api.example.com/?apikey=k1&secret=s2&name=test"
        result = scrub_url_credentials(url)
        assert "k1" not in result
        assert "s2" not in result
        assert "name=test" in result

    def test_scrubs_sensitive_params_with_legacy_semicolon_separator(self) -> None:
        result = scrub_url_credentials("https://example.com/?page=1;token=super-secret")
        assert "super-secret" not in result
        assert "token=REDACTED" in result

    def test_scrubs_userinfo_oauth_signatures_and_fragment(self) -> None:
        url = (
            "https://alice:password@example.com/callback?code=oauth-code&"
            "X-Amz-Signature=signed-value&next=ok#access_token=fragment-token"
        )
        result = scrub_url_credentials(url)
        for secret in [
            "alice",
            "password",
            "oauth-code",
            "signed-value",
            "fragment-token",
        ]:
            assert secret not in result
        assert "[REDACTED]@example.com" in result
        assert "next=ok" in result
