# Superplane API: token signing key and database credential cutover

This procedure replaces two credentials in a running Superplane installation:

1. the **org-scoped token signing key** (`JWT_SECRET_KEY`), and
2. the **API database URL** consumed by the API Deployment and the migrate/seed Jobs.

It exists because issue #5683 (A04) removed hardcoded signing-key and database
defaults from `src/superplane-api/app/config.py`, the database URL from `alembic.ini`,
and embedded database credentials from `src/superplane-api/deploy/`.
Removing a committed credential from source does not change what is already deployed.
Any environment that ran on the removed signing-key default is **still forgeable until
this procedure is completed there**, and the code change alone is not the remediation.

Nothing here is authorized by the code change that referenced this file. Rotation is
a live operation on a named environment: confirm the target account and obtain the
environment's normal change authorization first, per
[the deployment guide](../adp-platform-deployment/deploy-with-agent.md). Issue #5683
explicitly did **not** rotate any live secret, deploy, or apply infrastructure.

## What the signing key is, and why replacing it is user-visible

`JWT_SECRET_KEY` both signs and verifies the tokens `POST /auth/login` issues. It is
symmetric (HS256), and the API reads **exactly one** key — there is no secondary or
previous-key slot, so a token signed with the old key cannot be accepted after the
new key is live.

**Consequence, stated plainly: replacing this key invalidates every token issued
under the old one.** Signed-in users get 401 and must sign in again. There is no
overlap window available in the current implementation, so this is a brief
interactive-session cutover, not a transparent rotation. Token lifetime is
`JWT_EXPIRE_MINUTES` (default 60), which bounds how long old tokens would otherwise
have remained valid — it does **not** give you a grace period once the key changes.

Do not attempt to avoid the re-login by keeping the old key: an environment that ran
on the committed default has a key that is public, and every token under it is
forgeable by anyone who can read the repository. Prefer a scheduled cutover over
leaving a forgeable key in place.

If a future change adds multi-key verification, this section is what should be
revised — the ordering below is dictated by the single-key limitation.

## Where the values live

| Value | Source of truth | Reaches the pod as |
|---|---|---|
| Signing key (installed path) | `secrets.observation` in Secrets Manager, field `jwt-signing-key` | `JWT_SECRET_KEY` via `secretKeyRef` on the `superplane-observation` Secret, rendered by `installation/manifests.py` |
| Signing key (standalone `deploy/` path) | The environment's approved managed secret source, synchronized to the `superplane-api-secrets` Kubernetes Secret, key `JWT_SECRET_KEY` | `JWT_SECRET_KEY` via the required `secretKeyRef` in `deploy/deployment.yaml` |
| API database URL | `secrets.database` in Secrets Manager, fields `runtime-url` / `migration-url` | `DATABASE_URL` via `secretKeyRef` (`superplane-db` for the installed path; `superplane-api-db` for the standalone `deploy/` manifests) |

Secrets Manager reference names are per-environment inputs
(`secrets.observation`, `secrets.database` in the environment file). The installer
reads them into memory, applies Kubernetes Secrets through stdin, and records only
version IDs — see `modules/domain-apps/superplane/installation/README.md`.
The standalone manifests do not run that installer and do not create
`superplane-api-secrets`; their deployment automation must synchronize the named key
from an approved managed secret source, or an authorized operator must use the
stdin-only fallback documented below.

Both references are **required with no fallback**. The field check in
`installation/runner.py` is set equality, so an existing `secrets.observation` that
predates #5683 is refused until `jwt-signing-key` is added. `secretKeyRef` entries
use `optional: false`, so a missing key means the pod does not start rather than
starting with the variable unset.

Treat Terraform state and saved plans as secret material. The signing key must never
enter Terraform: `infra/control-plane/variables.tf` takes the secret **name**
(`jwt_secret_name`) and validates that it does not look like a value. Do not print
either credential, and do not paste one into an issue, a PR or a run log.

