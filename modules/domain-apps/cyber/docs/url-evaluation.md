# URL evidence accuracy and evaluation

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
explicit coverage limitations. Failed navigation, challenges, missing screenshots
or DOM, failed cited items, and unconfirmed session cleanup remain ineligible.
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

## S3 benchmark contract

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
