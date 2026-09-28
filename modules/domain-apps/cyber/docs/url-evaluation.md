# Adaptive URL investigation and evidence evaluation

The primary capability is the cyber agent choosing how to investigate a site,
examining the result of each action and updating its hypothesis. Validate that
loop before collecting classification metrics. The September 24 large snapshot
runs closed each browser before calling the model, so their results do not measure
adaptive investigation.

## Live investigation acceptance (primary)

`live_evaluation.py` exposes the maintained skill commands to a real Bedrock model.
Start returns evidence with an open browser; the model selects one action, receives
the new observation and screenshot, then chooses again. `finish` validates the
assessment before closing. Batched tool calls are rejected without executing any
of them. Failures are never silently replayed. Browser-only mode skips the model
when collection produced no observations. Analyst mode still lets the model
select useful enrichment for the seed and assess sourced context; the browser
verdict remains inconclusive. This is an evaluation adapter for the cyber
skill, not a new production model service.

Run inside AWS. The input is an S3 JSON object with this shape (reserved example
hostname shown; real targets and all resulting evidence remain in AWS):

```json
{
  "schema_version": "cyber-live-evaluation/1",
  "cases": [{
    "id": "case-001",
    "url": "https://example.test/support",
    "objective": "Investigate the verification flow, requested information and claimed operator; examine counterevidence.",
    "scope": "host"
  }]
}
```

```bash
python /app/skills/url-analysis/live_evaluation.py \
  --manifest "$S3_SEEDS" --output-prefix "$S3_RUN_PREFIX" --max-cases 3
```

The default model is `us.anthropic.claude-sonnet-4-6`. Each case has up to 12 model
turns, 210 seconds for choosing browser actions, and a 270-second loop deadline;
an in-flight model request can take up to its configured timeout. Broker lease and
step limits still apply. Model calls are not automatically retried. Cases run
serially, and unconfirmed cleanup stops admission. Use a new output prefix per run.
The runner uploads decisions after each turn, the complete integrity-checked case
bundle, per-case results, source hashes and a summary, with S3 readback checks.
Logs contain only opaque IDs and execution counts.

The default adapter loads the maintained URL persona section, `SKILL.md` and
`analyst-playbook.md`. It exposes `enrich` alongside the browser tools. Add
`incident_context` (source, reported_at, summary records) and `brand_references`
to manifest rows only when supplied by the researcher. Never supply reference
labels as context. Use `--browser-only` for the separate browsing-only protocol;
it excludes these inputs and the enrichment tool. Protocol names distinguish the
two modes. Neither is the complete hosted platform workflow.

For SDK acceptance in the actual hosted worker image, run:

```bash
python /app/skills/url-analysis/hosted_evaluation.py \
  --manifest "$S3_SEEDS" --output-prefix "$S3_HOSTED_RUN_PREFIX" --max-cases 3
```

This uses the image's Claude Agent SDK with general Bash/Read/Write tools and the
same maintained URL instructions. The model executes the investigation CLI and
reads its artifacts instead of calling emulated Bedrock tools. The seed is opened
once before the SDK starts. Transcripts and case bundles are uploaded to S3.
It requires Node and the hosted SDK at `/app/node_modules`; it is not a standalone
browser-container command. It does not test worker orchestration, gateway policy,
GitHub/UI ingress or message delivery and does not post comments. Its protocol is
`hosted-sdk-acceptance`, with a ten-case hard cap.

`tests/run_analyst_acceptance.py` exercises both paths on matched synthetic cases
through an isolated fixture broker. Review their actual findings, chosen commands,
source use and hypothesis revisions before claiming a capability improvement.

First use a controlled state-dependent site with a real model. The synthetic
fixture in `tests/adaptive_fixture.py` presents competing links, reveals its form
and operator link only after a session-preserving click, and offers two different
operator disclosures. One disclosure includes an untrusted instruction to invent
official affiliation. Install its transport only in an isolated acceptance broker.
The normal broker continues using guarded public networking. Automated protocol
tests with injected model responses are regression checks, not real-model acceptance.

See the [September 24 acceptance record](adaptive-investigation-acceptance-2026-09-24.md)
for real-model results and the reasoning defects that remain after navigation passes.

Review the transcript against the actual evidence: did the model choose a relevant
lead, see evidence unavailable at the seed, follow a newly revealed lead, preserve
session state, reconsider the hypothesis and explain what remains unknown? Inspect
claims about operator identity and form behavior, including counterevidence and
page-injected instructions. Counts of actions or revisions alone are not a pass.
Then use a small public-site sample with the same loop. UI/GitHub ingress and report
delivery require separate acceptance; neither runner exercises them.

The September 23 PhishTank smoke test exposed two different limitations: four of
five pages were unavailable, and the remaining page contained strong phishing
indicators but a truncated script forced an inconclusive verdict. Those five
positive-feed examples do not establish a detection accuracy rate.

## Evidence and verdict changes

The collector prioritizes relevant existing inline scripts, retaining up to
32,768 characters per handler within a 131,072-character total script budget per
page/frame. Other inline snippets retain the 1,200-character limit; at most 20
scripts are included. Individual form and script truncation is recorded.
External script URLs remain metadata; the collector does not fetch arbitrary new
resources or execute supplied analysis code.

