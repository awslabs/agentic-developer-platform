# Researcher URL cases

The cyber agent can collect a reproducible browser case, choose bounded follow-up
views, and assess findings against captured evidence. The maintained collector
runs in the trusted browser broker; the existing cyber agent supplies the
reasoning. This is the first researcher-workflow milestone, not a new autonomous
model service.

## What a researcher receives

| Artifact | Purpose |
| --- | --- |
| `case.json` | Authoritative observations, probe reasons/status and assessment |
| `manifest.json` | SHA-256 and byte count of each persisted file; unsigned integrity check |
| `obs-NNN.png` | Original viewport screenshot |
| `obs-NNN-dom.txt` | Bounded DOM with destination URLs redacted; inert text |
| `report.md`, `report.html` | Findings, linked evidence, coverage and recommended actions |
| `indicators.csv` | Observed URLs/domains/connected IPs, role, observation ID and unassessed disposition |

Observations include UTC timestamps, redirects, main-document HTTP status,
network request/response metadata, actual pinned connection IPs, forms without
input values, orphan credential fields, up to five child frames, and bounded
script samples. Each probe records its session ID, region, collector/Playwright/
browser versions, exact-input SHA-256, profile configuration and cleanup outcome.
No CDP endpoints, credentials or raw network bodies are returned.

The manifest detects changed or missing files. It is not signed, externally
anchored or proof of malicious intent. Original files stay intact when using
resized copies for model visual input. HTML escapes hostile page content and
prohibits scripts and external fetches. Keep all files together to view the report.

## Commands

Requirements: Python 3.11+, pydantic 2 and the existing skill dependencies. The
broker additionally needs Playwright 1.48+ and its managed AgentCore connection.
The client does not launch a local browser or hold AgentCore credentials.

From the installed runtime:

```bash
URL_SKILL=/app/skills/url-analysis
TARGET_URL='https://example.com/'
CASE_DIR=/tmp/run-artifacts/example-run/url-case
python "$URL_SKILL/research_case.py" capture "$TARGET_URL" \
  --output "$CASE_DIR" --wait-seconds 3
```

In a repository checkout, set `URL_SKILL` to
`modules/domain-apps/cyber/agent/skills/url-analysis` instead. The default broker is
`http://url-analysis-browser-broker.adp-agents.svc.cluster.local:8765`; override
`URL_ANALYSIS_BROWSER_BROKER` only with the trusted broker's address.

Capture normally exits **2** because evidence awaits assessment. Read `case.json`
even if the probe failed: its intent and safe diagnostic are retained. A failed
follow-up keeps earlier evidence and invalidates an earlier no-adverse conclusion.
Do not use `&&` to skip review just because capture returned 2.

```bash
python "$URL_SKILL/research_case.py" probe "$TARGET_URL" --case "$CASE_DIR" \
  --profile mobile --wait-seconds 5 --reason 'Compare the mobile rendering'
python "$URL_SKILL/research_case.py" assess --case "$CASE_DIR" \
  --assessment /tmp/research-assessment.json
python "$URL_SKILL/research_case.py" verify --case "$CASE_DIR"
```

Assessment fields and a worked JSON shape are in the
[agent skill](../../modules/domain-apps/cyber/agent/skills/url-analysis/SKILL.md).
Use actual observation IDs. The command rejects invented IDs, unsupported content
variation, numeric confidence fields, hypotheses-only conclusions and false
clearance on incomplete probes. It validates evidence references and completeness;
it cannot prove that an assessor's prose is semantically supported by an image.
A researcher must still review the reasoning.

Exit statuses: **0** conclusive assessment or successful verification; **2**
inconclusive/unassessed case, including a recorded broker failure; **1** invalid
input, integrity error or local failure. `capture` requires a new directory.
`probe` accepts only the exact original URL. Concurrent changes use a case lock.

Local persistence survives CLI failures, but an ephemeral worker directory is not
long-term storage. Deliver the full directory through the existing run artifact
publisher before pod deletion. Automatic S3 upload is not part of this CLI. The
HTML report uses relative image links, so download the complete bundle for review.

## Coverage and boundaries

- Maximum four probes per case. Each probe uses a fresh session and captures one
  initial view, optionally another view after 0–15 seconds in the same session.
- Desktop: 1440×900. Mobile: 390×844, touch/mobile emulation with a fixed Android
  user agent. The actual browser version and effective user agent are recorded;
  mobile emulation is not a physical mobile device.
