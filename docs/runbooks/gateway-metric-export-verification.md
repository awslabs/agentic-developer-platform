# Gateway metric delivery: scoped verification and rollback

Use this procedure for issue #7035 (SCN-01 / AC-01) **after** a named operator
selects and authorizes an existing deployment and live-validation owner. A
published PR, a successful local EMF write or an IAM simulation is not proof of
CloudWatch delivery. Do not run probes against an unverified account or create a
validation service. Keep all target details and raw evidence in an access-controlled
operator store, never in this repository, PR comments or public logs. The names
`ADP/Gateway` and `ADP/UnrelatedExample` below illustrate an allowed and unrelated
namespace; neither selects a deployment.

## Scoped EMF collector repair (#8337)

This is the log/EMF collector path, **not** the direct `PutMetricData`
permission procedure below. The gateway emits root-level `_aws` in pod stdout,
but the default Kubernetes log parser delivers it under `log_processed`.
[`gateway-emf.conf`](../../platform/infra/modules/eks/gateway-emf.conf) routes
only parsed `BedrockGateway` events from the gateway container to the existing
application log group using their original `log` value and a per-source
log stream. A Kubernetes tail tag looks like
`application.var.log.containers.bedrockgateway-proof_adp-gateway_bedrockgateway-012345.log`:
the fields are separated by **underscores**, not hyphens. The route also bounds
its emitter memory, filesystem backlog and CloudWatch output retries. The
default route keeps other logs in full. Terraform preserves addon configuration during ordinary
upgrades; the separate [guarded addon command](../../platform/scripts/gateway_emf_addon.py)
is the **only** step here that can update the addon. Deploying a gateway image or
merging the PR does not install this collector change. Do not provision another
collector, test service or logging destination, broaden IAM, or apply the whole
platform for this repair.

The delivery coordinator selects the **approved** target and update window; the
human owner accepts the private live evidence. Before updating, privately pin
the reviewed Git revision and collector-file hash, existing addon version and
entire configuration, account, region, cluster, actual collector log-delivery
identity, operator role, and rollback owner. Verify that the installed addon
supports `containerLogs.fluentBit.config.extraFiles`; inspect its current
Fluent Bit ConfigMap and confirm `application-log.conf` (including the
Kubernetes `log_processed` parser) is included before `gateway-emf.conf`.
Identify the actual Fluent Bit writer rather than assuming the addon service
account role is its credential source. A denied `PutMetricData` call is **not**
established by missing EMF datapoints. Stop on an unverified target, unexpected
addon version, existing `gateway-emf.conf`, conflicting configuration or
unattributed writer. Check for an already-running matching rollout before
starting another.

These commands run in **one operator-owned private terminal** at the reviewed
repository revision, with `adp-cred`, AWS CLI, Python 3 and `jq` installed. Set
`PRIVATE_DIR` to an access-controlled directory **outside the repository**;
set `OPERATOR_LABEL`, `EXPECTED_ACCOUNT`, `EXPECTED_ASSUMED_ROLE`, `REGION`
and `CLUSTER` from the approved private target binding. The expected role is
the STS assumed-role ARN **without** its final `/session` segment, not the
collector's role. These are placeholders, not permission to operate on an
unverified account. Do not paste commands with unset values or publish the
resulting snapshots, receipts, pod logs or operator identities.

```sh
umask 077
mkdir -m 700 -p -- "$PRIVATE_DIR"
git rev-parse HEAD > "$PRIVATE_DIR/source-revision.txt"
sha256sum platform/infra/modules/eks/gateway-emf.conf > "$PRIVATE_DIR/collector-hash.txt"
adp-cred assume --service aws --label "$OPERATOR_LABEL" --exec aws sts get-caller-identity > "$PRIVATE_DIR/operator-identity.json"
adp-cred assume --service aws --label "$OPERATOR_LABEL" --exec aws eks describe-addon \
  --region "$REGION" --cluster-name "$CLUSTER" --addon-name amazon-cloudwatch-observability \
  > "$PRIVATE_DIR/baseline.json"
```

Privately compare **both** the account and exact assumed operator role with the
binding before the next step. Confirm the baseline version, active status,
configuration and writer identity against the approved target; save the
before-state and evidence destination privately. `prepare` does not contact
AWS. It refuses stale, unsupported and conflicting configuration and retains
unrelated annotations and extra collector files.

```sh
python3 platform/scripts/gateway_emf_addon.py prepare \
  --baseline "$PRIVATE_DIR/baseline.json" --before "$PRIVATE_DIR/baseline.json" \
  --output "$PRIVATE_DIR/candidate.json"
```

