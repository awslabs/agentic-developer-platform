# Engine-command signing-key rotation

This procedure rotates the HMAC keyring that authenticates `@agent-engine`
command attribution (issue #4539), with a bounded overlap window so commands
that arrived before the rotation are still consumable afterwards.

Follow the [deployment guide](../adp-platform-deployment/deploy-with-agent.md)
for account/environment confirmation and approval of an actual change. The
implementation has not been deployed as part of the PR that introduced this
runbook, and nothing here authorizes enabling
`FEATURE_ORCHESTRATION_ENGINE_ENABLED`.

## What the key protects

An `@agent-engine` command does not travel as a call. The webhook Lambda marks a
row in the `webhook-events` table with `engine_command_status=pending` and the
gateway orchestration tick finds it on the sparse `engine-command-index` on its
next wake. The row therefore carries *who asked for what, on which plan*:
tenant, installation, repository, issue number, commenter id, author kind and
the command body.

Before #4539 those were ordinary mutable attributes. The key is what lets the
tick tell a tuple that was **delivered** by a signature-verified GitHub webhook
from one that was **authored** by anything else able to write the row. Its
confidentiality is the whole control: a party holding this key can mint a
command naming any tenant and any commenter, with a signature that verifies.

A valid signature is **not** an authorization. It establishes provenance only.
Tenant membership and the `PLAN_APPROVE` / human-only gate checks still run on
every command and are not affected by anything in this procedure.

## Who signs and who verifies

| Deploy unit | Role | Access | How it gets the ARN |
|---|---|---|---|
| `modules/agent-factory/webhook-ingress` (Lambda zip) | **Signer** — `lambda/common/command_signing.py` | read + decrypt | `ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN` on the function, set in `infra/lambdas.tf` |
| `modules/gateway` (container image) | **Verifier** — `src/orchestration/command_attribution.py` | read + decrypt | same env var, from `/adp/<env>/webhook-ingress/engine-command-signing-key-arn` |

No other deployed unit signs or verifies. In particular the agent worker cohort
(`agent_scaledjob`, and `agent_authority_worker` when enabled) must never hold
access; `infra/engine-command-signing.tf` names both in an explicit key-policy
`Deny`, and `tests/test_engine_command_signing_iam.py` fails if that changes.

The two implementations cannot import each other — one is a zip, the other an
image — so the canonical form is written twice and pinned by the shared golden
fixture at `contracts/engine-command-envelope/v1/`. **A rotation does not touch
canonicalization.** If a rotation appears to require a code change on one side
only, stop: that is a protocol change, which means a new `protocol_version` and
a new vector in the contract, not a key rotation.

## Key locations

Terraform (`modules/agent-factory/webhook-ingress/infra/engine-command-signing.tf`)
provisions the container and the grants. It never holds the value:

- Secret: `adp/<env>/webhook-ingress/engine-command-signing-key`
- CMK: `alias/adp-<env>-engine-command-signing` (dedicated, not the shared
  webhook-secrets key, and not the marker-signing key)
- ARN published at `/adp/<env>/webhook-ingress/engine-command-signing-key-arn`

The initial version is the placeholder `PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND`,
which **both** implementations refuse by name, and `ignore_changes = [secret_string]`
keeps the real value out of every later plan and state refresh.

Do not print the secret, paste it into an issue comment, pass it on a command
line, or write it to a file in the repo.

The rotation commands below therefore pipe the keyring into
`--secret-string file:///dev/stdin`, so the value never appears in an argument
vector (where `ps` and shell history can see it) and never lands on disk. Two
consequences to know before you run them:

- **`--debug` prints the value.** The CLI logs resolved parameters, so a
  `--debug` added to one of these commands while troubleshooting puts the key
  material in the terminal and in any captured log. Troubleshoot the
  read/transform side, never the `put-secret-value` itself.
- **Each pipeline ends with `set -o pipefail`** in the snippets. Without it, a
  failing transform (the id-reuse assert) still runs `aws` with empty stdin.
  That is refused — `Invalid length for parameter SecretString` — so it cannot
  blank the secret, but the pipeline would exit 0 and read as success.

## Keyring shape

```json
{
  "active_key_id": "2026-09",
  "keys": {
    "2026-09": "<random>",
    "2026-06": "<random>"
  },
  "previous_valid_until": "2026-09-22T00:00:00Z"
}
```

- `active_key_id` — the **only** key the signer will ever use.
- `keys` — every key the verifier may consider, by id.
- `previous_valid_until` — ISO-8601 with a `Z` or explicit offset. The verifier
  accepts a non-active key **only while this instant is in the future**.

Fail-closed properties you are relying on, all of them tested:

| Condition | Signer | Verifier |
|---|---|---|
| Secret absent / env var unset | refuses to sign | every command quarantined, `no_verification_key` |
| Value is a known placeholder | refuses to sign | treated as no key at all |
| Not JSON, or `active_key_id` has no material | refuses to sign | refuses |
| An individual key holds a placeholder | that id is dropped | that id is **unknown**, refuses |
| Row's `key_id` not in `keys` | n/a | `unknown_key_id` |
| Non-active `key_id`, no `previous_valid_until` | n/a | `stale_key_id` |
| Non-active `key_id`, window expired **or unparseable** | n/a | `stale_key_id` |

An unparseable window is treated as expired, not as unbounded: it is the
fail-closed reading of "we cannot tell whether this key is still valid". A typo
in the timestamp therefore refuses commands rather than extending the overlap
forever.

## Choosing the overlap window

The window must cover the longest time a row can sit `pending` between being
signed and being read. That is bounded by the tick interval plus any backlog,
not by the webhook latency. **48 hours is the default recommendation**; it
comfortably covers a tick outage over a weekend.

Two forces set the floor and the ceiling:

- **Too short** — rows signed just before the rotation refuse with
  `stale_key_id`. Those are real human commands, silently quarantined. They are
  recoverable only by re-commenting, and the operator has to notice first.
- **Too long** — the retired key stays valid for that whole period, so a
  compromise of the old material remains exploitable until it expires.

Both implementations cache the keyring per process (Lambda cold start, gateway
process), so a rotation reaches the fleet on the next cold start / rollout, not
instantly. Step 4 is what makes that bounded rather than unknown.

## Rotation

Throughout: `ENV` is the environment, `PROFILE` the AWS profile for its account.
Confirm the account before the first command.

```bash
export ENV=dev PROFILE=<profile>
export SECRET_ID="adp/${ENV}/webhook-ingress/engine-command-signing-key"
aws sts get-caller-identity --profile "$PROFILE"
```

### 1. Record the starting state

```bash
aws secretsmanager get-secret-value --profile "$PROFILE" --secret-id "$SECRET_ID" \
  --query SecretString --output text \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("active:", d["active_key_id"]); print("ids:", sorted(d["keys"])); print("previous_valid_until:", d.get("previous_valid_until"))'
```

This prints ids and the window, never material. Record the active id, every
known id and the current window.

Then the count of rows currently awaiting the tick, which is what the overlap
window has to cover:

```bash
aws dynamodb query --profile "$PROFILE" \
  --table-name "adp-${ENV}-webhook-events" \
  --index-name engine-command-index \
  --key-condition-expression 'engine_command_status = :s' \
  --expression-attribute-values '{":s":{"S":"pending"}}' \
  --select COUNT --query Count --output text
```

**A non-empty pending count is not a blocker** — the overlap window is what
covers them — but it is what you check again in step 6 before retiring the
outgoing key. Note this is a `Query` on the sparse index, not a table `Scan`:
only outstanding commands are projected, so the count is cheap and exact.

### 2. Generate the incoming key

New id convention: `YYYY-MM` of the rotation. It must not collide with any
existing id in `keys`; reusing an id silently changes what that id means and
turns already-signed rows into bad-signature refusals.

```bash
export NEW_ID="$(date -u +%Y-%m)"
export PREV_ID="<the active id from step 1>"
# 32 bytes from the system CSPRNG. Held in an env var for the length of this
# shell only; do not echo it, do not write it to a file, do not commit it.
export NEW_KEY="$(openssl rand -base64 32)"
```

If the current active id already equals `$NEW_ID` (a second rotation in the same
month), append a suffix — `2026-09b`. Do not reuse.

### 3. Publish both keys, still signing with the outgoing one

Fetch, merge, and put in one pipeline so the value is never in a file. The
merge is deliberate: a hand-written full document is how the *other* key gets
dropped, which retires it with no overlap at all.

```bash
set -o pipefail
export OVERLAP_UNTIL="$(date -u -d '+48 hours' +%Y-%m-%dT%H:%M:%SZ)"

aws secretsmanager get-secret-value --profile "$PROFILE" --secret-id "$SECRET_ID" \
    --query SecretString --output text \
  | NEW_ID="$NEW_ID" NEW_KEY="$NEW_KEY" ACTIVE_ID="$PREV_ID" OVERLAP_UNTIL="$OVERLAP_UNTIL" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
new_id = os.environ["NEW_ID"]
assert new_id not in d["keys"], f"key id {new_id} already exists — choose another, do not reuse"
d["keys"][new_id] = os.environ["NEW_KEY"]
d["active_key_id"] = os.environ["ACTIVE_ID"]   # unchanged: still signing with the outgoing key
d["previous_valid_until"] = os.environ["OVERLAP_UNTIL"]
json.dump(d, sys.stdout)
' \
  | aws secretsmanager put-secret-value --profile "$PROFILE" --secret-id "$SECRET_ID" \
      --secret-string file:///dev/stdin --query VersionId --output text
```

The `assert` is the guard against id reuse. If it fires, nothing was written.

Confirm with the step-1 read-back: both ids present, `active_key_id` still the
outgoing one, window set. Commands keep flowing throughout; the verifier already
accepts the incoming key, and nothing is signing with it yet.

### 4. Switch the signer

```bash
set -o pipefail
aws secretsmanager get-secret-value --profile "$PROFILE" --secret-id "$SECRET_ID" \
    --query SecretString --output text \
  | NEW_ID="$NEW_ID" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
new_id = os.environ["NEW_ID"]
assert new_id in d["keys"], "incoming key is not in the keyring — step 3 did not complete"
d["active_key_id"] = new_id
json.dump(d, sys.stdout)
' \
  | aws secretsmanager put-secret-value --profile "$PROFILE" --secret-id "$SECRET_ID" \
      --secret-string file:///dev/stdin --query VersionId --output text
```

Then force both sides to pick it up rather than waiting for natural cold starts,
so the overlap window is measured from a known instant:

```bash
# Signer: a configuration update replaces the execution environment.
aws lambda update-function-configuration --profile "$PROFILE" \
  --function-name "adp-${ENV}-github-webhook" \
  --description "engine-command key rotation to ${NEW_ID}" \
  --query LastUpdateStatus --output text

# Verifier: roll the gateway through the normal deployment path.
kubectl rollout restart deployment/bedrockgateway -n adp-gateway
kubectl rollout status  deployment/bedrockgateway -n adp-gateway --timeout=5m
```

The Lambda description is a harmless field chosen precisely because changing it
replaces the environment without altering behaviour. **Do not** set the key
material as a Lambda environment variable to achieve the same effect — that
puts it in the function configuration, where it is readable by anything with
`lambda:GetFunctionConfiguration` and visible in the console.

### 5. Verify positively and negatively

From an issue in a tenant repository, with commands enabled in that environment:

1. Comment a valid `@agent-engine` command as a human with `PLAN_APPROVE`.
   Confirm it is applied, and that the row's stored key id is `$NEW_ID`.
2. Confirm the refusal path still refuses: a command from an account without
   `PLAN_APPROVE` gets the uniform, tagless refusal — not an error, and not a
   quarantine.
3. Confirm nothing was quarantined by the rotation itself:

```bash
aws cloudwatch get-metric-statistics --profile "$PROFILE" \
  --namespace ADP/Orchestration --metric-name CommandsQuarantined \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum
```

`CommandsQuarantined` carries an `AttributionFailureReason` dimension of bounded
cardinality. Any `stale_key_id` or `unknown_key_id` during a rotation means the
overlap was too short or step 3's merge dropped a key — see Failure and
recovery. `no_verification_key` means the verifier cannot read the secret at
all, which is a grant problem, not a rotation problem.

### 6. Retire the outgoing key after the window

Only after `previous_valid_until` has passed **and** the pending count from
step 1 has drained:

```bash
set -o pipefail
aws secretsmanager get-secret-value --profile "$PROFILE" --secret-id "$SECRET_ID" \
    --query SecretString --output text \
  | PREV_ID="$PREV_ID" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
prev = os.environ["PREV_ID"]
assert d["active_key_id"] != prev, "refusing to remove the ACTIVE key — step 4 did not complete"
d["keys"].pop(prev, None)
d.pop("previous_valid_until", None)
json.dump(d, sys.stdout)
' \
  | aws secretsmanager put-secret-value --profile "$PROFILE" --secret-id "$SECRET_ID" \
      --secret-string file:///dev/stdin --query VersionId --output text
```

Removing `previous_valid_until` along with the key is deliberate: a stale window
with no non-active keys is harmless but misleading, and it is the thing someone
reads next time to decide whether an overlap is in progress.

After retirement the keyring holds exactly one id. A row signed under the
removed id now refuses with `unknown_key_id`, which is the intended end state.

## Failure and recovery

**Before step 4** — a failed check means abandon the rotation. Re-run step 6 to
remove the *incoming* key (substituting `PREV_ID=$NEW_ID`); nothing signed with
it, so nothing is lost.

**During overlap, after step 4** — roll the signer back by setting
`active_key_id` to the outgoing id (step 4's script with `NEW_ID=$PREV_ID`) and
replacing the execution environment again. Both keys are still published, so
rows signed under either are still verifiable. This is the reason the switch and
the retirement are separate steps.

**After retirement** — a rollback must first **republish** the required key
material and confirm the verifier has reloaded it, before switching the signer
back. Switching to a key the verifier does not hold quarantines every command
that follows. If the retired material is gone, it is gone: generate a fresh key
and rotate forward instead.

**Rows quarantined during the rotation** are not lost data, but they are not
retried either. Quarantine is terminal by design — it stops the endless reread
without applying an unverified command. The affected human commands must be
re-issued as new comments. A quarantined row deliberately carries no
repository, issue or installation fields, so there is no automated path to
answer it on the thread and there should not be one: that path would be a
GitHub write addressed with attacker-selectable routing.

**Secret deleted by accident** — the secret has a 7-day recovery window:

```bash
aws secretsmanager restore-secret --profile "$PROFILE" --secret-id "$SECRET_ID"
```

Until it is restored every command quarantines with `no_verification_key`. Fails
closed, but every command in the table is refused meanwhile.

## Initial seeding

First-time seeding is the same operation with no outgoing key. After
`terraform apply` has created the secret with its placeholder:

```bash
set -o pipefail
export NEW_ID="$(date -u +%Y-%m)"

# Piped, not command-substituted: `--secret-string "$(...)"` would expand the whole
# keyring into the argument vector, where `ps` and shell history can read it. Same
# reason every rotation step above pipes.
NEW_ID="$NEW_ID" NEW_KEY="$(openssl rand -base64 32)" python3 -c '
import json, os, sys
json.dump({"active_key_id": os.environ["NEW_ID"], "keys": {os.environ["NEW_ID"]: os.environ["NEW_KEY"]}}, sys.stdout)
' \
  | aws secretsmanager put-secret-value --profile "$PROFILE" --secret-id "$SECRET_ID" \
      --secret-string file:///dev/stdin --query VersionId --output text
```

No `previous_valid_until` on a first seed: there is no previous key, and a
window naming none would be noise.

Seeding belongs in the #4539 activation order, which this runbook does not
short-circuit:

1. Keep commands disabled (`FEATURE_ORCHESTRATION_ENGINE_ENABLED` unset).
2. Apply the Terraform; provision and seed the key.
3. Deploy the signer, then the verifier.
4. Reconcile any pre-signing `pending` rows — see
   [Pre-signing pending rows](#pre-signing-pending-rows) below.
5. Prove the worker cohort cannot read the key (below).
6. Exercise positive and negative cases (step 5 above).
7. Only then request release activation.

## Pre-signing pending rows

A row marked `engine_command_status=pending` before the signer shipped carries no
signature. On the first tick after the verifier ships, each one is refused with
`missing_signature` and quarantined. **That is the correct outcome and needs no
migration**: quarantine is terminal, so the rows stop being re-read, no decision
is appended, nothing is dispatched, and no acknowledgement is sent to
row-supplied routing.

What an operator does need is the **count, in advance**. Otherwise the first tick
after activation produces a quarantine spike that is indistinguishable from an
attack or a misconfigured key, and the natural reaction to that ambiguity —
assume the verifier is broken, roll it back — is the one action that reopens the
forgery.

There is deliberately **no reconcile script**. Rewriting these rows is the one
thing that must not happen: a script that "reconciles" a pre-signing row can only
do so by either signing it (minting authority for a tuple nothing verified — the
exact forgery, now performed by us) or marking it human-approved. Counting is
read-only, so it is a command, not a tool.

Run before enabling commands, against the deploy environment:

```bash
# Every pending row, and how many of them predate signing. Read-only: Select=COUNT
# returns no attribute values, so this cannot print a signature or a command body.
TABLE="adp-${ENVIRONMENT}-webhook-events"

aws dynamodb query \
  --profile "$PROFILE" \
  --table-name "$TABLE" \
  --index-name engine-command-index \
  --key-condition-expression 'engine_command_status = :s' \
  --expression-attribute-values '{":s":{"S":"pending"}}' \
  --select COUNT \
  --query 'Count'

aws dynamodb query \
  --profile "$PROFILE" \
  --table-name "$TABLE" \
  --index-name engine-command-index \
  --key-condition-expression 'engine_command_status = :s' \
  --filter-expression 'attribute_not_exists(engine_command_signature)' \
  --expression-attribute-values '{":s":{"S":"pending"}}' \
  --select COUNT \
  --query 'Count'
```

The second number is how many rows will quarantine as `missing_signature` on the
first tick. Record it before activation. `engine-command-index` is a sparse GSI
carrying only `@agent-engine` rows, so both queries stay small; note that
`--filter-expression` is applied after the key condition, so `Count` is the
filtered count while `ScannedCount` is not.

Expected values, and what each means:

| Second count | Reading |
|---|---|
| `0` | Nothing to reconcile. Any `missing_signature` after activation is new and worth investigating immediately. |
| Small, matches known pre-signing traffic | Expected. These quarantine on the first tick; the humans who sent them must re-issue the commands as new comments. |
| Larger than the first count | Impossible — a subset cannot exceed its superset. Re-check the table name and index before proceeding. |

Then confirm the spike matched the prediction, rather than assuming it did:

```bash
# Log group is adp-<env>, NOT bedrockgw-<env>: the gateway root passes
# name_prefix = "adp-${var.environment}" to the orchestration-tick module
# specifically, unlike every other module in that state.
aws logs filter-log-events \
  --profile "$PROFILE" \
  --log-group-name "/aws/lambda/adp-${ENVIRONMENT}-orchestration-tick" \
  --filter-pattern '"missing_signature"' \
  --start-time "$(( ( $(date +%s) - 3600 ) * 1000 ))" \
  --query 'length(events)'
```

A count materially **above** the prediction means rows are being marked pending
without a signature *now* — a signer that cannot reach its key, not a backlog.
Check the signer's logs for the unsigned-row metric rather than rolling back the
verifier.

Affected human commands are re-issued as new comments. There is no automated
reply: a quarantined row carries no trusted repository, issue or installation
fields, and answering on the thread using row-supplied routing would be a GitHub
write addressed with attacker-selectable values.

## Proving the worker cannot read the key

`tests/test_engine_command_signing_iam.py` asserts the policy shape from
rendered Terraform expressions, which catches the regression that lands in a
diff. It is not a live check. Against a deployed environment, the isolation is
verified by attempting the read as the worker role and confirming it is denied:

```bash
CREDS="$(aws sts assume-role --profile "$PROFILE" \
  --role-arn "arn:aws:iam::<account>:role/adp-${ENV}-agent-scaledjob-role" \
  --role-session-name engine-command-key-isolation-check \
  --query Credentials --output json)"

AWS_ACCESS_KEY_ID="$(echo "$CREDS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKeyId"])')" \
AWS_SECRET_ACCESS_KEY="$(echo "$CREDS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["SecretAccessKey"])')" \
AWS_SESSION_TOKEN="$(echo "$CREDS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["SessionToken"])')" \
  aws secretsmanager get-secret-value --secret-id "$SECRET_ID" 2>&1 | tail -2
```

An `AccessDeniedException` is the pass. **Anything that returns a value is a
critical finding**: stop, treat the key as compromised, and rotate it before
doing anything else. Record only the error string as evidence — never the value,
and never the assumed-role credentials.

## Evidence

Record: outgoing and incoming **key ids**, the overlap window, both version ids
returned by `put-secret-value`, the signer environment replacement and the
gateway rollout completion, the applied positive command and its stored key id,
the refused-not-quarantined negative case, `CommandsQuarantined` by reason
across the rotation window, the pending count before and after, the retirement
put, and the `AccessDeniedException` from the worker-isolation check.

Excluded from evidence: key material of any generation, keyring documents,
assumed-role credentials, and command bodies.