## Common preparation

- Confirm the target AWS account, environment, and region before changing anything.
  Set `AWS_PROFILE` to the approved profile, set `AWS_REGION` to the environment
  file's top-level `region`, run `aws sts get-caller-identity`, and have both the
  account ID and `AWS_REGION` confirmed under the environment's normal change
  process. Do not rely on the profile's or shell's ambient region.
- Obtain a maintenance window. Users are signed out when the new key becomes active.
- Ensure no other operator or automation will update the selected signing-key source
  during the cutover. Secrets Manager does not provide a compare-and-swap for the
  installed-path read/modify/write below.
- Record the current managed-secret version ID (installed path) or Secret
  `resourceVersion` (standalone path), API image tag, and Deployment state without
  recording a credential value or token body.
- Capture one valid token before the rollout and keep it only for the rejection check.

## Installer-backed signing-key source update

Choose the procedure that matches the environment:

- **Initial remediation:** the running API predates #5683 or
  `secrets.observation` does not yet contain `jwt-signing-key`. Seed the field before
  deploying the fixed installer and image; otherwise the updated installer's exact
  field check correctly refuses the installation.
- **Later rotation:** the running image includes #5683 and the field is already
  present. Replace it, then restart the API using the same no-overlap rollout.

The following command preserves every existing JSON field, generates 48 bytes of
random material inside the pipe, adds or replaces only `jwt-signing-key`, and writes
the complete object back. It emits only the resulting version ID. Disable shell
tracing first so operator tooling cannot echo command expansion.

```sh
set +x
set -o pipefail
export AWS_REGION='<value of top-level region from the environment file>'
export OBSERVATION_SECRET_ID='<value of secrets.observation from the environment file>'

aws secretsmanager get-secret-value \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --secret-id "$OBSERVATION_SECRET_ID" \
    --query SecretString \
    --output text \
  | python3 -c '
import json
import secrets
import sys

document = json.load(sys.stdin)
if not isinstance(document, dict):
    raise SystemExit("secrets.observation must contain a JSON object")
document["jwt-signing-key"] = secrets.token_urlsafe(48)
json.dump(document, sys.stdout, separators=(",", ":"))
' \
  | aws secretsmanager put-secret-value \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      --secret-id "$OBSERVATION_SECRET_ID" \
      --secret-string file:///dev/stdin \
      --query VersionId \
      --output text
```

Do not split this pipeline into a plaintext file or a copy/paste step. The installer
requires at least 32 characters; the command supplies more and never places the value
in shell history, argv, terminal output, or the repository. Record the returned
version ID, not the JSON.

## Initial remediation from the removed fallback

Use this path when the current image may still accept the removed source fallback.
The order is deliberate: the old pods stay available while the managed field is
seeded, and only then are they replaced by pods that require it.

1. **Seed the source secret while old pods remain running.** Run the common update
   command. Do not run the updated installer first: until this step completes, its
   exact secret-field validation will refuse the old `secrets.observation` object.
   Adding the field does not change old pods; they do not receive the new environment
   reference until the Deployment is rendered and replaced.
2. **Start the maintenance window and deploy the fixed release with no key overlap.**
   The installer renders `superplane-api` as one replica with strategy `Recreate`, so
   the old pod is stopped before the new pod starts. Use the environment's normal
   installer or platform deployment path and verify it retains that strategy. If a
   different path manages the Deployment, stop or scale the old API to zero before
   applying the fixed manifest; do not use a rolling update that serves old-key and
   new-key pods simultaneously.
3. **Complete and verify the rollout.** Do not restore traffic until the fixed-image
   pod is `Running` and ready. A `CreateContainerConfigError` means the Secret field
   is missing or misnamed; fix the reference rather than weakening it. Confirm a new
   sign-in works, the pre-cutover token is rejected with 401, and the API logs do not
   contain `JWT_SECRET_KEY is not set`.