Review the private candidate against the before-state: only the
`containerLogs.fluentBit.config.extraFiles.gateway-emf.conf` member should be
added. Before the following separately authorized step, the coordinator must
establish **exclusive mutation ownership of this addon** through the target's
approved deployment/change-control procedure: observe or stop any matching
automatic update, confirm no other addon writer is active, and hold that
ownership through readback or rollback. A Terraform state lock alone does not
cover this direct EKS API call. If the coordinator cannot establish exclusive
ownership, do not submit the update. Record the owner and window privately.
`apply` re-verifies the
selected role and reads the actual addon twice before calling `UpdateAddon`
with `resolveConflicts=NONE`; no ambient AWS credentials are used. A stale
baseline creates **no** receipt and sends **no** update. Immediately before
submission it fsyncs a private, exclusive-create intent containing the reviewed
source, target, baseline fingerprint and stable client request token. It never
retries an unknown outcome. Reserve a fresh receipt filename in a private
mode-0700 directory for each approved attempt. Do not reuse a receipt name.

```sh
python3 platform/scripts/gateway_emf_addon.py apply \
  --baseline "$PRIVATE_DIR/baseline.json" --aws-label "$OPERATOR_LABEL" \
  --expected-account "$EXPECTED_ACCOUNT" --expected-assumed-role "$EXPECTED_ASSUMED_ROLE" \
  --region "$REGION" --cluster "$CLUSTER" --receipt "$PRIVATE_DIR/apply-receipt.json"
```

A receipt starts as an **intent**, not proof that the update was submitted.
`state=submitted` contains an update ID at `.result.updateId`; `state=unknown`
or `state=intent` after interruption requires reconciliation, not resubmission.
Even a matching configuration alone is not proof that the specific update
succeeded. Privately record the ID when present, then use the same selected
connection for `eks describe-update`
and `eks describe-addon`. Poll at most six times, 30 seconds apart (three
minutes maximum). Require the specific update to report `Successful`, the
addon to be `ACTIVE`, and the readback JSON to equal the reviewed candidate;
verify the installed ConfigMap and route after the update. Save the update,
readback, collector identity and log-stream evidence in the private directory.
For example, one bounded poll/readback is:

```sh
adp-cred assume --service aws --label "$OPERATOR_LABEL" --exec aws eks describe-update \
  --region "$REGION" --cluster-name "$CLUSTER" --name amazon-cloudwatch-observability \
  --update-id "$(jq -er '.result.updateId' "$PRIVATE_DIR/apply-receipt.json")" \
  > "$PRIVATE_DIR/update-check.json"
adp-cred assume --service aws --label "$OPERATOR_LABEL" --exec aws eks describe-addon \
  --region "$REGION" --cluster-name "$CLUSTER" --addon-name amazon-cloudwatch-observability \
  > "$PRIVATE_DIR/after.json"
jq -e --slurpfile expected "$PRIVATE_DIR/candidate.json" \
  '(.addon.configurationValues | fromjson) == $expected[0]' "$PRIVATE_DIR/after.json" >/dev/null
```

Do not call an empty or failed query success. If the acknowledgement is lost
or the process stops after writing intent, use `recover` at the **same pinned
source revision**, with the **same** private baseline, target, role and receipt:

```sh
python3 platform/scripts/gateway_emf_addon.py recover \
  --baseline "$PRIVATE_DIR/baseline.json" --aws-label "$OPERATOR_LABEL" \
  --expected-account "$EXPECTED_ACCOUNT" --expected-assumed-role "$EXPECTED_ASSUMED_ROLE" \
  --region "$REGION" --cluster "$CLUSTER" --receipt "$PRIVATE_DIR/apply-receipt.json" \
  --expected-action apply
```

For a rollback receipt, use its separate path and `--expected-action revert`.
Recovery reads the selected identity, specific update status when an ID was
recorded, and current addon; it writes observations into the protected receipt
but **never** submits an update. A missing update ID leaves its status unknown:
coordinate independent investigation of the actual addon and updates before
any further operation. If the receipt itself is missing, stop and investigate
privately; never resend the request blindly. An update can race an
unrelated edit between its final read and the EKS API mutation (there is no
atomic compare-and-swap). `resolveConflicts=NONE` and two reads do **not**
provide an atomic stale-writer fence; the exclusive coordinator-owned window
is required, and this residual race still needs readback. On an unexpected
readback, conflict or concurrent change, stop, preserve evidence, and seek a
fresh approved baseline rather
than overwriting newer configuration.

