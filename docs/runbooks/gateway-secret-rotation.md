# Gateway secrets: rotation and consumer reload

This runbook covers the gateway's own secrets — the key that signs platform
sessions, the key that signs single-use identity-linking ("magic link") tokens,
the shared secret in front of the internal control plane, and the edge provenance
proof. For each it records where the value lives, which consumers read it, how to
replace it, and what replacing it does that users can see.

It exists because issue #5656 (A05) separated the magic-link signing key from the
session-signing key. Before that change, `src/shared/config.py` resolved the
magic-link key as `magic_link_secret or token_secret_key`, and **no deployment
path set `BG_MAGIC_LINK_SECRET` at all** — so every environment signed
identity-linking tokens with the key that signs every platform session. Those two
purposes could not be replaced independently: containing a leaked magic-link
token meant rotating the session key and signing out every user, and a
session-key rotation silently broke in-flight identity linking.

**Removing the fallback from source does not change what is already deployed.**
An environment running an image that predates #5656 is still signing magic links
with the session key until the cutover below is completed there. Issue #5656
changed code, tests and deployment definitions only: it rotated nothing, deployed
nothing, and applied no infrastructure.

Nothing here is authorized by that code change. Rotation is a live operation on a
named environment: confirm the target account and obtain the environment's normal
change authorization first, per
[the deployment guide](../adp-platform-deployment/deploy-with-agent.md).

## Scope, and what this deliberately does not do

The scanner finding behind #5656 asked for an **automated rotation schedule on
every managed secret**. This runbook is the deliberate narrowing of that, per the
issue's own reviewed acceptance:

> Document supported provider-specific rotation/consumer reload instead of
> blindly adding automatic rotation to every secret.

The reason is concrete. An `aws_secretsmanager_secret_rotation` schedule attached
to a secret with no working rotation handler fails on a timer. Attached to a
secret whose consumers read it only at process start — which is every gateway
secret below, because they arrive as container environment variables — it is
worse than useless: Secrets Manager rotates the stored value, the running pods
keep serving the old one, and the two silently disagree until something restarts
at an hour nobody chose. Scheduled rotation is only safe where the consumer
re-reads, and the honest statement of that per secret is the control. Where a
secret genuinely does support managed rotation, this is noted below.

**Key-management-service (KMS) key rotation is a different control and does not
satisfy this one.** KMS rotates the key that encrypts a secret at rest; it does
not change the secret value, so it does nothing about a value that has leaked.

## The secrets

| Secret | Source of truth | Reaches the pod as | Replacing it is visible to |
|---|---|---|---|
| Session signing key | Secrets Manager `adp/<env>/gateway/token-secret-key` | `BG_TOKEN_SECRET_KEY` via `secretKeyRef` on `bedrockgateway-secrets`, key `token-secret-key` | **Every signed-in user** — all sessions end |
| Magic-link signing key | Secrets Manager `adp/<env>/gateway/magic-link-secret` | `BG_MAGIC_LINK_SECRET` via `secretKeyRef` on `bedrockgateway-secrets`, key `magic-link-secret` | Only users mid-way through linking a chat identity |
| Internal shared secret | Secrets Manager `adp/<env>/gateway/internal-api-key` | Gateway `BG_INTERNAL_API_KEY` and `ADP_DOOR_SERVICE_KEY`; agent-context and legacy-worker copies described below | Ingestion callbacks and agent Knowledge Door access until consumers agree |
| Edge provenance proof | SSM SecureString `/adp/<env>/gateway/apigw-provenance-secret` | `BG_APIGW_PROVENANCE_SECRET` via `secretKeyRef`, key `apigw-provenance-secret` | All API-Gateway-routed traffic if the two sides disagree |

Both deployment paths — `.github/workflows/gateway-deploy.yml` and
`platform/scripts/deploy-all.sh` — synchronize the configured values into the
`bedrockgateway-secrets` Kubernetes Secret. The new
magic-link bootstrap uses `scripts/ensure-signing-secret.py` and generates 32
random bytes only after Secrets Manager explicitly reports the key absent. Read
errors abort deployment; a concurrent creator is handled by rereading its value,
never by replacing it. Deliberate rotation remains a separate operator action.
Older neighboring secret-bootstrap blocks have their own error behavior; do not
use a routine deployment to attempt rotation.

The two signing keys are **not** declared in Terraform. The edge provenance
parameter is (`modules/gateway/infra/modules/api-gateway/main.tf`), and rotating
it requires the API Gateway side to change in the same apply — see its own
section.

Treat Terraform state and saved plans as secret material. Do not print any of
these values, and do not paste one into an issue, a PR or a run log.

