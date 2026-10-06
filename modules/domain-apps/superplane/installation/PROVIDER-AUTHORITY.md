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
