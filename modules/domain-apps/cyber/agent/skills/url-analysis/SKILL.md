---
name: url-analysis
description: |
  Analyze a suspicious URL by visiting it in an isolated AgentCore Browser
  session. Captures screenshots, DOM, network requests, redirects, and
  extracted IOCs. Use for phishing triage, suspicious-link investigation,
  and malicious-site fingerprinting.
compatibility: requires agentcore-browser, requires url-allowlist-config
allowed-tools: Bash Read Write WebFetch
metadata:
  stage: url-triage
  typical_duration_seconds: 120
  session_timeout_seconds: 300
---

# url-analysis skill

## What this skill does

Analyze a suspicious URL using an isolated AgentCore Browser session, produce a
structured forensic report with verdict + confidence + IOCs + recommended actions.

The browser session runs in AWS-managed infrastructure, never in our VPC. Evidence
is captured and synthesized into a deterministic verdict via `verdict.py`.

## Your job as the executing agent

Given a URL to analyze:

### 1. Pre-flight validation

```python
from denylist import DenylistConfig, check_url, scrub_url_credentials

safe_url = scrub_url_credentials(url)
result = check_url(url)
if not result.allowed:
    # Return immediately with status "refused" and result.reason
    # Do NOT create a browser session
    pass
```

### 2. Write and execute an orchestration script

Write a Python script that:
- Opens an AgentCore Browser session (see `agentcore-browser-contract.md`)
- Navigates to the URL
- Captures a screenshot
- Extracts visible text (via screenshot + your interpretation, or via CDP)
- Detects any auto-downloads or forms
- Populates an `Evidence` object (see `evidence_schema.py`)
- Stops the browser session in a `finally` block

**Key rules for the orchestration script:**
- Language: Python 3.11+
- Use `boto3.client('bedrock-agentcore', region_name='us-east-1')`
- Follow `agentcore-browser-contract.md` for exact API shapes
- The API provides OS-level actions (mouseClick, keyType, screenshot) NOT
  high-level browser automation (no `navigate`, no `evaluate`, no `getHar`)
- To navigate: type the URL into the browser address bar or use Playwright via CDP
- Screenshots return base64-encoded PNG data
- Always call `stop_browser_session` in a finally block
- Save the script to `/tmp/run-artifacts/{run_id}/orchestration.py`

**Two approaches to browser interaction (choose based on task):**

1. **Playwright via CDP WebSocket** (preferred for URL analysis):
   - Start session, get `automationStream.streamEndpoint` from get_browser_session
   - Connect Playwright over CDP: `chromium.connect_over_cdp(ws_url, headers=headers)`
   - Use standard Playwright APIs: `page.goto()`, `page.screenshot()`, `page.content()`
   - Full DOM access, network interception, form detection

2. **InvokeBrowser OS actions** (for dialogs, CAPTCHAs, OS-level interaction):
   - Use `invoke_browser` with action types: mouseClick, keyType, screenshot, etc.
   - Lower-level, no DOM access — use when CDP can't handle the interaction

### 3. Enrichment (parallel with browser work if possible)

```python
from enrichment import run_enrichment

enrichment_result = run_enrichment(url, region="us-east-1")
```

This calls WHOIS, passive DNS, cert transparency, VT, URLhaus, MISP. Each source
degrades gracefully if unavailable.

### 4. Populate Evidence

```python
from evidence_schema import Evidence, ScreenshotCapture, RedirectHop, DetectedForm

evidence = Evidence(
    target_url=url,
    final_url=final_url_after_redirects,
    http_status=200,
    page_title=title,
    screenshots=[ScreenshotCapture(...)],
    visible_text=extracted_text,
    forms=[DetectedForm(...)],
    auto_downloads=[...],
    enrichment={
        "whois": enrichment_result.whois,
        "passive_dns": enrichment_result.passive_dns,
        "cert_transparency": enrichment_result.cert_transparency,
        "virustotal": enrichment_result.virustotal,
        "urlhaus": enrichment_result.urlhaus,
        "misp": enrichment_result.misp,
    },
    run_started_at=start_iso,
    run_completed_at=end_iso,
)
```

### 5. Verdict (deterministic - do NOT modify)

```python
from verdict import synthesize_verdict

browser_evidence_dict = evidence.to_browser_evidence_dict()
verdict = synthesize_verdict(
    url=url,
    domain=domain,
    browser_evidence=browser_evidence_dict,
    enrichment=evidence.enrichment,
)
```

### 6. Report

```python
from report import render_markdown_report, render_json_report

findings = {
    "url": safe_url,
    "final_url": evidence.final_url,
    "redirect_chain": [r.to_url for r in evidence.redirects],
    "http_status": evidence.http_status,
    "page_title": evidence.page_title,
    "screenshots": [],  # S3 URIs after upload
    "forms_detected": browser_evidence_dict["forms_detected"],
    "auto_downloads": browser_evidence_dict["auto_downloads"],
    "enrichment": evidence.enrichment,
    "iocs": extracted_iocs,
}

md_report = render_markdown_report(safe_url, findings, verdict.to_dict(), duration)
```

### 7. Cleanup

Always call `stop_browser_session` in a finally block. If the session is already
terminated, the API returns without error (ResourceNotFoundException is safe to ignore).

## Example orchestration scripts

See `examples/` for 3 reference scripts demonstrating successful analyses:
- `001-basic-clean.py` — Clean URL via Playwright CDP
- `002-broken-tls.py` — Handling TLS errors gracefully
- `003-malware-delivery.py` — Malware URL with auto-download detection

Use these as starting points, not as gospel. The API may drift; if the contract
seems wrong, try small experiments and document the real shape in a comment.

## Outputs

Stage envelope (JSON):

```json
{
  "artifact_id": "<ARTIFACT_ID>",
  "stage": "url-analysis",
  "stage_name": "url-analysis",
  "timestamp": "<ISO8601 UTC>",
  "status": "ok | partial | failed | refused",
  "duration_seconds": 42,
  "findings": { ... },
  "verdict": {
    "severity": "clean | suspicious | malicious",
    "confidence": 85,
    "category": "phishing | malware-delivery | c2 | scam | unclassified-risk | false-positive",
    "reasoning": "...",
    "mitre_attack": ["T1566.002"],
    "recommended_actions": ["block domain at proxy"]
  },
  "tool_calls": 8,
  "notes": ""
}
```

## Guardrails

- **Never visit internal URLs.** Denylist is enforced before any session creation.
- **Never submit forms.** Read-only observation of page content.
- **Never click downloads.** Detect auto-downloads but don't interact.
- **Session timeout enforced.** Default 300s, configurable per-tenant.
- **Credentials scrubbed.** Any URL containing auth tokens is masked before persistence.
- **Explicit session termination.** Always call StopBrowserSession in a finally block.

## Failure handling

- URL denylist match: refuse immediately, no session created
- Session creation fails: retry 3x with backoff, then fail with "browser unavailable"
- Navigation timeout: terminate session, produce partial report with evidence so far
- Enrichment source unavailable: degrade gracefully, note missing sources
- Session cleanup fails: log warning, AWS will auto-clean after timeout
