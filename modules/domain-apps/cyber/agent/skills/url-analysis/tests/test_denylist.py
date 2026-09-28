"""Unit tests for the URL denylist module."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from denylist import (
    DenylistConfig,
    check_url,
    scrub_url_credentials,
)


class TestDenylistRejectsInternalRanges:
    """Denylist must reject all RFC 1918 and reserved ranges."""

    @pytest.mark.parametrize(
        "url,reason_contains",
        [
            ("http://10.0.0.1/admin", "10.0.0.0/8"),
            ("http://10.255.255.255/path", "10.0.0.0/8"),
            ("http://172.16.0.1/api", "172.16.0.0/12"),
            ("http://172.31.255.255/x", "172.16.0.0/12"),
            ("http://192.168.1.1/login", "192.168.0.0/16"),
            ("http://192.168.0.100:8080/", "192.168.0.0/16"),
            ("http://127.0.0.1/", "127.0.0.0/8"),
            ("http://127.0.0.53/dns", "127.0.0.0/8"),
        ],
    )
    def test_rejects_private_ip_literals(self, url: str, reason_contains: str) -> None:
        result = check_url(url)
        assert not result.allowed
        assert reason_contains in result.reason

    def test_rejects_aws_metadata_ip(self) -> None:
        result = check_url("http://169.254.169.254/latest/meta-data/")
        assert not result.allowed
        assert "169.254.169.254" in result.reason

    def test_rejects_link_local(self) -> None:
        result = check_url("http://169.254.1.1/")
        assert not result.allowed
        assert "169.254.0.0/16" in result.reason

    def test_rejects_ipv6_loopback(self) -> None:
        result = check_url("http://[::1]/admin")
        assert not result.allowed

    @patch("socket.getaddrinfo")
    def test_rejects_hostname_resolving_to_internal(self, mock_dns: patch) -> None:
        """Hostname that resolves to a private IP must be rejected."""
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", 80))
        ]
        result = check_url("http://evil-internal.example.com/steal")
        assert not result.allowed
        assert "10.0.0.5" in result.reason


class TestDenylistAllowsPublicURLs:
    """Denylist must allow legitimate public internet URLs."""

    @patch("socket.getaddrinfo")
    def test_allows_public_ip(self, mock_dns: patch) -> None:
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 80))
        ]
        result = check_url("http://example.com/page")
        assert result.allowed

    @patch("socket.getaddrinfo")
    def test_allows_google(self, mock_dns: patch) -> None:
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("142.250.80.46", 443))
        ]
        result = check_url("https://www.google.com/search?q=test")
        assert result.allowed

    def test_allows_public_ip_literal(self) -> None:
        result = check_url("https://93.184.216.34/page")
        assert result.allowed

    @patch("socket.getaddrinfo")
    def test_allows_dns_failure_gracefully(self, mock_dns: patch) -> None:
        """If DNS fails, allow (AgentCore handles DNS itself)."""
        mock_dns.side_effect = socket.gaierror("DNS resolution failed")
        result = check_url("https://unresolvable-domain.example.org/")
        assert result.allowed
        assert result.resolved_ips == []


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

    @patch("socket.getaddrinfo")
    def test_allows_non_matching_pattern(self, mock_dns: patch) -> None:
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 443))
        ]
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
