# #4969 — OpenAI Bedrock pricing: correctness, daily refresh, first-deploy initialization

**Status:** revision 3 — responds to all eight changes requested on `d9b15798` in
[PR #4972](https://github.com/aws-e/adp/pull/4972). Still design only: no code,
schema, Terraform or production pricing changed by this note.
**Issue:** [#4969](https://github.com/aws-e/adp/issues/4969)
**Parent:** #768 (bug-fixes EPIC)
**Coordinates with:** #1017 (cold-start seed story, under #1013) — see [§8](#8-relationship-to-1017). #1017 stays open until its own acceptance criteria are delivered.
**Preserves:** #4968 (Codex settlement, merged as `662906e`), #1486/#4592 (Claude cache accounting)
**Repository audited at:** `662906e` (`main`), re-checked 2026-09-12
**Rates re-verified:** 2026-09-12 against AWS model cards, the AWS prompt-caching
guide and the 20260911124408 us-east-1 bulk catalog — see [§10](#10-verified-rate-inventory)

---

## 1. Scope and outcome

This corrective release fixes the OpenAI Bedrock rates used by the gateway and
Budget & Spend, seeds a fresh installation, and publishes validated AWS rate
changes daily at 06:00 UTC. It includes all 12 models and the published geography,
service-tier and long-context variants in §10. Historical ledger rewriting is
out of scope. #1017 remains open until the delivered seed passes its acceptance
criteria.

Revision 2 supplied the accepted AWS evidence but had incompatible storage and
rollout rules. This revision replaces those rules throughout the design. All
source paths below are relative to `modules/gateway/` unless they start with
`.github/`, `codebuild/`, `platform/` or `docs/`.

## 2. Review disposition

All eight decisions are recorded here; none requires another owner decision.
This is a proposal for implementation review, not a claim of delivered behavior.

| Review point | Implementable resolution |
|---|---|
| 1 — immutable generations | New `model_pricing_rates_v2` includes generation in its primary key; generation FK, immutable validated rows and a singleton active pointer (§4.2). |
| 2 — compatibility and corrective release | Keep the legacy table and unique model key unchanged. Replace the old writer first, deploy consumers that tolerate missing tables/columns, then seed and activate in the same release (§4.6). |
| 3 — one cost per request | The gateway emits one exact decimal pricing decision in the existing S3 settlement event. The tracker verifies/reuses it despite cache skew or retries; stale good rates remain usable (§4.1, §4.3). |
| 4 — cache-write meaning | Distinguish full write price, no additional fee, and unpublished policy; invalid usage gets a bounded decomposition and estimated confidence (§4.3). |
| 5 — complete publication | Assemble fresh validated rows plus retained prior rows without renewing retained verification timestamps. Require full variant-key coverage (§4.5). |
| 6 — asynchronous failures | Configure EventBridge delivery retry/DLQ and Lambda function-error retry/age/on-failure destination separately, including IAM and alarms (§4.5). |
| 7 — effective tier | Capture upstream served tier, final streaming metadata, forwarded profile and endpoint region; unconfirmed tier is estimated (§4.4). |
| 8 — shared artifact | One runtime policy/snapshot package ships in the gateway and both Lambdas; the migration seed is separately frozen and checked against its pinned version (§4.1, §4.6). |

## 3. Recorded decisions

| ID | Decision |
|---|---|
| D1 | The active database generation is the normal source of OpenAI rates. Daily refresh changes actual spend calculation within the healthy cache refresh bound. |
| D2 | Add versioned storage alongside the unchanged legacy table. No dimensioned rows or new writer conflict targets are introduced into `model_pricing`. |
| D3 | Select long context from raw total input, including cache reads/writes. Cache-write policy is explicit per model/variant. |
| D4 | Price the effective served variant when known; preserve requested and observed metadata. Unsupported, stale, incomplete or inferred evidence produces `estimated`, with reasons. |
| D5 | Do not rewrite historical ledger rows or replay historical settlement objects. Document the affected window. |
| D6 | One dependency-free runtime package owns pricing policy and versioned snapshots. The migration contains an independently frozen literal; it never imports runtime code. |
| D7 | V2 rate columns use `NUMERIC(14,10)`; rates and calculations remain decimal. Existing six-decimal ledger storage receives one consistently rounded value. |
| D8 | Ship schema, seed, compatible consumers, refresh and infrastructure as one corrective release, with the executable order in §4.6. Compatibility remains available for rollback. |

## 4. Design

### 4.1 One runtime policy and published rate source

Create `pricing_policy/` at the gateway module root. It contains `__init__.py`,
`policy.py` (decimal math, normalization, variant selection and decision
validation), and `snapshots/2026-09-12.1.json` (rates, required variant keys,
cache policies, provenance and source checksums). It uses only the Python
standard library; DB access remains in the gateway's async adapter and the
Lambdas' psycopg2 adapter. Consumers import `pricing_policy`, never each other's
application modules. Keep the snapshot version immutable; later bundles add a
new version and change the current-version selector. Retain version
`2026-09-12.1` for deterministic legacy-event compatibility (§4.3).

The existing image/import boundary is a packaging constraint to change. It is
not a reason to create two hand-edited runtime literals. The immutable migration
seed is the only intentional separate copy and has a reproducible parity test.

| Build or trigger | Required edit in this release |
|---|---|
| `modules/gateway/Dockerfile` | Add `COPY pricing_policy/ pricing_policy/` to the runtime stage, under `/app`; preserve package directories and snapshot JSON. |
| `pyproject.toml` | Extend setuptools package discovery from `src*` to include `pricing_policy*`, and package data to include `snapshots/*.json`; verify wheel and editable installs load the same snapshot. The Docker runtime COPY supplies the package even though its dependency-build stage currently copies only pyproject metadata. |
| `infra/modules/budget-lambda/main.tf` | Add the same package to **both** `archive_file` inputs using `fileset("${path.root}/../pricing_policy", "**")` and dynamic `source` entries with zip filename `pricing_policy/${relative_path}`. Never flatten package files. Both archive hashes include the package. |
| `.github/workflows/gateway-deploy.yml` ZIP step | Recursively add the same package files under `pricing_policy/` to each zip, alongside existing handlers/shared adapters. Keep the Terraform and CI file manifests identical. |
| `.github/workflows/gateway-deploy.yml` triggers/detection | Add `modules/gateway/pricing_policy/**` to push paths; match `^modules/gateway/pricing_policy/` in both backend and budget-Lambda change detection. Keep migration changes forcing a backend rebuild. |
| `.github/workflows/gateway-ci.yml` | Add `modules/gateway/pricing_policy/**`, `modules/gateway/alembic/versions/**` and `modules/gateway/infra/modules/budget-lambda/**` to both push/PR path filters; include deployment-workflow/buildspec changes that alter packaging. Extend Ruff inputs to `pricing_policy/` and run package/parity tests under the existing gateway suite, which already includes `tests/lambda/`. The separate `lambda-tests.yml` belongs to agent-factory and needs no unrelated change. |
| `codebuild/bs-gateway-smoke.yml`, `codebuild/bs-gateway-build.yml` | Preserve their module-root Docker context and add a built-image `python -c` package-import/snapshot-load check before smoke success or deployable-image push. Verify source archives and exclusions include `pricing_policy/`. |

The snapshot contains the complete OpenAI inventory and the existing curated
non-OpenAI policies, including Claude's four-rate entries and ID normalization.
Migrate those policies mechanically, preserving their current values. Remove the
independent pricing literals from `src/budget/pricing.py`,
`lambda/shared/pricing_fallback.py` and `src/budget/config.py`; preserve their
public adapters where callers require them. `src/budget/utils.py`, the public
cost endpoint, estimator and enforcement estimate use the shared policy.
Client-supplied estimator inputs never become settlement evidence.

**Runtime sources.** Prefer the active V2 database generation for OpenAI.
The refresh uses the bulk catalog for its four GPT-OSS models, and the AWS
Markdown model cards for eight frontier models absent from that catalog.
Source authority is assigned by supported model/variant, not by a global rule
that could turn a model-card outage into an unrelated catalog fallback. Bundled
rates bootstrap consumers when V2 is unavailable; a failed refresh never writes
them over published rows. Non-OpenAI reads use the existing legacy DB data merged
per model with curated cache policies (§7).

**Cache contract.** Load V2 on startup when available, then refresh asynchronously
with a 900-second TTL; swap only a fully validated, single-generation cache.
Track the pointer's revision as well as generation ID: normal publications move
forward, while an explicit rollback may select an older generation. The cache
must accept that newer pointer revision. DB reads select the pointer and rows in
one query or a repeatable-read transaction. Retry connection failures with
bounded backoff (30–900 seconds). A successful publish reaches healthy active
consumers within 15 minutes; rollout verification explicitly warms/probes them.
No synchronous DB call is added to the upstream inference request itself.

Keep the last-known-good in-memory generation indefinitely on refresh failure.
Never replace it with an older bundled snapshot merely because it aged. Emit
`PricingCacheRefreshFailure` and cache age. Mark decisions estimated if the cache
has been unable to refresh for 30 minutes, or any selected source row was last
verified more than 48 hours ago. Preserve original row verification times. A
cold process with no good DB/cache result uses the bundled snapshot, marked
estimated with `bootstrap_fallback`; a recovered DB replaces it. A generation's
publication timestamp never makes an individually stale row fresh.

Independent caches may choose different generations for **different** requests.
For a single request, the gateway's durable decision is the sole pricing input
to settlement (§4.3); the tracker must not substitute its current cache.

### 4.2 Storage, immutability and compatibility

Leave the legacy schema, its values and its primary key untouched by the V2
migration:

```text
model_pricing(
  model_id VARCHAR(255) PRIMARY KEY,
  input_price_per_1k_tokens NUMERIC(10,6) NOT NULL,
  output_price_per_1k_tokens NUMERIC(10,6) NOT NULL,
  source VARCHAR(20) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL)
```

An old writer's `ON CONFLICT(model_id)` and an old reader's model-keyed dict
therefore retain their original meaning. New OpenAI consumers ignore this
legacy table, including known-wrong `source='fallback'` OpenAI rows. It is not
backfilled with several variants or used as an activation signal.

Migration 044 adds the following separate objects; the names are provisional
revision numbers and must be rebased onto the then-current Alembic head before
implementation merges (`043_person_anchor_rekey` at the audited revision):

```text
model_pricing_generations(
  generation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  schema_version INTEGER NOT NULL CHECK (schema_version = 2),
  policy_version INTEGER NOT NULL,
  snapshot_version TEXT NOT NULL,         -- seed version or refresh timestamp+hash
  status TEXT NOT NULL CHECK (status IN ('building', 'validated')),
  required_variants JSONB NOT NULL,       -- canonical list of complete variant keys
  content_sha256 CHAR(64) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  validated_at TIMESTAMPTZ NULL)

model_pricing_rates_v2(
  generation_id BIGINT NOT NULL REFERENCES model_pricing_generations(generation_id),
  model_id VARCHAR(255) NOT NULL,
  geography VARCHAR(32) NOT NULL,         -- in_region | geo_cris | global_cris | govcloud
  service_tier VARCHAR(16) NOT NULL,      -- standard | priority | flex | batch
  context_tier VARCHAR(16) NOT NULL,      -- short | long | flat
  region VARCHAR(32) NOT NULL,            -- concrete invocation endpoint region
  max_input_tokens INTEGER NULL,
  input_price_per_1k_tokens NUMERIC(14,10) NOT NULL,
  output_price_per_1k_tokens NUMERIC(14,10) NOT NULL,
  cache_read_price_per_1k_tokens NUMERIC(14,10) NULL,
  cache_write_price_per_1k_tokens NUMERIC(14,10) NULL,
  cache_write_policy TEXT NOT NULL,       -- full_rate | no_additional_fee | unpublished
  source VARCHAR(32) NOT NULL,            -- bulk_catalog | model_card | bundled_snapshot
  source_url TEXT NOT NULL,
  source_content_sha256 CHAR(64) NOT NULL,
  source_effective_at TIMESTAMPTZ NULL,
  verified_at TIMESTAMPTZ NOT NULL,
  snapshot_version TEXT NULL,            -- original bundle version for seeded/retained rows
  PRIMARY KEY (generation_id, model_id, geography, service_tier, context_tier, region))

model_pricing_active(
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  current_generation_id BIGINT NULL REFERENCES model_pricing_generations(generation_id),
  pointer_revision BIGINT NOT NULL DEFAULT 0,
  consumers_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  refresh_paused BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL)
```

Insert exactly one pointer row during 044. DB checks restrict dimension/source/
policy enums to the values above, reject negative/non-finite rates, require
positive input/output rates and positive finite `max_input_tokens` when present,
and require a bundle version for `source='bundled_snapshot'`. A full-rate cache
write requires a non-null full price; `no_additional_fee` requires full write
price equal to input; `unpublished` requires NULL. Cache-read NULL means only
unpublished. Zero is not a synonym for unpublished. Enforce generation status
and `validated_at` consistency, and prevent enabling consumers with a NULL
pointer.

**Immutability and publication transaction.** Fetch/parse outside the write
transaction against base pointer revision R. In the transaction, acquire
`SELECT ... FOR UPDATE` on the singleton; reject/rebuild if R changed (or refresh
is paused). Insert one building generation and its **complete** rate set,
validate coverage/constraints/content hash, mark the generation validated, then
advance the pointer and increment `pointer_revision`. Commit all or roll back
all. No building generation is externally visible. Concurrent publishers must
rebase their candidate against the winning pointer; no stale candidate can
silently overwrite a newer publication.

Migration-installed triggers reject any rate INSERT/UPDATE/DELETE against a
validated generation and any mutation/deletion of validated generation metadata.
The pointer trigger requires the target generation to be validated and contain
the manifest's full key set, and rejects pointer deletion. These guards enforce
the trusted application's immutable-publication contract; they are not a
privilege boundary against the connected database administrator.

**Existing database trust boundary.** `infra/main.tf` passes `var.rds_username`
to both budget Lambdas; `infra/variables.tf` defines that as the RDS master
username, default `bgadmin`. Both receive it as `DB_USERNAME`, and
`lambda/shared/db.py` connects as that user. The refresh AWS IAM role permits
`rds-db:connect` for that username; it does not restrict PostgreSQL DDL or
trigger-disable privileges. This release introduces no separate DB credentials
or roles and makes no claim that an administrative session cannot bypass the
triggers. Immutability and required coverage rely on the trusted publisher and
migration code following this contract, backed by guards on ordinary SQL writes and
tests. The DB coverage check verifies rows against the stored manifest; the
publisher must compute that manifest from trusted inputs as specified in §4.5.
A seed uses the same lock and validation contract but its own frozen
migration-local implementation.
Retain all generations in this release: small daily rate sets keep near-term
storage modest, and deletion/retention policy is separate future work.

**Rollback selects an explicit validated ID**, never `generation - 1`. Execute in
one transaction (bind `:known_good_id` from recorded deployment evidence):

```sql
BEGIN;
SELECT * FROM model_pricing_active WHERE singleton FOR UPDATE;
-- Abort unless :known_good_id is validated with full required-key coverage.
UPDATE model_pricing_active
SET current_generation_id = :known_good_id,
    pointer_revision = pointer_revision + 1,
    refresh_paused = TRUE,
    updated_at = now()
WHERE singleton;
COMMIT;
```

The pause stops a scheduled run immediately undoing the rollback; an in-flight
publisher loses its expected revision and cannot publish. Probe cache uptake.
Already emitted request decisions remain pinned to their original generation.
Resume refresh explicitly after correcting and validating its source/parser.
Code rollback leaves V2 storage intact; do not run destructive Alembic downgrades.

**Safe schema detection.** Both Lambda and gateway adapters probe V2 reads in a
separate read transaction/savepoint before processing a settlement. Catch only
PostgreSQL `42P01` (`UndefinedTable`) and `42703` (`UndefinedColumn`) as schema
unavailability, roll back that transaction/savepoint, then use compatibility
pricing. Never issue a fallback query on an aborted psycopg2/SQLAlchemy
transaction. A present but empty/disabled pointer has the same compatible read
behavior; a malformed generation is an operational error, not proof of an old
schema. Other connection/query errors retain good cache or bootstrap as in
§4.1, with metrics. Do not cache “schema absent” indefinitely: retry detection
within 60 seconds during rollout. Keep these fallbacks for this release.

**Precision.** The reproduced scale-6 errors motivating V2 precision remain:

| Rate | per 1M | exact per 1K | scale 6, half-up | error |
|---|---:|---|---|---:|
| Cyber cache write | 17.1875 | 0.0171875 | 0.017188 | 0.003% |
| Luna GovCloud cache read | 0.0264 | 0.0000264 | 0.000026 | 1.52% |
| Luna in-region cache read | 0.022 | 0.000022 | 0.000022 | exact |
| gpt-oss-120b input | 0.15 | 0.00015 | 0.000150 | exact |

`NUMERIC(14,10)` represents every §10 rate exactly. V2 precision is independent
of the unchanged legacy table; no old table/key rewrite is needed.

### 4.3 Usage normalization, cache costs and the durable decision

**Keep API conventions separate.** AWS documents Converse `inputTokens` as
non-cached input: raw total is input + cache reads + cache writes. Its Responses
example reports input 2048, cached 1920, output 256 and total 2304; Responses
input is therefore inclusive of cached tokens. Do not add cached tokens to that
input total. The non-zero Responses write-counter relationship is not explicitly
documented; record this evidence limit and handle inconsistent counters below.

Normalize into a versioned structure containing `api_format`, `input_semantics`,
raw reported input/output/cache counters (including absent versus measured zero),
`total_input_tokens`, `uncached_input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens` and `output_tokens`:

```text
Responses: T = reported input_tokens
Converse:  T = reported inputTokens + reported cache reads + reported cache writes
valid usage: U = T - C - W;  U + C + W = T
```

For Converse, validate each additive counter before forming T; malformed cache
counters are retained as raw evidence but contribute zero to the estimated T.
Do not apply Responses' inclusive-input subtraction to the raw Converse input.

For valid measured counters, preserve them exactly. If counters are negative,
non-integral or overlap, retain the raw values for diagnosis and set
`confidence=estimated`, reason `invalid_usage_counters`, plus a metric. For a
measured non-negative integer T, form one bounded, conservative decomposition:
convert invalid/non-integral counters to zero, clamp negative counters to zero,
then `W = min(raw_W, T)`, `C = min(raw_C, T-W)`, `U = T-W-C`. Writes take
precedence over discounted reads. Never independently charge C+W greater than T.
Invalid reported input/output counts are not measured zero: retain the existing
missing-usage path and emit the anomaly rather than fabricate a settled zero.
Absent cache counters stay NULL in recorded evidence; calculation assumes zero
reported cache activity and marks estimated where activity cannot be established.
An API's explicitly measured zero cache counters are not uncertain.

Context tier uses **T**, before decomposition. T=400,000 and C=320,000 remains
long context. The snapshot stores the short threshold of 272,000 and published
long maximum (Astra 1,050,000; Sol/Terra/Luna/Daybreak Blue 1,000,000). Test
272,000 versus 272,001. Flat models stay flat. A reported total outside published
context coverage uses the longest available rate, marked estimated; it is never
silently certified as a supported context.

**Cache-write policy is explicit; the stored write rate is always a full price.**

| Policy | Stored full write rate | Cost of W newly written tokens |
|---|---|---|
| `full_rate` | Source-published rate (1.25× input on the relevant frontier models) | W × full write rate; do not charge input again on those W tokens. |
| `no_additional_fee` | Equal to ordinary input rate, with the published policy recorded | W × input rate. GPT-5.5/5.4 cache writes remain ordinary paid input; only the uplift is zero. |
| `unpublished` | NULL | Preserve W, estimate at ordinary input rate, and add `unpublished_cache_write_rate`; do not claim a verified zero or invent a 1.25× uplift. |

For unpublished cache-read rates, preserve measured C and estimate those tokens
at ordinary input rate with `unpublished_cache_read_rate`. This applies to
GPT-OSS, whose catalog publishes no cache prices. Do not derive OpenAI cache
rates from Claude rules. Published zero, where a future source explicitly says
so, is distinct from NULL and must pass source validation.

With rates per 1K, calculate exactly with Decimal parsed from decimal strings:

```text
exact_cost_usd = (U*input_rate + C*read_rate + W*full_write_rate + O*output_rate) / 1000
ledger_cost_usd = exact_cost_usd.quantize(Decimal('0.000001'), ROUND_HALF_UP)
```

Only the final ledger value is quantized. Store both decimal strings in the
decision; retain rates at their exact stored scale. Use the same
`ledger_cost_usd` for `usage_logs.cost_usd` and budget settlement. Do not cast to
float before persistence; JSON display conversion is a presentation boundary.
`usage_logs.input_tokens` remains billable uncached input, with the existing
cache columns storing C/W; T is in the event. Thus the tracker's token count is
T+O exactly once and historical column meaning is preserved.

**One server-generated pricing decision.** Extend `ChatLog` in
`src/chat_logging/schemas.py` with an optional top-level `pricing_decision` and
extend `UsageInfo` to carry T and the input convention. Update the builders in
`src/chat_logging/service.py` explicitly; the current Pydantic construction
would otherwise discard extra keys. Add an internal typed `pricing_decision`
argument to logging, supplied by the pricing service, never copied from request
JSON, arbitrary headers, or the upstream response's similarly named field.

The decision includes:

- `decision_version=1`, `policy_version=1`, gateway `request_id` and tenant ID;
- `generation_id` and `pointer_revision` for V2, or NULL generation plus exact
  `snapshot_version` and `source_kind=bundled` for a bootstrap decision;
- the complete selected variant key, exact decimal rate strings, cache-write
  policy, selected row's `verified_at` and source/content hash;
- normalized usage and retained raw counters, requested/served tier and routing
  evidence (§4.4), `confidence` and sorted `estimate_reasons`;
- `exact_cost_usd` and six-decimal `ledger_cost_usd`, both strings, plus a hash of
  canonical decision content for corruption diagnostics (not authentication).

At response completion, including final streaming metadata, the gateway captures
one immutable cache generation and computes this decision **once**, before
scheduling S3 settlement and writing the usage row. Both operations receive that
same object. A later cache swap must not change it. Preserve #4968's behavior:
S3 settlement emission does not depend on successful usage-row persistence,
upstream bytes remain unchanged, and missing usage is not fabricated as zero.

The tracker validates decision version, tenant/request identity, bounded usage,
rate format, and exact arithmetic with the shared versioned policy; it then uses
that decision's ledger value. It may independently reproduce the value using
the embedded rates or the retained immutable generation, but it **must not**
select rates from its own active cache for this event. An unavailable historical
DB row is not grounds to reprice: rates are in the durable event. A malformed
present decision is an error for investigation, never silently treated as a
legacy event. The trusted transport is the existing gateway-written S3 object
and its IAM restrictions, not any client-provided field or the diagnostic hash.
Do not add a public API accepting settlement decisions.

**Retries and legacy events.** Re-reading the same new event always reproduces
the same cost even after publication, code restart, cache skew or rollback.
Keep policy version 1 reproducible while such events are retained. For an OpenAI
legacy event with no decision, use the frozen `2026-09-12.1` compatibility
snapshot and policy version 1, not the current database/cache. Interpret input
according to its `api_format`; choose any recoverable variant dimensions and
apply §4.4's deterministic conservative fallback for the missing ones. Mark
`legacy_event` estimated and emit a metric. Future snapshot updates must retain
this compatibility resolver and its version: retries remain stable. Existing
non-OpenAI legacy events retain their existing curated-policy behavior.

This reuses the S3 key, request identity, recovery `IfNoneMatch` safeguard and
ledger bridge from #4968. It adds no receipt table and changes no entity/period
attribution or historical-recovery selection. Cost stability on retry is
separate from delivery deduplication: the audited tracker increments aggregate
rows per delivery; this design does not claim that #4968 established general
exactly-once processing. Do not replay old objects to verify pricing or repair
historical rates. Any broader duplicate-delivery remedy requires separate work.

### 4.4 Served variant and conservative estimates

Capture routing evidence where `_apply_inference_profile` constructs the actual
forwarded body in `src/proxy/mantle_service.py`. Preserve separately the original
model ID, normalized billing ID, actual forwarded model/profile ARN or ID,
endpoint host, invocation region, and any upstream-reported execution region.
A bare normalized ID cannot recover geography: the current code strips that
information from pricing. Use the actual resolved forwarding configuration to
record `in_region`, `geo_cris`, `global_cris` or `govcloud`. For inference profiles,
rate lookup region means invocation endpoint region; record execution region
separately when supplied, and do not assume the profile's destination. For an
unknown/custom profile, retain the original identifier and mark geography
unconfirmed rather than guessing from a stripped model name.

Capture `requested_service_tier` from the forwarded body, then
`served_service_tier_raw` from the upstream success response. Extend the SSE
sniffer to capture tier from `response.completed`/terminal response metadata
alongside final usage, including across split chunks. Absence of a tier on an
early streaming chunk must not overwrite the final observed tier. Preserve both
requested and observed values; an upstream effective tier wins over the request.
A truncated stream with usage but no confirmed tier remains estimated.

| Observed evidence | Rate selection and confidence |
|---|---|
| Upstream explicitly reports `standard`, `priority`, `flex` or `batch`, and the exact variant is published | Use that served tier, subject to source freshness and usage confidence. |
| Requested tier differs from explicit upstream tier | Use upstream tier; record mismatch and requested value. |
| Upstream says `default`/`auto`, tier is absent, or only the request declares a tier | Treat effective tier as unconfirmed. Do not equate these strings with standard without a versioned, fixture-backed AWS contract for this endpoint. |
| Confirmed tier/geography/region/context has no exact published row | Mark `unsupported_variant`; use the conservative fallback below, never verified standard pricing. |
| Unknown model | Use the existing generic default, marked `unknown_model`; emit `UnknownModelPricing`. |

**Deterministic fallback.** For an unconfirmed tier, select the highest total
cost among published tiers for the known model, geography, endpoint region and
context, using this request's normalized usage. Ignore a requested cheaper tier
until confirmed. For missing/unsupported geography or region, broaden to known
published variants for that model/context and select the greatest complete-row
cost; retain both observed and selected dimensions and mark the inference. For
unsupported context select the longest available tier first. Break equal-cost
ties lexicographically on the canonical variant key. Never mix input from one
row with output from another. If there is no model row, use the generic default.
This bounds the estimate within known rates; it cannot establish a price for a
truly unpublished variant. Frontier cards publish only standard rates, so an
unconfirmed tier uses that sole rate **with estimated confidence**. GPT-OSS
unconfirmed tier normally selects its published Priority rate; confirmed Flex
uses its own SKU and must not be billed as standard. Public estimates follow the
same policy and display uncertainty; only upstream-confirmed measurements can
produce a verified settlement decision.

### 4.5 Daily refresh, complete generations and failure handling

The schedule stays `cron(0 6 * * ? *)`. Each successful run publishes actual
validated rates into V2; a drift-only alarm does not satisfy this defect.

**Source parsing.** The accepted investigation found two independent reasons
the old catalog parser accepts nothing: none of 1,032 catalog products has its
required `attributes.modelId`, and input/output are in separate SKUs rather than
in one product. Replace the synthetic modelId-shaped fixture with real products.
Use `provider`, `model`, `usagetype`, `inferenceType`, `service_tier` and
`regionCode`; join input/output by canonical model/region/tier. The catalog's
legacy `feature` family and mantle `service_tier` family duplicate some rates:
dedupe identical variants, reject conflicting values. `operation` is empty and
must not be a key. Request `regionCode` filters for the configured supported
endpoint regions. Units are explicitly `1K tokens` for these catalog SKUs and
per 1M for the cards; convert to per 1K and reject unknown units. Read every
GPT-OSS service-tier SKU; Safeguard rounding means multipliers cannot derive it.

The eight frontier cards are AWS Markdown with one `## Pricing` section. Their
scope labels are bold paragraphs, not nested headings. Implement a state machine
for commercial/GovCloud scope, context, and table columns; especially Terra/Luna,
whose GovCloud blocks follow commercial tables and repeat context headings.
Require complete recognizable table/header/scope structure before accepting any
row from a card. The four GPT-OSS cards have no table and deliberately defer to
the catalog; this absence is expected. Never scrape the JavaScript-populated
pricing-page HTML, whose static GPT-OSS rows are Sydney prices.

Fetch only the eight frontier cards plus catalogs needed for the supported
region manifest. Use HTTPS, bounded response size (10 MiB per catalog; 1 MiB per
card), connection/read deadlines, at most four concurrent fetches and bounded
source retries. Start with a 180-second Lambda timeout and a 120-second overall
fetch/parse deadline, leaving time for publication/metrics. Measure against real
fixtures and source latency before shipping; adjust bounds from evidence, not an
unbounded retry loop. Save URL, source publication date if available, retrieval
verification time, and content hash. A changed hash by itself is not an error:
price changes must be adopted; changed unrecognized structure is an error.

**Required coverage is a set of variant keys, not a model count.** The frozen
snapshot enumerates `(model_id, geography, service_tier, context_tier, region)`
for all 12 models and every published combination in §10. Expand combined
“In-Region / Geo CRIS” rows into separate keys. Region expansion is limited to
endpoint regions explicitly covered by the publication: include the verified
commercial and GovCloud scopes, and never invent us-east-1 availability for a
card that lists only us-west-2. Store this concrete expansion, including its
source availability evidence, in the snapshot; tests compare exact key sets.

A candidate's required set is the union of the bundled required keys and the
active generation's keys, recomputed by the trusted publisher under the pointer
lock. Invocation payloads and external callers cannot supply or override this
manifest, the bundled seed floor, or a retirement list; reject any such
fields rather than trusting a caller's smaller required set. Newly discovered
supported variants add keys after validation; removal requires an explicit
reviewed manifest revision, never just
absence from a fetch. This keeps long-context, geography and service-tier rows
from disappearing behind a “12 models covered” metric.

**Candidate assembly and validation.** Begin with all active rows (or the
frozen seed for the first population). Replace a row only with a freshly fetched,
validated value from its designated AWS source. A copied prior row retains its
original `source`, `source_url`, content hash, `snapshot_version`,
`source_effective_at` and **`verified_at`**. Only rows confirmed by their own AWS
source advance verification time, even when their price is unchanged. A new
generation may therefore contain both fresh and retained rows.

Validate recognized units, positive finite input/output, valid decimal scale,
exact model/region/tier mappings, cache-write policy, context bounds, duplicate
consistency and full required-key coverage. Compare changed prices with the
prior same-variant value: initially reject values below 0.5× or above 2× as
suspect and emit the source evidence for review. For families publishing full
cache-write/read rates, confirm 1.25×/0.10× against source values; these are
corroboration checks, not generated prices. For GPT-5.5/5.4, validate the explicit
no-additional-fee policy instead. Unpublished cache prices remain NULL.

| Outcome | Publication and invocation result |
|---|---|
| All required keys freshly validated | Atomically publish a complete generation; emit success, coverage and source freshness. |
| A source fetch fails/times out, or a well-formed source omits a previously known variant; at least one source supplies fresh valid rows and retained rows complete coverage | Publish one **complete** mixed generation, emit `PricingRefreshPartial` and retained-key count, then raise `PartialRefreshError`. Lambda retry can recover the failed source. Never report full success or renew retained timestamps. |
| Fetched required source is empty/unparseable, schema/scope validation fails, conflicting duplicates or an invalid new rate appears | Reject the entire attempted publication; raise, pointer unchanged. Do not rescue a parser/validation failure with bundled rates. |
| All sources fail, zero fresh usable rows, or the assembled candidate lacks any required key | Raise; pointer unchanged, prior data retained. A first deployment already has the seed. |
| DB/transaction failure or concurrent pointer change | Roll back. Retry a concurrency conflict after rebuilding from the winning generation; raise on unresolved/DB failure. |
| V2 schema not yet present, no seed/disabled consumers, or refresh paused | Perform no legacy writes. Record `PricingRefreshDeferred` and return a deployment/paused result; rollout checks must subsequently prove activation and a real refresh. |

A valid partial publication is not a partially written snapshot: all required
rows exist in its transaction, and stale retained keys remain visible in
coverage/freshness metrics. If its post-commit exception is retried, the next
attempt starts from the now-active generation; retries cannot undo fresh good
rates. Publish no fallback values in a failure handler.

**Delivery and execution failures are different retry domains.** EventBridge
invokes this Lambda asynchronously. Returning `{'statusCode': 500}` does not
signal a function failure; raise an exception. EventBridge successfully handing
an event to Lambda does not mean the function completed successfully.

| Layer | Terraform and exact policy |
|---|---|
| EventBridge delivery | `aws_cloudwatch_event_target.pricing_refresh.retry_policy`: `maximum_retry_attempts=2`, `maximum_event_age_in_seconds=3600`; `dead_letter_config` targets a dedicated SQS delivery DLQ. This handles failure to deliver to Lambda. |
| Lambda asynchronous execution | Add `aws_lambda_function_event_invoke_config.pricing_refresh` for the actual unqualified target function: `maximum_retry_attempts=2`, `maximum_event_age_in_seconds=3600`, and `destination_config.on_failure.destination` pointing to a separate SQS execution-failure queue. This handles function exceptions/exhausted asynchronous events. |
| Permissions | Delivery queue policy permits `events.amazonaws.com` `sqs:SendMessage`, scoped to this rule ARN and account. Lambda role receives `sqs:SendMessage` for its execution-failure queue. Use SQS-managed encryption for these queues; if customer KMS is required, include the matching producer key permissions. Retain the scoped `aws_lambda_permission` for EventBridge invocation. |
| Metrics IAM | Add refresh-role `cloudwatch:PutMetricData` limited by namespace; current role has none. Log/raise operational metric publication failures, do not silently `except: pass`. Pricing API access still requires `Resource: '*'`. |

Both queues retain messages for 14 days and expose inspectable failure context.
Alarm separately on EventBridge `FailedInvocations`/DLQ-delivery failure,
Lambda `Errors`, `AsyncEventsDropped` and `DestinationDeliveryFailures`, and each
queue's visible-message count/age. Provision alarms on:

- missing/retained required variants (`PricingRequiredVariantsMissing`,
  `PricingVariantsRetained`), plus model count as supplementary information;
- `PricingOldestVerifiedAgeHours` (maximum selected row age, threshold 48 hours),
  source-specific verification age and absence of a full refresh for 30 hours;
- `PricingRefreshPartial`, `PricingRefreshRejected`, schema-deferred state after
  deployment, and consumer cache/staleness/unknown-variant estimates.

Use missing-data-as-breaching for daily-refresh/freshness heartbeats. Wire a real
notification destination through `budget_alarm_sns_topic_arns`; an empty default
is not delivered observability. Validate alarm action ARNs and subscriptions in
the release evidence. Infrastructure lives in `infra/modules/budget-lambda/`
(`main.tf`, `iam.tf`, `alarms.tf`, `variables.tf`, `outputs.tf`) and its root
variable/module wiring.

### 4.6 One-release bootstrap, convergence and rollback

Migration 045 contains a **literal** frozen snapshot version `2026-09-12.1`,
with all rates, required keys, cache policy and source evidence necessary to
seed V2. It imports neither `pricing_policy` nor `lambda/shared`. Its independent
migration-local validation/publish helper obeys §4.2; the pod can execute it with
only Alembic and its normal DB dependencies. Runtime packaging does not make
mutable migration imports acceptable.

On a clean DB, 044 creates disabled empty V2 state; 045 publishes the complete
seed and enables consumers in the same transaction. Seeded rows use
`source='bundled_snapshot'`, original AWS URLs/hashes, `snapshot_version`, and
`verified_at=2026-09-12` at the recorded verification time, **not deployment
now** (use 00:00:00 UTC conservatively if only the verification date is known).
The seed is immediately useful; an old seed may be estimated until the
first real refresh. No scheduled run is needed to populate it.

On an existing environment, the same migration publishes complete V2 coverage.
Legacy wrong OpenAI `source='fallback'` rows remain physically unchanged but are
no longer selected as soon as V2 activates; all consumer paths must converge in
this corrective deploy. If V2 already contains data (e.g. retry/recovery), merge
by variant under the pointer lock: preserve `bulk_catalog`/`model_card` rows and
their timestamps, replace older bundled rows only with the newer explicit
snapshot revision, and fill missing keys. Never compare arbitrary version
strings lexicographically: the seed declares its ordered bundle revision and
supported predecessor versions. If a current row's version is unknown, retain
it and report it rather than overwrite. No fresh verified row is downgraded to
a bundled value. An identical candidate is a no-op; repeated seed helper calls
or repeated `alembic upgrade head` do not create new generations.

**Executable corrective-release order in existing CI.** These are deployment
steps in one release, not three future releases. Keep the Lambda-before-backend
ordering already present in `.github/workflows/gateway-deploy.yml` and add the
following guards and verification:

1. CI builds/tests the common package, both zip shapes, migrations and gateway
   image from the same commit. Serialize gateway deployments for the target
   account/environment (`cancel-in-progress: false`). Before replacing existing
   refresh code, disable its EventBridge rule and wait at least its configured
   maximum running timeout to drain old invocations. Update the **refresh Lambda
   first**, wait for function update, then update the tracker, also waiting.
   The new refresh never writes legacy OpenAI rows, including when an old queued
   event later invokes it. If a deployment fails here, leave the rule disabled
   and surface that failure rather than re-enable the known-bad writer.
2. The new tracker works before migration: OpenAI events lacking a decision use
   the pinned compatibility snapshot; non-OpenAI uses legacy data plus curated
   cache policy. Missing V2 tables **or columns** are caught and the probe
   transaction rolled back before settlement. Existing producer events remain
   readable. Deploy the new gateway image next; until V2 is ready it uses the
   same bundle and emits durable pricing decisions already readable by the new
   tracker. DB schema rollout cannot poison existing ledger transactions.
3. The existing `run-migrations` job, after `deploy-backend`, runs 044 and 045 in
   the new pod. This publishes the seed and turns on V2 in one transaction.
   Add a post-migration pricing verification job (depending on both deployment
   and migrations) that asserts enabled pointer, complete required-key coverage,
   seed version and corrected OpenAI resolutions through both adapters. Warm or
   probe all gateway workers/refresh caches; allow at most the 15-minute TTL,
   with schema-negative probes retried within 60 seconds. Do not declare deploy
   success while consumers still price known bad legacy fallback rows.
4. Review and dispatch `.github/workflows/gateway-infra-apply.yml` at this same
   release commit to deploy IAM, both retry domains/queues and alarm actions;
   it is manual-only and must actually run before completion. Its Terraform
   archives contain the same package/code. Re-enable the refresh rule only when
   the new code, seeded schema and infra configuration are verified; avoid a
   simultaneous older infra apply restoring stale archives during these steps.
5. Invoke one refresh immediately with `aws lambda invoke` (check
   `FunctionError`, not an HTTP-shaped response), verify publication/retention
   metrics and consumer adoption, then verify rule state `ENABLED`, its 06:00
   UTC schedule, execution retry config, both failure destinations and alarm
   actions. Capture the generation ID/revision as the known-good rollback
   target. Run a controlled new Codex request to verify the event's exact
   decision and settled amount. This completes the correction without waiting
   until the next scheduled run.

For a fresh `deploy-all.sh`, absent functions/rules can be skipped only until
infrastructure creates them with the new code. Its pre-rollout migration,
post-rollout migration and opportunistic third `alembic upgrade head` invocation
all remain safe: an old pod may perform no new migration; the new pod runs the
frozen seed; repeats are no-ops. Add equivalent final seed/activation/refresh
verification to that path so an absent initial Lambda is not left unverified.

**Rollback behavior.** A bad rate publication uses the locked, known-ID rollback
transaction in §4.2 and keeps refresh paused. An application rollback disables
consumers in the pointer under the same lock, increments its revision and pauses
refresh, then rolls back the gateway producer first. Keep the compatible tracker
and shared policy until all new-decision events can be drained/retried; reverting
that consumer to code that ignores decisions can reprice in-flight events. The
legacy table/key remains valid for old gateway code. Returning fully to old code
also returns its old pricing defect and is an incident fallback, not successful
completion. Leave 044/045 in place; migration downgrades are not part of rollback.

## 5. Staged implementation plan

Implement these stages in one corrective implementation PR/release after this
design is approved. No stage alone closes #4969. Deployment ordering is §4.6;
these stages describe construction and verification dependencies.

| Stage | Deliverable | Depends on | Completion evidence |
|---|---|---|---|
| S1 | Shared runtime policy, versioned complete snapshot, model normalization, packaging and trigger changes; remove competing literals through compatible adapters. | D6 | Exact rate/variant inventory, package import and archive parity checks. |
| S2 | 044 creates V2 generations/rates/pointer, immutability and publication guards; 045 independently frozen seed with idempotent convergence. | S1 contract | Fresh PostgreSQL deployment, legacy compatibility, rollback/concurrency and seed parity tests. |
| S3 | V2 feature-detecting caches and compatibility adapters; public cost endpoint/estimator use shared policy; preserve non-OpenAI curated cache rules. | S1, S2 contract | Missing-table/column transaction recovery, stale-good retention and immediate corrected fallback tests. |
| S4 | Responses usage normalization, served variant capture, terminal streaming metadata, durable decision construction and tracker reuse/versioned legacy fallback. | S1, S3 | Hand-computed costs, cache-skew/replay and actual S3 builder integration tests; #4968 behavior retained. |
| S5 | Source parsers, required-key coverage, complete candidate assembly, validation and atomic daily publication; new writer never overwrites legacy OpenAI rows. | S1–S3 | Real AWS fixtures, failure/partial publication and source freshness tests. |
| S6 | Retry domains, IAM, queues, alarms, time bounds and one-release CI/deploy-all activation/verification safeguards. | S2–S5 | Terraform plan, workflow/package checks and staged rollout rehearsal. |
| S7 | Deliver code plus infra, seed/activate, refresh immediately, verify new request cost and scheduled operation; update operator docs and attach evidence. | S1–S6 | Exact generation/variant coverage, live configuration and ledger evidence; #1017 checklist assessed separately. |

## 6. Test and acceptance plan

**Inventory and packaging (S1).** Table-driven assertions cover all 12 OpenAI
models, eight frontier cards, all §10 rates and exact variant-key coverage,
including long context and every published geography/service tier. Pin decimal
units, source URLs/hashes and policies. Build both CI and Terraform Lambda
archives and compare normalized entry names/content hashes; unpack them into
isolated temporary directories and cold-import/load snapshot without repository
`src/` or `lambda/shared` on `PYTHONPATH`. Smoke-import the same package in the
gateway image. Verify a change only under `pricing_policy/` triggers both Lambda
updates and the backend build and runs the corresponding CI tests.

**Frozen seed parity (S2).** Pin the canonical JSON serialization and SHA-256 of
`2026-09-12.1`; compare migration-owned literal rows, policies, required keys and
provenance to that **specific** immutable runtime snapshot version. Do not compare
an old migration with the mutable current-version selector. A test upgrades a
clean DB after substituting a later runtime selector and proves old seed content
is unchanged. This reproducibly checks the intentional duplicate without an
import from live runtime code.

**Storage, precision and concurrency (S2).** Use isolated PostgreSQL 16 with
actual migrations, not SQLite. Same variant can exist in two generations; rows
from both remain queryable. Test generation FKs, immutability triggers, active
pointer uniqueness, rejected unvalidated/partial targets and whole-transaction
rollback. Two refresh publishers racing must retain the winning rows when the
loser rebuilds; an in-flight publisher must not undo an operator rollback or
pause. Roll back to a specified validated ID even with gaps in generation IDs;
consumers adopt its newer pointer revision. Round-trip the exact per-1K decimals
0.0171875 (Cyber write), 0.0000264 (Luna GovCloud read) and 0.0002 (Safeguard 20B
output) through `NUMERIC(14,10)`, and compare hand-calculated costs. Ensure the
rate writer rejects values exceeding supported scale rather than silently
rounding them.

**Bootstrap and compatibility (S2/S3).** Clean DB upgrades create complete enabled
seed state. Run seed helper repeatedly and exercise all three deploy-all
migration invocations: no extra generation or value changes. Start with wrong
legacy OpenAI fallback rows and assert new gateway/tracker/public estimates
converge to corrected policy without waiting for 06:00. Keep known-good
`bulk_catalog`/`model_card` V2 rows and their verification timestamps on seed
replay; only older known bundled rows may change. Prove old legacy
`ON CONFLICT(model_id)` and dict readers still work with one row per model.
Exercise new readers against pre-044 schema, a missing required V2 column,
disabled/empty pointer, failed query and successful subsequent ledger write on
the same connection: the failed probe must have been rolled back. Test a cache
with a newer DB rate whose connection then fails for hours; it stays on that
rate, becomes estimated and emits staleness, never downgrading to the old bundle.

**Hand-computed request costs (S4).** The following use USD per 1M rates from
§10, commercial in-region standard tier, with explicit upstream tier/region
confirmation and measured zero counters unless stated. Assert both exact and
six-decimal ledger amounts. Expected numbers are fixed oracles, not outputs
from the implementation being tested.

| Request | Exact USD | Proves |
|---|---:|---|
| Sol short: T=1,000, O=1,000, no cache | 0.0264 | Corrected $4.40/$22.00 rates. |
| Sol short: T=2,048, C=1,920, W=0, O=256 (AWS example) | 0.00704 | Inclusive Responses input; cached tokens charged once. |
| Astra: T=400,000, C=320,000, W=0, O=1,000 | 2.5465 | Long tier from raw total; 80k × $22 + 320k × $2.20 + 1k × $82.50, divided by 1M. |
| Sol short: T=1,000, C=200, W=400, O=100 | 0.006248 | Full 1.25× write price once. |
| GPT-5.5: T=1,000, C=200, W=400, O=100 | 0.00781 | Newly written tokens remain normal paid input with no additional fee. |
| GPT-5.4: T=1,000, C=200, W=400, O=100 | 0.003905 | Same no-uplift policy at its distinct rate. |
| GPT-OSS-120B confirmed Flex: T=1,000, O=1,000 | 0.000375 | Actual Flex SKU, not standard. |
| GPT-OSS-120B unconfirmed tier: T=1,000, O=1,000 | 0.0013125 | Estimated maximum complete-row Priority cost; ledger 0.001313. |
| Sol short invalid overlap: T=1,000, raw C=900, raw W=600, O=0 | 0.003476 | W=600, C=400, U=0, estimated; all charged input is bounded by T. |

Also cover the 272,000/272,001 boundary, flat models, long maximum overflow,
absent versus measured-zero cache counters, invalid negative/non-integral
counters, unpublished read/write policy, unknown model and unsupported region/
tier. A Converse fixture with equivalent normalized usage must produce the same
cost while using its additive raw-input convention.

**Decision and effective-tier integration (S4).** Extend
`tests/lambda/test_mantle_budget_settlement.py` using the actual gateway request,
SSE sniffer, Pydantic chat-log builder, S3 payload and tracker. Requested Flex but
served Priority uses Priority. Requested/default/auto with no confirmed tier is
estimated. Final tier metadata split across SSE chunks survives; an early missing
tier cannot erase it. Normalizing `global.`/`us.` prefixes must preserve the
forwarded profile, endpoint region and selected geography. Verify clients cannot
inject decision/cost fields through body or headers.

Warm gateway cache on generation A and tracker on B, publish C between inference
and settlement, then retry the exact event after process restart and a pointer
rollback. Every attempt must use the original decimal decision from A, without
consulting B/C for new rates. Validate corrupt decisions fail explicitly, and
new events still settle when historical generation lookup is unavailable because
the embedded rates suffice. Legacy OpenAI events without a decision use the
same frozen compatibility cost before/after publication and bundle-selector
changes. This checks pricing stability, not an unsupported exactly-once claim.
Do not use historical replays as a production test.

**Parsers and failures (S5).** Use sanitized fixtures from AWS catalog version
20260911124408: split input/output SKUs, both naming families, all four GPT-OSS
tiers, non-us-east-1 region, exact declared unit, unrecognized unit and conflicting
duplicate. Show the old parser returns zero on real products and the new parser
produces the expected keys/rates. Markdown fixtures include Astra's dual contexts,
Terra's repeated commercial/GovCloud contexts, Luna's smallest prices, GPT-5.5's
no-context/no-additional-fee shape, Cyber's one in-region table, Daybreak Blue's
long row, and expected no-table GPT-OSS cards.

Test each §4.5 outcome independently. One source transport failure plus fresh
valid rows publishes complete coverage with retained rows' original
`verified_at`, emits partial and raises. A missing long/geography key with no
prior row rejects publication even if model count remains 12. Parse error,
invalid rate, all-source outage, zero fresh rows and DB failure preserve pointer
and timestamps. A caller-supplied smaller manifest is rejected and cannot bypass
the publisher's union of bundled required keys and active-generation keys.
A rate-only content change with valid structure publishes the
new price and affects a subsequently priced request. No error path writes
bundled values over good published rates.

**Infrastructure and rollout (S6/S7).** Assert both Terraform retry resources,
queue policies and Lambda role permissions. Separately simulate EventBridge
delivery denial and Lambda function exception; confirm the appropriate queue,
metric and alarm each time, using an isolated test target or controlled failure
injection that cannot publish bad rates. Verify age/retry exhaustion and
execution destination delivery permission. Rehearse Lambda-before-migration and
backend-before-seed windows, failure cleanup, same-commit infra apply, and final
verification gates. Ensure disabled schedule or empty alarm actions cannot be
reported as completed deployment. Test runtime duration against the selected
deadlines with representative full source fixtures.

**Regression.** Run `test_cache_token_pricing.py`,
`TestIssue4592MissingModelIds`, `test_mantle_budget_settlement.py` and
`test_mantle_budget_recovery.py`; adapt fixtures only for the explicit event
schema/decision contract, preserving their existing invariants. Assert Claude/
Nova resolved rates, normalization, curated cache policy, unknown-model metrics,
entity attribution and no duplicate root debit are unchanged. Preserve the
existing direct-proxy missing-usage, logging-failure, upstream passthrough and
usage-DB-failure settlement behavior.

**Delivered acceptance evidence (S7).** Record migration head, active generation
ID and pointer revision, all required variant keys, original seed verification
age, source/precision checks, deployed image/zip package versions, Lambda async
config, EventBridge schedule/retry/DLQ and alarm actions. Run one immediate
refresh and show actual validated rates published (or report a real source
failure without claiming completion). Probe both consumers and perform a
controlled new Codex request, matching its S3 decision and token decomposition
to the Budget & Spend amount. Record the known-good rollback ID and affected
historical window; attach evidence before closing #4969 or assessing #1017.

## 7. Preserve non-OpenAI cache accounting

The current tracker loads three DB columns and selects its DB dict on whole-dict
truthiness. That bypasses the curated four-key Claude cache entries and can lose
normalization/unknown-model metrics. Simply populating legacy `model_pricing`
would amplify that problem. This release therefore seeds **V2 OpenAI storage**
and keeps the legacy table/key untouched.

The shared resolver merges non-OpenAI legacy input/output rows per normalized
model with its existing curated cache policies. For a curated four-rate Claude
entry, preserve its explicit cache-read/write prices; a three-column DB row must
not replace them with an inferred universal multiplier. For a model whose
existing policy deliberately derives cache rates, preserve that policy and mark
missing evidence according to its existing behavior. Keep suffix/prefix
normalization and `UnknownModelPricing` emission reachable. No OpenAI cache rule
is applied to Claude, Nova or other providers. Tests pin existing resolved
non-OpenAI behavior and the curated policies through empty, partial and nonempty
legacy DB dictionaries.

## 8. Relationship to #1017

#1017's cold-start seed requirement is delivered through S2 plus S3/S7 activation
and verification, not by importing the live fallback dict. Its sample references
nonexistent column names (`input_per_1k_usd`, `output_per_1k_usd`,
`last_refreshed`) and assumes `lambda/shared` exists in the migration pod.
The actual legacy names are shown in §4.2; V2 is the new seed target, and the
migration is self-contained. Update the story to link the delivered migration
and explain the compatibility storage boundary. Keep #1017 open until fresh
bootstrap, idempotence, existing-environment convergence and good-rate retention
are demonstrated against its checklist. Writing or merging this design does
not deliver those criteria.

## 9. Evidence limits and residual risks

- AWS's Responses cache example establishes inclusive input for cache reads;
  its non-zero write-counter relationship still needs a live example. The
  bounded decomposition and confidence reason keep that uncertainty explicit.
- AWS Markdown is a scraped publication, with bold scope labels and variable
  headings. Strict structure validation, real fixtures and complete key coverage
  protect publication; a site reshape may stop refresh and stale rows will alarm.
  A hash change alone must not stop valid price updates.
- No verified AWS contract here equates effective `default`/`auto` with standard.
  The conservative estimated tier policy is deliberate until such a contract is
  captured in versioned fixtures. Missing served-tier metadata may make otherwise
  correct requests estimated; it must not silently imply verified billing.
- Astra's card lists mantle availability in us-west-2 while this deployment's
  default Mantle region is us-east-1. Capture actual forwarding and mark an
  unsupported region estimated; pricing correction does not fix routing.
- Live legacy DB rows, deployed Lambda configuration, notification subscriptions
  and measured refresh runtime were not inspected in this design. Release
  verification must establish them. Empty SNS configuration is a delivery task,
  not evidence that alarms reach an operator.
- Existing S3 duplicate delivery can increment aggregate usage again. This
  revision pins the amount and retains #4968's recovery guards; a broader
  idempotency change is separate work, and no historical replay is authorized
  by this design.

## 10. Verified rate inventory

USD per 1,000,000 tokens, retrieved 2026-09-12. Columns: input / 30-minute cache
write / cache read / output. Every frontier card carries the identical footnote:
*"All prices are per 1 million tokens. Pricing shown is for the Standard tier.
Priority and Flex tiers are not supported for this model."* Cache write is
exactly 1.25× input and cache read exactly 0.10× input in every row that
publishes them, across all geographies — verified arithmetically across the eight
frontier cards where those rates are published.

“No additional fee” below means normal input cost with zero write uplift, not
free newly written input. The V2 full write price for those rows equals input.

| Model | Context | Geography | In | Cache write | Cache read | Out |
|---|---|---|---:|---:|---:|---:|
| GPT-6 Astra | short (272K) | In-Region / Geo CRIS | 11.00 | 13.75 | 1.10 | 55.00 |
| GPT-6 Astra | short | Global CRIS | 10.00 | 12.50 | 1.00 | 50.00 |
| GPT-6 Astra | long (1.05M) | In-Region / Geo CRIS | 22.00 | 27.50 | 2.20 | 82.50 |
| GPT-6 Astra | long | Global CRIS | 20.00 | 25.00 | 2.00 | 75.00 |
| GPT-5.6 Sol | short (272K) | In-Region / Geo CRIS | 4.40 | 5.50 | 0.44 | 22.00 |
| GPT-5.6 Sol | short | Global CRIS | 4.00 | 5.00 | 0.40 | 20.00 |
| GPT-5.6 Sol | long (1M) | In-Region / Geo CRIS | 8.80 | 11.00 | 0.88 | 33.00 |
| GPT-5.6 Sol | long | Global CRIS | 8.00 | 10.00 | 0.80 | 30.00 |
| GPT-5.6 Terra | short | In-Region / Geo CRIS | 2.20 | 2.75 | 0.22 | 13.20 |
| GPT-5.6 Terra | short | Global CRIS | 2.00 | 2.50 | 0.20 | 12.00 |
| GPT-5.6 Terra | long | In-Region / Geo CRIS | 4.40 | 5.50 | 0.44 | 19.80 |
| GPT-5.6 Terra | long | Global CRIS | 4.00 | 5.00 | 0.40 | 18.00 |
| GPT-5.6 Terra | short | GovCloud (US-East, US-West) | 2.64 | 3.30 | 0.264 | 15.84 |
| GPT-5.6 Terra | long | GovCloud | 5.28 | 6.60 | 0.528 | 23.76 |
| GPT-5.6 Luna | short | In-Region / Geo CRIS | 0.22 | 0.275 | 0.022 | 1.32 |
| GPT-5.6 Luna | short | Global CRIS | 0.20 | 0.25 | 0.02 | 1.20 |
| GPT-5.6 Luna | long | In-Region / Geo CRIS | 0.44 | 0.55 | 0.044 | 1.98 |
| GPT-5.6 Luna | long | Global CRIS | 0.40 | 0.50 | 0.04 | 1.80 |
| GPT-5.6 Luna | short | GovCloud (US-East, US-West) | 0.264 | 0.33 | 0.0264 | 1.584 |
| GPT-5.6 Luna | long | GovCloud | 0.528 | 0.66 | 0.0528 | 2.376 |
| GPT-5.5 | flat | In-Region | 5.50 | no additional fee | 0.55 | 33.00 |
| GPT-5.4 | flat | In-Region | 2.75 | no additional fee | 0.275 | 16.50 |
| GPT-5.4 | flat | GovCloud (US-West) | 3.30 | no additional fee | 0.33 | 19.80 |
| GPT-5.6 Cyber | short only | In-Region | 13.75 | 17.1875 | 1.375 | 82.50 |
| Daybreak Blue 5.6 Sol | short | In-Region | 5.50 | 6.875 | 0.55 | 33.00 |
| Daybreak Blue 5.6 Sol | long | In-Region | 11.00 | 13.75 | 1.10 | 49.50 |

GPT-5.5, GPT-5.4, Cyber and Daybreak Blue have no CRIS rows. Astra, Sol and
Daybreak Blue have no GovCloud rows. GovCloud scope differs per card: US-East and
US-West for Terra and Luna, US-West only for GPT-5.4.

**GPT-OSS, all four tiers** (us-east-1, from the bulk catalog; no cache or
context-tier rates published for any of them). The published "Priority +75% /
Flex-Batch −50%" footnote holds exactly for gpt-oss-20b and 120b but **not** for
the Safeguard SKUs, which are rounded to whole cents — read the SKU, never derive:

| Model | Standard in/out | Priority in/out | Flex in/out | Batch in/out |
|---|---:|---:|---:|---:|
| gpt-oss-20b | 0.07 / 0.30 | 0.1225 / 0.525 | 0.035 / 0.15 | 0.035 / 0.15 |
| gpt-oss-120b | 0.15 / 0.60 | 0.2625 / 1.05 | 0.075 / 0.30 | 0.075 / 0.30 |
| GPT OSS Safeguard 20B | 0.07 / 0.20 | 0.12 / 0.35 | 0.03 / 0.10 | 0.03 / 0.10 |
| GPT OSS Safeguard 120B | 0.15 / 0.60 | 0.26 / 1.05 | 0.07 / 0.30 | 0.07 / 0.30 |

**Bundled versus published** (per 1M, in-region short-context standard; ratio is
bundled ÷ published):

| Model | Published in/out | `src/budget/pricing.py` today | Input | Output |
|---|---|---|---:|---:|
| GPT-6 Astra | 11.00 / 55.00 | absent → default 3.00 / 15.00 | 0.27× | 0.27× |
| GPT-5.6 Sol | 4.40 / 22.00 | 5.50 / 33.00 | 1.25× | 1.50× |
| GPT-5.6 Terra | 2.20 / 13.20 | 2.75 / 16.50 | 1.25× | 1.25× |
| GPT-5.6 Luna | 0.22 / 1.32 | 1.10 / 6.60 | 5.00× | 5.00× |
| GPT-5.5 | 5.50 / 33.00 | 5.50 / 33.00 | 1.00× | 1.00× |
| GPT-5.4 | 2.75 / 16.50 | absent → default | 1.09× | 0.91× |
| GPT-5.6 Cyber | 13.75 / 82.50 | absent → default | 0.22× | 0.18× |
| Daybreak Blue 5.6 Sol | 5.50 / 33.00 | absent → default | 0.55× | 0.45× |
| gpt-oss-120b | 0.15 / 0.60 | 0.1545 / 0.618 | 1.03× | 1.03× |
| gpt-oss-20b | 0.07 / 0.30 | absent → default | 42.9× | 50.0× |
| GPT OSS Safeguard 120B | 0.15 / 0.60 | absent → default | 20.0× | 25.0× |
| GPT OSS Safeguard 20B | 0.07 / 0.20 | absent → default | 42.9× | 75.0× |

The agent-worker's Codex default is `openai.gpt-5.6-sol`
(`modules/agent-factory/agent-worker-image/codex-config.toml:25`), so the
1.25×-input / 1.50×-output overcharge is what most current traffic hits. It is an
overcharge, so caps bite earlier than they should. The `0.1545` gpt-oss-120b
figure is the Asia-Pacific (Sydney) rate, which is the one statically rendered in
the pricing page HTML — consistent with it having been copied from there rather
than from a us-east-1 source.

**Runtime ids.** Astra/Sol/Terra/Luna: bare `openai.<model>` plus `us.` and
`global.` inference profiles; the bare id is not invocable on `bedrock-runtime`
(profile required), which `mantle_service.py:205-219` compensates for. GPT-5.5,
GPT-5.4, Cyber and Daybreak Blue are mantle-endpoint only with no geo/global
profiles. gpt-oss models are invoked with the bare id
(`BG_MANTLE_ON_DEMAND_MODELS`) and have `-1:0` runtime variants. Endpoint paths
are `/openai/v1` on both `bedrock-mantle.{region}.api.aws` and
`bedrock-runtime.{region}.amazonaws.com`.

## 11. Evidence boundary

**Verified by direct inspection at `662906e`:** every file:line citation in this
note; the parser's two independent rejection causes; the `float()` cast in
`upsert_model_pricing:186` (checked and *not* a live precision error — psycopg2
sends the shortest round-trip repr — so it is not claimed as a defect); the
refresh role having zero `PutMetricData` statements against the tracker's three;
`gateway-deploy.yml` job ordering and path filters; the Dockerfile's COPY list
and the absence of any import path between `src/` and `lambda/`; the three
migration invocations in `deploy-all.sh`; the current Alembic head
(`043_person_anchor_rekey`); `BG_MANTLE_ON_DEMAND_MODELS` and the
inference-profile prefix being config-sourced; the absence of any `service_tier`
handling in `src/`; the absence of any `INSERT INTO model_pricing` in Alembic.

**Verified against live AWS publications on 2026-09-12:** every rate in §10; the
12 model-card `.md` URLs returning HTTP 200 `text/markdown` and their heading and
column structure; the cache-write 1.25× and cache-read 0.10× statements; the
GPT-5.5/5.4 no-cache-write-fee statement; the single Responses API usage example
and the absence of any documented statement on Responses `input_tokens`
semantics; the Converse-only scope of the documented additive formula; the
catalog's attribute shape, unit strings, dual SKU families, gpt-oss tier rates
and absence of `modelId`; the gpt-oss cards containing no pricing tables; the
pricing page's JS-populated regional tables.

**Reproduced by execution in the prior investigation:** the scale-6 rounding
table in §4.2. The revision-3 SQL, rollout and test plans are proposed contracts,
not claims of executed validation.

**Not verified:** live `model_pricing` contents; live Lambda configuration as
deployed (Terraform source only); whether the refresh Lambda has ever succeeded
in dev; catalog files for other regions or versions; a non-zero
`cache_write_tokens` response from a live request; measured refresh runtime
against either the existing 60s timeout or the proposed 180s timeout.
