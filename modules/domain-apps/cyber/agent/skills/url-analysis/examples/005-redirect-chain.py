"""
Example orchestration script: Redirect chain tracking (link shorteners,
cloaking, exploit-kit hops).

Uses the broker's response and frame-navigation capture to record hops from the
target URL to final landing page — HTTP 3xx,
meta-refresh, and JS-driven `location.href=` redirects. Detects TLD
and hostname drift across the chain which is a strong cloaking signal.

Expected verdict: suspicious when the chain crosses 2+ different
registered domains, or lands on a different TLD than it started.
"""

import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

from browser_client import analyze_url
from browser_guard import DestinationRefused

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://bit.ly/example"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_of(u: str) -> str:
    try:
        return urlparse(u).hostname or ""
    except ValueError:
        return ""


def reg_domain(host: str) -> str:
    """Coarse registered-domain extract: last 2 labels (no PSL dependency)."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# -- Main --
run_started_at = iso_now()
hops: list[dict] = []  # {from_url, to_url, status_code, method}

try:
    result = analyze_url(URL)
    session_id = result["session_id"]
    final_url = result["final_url"]
    http_status = result["http_status"]
    page_title = result["page_title"]
    screenshot_b64 = result["screenshot_base64"]
    visible_text = result["visible_text"]
    hops.extend(
        {
            "from_url": redirect["from_url"],
            "to_url": redirect["location"] or "(no Location header)",
            "status_code": redirect["status"],
            "method": "http",
        }
        for redirect in result["redirects"]
    )
    last_url = URL
    for navigation in result["frame_navigations"]:
        if navigation != last_url and not any(
            hop["to_url"] == navigation and hop["method"] == "http" for hop in hops[-3:]
        ):
            hops.append(
                {
                    "from_url": last_url,
                    "to_url": navigation,
                    "status_code": 0,
                    "method": "js",
                }
            )
        last_url = navigation

    run_completed_at = iso_now()

    # -- Cloaking heuristics --
    anti_analysis_signals = []

    chain_hosts = [host_of(URL)] + [host_of(h["to_url"]) for h in hops]
    chain_hosts = [h for h in chain_hosts if h]
    distinct_regs = {reg_domain(h) for h in chain_hosts}

    if len(distinct_regs) >= 3:
        anti_analysis_signals.append(
            f"redirect_fanout:{len(distinct_regs)}_registered_domains"
        )

    start_tld = urlparse(URL).hostname or ""
    end_tld = urlparse(final_url).hostname or ""
    if start_tld and end_tld and start_tld.split(".")[-1] != end_tld.split(".")[-1]:
        anti_analysis_signals.append(
            f"tld_drift:{start_tld.split('.')[-1]}->{end_tld.split('.')[-1]}"
        )

    # JS-only redirects (not HTTP 3xx) are a classic cloaking tell
    if any(h["method"] == "js" for h in hops):
        anti_analysis_signals.append("js_redirect_in_chain")

    # -- Build Evidence --
    from evidence_schema import Evidence, RedirectHop, ScreenshotCapture

    evidence = Evidence(
        target_url=URL,
        final_url=final_url,
        http_status=http_status,
        page_title=page_title,
        redirects=[
            RedirectHop(
                from_url=h["from_url"],
                to_url=h["to_url"],
                status_code=h["status_code"],
                method=h["method"],
            )
            for h in hops
        ],
        screenshots=[
            ScreenshotCapture(
                session_id=session_id,
                image_base64=screenshot_b64,
                captured_at=run_completed_at,
            )
        ],
        visible_text=visible_text[:10000],
        anti_analysis_signals=anti_analysis_signals,
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        session_id=session_id,
    )

    print(f"Evidence: {len(hops)} hops, final={final_url}")
    print(f"  Chain: {' -> '.join(chain_hosts)}")
    print(f"  Signals: {anti_analysis_signals}")

except DestinationRefused as refusal:
    print(f"REFUSED [{refusal.reason_code}]: {refusal.reason}")
    raise SystemExit(0) from refusal