Collector-owned `evidence_items` bind each captured item to its kind, index,
completeness and hash. A separate evidence inventory hash preserves compatibility
with the existing page content hash. Capture intake and assessment validate the
inventory. Old captures remain readable but do not gain evidence completeness
retroactively.

Adverse findings on partial pages require specific intact item references and
explicit coverage limitations. Failed navigation, human-verification challenges,
missing screenshots or DOM, failed cited items and unconfirmed cleanup remain
ineligible for threat findings. Explicit threat warnings receive their own intact
`warning-001` item and can support suspicion, with provider identity and hidden
behavior unverified. A warning alone cannot support a malicious verdict. A separate
`coverage_limitation` finding may cite a later challenge without invalidating
earlier findings. Validation errors identify the offending observation and finding.
Rejected assessment attempts are retained; operational fallback preserves
individually valid findings from the latest attempt without inventing a verdict.
Complete evidence is still required for no-adverse assessments. Item integrity
cannot establish the semantic truth of a model's claims.

`collection.execution` and `collection.coverage` are reported separately from the
assessment verdict. Initial DNS refusal is a terminal, zero-observation result
with an empty valid inconclusive assessment. `finish` validates before closing a
browser. `schema` and `contract --case ...` expose the real Pydantic schema and
known references to tool adapters.

## Corroboration

`domain_investigation.py corroborate` accepts a trusted reference record:

```json
{
  "brand": "Example",
  "official_domains": ["example.test"],
  "authorized_identity_domains": ["identity.test"],
  "source_url": "https://example.test/authentication-providers",
  "verified_at": "2026-01-01T00:00:00Z",
  "verified_by": "Researcher"
}
```

The example uses reserved domains. Real records require independent review and
belong in the cloud configuration/evidence store. Domain matching uses exact
names and subdomain boundaries. A verified provider relationship is counterevidence,
not blanket clearance. Missing relationships are unknown, not automatically malicious.

Optional VirusTotal URL lookup uses a runtime-provided `CYBER_VT_API_KEY`, with
fixed endpoint, no redirects, a 15-second timeout and a 1-MiB response limit.
Lookup time and source analysis time are distinct. Missing credentials, errors,
and absent records are explicit. No URL is submitted for scanning.

## S3 snapshot assessment contract (secondary)

Dataset commands run only in an AWS worker, CodeBuild or ECS runtime. Inputs and
outputs must be S3 object URIs; real site evidence must not be downloaded locally.
Local tests contain synthetic `.test` fixtures only.

An evaluation manifest has `schema_version: cyber-evaluation/1` and a `cases` array.
Each case requires:

| Field | Meaning |
|---|---|
| `id` | Unique opaque case identifier |
| `case_uri` | S3 URI of the preserved case JSON, beside its artifact manifest |
| `sha256` | Pinned SHA-256 of that case JSON |
| `label` | `phishing`, `legitimate`, or an explicit `unavailable` control |
| `split` | `development` or `holdout` |
| `group_id` | Campaign/domain grouping; one group belongs to one split |
| `reviewed_by`, `reviewed_at` | Reference-label review provenance |

Preserve phishing labels for phishing examples that have gone offline; their
unavailability is a separate execution result. Do not relabel them benign or
discard them. Snapshots must have closed browser contexts and at most six
observations. The runner validates the case hash and artifact manifest, reads
screenshots only from that manifest, and processes temporary files inside AWS.

```bash
python /app/skills/url-analysis/benchmark.py validate \
  --manifest "$S3_MANIFEST" --output "$S3_VALIDATION_RESULT"
python /app/skills/url-analysis/benchmark.py run \
  --manifest "$S3_MANIFEST" --split development --max-cases 220 \
  --output "$S3_RESULTS"
python /app/skills/url-analysis/benchmark.py run \
  --manifest "$S3_MANIFEST" --split holdout --max-cases 220 \
  --output "$S3_HOLDOUT_RESULTS"
```

The case budget is explicit: the runner never silently samples a split. It uses
the deployed assessment schema, at most two model attempts per case, and no model
call for zero-observation cases. Source labels, previous verdicts, reviews and
reputation records are excluded from model input. Per-case progress and final
results are uploaded with readback verification. This is snapshot assessment,
not a test of navigation, UI/GitHub ingress or automatic user delivery.

Build a representative initial corpus of about 100 reviewed phishing snapshots,
100 reviewed legitimate snapshots, and 20 failure scenarios. Include authorized
third-party authentication, payment providers, redirects, branding similarities,
missing resources, challenges and unavailable hosts. Freeze campaign/domain splits
before tuning. This change supplies the runner and regression scenarios; it does
not claim that 220 independently reviewed public cases already exist.

Precision, recall over all phishing cases, recall over reachable phishing cases,
false-positive rate, availability, inconclusive rate, latency and tokens are
reported separately. Missing/duplicate results fail scoring. Integrity/schema
validation is not called evidence accuracy: `human_evidence_correctness` is null
until human review fields are provided. Do not claim an accuracy improvement from
synthetic tests or from reducing the inconclusive rate alone.