- The browser remains offline. CDP Fetch interception fulfills each intercepted
  HTTP request, including redirects, through the broker's DNS-vetted, IP-pinned
  transport. HTTP traffic originates at the broker's egress, not AgentCore egress.
  OOPIFs/workers not attached to interception remain offline; failures are partial.
- Only GET/HEAD/OPTIONS are sent. Forms are not submitted. The collector cannot
  guarantee remote GET endpoints have no side effects. Service workers,
  WebSockets and popups are blocked, which can change the rendered behavior.
- TLS verification is mandatory. Initial policy refusals occur before session
  creation. Navigation refusals stop collection and retain a typed probe failure.
  The broker attempts cleanup in `finally`; failed cleanup marks evidence partial.
- Per response: 25 MiB and 30 seconds. Transport/session backstop: 100 MiB and 300
  seconds. Screenshot: 5 MiB. Broker reply: 16 MiB. Per snapshot: 20,000 text
  characters, 100,000 DOM characters, 30 forms, 50 fields/form, 5 child frames,
  50 links, 20 script samples (1,200 inline characters each), 200 events per kind.
  Truncation, failed/blocked requests, challenges and capture errors are partial.
- Network logs contain method, URL, resource type, status, MIME and timestamps;
  they are not HAR files. Form destinations are distinguished from observed
  requests. Downloads are cancelled and recorded as offers, without execution or
  payload hashes. TLS certificate-chain/WHOIS/passive-DNS collection is not added
  by this collector.
- URL query values, fragments and userinfo are removed from structured evidence;
  DOM destination attributes are resolved and redacted. Input credentials and
  recognizable token-query names are refused. This is not full PII redaction:
  URL paths, relative URL literals in scripts, page text and screenshots may
  contain sensitive data. Query redaction also limits exact reproduction by a
  recipient who does not retain the original submitted URL.

All cases start inconclusive. Non-inconclusive findings must cite complete
observations. `no_adverse_behavior_observed` requires every observation and probe
to be complete; it describes only tested views. Content variation requires
same-input observations with different content hashes and does not by itself
prove evasion. Indicator exports remain unassessed until corroborated.

## Verification and rollout

The regression suite runs real local Chromium against synthetic fixtures through
the same collector and guard. Only the managed session adapter and pinned HTTP
transport are replaced; fixture traffic does not visit live sites or AWS. It
covers delayed password forms, desktop/mobile variation, HTTP redirects, internal
redirect refusal, frames, method blocking, failed resources, challenges, report
escaping, redaction, failed follow-ups, evidence integrity and broker contracts.

```bash
python -m pip install -r modules/domain-apps/cyber/agent/skills/url-analysis/tests/requirements.txt
python -m playwright install chromium
python -m pytest modules/domain-apps/cyber/agent/skills/url-analysis/tests -q
python -m pytest --noconftest modules/agent-factory/webhook-ingress/tests/test_url_analysis_browser_boundary.py -q
```

Linux hosts may also need `python -m playwright install-deps chromium`. The
browser fixtures fail if Chromium is missing. These checks measure known
regressions, not phishing-detection accuracy against a representative threat corpus.

The new code targets main's guarded broker architecture (PR #5721). The broker
and reasoning worker both use the agent runtime image. Releasing requires a
coordinated image update and the existing broker identity/service/network-policy
infrastructure. Do not update workers to the broker-only skill against a cluster
that lacks the broker. There is no direct-browser fallback.

At the 2026-09-23 review, Embark1 (`879318057152`, `us-east-1`) had an older runtime
that started AgentCore sessions directly on demand. A harmless `example.com`
smoke test succeeded and its session was independently confirmed terminated.
That confirms the old deployed path only. **This collector has not been deployed
or live-tested in Embark1.** Absence of idle browser sessions is not evidence that
the on-demand browser path is broken.

For rollout, follow the canonical
[agent deployment guide](../adp-platform-deployment/deploy-with-agent.md), confirm
the account, build/pin the runtime image, and coordinate the worker/broker update.
Then validate a harmless URL through `/v1/capture`, verify the downloaded case,
confirm on-demand session termination independently, and test a controlled delayed
fixture before accepting researcher traffic. Production deployment remains a
separate step from this implementation.

Later milestones: authenticated threat-intelligence integration (including
Intelix), analyst review and comparison in the app, automatic case delivery,
autonomous investigation evaluation, representative labeled-corpus measurement,
and richer permitted network/file evidence. None is implied by a passing fixture.
