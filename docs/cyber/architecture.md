# Cyber Domain App — Architecture

> **Scope.** This document describes the cyber domain app (`modules/domain-apps/cyber/`) as-built on 2026-05-06. It supersedes the v1 architecture diagram in [EPIC #224](https://github.com/aws-e/adp/issues/224). Sources of truth remain the code, the skill playbooks, and the EPIC thread — this document summarizes and connects them.

---

## 1. What the cyber domain app is

A research assistant for threat researchers, built as a thin domain-specific layer on top of ADP. One persona (`malware-analysis-agent`) drives two analytical paths:

1. **File analysis** — seven-stage pipeline that detonates a sample, produces a structured case file, and recovers MITRE ATT&CK techniques from evidence.
2. **URL analysis** — single-turn skill that visits a suspicious URL in an isolated browser, captures evidence, and produces a structured forensic report.

Both paths use the same trigger surface (GitHub label / @mention / webhook / email-gateway forward / SIEM / chat), the same persona, the same reasoning-tier → byte-handling-tier split, and drop their output into the same surfaces (issue comments, chat-artifacts bucket, DDB case rows).

**What's cyber-specific:** the persona, seven stage-skills plus the URL-analysis skill, the worker Dockerfile, and the CAPE / image-builder / AgentCore-Browser Terraform. Everything else — trigger ingress, queues, scaling, Bedrock access, vault credentials, artifact storage, GitHub App plumbing — comes from ADP foundations.

---

## 2. v2 Architecture — two substrates, one pipeline

The original v1 diagram (see EPIC #224 §Architecture) was drawn when the cyber domain handled files only. The URL path added a second byte-handling substrate, so v2 shows both side-by-side.

**The guarantee that didn't change:** the reasoning tier never holds sample bytes. That property now holds via two different mechanisms — VPC isolation for files, AWS-managed ephemeral browser sessions for URLs.

```
┌──────────────────── TRIGGER + MONITOR CHANNELS ─────────────────────┐
│                                                                     │
│   GitHub label / @mention │ EDR webhook │ Chat UI │ SIEM webhook    │
│   S3 drop (samples)       │ Email-gateway URL forward │ Teams/Slack │
│                                                                     │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ webhook-ingress Lambda (#319)
                                   │ → SQS FIFO → KEDA ScaledJob
                                   ▼
┌────────────── ADP VPC — cyber-analyst agent (main EKS) ─────────────┐
│                                                                     │
│   Reasoning tier — never holds sample bytes. Drives both analytical │
│   paths under one persona (malware-analysis-agent):                 │
│                                                                     │
│   ╭─── File analysis (7 stages) ───────────────────────────╮        │
│   │  Stage 2 Research → Stage 5 Correlation                │        │
│   │                   → Stage 6 Verdict → Stage 7 Report   │        │
│   │  Drives Stages 1/3/4 workers in Threat Research VPC    │        │
│   ╰────────────────────────────────────────────────────────╯        │
│                                                                     │
│   ╭─── URL analysis (single-turn skill) ───────────────────╮        │
│   │  Pre-flight denylist → AgentCore Browser session        │        │
│   │  → Playwright-over-CDP (BrowserClient generates SigV4)  │        │
│   │  → Enrichment (WHOIS, PDNS, crt.sh, VT, URLhaus, MISP)  │        │
│   │  → verdict.py (deterministic) → markdown report         │        │
│   │  Evidence envelope + screenshots to cyber S3 bucket     │        │
│   ╰────────────────────────────────────────────────────────╯        │
│                                                                     │
│   Shared: Bedrock gateway · vault-scoped credentials (#132)         │
│           chat-artifacts bucket · agent-factory runtime             │
│                                                                     │
└───┬───────────────────────┬────────────────────┬────────────────────┘
    │ SQS FIFO + S3         │ SigV4 CDP WebSocket│ S3 PutObject +
    │ (per-sample tasks)    │ + InvokeBrowser    │ presigned GET
    ▼                       ▼                    ▼
┌───────────────────┐ ┌──────────────────────┐ ┌─────────────────────┐
│ THREAT RESEARCH   │ │ AWS Bedrock          │ │ url-analysis        │
│ VPC — file bytes  │ │ AgentCore Browser    │ │ evidence bucket     │
│                   │ │ (AWS-managed)        │ │ (cyber-owned S3)    │
│ Stage 1 Triage    │ │                      │ │                     │
│ Stage 3 Static    │ │ Isolated Chromium    │ │ screenshots +       │
│ Stage 4 Dynamic   │ │ per session,         │ │ Evidence envelopes  │
│  + CAPE fleet     │ │ CDP WebSocket,       │ │ AES-256, BPA on,    │
│                   │ │ no persistent state, │ │ 30-day lifecycle,   │
│ libvirt 192.168   │ │ ≤ 5-min session TTL, │ │ tenant-prefixed,    │
│ .100.0/24         │ │ no customer-VPC      │ │ resource-policy     │
│ + INetSim         │ │ attach               │ │ access scoping      │
│ no internet       │ │                      │ │ (#499 / PR #501)    │
└───────────────────┘ └──────────────────────┘ └─────────────────────┘
```

### Invariants across both paths

| Invariant | File path mechanism | URL path mechanism |
|---|---|---|
| Reasoning tier never holds bytes | File bytes never leave Threat Research VPC; reasoning agent sees hashes + evidence structs only | Screenshot bytes live in AgentCore session + (optionally) cyber S3; reasoning agent sees metadata + downsized screenshot |
| Ephemeral, state-free sandbox | One-shot KEDA ScaledJob pod or CAPE analysis VM (snapshotted between samples) | AgentCore Browser session, ≤ 5-minute TTL, destroyed after use |
| Output surface is shared | Case file → GitHub comment + chat-artifacts S3 bucket + DDB row | Markdown report → GitHub comment; Evidence envelope + screenshots → url-analysis evidence bucket |
| Rules / samples / evidence versioned by content | Immutable S3 prefixes for YARA layers, samples, reports | Session-ID-keyed objects, `tenant=/issue=/run=/url-N/` path layout |
| All persistent state outside the worker | DDB (task state), SQS (queues), S3 (reports, rules, samples) | DDB (issue state), SQS (comment dispatch), S3 (evidence) |

### Trigger ingress (shared)

The webhook-ingress Lambda (#319) authenticates and normalizes every channel into a single SQS FIFO queue. KEDA's ScaledJob then spawns one hosted-agent worker pod per message.

```
GitHub label/@mention ──┐
EDR webhook ────────────┤
Email gateway forward ──┤   webhook-ingress Lambda       agent-scaledjob
SIEM webhook ───────────┼── (auth, normalize, authZ) ──► SQS FIFO ──► KEDA ──► pod
Chat UI @bot ───────────┤                                                       │
S3 drop (samples) ──────┘                                                       ▼
                                                                    Reasoning runtime
                                                                    loads cyber persona
                                                                    + skills bundle
```

Per-tenant scoping lives in the SQS `MessageGroupId` (`tenant#repo#issue`), so cross-tenant head-of-line blocking is avoided while still serializing concurrent requests for the same issue.

---

## 3. Persona — `malware-analysis-agent`

Located at `modules/domain-apps/cyber/agent/personas/malware-analysis-agent.md`.

**Role.** The same persona handles both URL triage and file analysis. It knows when to use which skill:

- A URL in the issue body or a label targeting URL work → loads `url-analysis` skill, runs the single-turn playbook (pre-flight → browser → enrichment → verdict → report).
- A sample S3 URI + label targeting file analysis → loads the seven stage skills, orchestrates the chain.

**Reasoning heuristics baked into the persona:**

- **Default stance**: "treat every submission as suspicious until evidence moves it; don't over-flag established legitimate sources either."
- **Confidence calibration bands**: <30 = needs more signal; 30–60 = suspicious-but-ambiguous; 60–80 = solid malicious or solid clean; 80+ = unambiguous.
- **Analyst-handoff voice**: executive line first, then evidence, caveats, IOCs, MITRE, recommended actions. Never narrate internal deliberation.
- **MITRE ATT&CK cheat sheet**: common TTPs keyed to evidence patterns the agent might see (DLL delivery → T1105, Unix shell exec → T1059.004, credential harvest form → T1566.002, etc.).
- **URL-triage reasoning heuristics** (added in PR #491): strong-positive signals (vendor interstitial, AV hit, leet-speak path, throwaway TLD), strong-negative signals (tranco-top-10k, gov/edu TLDs, established CDN hosts), and "ambiguous" signals that need cross-referencing.

**Hard invariants the persona enforces:**

- Never click through interstitials. Don't solve captchas. Don't submit credentials.
- Never persist malware payloads. Hash-only for downloads.
- Closed-loop integrity: if a ground-truth TTP file accompanies the sample, do not read it before Stage 5 is complete.

---

## 4. Skills

Located at `modules/domain-apps/cyber/agent/skills/`.

| Directory | Purpose | Mode |
|---|---|---|
| `stage-1-triage/` | File fingerprinting | Sandboxed worker (file path) |
| `stage-2-osint/` | OSINT research, hypothesis steering | Reasoning agent (file path) |
| `stage-3-static/` | Static analysis (dual-mode) | Sandboxed worker (file path) |
| `stage-4-dynamic/` | CAPE submission + behavior trace | CAPE host (file path) |
| `stage-5-correlation/` | Combine evidence → MITRE TTPs | Reasoning agent (file path) |
| `stage-6-verdict/` | Severity + confidence + IOCs | Reasoning agent (file path) |
| `stage-7-report/` | Case file bundle | Reasoning agent (file path) |
| `url-analysis/` | Full URL triage in one skill | Reasoning agent (URL path) |

Each directory contains a `SKILL.md` (playbook the agent reads at runtime) and, where applicable, Python modules the agent imports or executes.

---

## 5. Deep dive — URL analysis

### 5.1. Shape

One skill, one agent turn, one or more AgentCore Browser sessions. No multi-stage pipeline. The agent loads `url-analysis/SKILL.md`, writes an orchestration script per URL, executes it, populates an Evidence object, runs `verdict.py`, renders a report, posts a comment.

```
        GitHub issue with URL(s)
                 │
                 ▼
   malware-analysis-agent pod
                 │
                 ├── Pre-flight denylist check
                 │   (refuse internal/private URLs)
                 │
                 ├── For each URL:
                 │     │
                 │     ├── AgentCore Browser session
                 │     │   (StartBrowserSession →
                 │     │    generate_ws_headers →
                 │     │    Playwright connect_over_cdp)
                 │     │
                 │     ├── Navigate + capture:
                 │     │   page.goto → page.screenshot
                 │     │   page.title → page.inner_text
                 │     │   page.evaluate(forms, redirects)
                 │     │   page.on(response, download)
                 │     │
                 │     ├── Resize screenshot for Claude
                 │     │   (shrink_for_claude, max 1024px)
                 │     │
                 │     ├── Enrichment (in parallel where possible):
                 │     │   WHOIS/RDAP, PassiveDNS, crt.sh,
                 │     │   VirusTotal, URLhaus, MISP
                 │     │
                 │     ├── Populate Evidence (pydantic)
                 │     │   target_url, final_url, http_status,
                 │     │   screenshots, redirects, forms,
                 │     │   auto_downloads, anti_analysis_signals,
                 │     │   enrichment
                 │     │
                 │     ├── Upload evidence to S3
                 │     │   (screenshots + envelope.json)
                 │     │   — gracefully falls back to inline
                 │     │   base64 if bucket/policy missing
                 │     │
                 │     ├── verdict.synthesize_verdict()
                 │     │   deterministic scoring →
                 │     │   severity, confidence, category,
                 │     │   MITRE TTPs, recommended actions
                 │     │
                 │     └── report.render_markdown_report()
                 │         executive line → evidence → caveats
                 │         → IOCs → MITRE → recommended actions
                 │
                 └── Post comment(s) to issue + run summary
```

### 5.2. Substrate: AWS Bedrock AgentCore Browser

- AWS-managed ephemeral browser runtime. We do not operate or peer with it.
- Access is two-channel:
  - **CDP WebSocket** (`automationStream.streamEndpoint`) — SigV4-signed; reached via `bedrock_agentcore.tools.browser_client.BrowserClient.generate_ws_headers()`. Driven by Playwright (`chromium.connect_over_cdp(ws_url, headers=headers)`).
  - **InvokeBrowser REST** — OS-level actions (mouseClick, keyType, screenshot). Fallback when CDP isn't viable for a specific site.
- Sessions are short-lived (skill target: 30-60 s per URL). Always stopped in a `finally` block.
- IAM on the scaledjob role grants: `StartBrowserSession`, `GetBrowserSession`, `StopBrowserSession`, `ListBrowserSessions`, `InvokeBrowser`, `ConnectBrowserAutomationStream`, `UpdateBrowserStream`, scoped by `aws:RequestedRegion`.

### 5.3. Evidence schema (`evidence_schema.py`)

Bridges the agent-authored orchestration script to the deterministic `verdict.py`. The agent populates an `Evidence` pydantic model; `verdict.py` consumes `evidence.to_browser_evidence_dict()` without modification — keeping the scoring engine byte-identical across skill refactors.

Key fields:

| Field | Purpose |
|---|---|
| `target_url` / `final_url` / `http_status` / `page_title` | Core navigation outcome |
| `redirects: list[RedirectHop]` | Each hop: from_url, to_url, status_code, method (http/meta-refresh/js) |
| `screenshots: list[ScreenshotCapture]` | `image_base64` (inline fallback) or `image_s3_uri` (when bucket configured) |
| `visible_text`, `dom_snapshot` | Text extraction for reasoning |
| `forms: list[DetectedForm]` | action URL, method, fields (name, type, hidden) |
| `auto_downloads: list[AutoDownload]` | URL, MIME, size, SHA-256 — hash only, never the payload |
| `anti_analysis_signals: list[str]` | e.g. `external_block:cloudflare`, `form_action_host_mismatch:...`, `tld_drift:...` |
| `tls_info`, `network_requests`, `har_data` | Optional rich traces |
| `enrichment: dict` | Populated by `run_enrichment()` from WHOIS / PDNS / crt.sh / VT / URLhaus / MISP |

### 5.4. Evidence persistence (`evidence_store.py`)

S3-backed, resilient-by-default:

- `upload_screenshot(png_bytes, *, run_id, url_index, shot_index) -> str` — validates PNG magic, enforces 5 MB cap, returns `s3://...` URI or `""` on any failure
- `upload_evidence_envelope(evidence, ...) -> str` — strips `image_base64` (keeps envelope compact), enforces 2 MB cap, same fallback
- `presign(s3_uri, expires_in=86400) -> str` — 24 h presigned GET; empty URI returns empty string
- `shrink_for_claude(png_bytes, max_side=1024) -> bytes` — Pillow LANCZOS downscale so full-page screenshots fit Bedrock's image-input cap

**Key behavior**: when the bucket is missing, `URL_ANALYSIS_EVIDENCE_BUCKET` is unset, the bucket policy doesn't allow PutObject, or Pillow isn't installed — all of these degrade to the inline-base64 path without crashing the run (PR #502). An evidence-upload failure never prevents a report from posting.

### 5.5. Deterministic verdict (`verdict.py`)

Input: the dict returned by `Evidence.to_browser_evidence_dict()` plus enrichment.

Scoring factors (non-exhaustive):

- Forms with `type=password` + credential-harvest patterns
- Auto-downloads with executable MIME types
- Redirect-chain length and TLD drift
- Visible-text keyword matches (brand impersonation, ransom-note phrasing)
- Enrichment hits (VT positive count, URLhaus match, MISP correlation)
- `anti_analysis_signals` entries
- Suspicious TLD list, public-suffix drift

Output: `Verdict(severity, confidence, category, reasoning, mitre_attack[], recommended_actions[])`. Deterministic — same evidence → same verdict, always.

The persona's analyst reasoning can **override** the machine verdict upward when cross-source signals agree but the deterministic score is conservative (e.g. "CF interstitial + Mirai-naming URL path + throwaway `.surf` domain → malicious 85" even if machine-scorer returns suspicious 30). This is documented in the persona's confidence calibration section.

### 5.6. Examples directory

`url-analysis/examples/` — reference orchestration scripts the agent pattern-matches against. Each is a runnable Python script demonstrating one scenario shape.

| # | File | Scenario | Evidence surface |
|---|---|---|---|
| 001 | `001-basic-clean.py` | Clean URL baseline | Navigation + screenshot + forms + text |
| 002 | `002-broken-tls.py` | TLS errors (expired, mismatch) | Graceful degradation, `status=partial` |
| 003 | `003-malware-delivery.py` | Direct file delivery | `page.on("download")`, SHA-256 without persisting |
| 004 | `004-phishing-form.py` | Credential harvest / brand impersonation | `page.evaluate()` form enumeration, orphan inputs, brand-host mismatch |
| 005 | `005-redirect-chain.py` | Link shortener / cloaking | HTTP 3xx + JS + meta-refresh hop tracking, TLD drift heuristics |
| 006 | `006-cloudflare-interstitial.py` | Vendor block pages | Interstitial signature detection, Ray ID extraction, do-not-bypass |

### 5.7. Typical run

Real #500 run numbers (morning 4-URL triage):

| Metric | Value |
|---|---|
| Wall time | ~5 min |
| Sessions | 4 AgentCore Browser sessions (one per URL), all TERMINATED |
| CDP vs InvokeBrowser | 4/4 CDP (no fallback) |
| Enrichment sources reached | WHOIS (partial), PassiveDNS (all), crt.sh (degraded), VT / URLhaus / MISP (skipped — creds pending) |
| Reports posted | 4 forensic reports + 1 run summary |
| Verdict accuracy | 4/4 correct (2 malicious phish, 1 malicious malware, 1 clean gov) |

---

## 6. Deep dive — File analysis (7 stages)

### 6.1. Shape

Seven stages across two VPCs, orchestrated by the reasoning agent. The split is rigid: Stages 1, 3, 4 run in sandboxed workers; Stages 2, 5, 6, 7 run as the reasoning agent.

```
  Sample submission (S3 URI + issue)
              │
              ▼
   malware-analysis-agent pod
              │
              ▼
   ┌─── Stage 1 — Triage (sandboxed worker, Threat Research VPC) ───┐
   │  Input:  S3 URI (the sample bytes)                             │
   │  Tools:  libmagic, magika, iocextract, lief, PE/ELF parsers    │
   │  Output: hashes, file type, section entropy, strings,          │
   │          candidate IOCs                                        │
   └────────────────────────────────┬──────────────────────────────┘
                                    │ envelope (no bytes)
                                    ▼
   ┌─── Stage 2 — Research (reasoning agent) ───────────────────────┐
   │  Input:  Stage 1 fingerprint                                   │
   │  Tools:  MalwareBazaar, VirusTotal, Shodan, urlscan, MISP,     │
   │          org zoo, case tracker, vendor reporting, GitHub code  │
   │  Output: prior reporting, MITRE hypothesis, YARA rule hints,   │
   │          short-circuit flag (e.g. "known benign, skip to 6")   │
   └────────────────────────────────┬──────────────────────────────┘
                                    │ hypothesis + rule hints
                                    ▼
   ┌─── Stage 3 — Static (sandboxed worker, dual-mode) ─────────────┐
   │  Mode A — rule-driven: lief parse, IAT, YARA narrowed by       │
   │    Stage 2 hints, suspicious API combos, oletools for docs     │
   │  Mode B — agent-authored script: reasoning agent writes Python │
   │    tailored to the hypothesis, uploads to S3, worker runs it   │
   │    in a 300 s subprocess (non-root, no network)                │
   │  Output: YARA hits (rule, layer, version), anti-analysis       │
   │          signals, family-specific extraction                   │
   └────────────────────────────────┬──────────────────────────────┘
                                    │
                                    ▼
   ┌─── Stage 4 — Dynamic (CAPE host, Threat Research VPC) ─────────┐
   │  Substrate: CAPE on EC2, KVM + libvirt 192.168.100.0/24,       │
   │             INetSim faking internet, Linux qcow2 analysis VM   │
   │             (Windows VM behind #262)                           │
   │  Output: process tree, file/registry/network events, injection │
   │          chains, dropped payloads, API trace, screenshots      │
   │  Fleet:   independent CAPE hosts federated by dispatcher +     │
   │           DDB host registry + SQS submit queues +              │
   │           S3 report export, scaling 0-50 on EC2 Spot           │
   └────────────────────────────────┬──────────────────────────────┘
                                    │
                                    ▼
   ┌─── Stage 5 — Correlation (reasoning agent) ───────────────────┐
   │  Combines all prior evidence → MITRE ATT&CK mapping,           │
   │  family identification, campaign overlap, hypothesis outcome   │
   │  (confirmed / revised / rejected)                              │
   └────────────────────────────────┬──────────────────────────────┘
                                    │
                                    ▼
   ┌─── Stage 6 — Verdict (reasoning agent) ───────────────────────┐
   │  severity (malicious / suspicious / benign), confidence,       │
   │  reasoning, STIX 2.1 + CSV IOCs, recommended actions           │
   └────────────────────────────────┬──────────────────────────────┘
                                    │
                                    ▼
   ┌─── Stage 7 — Report (reasoning agent) ────────────────────────┐
   │  Case file bundle in chat-artifacts:                           │
   │    executive_summary.md · technical_report.md                  │
   │    iocs.stix.json · iocs.csv · timeline.json                   │
   │    hunt_queries.{splunk,elastic,sentinel}.txt                  │
   │  DDB row with verdict + full rule-provenance manifest          │
   └───────────────────────────────────────────────────────────────┘
```

### 6.2. Substrate: Threat Research VPC

- **EKS cluster** (`adp-dev-cyber-eks`) with KEDA ScaledJob workers for triage + static. One pod per SQS message; idle cost = zero.
- **CAPE host** on EC2 with nested virtualization (KVM/libvirt). Each host runs its own CAPE stack + pool of analysis VMs, snapshotted and reverted between samples.
- **Sandbox network** — `192.168.100.0/24`, libvirt, default-deny iptables, INetSim providing fake DNS/HTTP/SMTP/etc. Analysis VMs have no route to AWS metadata, no route to any other VPC, no internet. This is the pipeline's hardest security property.
- **VPC peering** — port 443 only to the main ADP VPC, for CAPE API calls from the reasoning tier. No other ports open either direction.

### 6.3. Rule layering

Three tiers of YARA rules, all content-versioned in S3:

| Layer | Source | Rollup cadence |
|---|---|---|
| `public/` | Florian Roth signature-base, YARA-Forge, Elastic detection-rules | Hourly ingestion with canary-benign validation (rules firing > 5% of canary corpus auto-quarantined) |
| `org/<org-id>/` | MSSP-curated | Customer-managed |
| `tenant/<tenant-id>/` | End-customer private | Customer-managed |

Stage 3 tags every YARA hit with `{rule, layer, version}` so Stage 7's report answers "why did this fire?" in a click.

### 6.4. Case file

Landing location: existing chat-artifacts S3 bucket (no cyber-specific UI needed; existing ADP artifact renderer displays it).

Bundle contents:

```
case-files/<tenant>/<artifact_id>/
  executive_summary.md     # 90-second read for a researcher
  technical_report.md       # full per-stage evidence
  iocs.stix.json            # STIX 2.1 bundle
  iocs.csv                  # flat CSV
  timeline.json             # event timeline
  hunt_queries/
    splunk.txt
    elastic.txt
    sentinel.txt
  stages/
    stage-1-envelope.json   # each stage's raw envelope
    stage-2-envelope.json
    ...
  manifest.json             # rule versions, worker image tag, timings
```

### 6.5. Closed-loop integrity

When a sample ships with ground-truth metadata (e.g. Atomic Red Team atomics carry their expected TTP in `expected-ttps.json`), the persona must NOT read the ground-truth file before Stage 5 is complete. The independent recovery at Stage 5 is what makes the demo a demo and the test a test.

Demonstrated on #304 (T1059.004 Unix shell exec) — Stage 5 independently named `T1059.004` from the Stage 4 process tree; post-Stage-7 verification confirmed the match.

### 6.6. Typical run

Reference #304 (T1059.004 closed-loop):

| Metric | Value |
|---|---|
| Wall time | 21 m 26 s |
| Stages completed | 7/7 |
| Closed-loop | Pass (Stage 5 named `T1059.004` without reading the ground-truth file) |
| CAPE task status | `reported` |
| Evidence surface exercised | Stage 1 hashes, Stage 3 YARA (0 hits on a benign emulator), Stage 4 process tree + INetSim capture, Stage 5 MITRE mapping, Stage 6 `suspicious` verdict (ground truth: benign emulator — correct as "behavior pattern that real malware uses") |

---

## 7. Shared ADP primitives

| ADP primitive | Cyber domain reuse |
|---|---|
| `modules/gateway/` (Bedrock proxy) | Reasoning stages (2, 5, 6, 7) + URL-analysis skill all reach Claude via the gateway. Rate limiting, audit, credential isolation inherited. |
| `modules/agent-factory/` persona + skills runtime | `malware-analysis-agent` + all 8 skills loaded by the standard runtime. |
| SQS FIFO + KEDA ScaledJob | Triage, static, and URL-analysis workers scale identically — one pod per message, zero idle. |
| GitHub label / @mention trigger | `.github/workflows/malware-analysis-agent.yml` mirrors `developer` / `pm` / `operations` workflows. |
| `modules/user-services/vault/` (#132) | Per-org external API keys (VT, Shodan, HA, MISP, URLhaus) delivered at run-time. |
| SaaS identity (#181) | Per-tenant identity propagates from trigger through queue through pod into the case file / evidence envelope. |
| Platform infra (VPC, EKS, IAM, Secrets Manager, CloudTrail) | Threat Research VPC peers to ADP VPC on port 443. AgentCore Browser access via IAM only. Standard IRSA everywhere. |
| Chat-artifacts bucket | Case files land in the existing artifact layout. Existing UI renders them. |

Cyber-specific code footprint stays at roughly 1:10 vs. platform code. The URL path added ~1.2k lines (skill + examples + tests + 2 Terraform files) with zero platform changes.

---

## 8. IAM surface summary

### Reasoning-tier role (`adp-dev-agent-scaledjob-role`)

- SQS receive/delete on `adp-dev-agent-submit.fifo`
- Bedrock `InvokeModel*` on foundation models + inference profiles
- Secrets Manager `GetSecretValue` on `adp/*`
- STS `AssumeRole` with ExternalId `adp-dev-hosted-agent` for tenant ops
- **AgentCore Browser:** `StartBrowserSession`, `GetBrowserSession`, `StopBrowserSession`, `ListBrowserSessions`, `InvokeBrowser`, `ConnectBrowserAutomationStream`, `UpdateBrowserStream`

### Byte-handling-tier role (`adp-dev-cyber-worker`)

- SQS receive/delete on cyber stage queues
- S3 Get/Put on cyber prefixes (samples, rules, reports)
- Additional AgentCore Browser grants (for cyber ARC flow when it runs URL analysis directly)

### Evidence bucket resource policy

`adp-dev-url-analysis-evidence-<account>` allows `PutObject` + `GetObject` to the scaledjob role and cyber worker role; `Deny *` for anyone else. Defense-in-depth on top of the role-inline grants.

---

## 9. Safety properties

| Property | Mechanism |
|---|---|
| File bytes never leave Threat Research VPC | VPC peering port 443 only; sandbox network default-deny; no NAT egress from sandbox subnet |
| URL bytes never leave AgentCore Browser session | Session-scoped; ≤ 5-min TTL; not attached to any customer VPC; Chromium destroyed at session stop |
| No malware payload persistence | Stage 4 evidence includes hashes + metadata only; Stage 7 case file has no executable bytes; URL-analysis `page.on("download")` captures SHA-256 and discards |
| No credential storage outside vault | VT / Shodan / URLhaus / MISP keys only in Secrets Manager, delivered at run-time, never in code / logs / images |
| No interstitial bypass | Persona hard rule: capture vendor block pages as evidence, mark `status=partial`, do NOT solve captchas or click through |
| Audit trail | Every run produces an immutable record: stage envelopes, rule versions, source queries, verdict, reasoning, timestamps — exportable to SIEM |
| Graceful degradation | Missing enrichment source → empty brief; missing evidence bucket → inline base64; missing Pillow → reason-from-text; missing ground-truth → skip closed-loop check |

---

## 10. Multi-tenancy posture

Designed for MSSP-scale from day one. Current state: single-tenant in dev; multi-tenant controls designed and partially implemented.

| Dimension | Enforcement |
|---|---|
| Rule isolation | Three-layer YARA model (public / org / tenant); IAM-scoped S3 prefixes, not application logic |
| Credential isolation | Per-tenant vault scope `adp/<env>/tenants/<tid>/...` |
| Sample / report isolation | S3 prefixes and DDB rows tenant-scoped; `tenant#repo#issue` SQS MessageGroupId prevents head-of-line blocking across tenants |
| Evidence isolation (URL path) | `tenant=<tid>/issue=<n>/run=<run>/url-<i>/...` key layout in evidence bucket |
| Throughput | Target 1,000 samples/hour sustained, 3,000/hour burst; scale-to-near-zero idle via EC2 Spot for CAPE + KEDA for workers |

---

## 11. Current state (as of 2026-05-06)

**Operational:**
- Threat Research VPC, EKS, CAPE host with nested virt, VPC peering, SQS FIFO, KEDA workers, DDB — deployed and smoke-tested
- Persona + 7 file stages + URL analysis skill, label/mention-triggered workflows — live
- Linux CAPE analysis VM — end-to-end smoke reaches `reported` (see #278, #297, #304)
- URL analysis via Playwright-over-CDP — live (see #500, #503)
- Hourly public YARA rule ingestion with canary validation — live
- Evidence bucket (cyber-owned) — bucket exists; resource policy pending ECR-drift cleanup

**Designed, in backlog:**
- Windows CAPE analysis VM (gated on #262 build pipeline)
- Wazuh EDR telemetry integration (#260)
- Multi-tenant org + tenant rule authoring API
- CAPE fleet scaling to 1,000 samples/hour (#261)
- URL-analysis `verdict.py` scoring of interstitials + URL-path heuristics (follow-up from #503)

---

## 12. Not this

- **Not EDR.** We analyze what EDR flags; we don't run on endpoints.
- **Not a SOC.** No 24/7 monitoring or analyst on-call.
- **Not a threat-intel platform.** We query existing TIPs; we don't replace them.
- **Not MITRE ATT&CK Evaluations candidate.** Those test live-attack detection against production enterprises — different category.
- **Not for classified / export-controlled samples.** Dev environment only until a proper clean-room is provisioned.

---

## References

- **EPIC:** https://github.com/aws-e/adp/issues/224
- **Project board:** https://github.com/orgs/aws-e/projects/3
- **Persona:** `modules/domain-apps/cyber/agent/personas/malware-analysis-agent.md`
- **Skills:** `modules/domain-apps/cyber/agent/skills/` (8 subdirectories)
- **Infra:** `modules/domain-apps/cyber/infra/` — CAPE, EKS, VPC, KEDA, URL-analysis evidence bucket
- **AgentCore Browser contract (extracted for the agent):** `modules/domain-apps/cyber/agent/skills/url-analysis/agentcore-browser-contract.md`
- **AWS Bedrock AgentCore developer guide (full, checked in):** `docs/bedrock-agentore-dg.md`
- **Recent runs grounding v2:**
  - #278 — 7-stage wiring smoke (gate for demo)
  - #297 — first end-to-end file run (benign `/bin/ls`)
  - #304 — Atomic T1059.004 closed-loop (21 m 26 s, Stage 5 independent TTP recovery)
  - #500 — URL triage, 4 URLs, all CDP, 4/4 correct verdicts
  - #503 — URL triage, 3 URLs, surfaced Claude-image-size issue (fixed PR #504)
  - #505 — URL triage afternoon batch
  - #506 — T1059.004 fresh run (in progress)
- **Related PRs:**
  - PR #487, #493, #498 — AgentCore Browser IAM + examples
  - PR #501 — evidence persistence scaffold
  - PR #502 — resilience fix + cyber-infra workflows
  - PR #504 — CDP-first guidance + shrink_for_claude
