# Researcher URL cases

For reasoning-led exploration across multiple pages, use the
[domain investigation workflow](domain-investigations.md). The commands below
describe the compatible single-URL case collector and shared evidence artifacts.

The cyber agent can collect a reproducible browser case, choose bounded follow-up
views, and assess findings against captured evidence. The maintained collector
connects directly to AgentCore Browser in native mode; the existing cyber agent
supplies the reasoning. Explicit broker mode supports legacy deployments.

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
network request/response metadata, forms without
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
worker needs Playwright and the AgentCore SDK plus regional Browser permissions.
The browser runs in AgentCore; a local driver preserves session state.

From the installed runtime:

```bash
URL_SKILL=/app/skills/url-analysis
TARGET_URL='https://example.com/'
CASE_DIR=/tmp/run-artifacts/example-run/url-case
python "$URL_SKILL/research_case.py" capture "$TARGET_URL" \
  --output "$CASE_DIR" --wait-seconds 3
```

In a repository checkout, set `URL_SKILL` to
`modules/domain-apps/cyber/agent/skills/url-analysis` instead. Native mode is the
client default. Terraform explicitly selects the deployment mode; use
`browser_mode = "native"` after the protected worker image and IAM are ready.

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
[agent skill](../agent/skills/url-analysis/SKILL.md).
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

New cases carry an unassessed collection placeholder until the model supplies its
conclusion. The model decides the overall verdict using browser observations and
sourced context, including Common Crawl. Application checks cover report format,
reference existence and evidence integrity. Capture completeness, finding type and
cleanup status do not veto or replace the model verdict. Truncated evidence may
be cited for its captured portion, with the truncation retained in the record.
The model must explain uncertainty and distinguish observed facts from inference.
Indicator exports remain unassessed until the model establishes their relevance.

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
python -m pytest --noconftest modules/domain-apps/cyber/tests/test_url_analysis_browser_boundary.py -q
```

Linux hosts may also need `python -m playwright install-deps chromium`. The
browser fixtures fail if Chromium is missing. These checks measure known
regressions, not phishing-detection accuracy against a representative threat corpus.

The native transport removes the broker dependency. Prepare the protected worker's
regional Browser lifecycle/CDP permission, validate the image and native capture,
then switch worker mode. Drain existing leases before setting
`browser_broker_enabled = false`. See the [browser contract](../agent/skills/url-analysis/agentcore-browser-contract.md).
The September 23 record below describes the earlier guarded deployment.

The collector was deployed and live-tested in Embark1 (`879318057152`,
`us-east-1`) on 2026-09-23. The worker/broker path captured `example.com` and
controlled delayed desktop/mobile views, verified evidence and S3 readback,
refused worker direct-browser access and a private destination, and independently
confirmed session termination. See the
[deployment record](url-researcher-deployment-2026-09-23.md) for the immutable
image, merged PRs, evidence archive and validation limits. Sessions still start
on demand; absence of idle sessions does not indicate a broken browser path.

For future rollout, follow the canonical
[agent deployment guide](../../../../docs/adp-platform-deployment/deploy-with-agent.md), confirm
the account, build/pin the runtime image, and coordinate the worker/broker update.
Then validate a harmless URL through `/v1/capture`, verify the downloaded case,
confirm on-demand session termination independently, and test a controlled delayed
fixture before accepting researcher traffic.

Later milestones: authenticated threat-intelligence integration (including
Intelix), analyst review and comparison in the app, automatic case delivery,
autonomous investigation evaluation, representative labeled-corpus measurement,
and richer permitted network/file evidence. None is implied by a passing fixture.