4. **Record completion.** Record the new secret version ID, fixed image tag, Recreate
   rollout completion, successful new sign-in, and rejected old token. Record no
   credential or token value.

## Later signing-key rotation

Use this path only when the running image already includes #5683 and consumes
`jwt-signing-key` from `secrets.observation`.

1. **Replace the managed field.** Run the common update command. Updating Secrets
   Manager alone does not change running pods; environment variables are fixed at pod
   start.
2. **Restart with no key overlap.** Run the normal installer or platform deployment
   path and retain the rendered one-replica `Recreate` strategy. If another path is
   used, stop old-key pods before starting new-key pods. A rolling Deployment with
   both generations live will accept a token only on the generation whose key signed
   it.
3. **Verify the cutover.** Confirm the new pod is ready, a fresh sign-in succeeds,
   the pre-rotation token is rejected with 401, and the startup refusal is absent.
4. **Record completion.** Record only the ending version ID, image tag, rollout
   completion, and verification results.

## Standalone signing-key remediation and rotation

Use this path when `src/superplane-api/deploy/deployment.yaml` owns the API. It runs
two replicas and reads `JWT_SECRET_KEY` from the `superplane-api-secrets` Secret, key
`JWT_SECRET_KEY`. Updating `secrets.observation` does not populate this separate
Secret. Seed it **before** applying the fail-closed Deployment on an initial
remediation; otherwise both pods remain in `CreateContainerConfigError`.

Prefer the environment's approved managed secret-sync controller. Create a new random
value of at least 32 characters in its managed source, map only that value to
`superplane-api-secrets/JWT_SECRET_KEY` in the `superplane` namespace, and wait until
the Kubernetes Secret has the expected new source version. Do not commit the source
value or a generated `kind: Secret` manifest. Ensure the sync controller continues to
own later updates; do not alternate between controller and manual ownership.

If this environment has no approved secret-sync mechanism, an authorized operator may
create or update the Kubernetes Secret through stdin. Confirm the context first and
keep shell tracing disabled. The `superplane` namespace must already exist; on a new
cluster, create only that namespace through the approved bootstrap path before
seeding the Secret. Do not apply the fail-closed Deployment first.

When the Secret does not yet exist, create it directly from stdin. The value is never
rendered as a manifest or command argument, and the command prints only the normal
`kubectl create` status.

```sh
set +x
set -o pipefail
export KUBE_CONTEXT='<approved standalone cluster context>'

python3 -c '
import secrets
import sys

sys.stdout.write(secrets.token_urlsafe(48))
' \
  | kubectl --context "$KUBE_CONTEXT" --namespace superplane \
      create secret generic superplane-api-secrets \
      --from-file=JWT_SECRET_KEY=/dev/stdin
```

For a later rotation, patch only the existing key so any unrelated Secret fields and
metadata are preserved. This command emits a merge patch inside the pipe and prints
only the normal `kubectl patch` status.

```sh
set +x
set -o pipefail
export KUBE_CONTEXT='<approved standalone cluster context>'

python3 -c '
import base64
import json
import secrets
import sys

value = secrets.token_urlsafe(48).encode("ascii")
json.dump(
    {"data": {"JWT_SECRET_KEY": base64.b64encode(value).decode("ascii")}},
    sys.stdout,
    separators=(",", ":"),
)
' \
  | kubectl --context "$KUBE_CONTEXT" --namespace superplane \
      patch secret superplane-api-secrets \
      --type=merge \
      --patch-file=/dev/stdin
```

Do not replace `/dev/stdin` in either command with a command-line literal,
environment variable, or temporary file. Record only the resulting Secret
`resourceVersion`; do not inspect or record its payload.

Then perform the same sequence for initial remediation or later rotation:

