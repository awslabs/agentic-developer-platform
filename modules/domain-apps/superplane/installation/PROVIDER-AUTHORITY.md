# Installation-owned provider authority

Refs #7135. Source implementation is separate from infrastructure review,
activation, image qualification and live demo acceptance.

Personal AWS connections continue to reject the platform account. The distinct
`spda1:<canonical-lowercase-UUIDv5>` handle names a protected installation-owned
record, never a `UserCredential`. Reserved-prefix aliases, missing records,
revocation and store outages never fall through to personal delivery. Generic
vault resolution cannot return these handles, even if a colliding row exists.

The protected deployment owner records exact installation, domain/ADP organization,
future workspace and create-request IDs; beneficiary subject, canonical user and
membership selectors; provider ARN/RoleId and policy generation; child boundary
identity; and Secret ARN/VersionId. Workspace identity uses the existing UUIDv5
derivation from the actual create request. Enrollment does not create a workspace,
approval or live identity proof. Gateway establishes current Cognito identity and
SQL membership on every consumption. The record expires and can be conditionally
revoked; revoked identities cannot be silently restored or re-enrolled.

Account admission constructs the existing domain account request from owner-held
material and redacts the role/ExternalId response. Evidence reports installation
ownership and delegation to exactly one workspace through the existing contract.
The refresh-only validation endpoint accepts no caller readings or profile. Its
fixed profile drives EC2 DryRun and quota observations; physical capacity remains
unknown and the report does not prove all native Terraform permissions.

Validation observations are immutable rows in a separate table. One DynamoDB
transaction checks the current authority revision and writes both digest-indexed
and current-generation observations. Gateway can condition/read authority but
cannot mutate it. Reports are bounded to six hours and the authority expiry;
revocation, secret rotation and generation changes invalidate their use.

The existing paid-operation broker alone releases AWS sessions. It retains sealed
target, live worker/run/pod identity, lease/fence, approval deadline and cleanup
restrictions. It re-establishes governed authority around STS, provider reads and
audit commit, checks the returned immutable RoleId and limits sessions to 900
seconds. Revocation cannot revoke an already-issued STS session before its expiry.

All new storage and Gateway IAM definitions live in the app-owned
`infra/provider-authority` child module. The existing webhook Terraform root only
composes it and publishes configuration through its maintained rollout. Enrollment
is a separate conditional owner command after the actual installation/binding is
prepared; storage existence is not enrollment or worker readiness. Tables and
authority history are retained and protected from deletion.

The provider-role policy and mandatory child-role boundary belong to the separate
app-owned provider root. Generated workspace IAM roles need explicit boundary
support in the packaged workspace module and lifecycle configuration. Both API
and worker therefore require genuine new builds and qualification. IAM controls
combine with sealed reviewed native source/plan checks; EKS creation and service
linked role effects cannot be represented as complete IAM-only VPC isolation.
No source test or empty-default infrastructure composition establishes live demo
readiness.

## Policy identity and operator procedure

AWS's inline-role aggregate policy limit cannot fit the fully scoped native
policy. The admitted recipe has exactly four same-account app-owned managed
policies (`network`, `identity`, `lifecycle`, `state-validation`) under the exact
provider role name, with no inline policy or provider boundary. Gateway hashes
the role trust and each policy ARN, current DefaultVersionId and canonical policy
document. Missing, extra or changed attachments/versions refuse. The child-role
boundary ARN and version/document digest are pinned independently. These digests
identify the reviewed policies; they do not replace review of their permissions.

The shared root's optional `domain_provider_authority` input composes storage and
exact Gateway grants. Its default is null. It publishes the verified account as
`ADP_DOMAIN_PROVIDER_ACCOUNT_ID`; the Bedrock service account setting does not
establish installation ownership. The configured public-route bucket must also
belong to this exact account. A real saved plan and owner authorization are
required before applying this module through the existing webhook state; any
active infrastructure deployment hold still applies. Do not create a second
state owner for the module's resources.

After that rollout, use the app command from a reviewed repository checkout:

```bash
PYTHONPATH=modules/domain-apps/superplane python -m installation.provider_authority --document "$PROVIDER_AUTHORITY_DOCUMENT"
```

The private, owner-readable JSON document has exactly `version: 1`, `environment`,
`operator_role_arn`, `operator_role_id`, `gateway_namespace`, `management_cluster`,
`registry_table` and `authority`. The last object uses the closed fields in
`modules/gateway/src/shared/domain_provider_contract.py`; generate its handle and
digests with that maintained contract, using actual independently read metadata.
Do not put SecretString, ExternalId or session credentials in this document.
Preserve the genuine create request and use its original operation ID when later
calling workspace preview/create. Preserve the exact reviewed enrollment document
for recovery and revocation.

The default command checks without writing. It verifies selected caller ARN and
RoleId, current provider/boundary generations, current Secret VersionId, the live
installation route, selected EKS endpoint/CA, the maintained Gateway ConfigMap and
the SSM-selected protected registry pair. It does not claim executable readiness
or validate the beneficiary selectors as a current human. Gateway performs that
separate proof at consumption. Following independent review, `--write` performs
only a conditional insert and exact readback. Retry a lost response with the same
document; differing records are refused rather than adopted.

`--revoke --write` withdraws only that exact original record with a conditional
generation change. It remains usable when the provider or installation has
broken, because withdrawal requires the protected owner and existing record,
not healthy runtime dependencies. Revocation is idempotent and irreversible
through this command. Rotation/re-enrollment of an existing handle is deliberately
unsupported; it requires a separate reviewed lifecycle design.

Tests use fake transports and Terraform mocks. They cover real API account-schema
and credential-evidence consumers, exact workspace delegation, current identity
and policy drift, conditional evidence writes, revocation during provider/audit
I/O, and a valid minted session near its deadline. No test result here claims
cloud provisioning, spend approval, deployed readiness or a working demo. The
existing approval contract also requires a second distinct, genuine human with
current authority; installing provider infrastructure cannot manufacture that
approval.