## Common preparation

- Confirm the target AWS account, environment and region before changing
  anything. Set `AWS_PROFILE` to the approved profile, set `AWS_REGION`
  explicitly, run `aws sts get-caller-identity`, and have the account ID
  confirmed under the environment's normal change process. Do not rely on the
  shell's ambient region.
- Obtain a maintenance window sized to the secret: replacing the session key ends
  every session, so it needs one; the magic-link key does not.
- Ensure no other operator or automation will deploy the gateway during the
  cutover. A concurrent `gateway-deploy.yml` run re-reads Secrets Manager and
  restarts pods, which will interleave with these steps.
- Record the current Secrets Manager version ID and the Deployment's current
  image tag, so you can identify what to return to. Record version IDs, never
  values.

## Consumer reload: what actually picks up a new value

Every secret above arrives as a container environment variable through
`secretKeyRef`. Two consequences that determine every procedure below:

1. **Updating Secrets Manager alone changes nothing.** The running pods hold the
   value they started with.
2. **Updating the Kubernetes Secret alone also changes nothing.** Unlike a
   mounted volume, `secretKeyRef` environment variables are resolved once at
   container start and are never refreshed.

So the reload step is always: update the source of truth, re-synchronize the
Kubernetes Secret, then **restart the pods**. A rollout restart is what makes a
rotation take effect:

```sh
kubectl rollout restart deployment/bedrockgateway -n adp-gateway
kubectl rollout status  deployment/bedrockgateway -n adp-gateway --timeout=5m
```

## Magic-link signing key

### First-time cutover for an environment on the old fallback

Do this **before** an image containing #5656 reaches the environment. The
magic-link key must exist first; if the new image arrives while
`adp/<env>/gateway/magic-link-secret` is absent, the identity-linking endpoints
answer `503 not_configured` — correct fail-closed behaviour, and a brief outage
of that one flow rather than a silent fallback, but avoidable by ordering.

```sh
set +x
export AWS_REGION='<the environment's region>'
export ENVIRONMENT='<env>'

aws secretsmanager create-secret \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --name "adp/${ENVIRONMENT}/gateway/magic-link-secret" \
  --description 'Signing key for single-use identity-linking (magic link) tokens; independent of the session-signing key (#5656)' \
  --secret-string "$(openssl rand -hex 32)" \
  --query VersionId --output text
```

Then deploy normally. Both deployment paths pick the secret up and project it
into the pod; neither overwrites an existing value.

Note what this cutover does **not** require: the session key is untouched, so
**no user is signed out**. Magic links issued under the old fallback stop
verifying once the new key is live, so any link already sent out must be
reissued. Those links have a 15-minute lifetime
(`src/auth/magic_link.py::_TOKEN_TTL_SECONDS`), which bounds the window in which
anyone is affected.

### Later rotation

```sh
set +x
aws secretsmanager put-secret-value \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --secret-id "adp/${ENVIRONMENT}/gateway/magic-link-secret" \
  --secret-string "$(openssl rand -hex 32)" \
  --query VersionId --output text
```

Re-synchronize the Kubernetes Secret and restart, per **Consumer reload** above.
Outstanding magic links stop verifying; sessions are unaffected. This is the
property #5656 bought — and the one to verify after rotating, because it is what
demonstrates the two purposes really are independent.

**Owner and interval.** Platform team, on compromise suspicion and at least
annually. Deliberately not on an automated schedule: the consumer reads this at
process start only, so an unattended rotation would leave running pods signing
with a key the store no longer holds until the next unrelated restart.

## Session signing key

**Replacing this key signs out every user.** It is symmetric (HS256) and the
gateway reads exactly one value, with no previous-key slot, so a token signed
with the old key cannot be accepted once the new one is live. There is no overlap
window available in the current implementation — this is a brief interactive
cutover, not a transparent rotation.

Do not avoid the re-login by keeping a key you believe has leaked. Prefer a
scheduled cutover.

If a future change adds multi-key verification — a current key that signs plus a
previous key accepted for verification only — this section is what should be
revised, and the ordering below is dictated only by the present single-key
limitation.

```sh
set +x
aws secretsmanager put-secret-value \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --secret-id "adp/${ENVIRONMENT}/gateway/token-secret-key" \
  --secret-string "$(openssl rand -hex 32)" \
  --query VersionId --output text
```

Re-synchronize and restart per **Consumer reload**. Announce the sign-out first.
Since #5656, this no longer disturbs identity linking.

**Owner and interval.** Platform team, on compromise suspicion and at least
annually, in an announced window. Not automated, for the reload reason above and
because it is user-visible.

## Internal shared secret

