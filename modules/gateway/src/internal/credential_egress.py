"""Credential egress binding — which hosts may receive a given service's secret.

Issue #4076 (sub-EPIC #4068 · F): vault-secret egress binding.

The proxy-request host allowlist (``_validate_proxy_url`` in
``credential_routes.py``) answers "may the platform talk to this host at all?".
It does NOT answer "may *this credential* be sent to this host?" — so a
``github`` credential could be injected into a request to any other allowlisted
host (e.g. ``slack.com``).  This module supplies the missing binding.

Design decisions (approved on #4076):

* The map is a **code constant**, not SSM and not a DB column.  It is a security
  invariant, so it must be reviewable in the diff; it needs no migration; and it
  must not be tenant-writable.  Shape follows the two in-repo precedents:
  ``shared/identity/providers.py`` (``SUPPORTED_PROVIDERS``) and
  ``credential_injector.py`` (``FILE_CREDENTIAL_TYPES``).
* ``UserCredential.service`` is *deliberately* free-form (see
  ``docs/user-identity-and-credentials-design.md``: "stays a free-form string in
  the schema"), so this map can never be complete.  Therefore: **mapped services
  fail closed, unmapped services fail open with a WARN.**  Failing closed on
  unmapped would break every custom credential (``custom-api-foo`` is a
  documented supported value); failing open on mapped would make the control
  cosmetic.  For unmapped services the global host allowlist remains the only
  egress control — that is acceptable *because* the allowlist is strong
  (HTTPS-only, embedded-credential rejection, private/loopback/link-local/
  reserved-IP rejection, empty = deny-all).
* Only services whose host set is **not deployment-specific** are listed.
  ``jira``/``atlassian`` is deliberately absent: the host is the tenant's own
  ``*.atlassian.net`` (or a self-hosted Jira), i.e. per-tenant data that a
  process-global constant cannot express.  Per-tenant host lists are the
  follow-on Wave 3 design (``Organization.settings`` JSON).  ``aws`` is absent
  for the same reason.

This module is pure — no I/O, no DB, no AWS.  Tests import it directly.
"""

from __future__ import annotations

# Service → the host patterns that service's credential may be sent to.
#
# Patterns use the same syntax as BG_VAULT_PROXY_HOST_ALLOWLIST: exact match, or
# a "*." prefix which suffix-matches subdomains AND the bare apex.
#
# Adding an entry makes that service fail CLOSED for every other host, so only
# add a service whose host set is the same in every deployment.  Anything
# deployment- or tenant-specific must stay unmapped (fail-open) until per-tenant
# host lists land.
SERVICE_HOST_BINDINGS: dict[str, frozenset[str]] = {
    "github": frozenset({"api.github.com", "github.com", "uploads.github.com", "codeload.github.com"}),
    "openai": frozenset({"api.openai.com"}),
    "anthropic": frozenset({"api.anthropic.com"}),
    "stripe": frozenset({"api.stripe.com", "files.stripe.com"}),
    "slack": frozenset({"slack.com", "*.slack.com"}),
}


def host_matches(hostname: str, patterns: frozenset[str] | set[str]) -> bool:
    """Return True if ``hostname`` matches any of ``patterns``.

    Extracted verbatim from the proxy host-allowlist matcher so the allowlist
    check and the credential→host binding check share one implementation.
    Forking it is how a bypass gets fixed in one copy and not the other.

    Matching rules:
        ``example.com``    — exact match only.
        ``*.example.com``  — matches ``sub.example.com`` and the bare
                             ``example.com`` apex.

    Deliberately strict, and these properties are pinned by tests:
        * ``evil-example.com`` does NOT match ``*.example.com`` (the wildcard
          retains its leading dot, so the suffix compared is
          ``".example.com"``).
        * ``sub.example.com.`` (trailing-dot FQDN) does NOT match — fails closed.
        * ``x.example.com.evil.com`` does NOT match.
    """
    hostname_lower = hostname.lower()
    for pattern in patterns:
        pattern_lower = pattern.strip().lower()
        if not pattern_lower:
            continue
        if pattern_lower.startswith("*."):
            suffix = pattern_lower[1:]  # ".example.com"
            if hostname_lower.endswith(suffix) or hostname_lower == pattern_lower[2:]:
                return True
        elif hostname_lower == pattern_lower:
            return True
    return False


def is_binding_enforced(service: str) -> bool:
    """Return True if ``service`` has a registered host set (i.e. fails closed).

    Unmapped services return False — they fall back to the global host allowlist.
    """
    return service.strip().lower() in SERVICE_HOST_BINDINGS


def allowed_hosts_for(service: str) -> frozenset[str]:
    """Return the host patterns bound to ``service``, or an empty set if unmapped.

    An empty return means "not bound" — NOT "deny all".  Callers must check
    ``is_binding_enforced`` (or treat empty as unmapped) rather than reading an
    empty set as a denial.
    """
    return SERVICE_HOST_BINDINGS.get(service.strip().lower(), frozenset())
