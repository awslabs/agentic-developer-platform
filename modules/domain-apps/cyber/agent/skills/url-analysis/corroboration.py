"""Explicit, timestamped context. Neither brand relationships nor reputation clear a page."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from case_contract import Contract, digest, redact_url, utcnow
from denylist import canonical_hostname
from pydantic import Field, HttpUrl, field_validator


class BrandReference(Contract):
    brand: str = Field(min_length=1, max_length=200)
    official_domains: list[str] = Field(min_length=1, max_length=30)
    authorized_identity_domains: list[str] = Field(default_factory=list, max_length=30)
    source_url: HttpUrl
    verified_at: datetime
    verified_by: str = Field(min_length=1, max_length=200)

    @field_validator("official_domains", "authorized_identity_domains")
    @classmethod
    def domains(cls, values):
        result = []
        for value in values:
            if any(c in value for c in "/:@*?#") or "." not in value:
                raise ValueError(
                    "Use exact reference domains without wildcards or URL components"
                )
            result.append(canonical_hostname(value))
        return result

    @field_validator("verified_at")
    @classmethod
    def timestamp(cls, value):
        if value.tzinfo is None or value > datetime.now(timezone.utc):
            raise ValueError(
                "Reference verification needs a timezone and cannot be in the future"
            )
        return value


def domain_relationship(host, reference):
    host = canonical_hostname(host)
    for field, relationship in (
        ("official_domains", "official_domain"),
        ("authorized_identity_domains", "authorized_identity_provider"),
    ):
        if any(host == d or host.endswith("." + d) for d in getattr(reference, field)):
            return relationship
    return "unverified_relationship"


def compare_brand(case, reference):
    reference = BrandReference.model_validate(reference)
    comparisons = []
    targets = [("seed", case["target_url"], None)]
    for observation in case["observations"]:
        targets.append(
            ("observed_page", observation.get("final_url", ""), observation["id"])
        )
        targets.extend(
            (
                "declared_form_destination_not_submitted",
                f.get("action", ""),
                observation["id"],
            )
            for f in observation.get("forms", [])
        )
    for role, url, observation_id in targets:
        host = urlsplit(url).hostname
        if host:
            comparisons.append(
                {
                    "role": role,
                    "url": redact_url(url),
                    "observation_id": observation_id,
                    "relationship": domain_relationship(host, reference),
                }
            )
    return {
        "kind": "brand_reference",
        "checked_at": utcnow(),
        "subject_sha256": case["subject_sha256"],
        "reference": reference.model_dump(mode="json"),
        "comparisons": comparisons,
        "limitations": [
            "The researcher-supplied reference is not independently verified by this tool.",
            "Unlisted domains may be legitimate providers. A listed domain does not establish page safety.",
            "Observed branding and credential handling still require evidence review.",
        ],
    }


def lookup_virustotal(url, api_key, *, get=requests.get):
    record = {
        "kind": "reputation",
        "source": "VirusTotal URL lookup",
        "checked_at": utcnow(),
        "subject_sha256": digest(url),
        "status": "unavailable",
        "verdict_effect": "model_assessed",
    }
    if not api_key:
        return {
            **record,
            "status": "skipped",
            "reason": "VirusTotal credential is not configured",
        }
    url_id = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    try:
        with get(
            "https://www.virustotal.com/api/v3/urls/" + url_id,
            headers={"x-apikey": api_key},
            timeout=15,
            allow_redirects=False,
            stream=True,
        ) as response:
            record["http_status"] = response.status_code
            if response.status_code != 200:
                return {
                    **record,
                    "status": "not_found"
                    if response.status_code == 404
                    else "unavailable",
                }
            chunks, length = [], 0
            for chunk in response.iter_content(65536):
                length += len(chunk)
                if length > 1024 * 1024:
                    return {
                        **record,
                        "reason": "Reputation response exceeded its size limit",
                    }
                chunks.append(chunk)
            attributes = json.loads(b"".join(chunks))["data"]["attributes"]
            record.update(
                status="available",
                last_analysis_date=attributes.get("last_analysis_date"),
                stats=attributes.get("last_analysis_stats", {}),
            )
            return record
    except (requests.RequestException, ValueError, KeyError, TypeError):
        # Do not copy exception strings that may include credentials or URL parameters.
        return {**record, "reason": "Reputation lookup failed"}