This is the secret `src/internal/auth_deps.py` validates on the `/internal/v1/*`
shared-secret path. Since #5656 it is compared with a constant-time comparison,
so a wrong value no longer reveals through timing how much of it was correct.

The same stored value serves several paths:

- Gateway `BG_INTERNAL_API_KEY` verifies direct legacy internal requests.
  `ADP_DOOR_SERVICE_KEY` also reads `bedrockgateway-secrets/internal-api-key`
  and authenticates the gateway's mediated Knowledge Door calls for protected runs.
- `modules/agent-context/deploy.sh` bridges the value into the
  `agent-context-gateway-callback` Secret. The `context-mcp` deployment reads
  `DOOR_API_KEY`; ingestion jobs read `GATEWAY_INTERNAL_API_KEY` for callbacks.
- Unprotected workers may read `ADP_DOOR_API_KEY_SECRET` once at startup into
  `DOOR_API_KEY`, or receive an explicit legacy key. Supported legacy gateway
  clients also accept `VAULT_INTERNAL_API_KEY` or `ADP_INTERNAL_API_KEY`.
- Protected workers deliberately hold no shared key: their bridge uses run-bound
  credentials and the gateway holds the Door key. They still depend on agreement
  between the gateway and Door. SigV4 authentication to the gateway does not make
  the subsequent shared-key Door hop independent of rotation.

This key has no overlap window. Before an authorized rotation, inventory the
active consumers and schedule a coordinated maintenance window. Let jobs carrying
an old key finish; update the Secrets Manager value and both Kubernetes Secret
copies, then reload the gateway and Door together and start fresh workers and
ingestion jobs. Environment variables in running pods do not refresh. Account
for explicit key overrides and any legacy clients outside those manifests.

Do not pause webhook ingress or remove its invoke permission as a rotation step.
If uninterrupted Knowledge Door access is required, defer rotation until a
reviewed overlap mechanism exists. Afterward, verify both a protected worker's
mediated Door request and any enabled legacy path, plus ingestion callbacks and
an explicit issue-triggered worker start. This runbook does not authorize those
live operations.

```sh
set +x
aws secretsmanager put-secret-value \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --secret-id "adp/${ENVIRONMENT}/gateway/internal-api-key" \
  --secret-string "$(openssl rand -hex 32)" \
  --query VersionId --output text
```

**Owner and interval.** Platform team, on compromise suspicion and at least
annually. Not automated: it requires a coordinated update of a separate
component, which no rotation handler currently performs.

## Edge provenance proof

`/adp/<env>/gateway/apigw-provenance-secret` is Terraform-managed
(`modules/gateway/infra/modules/api-gateway/main.tf`) and is injected by API
Gateway as a request header, then validated by the pod
(`src/auth/caller_provenance.py`). Both sides must change together: rotating the
parameter alone makes API Gateway present a value the pod rejects, which fails
every API-Gateway-routed request.

Rotate it through Terraform — replace `random_password.edge_provenance`, apply so
the integration header and the parameter change in one operation, then
re-synchronize and restart the pods. Do not edit the parameter by hand; the next
apply would revert it, restoring a value you intended to retire.

**Owner and interval.** Platform team, via the normal reviewed apply, on
compromise suspicion. Not on a timer, because the apply must be reviewed.

## Verification after any rotation

- `kubectl rollout status deployment/bedrockgateway -n adp-gateway` completes.
- `GET /api/health` is healthy and gateway pods are `Running`.
- Confirm no applied object, pod environment listing, container argument or log
  line contains a secret value, only references:

```sh
kubectl get deployment bedrockgateway -n adp-gateway -o yaml | grep -A3 'BG_.*SECRET\|BG_.*KEY'
```

Every match should show a `secretKeyRef`, never a `value:`.

- After a **magic-link** rotation: issue a fresh identity link and confirm it
  completes, and confirm existing sessions still work — the independence
  property. A link issued before the rotation must be refused.
- After a **session-key** rotation: confirm a token captured before the cutover
  is now refused, that a fresh sign-in works, and that identity linking still
  functions — demonstrating the two keys are independently rotatable.

## Related

- [`superplane-jwt-and-db-credential-rotation.md`](superplane-jwt-and-db-credential-rotation.md)
  — the equivalent procedure for the Superplane API's signing key and database
  credential (issue #5683, A04).
- [`agent-authority-key-rotation.md`](agent-authority-key-rotation.md) — the
  agent-control envelope signing key, which *does* have a secondary-key overlap.
- [`engine-command-signing-key-rotation.md`](engine-command-signing-key-rotation.md)
  — the command-attribution keyring, which supports a bounded overlap window.
