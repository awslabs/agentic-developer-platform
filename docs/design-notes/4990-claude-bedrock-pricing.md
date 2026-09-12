# Claude Bedrock pricing refresh and deployment seed (#4990)

The same atomic pricing publication and deployment pipeline introduced in #4969
will cover current Claude models served through AWS Bedrock. The coordinator
reviewed this extension against the existing release, source publications, and
historical-event compatibility requirements. It reuses the refresh Lambda,
06:00 UTC schedule, retry destinations, alarms, and operational inbox.

## Sources and coverage

Claude model cards document model IDs, endpoint availability, context limits and
features. Their Pricing sections link to the AWS Bedrock pricing page; they do
not contain the OpenAI-style pricing tables. The AmazonBedrock bulk catalog also
omits usable current Claude input/output/cache coverage. Neither is a sufficient
Claude price feed on its own.

Use the tokenized tables on <https://aws.amazon.com/bedrock/pricing/> and their
AWS-hosted USD data map:
<https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/bedrockfoundationmodels/USD/current/bedrockfoundationmodels.json>.
Join table column meanings and token IDs to exact region prices, then intersect
with reviewed model-card endpoint availability. Validate declared units and
preserve provenance and original verification times. Do not use Anthropic
direct-API prices or derive regional/tier/cache multipliers.

The initial audit covers 18 current Claude model cards. Unsupported endpoint
intersections and unavailable prices remain explicitly estimated. Published
batch prices do not authorize an online request to use the batch tier. Reserved
capacity prices per hour/TPM are outside token accounting. The latency-optimized
widget's unit/value ambiguity is excluded pending a separately verified source.
Older models without a current reviewed card keep their compatibility policy.

For example, the current global Haiku 4.5 prices are $1 input and $5 output per
million tokens, $1.25 five-minute writes, $2 one-hour writes and $0.10 reads.
The $0.80/$4 pair in #4978 belongs to Haiku 3.5. Fable/Mythos pricing also shows
why a universal cache-read multiplier is invalid.

## Shared policy and compatibility

Add an optional one-hour full cache-write rate alongside the existing default
write rate. For Claude, the default rate is five-minute retention; OpenAI's
existing interpretation remains unchanged. Retain aggregate cache creation for
existing columns, while preserving the upstream five-minute/one-hour breakdown.
Claude input is additive: uncached input, cache reads and cache writes together
form the context length. Select context thresholds from the published model and
route rather than the OpenAI-wide threshold.

New Claude decisions use version 2 and embed the selected rates, raw usage,
provenance, routing evidence, exact Decimal cost and rounded ledger cost. Unknown
write duration or serving context must carry an estimate reason; it must not be
silently certified as the cheapest variant. Older OpenAI version-1 decisions
remain verifiable without consulting current rates. Historical Claude events
without a decision retain the pinned compatibility snapshot and legacy database
behavior. No historical replay or repricing is part of this change.

Adding optional fields must not change hashes of existing generations. Omit
new null fields from their canonical representation. Combined generations can
use policy version 2 while still carrying the unchanged OpenAI rate contract.
Keep snapshot `2026-09-12.1` and migration 045 immutable; publish a new snapshot
and append a separately frozen migration after 046. Upgrade seeding unions new
Claude coverage with the active rates, preserving newer validated OpenAI values.
Repeated initialization must not create duplicate publications or reset an
operator's pause/disable decision.

## Gateway and settlement

Capture the actual resolved forwarding model, endpoint region and raw provider
usage before response translation. A request-scoped capture must survive the
stream lifetime without leaking between requests. Compute one Claude decision
and share it between usage persistence and the S3 settlement event. Streaming
buffers and schemas must retain cache duration counters and the distinction
between missing evidence and measured zero.

Completed usage must still reach settlement if response translation fails or
the client disconnects. Preserve the request's pricing task through cancellation
and emit its completed decision once. Compute pricing before optional account
enrichment, reservation reconciliation and usage persistence; bound that later
work to ten seconds so a database lock cannot indefinitely withhold the S3 event.
Missing final usage must not produce a fabricated zero-cost success.

The tracker reuses the embedded decision, so a daily publication between request
completion and settlement cannot change the charge. Before the new migration is
active, a new gateway must use its model-specific bundled Claude rates with
explicit bootstrap provenance, not a generic fallback price. Other providers
keep their existing behavior.

## Deployment and verification

The existing release workflow pauses refresh, updates both Lambda archives,
rolls out the gateway, migrates and verifies all bundled provider/model keys,
then invokes a real complete refresh before enabling the schedule. Verify the
active content hash and required one-hour rates, as well as archive parity and
existing delivery configuration. Normal node replacement may be waited on for
a bounded interval; wrong images and incomplete rollouts must never pass.

Validate source fixtures and hand-calculated cache/context examples, old
version-1 decisions and generation hashes, real PostgreSQL fresh/upgrade/repeated
migrations, partial-source retention, streaming/nonstream request isolation and
actual archive imports. After CI/CD succeeds, send controlled Claude traffic,
compare response counters against the selected AWS prices and persisted Decimal
cost, correlate the exact S3 event and successful tracker invocation, and verify
Budget & Spend periods. Audit S3/CloudWatch reads use existing CI permissions;
the gateway service role does not need archive-read permissions.