1. **Seed or update the Secret while the current pods remain running.** Complete one
   of the two source-update paths above and verify that
   `superplane-api-secrets/JWT_SECRET_KEY` exists without printing it. For initial
   remediation, do this before applying the fixed manifest. For later rotation,
   remember that existing pods retain the old environment value until restarted.
2. **Start the maintenance window and remove all old-key pods.** The checked-in
   standalone Deployment uses two replicas with the default rolling strategy. Do not
   use `kubectl rollout restart`: it would temporarily mix old-key and new-key pods.
   Scale the Deployment to zero and wait until every selected pod is gone:

   ```sh
   kubectl --context "$KUBE_CONTEXT" --namespace superplane \
       scale deployment/superplane-api --replicas=0
   kubectl --context "$KUBE_CONTEXT" --namespace superplane \
       wait --for=delete pod \
       --selector=app.kubernetes.io/name=superplane-api \
       --timeout=5m
   ```

3. **Apply the fixed standalone release and restore two replicas.** Use the approved
   standalone deployment path to apply the fixed `deploy/deployment.yaml` and its
   approved fixed API image. Its declared `replicas: 2` restores service only after all
   old-key pods have stopped. Wait for both new pods to become ready. A
   `CreateContainerConfigError` means the Secret name or key is absent; repair the
   reference or synchronization rather than making it optional.
4. **Verify both sides of the cutover.** Confirm a fresh sign-in succeeds and its token
   authenticates a protected request. Confirm the token captured before step 1 is
   rejected with 401, and confirm the startup refusal is absent from both new pods.
5. **Record completion.** Record only the ending managed-secret version ID or
   Kubernetes Secret `resourceVersion`, pinned image tag or digest, zero-scale and
   two-replica readiness completion, successful fresh-token request, and rejected old
   token.

## Database credential cutover

The API and the migrate/seed Jobs now read `DATABASE_URL` from a Secret instead of an
inline URI. Updating only the secret is not a credential rotation: PostgreSQL must
also accept the new credential, and the old credential must be disabled. Coordinate
the database and secret-store changes for each affected role.

The installed path has distinct scoped roles in `secrets.database`: `runtime-url` is
used by the API and `migration-url` is used by the migration Job. Rotate them in
separate maintenance operations so a failure identifies one role and one consumer.
The standalone `deploy/` path has one `DATABASE_URL` in the `superplane-api-db`
Secret; apply the same procedure to the role named by that URL. Do not rotate an
in-flight migrate/seed Job: let it finish or terminate it before changing its role.

Before either cutover:

1. Have the database owner identify the exact PostgreSQL role, its current grants,
   and every consumer. Confirm the target endpoint and database without recording the
   URL or password.
2. Record the current `secrets.database` version ID (installed path) or Kubernetes
   Secret `resourceVersion` (standalone path), the consumer image, and Deployment/Job
   state. Never record a Secret payload.
3. Choose one of the two supported strategies below. Prefer a replacement role when
   the database owner can reproduce and verify least-privilege grants; otherwise use
   the maintenance-window password change.

### Strategy A: change the existing role password

PostgreSQL has one active password for a role, so this strategy has an intentional
outage between the database change and consumer restart.

1. Stop or scale to zero every long-running consumer of the role. Ensure no one-shot
   Job using it remains active.
2. Through the database owner's approved password-management channel, generate a new
   random password and alter the PostgreSQL role to that password. Do not place the
   password in shell history, command arguments, files, tickets, or logs.
3. Build the updated URL in a protected channel. For the installed path, replace only
   the matching `runtime-url` or `migration-url` field in `secrets.database`, preserve
   every other JSON field, and record only the resulting version ID. For the
   standalone path, update the `DATABASE_URL` key in `superplane-api-db` through stdin
   or the environment's secret-sync mechanism, never as a literal manifest value.
4. Run the normal installer or platform deployment path so the Kubernetes Secret is
   refreshed. Restart the API for `runtime-url`; create a new migration/seed Job for
   `migration-url` or the standalone Job, because an existing pod does not reread its
   environment.