For live extraction, use the bounded private observation below with
`NAMESPACE=BedrockGateway` and `METRIC_NAME=ExporterEmitAttempt`: record a
pre-event baseline, one approved **non-model** producer event and its controlled
UTC timestamp, then query a narrow new window. Inspect the selected pod's
stdout, the real collector log stream and ordinary/unrelated log envelopes to
establish both metric arrival and intact log delivery. A passing local
serializer test is not live extraction. Keep missing datapoints or damaged
logs open for the coordinator and human to resolve.

For SCN-03, the delivery coordinator must bind the controlled event to the
**same reviewed source and addon update** before interpreting datapoints.
Privately compare `git rev-parse HEAD` and the gateway file SHA-256 with the
pinned `source-revision.txt` and `collector-hash.txt`, and record the installed
addon version, successful specific update ID, active configuration and actual
collector writer identity. Record the UTC timestamp immediately before and
after exactly one approved non-model producer event, its selected pod and
source stream, and the precise UTC baseline and post-event query windows.
Poll the dimensionless `BedrockGateway/ExporterEmitAttempt` query at most six
times, 30 seconds apart. In the private receipt, store the query arguments,
observation timestamps, raw metric and log results, expected namespace and
value, and whether the new datapoint appears **after** the controlled event
rather than in the baseline. Preserve ordinary and unrelated log envelopes
from the same observation period and the actual collector identity. A matching
metric without attributable log evidence, an empty query, or a failed query is
not live acceptance; stop on a mismatched source, configuration or writer.

After the bounded observation, stop the controlled producer. Record a protected
cleanup receipt with operator, UTC completion time, event window, temporary
artifact disposition, final addon version/configuration and log-delivery
readback, and any rollback update ID and status. If the authorized owner
requires rollback, use the fresh-snapshot `rollback`/`revert` procedure below,
verify only the owned routing delta was removed, and retain required audit and
accounting logs; otherwise explicitly record that the approved routing remains
installed. Record incomplete cleanup or unavailable readback as open work,
never as successful acceptance. Keep the entire receipt and evidence location
private for the coordinator and human reviewer.

For an approved rollback, first stop probe traffic and take a **fresh** private
addon snapshot. `rollback` prepares a candidate that removes only the still-
owned gateway file; it refuses changed or originally owned routing and retains
newer unrelated settings. Inspect the diff and obtain the rollback owner's
approval before `revert`, which rechecks the live state and identity. Use a
new receipt name; then apply the same bounded status, readback, ConfigMap and
log-delivery checks. On interrupted rollback, use read-only `recover` for its
receipt and `--expected-action revert` before any further mutation. Never
restore the whole original configuration over later
changes or erase required audit/accounting logs.

```sh
adp-cred assume --service aws --label "$OPERATOR_LABEL" --exec aws eks describe-addon \
  --region "$REGION" --cluster-name "$CLUSTER" --addon-name amazon-cloudwatch-observability \
  > "$PRIVATE_DIR/rollback-before.json"
python3 platform/scripts/gateway_emf_addon.py rollback \
  --baseline "$PRIVATE_DIR/baseline.json" --before "$PRIVATE_DIR/rollback-before.json" \
  --output "$PRIVATE_DIR/rollback-candidate.json"
python3 platform/scripts/gateway_emf_addon.py revert \
  --baseline "$PRIVATE_DIR/baseline.json" --aws-label "$OPERATOR_LABEL" \
  --expected-account "$EXPECTED_ACCOUNT" --expected-assumed-role "$EXPECTED_ASSUMED_ROLE" \
  --region "$REGION" --cluster "$CLUSTER" --receipt "$PRIVATE_DIR/revert-receipt.json"
```

Hand the coordinator a protected receipt of before/after and actual writer
identities, reviewed source and addon version, update/rollback IDs, controlled
event window, bounded metric and log results, any error classification, and
cleanup. Publish only a sanitized result; the human accepts live AC-04/SCN-03
after the private evidence is checked. Filing, PR publication and merge do not
authorize deployment or close those live rows.

## Reproduce the local collector serialization check (SCN-01)

This checks the **serializer**, not a deployed addon or live metric extraction.
On a Linux x86-64 host with Bash, Python 3, OpenSSL, Docker and a local Docker daemon,
run from the repository root. The reference is the reviewed collector image
`public.ecr.aws/aws-observability/aws-for-fluent-bit:3.4.16`, pinned by digest;
the test independently checks the extracted executable's SHA-256 before sending
synthetic events. The image pull is the only remote prerequisite. No AWS
connection, cluster, model inference or new validation service is required.

