"""
URL denylist validation — prevents analysis of internal/private network URLs.

Rejects:
- RFC 1918 private ranges (10/8, 172.16/12, 192.168/16)
- Link-local (169.254/16, including AWS metadata at 169.254.169.254)
- Loopback (127/8)
- Custom host patterns (glob-style)

Always enforced before any AgentCore Browser session is created.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import urlparse


# Default private/reserved CIDR ranges that must never be browsed
DEFAULT_DENIED_CIDRS: list[str] = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",  # Link-local + AWS metadata
    "127.0.0.0/8",  # Loopback
    "0.0.0.0/8",  # "This" network
    "::1/128",  # IPv6 loopback
    "fc00::/7",  # IPv6 unique local
    "fe80::/10",  # IPv6 link-local
]

# Specific IPs that are always blocked regardless of CIDR config
ALWAYS_BLOCKED_IPS: list[str] = [
    "169.254.169.254",  # AWS instance metadata
    "fd00:ec2::254",  # AWS IMDSv2 IPv6
]


@dataclass
class DenylistConfig:
    """Configuration for the URL denylist."""

    denied_cidrs: list[str] = field(default_factory=lambda: list(DEFAULT_DENIED_CIDRS))
    denied_host_patterns: list[str] = field(default_factory=list)
    always_blocked_ips: list[str] = field(
        default_factory=lambda: list(ALWAYS_BLOCKED_IPS)
    )


@dataclass
class DenylistResult:
    """Result of a denylist check."""

    allowed: bool
    reason: str = ""
    resolved_ips: list[str] = field(default_factory=list)


def _parse_networks(
    cidrs: list[str],
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse CIDR strings into network objects, skipping invalid entries."""
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return networks


def _resolve_hostname(hostname: str) -> list[str]:
    """Resolve hostname to IP addresses. Returns empty list on failure."""
    try:
        results = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
        return list({r[4][0] for r in results})
    except (socket.gaierror, OSError):
        return []


def _is_ip_denied(
    ip_str: str,
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    always_blocked: list[str],
) -> str | None:
    """Check if an IP is in denied ranges. Returns reason string or None."""
    if ip_str in always_blocked:
        return f"IP {ip_str} is explicitly blocked (metadata/reserved)"

    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return None

    for network in networks:
        if addr in network:
            return f"IP {ip_str} resolves to denied range {network}"

    return None


def _matches_host_pattern(hostname: str, patterns: list[str]) -> str | None:
    """Check if hostname matches any denied glob pattern. Returns reason or None."""
    hostname_lower = hostname.lower()
    for pattern in patterns:
        pattern_lower = pattern.lower()
        if fnmatch(hostname_lower, pattern_lower):
            return f"hostname '{hostname}' matches denied pattern '{pattern}'"
    return None


def check_url(url: str, config: DenylistConfig | None = None) -> DenylistResult:
    """
    Check whether a URL is allowed for analysis.

    Validates:
    1. URL is well-formed with http/https scheme
    2. Hostname doesn't match denied patterns
    3. Resolved IPs are not in denied CIDR ranges

    Args:
        url: The URL to validate
        config: Denylist configuration (uses defaults if None)

    Returns:
        DenylistResult with allowed=True if URL can be analyzed
    """
    if config is None:
        config = DenylistConfig()

    # Parse URL
    try:
        parsed = urlparse(url)
    except Exception:
        return DenylistResult(
            allowed=False, reason="URL is malformed and cannot be parsed"
        )

    # Validate scheme
    if parsed.scheme not in ("http", "https"):
        return DenylistResult(
            allowed=False,
            reason=f"URL scheme '{parsed.scheme}' is not allowed (only http/https)",
        )

    # Extract hostname
    hostname = parsed.hostname
    if not hostname:
        return DenylistResult(allowed=False, reason="URL has no hostname")

    # Check host patterns
    pattern_match = _matches_host_pattern(hostname, config.denied_host_patterns)
    if pattern_match:
        return DenylistResult(allowed=False, reason=pattern_match)

    # Check if hostname is already an IP literal
    try:
        addr = ipaddress.ip_address(hostname)
        # It's an IP literal — check directly
        networks = _parse_networks(config.denied_cidrs)
        reason = _is_ip_denied(str(addr), networks, config.always_blocked_ips)
        if reason:
            return DenylistResult(
                allowed=False, reason=reason, resolved_ips=[str(addr)]
            )
        return DenylistResult(allowed=True, resolved_ips=[str(addr)])
    except ValueError:
        pass  # Not an IP literal, it's a hostname — resolve it

    # Resolve hostname to IPs
    resolved_ips = _resolve_hostname(hostname)
    if not resolved_ips:
        # Cannot resolve — allow (AgentCore will handle DNS itself)
        # We only block what we can definitively identify as internal
        return DenylistResult(allowed=True, resolved_ips=[])

    # Check each resolved IP against denied ranges
    networks = _parse_networks(config.denied_cidrs)
    for ip_str in resolved_ips:
        reason = _is_ip_denied(ip_str, networks, config.always_blocked_ips)
        if reason:
            return DenylistResult(
                allowed=False, reason=reason, resolved_ips=resolved_ips
            )

    return DenylistResult(allowed=True, resolved_ips=resolved_ips)


def scrub_url_credentials(url: str) -> str:
    """
    Remove sensitive query parameters from a URL before persistence.

    Masks values of parameters that look like credentials/tokens.
    """
    sensitive_params = re.compile(
        r"(api_key|apikey|token|secret|password|passwd|auth|key|session_id|sid)"
        r"=([^&]+)",
        re.IGNORECASE,
    )
    return sensitive_params.sub(r"\1=REDACTED", url)
