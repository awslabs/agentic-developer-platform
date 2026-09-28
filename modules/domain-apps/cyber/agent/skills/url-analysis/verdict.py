"""
Verdict synthesis — analyze browser evidence + enrichment to produce a structured verdict.

Scoring logic:
- Combines signals from browser evidence, enrichment, and heuristics
- Each signal contributes a weighted score toward malicious/suspicious/clean
- Confidence is derived from the number and quality of signals available
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Verdict:
    """Structured analysis verdict."""

    severity: str  # "clean" | "suspicious" | "malicious"
    confidence: int  # 0-100
    category: str  # "phishing" | "malware-delivery" | "c2" | "scam" | "unclassified-risk" | "false-positive"
    reasoning: str
    mitre_attack: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "confidence": self.confidence,
            "category": self.category,
            "reasoning": self.reasoning,
            "mitre_attack": self.mitre_attack,
            "recommended_actions": self.recommended_actions,
        }


# Known-good domains that skip full analysis
KNOWN_GOOD_DOMAINS: set[str] = {
    "google.com",
    "www.google.com",
    "github.com",
    "www.github.com",
    "microsoft.com",
    "www.microsoft.com",
    "apple.com",
    "www.apple.com",
    "amazon.com",
    "www.amazon.com",
    "cloudflare.com",
    "www.cloudflare.com",
}

# Phishing indicators in page content
PHISHING_KEYWORDS: list[str] = [
    "verify your account",
    "confirm your identity",
    "update your payment",
    "suspended your account",
    "unusual activity",
    "verify your email",
    "click here to login",
    "reset your password immediately",
]

# Suspicious TLD patterns
SUSPICIOUS_TLDS: set[str] = {
    ".tk",
    ".ml",
    ".ga",
    ".cf",
    ".gq",  # Free TLDs abused for phishing
    ".xyz",
    ".top",
    ".buzz",
    ".icu",  # Commonly abused
    ".zip",
    ".mov",  # Confusing TLDs
}


def _score_domain_age(whois: dict[str, Any]) -> tuple[int, str]:
    """Score based on domain age. Newer domains are more suspicious."""
    age_days = whois.get("age_days", -1)
    if age_days < 0:
        return 0, ""
    if age_days < 7:
        return 30, f"Domain registered {age_days} days ago (very new)"
    if age_days < 30:
        return 20, f"Domain registered {age_days} days ago (new)"
    if age_days < 90:
        return 10, f"Domain registered {age_days} days ago (recent)"
    return -5, f"Domain is {age_days} days old (established)"


def _score_virustotal(vt: dict[str, Any]) -> tuple[int, str]:
    """Score based on VirusTotal results."""
    if vt.get("skipped") or not vt.get("found"):
        return 0, ""

    malicious = vt.get("malicious", 0)
    suspicious = vt.get("suspicious", 0)
    total_bad = malicious + suspicious

    if total_bad == 0:
        return -15, "VirusTotal: 0 detections (clean)"
    if total_bad <= 2:
        return 10, f"VirusTotal: {total_bad} detections (low)"
    if total_bad <= 5:
        return 25, f"VirusTotal: {total_bad} detections (moderate)"
    return 40, f"VirusTotal: {total_bad} detections (high)"


def _score_urlhaus(urlhaus: dict[str, Any]) -> tuple[int, str]:
    """Score based on URLhaus results."""
    if not urlhaus.get("found"):
        return 0, ""
    threat = urlhaus.get("threat", "unknown")
    return 40, f"URLhaus: known malicious ({threat})"


def _score_forms(forms: list[dict[str, Any]]) -> tuple[int, str]:
    """Score based on detected forms — credential harvesting indicator."""
    if not forms:
        return 0, ""

    password_forms = 0
    for form in forms:
        fields = form.get("fields", [])
        field_types = {f.get("type", "").lower() for f in fields}
        if "password" in field_types:
            password_forms += 1

    if password_forms > 0:
        return 25, f"Page contains {password_forms} password form(s)"
    return 5, f"Page contains {len(forms)} form(s) (no password fields)"


def _score_redirects(redirect_chain: list[str]) -> tuple[int, str]:
    """Score based on redirect chain length — excessive redirects are suspicious."""
    count = len(redirect_chain)
    if count <= 1:
        return 0, ""
    if count <= 3:
        return 5, f"Redirect chain: {count} hops"
    return 15, f"Redirect chain: {count} hops (excessive)"


def _score_auto_downloads(downloads: list[dict[str, Any]]) -> tuple[int, str]:
    """Score based on auto-download detection."""
    if not downloads:
        return 0, ""
    return 30, f"Auto-download detected: {len(downloads)} file(s)"


def _score_phishing_content(visible_text: str) -> tuple[int, str]:
    """Score based on phishing keyword detection in page text."""
    text_lower = visible_text.lower()
    hits = [kw for kw in PHISHING_KEYWORDS if kw in text_lower]
    if not hits:
        return 0, ""
    if len(hits) >= 3:
        return 30, f"Multiple phishing keywords detected: {hits[:3]}"
    return 15, f"Phishing keyword detected: {hits[0]}"


def _score_tld(domain: str) -> tuple[int, str]:
    """Score based on TLD reputation."""
    for tld in SUSPICIOUS_TLDS:
        if domain.endswith(tld):
            return 10, f"Suspicious TLD: {tld}"
    return 0, ""


def _score_misp(misp: dict[str, Any]) -> tuple[int, str]:
    """Score based on MISP hits."""
    if misp.get("skipped") or not misp.get("found"):
        return 0, ""
    count = misp.get("matching_attributes", 0)
    return 35, f"MISP: {count} matching attribute(s) in threat intel"


def _determine_category(
    scores: list[tuple[int, str]],
    forms: list[dict[str, Any]],
    downloads: list[dict[str, Any]],
    urlhaus: dict[str, Any],
) -> str:
    """Determine the threat category based on signal types."""
    reasons = [r for _, r in scores if r]

    # Check for phishing indicators
    has_password_form = any("password form" in r for r in reasons)
    has_phishing_kw = any("phishing keyword" in r.lower() for r in reasons)
    if has_password_form or has_phishing_kw:
        return "phishing"

    # Check for malware delivery
    if downloads:
        return "malware-delivery"

    # Check URLhaus classification
    urlhaus_threat = urlhaus.get("threat", "")
    if "malware" in urlhaus_threat.lower():
        return "malware-delivery"
    if "phishing" in urlhaus_threat.lower():
        return "phishing"

    # Default
    return "unclassified-risk"


def _determine_mitre(category: str, evidence_signals: list[str]) -> list[str]:
    """Map category + signals to MITRE ATT&CK technique IDs."""
    techniques = []

    if category == "phishing":
        techniques.append("T1566.002")  # Phishing: Spearphishing Link
        if any("password form" in s for s in evidence_signals):
            techniques.append("T1056.003")  # Input Capture: Web Portal Capture

    if category == "malware-delivery":
        techniques.append("T1189")  # Drive-by Compromise
        techniques.append("T1204.001")  # User Execution: Malicious Link

    if any("redirect" in s.lower() for s in evidence_signals):
        techniques.append("T1608.005")  # Stage Capabilities: Link Target

    return techniques


def _determine_actions(severity: str, category: str, domain: str) -> list[str]:
    """Generate concrete recommended actions based on verdict."""
    actions = []

    if severity == "malicious":
        actions.append(f"Block domain '{domain}' at web proxy and DNS sinkhole")
        actions.append(f"Add '{domain}' to perimeter denylist")
        actions.append(
            "Search email gateway logs for this URL — quarantine matching messages"
        )
        actions.append("Alert affected users who may have clicked the link")
        if category == "phishing":
            actions.append(
                "Force password reset for any users who submitted credentials"
            )
        if category == "malware-delivery":
            actions.append("Scan endpoints that visited this URL for dropped payloads")
    elif severity == "suspicious":
        actions.append(f"Monitor traffic to '{domain}' — do not block yet")
        actions.append(
            "Investigate further with full sandbox detonation if payload was downloaded"
        )
        actions.append("Add to watchlist for 7-day monitoring period")
    else:
        actions.append("No action required")

    return actions


def synthesize_verdict(
    url: str,
    domain: str,
    browser_evidence: dict[str, Any],
    enrichment: dict[str, Any],
) -> Verdict:
    """
    Synthesize a verdict from browser evidence and enrichment data.

    Combines weighted signals from multiple sources to produce a
    severity rating, confidence score, and actionable recommendations.
    """
    scores: list[tuple[int, str]] = []

    # Domain-based signals
    whois = enrichment.get("whois", {})
    scores.append(_score_domain_age(whois))
    scores.append(_score_tld(domain))

    # Reputation-based signals
    scores.append(_score_virustotal(enrichment.get("virustotal", {})))
    scores.append(_score_urlhaus(enrichment.get("urlhaus", {})))
    scores.append(_score_misp(enrichment.get("misp", {})))

    # Browser evidence signals
    forms = browser_evidence.get("forms_detected", [])
    scores.append(_score_forms(forms))
    scores.append(_score_redirects(browser_evidence.get("redirect_chain", [])))
    scores.append(_score_auto_downloads(browser_evidence.get("auto_downloads", [])))
    scores.append(_score_phishing_content(browser_evidence.get("visible_text", "")))

    # Calculate total score
    total_score = sum(s for s, _ in scores)
    evidence_signals = [r for _, r in scores if r]

    # Determine severity
    if domain in KNOWN_GOOD_DOMAINS:
        severity = "clean"
        total_score = -50  # Override
    elif total_score >= 50:
        severity = "malicious"
    elif total_score >= 20:
        severity = "suspicious"
    else:
        severity = "clean"

    # Determine confidence based on available evidence
    evidence_count = len([s for s, r in scores if r])  # Non-empty signals
    enrichment_sources_available = sum(
        1
        for k in ("virustotal", "urlhaus", "misp")
        if enrichment.get(k, {}).get("found")
        or (not enrichment.get(k, {}).get("skipped"))
    )

    if evidence_count >= 5 and enrichment_sources_available >= 2:
        confidence = min(95, 60 + evidence_count * 5)
    elif evidence_count >= 3:
        confidence = min(80, 40 + evidence_count * 5)
    else:
        confidence = max(30, 20 + evidence_count * 10)

    # If known-good domain, high confidence clean
    if domain in KNOWN_GOOD_DOMAINS:
        confidence = 95

    # Determine category
    urlhaus = enrichment.get("urlhaus", {})
    downloads = browser_evidence.get("auto_downloads", [])
    category = _determine_category(scores, forms, downloads, urlhaus)
    if severity == "clean":
        category = "false-positive"

    # MITRE mapping
    mitre = _determine_mitre(category, evidence_signals)

    # Recommended actions
    actions = _determine_actions(severity, category, domain)

    # Build reasoning
    reasoning_parts = [r for _, r in scores if r]
    if not reasoning_parts:
        reasoning = "No significant signals detected. URL appears benign."
    else:
        reasoning = "; ".join(reasoning_parts[:5])  # Top 5 signals

    return Verdict(
        severity=severity,
        confidence=confidence,
        category=category,
        reasoning=reasoning,
        mitre_attack=mitre,
        recommended_actions=actions,
    )
