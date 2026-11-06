# Verify group runner registration and private GitLab trust on dev

This is an operator procedure for issue #9284, **not** evidence of a deployment.
The module creates secret containers and discovery references; the registrar
stores a paused group's token. Activation and rotation require the separate,
qualified runner manager integration. Do not interpret a staged token or a
Terraform mock as a working runner or a successful CA access check.

## Private prerequisites and stop conditions

- Provision the separate exporter identity from the module's
  [CA-exporter Terraform root](runner-ca-exporter-infrastructure.md), or verify
  its existing state ownership. Set the instance root's
  `runner_ca_exporter_role_name` from that root's output. The instance root owns
  its certificate/secret permissions; the exporter setup root owns role trust.
- Obtain explicit authorization for the dev instance deployment and a private,
  **run-owned tenant-parent group**. Never test against a shared or existing
  tenant, and never use an instance runner. The module owner records the numeric
  group ID, canonical tenant marker, group membership, source revision, exact
  target account/role, region and operation IDs in a private run manifest.
- Select the intended registrar, **separate CA exporter**, one manager role for
  this group, and unrelated denied identities. Confirm the exact account and
  assumed IAM role for every selected connection with `adp-cred assume --service
  aws --label <PRIVATE_LABEL> --exec aws sts get-caller-identity`; an account match
  with the wrong role is a stop. Do not fall back to ambient credentials or
  borrow a more privileged role for a negative probe. The manager-role read must
  run through the approved runner-stack identity/harness, not an impersonation.
- Confirm that the instance stack is applied with a private DNS hostname covered
  by the configured ACM certificate, `/adp/<env>/gitlab/runner-url` is private
  HTTPS (with `/gitlab` when installed at that root), and
  `/adp/<env>/gitlab/ca-chain-secret-arn` contains an ARN **only**. Preserve
  `/adp/<env>/gitlab/url`. Keep the reviewed CA root file and SHA-256 fingerprint
  in protected operator configuration. Confirm the private API/clone/artifact
  routes work without cross-origin redirects or TLS bypasses.
- Check the admin credential's independent expiry and the server's runner-token
  auto-rotation settings. If unattended rotation cannot be disabled or its
  write-back cannot be verified, **stop before activation**. Verify the separate
  manager integration can drain the old runner, switch to a stored version,
  prove manager ID, and unpause only after qualification. If absent, verify the
  paused create/CA export only; do **not** claim rotation, revocation, or SCN-02.
- Use a protected operator host with private network reachability, a mode-0600
  configuration file owned by the running UID, and an exclusive operation window
  for this group. The file contains the opaque tenant ID, numeric group ID,
  `adp-group-<group-id>` tag, protected access policy, approved instance/runner
  HTTPS URLs and approved CA file path. Keep the config, fixtures, identity
  mapping and evidence outside this public repository. No shell tracing,
  provider debug output, raw responses, copied Kubernetes Secrets or token
  arguments. Use an already approved, access-controlled **versioned private S3
  evidence store**, not a new validation service.

Allocate a new fixed operation UUID in the private manifest for each **new**
create, rotate or revoke; use the **same** UUID for retries and `--reconcile`.
Give each phase a 10-minute deadline (including identity checks and cleanup).
If a deadline expires, leave the operation record in place, stop starting new
operations and hand recovery to the module owner; never treat a timeout as a
successful deletion. Record every attempt and timeout privately.

## Export and verify the CA

