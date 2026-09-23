"""
Example orchestration script: Phishing credential-harvest form detection.

Demonstrates full Evidence population for a page with a login form:
uses the broker's fixed form extraction, detects
brand mismatch between visible text and form `action` host, and
flags it as an `anti_analysis_signal` for the verdict engine.

Expected verdict: malicious (category=credential-harvest) when the
form action hostname differs from the rendered-brand hostname.
"""

import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

from browser_client import analyze_url
from browser_guard import DestinationRefused

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://example-phish.test/login"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_of(u: str) -> str:
    try:
        return urlparse(u).hostname or ""
    except ValueError:
        return ""


# -- Main --
run_started_at = iso_now()
try:
    result = analyze_url(URL)
    session_id = result["session_id"]
    final_url = result["final_url"]
    http_status = result["http_status"]
    page_title = result["page_title"]
    screenshot_b64 = result["screenshot_base64"]
    visible_text = result["visible_text"]
    forms_raw = result["forms"]
    orphan_inputs = result["orphan_inputs"]

    run_completed_at = iso_now()

    # -- Anti-analysis / brand-impersonation heuristics --
    anti_analysis_signals = []

    has_password_field = any(
        fd.get("type") == "password" for f in forms_raw for fd in f.get("fields", [])
    ) or any(i.get("type") == "password" for i in orphan_inputs)

    if has_password_field:
        page_host = host_of(final_url)
        # Brand mismatch: form action posts to a different host than the
        # one the user sees in the address bar.
        for f in forms_raw:
            action_host = host_of(f.get("action", ""))
            if action_host and page_host and action_host != page_host:
                anti_analysis_signals.append(
                    f"form_action_host_mismatch:{page_host}->{action_host}"
                )
                break

        # Famous-brand keywords in title/text but domain is unrelated
        brand_hints = [
            "microsoft",
            "office 365",
            "outlook",
            "google",
            "gmail",
            "apple",
            "icloud",
            "paypal",
            "amazon",
            "netflix",
            "bank",
        ]
        title_lower = (page_title or "").lower()
        text_lower = visible_text[:2000].lower()
        for brand in brand_hints:
            if (
                brand in title_lower or brand in text_lower
            ) and brand not in page_host.lower():
                anti_analysis_signals.append(
                    f"brand_impersonation:{brand}_on_{page_host}"
                )
                break

        if orphan_inputs:
            anti_analysis_signals.append("detached_password_input")

    # -- Build Evidence --
    from evidence_schema import (
        DetectedForm,
        Evidence,
        FormField,
        ScreenshotCapture,
    )

    forms_model = [
        DetectedForm(
            action=f.get("action", ""),
            method=f.get("method", "GET"),
            fields=[
                FormField(
                    name=fd.get("name", ""),
                    field_type=fd.get("type", "text"),
                    is_hidden=fd.get("hidden", False),
                )
                for fd in f.get("fields", [])
            ],
        )
        for f in forms_raw
    ]
    # Also synthesize a pseudo-form for orphan inputs so verdict.py
    # sees them
    if orphan_inputs:
        forms_model.append(
            DetectedForm(
                action="(no form — JS submission)",
                method="POST",
                fields=[
                    FormField(name=i["name"], field_type=i["type"])
                    for i in orphan_inputs
                ],
            )
        )

    evidence = Evidence(
        target_url=URL,
        final_url=final_url,
        http_status=http_status,
        page_title=page_title,
        screenshots=[
            ScreenshotCapture(
                session_id=session_id,
                image_base64=screenshot_b64,
                captured_at=run_completed_at,
            )
        ],
        visible_text=visible_text[:10000],
        forms=forms_model,
        anti_analysis_signals=anti_analysis_signals,
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        session_id=session_id,
    )

    print(
        f"Evidence: forms={len(forms_model)}, "
        f"password_field={has_password_field}, "
        f"signals={anti_analysis_signals}"
    )

except DestinationRefused as refusal:
    print(f"REFUSED [{refusal.reason_code}]: {refusal.reason}")
    raise SystemExit(0) from refusal
