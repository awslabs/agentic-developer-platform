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

# URL Analysis Skill

Analyze a suspicious URL using AWS Bedrock AgentCore Browser — an isolated, ephemeral browser runtime. The URL is visited in AWS-managed infrastructure, never in our VPC. Evidence is captured and synthesized into a structured forensic report.

## Inputs

| Var | Source | Shape |
|---|---|---|
| `TARGET_URL` | issue body or event payload | string, well-formed URL |
| `ARTIFACT_ID` | issue body or generated | string, stable across stages |
| `CYBER_RESULTS_TABLE` | env | DDB table name |
| `AWS_REGION` | env | AWS region (default us-east-1) |

## Outputs

Stage envelope:

```json
{
  "artifact_id": "<ARTIFACT_ID>",
  "stage": "url-analysis",
  "stage_name": "url-analysis",
  "timestamp": "<ISO8601 UTC>",
  "status": "ok | partial | failed",
  "duration_seconds": <int>,
  "findings": {
    "url": "<analyzed URL>",
    "final_url": "<after redirects>",
    "redirect_chain": ["url1", "url2", "..."],
    "http_status": 200,
    "page_title": "...",
    "screenshots": ["s3://...pre-scroll.png", "s3://...post-scroll.png"],
    "dom_snapshot_uri": "s3://...",
    "har_file_uri": "s3://...",
    "network_requests": [{"url": "...", "status": 200, "mime": "...", "size": 0}],
    "forms_detected": [{"action": "...", "fields": ["email", "password"]}],
    "auto_downloads": [{"url": "...", "mime": "...", "sha256": "..."}],
    "anti_analysis_signals": [],
    "enrichment": {
      "whois": {"registrar": "...", "creation_date": "...", "age_days": 0},
      "passive_dns": [],
      "cert_transparency": {},
      "virustotal": {},
      "urlhaus": {},
      "misp": {}
    },
    "iocs": {
      "domains": [],
      "ips": [],
      "urls": [],
      "file_hashes": [],
      "email_addresses": []
    }
  },
  "verdict": {
    "severity": "clean | suspicious | malicious",
    "confidence": 85,
    "category": "phishing | malware-delivery | c2 | scam | unclassified-risk | false-positive",
    "reasoning": "...",
    "mitre_attack": ["T1566.002"],
    "recommended_actions": ["block domain at proxy", "alert affected users"]
  },
  "tool_calls": <int>,
  "notes": "free text"
}
```

## Steps

### 1. Pre-flight validation

- Parse and validate URL format
- Resolve domain to IP(s)
- Check against denylist (internal subnets, AWS metadata, configured blocked hosts)
- If denylisted: return immediately with `status: "refused"` and reason

### 2. Create AgentCore Browser session

```python
from url_analysis.browser_client import AgentCoreBrowserClient

client = AgentCoreBrowserClient(region=region, session_timeout=300)
session = client.create_session()
```

### 3. Navigate and capture evidence

- Visit URL via AgentCore Browser
- Capture full-page screenshot (pre- and post-scroll)
- Extract DOM + JavaScript sources
- Record network requests (URLs, status codes, MIME types, sizes)
- Record redirect chain (server + client-side)
- Detect forms + hidden fields (phishing indicators)
- Detect auto-downloads (file URL, MIME, SHA256)
- Detect anti-analysis signals (headless detection, cloaking)

### 4. Enrichment (parallel)

- Domain WHOIS + passive DNS (age, registrar)
- Certificate transparency log lookup
- VirusTotal URL/domain lookup (if credentials available)
- URLhaus check
- MISP lookup (if tenant connection configured)

### 5. Synthesis and verdict

- Reason over captured evidence + enrichment data
- Produce structured verdict: severity, confidence, category
- Map to MITRE ATT&CK techniques where applicable
- Generate recommended actions

### 6. Artifact publishing

- Upload screenshots to S3
- Upload DOM dump, HAR file to S3
- Write structured JSON report
- Write envelope to DDB

### 7. Session cleanup

- Explicitly terminate AgentCore Browser session
- Verify termination via GetBrowserSession

## Failure handling

- URL denylist match: refuse immediately, no session created, clear error message
- Session creation fails: retry 3x with backoff, then fail with "browser unavailable"
- Navigation timeout: terminate session, produce partial report with "inconclusive" verdict
- Enrichment source unavailable: degrade gracefully, note missing sources
- Session cleanup fails: log warning, AWS will auto-clean (cost bounded by timeout)

## Guardrails

- **Never visit internal URLs.** Denylist is enforced before any session creation.
- **Never submit forms.** Read-only observation of page content.
- **Never click downloads.** Detect auto-downloads but don't interact.
- **Session timeout enforced.** Default 300s, configurable per-tenant.
- **Credentials scrubbed.** Any URL containing auth tokens in query params is masked before persistence.
- **Explicit session termination.** Always call StopBrowserSession in a finally block.