After matching the registrar/exporter identities to the approved roles, run the
exporter with the selected exporter connection; values below refer to **private
operator inputs**, not example targets. The root fingerprint must be approved
independently of the ACM leaf. The exporter validates the chain, hostname and
private HTTPS liveness response before writing the trust bundle; it refuses
redirects. It calls `/-/liveness` under the configured GitLab URL prefix and
requires HTTP 200 with `{"status":"ok"}`, as documented in GitLab's
[liveness check](https://docs.gitlab.com/administration/monitoring/health_check/#liveness).
This endpoint needs no API token but is subject to the monitoring IP allowlist.
Use the private HTTPS route from an allowed source; the instance stack configures
the VPC CIDR for monitoring and keeps this route off the public HTTP listener.
The exporter must not receive administrator-token access. This probe verifies
TLS and application reachability, not runner API, clone or artifact acceptance.

```bash
adp-cred assume --service aws --label "$CA_EXPORTER_LABEL" --exec \
  timeout 600s python3 modules/source-control/gitlab/scripts/export-runner-ca.py \
  --environment "$ENVIRONMENT" --region "$REGION" \
  --certificate-arn "$CERTIFICATE_ARN" --runner-url "$PRIVATE_RUNNER_URL" \
  --approved-root-file "$ROOT_FILE" --approved-root-sha256 "$ROOT_SHA256"
```

Do not publish the resolved variables or command output. Confirm the CA secret
version exists using metadata only. Via the approved manager harness and *that
exact manager role*, attempt `GetSecretValue` on the CA ARN and its own group
secret. A successful API read followed by validated PEM/hostname and schema
checks is the allowed result; version metadata alone does not establish TLS.
Via an independently verified unrelated identity and a second group's manager
(if available), attempt the same exact reads; require AWS `AccessDenied`, not
DNS, expired credentials, or a missing object. The registrar/exporter checks
must prove that the exporter cannot read admin/group tokens and managers cannot
read another group's token. Save only result categories, timestamp, role and
resource **identifiers** privately; never store returned secret strings.

## Create, qualify and read back

Use the selected registrar connection and a fixed UUID from the private
manifest. Keep each command under its 10-minute phase deadline:

```bash
adp-cred assume --service aws --label "$REGISTRAR_LABEL" --exec \
  timeout 600s python3 modules/source-control/gitlab/scripts/register-runner.py \
  --environment "$ENVIRONMENT" --region "$REGION" --config "$RUN_CONFIG" \
  --operation-id "$CREATE_ID" --create
```

The only successful initial phase is `staged`: SSM holds non-secret operation
metadata, and Secrets Manager has an `AWSPENDING` version with the runner token.
Before activation, use the same verified admin path to read `groups/<group-id>`,
`groups/<group-id>/runners` and the exact runner ID returned by the command.
Require the canonical tenant marker, top-level group and service-only owner,
`runner_type=group_type`, exact group association, `locked=true`,
`run_untagged=false`, required group tag, chosen `access_level` and `paused=true`.
Reject instance/shared types, ambiguous groups or incorrect readback. Check
that the runner is **not** listed as an instance runner. Retain only redacted
field checks and identifiers in the private evidence store.

Only the authorized runner manager integration can promote the verified pending
version, prove manager use of that runner ID, then unpause it. Read back
`paused=false` and verify a single matching manager; do not manually unpause a
runner or reset its token. A create without that integration remains staged and
cannot satisfy live acceptance. Verify pipeline assignment only under this
run-owned group and its protected/tag policy, never another tenant's job.

## Rotate, reject the old token and revoke

Once the initial record is `finished` and manager use has been verified, record
both runner IDs and the **old secret version ID** privately. Before rotation,
qualify an in-memory runner-authentication check for the old token against the
private `/api/v4/runners/verify` endpoint over the approved CA, recording only
HTTP status. If the old token cannot be verified before rotation, it is not a
valid old-token rejection oracle; stop and investigate.

```bash
adp-cred assume --service aws --label "$REGISTRAR_LABEL" --exec \
  timeout 600s python3 modules/source-control/gitlab/scripts/register-runner.py \
  --environment "$ENVIRONMENT" --region "$REGION" --config "$RUN_CONFIG" \
  --operation-id "$ROTATE_ID" --rotate
```

Expect a *paused* replacement and an `AWSPENDING` version while the old token
remains `AWSCURRENT`; an unfinished record means no other operation may begin.
The manager integration drains the old runner with a deadline, switches only
once the new version is promoted and its ID read back, then unpauses the new
runner and removes the old registration. Repeat the private verified-token
probe against the **old version** and require authentication rejection with an
actual provider HTTP response; a timeout, redirect, TLS failure or absence of
baseline is not rejection. Read back new `group_type`, association and
restrictions. Never print the old or new token, save plaintext locally, or
place either in the run manifest.

After verifying the replacement, revoke it with another fixed UUID:

```bash
adp-cred assume --service aws --label "$REGISTRAR_LABEL" --exec \
  timeout 600s python3 modules/source-control/gitlab/scripts/register-runner.py \
  --environment "$ENVIRONMENT" --region "$REGION" --config "$RUN_CONFIG" \
  --operation-id "$REVOKE_ID" --revoke
```

Confirm the exact runner is absent from its group and the admin runner read,
the token verification endpoint rejects the most recent authentication token,
and the group's secret is scheduled for deletion. An already-running job may
still have its own credential: cancel/drain run-owned jobs and inspect their
namespace before declaring cleanup. Do not delete a shared CA chain during
ordinary group revocation.

## Recovery and evidence handoff

If a create/rotate response is lost or a secret write fails, never retry with a
new UUID or unpause an orphan. Resume the existing operation under the same
registrar identity, private config and exclusive group window:

```bash
adp-cred assume --service aws --label "$REGISTRAR_LABEL" --exec \
  timeout 600s python3 modules/source-control/gitlab/scripts/register-runner.py \
  --environment "$ENVIRONMENT" --region "$REGION" --config "$RUN_CONFIG" \
  --operation-id "$UNFINISHED_ID" --reconcile
```

An unpersisted token is unrecoverable: the CLI removes a paused orphan, confirms
absence, then creates a new paused runner. A failed revocation likewise remains
`revoking` until a retry confirms runner absence before scheduling secret
deletion. `--reconcile` does **not** qualify staged rotation: the separate
manager integration must finish it. If integration is absent or cleanup cannot
be confirmed inside the deadline, keep the runner paused, preserve SSM/secret
state and escalate; do not force deletion, empty operation state, or claim
success. For an initial staged registration that cannot be qualified, the
module owner must explicitly approve an isolated cleanup using the verified
admin path; the CLI's normal `--revoke` refuses an unfinished staging record.

Store the private fixture manifest, exact selected roles/source revision,
operation and runner IDs, version IDs, per-phase timestamps, API readback and
allowed/denied outcomes in the **existing approved versioned private S3 evidence
store**. Record final absence of run-owned runner/job resources, scheduled
secret deletion and unchanged public discovery; retain failed attempts. Publish
only a sanitized per-criterion outcome after live verification, not the manifest,
private URLs, account IDs, logs, raw tokens or CA payload. If any prerequisite
or independent oracle is missing, mark SCN-02 and its AC evidence **not checked**
and identify the module owner as the next actor.
