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

### 1. Submit the URL to the trusted browser broker

`browser_client.analyze_url` is the only browser entry point available to the
reasoning pod. The pod's IAM role explicitly denies every AgentCore Browser API,
including `InvokeBrowser`, session lifecycle, and CDP stream access. A separate
broker pod owns the narrow session/CDP role and never exposes a raw browser,
page, stream URL, or AWS credential.

The broker vets the initial URL before starting AgentCore, makes the browser
offline, blocks service workers and WebSockets, and installs request policy
before creating the page. Every HTTP request is fetched over a socket pinned to
the freshly vetted address while preserving Host and TLS SNI.

```python
from browser_client import analyze_url
from browser_guard import DestinationRefused
from denylist import scrub_url_credentials

safe_url = scrub_url_credentials(url)

try:
    analysis = analyze_url(url)
    screenshot_base64 = analysis["screenshot_base64"]
    text = analysis["visible_text"]
except DestinationRefused as refusal:
    # Emit the stage envelope with status "refused" and refusal.reason.
    # No evidence exists for this target.
    # refusal.reason_code distinguishes the cases:
    #   "blocked_address"   — policy refusal (internal/reserved destination)
    #   "resolution_failed" — the analysis environment could not resolve the
    #                         host, so the destination could not be vetted.
    #                         A spike in this code means a resolver problem,
    #                         not an attack.
    #   "host_pattern", "scheme_not_allowed", "malformed_url", "no_hostname"
    pass
```

Do not import `BrowserClient`, create a `bedrock-agentcore` client, call
`InvokeBrowser`, or connect over CDP. Those calls are denied by IAM, not merely
forbidden by this playbook. If the broker is unavailable, report an environment
failure; there is no unguarded fallback.

### 2. Write and execute an orchestration script

Write a Python script that:
- Calls `analyze_url` once for each URL
- Uses the returned screenshot, visible text, redirects, forms, and downloads
- Populates an `Evidence` object (see `evidence_schema.py`)

**Key rules for the orchestration script:**
- Language: Python 3.11+
- Use `browser_client.analyze_url`; never instantiate `BrowserClient` directly
- Treat `BrowserBrokerError` as an environment failure, not a policy approval
- Save the script to `/tmp/run-artifacts/{run_id}/orchestration.py`

**There is one browser interaction path: the guarded broker operation.**

   ```python
   from browser_client import analyze_url

   analysis = analyze_url(url, wait_until="networkidle", timeout_ms=30000)
   screenshot_base64 = analysis["screenshot_base64"]
   text = analysis["visible_text"]
   forms = analysis["forms"]
   redirects = analysis["redirects"]
   ```

`InvokeBrowser` cannot enforce redirects, subresources, or the actual socket
destination and is therefore denied. WebSockets are refused because they cannot
currently be proxied while retaining host identity and address pinning.

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

### 8. Screenshot handling (MANDATORY — do not skip)

Browser screenshots at the default viewport (1456×819, full_page=True) can be
several MB. Bedrock rejects over-size images with
`API Error: 400 Could not process image` and the whole run dies. Resize before
showing to Claude OR keep the screenshot on disk and reason from text evidence.

**Before opening a screenshot for visual reasoning, always resize it:**

```python
from url_analysis.evidence_store import shrink_for_claude

resized_bytes = shrink_for_claude(screenshot_bytes, max_side=1024)
with open("/tmp/url1_screenshot.png", "wb") as f:
    f.write(resized_bytes)
```

`shrink_for_claude` downscales the longest side to `max_side` pixels and
re-encodes as PNG. It's a no-op if the image is already small. Full-resolution
bytes stay in the Evidence envelope (uploaded to S3 when the bucket is
configured); the on-disk copy is only for Claude's visual input.

**If Pillow/PIL is unavailable in the runtime**, skip the screenshot read
entirely — `page.title()` + `page.inner_text("body")` + detected forms give
Claude enough to reason from without the image. A missing image must NEVER
crash the run.

## Example orchestration scripts

See `examples/` for reference scripts covering the common scenarios:

| # | File | Scenario | Evidence surface exercised |
|---|------|----------|----------------------------|
| 001 | `001-basic-clean.py` | Clean URL baseline | navigation, screenshot, forms, text |
| 002 | `002-broken-tls.py` | TLS errors (expired, mismatch) | graceful degradation, partial evidence |
| 003 | `003-malware-delivery.py` | Direct-file delivery (`.sh`, `.dll`) | `page.on("download", ...)`, SHA-256 without persisting payload |
| 004 | `004-phishing-form.py` | Credential harvest / brand-impersonation forms | `page.evaluate()` form enumeration, detached-input detection, brand-host mismatch signals |
| 005 | `005-redirect-chain.py` | Link shorteners, cloaking, exploit-kit hops | `page.on("response")` + `page.on("framenavigated")` → `RedirectHop[]`, TLD-drift + registered-domain-fanout signals |
| 006 | `006-cloudflare-interstitial.py` | Vendor block pages (Cloudflare / Google SB / SmartScreen) | interstitial signature detection, Ray ID extraction, `status=partial`, **do not bypass** |

Pick the closest match to the URL's signal profile. You can combine
patterns — a phishing URL that also uses redirects wants forms from
004 + hop tracking from 005 + the `status=partial` pattern from 006
if it gets intercepted.

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

- **Never visit internal URLs.** Enforced structurally by
  the trusted broker: reasoning workers have an explicit AgentCore API deny;
  the broker refuses before session creation, disables raw browser networking
  and service workers, intercepts every HTTP request, and fetches only through
  a socket pinned to a vetted address. Resolution fails closed and canonical
  address forms receive the same verdict.
- **Never submit forms.** Read-only observation of page content.
- **Never click downloads.** Detect auto-downloads but don't interact.
- **Resource budgets enforced.** Each response is limited to 25 MiB and 30s;
  each browser analysis is limited to 100 MiB and 300s across all requests.
- **Credentials scrubbed.** Any URL containing auth tokens is masked before persistence.
- **Explicit session termination.** The broker closes every session before responding.

## Failure handling

- `DestinationRefused` from `analyze_url`: status "refused" and no evidence is
  returned. Report `reason_code` — `resolution_failed` means the
  analysis environment could not resolve the host (an environment problem the
  analyst should be told about), everything else is a policy refusal.
- `analysis["refusals"]` non-empty: a subresource or WebSocket was blocked.
  A blocked navigation or redirect returns `DestinationRefused` before capture.
- `response_too_large` or `fetch_deadline_exceeded`: the pinned transport
  stopped an untrusted response at its byte or wall-clock budget.
- `BrowserBrokerError`: fail with "browser unavailable"; never browse directly.
- Navigation timeout: the broker terminates the session and returns no capture.
- Enrichment source unavailable: degrade gracefully, note missing sources
- Session cleanup fails: log warning, AWS will auto-clean after timeout