5. Verify the new role credential with a minimal authenticated database check, then
   verify the actual consumer: API readiness and a database-backed request for the
   runtime role, or successful completion for the migration/seed role. Confirm the
   previous password is rejected without printing either password.

The role's previous password is disabled at step 2; the final rejection check proves
that database-side change rather than assuming a Secret update performed it.

### Strategy B: replace the database role

This strategy permits a staged consumer cutover but must not leave the disclosed role
enabled afterwards.

1. Through the database owner, create a new login role with a newly generated
   credential and only the grants, schema ownership, and role defaults required by
   the old role's scoped purpose. Verify the grants directly; do not infer them from a
   role name.
2. Update the matching managed-secret URL to name the replacement role, preserving
   unrelated fields as in Strategy A, then refresh the Kubernetes Secret through the
   normal deployment path.
3. Restart only that role's consumers and perform the same database and consumer
   checks as Strategy A. Keep unrelated roles and consumers unchanged.
4. After all old-role sessions drain, revoke login from the old role and terminate
   any unexpected remaining sessions under the environment's database change policy.
   Confirm the old credential no longer authenticates. Drop the old role only after
   the database owner confirms no object ownership or grants still require it.

Any environment that applied the old standalone manifests used a repository-known
credential. Treat that password and every copy of it as disclosed. A rollback must
never restore that password or re-enable that role with the disclosed credential.

The standalone Secret must hold the SQLAlchemy form (`postgresql+asyncpg://…`) because
the API and Alembic consume it. `db-seed-job.yaml` normalizes the scheme for `psql`, so
do not create a second password-bearing URL in libpq form.

`src/superplane-api/deploy/integration-test.yaml` is deliberately excluded from all of
the above. It is a throwaway local fixture: an `emptyDir` Postgres destroyed with its
pod, whose signing key is generated per apply. It has nothing to rotate and must not
be applied to a shared cluster.

## Failure and rollback

If a signing-key rollout fails, recover forward: keep API traffic stopped, repair the
managed Secret field or generate another new key, and complete the no-overlap rollout.
For every environment that used the removed committed default, restoring the previous
key is prohibited because it is publicly known and would immediately restore the
cross-tenant token-forgery vulnerability. Availability does not justify re-enabling a
disclosed authentication key.

For a later rotation where the prior key is positively known never to have been
disclosed, an authorized operator may coordinate restoration of that prior managed
secret version and the matching API rollout. This exception never applies to the
committed default removed by #5683 or to any other key whose confidentiality is
uncertain.

For an in-place database password change, keep consumers stopped while repairing the
new password or URL. If the previous password was not disclosed, the database owner
may restore it and the operator may restore the matching prior secret version as one
coordinated rollback. For an environment affected by the repository-known credential,
never restore the old password; generate another new password, update PostgreSQL and
the managed secret, and complete the rollout forward.

For a replacement-role cutover, rollback before old-role revocation may point consumers
back to the old role only when its credential is known not to be disclosed. After
revocation, or for every environment affected by this issue, repair the replacement
role or create another replacement instead. Do not leave both roles login-enabled as a
long-term rollback state.

A pod that will not start because a Secret field is absent is the fail-closed design
working. The fix is to add the field, never to set `optional: true` or reintroduce a
default in `app/config.py`; the credential-requirement tests and installer checks fail
if either is attempted.

Rotation is not retroactive. Tokens forged under the old key before the cutover may
already have been used. If the environment ran on the committed default and you have
reason to suspect misuse, the audit review of what those tokens did is separate work
and is not in scope for #5683.

## Evidence to record

Starting and ending `secrets.observation` version IDs, the API image tag, rollout
completion, the successful new sign-in, the rejected pre-rotation token, the absence
of the startup refusal in logs, and — for the database half — confirmation that the
previous password no longer authenticates. Credentials, key material and token bodies
are excluded from the record.
