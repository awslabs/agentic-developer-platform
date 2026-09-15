# Pricing infrastructure and emitter contract

The daily EventBridge rule uses `cron(0 6 * * ? *)`. New rules start **disabled**;
Terraform ignores subsequent rule-state changes. The release workflow must pause
an existing rule, drain the old runtime, deploy matching code and schema, verify
the seed and notification delivery, invoke a real refresh, and explicitly enable
the rule. An infra apply alone is not completed pricing activation. The refresh
timeout defaults to 180 seconds; its source-fetch deadline is 120 seconds.

Delivery to Lambda and execution in Lambda each have two retries and a maximum
event age of 3,600 seconds. EventBridge delivery failures go to
`<prefix>-pricing-delivery-failure`. Lambda asynchronous execution failures go to
`<prefix>-pricing-execution-failure`. These separate SQS queues retain records for
14 days and use SQS-managed encryption. The Lambda execution role can send only
to its execution-failure queue. EventBridge's queue permission is scoped to the
rule ARN and source account. Existing S3 tracker permissions are preserved.

Supplied `budget_alarm_sns_topic_arns` receive pricing alarms. When that list is
empty, the module creates `<prefix>-pricing-alarms` and an automatically subscribed
`<prefix>-pricing-alarm-inbox` SQS queue. This provides machine delivery and an
inspectable operational inbox. It does not page a human. The SNS topic uses a
dedicated rotating KMS key with CloudWatch encryption permission constrained to
the topic's encryption context. Topic and queue publication permissions bind
their service principals to the source deployment/topic. The inbox uses
SQS-managed encryption; it is separate from both failure queues.

Root output `pricing_refresh_operations` exposes function/tracker names, rule
name/ARN, failure queue ARN/URLs, alarm topics, and (for the default route) inbox
URL/ARN, subscription ARN and encryption key ARN. For example, after reading the
reviewed output into `PRICING_INBOX_URL`, inspect messages without deleting them:

```bash
aws sqs receive-message --queue-url "$PRICING_INBOX_URL" \
  --visibility-timeout 0 --max-number-of-messages 10 --wait-time-seconds 1
```

Verify a live CloudWatch alarm can reach the SNS topic and subscribed inbox.
A direct SNS publish only proves the SNS-to-SQS portion. Use an isolated test
alarm with the deployment's alarm-name prefix and the same notification topic;
do not put the live production alarm into ALARM to manufacture evidence. Verify
EventBridge delivery failure and Lambda execution failure separately, without
breaking the live refresh target or writing invalid pricing generations.

## Refresh metrics

All custom metrics use `ADP/Gateway`. The refresh emits these metrics with exactly
`FunctionName=<actual pricing-refresh function name>`:

| Metric | Meaning and alarm |
|---|---|
| `PricingRequiredVariantsMissing` | Required variant count absent; alarm above zero. |
| `PricingVariantsRetained` | Prior rows retained without renewing verification; alarm above zero. |
| `PricingRefreshPartial` | Complete mixed generation published after a source failure; alarm above zero. |
| `PricingRefreshRejected` | Publication rejected; alarm above zero. |
| `PricingRefreshDeferred` | Schema/seed/activation unavailable or paused; alarm above zero. |
| `PricingOldestVerifiedAgeHours` | Maximum age across required rows; alarm above 48 hours or missing daily measurement. |
| `PricingRefreshSuccess` | Emit 1 only for fully fresh publication. Missing hourly buckets are filled with zero; 30 consecutive hours without success alarm. |

`PricingSourceVerifiedAgeHours` adds `ModelId` and `Source` dimensions, with
`Source=model_card` for eight frontier models or `bulk_catalog` for four GPT-OSS
models. Each value is the maximum verification age among the required rows for
that model/source. Twelve source alarms are generated from the immutable
deployment snapshot's model manifest, with a 48-hour threshold and missing daily
measurement treated as breaching. A future manifest version must update that
explicit baseline in `pricing-alarms.tf` as part of its reviewed rollout.

Event-only anomaly metrics use `notBreaching` for missing data. Daily freshness
and full-refresh heartbeats use `breaching`. Standard AWS alarms independently
cover EventBridge failed invocations/DLQ delivery failures, Lambda Errors,
AsyncEventsDropped and DestinationDeliveryFailures, and each failure queue's
visible message count and oldest message age.

## Consumer metrics

The existing `UnknownModelPricing` metric remains dimensionless, with missing
data treated as not breaching. Consumer operational alarms provisioned by this
module use dimensionless `PricingCacheRefreshFailure`, `PricingUnknownVariant`,
`PricingStaleRate`, and `PricingCacheAgeSeconds` (threshold 1,800 seconds). The
release reviewer must match these names and dimensions to the integrated gateway
and tracker emitters before claiming consumer observability complete. A quiet
consumer has no request-driven metrics, so missing data is not an outage signal.

## Local verification without AWS calls

From `modules/gateway`, run:

```bash
python tests/infra/verify_pricing_infra.py
```

Terraform 1.14+ and the AWS/archive providers are required. An optional
`--plugin-dir PATH` uses an existing local provider mirror. The verifier stages
the actual module, tests both default and supplied notification destinations
with Terraform's mock AWS provider, and builds both real Lambda ZIPs through the
archive provider. It compares every archive member name/content against the
per-function Python sources, shared Python sources, recursive policy Python
sources and snapshot JSON. AWS credentials are removed from this test process;
the test does not contact AWS or deploy infrastructure. Live notification and
failure-route delivery remain release verification requirements.