```sh
set -euo pipefail
image='public.ecr.aws/aws-observability/aws-for-fluent-bit@sha256:7bece8651fc81b1bd7f904f7b71a3542b1abc8ba94dc989b4a326b576ab8f6bc'
root_dir="$(mktemp -d)/root"
mkdir "$root_dir"
docker pull --platform linux/amd64 "$image"
container_id="$(docker create --platform linux/amd64 "$image")"
docker export "$container_id" | tar -xf - -C "$root_dir"
docker rm "$container_id" >/dev/null
printf '%s  %s\n' '4de1d009e42d84922f0b0531d642ee3639d50aaeff010f033b370a54f8a2b4c8' \
  "$root_dir/fluent-bit/bin/fluent-bit" | sha256sum -c -
FLUENT_BIT_ROOT="$root_dir" python3 -m unittest discover \
  -s platform/infra/modules/eks/tests -p 'test_gateway_emf_collector.py' -v
```

Expected: three tests pass. Fixed real-form Kubernetes tail tags and gateway
EMF payloads deliver exactly one root `_aws` with its referenced metric value
per input and distinct pod streams. An ordinary gateway log, unrelated metric
namespace, unrelated container, other Kubernetes namespace, sidecar and
lookalike pod each retain the complete original envelope exactly once. The
harness starts a loopback TLS fake CloudWatch Logs API, trusts its disposable
certificate only for the local process, supplies dummy credentials, disables
instance metadata lookup and uses the bounded collector retry setting. It
stops the collector after a short deadline; no synthetic addon update is made.
Remove the disposable extracted `root_dir` after checking the results.

## Identify the writer before changing permissions

1. Privately record the approved target, revision, region, producer, collector
   configuration, actual writer identity, intended namespace and metric name,
   permissions boundary/SCP context, rollback revision and protected evidence
   destination. Confirm the deployed revision before beginning. Obtain an
   operator connection with read and simulation permissions, and a **separate**
   approved connection to the actual producer identity if a direct API denial
   test is required. Do not substitute a similarly named role or ambient AWS
   credentials. If either identity or the collector path is unknown, stop the
   IAM/probe step and collect private attribution first.
2. For gateway EMF, [`metrics.py`](../../modules/gateway/src/shared/metrics.py)
   writes `ExporterEmitAttempt` without dimensions in the `BedrockGateway`
   namespace to pod stdout. The [platform observability addon](../../platform/infra/modules/eks/main.tf)
   configures CloudWatch agent and Fluent Bit with a collector role and an AWS
   managed policy. Trace the selected pod, its log stream, extraction/collector
   configuration and the *actual* collector identity. Do not grant
   `cloudwatch:PutMetricData` to the gateway pod merely because a metric is
   missing; EMF extraction may be a log delivery problem.
3. Direct CloudWatch API writers are different: the
   [pricing fallback](../../modules/gateway/lambda/shared/pricing_fallback.py)
   sends `UnknownModelPricing` to `ADP/Gateway`, while
   [other gateway paths](../../modules/gateway/src/admin/cognito_claims.py)
   can publish their own namespaces. Verify the invocation and deployed role;
   the [pricing](../../modules/gateway/infra/modules/budget-lambda/iam.tf) and
   [tick](../../modules/gateway/infra/modules/orchestration-tick/iam.tf) policies
   show the source precedent: only `cloudwatch:PutMetricData`, `Resource = "*"`
   (this action has no metric resource ARN), and `StringEquals` on
   `cloudwatch:namespace`. They are not evidence that another role has access.

## Run one bounded, private observation

The following commands run on the **operator's private terminal**, with `aws`
and `adp-cred` available. Supply `READ_AWS_LABEL`, `REGION`, `NAMESPACE`,
`METRIC_NAME`, `WINDOW_START` and `WINDOW_END` from private configuration;
use UTC ISO-8601 times spanning the approved probe, a 60-second period and a
window narrow enough to exclude unrelated runs. For EMF use the dimensionless
`ExporterEmitAttempt`; for a direct writer use its actual name and dimensions.
The command below queries a dimensionless metric; if the selected writer adds
dimensions, include its matching `--dimensions` from private configuration.
Do not copy command output into a public report. Verify the returned account
and **exact** assumed role against the approved target before the next command.

```sh
adp-cred assume --service aws --label "$READ_AWS_LABEL" --exec aws sts get-caller-identity
adp-cred assume --service aws --label "$READ_AWS_LABEL" --exec aws cloudwatch get-metric-statistics \
  --region "$REGION" --namespace "$NAMESPACE" --metric-name "$METRIC_NAME" \
  --start-time "$WINDOW_START" --end-time "$WINDOW_END" --period 60 --statistics Sum
```

