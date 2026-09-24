"""Sourced context and bounded, agent-selected lookups. No target-page fetching."""

from __future__ import annotations

import ipaddress
import json
from types import SimpleNamespace
from urllib.parse import quote, urljoin, urlsplit, urlencode

from browser_guard import DestinationRefused, PinnedHTTPTransport, vet_destination
from case_contract import IncidentReport, sanitize, utcnow
from corroboration import lookup_virustotal

SOURCES = ("rdap", "dns", "cert_transparency", "virustotal", "urlhaus", "common_crawl")


def incident_records(reports):
    if not isinstance(reports, list) or len(reports) > 10:
        raise ValueError("Supply at most ten sourced incident reports")
    return [
        {
            **sanitize(IncidentReport.model_validate(report).model_dump()),
            "id": f"incident-{i + 1:03d}",
            "kind": "incident_report",
            "status": "reported",
            "checked_at": utcnow(),
            "provenance": "researcher_supplied_not_independently_verified",
        }
        for i, report in enumerate(reports)
    ]


def context_records(case):
    return case.get("incident_context", []) + case.get("corroboration", [])


class ProviderJSON:
    """Reuse address-pinned transport; fetch only tool-owned provider URLs.

    Redirects are never followed. RDAP registry endpoints come from IANA's HTTPS
    bootstrap, not a target page. Every provider socket is vetted and pinned.
    """

    def __init__(self):
        self.transport = PinnedHTTPTransport(
            timeout=5,
            response_timeout=10,
            analysis_timeout=20,
            max_response_bytes=1024 * 1024,
            max_analysis_bytes=2 * 1024 * 1024,
        )

    def __call__(self, url, *, data=None, headers=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise ValueError("Provider endpoint must be HTTPS")
        response = self.transport.fetch(
            SimpleNamespace(
                url=url,
                method="POST" if data is not None else "GET",
                post_data_buffer=data,
                all_headers=lambda: {"Accept": "application/json", **(headers or {})},
            ),
            vet_destination(url),
        )
        if response.status != 200:
            raise ValueError(f"Provider returned HTTP {response.status}")
        return json.loads(response.body)


def lookup(source, url, *, api_key=None, get_json=None):
    if source not in SOURCES:
        raise ValueError("Unsupported enrichment source")
    if source == "common_crawl":
        from common_crawl import lookup_common_crawl

        return lookup_common_crawl(url)
    if source == "virustotal":
        return lookup_virustotal(url, api_key)
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    record = {
        "kind": "enrichment",
        "source": source,
        "checked_at": utcnow(),
        "subject": host,
        "status": "unavailable",
        "verdict_effect": "model_assessed",
    }
    if literal and source != "urlhaus":
        return {
            **record,
            "status": "skipped",
            "reason": "This source requires a DNS hostname",
        }
    get_json = get_json or ProviderJSON()
    try:
        if source == "dns":
            data = get_json(
                "https://dns.google/resolve?name=" + quote(host, safe="") + "&type=A"
            )
            return {
                **record,
                "status": "available",
                "dns_status": data.get("Status"),
                "answers": data.get("Answer", [])[:30],
                "limitations": [
                    "Current DNS lookup, not historical passive DNS or proof of ownership."
                ],
            }
        if source == "cert_transparency":
            data = get_json(
                "https://crt.sh/?q=" + quote(host, safe="") + "&output=json"
            )
            return {
                **record,
                "status": "available",
                "certificates": [
                    {
                        k: c.get(k)
                        for k in (
                            "issuer_name",
                            "common_name",
                            "not_before",
                            "not_after",
                        )
                    }
                    for c in data[:20]
                ],
                "limitations": [
                    "Certificate issuance does not establish legitimacy; records may be incomplete."
                ],
            }
        if source == "urlhaus":
            if not api_key:
                return {
                    **record,
                    "status": "skipped",
                    "reason": "URLhaus Auth-Key is not configured",
                }
            data = get_json(
                "https://urlhaus-api.abuse.ch/v1/url/",
                data=urlencode({"url": url}).encode(),
                headers={
                    "Auth-Key": api_key,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            return {
                **record,
                "status": "available",
                "query_status": data.get("query_status"),
                "reported": {
                    k: data.get(k)
                    for k in (
                        "url_status",
                        "threat",
                        "tags",
                        "date_added",
                        "last_online",
                        "blacklists",
                    )
                },
                "limitations": [
                    "Provider-reported reputation, not proof of current page behavior."
                ],
            }
        bootstrap = get_json("https://data.iana.org/rdap/dns.json")
        endpoints = next(
            (
                urls
                for tlds, urls in bootstrap["services"]
                if host.rsplit(".", 1)[-1] in tlds
            ),
            [],
        )
        base = next((s for s in endpoints if s.startswith("https://")), None)
        if not base:
            return {
                **record,
                "status": "skipped",
                "reason": "No HTTPS registry endpoint in IANA bootstrap",
            }
        import tldextract

        parsed_domain = tldextract.TLDExtract(
            suffix_list_urls=(), cache_dir=None, include_psl_private_domains=False
        )(host)
        registered = parsed_domain.top_domain_under_public_suffix
        if not registered:
            return {
                **record,
                "status": "skipped",
                "reason": "Registrable domain unavailable in bundled public suffix data",
            }
        record["queried_domain"] = registered
        data = get_json(
            urljoin(base.rstrip("/") + "/", "domain/" + quote(registered, safe=""))
        )
        return {
            **record,
            "status": "available",
            "provider": urlsplit(base).hostname,
            "domain": data.get("ldhName"),
            "events": data.get("events", [])[:20],
            "domain_status": data.get("status", [])[:20],
            "limitations": [
                "Public-suffix-derived registration lookup; parent registration does not identify a hosted page operator."
            ],
        }
    except (DestinationRefused, OSError, ValueError, KeyError, TypeError) as error:
        return {
            **record,
            "reason": "Provider lookup failed, was refused, or exceeded its budget",
            "error_type": type(error).__name__,
            "diagnostic": sanitize(str(error))[:500],
        }
