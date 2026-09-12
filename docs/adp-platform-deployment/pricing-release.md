# Pricing release and deployment verification

Pricing uses the AWS Bedrock source inventory, an immutable migration seed, and
versioned database generations. A fresh `alembic upgrade head` installs usable
pricing automatically; it does not need a manually edited pricing table. The
same shared policy and snapshot ship in the gateway and both budget Lambdas.
Daily refresh subsequently publishes freshly validated AWS rates at 06:00 UTC.
Retained rows keep their original verification dates, and failed refreshes keep
the last validated generation.

## Deployment ordering

`deploy-all.sh` pauses the old refresh schedule before gateway infrastructure
changes, waits the old Lambda's configured timeout plus five seconds, and builds
a gateway image tagged with the full source commit SHA. After all requested
replicas are Ready on that image, it runs migrations on a matching pod and
verifies the enabled generation and required key coverage on each replica.
Finally it verifies both Lambda archives against the checked-out release,
checks retry/failure destinations and notification subscriptions, invokes a real
refresh, matches the returned generation to the database, and enables the daily
schedule. Errors after quiescence leave the schedule disabled.

When chat logging is explicitly disabled in the gateway deployment, migrations
and seed verification still run. Budget Lambda creation and scheduled refresh
are optional with that configuration, so finalization reports this condition
and does not require missing Lambda resources. An enabled gateway with missing
pricing infrastructure fails finalization.

The GitHub gateway workflow uses the same commands and updates the refresh
Lambda before the tracker. It always migrates after a backend rollout and has a
required pricing-finalization job. A generic HTTP smoke test cannot replace this
check. A pricing policy, shared Lambda helper, archive builder, migration
workflow or finalization workflow change triggers the relevant build/deployment.
Gateway deployment, gateway infrastructure apply and standalone finalization
share an environment lock. Different accounts using the same environment name
also serialize, so an empty platform-account input cannot evade that lock.

## First corrective release into an existing deployment

The gateway workflow does **not** authorize or automatically apply a broad
infrastructure plan. Review the plan for the same release, including replacement
actions. Existing unrelated RDS, authentication or networking drift is not part
of pricing delivery. If the first release's finalization finds missing retry
queues, timeout or alarms, its gateway code/schema may already be deployed, but
the workflow fails with the schedule disabled. Complete the reviewed pricing
infrastructure apply, then resume the finalization phase:

```bash
gh workflow run pricing-finalize.yml --repo aws-e/adp \
  --field release_sha="$PRICING_RELEASE_SHA" \
  --field account_id="$PRICING_ACCOUNT_ID" \
  --field environment="$PRICING_ENVIRONMENT"
```

`release_sha` must be the full SHA of the deployed gateway image. The workflow
checks out that exact commit, verifies the matching image and normalizes Lambda
ZIP entries before comparing source bytes. Different archive timestamps or
compression do not hide different deployed source. Inspect the resulting run;
a successful infrastructure apply alone is not completed activation.

The equivalent CLI commands, using the already confirmed target credentials,
are:

```bash
python3 modules/gateway/scripts/pricing-rollout.py quiesce \
  --account-id "$PRICING_ACCOUNT_ID" --environment "$PRICING_ENVIRONMENT"
# Deploy the reviewed Lambda/backend/infra artifacts while refresh is paused.
python3 modules/gateway/scripts/pricing-rollout.py migrate \
  --account-id "$PRICING_ACCOUNT_ID" --environment "$PRICING_ENVIRONMENT" \
  --expected-image "$PRICING_RELEASE_IMAGE"
python3 modules/gateway/scripts/pricing-rollout.py finalize \
  --account-id "$PRICING_ACCOUNT_ID" --environment "$PRICING_ENVIRONMENT" \
  --expected-image "$PRICING_RELEASE_IMAGE"
```

All phases verify the AWS account before mutations. A failed AWS permission check
is not treated as a missing bootstrap resource. The immediate Lambda invocation
must report a full `published` result; deferred/paused responses and function
errors cannot pass because a concurrent writer happened to advance the pointer.
An explicitly paused database pointer is preserved for operator rollback.

## Operational evidence

The default notification route is an encrypted pricing SNS topic with an active
SQS operational-inbox subscription. Supplied topics must have confirmed
subscriptions. Finalization checks the configured notification routes and queue
retention/encryption; it does not claim that a configured subscription proves
end-to-end delivery. See
[budget Lambda operations](../../modules/gateway/infra/modules/budget-lambda/PRICING_OPERATIONS.md)
for the exact metric contract, failure queues and default inbox outputs.

For release acceptance, additionally record an isolated CloudWatch-alarm delivery
through SNS to the inbox, separate EventBridge delivery and Lambda execution
failure evidence, actual refresh/source freshness, and a new controlled Codex
request whose durable decision matches the settled Budget & Spend charge.
Verify healthy consumers adopt the active generation within 15 minutes. Do not
rewrite historical cost records or replay old usage to validate a new release.

Rollback pauses refresh and selects an already validated generation using a new
pointer revision. Do not downgrade the schema or decrement a generation/revision
counter. Revert a gateway producer before reverting its decision-aware tracker,
so outstanding settlement events remain readable.
