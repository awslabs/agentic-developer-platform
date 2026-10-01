# Internal and Knowledge Door identity (#6824)

This change removes shared-key authentication from the gateway internal plane
and the Knowledge Door. It requires a coordinated rollout; merging source does
not complete live remediation.

## Authorization boundaries

- Gateway internal callers authenticate with IAM through the existing API
  Gateway edge provenance check and agent registry. A shared key never produces
  an authenticated principal. Existing broker run-binding checks remain mandatory.
- Identity routing and admin reads additionally require registry-owned operation
  capabilities. A tenant caller is bound to its registry `org_id`; changing a
  request's tenant does not change its entitlement. Only explicitly trusted
  ingress registrations receive `internal:cross-tenant`. Worker registrations
  receive none of these service capabilities.
- Provider ingestion receives `internal:identity:resolve` and
  `internal:identity:link`. Webhook ingress receives `internal:identity:resolve`
  and `internal:installation:resolve`. Both ingress roles need cross-tenant
  routing because they route authenticated provider events across the installation.
  Neither receives internal audit/configuration read privileges.
- An operational auditor must be registered with the exact tenant `org_id` and
  `internal:audit:read` and/or `internal:tenant-config:read`. Do not give a worker
  these capabilities. These values are provisioned through trusted infrastructure,
  not accepted from client headers or the public registry management schemas.
  The adversarial workflow now uses the IAM endpoint and signed inspection
  requests; grant its trusted job role only these sandbox-tenant read capabilities.
  Installation backfill also uses IAM and requires `internal:installation:resolve`
  with the intended tenant scope. Deprecated key CLI arguments are ignored.
- Ingestion status callbacks use their existing signed asset/tenant/attempt grant
  and persisted digest. Removing the redundant transport key does not remove
  expiry, replay, tenant, or current-attempt checks.
- The gateway's mediated knowledge path verifies the live run, grant and current
  membership before signing a Door assertion. It derives human identity from
  database records; the worker cannot select a different person or tenant.
- Door assertions use the existing gateway-only Ed25519 signing key and a
  separate `adpd1` protocol and `adp-knowledge-door` audience. They bind the run
  principal, tenant, personal owner, GitHub login, HTTP method, exact path and
  body digest. Validity is at most 30 seconds with five seconds of issue-time
  clock tolerance. Door rejects query strings and bodies over 1 MiB.
- Door receives only pinned public keys. Both REST and mounted MCP pass through
  the same verification middleware, which replaces all caller-supplied ACL
  headers. The legacy authentication-disable setting has no effect. New requests
  undergo live gateway revalidation; an already-forwarded signed request retains
  its bounded validity window. This protocol does not provide single-use delivery.

## Rollout order

Follow the canonical deployment guide for account confirmation and reviewed
applies. No deployment, credential deletion or IAM apply is performed by this PR.

1. Pause new worker admission and drain legacy jobs. Keep
   `agent_authority_prepared=true`. Preparation provisions the existing signing
   keyring, protected worker role and public-key ConfigMap. Admission now defaults
   to paused; explicit unprotected admission fails Terraform validation.
2. Apply the ingress registry capabilities and IAM invoke permissions. Point
   ingress resolution at the IAM `execute-api` endpoint, then deploy the signed
   Lambda clients. These clients also work with the previous gateway IAM path.
   Inventory any additional internal callers before enforcing the new gateway;
   unregistered or unscoped callers will receive 403.
3. Deploy the gateway and Door during the paused window. Their protocol cutover
   is coordinated: the previous Door does not understand signed assertions.
   Agent-context deployment copies the public keyring from the platform's
   `adp-control-verification-keys` ConfigMap. A digest annotation rolls Door pods
   when the keyring changes. Neither the deploy helper nor Door needs a private key.
4. Deploy current workers using protected run identity. The worker environment
   no longer loads broad internal credentials, and knowledge clients always use
   the authenticated loopback bridge. Unprotected workers have no direct-Door
   fallback. Preserve the existing immutable-image and runtime-readiness gates.
5. Complete the existing legacy-role retirement and Kubernetes isolation checks.
   Confirm no legacy workload can read gateway signing secrets, SSM authority
   material, Terraform state or gateway Kubernetes secrets. Unpause only after
   protected activation, legacy admin retirement and verified isolation. These
   operator assertions must reflect live evidence, not just Terraform inputs.
6. Run a protected canary in each of two tenants: each must retrieve its own
   allowed repository/personal data and be denied the other tenant's data.
   Verify live callback completion/replay rejection, identity resolution, and
   audit/configuration tenant scoping. The verb-ops workflow now checks rejection
   of unsigned REST/MCP requests; it does not impersonate an arbitrary user.
7. After all consumers have migrated, revoke/delete the old gateway internal
   secret and leftover Kubernetes copies through the deployment's secret-lifecycle
   procedure. Terraform removes the adversarial SSM mirror. Check any pre-existing
   mirror consumers before apply. Do not restore shared-key access as a rollback.

For key rotation, publish both verification keys and roll Door before switching
the gateway signer. After old requests expire, remove the old public key and roll
Door again. A missing keyring fails closed. If a rollout fails, leave admissions
paused and restore a matched gateway/Door version pair while diagnosing it;
restoring a legacy pair also restores the original exposure.

## Verification

Tests cover real gateway-to-Door signing/verification, forged identity headers on
REST and mounted MCP, changed bodies, expired/wrong-audience assertions, missing
keys, tenant entitlements, signed ingestion callbacks against PostgreSQL,
resolution/signing/redirect behavior, and worker credential removal. Mocked
Terraform plans exercise safe preparation, protected activation and rejection of
unprotected admission. Live two-tenant canaries remain a deployment requirement.
