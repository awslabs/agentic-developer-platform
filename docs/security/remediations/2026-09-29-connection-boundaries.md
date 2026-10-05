# Login and personal AWS connection boundaries

This change addresses #6655, #6663 and #6662. It has not been deployed.
Application and Terraform changes are included; production closure still needs
operator configuration, a reviewed Terraform apply and runtime validation.

## Behavior

Broker callbacks require a nonempty, matching, single-use browser state for both
code and legacy token transports. A callback without state must restart login.
Rejected callbacks clear the browser URL and never save tokens.

The deployment script no longer includes `*.atlassian.net` in either fallback
host list. An explicitly configured SSM allowlist continues to take precedence.
Operators must remove any existing wildcard from
`/adp/ENV/gateway/vault-proxy-host-allowlist` and configure exact approved tenant
hosts, such as `acme.atlassian.net`. This default change does not automatically
remove an existing setting, nor does it add per-credential tenant binding.

Personal AWS connections now have a server-issued trust ID in a dedicated DB
column. Generic vault input and caller-supplied secrets cannot create this
provenance. Verification requires the expected provider-attested identity and
successful assumption with that ID, followed by explicit AccessDenied for both
a random ID and no ID. Other provider errors fail verification. These negative
probes use the same tags and session parameters as the successful request and
never deliver their credentials. Rejected provider trust checks write an audit
record and expose one generic failure reason.

Customer-role assumptions refuse the gateway AWS account, the configured platform
Bedrock account, and reserved role
names (including IAM paths). `ADP-Agent-*` is allowed only in customer accounts,
since it is the existing customer CloudFormation namespace. Configure the real
`ADP_GATEWAY_ROLE_ARN` / `ADP_GATEWAY_ACCOUNT_ID`; unknown platform identity fails
closed. The configured reserved-role prefixes are shared with agent registration.

The internal session broker, workspace validation and Superplane account adapter
require the server-issued ID and current, version-bound verification evidence.
Generic or team vault entries without ownership evidence cannot mint sessions.

## Existing-role onboarding

Run `adp aws connect --account ACCOUNT --role-arn ARN --name NAME --yes` without
legacy ExternalId flags. The CLI registers a pending connection and returns its
server-generated ExternalId and suggested trust policy. Give that policy to the
AWS account administrator. They must review the role's existing trust policy:
all statements allowing ADP to assume it must require this connection's ID;
leaving an unconditional Allow alongside the new statement will fail verification.
The supplied statement also pins `adp:user_id` to the connection owner. Preserve
any separately required trust for other principals only after administrator review.

Then run `adp aws verify CONNECTION_ID`. Import itself never reports a verified
connection. Repeating import as the same owner returns the same ID and setup;
another owner receives a different connection and ID. This is an ownership
challenge, not a general secret export.

## Rollout

Before applying infrastructure, populate `gateway_customer_role_arns` in the
platform environment's Terraform inputs with the exact approved customer role
ARNs, including customer routing destinations using the same gateway grant.
Wildcards and the platform's own account are rejected. The default is an
empty list, which removes the gateway's cross-account customer assume grant.
New customer roles therefore require operator approval of their ARN in addition
to the connection ownership proof. Collect all active role ARNs before applying
this change; an incomplete list interrupts those connections.

If `persona_model_probe_destination_enabled` is true, configure a random
`persona_model_probe_external_id` of the form `platform-probe:` plus 32–128
random letters, digits, underscores or hyphens. Configure that same value in the
operator-owned probe destination's linked AWS vault credential (`credential_id`)
before applying its trust policy. The existing routing signer reads `external_id`
from that credential's Secrets Manager value; a destination without a linked
credential cannot satisfy the new condition. Personal connection IDs are UUIDs and cannot satisfy this namespace.
The Terraform value is sensitive but is still present in protected Terraform
state. The platform role is excluded from personal customer registration; the
existing administrator-owned model-routing path remains separate.

Deploy the gateway and migration first, reconnect customer roles, then review
and apply the platform and gateway Terraform plans. Verify customer assumptions
and the platform model probe before promoting the change to another environment.
No Terraform apply is part of this source-remediation task.

Apply migration `082_aws_connection_trust` with the gateway deployment. It leaves
legacy trust IDs NULL, clears verification evidence and marks AWS records pending.
There is deliberately no automatic backfill from old secrets: those values may
have been selected by a caller. Existing connections must be disconnected and
registered again, their trust policy updated, and verified before use. This also
applies to old Quick-Create connections; import the existing role to avoid
recreating the CloudFormation stack. Stop affected worker runs during migration
and reconnect the required accounts before resuming them. Existing AWS sessions
already issued remain valid until their provider expiration or AWS revocation.

Review any routing destinations referencing old connection IDs and relink them
to the newly verified records. Keep the migration on service rollback; dropping
the column is supported only after rolling back the application. Rollback
restores the old security behavior and does not constitute remediation.

Validation uses mocked STS boundaries and a real local PostgreSQL migration.
An operator still needs to verify a complete connection lifecycle with actual
AWS trust policies after deployment. No cloud resources or SSM settings were
changed while preparing this patch.
