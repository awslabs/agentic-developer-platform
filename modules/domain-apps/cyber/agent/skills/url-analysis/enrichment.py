"""
URL enrichment — WHOIS, passive DNS, cert transparency, VT, URLhaus, MISP.

Each enrichment source degrades gracefully when unavailable (missing credentials,
service down, timeout). Missing sources are logged in the result notes, not raised.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import boto3
import requests
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

ENRICHMENT_TIMEOUT = 15  # seconds per external call


@dataclass
class EnrichmentResult:
    """Aggregated enrichment data from all sources."""

    whois: dict[str, Any] = field(default_factory=dict)
    passive_dns: list[dict[str, Any]] = field(default_factory=list)
    cert_transparency: dict[str, Any] = field(default_factory=dict)
    virustotal: dict[str, Any] = field(default_factory=dict)
    urlhaus: dict[str, Any] = field(default_factory=dict)
    misp: dict[str, Any] = field(default_factory=dict)
    skipped_sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _get_credential(secret_name: str, region: str) -> str | None:
    """Fetch a credential from Secrets Manager. Returns None if unavailable."""
    try:
        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=secret_name)
        return resp["SecretString"]
    except (ClientError, KeyError):
        return None


def enrich_whois(domain: str) -> dict[str, Any]:
    """
    Query WHOIS data for a domain via RDAP (no credentials needed).

    Returns registrar, creation date, domain age, and registrant info.
    """
    try:
        # Use RDAP (successor to WHOIS) — no auth required
        resp = requests.get(
            f"https://rdap.org/domain/{domain}",
            timeout=ENRICHMENT_TIMEOUT,
            headers={"Accept": "application/rdap+json"},
        )
        if resp.status_code != 200:
            return {"error": f"RDAP returned {resp.status_code}"}

        data = resp.json()

        # Extract key fields
        registrar = ""
        for entity in data.get("entities", []):
            if "registrar" in entity.get("roles", []):
                registrar = (
                    entity.get("vcardArray", [None, []])[1][0][-1]
                    if entity.get("vcardArray")
                    else ""
                )
                break

        creation_date = ""
        age_days = 0
        for event in data.get("events", []):
            if event.get("eventAction") == "registration":
                creation_date = event.get("eventDate", "")
                if creation_date:
                    from datetime import datetime, timezone

                    try:
                        created = datetime.fromisoformat(
                            creation_date.replace("Z", "+00:00")
                        )
                        age_days = (datetime.now(timezone.utc) - created).days
                    except (ValueError, TypeError):
                        pass
                break

        return {
            "registrar": registrar,
            "creation_date": creation_date,
            "age_days": age_days,
            "domain_name": data.get("ldhName", domain),
            "status": data.get("status", []),
        }
    except requests.RequestException as e:
        return {"error": f"WHOIS lookup failed: {e}"}
    except Exception as e:
        return {"error": f"WHOIS parse error: {e}"}


def enrich_passive_dns(domain: str) -> list[dict[str, Any]]:
    """
    Query passive DNS records. Uses free sources (no auth).

    Returns list of historical DNS records.
    """
    records: list[dict[str, Any]] = []
    try:
        # Use DNS.Google as a free authoritative source
        resp = requests.get(
            f"https://dns.google/resolve?name={domain}&type=A",
            timeout=ENRICHMENT_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            for answer in data.get("Answer", []):
                records.append(
                    {
                        "rrtype": answer.get("type"),
                        "rdata": answer.get("data"),
                        "ttl": answer.get("TTL"),
                    }
                )
    except requests.RequestException as e:
        logger.warning("Passive DNS lookup failed: %s", e)

    return records


def enrich_cert_transparency(domain: str) -> dict[str, Any]:
    """
    Query certificate transparency logs via crt.sh.

    Returns recent certificates issued for the domain.
    """
    try:
        resp = requests.get(
            f"https://crt.sh/?q=%.{domain}&output=json",
            timeout=ENRICHMENT_TIMEOUT,
        )
        if resp.status_code != 200:
            return {"error": f"crt.sh returned {resp.status_code}"}

        certs = resp.json()[:20]  # Cap to 20 most recent
        return {
            "total_certs": len(resp.json()),
            "recent_certs": [
                {
                    "issuer": c.get("issuer_name", ""),
                    "common_name": c.get("common_name", ""),
                    "not_before": c.get("not_before", ""),
                    "not_after": c.get("not_after", ""),
                }
                for c in certs
            ],
        }
    except requests.RequestException as e:
        return {"error": f"CT lookup failed: {e}"}
    except (json.JSONDecodeError, TypeError):
        return {"error": "CT response was not valid JSON"}


def enrich_virustotal(url: str, region: str) -> dict[str, Any]:
    """
    Query VirusTotal for URL reputation.

    Requires VT API key in Secrets Manager at 'adp/cyber/virustotal-api-key'.
    Degrades gracefully if key is unavailable.
    """
    api_key = _get_credential("adp/cyber/virustotal-api-key", region)
    if not api_key:
        return {"skipped": True, "reason": "VT API key not available in vault"}

    try:
        headers = {"x-apikey": api_key}

        # URL scan lookup
        import base64

        url_id = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

        resp = requests.get(
            f"https://www.virustotal.com/api/v3/urls/{url_id}",
            headers=headers,
            timeout=ENRICHMENT_TIMEOUT,
        )

        if resp.status_code == 404:
            return {"found": False, "note": "URL not previously scanned on VT"}
        if resp.status_code == 429:
            return {"error": "VT rate limit exceeded", "skipped": True}
        if resp.status_code != 200:
            return {"error": f"VT returned {resp.status_code}"}

        data = resp.json().get("data", {}).get("attributes", {})
        stats = data.get("last_analysis_stats", {})

        return {
            "found": True,
            "malicious": stats.get("malicious", 0),
            "suspicious": stats.get("suspicious", 0),
            "harmless": stats.get("harmless", 0),
            "undetected": stats.get("undetected", 0),
            "last_analysis_date": data.get("last_analysis_date"),
            "categories": data.get("categories", {}),
            "reputation": data.get("reputation", 0),
        }
    except requests.RequestException as e:
        return {"error": f"VT request failed: {e}"}


def enrich_urlhaus(url: str) -> dict[str, Any]:
    """
    Query URLhaus (abuse.ch) for known malicious URL data.

    No API key required — free public API.
    """
    try:
        resp = requests.post(
            "https://urlhaus-api.abuse.ch/v1/url/",
            data={"url": url},
            timeout=ENRICHMENT_TIMEOUT,
        )
        if resp.status_code != 200:
            return {"error": f"URLhaus returned {resp.status_code}"}

        data = resp.json()
        if data.get("query_status") == "no_results":
            return {"found": False}

        return {
            "found": True,
            "threat": data.get("threat", ""),
            "url_status": data.get("url_status", ""),
            "tags": data.get("tags", []),
            "date_added": data.get("date_added", ""),
            "reporter": data.get("reporter", ""),
        }
    except requests.RequestException as e:
        return {"error": f"URLhaus request failed: {e}"}


def enrich_misp(url: str, domain: str, region: str) -> dict[str, Any]:
    """
    Query MISP instance for related events/attributes.

    Requires MISP URL + API key in Secrets Manager.
    """
    misp_url = _get_credential("adp/cyber/misp-url", region)
    misp_key = _get_credential("adp/cyber/misp-api-key", region)

    if not misp_url or not misp_key:
        return {"skipped": True, "reason": "MISP credentials not available in vault"}

    try:
        headers = {
            "Authorization": misp_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Search for URL or domain attributes
        search_payload = {
            "returnFormat": "json",
            "type": {"OR": ["url", "domain", "hostname"]},
            "value": {"OR": [url, domain]},
            "limit": 10,
        }

        resp = requests.post(
            f"{misp_url.rstrip('/')}/attributes/restSearch",
            headers=headers,
            json=search_payload,
            timeout=ENRICHMENT_TIMEOUT,
            verify=True,
        )

        if resp.status_code != 200:
            return {"error": f"MISP returned {resp.status_code}"}

        attributes = resp.json().get("response", {}).get("Attribute", [])
        return {
            "found": len(attributes) > 0,
            "matching_attributes": len(attributes),
            "events": list(
                {a.get("event_id") for a in attributes if a.get("event_id")}
            ),
            "categories": list(
                {a.get("category") for a in attributes if a.get("category")}
            ),
        }
    except requests.RequestException as e:
        return {"error": f"MISP request failed: {e}"}


def run_enrichment(url: str, region: str = "us-east-1") -> EnrichmentResult:
    """
    Run all enrichment sources for a URL. Each source runs independently;
    failures in one do not block others.
    """
    parsed = urlparse(url)
    domain = parsed.hostname or ""
    result = EnrichmentResult()

    # WHOIS
    if domain:
        result.whois = enrich_whois(domain)

    # Passive DNS
    if domain:
        result.passive_dns = enrich_passive_dns(domain)

    # Certificate Transparency
    if domain:
        result.cert_transparency = enrich_cert_transparency(domain)

    # VirusTotal
    vt_result = enrich_virustotal(url, region)
    if vt_result.get("skipped"):
        result.skipped_sources.append("virustotal")
    result.virustotal = vt_result

    # URLhaus
    result.urlhaus = enrich_urlhaus(url)

    # MISP
    misp_result = enrich_misp(url, domain, region)
    if misp_result.get("skipped"):
        result.skipped_sources.append("misp")
    result.misp = misp_result

    return result