Record a baseline first. Trigger **one** approved controlled request or writer
event in the verified deployment, privately record its UTC start/end and
revision, and compare datapoints in the new window with baseline rather than
accepting historical data. Poll the same query at most six times, 30 seconds
apart (at most three minutes); no new datapoint, unexpected namespace,
collector errors or a denied write is a failed/incomplete check, not an empty
success. A CloudWatch datapoint proves arrival, not that every event was
delivered. For gateway EMF, use the platform-admin-only
`/admin/diagnostics/export-health` route to compare process-local attempts,
failures and last-success time for the selected process, if reachable. A local
success without a datapoint calls for private log-stream, EMF extraction and
collector investigation; don't assume an IAM denial. Never publish raw pod
logs, tokens, request or tenant identifiers, SQL or diagnostic connection URLs.

## Verify least access on the actual direct writer

Only when tracing shows `PutMetricData` is the denied action: compare the
deployed policy on the **actual** producer role to its intended namespace;
check boundary, session policy and organization guardrails too. A candidate
change must constrain `cloudwatch:PutMetricData` with
`cloudwatch:namespace = NAMESPACE` and must not add `cloudwatch:*`, wildcard
services or a grant to unrelated roles. An operator reviews and applies only
that scoped change through the authorized manual infrastructure workflow.
Do not use this direct-API procedure to assert namespace isolation of the
log/EMF collector unless its actual delivery action supports that condition.

With `PRODUCER_ROLE_ARN` and `UNRELATED_NAMESPACE` from private configuration,
the operator's read connection can first *screen* both decisions. Set
`UNRELATED_NAMESPACE` to an approved fictional negative-test value distinct
from the deployed allowlist. IAM simulation is not the final effective-policy
oracle; record missing context, boundaries and organization restrictions.

```sh
for namespace in "$NAMESPACE" "$UNRELATED_NAMESPACE"; do
  adp-cred assume --service aws --label "$READ_AWS_LABEL" --exec aws iam simulate-principal-policy \
    --policy-source-arn "$PRODUCER_ROLE_ARN" --action-names cloudwatch:PutMetricData \
    --resource-arns '*' \
    --context-entries "ContextKeyName=cloudwatch:namespace,ContextKeyValues=$namespace,ContextKeyType=string"
done
```

Under a separately authorized connection to the **actual producer role**, first
set `PRODUCER_AWS_LABEL` from the approved private connection; no operator/read
identity is interchangeable with this label. Then
verify `sts get-caller-identity` and compare the account and exact role again.
Then trigger the approved real producer event and confirm allowed-namespace
arrival as above. The negative probe below must return `AccessDenied`; an
unexpected success, missing permission to assume the producer role, ambiguous
session identity or other error **blocks** SCN-01 rather than proving a denial.
This probe intentionally carries a value only if the permission boundary is
broken; obtain explicit approval for this negative test and keep its output
private. Never run it using the read/operator connection instead of the writer.

```sh
adp-cred assume --service aws --label "$PRODUCER_AWS_LABEL" --exec aws sts get-caller-identity
adp-cred assume --service aws --label "$PRODUCER_AWS_LABEL" --exec aws cloudwatch put-metric-data \
  --region "$REGION" --namespace "$UNRELATED_NAMESPACE" \
  --metric-data 'MetricName=NamespaceDenialProbe,Value=1,Unit=Count'
```

For an EMF/log path without a namespace-conditioned writer action, document
the actual collector delivery actions and effective permissions privately;
do **not** claim the direct-API negative probe tested EMF namespace isolation.
If an unrelated namespace cannot be denied by the actual path, leave SCN-01
open for an owner-approved collector guard or revised acceptance procedure.

## Revert and hand off

After the bounded probe, stop test traffic, restore quotas, remove temporary
test identities/credentials and revert only this change's optional collector,
diagnostic or namespace-policy configuration using the recorded rollback
revision. Do not disable required accounting, audit, reservation holds or
recovery records to silence exporter warnings. Privately verify the restored
role/configuration, export-health trend and gateway revision; record cleanup
and any still-missing datapoints. Hand the live owner a protected report of
producer → collector/API → actual writer identity, allowed and denied results,
baseline and probe windows, policy/simulator and effective-role evidence,
failure classification and rollback receipt. Publish only the sanitized
outcome and outstanding checks. An unrun or failed live check remains open.
